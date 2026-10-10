"""Offline Worker/authority handoff contracts, not a native goal trial."""
import copy
import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.codex import CodexError
from assistant_mesh.store import Store
from tests import test_native_goal_worker as worker_fixture


class NativeYieldWorkerTests(unittest.TestCase):
    def setUp(self):
        # Import the fixture module, not its TestCase into this module's globals:
        # unittest discovery must not count its existing tests a second time.
        self.fixture = worker_fixture.GoalWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.worker, self.agent = self.fixture.worker, self.fixture.agent
        self.task, self.client, self.timeline = self.fixture.task, self.fixture.client, self.fixture.timeline
        self.agent.config = {'native_goal_yield': True, 'native_goal_drain': True, 'native_transport': 'unix'}
        self.agent.native_drain_active = True
        self.agent.process.pid = 424242
        self.agent.native_lifecycle['expected_goal_objective'] = 'fixture'
        self.agent.native_lifecycle['goal'].update(
            createdAt=1000, updatedAt=1000, tokensUsed=10, timeUsedSeconds=2, tokenBudget=None)
        self.path = '/fixture/auth/sessions/original-history.jsonl'
        self.fingerprint = {'dev': 1, 'ino': 2, 'uid': 1000, 'mode': 0o600,
                            'size': 123, 'sha256': 'a' * 64, 'mtime_ns': 12, 'ctime_ns': 13}
        self.goal_status = 'active'
        self.mutate_seal = None
        self.agent.finish.side_effect = self.drained
        self.agent.seal_native_yield.side_effect = self.seal
        self.fixture.saved.side_effect = self.save

    def drained(self, **kwargs):
        self.assertIn('yield_requested', kwargs)
        self.worker.wait_children = True  # Simulate the actual admitted Mesh request.
        lifecycle = self.fixture.drain_state()
        lifecycle['pre_drain_goal'] = copy.deepcopy(lifecycle['goal'])
        lifecycle['history_prefix'] = {'path': self.path, 'fingerprint': {
            key: value for key, value in self.fingerprint.items() if key not in ('mtime_ns', 'ctime_ns')}}
        self.agent.native_lifecycle = lifecycle
        self.worker.on_activity('native_drain_intent', lifecycle)
        self.worker.on_activity('turn_completed', {'threadId': 'original-thread',
            'turn': {'id': 'first-turn', 'status': 'completed'}})
        lifecycle['drain_intent']['status'] = 'sent'
        lifecycle.update(outcome='drained_unverified', runtime_closed=True, settled=False, quiescent=False,
            drain_evidence={'pid': 424242, 'natural_exit_code': 0, 'reader_eof': True,
                'idle_observed': True, 'started_turn_ids': ['first-turn'],
                'natural_completion_statuses': {'first-turn': 'completed'}, 'host_request_count': 1})
        self.worker.on_activity('native_drained', lifecycle)
        self.timeline.append(('drained', None))
        return 'Local drain requires independent same-goal and history proof.'

    def seal(self, tick=None, timeout=30):
        self.timeline.append(('independent_seal', timeout))
        self.assertIsNotNone(tick)
        tick()
        lifecycle = copy.deepcopy(self.agent.native_lifecycle)
        lifecycle.update(settled=True, runtime_closed=True, quiescent=True,
            outcome='yielded' if self.goal_status == 'active' else 'terminal',
            thread_status={'type': 'notLoaded'},
            independent_observer={'thread_id': 'original-thread', 'pid': 424243,
                'natural_exit_code': 0, 'reader_eof': True, 'unloaded_before': True,
                'unloaded_after': True, 'read_only': True, 'turns_verified': ['first-turn']},
            history_proof={'path': self.path, 'fingerprint': copy.deepcopy(self.fingerprint),
                'prefix_preserved': True, 'turns_verified': ['first-turn']})
        lifecycle['goal'].update(status=self.goal_status, updatedAt=1001, tokensUsed=12, timeUsedSeconds=3)
        value = {'path': self.path, 'fingerprint': copy.deepcopy(self.fingerprint), 'lifecycle': lifecycle}
        if self.mutate_seal:
            self.mutate_seal(value)
        self.agent.native_lifecycle = copy.deepcopy(value['lifecycle'])
        return value

    def save(self, client, task, node, harness, state, rollout=None, tick=None, expected_fingerprint=None):
        self.timeline.append(('session_save', copy.deepcopy(state)))
        if expected_fingerprint is not None:
            self.assertEqual(self.path, rollout)
            self.assertEqual(self.fingerprint, expected_fingerprint)
            self.assertTrue(state['side_effect_started'])
            self.assertTrue(task['checkpoint']['side_effect_started'])
            self.assertEqual('original-task', task['id'])
            self.assertEqual(4, task['epoch'])
            self.assertIsNotNone(tick)
            tick()
            self.timeline.append(('artifact_commit_ack', True))
            return {'ok': True, 'saved': True, 'artifact_saved': True, 'scope': task['scope']}
        return {'ok': True, 'saved': True, 'artifact_saved': False, 'scope': task['scope']}

    def updates(self):
        return self.fixture.updates()

    def assert_no_closed_runtime_rpc(self):
        self.agent.native_rollout.assert_not_called()
        self.agent.goal.assert_not_called()
        self.agent.seal_native_lifecycle.assert_not_called()
        self.agent.rpc.assert_not_called()

    def assert_unknown_guarded(self, category, phase=None):
        self.assertTrue(self.task['checkpoint']['side_effect_started'])
        final = self.updates()[-1]
        self.assertEqual('waiting_backend', final['status'])
        failure = final['checkpoint']['runtime_failure']
        self.assertEqual(category, failure['category'])
        if phase:
            self.assertEqual(phase, failure['phase'])
        self.assertEqual('unknown', failure['outcome'])
        self.assertFalse(failure['retry_authorized_by_report'])
        self.assertTrue(all(body['id'] == 'original-task' and body['epoch'] == 4 for body in self.updates()))
        self.assertFalse(any(body.get('status') == 'completed' for body in self.updates()))
        self.assert_no_closed_runtime_rpc()

    def test_seal_frozen_commit_then_final_update_on_original_task_epoch(self):
        self.worker.config['native_yield_seal_timeout'] = 19
        self.worker.run_once()
        self.agent.seal_native_yield.assert_called_once()
        self.assert_no_closed_runtime_rpc()
        final = self.updates()[-1]
        self.assertEqual('waiting_children', final['status'])
        self.assertFalse(final['checkpoint']['side_effect_started'])
        self.assertTrue(final['checkpoint']['native_execution_intent']['settled'])
        self.assertEqual('yielded', final['checkpoint']['native_lifecycle']['outcome'])
        self.assertEqual('active', final['checkpoint']['goal']['status'])
        self.assertEqual(1000, final['checkpoint']['goal']['createdAt'])
        names = [name for name, value in self.timeline]
        self.assertLess(names.index('drained'), names.index('independent_seal'))
        self.assertLess(names.index('independent_seal'), names.index('artifact_commit_ack'))
        at = names.index('artifact_commit_ack')
        self.assertTrue(all(body.get('checkpoint', {}).get('side_effect_started', True) is not False
            for name, body in self.timeline[:at] if name == 'task_update'))
        snapshots = [state for name, state in self.timeline if name == 'session_save']
        self.assertEqual(2, len(snapshots))  # Initial guarded metadata + frozen artifact.
        self.assertTrue(all(state['side_effect_started'] for state in snapshots))
        self.assertIn(('independent_seal', 19), self.timeline)
        self.assertTrue(all(body['id'] == 'original-task' and body['epoch'] == 4 for body in self.updates()))

    def test_default_disabled_drain_preserves_existing_unknown_boundary(self):
        self.agent.config['native_goal_yield'] = False
        self.worker.run_once()
        self.agent.seal_native_yield.assert_not_called()
        self.assertEqual(1, self.fixture.saved.call_count)
        self.assert_unknown_guarded('codex_native_yield_seal_required', 'native_finish')

    def test_seal_failure_keeps_guard_and_never_uploads_frozen_history(self):
        self.agent.seal_native_yield.side_effect = CodexError('codex_native_yield_seal_invalid')
        self.worker.run_once()
        self.assertEqual(1, self.fixture.saved.call_count)
        self.assert_unknown_guarded('codex_native_yield_seal_invalid', 'native_yield_seal')

    def test_history_failure_has_fixed_phase_scoped_diagnostic_without_recovery_authority(self):
        from assistant_mesh.worker import _HISTORY_FAILURES, _runtime_failure_code
        self.assertEqual(15, len(_HISTORY_FAILURES))
        for code in _HISTORY_FAILURES:
            with self.subTest(code=code):
                self.assertEqual(code, _runtime_failure_code(ValueError(code), 'native_yield_seal'))
                self.assertEqual(code, _runtime_failure_code(ValueError(code), 'native_finish'))
                self.assertEqual('worker_unavailable', _runtime_failure_code(ValueError(code), 'coordination'))
        self.agent.seal_native_yield.side_effect = ValueError('native_history_prefix_changed')
        self.worker.run_once()
        self.assertEqual(1, self.fixture.saved.call_count)
        self.assert_unknown_guarded('native_history_prefix_changed', 'native_yield_seal')

    def test_frozen_upload_failure_never_reaches_false_guard_update(self):
        save = self.save
        def failed(*args, **kwargs):
            if kwargs.get('expected_fingerprint') is not None:
                self.assertTrue(args[4]['side_effect_started'])
                raise ValueError('native_session_fingerprint_changed')
            return save(*args, **kwargs)
        self.fixture.saved.side_effect = failed
        self.worker.run_once()
        self.agent.seal_native_yield.assert_called_once()
        self.assert_unknown_guarded('native_session_fingerprint_changed', 'session_save')

    def test_false_missing_or_wrong_commit_ack_never_releases_guard(self):
        save = self.save
        for response in (None, {}, {'ok': False, 'saved': True, 'artifact_saved': True, 'scope': self.task['scope']},
                         {'ok': True, 'saved': False, 'artifact_saved': True, 'scope': self.task['scope']},
                         {'ok': True, 'saved': True, 'artifact_saved': False, 'scope': self.task['scope']},
                         {'ok': True, 'saved': True, 'artifact_saved': True, 'scope': 'other-scope'}):
            with self.subTest(response=response):
                def ack(*args, **kwargs):
                    return response if kwargs.get('expected_fingerprint') is not None else save(*args, **kwargs)
                self.fixture.saved.side_effect = ack
                self.worker.run_once()
                self.assert_unknown_guarded('codex_native_yield_commit_unconfirmed', 'session_save')

    def test_lost_upload_reply_is_unknown_not_a_second_save_or_new_identity(self):
        save = self.save
        calls = []
        def lost(*args, **kwargs):
            if kwargs.get('expected_fingerprint') is not None:
                calls.append(True)
                raise OSError('private network detail not exposed')
            return save(*args, **kwargs)
        self.fixture.saved.side_effect = lost
        self.worker.run_once()
        self.assertEqual(1, len(calls))
        self.assert_unknown_guarded('worker_unavailable', 'session_save')
        self.assertNotIn('private network', json.dumps(self.updates()[-1]))

    def test_lost_or_false_task_fence_stops_before_independent_observer(self):
        request = self.client.request
        for result in ('false_ack', 'lease_lost'):
            with self.subTest(result=result):
                def fence(route, body=None):
                    if route == '/v1/task/update' and self.agent.native_lifecycle.get('outcome') == 'drained_unverified':
                        if result == 'lease_lost':
                            raise ValueError('private stale lease detail')
                        return {'ok': False}
                    return request(route, body)
                # The final drain activity itself also needs an ACK; arrange
                # failure only after its successful persistence/return.
                drained = self.drained
                def finish(**kwargs):
                    value = drained(**kwargs)
                    self.client.request = fence
                    return value
                self.agent.finish.side_effect = finish
                self.worker.run_once()
                self.client.request = request
                self.agent.seal_native_yield.assert_not_called()
                self.assertTrue(self.task['checkpoint']['side_effect_started'])
                failure = self.task['checkpoint']['runtime_failure']
                self.assertEqual('native_yield_seal', failure['phase'])
                self.assertEqual('codex_native_yield_fence_unconfirmed' if result == 'false_ack'
                                 else 'worker_unavailable', failure['category'])
                self.assert_no_closed_runtime_rpc()

    def test_lease_failure_during_frozen_upload_never_commits_or_finalizes(self):
        save = self.save
        def lost_during_upload(*args, **kwargs):
            if kwargs.get('expected_fingerprint') is not None:
                self.assertTrue(args[4]['side_effect_started'])
                self.worker.last_tick = 0
                self.client.failure = lambda route, body: (
                    (_ for _ in ()).throw(ValueError('lease_lost'))
                    if route == '/v1/heartbeat' else None)
                args[6]()  # The actual uploader uses this lease-aware callback.
                self.fail('upload must stop before commit')
            return save(*args, **kwargs)
        self.fixture.saved.side_effect = lost_during_upload
        self.worker.run_once()
        self.assertNotIn('artifact_commit_ack', [name for name, value in self.timeline])
        self.assert_unknown_guarded('worker_unavailable', 'session_save')

    def test_false_final_update_ack_is_unknown_without_retrying_commit(self):
        request = self.client.request
        def final(route, body=None):
            value = request(route, body)
            if route == '/v1/task/update' and body.get('status') == 'waiting_children':
                return {'ok': False}
            return value
        self.client.request = final
        self.worker.run_once()
        self.assertEqual(2, self.fixture.saved.call_count)
        self.assertEqual(1, sum(body.get('status') == 'waiting_children' for body in self.updates()))
        self.assert_unknown_guarded('codex_native_yield_finalize_unconfirmed', 'task_finalize')

    def test_actual_terminal_goal_never_returns_to_active_child_wait(self):
        for status in ('complete', 'paused', 'blocked', 'budgetLimited', 'usageLimited'):
            with self.subTest(status=status):
                # Each case is a fresh pre-drain fixture, not a real request to
                # reactivate a previously terminal native goal.
                self.agent.native_lifecycle['goal']['status'] = 'active'
                self.goal_status = status
                self.worker.run_once()
                final = self.updates()[-1]
                self.assertEqual('completed' if status == 'complete' else 'needs_review', final['status'])
                self.assertEqual(status, final['checkpoint']['goal']['status'])
                self.assertEqual('terminal', final['checkpoint']['native_lifecycle']['outcome'])
                self.assertFalse(final['checkpoint']['side_effect_started'])
                self.assert_no_closed_runtime_rpc()

    def test_incomplete_foreign_or_modified_seal_is_not_settlement(self):
        self.worker.current, self.worker.agent = self.task, self.agent
        self.task['checkpoint']['side_effect_started'] = True
        self.drained(yield_requested=lambda: True)
        before = copy.deepcopy(self.agent.native_lifecycle)
        good = self.seal(tick=lambda: None)
        mutations = [
            lambda v: v.update(path='relative.jsonl'),
            lambda v: v['fingerprint'].pop('ctime_ns'),
            lambda v: v['lifecycle'].update(thread_id='foreign-thread'),
            lambda v: v['lifecycle'].update(settled=False),
            lambda v: v['lifecycle'].update(thread_status=[]),
            lambda v: v['lifecycle']['drain_evidence'].update(pid=9000),
            lambda v: v['lifecycle']['drain_intent'].update(pid=9000),
            lambda v: v['lifecycle']['history_prefix'].update(path='/other/prefix'),
            lambda v: v['lifecycle']['independent_observer'].update(pid=424242),
            lambda v: v['lifecycle']['independent_observer'].update(read_only=False),
            lambda v: v['lifecycle']['independent_observer'].update(unloaded_before=False),
            lambda v: v['lifecycle']['independent_observer'].update(turns_verified=[]),
            lambda v: v['lifecycle']['history_proof'].update(prefix_preserved=False),
            lambda v: v['lifecycle']['history_proof'].update(path='/other/file'),
            lambda v: v['lifecycle']['history_proof']['fingerprint'].update(sha256='b' * 64),
            lambda v: v['lifecycle']['goal'].update(createdAt=1001),
            lambda v: v['lifecycle']['goal'].update(objective='replacement'),
            lambda v: v['lifecycle']['goal'].update(tokenBudget=500),
            lambda v: v['lifecycle']['goal'].update(tokensUsed=9),
            lambda v: v['lifecycle']['goal'].update(timeUsedSeconds=float('nan')),
            lambda v: v['lifecycle']['goal'].update(tokensUsed=12.0),
            lambda v: v['lifecycle']['goal'].update(updatedAt=999),
            lambda v: v['lifecycle']['goal'].update(status=[]),
            lambda v: v['lifecycle']['goal'].update(status={}),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = copy.deepcopy(good)
                mutate(value)
                self.agent.native_lifecycle = copy.deepcopy(value['lifecycle'])
                with self.assertRaisesRegex(CodexError, '^codex_native_yield_seal_invalid$'):
                    self.worker._checked_native_yield_seal(self.agent, before, value)
                self.assertTrue(self.task['checkpoint']['side_effect_started'])

    def test_active_seal_without_actual_child_wait_is_rejected(self):
        self.mutate_seal = lambda value: setattr(self.worker, 'wait_children', False)
        self.worker.run_once()
        self.assertEqual(1, self.fixture.saved.call_count)
        self.assert_unknown_guarded('codex_native_yield_seal_invalid', 'native_yield_seal')

    def test_authority_early_completed_children_wake_same_parent_new_epoch(self):
        # Exercise the actual upload/commit and Store wake path; all native
        # runtime/observer evidence remains explicitly a fixture.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / 'db', clock=lambda: 1000)
        node = 'fixture-node'
        parent = store.create_task('authorized parent', task_id='original-task')
        worker, fixture = self.worker, self.fixture
        self.fixture.save_patch.stop()  # Use the actual fingerprinted uploader.
        history = Path(temp.name) / 'history.jsonl'
        history.write_bytes(b'{"fixture":"opaque native history"}\n')
        history.chmod(0o600)
        metadata = history.stat()
        self.path = str(history)
        self.fingerprint = {'dev': metadata.st_dev, 'ino': metadata.st_ino,
            'uid': metadata.st_uid, 'mode': stat.S_IMODE(metadata.st_mode), 'size': metadata.st_size,
            'sha256': hashlib.sha256(history.read_bytes()).hexdigest(),
            'mtime_ns': metadata.st_mtime_ns, 'ctime_ns': metadata.st_ctime_ns}

        class StoreClient:
            def request(client, route, body=None):
                if route == '/v1/heartbeat':
                    return store.heartbeat(node, body['capabilities'])
                if route == '/v1/claim':
                    task = store.claim(node)
                    fixture.task = task
                    return {'task': task}
                if route == '/v1/task/update':
                    return store.update_task(body['id'], node, body['epoch'],
                        checkpoint=body.get('checkpoint'), result=body.get('result'), status=body.get('status'))
                if route == '/v1/session':
                    return store.session_action(body['task_id'], node, body['epoch'], body['action'], body['payload'])
                if route == '/v1/agent/action':
                    return store.agent_action(body['task_id'], node, body['epoch'], body['call_id'], body['action'], body['arguments'])
                if route == '/v1/steering':
                    return {'steering': None}
                raise AssertionError('unexpected route ' + route)

        worker.client = StoreClient()
        def fresh_start(text, resume):
            worker.on_activity('native_start_intent', {'thread_id': 'original-thread'})
            worker.on_activity('session_ready', {'thread_id': 'original-thread'})
            self.agent.turn_id = 'first-turn'
            worker.on_activity('native_turn_adopted', {'thread_id': 'original-thread', 'turn_id': 'first-turn'})
            return {'thread_id': 'original-thread', 'turn_id': 'first-turn'}
        self.agent.start.side_effect = fresh_start
        drained = self.drained
        def early_children(**kwargs):
            task = worker.current
            child = store.agent_action(parent, node, task['epoch'], 'original-child-call',
                'delegate', {'input': 'bounded specialist'})
            claimed = store.claim(node)
            self.assertEqual(child['id'], claimed['id'])
            store.update_task(child['id'], node, claimed['epoch'], status='completed')
            self.assertEqual('running', store.task_status(parent)['status'])
            return drained(**kwargs)
        self.agent.finish.side_effect = early_children
        worker.run_once()
        observed = store.task_status(parent)
        self.assertEqual('pending', observed['status'])
        with store.transaction() as db:
            checkpoint = json.loads(db.execute('SELECT checkpoint FROM tasks WHERE id=?', (parent,)).fetchone()[0])
            session = dict(db.execute('SELECT * FROM native_sessions WHERE scope=?', (observed['scope'],)).fetchone())
        self.assertFalse(checkpoint['side_effect_started'])
        self.assertTrue(json.loads(session['state'])['side_effect_started'])
        self.assertIsNotNone(session['artifact'])
        self.assertEqual('yielded', checkpoint['native_lifecycle']['outcome'])
        claimed = store.claim(node)
        self.assertEqual(parent, claimed['id'])
        self.assertEqual(observed['epoch'] + 1, claimed['epoch'])
        self.assertEqual('original-thread', claimed['checkpoint']['thread_id'])
        self.assertEqual('active', claimed['checkpoint']['goal']['status'])
        self.assert_no_closed_runtime_rpc()


if __name__ == '__main__':
    unittest.main()
