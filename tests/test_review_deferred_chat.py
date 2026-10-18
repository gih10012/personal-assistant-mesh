"""Small real SQLite tests of the separately closed owner-deferred candidate."""
import contextlib
import fcntl
import gzip
import importlib.util
import json
import os
from pathlib import Path
import signal
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
RealHeldClosure = review.HeldClosure


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
        class SyntheticGuard:
            nonce = 'synthetic-only-nonce'
            latest = {'synthetic_observer': True, 'nonce': nonce, 'pid': 12345, 'sequence': 0}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def check(self): return self.latest
            def commit_authorizer(self, *args): return sqlite3.SQLITE_OK
            def release(self): return {'phase': 'released', 'nonce': self.nonce, 'pid': 12345, 'sequence': 1}
        self.held = patch.object(review, 'HeldClosure', side_effect=lambda spec: SyntheticGuard())
        self.held.start(); self.addCleanup(self.held.stop)

    @contextlib.contextmanager
    def actual_peer_guard(self, mutate_guard=None):
        """Real original host lock and observer process, zero SSH/Chat/model IO."""
        from assistant_mesh import chat_bridge_queue as original
        config = {'schema': 1, 'enabled': True, 'owner_id': 'synthetic-owner',
            'state_dir': str(self.root), 'expected_account': 'synthetic@163.com',
            'projects': {'project': 'https://chatgpt.com/c/01000000-0000-7000-8000-000000000001'},
            'dom': {'completion_selector': '.done', 'account_selector': '.account',
                    'account_attribute': 'textContent'}}
        cached = original.ChatBridgeQueue(config)
        cached.enqueue({'request_id': 'original', 'task_id': 'original-bridge-task',
                        'project_id': 'project', 'input': 'public synthetic fixture'})
        owner = self.root / 'old-host-owner.json'
        owner.write_text(json.dumps(dict(config, enabled=False))); owner.chmod(0o600)
        payload = self.root / 'original-chat-payload.json'
        payload.write_text('{}'); payload.chmod(0o600)
        (self.root / 'host.lock').touch(mode=0o600)
        targets = {'owner_config_path': str(owner), 'queue_path': str(cached.database),
                   'payload_path': str(payload), 'request_id': 'original'}
        with contextlib.closing(sqlite3.connect(str(cached.database))) as db:
            db.row_factory = sqlite3.Row
            row = dict(db.execute('SELECT * FROM chat_requests').fetchone())
        self.spec['local_observer']['targets'] = targets
        self.fixture.private('owner-deferred.json', self.spec)
        pins = dict(review.CLOSED, closure_targets=review.digest(targets),
            disabled_config=review.util.sha(owner.read_bytes()), queue_row=review.digest(row),
            payload=review.util.sha(payload.read_bytes()))
        guards = []
        def factory(spec):
            guard = RealHeldClosure(spec)
            guards.append(guard)
            if mutate_guard:
                mutate_guard(guard, cached)
            return guard
        with patch.object(review, 'CLOSED', pins), \
                patch.object(review, 'observer_command', side_effect=lambda spec, code: [sys.executable, '-c', code]), \
                patch.object(review, 'HeldClosure', side_effect=factory):
            try:
                yield guards, cached
            finally:
                # Synthetic child cleanup AFTER the test knows its own DB
                # outcome. Production never auto-kills/releases an unknown hold.
                for guard in guards:
                    if guard.process is not None:
                        if guard.process.poll() is None:
                            os.kill(guard.process.pid, signal.SIGKILL)
                            guard.process.wait(timeout=5)
                        for stream in (guard.process.stdin, guard.process.stdout):
                            stream.close()

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

    def test_real_owned_lock_covers_cas_commit_and_status_before_explicit_release(self):
        from assistant_mesh import chat_bridge_queue as original
        import io
        release_checks = []
        def mutate(guard, cached):
            real_release = guard.release
            def release():
                # Actual same-case readback has committed, and an old host
                # caching enabled=True still cannot acquire its original lock.
                release_checks.append(review.status(self.spec_path)['applied'])
                with self.assertRaisesRegex(Exception, 'chat_host_already_owned'):
                    original.native_host(cached, io.BytesIO(), io.BytesIO())
                self.assertTrue(cached.config['enabled'])
                self.assertFalse(cached.status('original')['send_intent'])
                return real_release()
            guard.release = release
        with self.actual_peer_guard(mutate) as (guards, cached):
            result = review.apply(self.spec_path, self.approved())
            self.assertTrue(result['applied'])
            self.assertTrue(result['peer_lock_release_verified'])
            self.assertEqual([True], release_checks)
            self.assertTrue(guards[0].released)
            self.assertEqual(0, guards[0].process.returncode)
            self.assertFalse(cached.status('original')['send_intent'])
            self.assertTrue(review.status(self.spec_path)['peer_lock_release_verified'])

    def test_actual_observer_death_at_commit_admission_rolls_back_original_guard(self):
        def mutate(guard, unused):
            authorizer = guard.commit_authorizer
            def die_at_commit(*args):
                if args[0] == getattr(sqlite3, 'SQLITE_TRANSACTION', 22) and args[1] == 'COMMIT':
                    os.kill(guard.process.pid, signal.SIGKILL)
                    guard.process.wait(timeout=5)
                return authorizer(*args)
            guard.commit_authorizer = die_at_commit
        with self.actual_peer_guard(mutate) as (guards, cached):
            result = review.apply(self.spec_path, self.approved())
            self.assertTrue(result['outcome_unknown'])
            self.assertFalse(guards[0].released)
            with contextlib.closing(review.util.quota.readonly(self.fixture.database)) as db:
                task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (self.fixture.task_id,)).fetchone())
            self.assertEqual(self.task, task)
            self.assertFalse(cached.status('original')['send_intent'])

    def test_transport_eof_keeps_real_peer_lock_and_cannot_admit_commit(self):
        with self.actual_peer_guard() as (guards, cached):
            guard = review.HeldClosure(self.spec).__enter__()
            guard.process.stdin.close()  # a lost transport is not release
            with contextlib.closing(sqlite3.connect(':memory:', isolation_level=None)) as db:
                db.execute('CREATE TABLE marker(value INTEGER)')
                db.execute('INSERT INTO marker VALUES(0)')
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE marker SET value=1')
                db.set_authorizer(guard.commit_authorizer)
                with self.assertRaises(sqlite3.DatabaseError):
                    db.commit()
                db.rollback()
                self.assertEqual(0, db.execute('SELECT value FROM marker').fetchone()[0])
            lock = os.open(str(self.root / 'host.lock'), os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIsNone(guard.process.poll())
            finally:
                os.close(lock)
            self.assertFalse(cached.status('original')['send_intent'])


if __name__ == '__main__':
    unittest.main()
