"""Two isolated checks; no systemd, production, SSH or native invocation."""
import contextlib
import copy
import gzip
import importlib.util
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch

from assistant_mesh.entrance import canonical, validate_policy
from tests import test_reconcile_native_quota as fixtures

loader = importlib.util.spec_from_file_location('review_entrance_abandon',
    str(Path(__file__).resolve().parents[1] / 'scripts' / 'review_entrance_abandon.py'))
review = importlib.util.module_from_spec(loader)
loader.loader.exec_module(review)
REAL_CLOSURE = review.closure


class EntranceAbandonTests(unittest.TestCase):
    def setUp(self):
        f = self.f = fixtures.NativeQuotaReconciliationTests(); f.setUp(); self.addCleanup(f.tearDown)
        self.event_id = 'notice-' + 'd' * 64
        self.task_id = 'entrance-' + self.event_id
        policy = {'owner_ref': 'synthetic-owner', 'conversation_id': 'synthetic-owner-channel',
                  'agent_id': 'synthetic-entrance'}
        self.policy = validate_policy(policy); self.node = 'synthetic-entrance-node'
        f.private('worker.json', {'node_id': self.node, 'capabilities': [self.policy['capability']],
            'entrance': policy, 'codex': {'auth_home': str(f.home)}})
        f.private('server.json', {'node_id': 'different-authority-node', 'database': str(f.database),
            'peers': [{'role': 'operator'}, {'role': 'worker', 'node': self.node,
                'capabilities': [self.policy['capability']]}]})
        cp = dict(f.checkpoint, codex_node=self.node); cp.pop('goal')
        cp['runtime_failure'] = {'category': 'codex_disconnected', 'phase': 'native_finish',
            'outcome': 'unknown', 'native_start_attempted': True, 'attempt_epoch': 3}
        state = dict(f.state, codex_node=self.node); state.pop('goal')
        self.event = {'event_id': self.event_id, 'task_id': self.task_id, 'direction': 'internal',
            'kind': 'notification', 'outbound_request_id': None, 'reply_to': None,
            'decision': canonical({'kind': 'silent', 'reason': 'synthetic audited notice'})}
        self.actions = [{'action_id': self.task_id + ':exec-' + str(n), 'fingerprint': 'synthetic-' + str(n),
                         'receipt': canonical({'action': name, 'synthetic': True}), 'created': n}
                        for n, name in enumerate(('history', 'status', 'silent'))]
        with f.store.transaction() as db:
            db.execute('UPDATE tasks SET id=?,required=?,scope=?,checkpoint=?,context=? WHERE id=?',
                (self.task_id, json.dumps([self.policy['capability']]), self.policy['scope'], json.dumps(cp),
                 json.dumps({'entrance': {'event_id': self.event_id}}), f.task_id))
            db.execute('UPDATE native_sessions SET scope=?,node=?,state=? WHERE scope=?',
                (self.policy['scope'], self.node, json.dumps(state), f.scope))
            db.execute('CREATE TABLE entrance_events(event_id TEXT,task_id TEXT,direction TEXT,kind TEXT,'
                       'outbound_request_id TEXT,reply_to TEXT,decision TEXT)')
            db.execute('INSERT INTO entrance_events VALUES(?,?,?,?,?,?,?)', tuple(self.event.values()))
            db.execute('CREATE TABLE entrance_actions(action_id TEXT,fingerprint TEXT,receipt TEXT,created INTEGER)')
            db.executemany('INSERT INTO entrance_actions VALUES(?,?,?,?)', [tuple(a.values()) for a in self.actions])
            db.execute('CREATE TABLE entrance_replies(event_id TEXT)')
            db.execute('CREATE TABLE entrance_closed_failures(task_id TEXT)')
        self.turn_records = [
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': f.turn}},
            {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec',
                'call_id': 'synthetic-call-one', 'input': 'audited synthetic history/status input'}},
            {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output',
                'call_id': 'synthetic-call-one', 'output': [{'type': 'text', 'text': 'original synthetic output'}]}},
            {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec',
                'call_id': 'synthetic-call-two', 'input': 'audited synthetic silent input'}},
            {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output',
                'call_id': 'synthetic-call-two', 'output': [{'type': 'text', 'text': 'original synthetic silent receipt'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'not task_complete'}]}},
        ]
        f.rollout.write_bytes(f.encode(f.history() + self.turn_records))
        self.raw = f.rollout.read_bytes()
        with contextlib.closing(review.util.quota.readonly(f.database)) as db:
            self.task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (self.task_id,)).fetchone())
            self.session = dict(db.execute('SELECT * FROM native_sessions WHERE scope=?', (self.policy['scope'],)).fetchone())
        self.reviews = f.root / 'reviews'; self.reviews.mkdir(mode=0o700)
        self.backup = f.root / 'existing-backup.sql.gz'
        with contextlib.closing(sqlite3.connect(str(f.database))) as db:
            sql = ('\n'.join(db.iterdump()) + '\n').encode()
        self.backup.write_bytes(gzip.compress(sql)); self.backup.chmod(0o600)
        version = review.util.read_checked(f.rollout, private=True)[1]
        for key in ('mtime_ns', 'ctime_ns'):
            version[key] = str(version[key])
        calls = [r['payload'] for r in self.turn_records if r['payload']['type'] == 'custom_tool_call']
        outputs = [r['payload']['output'] for r in self.turn_records if r['payload']['type'] == 'custom_tool_call_output']
        counts = {}
        for row in self.turn_records:
            key = row['type'] + ':' + str(row['payload'].get('type') or '')
            counts[key] = counts.get(key, 0) + 1
        self.spec = {'protocol': review.PROTOCOL, 'case': review.CASE, 'issued_at': time.time() - 1,
            'expires_at': time.time() + 900, 'worker_config': str(f.worker), 'server_config': str(f.server),
            'review_root': str(self.reviews), 'closure': {}, 'backup': {'path': str(self.backup),
                'sha256': review.util.sha(self.backup.read_bytes()), 'size': self.backup.stat().st_size,
                'sql_sha256': review.util.sha(sql), 'sql_size': len(sql)},
            'expected': {'task_id': self.task_id, 'epoch': 3, 'scope': self.policy['scope'], 'event_id': self.event_id,
                'thread_id': f.thread, 'turn_id': f.turn, 'worker_sha256': review.util.sha(f.worker.read_bytes()),
                'server_sha256': review.util.sha(f.server.read_bytes()), 'task_sha256': review.digest(self.task),
                'session_sha256': review.digest(self.session), 'checkpoint_sha256': review.util.sha(self.task['checkpoint'].encode()),
                'session_state_sha256': review.util.sha(self.session['state'].encode()), 'event_sha256': review.digest(self.event),
                'action_row_sha256': {a['action_id']: review.digest(a) for a in self.actions},
                'rollout_path': str(f.rollout), 'rollout_file': version,
                'selected_sha256': review.util.sha(f.encode(self.turn_records)), 'selected_size': len(f.encode(self.turn_records)),
                'record_counts': counts, 'old_artifact': {'id': f.old_artifact, 'parts': 1,
                    'compressed_size': len(f.original_old_chunks), 'compressed_sha256': review.util.sha(f.original_old_chunks),
                    'raw_size': len(f.encode(f.history())), 'raw_sha256': review.util.sha(f.encode(f.history()))},
                'tools': [{'call_id': call['call_id'], 'input': call['input'],
                    'input_sha256': review.util.sha(call['input'].encode()),
                    'output_canonical_sha256': review.digest(output)} for call, output in zip(calls, outputs)]}}
        self.path = f.private('owner-abandon.json', self.spec)
        # Isolated storage tests do NOT claim a real service/cgroup passed.
        self.closure_patch = patch.object(review, 'closure', return_value={'synthetic_current_boundary': True})
        self.closure_patch.start(); self.addCleanup(self.closure_patch.stop)

    def approval(self):
        return review.load(self.path)[1]['sha256']

    def test_sqlite_full_artifact_owner_abandon_and_lost_ack_never_replays(self):
        before = self.f.database.read_bytes(), self.f.rollout.read_bytes(), self.backup.read_bytes()
        context = review.inspect(self.path)
        self.assertEqual(self.task, context['selected']['task'])
        self.assertEqual(before, (self.f.database.read_bytes(), self.f.rollout.read_bytes(), self.backup.read_bytes()))
        write = review.util.write_json
        def lose_ack(path, value):
            if path.name == '07-receipt.json':
                raise OSError('synthetic lost ACK')
            return write(path, value)
        with patch.object(review.util, 'write_json', side_effect=lose_ack):
            self.assertTrue(review.apply(self.path, self.approval())['outcome_unknown'])
        observed = review.status(self.path, current_session=True)
        self.assertTrue(observed['applied']); self.assertFalse(observed['outcome_unknown'])
        for key in ('native_terminal_proven', 'historical_reader_eof_proven', 'effects_zero',
                    'external_effects_settled', 'original_input_requeued', 'retry_authorized'):
            self.assertIs(observed[key], False)
        with contextlib.closing(review.util.quota.readonly(self.f.database)) as db:
            task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (self.task_id,)).fetchone())
            session = dict(db.execute('SELECT * FROM native_sessions WHERE scope=?', (self.policy['scope'],)).fetchone())
            packed = b''.join(row[0] for row in db.execute('SELECT body FROM session_chunks WHERE id=? ORDER BY part',
                                                          (observed['complete_artifact'],)))
            self.assertEqual(self.f.original_old_chunks, db.execute('SELECT body FROM session_chunks WHERE id=?',
                                                                    (self.f.old_artifact,)).fetchone()[0])
            self.assertEqual(self.actions, [dict(r) for r in db.execute('SELECT * FROM entrance_actions ORDER BY action_id')])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM entrance_closed_failures').fetchone()[0])
            self.assertEqual('needs_review', db.execute('SELECT status FROM tasks WHERE id=?', (self.f.sibling_id,)).fetchone()[0])
        self.assertEqual(review.closed_task(self.task, self.spec), task)
        self.assertEqual(self.session['state'], session['state']); self.assertEqual(self.raw, gzip.decompress(packed))
        self.assertEqual(self.raw, self.f.rollout.read_bytes())
        self.assertEqual(self.task['result'], task['result']); self.assertEqual(self.task['input'], task['input'])
        self.assertEqual('unknown', json.loads(task['checkpoint'])['runtime_failure']['outcome'])
        with self.assertRaisesRegex(review.util.ReviewDenied, 'existing_intent_use_status'):
            review.apply(self.path, self.approval())

    def test_denies_missing_closure_approval_changed_epoch_input_or_truncated_backup(self):
        with self.assertRaisesRegex(review.util.ReviewDenied, 'explicit_spec_approval'):
            review.apply(self.path, 'not-approved')
        with patch.object(review, 'closure', side_effect=REAL_CLOSURE):
            with self.assertRaisesRegex(review.util.ReviewDenied, 'closure_pin_shape'):
                review.inspect(self.path)
        for field, changed in [('epoch', 4), ('tools', [dict(self.spec['expected']['tools'][0], input='changed'),
                                                      self.spec['expected']['tools'][1]])]:
            altered = copy.deepcopy(self.spec); altered['expected'][field] = changed
            self.f.private('owner-abandon.json', altered)
            with self.assertRaises(review.util.quota.AuditDenied if field == 'epoch' else review.util.ReviewDenied):
                review.inspect(self.path)
        self.f.private('owner-abandon.json', self.spec)
        backup_bytes = self.backup.read_bytes()
        self.backup.write_bytes(backup_bytes[:-4])
        with self.assertRaisesRegex(review.util.ReviewDenied, 'backup_changed'):
            review.inspect(self.path)
        self.assertEqual([], list(self.reviews.iterdir()))
        with contextlib.closing(review.util.quota.readonly(self.f.database)) as db:
            self.assertEqual(self.task, dict(db.execute('SELECT * FROM tasks WHERE id=?', (self.task_id,)).fetchone()))
        self.backup.write_bytes(backup_bytes)
        calls = []
        def lose_boundary(unused):
            calls.append(True)
            # Two audit observations, one pre-write observation, then COMMIT.
            return {'synthetic_current_boundary': len(calls) < 4}
        with patch.object(review, 'closure', side_effect=lose_boundary):
            self.assertTrue(review.apply(self.path, self.approval())['outcome_unknown'])
        self.assertTrue(review.status(self.path)['outcome_unknown'])
        with contextlib.closing(review.util.quota.readonly(self.f.database)) as db:
            self.assertEqual(self.task, dict(db.execute('SELECT * FROM tasks WHERE id=?', (self.task_id,)).fetchone()))
            self.assertEqual(self.session, dict(db.execute('SELECT * FROM native_sessions WHERE scope=?',
                                                           (self.policy['scope'],)).fetchone()))
        with self.assertRaisesRegex(review.util.ReviewDenied, 'existing_intent_use_status'):
            review.apply(self.path, self.approval())


if __name__ == '__main__':
    unittest.main()
