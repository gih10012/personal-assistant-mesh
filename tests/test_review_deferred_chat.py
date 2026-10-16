"""Small real SQLite tests of the separately closed owner-deferred candidate."""
import contextlib
import fcntl
import gzip
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from tests import test_reconcile_native_quota as quota_fixtures

loader = importlib.util.spec_from_file_location('review_deferred_chat',
    str(Path(__file__).resolve().parents[1] / 'scripts' / 'review_deferred_chat.py'))
review = importlib.util.module_from_spec(loader)
loader.loader.exec_module(review)


class DeferredChatReviewTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = quota_fixtures.NativeQuotaReconciliationTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        self.root = f.root
        with f.store.transaction() as db:
            row = db.execute('SELECT checkpoint FROM tasks WHERE id=?', (f.task_id,)).fetchone()
            checkpoint = json.loads(row[0]); checkpoint.pop('goal')
            db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (json.dumps(checkpoint), f.task_id))
            row = db.execute('SELECT state FROM native_sessions WHERE scope=?', (f.scope,)).fetchone()
            state = json.loads(row[0]); state.pop('goal')
            db.execute('UPDATE native_sessions SET state=? WHERE scope=?', (json.dumps(state), f.scope))
        self.reviews = self.root / 'reviews'; self.reviews.mkdir(mode=0o700)
        self.backup = self.root / 'existing-backup.sql.gz'
        with contextlib.closing(sqlite3.connect(str(f.database))) as db:
            sql = ('\n'.join(db.iterdump()) + '\n').encode()
        self.backup.write_bytes(gzip.compress(sql)); self.backup.chmod(0o600)
        with contextlib.closing(review.util.quota.readonly(f.database)) as db:
            self.task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (f.task_id,)).fetchone())
            self.session = dict(db.execute('SELECT * FROM native_sessions WHERE scope=?', (f.scope,)).fetchone())
        self.old = {'id': f.old_artifact, 'parts': 1, 'compressed_size': len(f.original_old_chunks),
            'compressed_sha256': review.util.sha(f.original_old_chunks),
            'raw_size': len(f.encode(f.history())), 'raw_sha256': review.util.sha(f.encode(f.history()))}
        now = time.time()
        self.spec = {'protocol': review.PROTOCOL, 'issued_at': now - 1, 'expires_at': now + 900,
            'worker_config': str(f.worker), 'server_config': str(f.server), 'task_id': f.task_id,
            'epoch': 3, 'rollout_path': str(f.rollout), 'old_artifact': self.old,
            'backup_path': str(self.backup), 'review_root': str(self.reviews),
            'local_observer': {'ssh_executable': '/usr/bin/ssh', 'ssh_config': '/synthetic/config',
                               'alias': 'synthetic-peer', 'targets': {'synthetic': 'only'}}}
        self.spec_path = f.private('owner-deferred.json', self.spec)
        pins = dict(review.CLOSED, task=review.digest(self.task), session=review.digest(self.session),
            rollout=review.util.sha(f.original), rollout_size=len(f.original),
            selected=review.util.sha(f.encode(f.failed_turn())), selected_size=len(f.encode(f.failed_turn())),
            old_artifact=review.digest(self.old), backup=review.util.sha(self.backup.read_bytes()),
            backup_size=self.backup.stat().st_size, backup_sql=review.util.sha(sql), backup_sql_size=len(sql),
            closure_targets=review.digest(self.spec['local_observer']['targets']))
        self.pins = patch.object(review, 'CLOSED', pins); self.pins.start(); self.addCleanup(self.pins.stop)
        self.closure = patch.object(review, 'closure', return_value={'synthetic_observer': True})
        self.closure.start(); self.addCleanup(self.closure.stop)

    def approved(self):
        return review.load(self.spec_path)[1]['sha256']

    def test_full_backup_audit_is_nonmutating_and_preserves_existing_backup_bytes(self):
        before = self.fixture.database.read_bytes(), self.backup.read_bytes()
        context = review.inspect(self.spec_path)
        self.assertEqual(context['selected']['task'], self.task)
        self.assertEqual(before, (self.fixture.database.read_bytes(), self.backup.read_bytes()))
        self.assertEqual(list(self.reviews.iterdir()), [])

    def test_only_reviewed_guard_settles_failed_deferred_without_requeue_or_state_rewrite(self):
        result = review.apply(self.spec_path, self.approved())
        self.assertTrue(result['applied'])
        with contextlib.closing(review.util.quota.readonly(self.fixture.database)) as db:
            task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (self.fixture.task_id,)).fetchone())
            state = dict(db.execute('SELECT * FROM native_sessions WHERE scope=?', (self.fixture.scope,)).fetchone())
            old = db.execute('SELECT body FROM session_chunks WHERE id=?', (self.fixture.old_artifact,)).fetchone()[0]
        self.assertEqual(task, review.closed_task(self.task, review.case_id()))
        self.assertEqual(task['result'], self.task['result'])
        self.assertEqual(task['input'], self.task['input'])
        self.assertEqual(state['state'], self.session['state'])
        self.assertEqual(old, self.fixture.original_old_chunks)
        self.assertTrue(review.status(self.spec_path)['applied'])
        with self.assertRaises(review.util.ReviewDenied):
            review.apply(self.spec_path, self.approved())

    def test_changed_code_owned_case_cannot_be_reapproved_by_editing_private_spec(self):
        self.fixture.rollout.write_bytes(self.fixture.original + b'{}\n')
        with self.assertRaisesRegex(review.util.ReviewDenied, 'deferred_full_history_changed'):
            review.inspect(self.spec_path)
        self.assertFalse(list(self.reviews.iterdir()))

    def test_truncated_existing_backup_is_rejected_without_second_backup(self):
        original = self.backup.read_bytes()
        self.backup.write_bytes(original[:-4])
        with self.assertRaisesRegex(review.util.ReviewDenied, 'deferred_backup_changed'):
            review.inspect(self.spec_path)
        self.assertEqual(list(self.reviews.iterdir()), [])

    def test_restore_rejects_external_database_even_when_synthetic_hashes_match(self):
        # Test-only code pins, never an owner-spec admission override.
        target = self.root / 'must-not-create.sqlite'
        sql = ("BEGIN TRANSACTION;\nATTACH DATABASE '" + str(target) + "' AS external;\nCOMMIT;\n").encode()
        raw = gzip.compress(sql); self.backup.write_bytes(raw)
        pins = dict(review.CLOSED, backup=review.util.sha(raw), backup_size=len(raw),
                    backup_sql=review.util.sha(sql), backup_sql_size=len(sql))
        with patch.object(review, 'CLOSED', pins), self.assertRaises(sqlite3.DatabaseError):
            review.inspect(self.spec_path)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.reviews.iterdir()), [])

    def test_failed_cas_keeps_original_task_and_same_unknown_intent(self):
        real = review.inspect
        def raced(path):
            context = real(path)
            with self.fixture.store.transaction() as db:
                db.execute('UPDATE tasks SET result=? WHERE id=?', ('later owner change', self.fixture.task_id))
            return context
        with patch.object(review, 'inspect', side_effect=raced):
            result = review.apply(self.spec_path, self.approved())
        self.assertTrue(result['outcome_unknown'])
        observed = review.status(self.spec_path)
        self.assertTrue(observed['outcome_unknown'])
        self.assertFalse(observed['retry_authorized'])
        with contextlib.closing(review.util.quota.readonly(self.fixture.database)) as db:
            row = db.execute('SELECT status,checkpoint FROM tasks WHERE id=?', (self.fixture.task_id,)).fetchone()
        self.assertEqual(row['status'], 'needs_review')
        self.assertTrue(json.loads(row['checkpoint'])['side_effect_started'])

    def test_lost_post_commit_receipt_ack_uses_original_status_not_second_apply(self):
        real = review.util.write_json
        def failed_ack(path, value):
            if path.name == '07-receipt.json':
                raise OSError('synthetic lost ACK')
            return real(path, value)
        with patch.object(review.util, 'write_json', side_effect=failed_ack):
            result = review.apply(self.spec_path, self.approved())
        self.assertTrue(result['outcome_unknown'])
        status = review.status(self.spec_path)
        self.assertTrue(status['applied'])
        self.assertFalse(status['outcome_unknown'])
        self.assertFalse(status['retry_authorized'])

    def test_fixed_peer_observer_requires_disabled_config_original_queue_and_real_host_lock(self):
        queue = self.root / 'queue.sqlite'; config = self.root / 'owner.json'
        payload = self.root / 'payload.json'; payload.write_text('{}'); payload.chmod(0o600)
        config.write_text(json.dumps({'enabled': False, 'state_dir': str(self.root)})); config.chmod(0o600)
        with contextlib.closing(sqlite3.connect(str(queue))) as db:
            db.execute('CREATE TABLE chat_requests(request_id TEXT,state TEXT,send_intent INTEGER)')
            db.execute("INSERT INTO chat_requests VALUES('original','queued',0)")
            db.commit()
        queue.chmod(0o600)
        lock_path = self.root / 'host.lock'; lock_path.touch(mode=0o600)
        request = {'nonce': 'fresh-synthetic-nonce', 'targets': {'owner_config_path': str(config),
            'queue_path': str(queue), 'payload_path': str(payload), 'request_id': 'original'}}
        def observe():
            return subprocess.run([sys.executable, '-c', review.OBSERVER], input=json.dumps(request).encode(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(observe().returncode, 0)
        descriptor = os.open(str(lock_path), os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(observe().returncode, 2)
        finally:
            os.close(descriptor)
        with contextlib.closing(sqlite3.connect(str(queue))) as db:
            db.execute('UPDATE chat_requests SET send_intent=1')
            db.commit()
        self.assertEqual(observe().returncode, 2)


if __name__ == '__main__':
    unittest.main()
