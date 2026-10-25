import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from assistant_mesh.store import Store
from assistant_mesh import superseded_notice as notice_scope


SPEC = importlib.util.spec_from_file_location('review_superseded_notice',
    str(Path(__file__).resolve().parents[1] / 'scripts' / 'review_superseded_notice.py'))
notice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notice)


class SupersededNoticeReviewTests(unittest.TestCase):
    """Synthetic single-row fixtures; no production/native/account access."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'profile'
        self.home.mkdir(mode=0o700)
        self.task_id, self.other_id = 'a' * 64, 'b' * 64
        self.thread = '01a10000-0000-7000-a000-000000000001'
        self.turn = '01a10000-0000-7000-a000-000000000002'
        self.text = 'Synthetic old AA relay: no configuration or Goal changes.'
        self.worker = self.private('worker.json', {'node_id': 'fixture', 'capabilities': ['agent'],
            'codex': {'auth_home': str(self.home), 'executable': str(self.root / 'native')},
            'codex_accounts': [{'auth_home': str(self.home)}]})
        self.database = self.root / 'ledger.sqlite'
        self.store = Store(self.database)
        self.server = self.private('server.json', {'node_id': 'fixture', 'database': str(self.database),
            'peers': [{'role': 'operator'}, {'role': 'worker', 'node': 'fixture'}]})
        self.private('profile/auth.json', {'private': 'synthetic-secret'})
        self.private('profile/config.toml', 'synthetic-original-profile')
        self.private('native', 'synthetic-executable-not-run')
        directory = self.home
        for part in ('sessions', '2026', '10', '25'):
            directory = directory / part
            directory.mkdir(mode=0o700)
        self.rollout = directory / ('rollout-2026-10-25T00-00-00-' + self.thread + '.jsonl')
        self.rollout.write_text(json.dumps({'type': 'session_meta',
            'payload': {'id': self.thread, 'timestamp': '2026-10-25T00:00:00Z'}}) + '\n' +
            'PRIVATE OLD UNKNOWN HISTORY IS NOT INTERPRETED\n')
        self.rollout.chmod(0o600)
        self.checkpoint = json.dumps({'harness': 'codex', 'thread_id': self.thread,
            'turn_id': self.turn, 'codex_auth_home': str(self.home), 'codex_node': 'fixture',
            'side_effect_started': True, 'retained_private_state': 'preserve-original-bytes',
            'runtime_failure': {'phase': 'native_finish', 'category': 'codex_disconnected',
                                'attempt_epoch': 1, 'outcome': 'unknown'}}, indent=2)
        self.session_state = ('{"thread_id":"' + self.thread + '", "codex_auth_home":' +
            json.dumps(str(self.home)) + ',"codex_node":"fixture","native-private":"retain"}')
        with self.store.transaction() as db:
            db.execute('''INSERT INTO tasks(id,input,required,status,node,epoch,checkpoint,result,
                created,scope,context) VALUES(?,?,?,'needs_review',NULL,1,?,?,0,'leader:owner',?)''',
                (self.task_id, self.text, '["leader"]', self.checkpoint, 'original native failure',
                 json.dumps({'origin': {'kind': 'entrance-handoff', 'message_id': self.task_id,
                                        'event_id': self.task_id}})))
            db.execute('''INSERT INTO tasks(id,input,required,status,epoch,checkpoint,created,scope)
                VALUES(?,'unrelated','["agent"]','needs_review',7,?,0,'other:scope')''',
                (self.other_id, '{"side_effect_started":true}'))
            db.execute('INSERT INTO native_sessions VALUES(?,?,?,?,?,?)',
                ('leader:owner', 'fixture', 'codex', self.session_state, 'c' * 64, 123))
            db.execute('INSERT INTO session_chunks VALUES(?,?,?)', ('c' * 64, 0, b'PRIVATE OLD CHUNK'))
        pins = {key: notice.review.sha(value.encode()) for key, value in
                {'task': self.task_id, 'input': self.text, 'thread': self.thread,
                 'turn': self.turn, 'auth_home': str(self.home)}.items()}
        self.pin_patch = patch.object(notice, 'CLOSED_CASE', pins)
        self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)
        self.scope_pin_patch = patch.object(notice_scope, 'CLOSED_CASE', pins)
        self.scope_pin_patch.start()
        self.addCleanup(self.scope_pin_patch.stop)
        self.service_patch = patch.object(notice.subprocess, 'run', return_value=SimpleNamespace(
            returncode=0, stdout='MainPID=0\nActiveState=inactive\nSubState=dead\n'))
        self.service = self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.native_patch = patch.object(notice, 'native_metadata', side_effect=self.native_result)
        self.native = self.native_patch.start()
        self.addCleanup(self.native_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def private(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def native_result(self, executable, auth_home, thread_id, rollout):
        self.assertEqual(str(self.home), auth_home)
        self.assertEqual(str(self.root / 'native'), executable)
        self.assertEqual(self.thread, thread_id)
        return {'thread_id': thread_id, 'path': str(rollout), 'status': {'type': 'notLoaded'}, 'goal': None}

    def run_review(self, **kwargs):
        return notice.close_notice(str(self.worker), str(self.server), self.task_id,
                                   str(self.rollout), **kwargs)

    def rows(self, table):
        with contextlib.closing(sqlite3.connect(str(self.database))) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]

    def tables(self):
        with contextlib.closing(sqlite3.connect(str(self.database))) as db:
            return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    def test_readonly_cli_binds_native_read_methods_and_retains_unknown(self):
        before = {table: self.rows(table) for table in self.tables()}
        history = self.rollout.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = notice.main(['--worker-config', str(self.worker), '--server-config', str(self.server),
                                '--task-id', self.task_id, '--rollout-path', str(self.rollout)])
        self.assertEqual(0, code)
        result = json.loads(output.getvalue())
        self.assertFalse(result['applied'])
        self.assertEqual('unknown', result['native_outcome'])
        self.assertTrue(result['side_effect_started'])
        self.assertFalse(result['effects_zero_verified'])
        self.assertFalse(result['notice_delivery_verified'])
        self.assertEqual({'initialize', 'thread/read', 'thread/goal/get'},
                         notice.NoticeMetadataObserver.READ_METHODS)
        for forbidden in ('synthetic-secret', 'PRIVATE', 'retained_private_state', str(self.home)):
            self.assertNotIn(forbidden, output.getvalue())
        self.assertEqual(before, {table: self.rows(table) for table in self.tables()})
        self.assertEqual(history, self.rollout.read_bytes())
        self.service.assert_called_once_with(['/usr/bin/systemctl', '--user', 'show', notice.SERVICE, '--no-pager',
            '--property=ActiveState,SubState,MainPID'], stdout=notice.subprocess.PIPE,
            stderr=notice.subprocess.DEVNULL, universal_newlines=True, timeout=10)

        # Exercise the real small metadata function with a fake protocol peer.
        self.native_patch.stop()
        self.addCleanup(self.native_patch.start)
        calls = []
        class Peer:
            def __init__(peer, config, thread, tick, deadline):
                calls.append(('initialize', config, thread))
            def read_thread(peer, path, include_turns):
                calls.append(('thread/read', path, include_turns))
                return {'id': self.thread, 'path': path, 'status': {'type': 'notLoaded'}}
            def rpc(peer, method, params):
                calls.append((method, params))
                return {'goal': None}
            def close(peer):
                calls.append(('close',))
        with patch.object(notice, 'NoticeMetadataObserver', Peer):
            self.assertIsNone(notice.native_metadata(str(self.root / 'native'), str(self.home),
                                                    self.thread, str(self.rollout))['goal'])
        self.assertEqual(['initialize', 'thread/read', 'thread/goal/get', 'close'],
                         [call[0] for call in calls])

    def test_apply_changes_one_task_preserves_guards_sessions_and_rejects_repeat(self):
        before = {table: self.rows(table) for table in self.tables()}
        audit = self.run_review()
        result = self.run_review(apply=True, expected_epoch=1,
                                 expected_review_digest=audit['review_digest'])
        self.assertTrue(result['applied'])
        self.assertEqual(('failed', 'deferred', 'superseded', 'unknown'),
            tuple(result[k] for k in ('status', 'disposition', 'resolution', 'native_outcome')))
        expected = dict(before['tasks'][0], status='failed', epoch=2,
                        result=self.rows('tasks')[0]['result'])
        self.assertEqual(expected, self.rows('tasks')[0])
        self.assertEqual(before['tasks'][1], self.rows('tasks')[1])
        self.assertEqual(self.checkpoint, self.rows('tasks')[0]['checkpoint'])
        self.assertTrue(json.loads(self.rows('tasks')[0]['checkpoint'])['side_effect_started'])
        for table, rows in before.items():
            if table != 'tasks':
                self.assertEqual(rows, self.rows(table), table)
        review_row = self.rows(notice.TABLE)[0]
        self.assertEqual(before['tasks'][0], json.loads(review_row['before_task']))
        self.assertEqual(before['native_sessions'][0], json.loads(review_row['before_session']))
        self.assertEqual(1, review_row['source_epoch'])
        self.assertEqual(2, review_row['closed_epoch'])
        self.native.reset_mock()
        with self.assertRaisesRegex(notice.review.ReviewDenied, 'notice_already_reviewed'):
            self.run_review(apply=True, expected_epoch=1, expected_review_digest=audit['review_digest'])
        self.native.assert_not_called()
        self.assertEqual(1, len(self.rows(notice.TABLE)))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as db:
                db.execute('DELETE FROM superseded_notice_reviews')

    def test_wrong_case_epoch_digest_and_active_boundaries_deny_without_closure(self):
        with self.assertRaisesRegex(notice.review.ReviewDenied, 'closed_notice_task_mismatch'):
            notice.close_notice(str(self.worker), str(self.server), self.other_id, str(self.rollout))
        for kwargs, code in (({'apply': True}, 'apply_explicit_epoch_required'),
                ({'expected_epoch': 2}, 'closed_notice_expected_epoch_mismatch'),
                ({'apply': True, 'expected_epoch': 1}, 'apply_explicit_review_digest_required'),
                ({'apply': True, 'expected_epoch': 1, 'expected_review_digest': '0' * 64},
                 'review_digest_changed')):
            with self.subTest(code=code), self.assertRaisesRegex(notice.review.ReviewDenied, code):
                self.run_review(**kwargs)
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET input=? WHERE id=?', ('changed old input', self.task_id))
        with self.assertRaisesRegex(notice.review.ReviewDenied, 'closed_notice_input_mismatch'):
            self.run_review()
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET input=? WHERE id=?', (self.text, self.task_id))
        self.service.return_value.stdout = 'MainPID=7\nActiveState=active\nSubState=running\n'
        self.native.reset_mock()
        with self.assertRaisesRegex(notice.review.ReviewDenied, 'worker_service_must_be_inactive_pid0'):
            self.run_review()
        self.native.assert_not_called()
        self.service.return_value.stdout = 'MainPID=0\nActiveState=inactive\nSubState=dead\n'
        with patch.object(notice, 'native_metadata', return_value={'goal': {'status': 'active'}}):
            with self.assertRaisesRegex(notice.review.ReviewDenied, 'original_native_metadata_changed'):
                self.run_review()
        for sql, args, cleanup, code in (
            ("UPDATE tasks SET scope='leader:owner',status='running' WHERE id=?", (self.other_id,),
             "UPDATE tasks SET scope='other:scope',status='needs_review' WHERE id=?", 'scope_has_running_task'),
            ('UPDATE tasks SET parent_id=? WHERE id=?', (self.task_id, self.other_id),
             'UPDATE tasks SET parent_id=NULL WHERE id=?', 'closed_notice_has_children'),
            ('INSERT INTO agent_actions VALUES(?,?,?)', (self.task_id + '-child-action', 'x', '{}'),
             'DELETE FROM agent_actions WHERE id=?', 'closed_notice_has_agent_actions')):
            with self.subTest(code=code):
                with self.store.transaction() as db:
                    db.execute(sql, args)
                with self.assertRaises((notice.review.ReviewDenied, notice.quota.AuditDenied)):
                    self.run_review()
                with self.store.transaction() as db:
                    db.execute(cleanup, (args[0] if 'agent_actions' in cleanup else self.other_id,))
        self.assertNotIn(notice.TABLE, self.tables())
        self.assertEqual(1, self.rows('tasks')[0]['epoch'])

    def test_reviewed_notice_releases_only_its_reservation_and_keeps_raw_guard(self):
        self.store.heartbeat('fixture', ['leader', 'agent'])
        pending = self.store.create_task('new owner request', ['leader'],
            context={'session_scope': 'leader:owner'})
        self.assertIsNone(self.store.claim('fixture'))
        audit = self.run_review()
        self.run_review(apply=True, expected_epoch=1, expected_review_digest=audit['review_digest'])
        self.assertIsNone(self.store.claim('fixture'))
        adapted = notice_scope.make_store_class(Store)(self.database, recover_inflight=False)
        claimed = adapted.claim('fixture')
        self.assertEqual(pending, claimed['id'])
        self.assertNotEqual(self.task_id, claimed['id'])
        with adapted.transaction() as db:
            original = db.execute('SELECT checkpoint FROM tasks WHERE id=?', (self.task_id,)).fetchone()[0]
            self.assertEqual(self.checkpoint, original)
            # Ordinary reads still see the UNKNOWN guard; only claim's exact
            # reservation lookup can use the owner adjudication.
            raw = db.execute(next(q for q in notice_scope.RESERVATION_QUERIES
                                 if q.startswith('SELECT checkpoint ')), ('leader:owner', pending)).fetchall()
            self.assertTrue(any(json.loads(r['checkpoint']).get('side_effect_started') for r in raw))
            db.execute("UPDATE tasks SET status='completed' WHERE id=?", (pending,))
            db.execute('UPDATE native_sessions SET updated=updated+1')
        another = adapted.create_task('later owner request', ['leader'],
            context={'session_scope': 'leader:owner'})
        self.assertEqual(another, adapted.claim('fixture')['id'])
        # Other scopes and their unresolved attempts are untouched.
        blocked = adapted.create_task('other scope request', ['agent'],
            context={'session_scope': 'other:scope'})
        self.assertIsNone(adapted.claim('fixture'))
        self.assertEqual('pending', next(r['status'] for r in self.rows('tasks') if r['id'] == blocked))
        self.assertTrue(json.loads(next(r['checkpoint'] for r in self.rows('tasks')
                                       if r['id'] == self.task_id))['side_effect_started'])

    def test_reservation_receipt_mismatch_and_legacy_query_keep_fail_closed_behavior(self):
        audit = self.run_review()
        self.run_review(apply=True, expected_epoch=1, expected_review_digest=audit['review_digest'])
        with self.store.transaction() as db:
            self.assertEqual(self.task_id, notice_scope.reviewed_task(db))
            cursor = notice_scope._ClaimConnection(db).execute(
                "SELECT checkpoint FROM tasks WHERE scope=? AND id<>? AND status<>'completed'",
                ('leader:owner', self.other_id))
            self.assertEqual([], cursor.fetchall())
            db.execute('UPDATE tasks SET result=? WHERE id=?', ('tampered receipt', self.task_id))
            self.assertIsNone(notice_scope.reviewed_task(db))
            cursor = notice_scope._ClaimConnection(db).execute(
                "SELECT checkpoint FROM tasks WHERE scope=? AND id<>? AND status<>'completed'",
                ('leader:owner', self.other_id))
            self.assertTrue(json.loads(cursor.fetchone()['checkpoint'])['side_effect_started'])

    def test_task_session_and_source_generation_conflicts_never_close_or_replay(self):
        audit = self.run_review()
        with self.store.transaction() as db:
            db.execute('UPDATE native_sessions SET updated=updated+1')
        with self.assertRaisesRegex(notice.review.ReviewDenied, 'review_digest_changed'):
            self.run_review(apply=True, expected_epoch=1, expected_review_digest=audit['review_digest'])
        # Inject a real separate-connection row change immediately before the
        # production transaction. There is no user/CLI callback parameter.
        original_transaction = Store.transaction
        @contextlib.contextmanager
        def raced_transaction(store):
            with original_transaction(self.store) as other:
                other.execute('UPDATE tasks SET result=? WHERE id=?', ('raced-private-result', self.task_id))
            with original_transaction(store) as db:
                yield db
        fresh = self.run_review()
        with patch.object(Store, 'transaction', raced_transaction):
            with self.assertRaisesRegex(notice.review.ReviewDenied, 'task_or_session_cas_changed'):
                self.run_review(apply=True, expected_epoch=1, expected_review_digest=fresh['review_digest'])
        # Profile generation change during the native query must also reject.
        def changed_profile(*args):
            (self.home / 'config.toml').write_text('changed-original-generation')
            return self.native_result(*args)
        with patch.object(notice, 'native_metadata', side_effect=changed_profile):
            with self.assertRaisesRegex(notice.review.ReviewDenied, 'review_source_generation_changed'):
                self.run_review()
        self.assertNotIn(notice.TABLE, self.tables())
        self.assertEqual(('needs_review', 1, self.checkpoint),
                         tuple(self.rows('tasks')[0][k] for k in ('status', 'epoch', 'checkpoint')))
        self.assertEqual(self.session_state, self.rows('native_sessions')[0]['state'])
        self.assertEqual(b'PRIVATE OLD CHUNK', self.rows('session_chunks')[0]['body'])


if __name__ == '__main__':
    unittest.main()
