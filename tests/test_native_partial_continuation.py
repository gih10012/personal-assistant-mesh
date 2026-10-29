"""Synthetic original Store/native receipts; no models, accounts or production."""
import base64
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from assistant_mesh import native_partial_continuation as partial
from assistant_mesh.rolling_runtime import LinuxProcess, RuntimeStore
from assistant_mesh.server import API
from assistant_mesh.store import Conflict


class NativePartialContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='native-partial-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.api = API({'database': str(self.root / 'ledger.db'), 'peers': []})
        self.store = self.api.store
        self.store.clock = lambda: 1000
        self.owner = {'role': 'native_maintenance', 'reviewer_id': 'fixture-owner-maintenance'}
        self.thread = '11111111-1111-4111-8111-111111111111'
        self.turn = '22222222-2222-4222-8222-222222222222'
        self.store.heartbeat('fixture-node', ['agent'])
        self.store.create_task('ORIGINAL INPUT MUST NOT BE REPLAYED', ['agent'], task_id='fixture-original-task',
                               context={'session_scope': 'fixture:native-scope', 'operation_id': 'original-operation'})
        self.claimed = self.store.claim('fixture-node')
        self.checkpoint = {'harness': 'codex', 'thread_id': self.thread, 'turn_id': self.turn,
                           'codex_node': 'fixture-node', 'codex_auth_home': '/fixture/original-auth',
                           'side_effect_started': True, 'goal': None, 'retained': {'original': True},
                           'runtime_failure': {'phase': 'native_finish', 'category': 'codex_usage_limit_exceeded',
                                               'native_start_attempted': True, 'outcome': 'unknown'}}
        self.prefix = [
            {'type': 'session_meta', 'payload': {'id': self.thread, 'history_mode': 'paginated'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'historical-turn'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 'ALL ORIGINAL HISTORY'}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'historical-turn'}}]
        self.call = {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'call_id': 'original-call',
                                                       'name': 'exec', 'input': 'original owner-authorized worktree creation'}}
        self.output = {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output',
                                                         'call_id': 'original-call', 'output': 'original command receipt'}}
        self.records = self.prefix + [
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': self.turn}},
            self.call, self.output,
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': self.turn,
                                           'error': {'codex_error_info': 'usage_limit_exceeded'}}}]
        self.old_packed = gzip.compress(self.raw(self.prefix), mtime=0)
        self.store.session_action(self.claimed['id'], 'fixture-node', 1, 'upload',
                                  {'part': 0, 'data': base64.b64encode(self.old_packed).decode()})
        self.store.session_action(self.claimed['id'], 'fixture-node', 1, 'commit',
                                  {'harness': 'codex', 'state': self.checkpoint, 'parts': 1})
        self.store.update_task(self.claimed['id'], 'fixture-node', 1, self.checkpoint, 'original quota failure', 'needs_review')
        self.store.create_task('unrelated original unknown', ['different-capability'], task_id='unrelated-unknown',
                               context={'session_scope': 'fixture:unrelated'})
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='needs_review',checkpoint=? WHERE id='unrelated-unknown'",
                       (json.dumps({'side_effect_started': True}),))
        self.before_task = self.row('tasks', 'id', self.claimed['id'])
        self.before_session = self.row('native_sessions', 'scope', self.before_task['scope'])
        self.before_unrelated = self.row('tasks', 'id', 'unrelated-unknown')
        self.payload = self.specification()

    @staticmethod
    def line(value):
        return (json.dumps(value) + '\n').encode()

    def raw(self, records):
        return b''.join(self.line(value) for value in records)

    def row(self, table, key, value):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (value,)).fetchone()
            return dict(row) if row else None

    def specification(self):
        raw = self.raw(self.records)
        inspected = partial.inspect(self.store, self.claimed['id'], self.owner)
        work = {'phase': 'unknown', 'task': {'id': self.claimed['id'], 'epoch': 1}}
        return {'protocol': partial.PROTOCOL,
                'binding': {'acceptance_id': 'fixture-owner-review', 'task_id': self.claimed['id'], 'expected_epoch': 1,
                            'thread_id': self.thread, 'turn_id': self.turn, 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)},
                'expected': inspected['expected'], 'parts': 1, 'operation_id': 'original-operation',
                'closure': {'node': 'fixture-node', 'auth_home': '/fixture/original-auth', 'thread_id': self.thread,
                            'turn_id': self.turn, 'runner_id': 'fixture-old-runner', 'runner_pid': os.getpid(),
                            'runner_birth': LinuxProcess.identity(os.getpid()), 'runtime_work_sha256': partial.digest(work),
                            'kind': 'owner-observed-native-runtime-closed', 'native_owner_live': False,
                            'owned_children_closed': True, 'forced_cleanup': False, 'goal_controller_active': False,
                            'goal_snapshot_sha256': partial.digest(self.checkpoint.get('goal')),
                            'evidence_ref': 'private:original-owned-close-receipt', 'evidence_sha256': 'a' * 64},
                'known_local_progress': [{'tool_call_id': 'original-call', 'operation_id': 'original-operation',
                                          'call_sha256': hashlib.sha256(self.line(self.call)).hexdigest(),
                                          'output_sha256': hashlib.sha256(self.line(self.output)).hexdigest(),
                                          'effect_kind': 'owner-reviewed-local-progress', 'unresolved_external_effects': False,
                                          'resource': 'worktree:original', 'evidence_ref': 'private:original-command-receipt',
                                          'observation': {'head': 'b' * 40, 'clean': True, 'already_created': True}}],
                'disposition': 'continue_remaining', 'instruction': 'Continue only the remaining source work in the existing worktree.',
                'result': None}

    def stage(self, payload=None, records=None):
        payload = self.payload if payload is None else payload
        packed = gzip.compress(self.raw(self.records if records is None else records), mtime=0)
        self.assertLessEqual(len(packed), 65536)
        return partial.upload(self.store, {'binding': payload['binding'], 'part': 0,
                                          'data': base64.b64encode(packed).decode()}, self.owner)

    def accept(self, payload=None):
        return partial.acceptance(self.store, self.payload if payload is None else payload, self.owner)

    def test_same_task_continues_with_full_before_and_full_history_once(self):
        self.stage()
        accepted = self.accept()
        self.assertTrue(accepted['accepted'])
        self.assertFalse(accepted['effects_zero'])
        self.assertFalse(accepted['independent_execution_verification'])
        task = self.row('tasks', 'id', self.claimed['id'])
        self.assertEqual((self.claimed['id'], 1, 'pending'), (task['id'], task['epoch'], task['status']))
        self.assertNotIn(self.before_task['input'], task['input'])
        self.assertIn('Do not replay', task['input'])
        self.assertIn('already_created', task['input'])
        stored = self.row('native_partial_acceptances', 'acceptance_id', accepted['acceptance_id'])
        self.assertEqual(self.before_task, json.loads(stored['before_task']))
        self.assertEqual(self.before_session, json.loads(stored['before_session']))
        with self.store.transaction() as db:
            prior = db.execute('SELECT body FROM native_partial_before_chunks').fetchone()[0]
            full = b''.join(row[0] for row in db.execute('SELECT body FROM session_chunks WHERE id=? ORDER BY part', (accepted['complete_artifact'],)))
            self.assertEqual(self.old_packed, prior)
            self.assertEqual(self.raw(self.records), gzip.decompress(full))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE native_partial_acceptances SET receipt='{}'")
        claimed = self.store.claim('fixture-node')
        self.assertEqual((self.claimed['id'], 2, self.thread), (claimed['id'], claimed['epoch'], claimed['checkpoint']['thread_id']))
        self.assertEqual(self.thread, claimed['sessions']['codex']['state']['thread_id'])
        self.store.update_task(claimed['id'], 'fixture-node', 2, {'side_effect_started': True})
        later = self.row('tasks', 'id', claimed['id'])
        self.assertEqual(accepted, self.accept())  # Lost acceptance ACK cannot reset epoch 2.
        self.assertEqual(later, self.row('tasks', 'id', claimed['id']))
        self.assertEqual(self.before_unrelated, self.row('tasks', 'id', 'unrelated-unknown'))

    def test_owner_reviewed_completion_is_result_available_without_new_native_or_outbound_replay(self):
        self.payload.update(disposition='owner_reviewed_completion', instruction='', result='Owner reviewed delivered source and deployment result.')
        self.stage(); receipt = self.accept()
        self.assertFalse(receipt['native_turn_started'])
        self.assertFalse(receipt['automatic_outbound_replay'])
        status = self.api.dispatch('POST', '/v1/task/status', {'id': self.claimed['id']}, {'role': 'operator'})
        self.assertEqual(('completed', 1, self.payload['result']), (status['status'], status['epoch'], status['result']))
        self.assertIsNone(self.store.claim('fixture-node'))
        self.assertEqual(self.before_unrelated, self.row('tasks', 'id', 'unrelated-unknown'))

    def test_only_separate_maintenance_principal_may_inspect_upload_accept_or_read_receipt(self):
        for peer in ({'role': 'operator', 'reviewed': True}, {'role': 'worker', 'node': 'fixture-node'},
                     {'role': 'ingress'}, {'role': 'viewer'}, {'completed': True}):
            with self.subTest(peer=peer), self.assertRaises(PermissionError):
                self.api.dispatch('POST', partial.PREFIX + 'accept', self.payload, peer)
        with self.assertRaises(PermissionError):
            self.api.dispatch('GET', '/v1/status', {}, self.owner)
        self.stage(); self.accept()
        self.assertEqual(self.payload['binding']['acceptance_id'], partial.receipt(self.store, 'fixture-owner-review', self.owner)['acceptance_id'])

    def test_live_owner_forced_cleanup_external_unknown_and_unreviewed_receipt_remain_guarded(self):
        self.stage()
        mutations = [lambda p: p['closure'].update(native_owner_live=True),
                     lambda p: p['closure'].update(owned_children_closed=False),
                     lambda p: p['closure'].update(forced_cleanup=True),
                     lambda p: p['closure'].update(turn_id='different-original-turn'),
                     lambda p: p['known_local_progress'][0].update(unresolved_external_effects=True),
                     lambda p: p['known_local_progress'][0].update(operation_id='different-operation'),
                     lambda p: p['known_local_progress'][0].update(output_sha256='0' * 64),
                     lambda p: p.update(known_local_progress=[])]
        for mutate in mutations:
            changed = copy.deepcopy(self.payload); mutate(changed)
            with self.subTest(changed=changed['closure']), self.assertRaises(Conflict):
                self.accept(changed)
        self.assertEqual(self.before_task, self.row('tasks', 'id', self.claimed['id']))

    def test_task_session_drift_or_incomplete_or_truncated_history_fails_before_acceptance(self):
        self.stage()
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?', ('concurrent change', self.claimed['id']))
        with self.assertRaisesRegex(Conflict, 'cas_changed'):
            self.accept()
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?', (self.before_task['result'], self.claimed['id']))
        for records in (self.records[:-1], self.prefix + self.records[len(self.prefix):len(self.prefix) + 2] + [self.records[-1]]):
            changed = copy.deepcopy(self.payload)
            raw = self.raw(records)
            changed['binding'].update(acceptance_id='incomplete-' + str(len(raw)), sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
            self.stage(changed, records)
            with self.assertRaises(Conflict):
                self.accept(changed)
        truncated = [self.prefix[0]] + self.records[len(self.prefix):]
        changed = copy.deepcopy(self.payload); raw = self.raw(truncated)
        changed['binding'].update(acceptance_id='missing-original-history', sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
        self.stage(changed, truncated)
        with self.assertRaisesRegex(Conflict, 'complete_prior_history'):
            self.accept(changed)

    def test_native_goal_read_must_match_review_before_first_explicit_resume_turn(self):
        self.stage(); accepted = self.accept()
        checkpoint = dict(self.checkpoint, owner_partial_continuation=accepted)
        partial.validate_resume_goal(checkpoint, None)
        with self.assertRaisesRegex(Conflict, 'actual_native_goal_changed'):
            partial.validate_resume_goal(checkpoint, {'status': 'active', 'threadId': self.thread})

    def test_native_exec_children_keep_outer_receipts_and_require_known_terminal_schema(self):
        command = {'type': 'CommandExecution', 'id': 'exec-synthetic-command', 'process_id': None,
                   'command': 'synthetic local command', 'cwd': '/synthetic/worktree', 'parsed_cmd': [],
                   'source': 'unified_exec_startup', 'status': 'completed', 'aggregated_output': 'synthetic output',
                   'exit_code': 0, 'duration': {'secs': 0, 'nanos': 1}}
        extension = {'type': 'Extension', 'id': 'exec-synthetic-search', 'kind': 'web.search',
                     'query': 'synthetic query', 'action': 'search', 'results': []}
        def event(item, kind='item_completed'):
            return {'type': 'event_msg', 'payload': {'type': kind, 'thread_id': self.thread,
                                                  'turn_id': self.turn, 'item': item}}
        before = self.prefix + [self.records[len(self.prefix)], self.call]
        # Internal completion may arrive after the outer output. It is still
        # original native metadata, never a second model-call receipt.
        records = before + [event(command), self.output, event(extension), self.records[-1]]
        expected = {'original-call': {'call_sha256': hashlib.sha256(self.line(self.call)).hexdigest(),
                                      'output_sha256': hashlib.sha256(self.line(self.output)).hexdigest()}}
        self.assertEqual(expected, partial.history_receipts(self.raw(records), self.thread, self.turn))
        for changes in ({'status': 'in_progress', 'exit_code': None}, {'source': 'unknown-executor'},
                        {'type': 'UnknownExecution'}):
            changed = dict(command, **changes)
            with self.subTest(changes=changes), self.assertRaises(Conflict):
                partial.history_receipts(self.raw(before + [event(changed), self.output, self.records[-1]]), self.thread, self.turn)
        running = dict(command, status='in_progress', exit_code=None)
        for unfinished in (before + [event(running, 'item_started'), self.output, self.records[-1]],
                           before + [event(command), self.records[-1]]):
            with self.assertRaises(Conflict):
                partial.history_receipts(self.raw(unfinished), self.thread, self.turn)
        unknown = dict(extension, kind='unknown.background.extension')
        with self.assertRaises(Conflict):
            partial.history_receipts(self.raw(before + [event(unknown), self.output, self.records[-1]]), self.thread, self.turn)

    def test_goal_accounting_and_unknown_managed_hold_are_not_silently_discarded(self):
        goal = {'status': 'complete', 'threadId': self.thread, 'objective': 'original goal',
                'createdAt': 10, 'tokensUsed': 120, 'timeUsedSeconds': 8, 'tokenBudget': 300}
        self.checkpoint['goal'] = goal
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (json.dumps(self.checkpoint), self.claimed['id']))
            db.execute('UPDATE native_sessions SET state=? WHERE scope=?', (json.dumps(self.checkpoint), self.before_task['scope']))
        self.payload = self.specification()
        self.stage()
        with self.store.transaction() as db:
            db.execute('''INSERT INTO managed_allocations VALUES(
                'original-managed-operation','fixture-fingerprint','node:fixture-node',?,1,'fixture-node','unknown',
                '{}','{}',1060,1,900,900,900)''', (self.claimed['id'],))
        with self.assertRaisesRegex(Conflict, 'managed_effect_not_settled'):
            self.accept()
        with self.store.transaction() as db:
            db.execute("UPDATE managed_allocations SET state='completed'")
        accepted = self.accept()
        self.assertEqual(partial.digest(goal), accepted['goal_sha256'])
        native = self.row('native_sessions', 'scope', self.before_task['scope'])
        self.assertEqual(goal, json.loads(native['state'])['goal'])

    def test_cooperative_retirement_uses_installed_receipt_resolver_and_keeps_unknown_work(self):
        runtime_root = self.root / 'runtime'; runtime_root.mkdir(mode=0o700)
        runtime = RuntimeStore(runtime_root)
        launch = {'kind': 'worker', 'component': 'fixture-worker', 'version': 'fixture-original',
                  'protocol_min': 1, 'protocol_max': 1, 'argv': [sys.executable, '-c', 'pass'],
                  'cwd': str(self.root), 'autorestart': False}
        runner = runtime.stage('fixture-stage', launch)['runner']
        with runtime.transaction() as db:
            db.execute("UPDATE runners SET state='starting',pid=?,birth=? WHERE id=?",
                       (os.getpid(), LinuxProcess.identity(os.getpid()), runner))
            token = db.execute('SELECT token FROM runners WHERE id=?', (runner,)).fetchone()[0]
        runtime.runner_request(runner, token, 'ready', {}, os.getpid())
        runtime.activate('fixture-activate', runner)
        runtime.runner_request(runner, token, 'admit', {'claim': 'original-claim'}, os.getpid())
        runtime.runner_request(runner, token, 'bind', {'claim': 'original-claim', 'task': {'id': self.claimed['id'], 'epoch': 1}}, os.getpid())
        runtime.runner_request(runner, token, 'unknown', {}, os.getpid())
        self.payload['closure']['runner_id'] = runner
        self.stage(); accepted = self.accept()
        before = next(row for row in runtime.status()['runners'] if row['id'] == runner)['work']
        with self.assertRaisesRegex(ValueError, 'authority_not_installed'):
            runtime.retire_closed_runner('fixture-retire', runner, accepted['acceptance_id'])
        runtime.retirement_resolver = lambda identity: partial.receipt(self.store, identity, self.owner)
        receipt = runtime.retire_closed_runner('fixture-retire', runner, accepted['acceptance_id'])
        self.assertTrue(receipt['original_work_retained'])
        response = runtime.runner_request(runner, token, 'heartbeat', {}, os.getpid())
        self.assertEqual('stopped', response['state'])
        self.assertTrue(response['busy'])
        after = next(row for row in runtime.status()['runners'] if row['id'] == runner)
        self.assertEqual(before, after['work'])
        self.assertFalse(after['claim_admission'])
        runtime.retirement_resolver = None
        self.assertEqual(receipt, runtime.retire_closed_runner('fixture-retire', runner, accepted['acceptance_id']))
