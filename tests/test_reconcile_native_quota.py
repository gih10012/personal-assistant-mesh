import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.store import Store


SPEC = importlib.util.spec_from_file_location('reconcile_native_quota',
    str(Path(__file__).resolve().parents[1] / 'scripts' / 'reconcile_native_quota.py'))
quota = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quota)


class NativeQuotaReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.thread = '01a11b4b-a008-7670-b172-ee8d02ab3fae'
        self.turn = '01a11b74-271f-7852-bb37-523928cc238d'
        self.old_turn = '01a11b4b-b440-7de1-a1a1-bffb508ffed4'
        self.task_id = 'a' * 64
        self.sibling_id = 'b' * 64
        self.scope = 'project:continuous:researcher'
        self.home = self.root / 'auth'
        self.rollout = self.home / 'sessions/2026/10/08' / (
            'rollout-2026-10-08T11-35-08-' + self.thread + '.jsonl')
        # pathlib's parents=True uses default permissions for intermediate
        # directories; create every fixture ancestor privately under any umask.
        fixture_directory = self.root
        for part in ('auth', 'sessions', '2026', '10', '08'):
            fixture_directory = fixture_directory / part
            fixture_directory.mkdir(mode=0o700)
        self.database = self.root / 'ledger.sqlite'
        self.store = Store(self.database)
        self.worker = self.private('worker.json', {'node_id': 'laptop', 'capabilities': ['agent'],
            'codex': {'auth_home': str(self.home)}, 'codex_accounts': [{'auth_home': str(self.home)}]})
        self.server = self.private('server.json', {'node_id': 'laptop', 'database': str(self.database),
            'peers': [{'role': 'operator'}, {'role': 'worker', 'node': 'laptop'}]})
        self.checkpoint = {'thread_id': self.thread, 'turn_id': self.turn, 'harness': 'codex',
            'codex_auth_home': str(self.home), 'codex_node': 'laptop', 'side_effect_started': True,
            'goal': {'objective': 'preserve original goal'}, 'retained_custom_state': {'value': 42}}
        self.state = dict(self.checkpoint, turn_id=self.old_turn, side_effect_started=False)
        self.old_artifact = 'c' * 64
        self.records = self.history() + self.failed_turn()
        self.write_rollout()
        self.original = self.rollout.read_bytes()
        self.original_old_chunks = gzip.compress(self.encode(self.history()))
        with self.store.transaction() as db:
            db.execute('''INSERT INTO tasks(id,input,required,status,node,epoch,checkpoint,result,created,scope)
                VALUES(?,?,?,'needs_review',NULL,3,?,'previous failure',0,?)''',
                (self.task_id, 'readonly recall', '["agent"]', json.dumps(self.checkpoint), self.scope))
            db.execute('''INSERT INTO tasks(id,input,required,status,node,epoch,checkpoint,result,created,scope)
                VALUES(?,?,?,'needs_review',NULL,5,?,'untouched sibling',1,?)''',
                (self.sibling_id, 'different job', '["agent"]', json.dumps({'side_effect_started': True}),
                 'project:unrelated:writer'))
            db.execute('INSERT INTO native_sessions VALUES(?,?,?,?,?,?)',
                (self.scope, 'laptop', 'codex', json.dumps(self.state), self.old_artifact, 123))
            db.execute('INSERT INTO session_chunks VALUES(?,?,?)',
                (self.old_artifact, 0, self.original_old_chunks))

    def tearDown(self):
        self.temp.cleanup()

    def private(self, name, body):
        path = self.root / name
        path.write_text(json.dumps(body))
        path.chmod(0o600)
        return path

    def history(self):
        return [
            {'type': 'session_meta', 'payload': {'id': self.thread,
                'timestamp': '2026-10-08T11:35:08.437Z', 'history_mode': 'paginated',
                'base_instructions': 'PRIVATE ORIGINAL INSTRUCTIONS'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': self.old_turn}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'historic_tool',
                'arguments': 'PRIVATE HISTORIC CALL'}},
            {'type': 'response_item', 'payload': {'type': 'reasoning',
                'summary': 'HIDDEN HISTORIC REASONING NEVER OUTPUT'}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': self.old_turn,
                'last_agent_message': 'ACK'}},
        ]

    def failed_turn(self):
        return [
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': self.turn}},
            {'type': 'turn_context', 'payload': {'turn_id': self.turn, 'sandbox_policy': {'type': 'danger-full-access'}}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'PRIVATE USER REQUEST'}]}},
            {'type': 'response_item', 'payload': {'type': 'reasoning',
                'summary': 'HIDDEN SELECTED REASONING NEVER OUTPUT'}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': self.thread,
                'turn_id': self.turn, 'item': {'type': 'UserMessage', 'content': 'PRIVATE USER REQUEST'}}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {}}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': self.turn,
                'last_agent_message': None, 'error': {'codex_error_info': 'usage_limit_exceeded',
                    'message': 'PRIVATE PROVIDER DETAIL BEARER secret'}}},
        ]

    def encode(self, records):
        return ''.join(json.dumps(row) + '\n' for row in records).encode()

    def write_rollout(self):
        self.rollout.write_bytes(self.encode(self.records))
        self.rollout.chmod(0o600)

    def reconcile(self, **kwargs):
        return quota.reconcile(str(self.worker), str(self.server), self.task_id,
                               str(self.rollout), **kwargs)

    def rows(self, table):
        with contextlib.closing(sqlite3.connect(str(self.database))) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM ' + table)]

    def captured(self):
        auth = quota.authority(str(self.worker), str(self.server))
        with contextlib.closing(quota.readonly(self.database)) as db:
            selected = quota.snapshot(db, self.task_id, 3, auth)
        proof = quota.inspect_rollout(self.rollout, selected)
        return auth, selected, proof

    def test_default_audit_has_no_database_or_native_history_mutation(self):
        before = {table: self.rows(table) for table in ('tasks', 'native_sessions', 'session_chunks', 'leader')}
        value = self.reconcile()
        self.assertTrue(value['eligible'])
        self.assertFalse(value['applied'])
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), value['rollout_sha256'])
        for table, rows in before.items():
            self.assertEqual(rows, self.rows(table))
        self.assertEqual(self.original, self.rollout.read_bytes())
        with contextlib.closing(sqlite3.connect(str(self.database))) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='native_quota_reconciliations'").fetchone())

    def test_apply_publishes_whole_history_without_overwrite_and_releases_only_original_task(self):
        before_tasks, before_session, leader = self.rows('tasks'), self.rows('native_sessions')[0], self.rows('leader')
        value = self.reconcile(apply=True, expected_epoch=3)
        rows = {row['id']: row for row in self.rows('tasks')}
        self.assertEqual('pending', rows[self.task_id]['status'])
        self.assertEqual(4, rows[self.task_id]['epoch'])
        expected = dict(self.checkpoint, side_effect_started=False)
        self.assertEqual(expected, json.loads(rows[self.task_id]['checkpoint']))
        self.assertEqual(before_tasks[1], rows[self.sibling_id])
        self.assertEqual(leader, self.rows('leader'))
        session = self.rows('native_sessions')[0]
        self.assertEqual(expected, json.loads(session['state']))
        self.assertEqual(self.thread, json.loads(session['state'])['thread_id'])
        self.assertEqual(self.turn, json.loads(session['state'])['turn_id'])
        chunks = self.rows('session_chunks')
        self.assertEqual(self.original_old_chunks, next(row['body'] for row in chunks if row['id'] == self.old_artifact))
        packed = b''.join(row['body'] for row in sorted(chunks, key=lambda row: row['part']) if row['id'] == value['artifact'])
        self.assertEqual(self.original, gzip.decompress(packed))
        self.assertEqual(self.original, self.rollout.read_bytes())
        audit = self.rows('native_quota_reconciliations')[0]
        self.assertEqual(before_session, json.loads(audit['before_session']))
        self.assertEqual(before_tasks[0]['checkpoint'], json.loads(audit['before_task'])['checkpoint'])
        self.assertEqual('needs_review', json.loads(audit['before_task'])['status'])
        self.assertEqual(self.old_artifact, audit['old_artifact'])
        self.assertEqual('usage_limit_exceeded', audit['error_category'])

    def test_apply_requires_explicit_epoch_and_refuses_wrong_epoch(self):
        with self.assertRaisesRegex(quota.AuditDenied, 'apply_expected_epoch_required'):
            self.reconcile(apply=True)
        with self.assertRaisesRegex(quota.AuditDenied, 'task_epoch_changed'):
            self.reconcile(apply=True, expected_epoch=2)

    def test_no_auth_file_or_token_read_and_no_private_content_output(self):
        self.assertFalse((self.home / 'auth.json').exists())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = quota.main(['--worker-config', str(self.worker), '--server-config', str(self.server),
                '--task-id', self.task_id, '--rollout-path', str(self.rollout)])
        self.assertEqual(0, code)
        for forbidden in ('PRIVATE', 'HIDDEN', 'BEARER', 'secret', 'auth_home', 'base_instructions'):
            self.assertNotIn(forbidden, output.getvalue())

    def test_every_native_call_or_unknown_response_item_denies_even_quota_terminal(self):
        for kind in ('function_call', 'custom_tool_call', 'local_shell_call', 'mcp_tool_call',
                     'function_call_output', 'computer_call', 'new_future_call'):
            self.records = self.history() + self.failed_turn()
            self.records.insert(-1, {'type': 'response_item', 'payload': {'type': kind}})
            self.write_rollout()
            with self.subTest(kind=kind), self.assertRaisesRegex(quota.AuditDenied, 'native_effect_or_unknown'):
                self.reconcile(apply=True, expected_epoch=3)
        self.assertEqual('needs_review', self.rows('tasks')[0]['status'])

    def test_event_execution_unknown_item_and_unknown_record_deny(self):
        unsafe = [
            {'type': 'event_msg', 'payload': {'type': 'exec_command_begin'}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'CommandExecution'}}},
            {'type': 'event_msg', 'payload': {'type': 'future_effect_event'}},
            {'type': 'compacted', 'payload': {'replacement_history': []}},
        ]
        for record in unsafe:
            self.records = self.history() + self.failed_turn()
            self.records.insert(-1, record)
            self.write_rollout()
            with self.subTest(record=record['type']), self.assertRaises(quota.AuditDenied):
                self.reconcile()

    def test_quota_alone_does_not_prove_no_final_or_no_effects(self):
        for replacement in ('answer', ''):
            self.records[-1]['payload']['last_agent_message'] = replacement
            self.write_rollout()
            with self.assertRaisesRegex(quota.AuditDenied, 'unexpected_final_answer'):
                self.reconcile()
        self.records[-1]['payload']['last_agent_message'] = None
        self.records[-1]['payload']['error']['codex_error_info'] = 'response_stream_disconnected'
        self.write_rollout()
        with self.assertRaisesRegex(quota.AuditDenied, 'not_proven_quota_failure'):
            self.reconcile()

    def test_missing_completion_duplicate_turn_or_later_turn_denies(self):
        for records in (self.records[:-1], self.records + [self.failed_turn()[0]],
                        self.records + [{'type': 'event_msg', 'payload': {'type': 'task_started',
                                         'turn_id': self.old_turn}}]):
            self.records = records
            self.write_rollout()
            with self.assertRaises(quota.AuditDenied):
                self.reconcile()

    def test_mismatched_turn_metadata_and_noncanonical_path_deny(self):
        self.records[-2]['payload']['turn_id'] = self.old_turn
        self.write_rollout()
        with self.assertRaisesRegex(quota.AuditDenied, 'selected_turn_identity_mismatch'):
            self.reconcile()
        self.records = self.history() + self.failed_turn()
        self.write_rollout()
        wrong = self.rollout.with_name('wrong-' + self.rollout.name)
        wrong.write_bytes(self.original)
        wrong.chmod(0o600)
        with self.assertRaisesRegex(quota.AuditDenied, 'canonical_rollout_path_required'):
            quota.reconcile(str(self.worker), str(self.server), self.task_id, str(wrong))

    def test_unlisted_auth_home_and_different_scope_session_deny(self):
        worker = json.loads(self.worker.read_text())
        other = self.root / 'other-auth'
        other.mkdir(mode=0o700)
        worker['codex_accounts'] = [{'auth_home': str(other)}]
        self.private('worker.json', worker)
        with self.assertRaisesRegex(quota.AuditDenied, 'auth_home_not_in_roster'):
            self.reconcile()
        worker['codex_accounts'] = [{'auth_home': str(self.home)}]
        self.private('worker.json', worker)
        with self.store.transaction() as db:
            db.execute('UPDATE native_sessions SET state=?', (json.dumps(dict(self.state, thread_id=self.old_turn)),))
        with self.assertRaisesRegex(quota.AuditDenied, 'scope_session_identity_mismatch'):
            self.reconcile()

    def test_permissions_symlinks_and_unowned_paths_deny(self):
        self.worker.chmod(0o644)
        with self.assertRaisesRegex(quota.AuditDenied, 'private_0600_file_required'):
            self.reconcile()
        self.worker.chmod(0o600)
        alias = self.root / 'alias'
        alias.symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(quota.AuditDenied, 'symlink_path_denied'):
            quota.checked_path(str(alias / 'sessions'))
        self.rollout.chmod(0o644)
        with self.assertRaisesRegex(quota.AuditDenied, 'private_0600_file_required'):
            self.reconcile()

    def test_rollout_change_before_transaction_never_releases_or_publishes(self):
        auth, selected, proof = self.captured()
        def change():
            with self.rollout.open('ab') as output:
                output.write(b'{"type":"unknown_effect","payload":{}}\n')
        with self.assertRaisesRegex(quota.AuditDenied, 'rollout_changed'):
            quota.release(auth, selected, self.rollout, proof, before_transaction=change)
        self.assertEqual('needs_review', self.rows('tasks')[0]['status'])
        self.assertEqual(1, len(self.rows('session_chunks')))

    def test_task_epoch_or_session_cas_race_never_publishes(self):
        for table in ('tasks', 'native_sessions'):
            auth, selected, proof = self.captured()
            def change():
                with self.store.transaction() as db:
                    if table == 'tasks':
                        db.execute('UPDATE tasks SET epoch=epoch+1 WHERE id=?', (self.task_id,))
                    else:
                        db.execute('UPDATE native_sessions SET updated=updated+1 WHERE scope=?', (self.scope,))
            with self.subTest(table=table), self.assertRaises(quota.AuditDenied):
                quota.release(auth, selected, self.rollout, proof, before_transaction=change)
            with self.store.transaction() as db:
                db.execute('UPDATE tasks SET epoch=3 WHERE id=?', (self.task_id,))
                db.execute('UPDATE native_sessions SET updated=123 WHERE scope=?', (self.scope,))
            self.assertEqual(1, len(self.rows('session_chunks')))

    def test_existing_artifact_namespace_is_never_overwritten(self):
        auth, selected, proof = self.captured()
        identity = hashlib.sha256(json.dumps([quota.NAMESPACE, self.task_id, 3, proof['sha256']],
                                             separators=(',', ':')).encode()).hexdigest()
        with self.store.transaction() as db:
            db.execute('INSERT INTO session_chunks VALUES(?,?,?)', (identity, 0, b'conflicting immutable object'))
        with self.assertRaisesRegex(quota.AuditDenied, 'artifact_namespace_conflict'):
            self.reconcile(apply=True, expected_epoch=3)
        self.assertEqual('needs_review', self.rows('tasks')[0]['status'])
        self.assertEqual(b'conflicting immutable object', self.rows('session_chunks')[1]['body'])

    def test_failure_after_chunk_staging_rolls_back_all_publication(self):
        auth, selected, proof = self.captured()
        repeat = quota.repeat_digest
        calls = [0]
        def fail_second(*args):
            calls[0] += 1
            if calls[0] == 2:
                raise quota.AuditDenied('rollout_changed')
            return repeat(*args)
        with patch.object(quota, 'repeat_digest', side_effect=fail_second):
            with self.assertRaisesRegex(quota.AuditDenied, 'rollout_changed'):
                quota.release(auth, selected, self.rollout, proof)
        self.assertEqual(1, len(self.rows('session_chunks')))
        self.assertEqual(self.old_artifact, self.rows('native_sessions')[0]['artifact'])
        self.assertEqual('needs_review', self.rows('tasks')[0]['status'])

    def test_live_scope_and_wrong_node_denied(self):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='running',scope=? WHERE id=?", (self.scope, self.sibling_id))
        with self.assertRaisesRegex(quota.AuditDenied, 'scope_has_running_task'):
            self.reconcile()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET scope='other' WHERE id=?", (self.sibling_id,))
            db.execute('UPDATE tasks SET checkpoint=? WHERE id=?',
                       (json.dumps(dict(self.checkpoint, codex_node='cloud')), self.task_id))
        with self.assertRaisesRegex(quota.AuditDenied, 'task_node_mismatch'):
            self.reconcile()

    def test_partial_or_malformed_record_denies_without_body_disclosure(self):
        self.rollout.write_bytes(self.original + b'PRIVATE MALFORMED SECRET')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = quota.main(['--worker-config', str(self.worker), '--server-config', str(self.server),
                '--task-id', self.task_id, '--rollout-path', str(self.rollout)])
        self.assertEqual(2, result)
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertNotIn('SECRET', output.getvalue())

    def test_unknown_metadata_and_ambiguous_json_deny(self):
        self.records[-2]['payload']['unknown_execution'] = {'command': 'do something'}
        self.write_rollout()
        with self.assertRaisesRegex(quota.AuditDenied, 'unknown_selected_turn_metadata'):
            self.reconcile()
        self.records = self.history() + self.failed_turn()
        self.write_rollout()
        complete = self.rollout.read_bytes()
        complete = complete.replace(b'"codex_error_info": "usage_limit_exceeded"',
            b'"codex_error_info": "other", "codex_error_info": "usage_limit_exceeded"')
        self.rollout.write_bytes(complete)
        with self.assertRaisesRegex(quota.AuditDenied, 'duplicate_rollout_json_key'):
            self.reconcile()

    def test_large_history_is_streamed_in_contiguous_chunks_without_truncation(self):
        self.records.insert(2, {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': os.urandom(160000).hex()}]}})
        self.write_rollout()
        original = self.rollout.read_bytes()
        applied = self.reconcile(apply=True, expected_epoch=3)
        chunks = [row for row in self.rows('session_chunks') if row['id'] == applied['artifact']]
        chunks.sort(key=lambda row: row['part'])
        self.assertGreater(len(chunks), 1)
        self.assertEqual(list(range(len(chunks))), [row['part'] for row in chunks])
        self.assertTrue(all(len(row['body']) <= 65536 for row in chunks))
        self.assertEqual(original, gzip.decompress(b''.join(row['body'] for row in chunks)))
        self.assertEqual(original, self.rollout.read_bytes())

    @unittest.skipUnless(hasattr(__import__('time'), 'tzset'), 'requires timezone selection')
    def test_original_local_time_and_restored_utc_names_are_both_canonical(self):
        import time
        previous = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'Asia/Shanghai'
            time.tzset()
            local = self.rollout.with_name('rollout-2026-10-08T19-35-08-' + self.thread + '.jsonl')
            local.write_bytes(self.original)
            local.chmod(0o600)
            self.assertTrue(quota.reconcile(str(self.worker), str(self.server), self.task_id, str(local))['eligible'])
            self.assertTrue(self.reconcile()['eligible'])
        finally:
            if previous is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = previous
            time.tzset()


if __name__ == '__main__':
    unittest.main()
