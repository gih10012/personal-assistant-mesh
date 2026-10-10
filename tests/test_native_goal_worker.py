"""Offline lease/teardown fixtures, not proof of native model execution."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from assistant_mesh.codex import CodexError
from assistant_mesh.store import Store
from assistant_mesh.worker import Worker


class GoalClient:
    def __init__(self, task, timeline):
        self.task, self.timeline = task, timeline
        self.calls = []
        self.failure = None

    def request(self, route, body=None):
        self.calls.append((route, copy.deepcopy(body)))
        if self.failure:
            self.failure(route, body)
        if route == '/v1/claim':
            return {'task': self.task}
        if route == '/v1/steering':
            return {'steering': None}
        if route == '/v1/agent/action':
            return {'children': []}
        if route == '/v1/task/update':
            self.timeline.append(('task_update', copy.deepcopy(body)))
        return {'ok': True}


class GoalWorkerTests(unittest.TestCase):
    def setUp(self):
        self.timeline = []
        self.task = {'id': 'original-task', 'epoch': 4, 'node': 'fixture-node',
                     'scope': 'leader:owner', 'input': 'Continue the authorized objective.',
                     'context': {}, 'checkpoint': {'thread_id': 'original-thread',
                         'turn_id': 'old-turn', 'harness': 'codex', 'codex_node': 'fixture-node',
                         'codex_auth_home': '/fixture/auth', 'side_effect_started': False}}
        self.client = GoalClient(self.task, self.timeline)
        self.worker = Worker({'node_id': 'fixture-node', 'codex': {
            'auth_home': '/fixture/auth', 'sandbox': 'read-only'}}, client=self.client)
        self.worker.harness = 'codex'
        self.worker.resource_reference = Mock(return_value={'native_tools_intercepted': False})
        self.agent = Mock()
        self.agent.__enter__ = Mock(return_value=self.agent)
        self.agent.__exit__ = Mock(side_effect=lambda *args: self.timeline.append(('close', None)))
        self.agent.auth_home = '/fixture/auth'
        self.agent.turn_id = None
        self.agent.rpc_depth = 0
        self.agent.native_lifecycle = {'controller_enabled': True, 'goal_managed': True,
            'quiescent': False, 'settled': False, 'runtime_closed': False,
            'thread_id': 'original-thread', 'turn_id': None, 'turn_ids': [],
            'goal': {'threadId': 'original-thread', 'objective': 'fixture', 'status': 'active'}}
        self.agent.start.side_effect = self.start
        self.agent.finish.side_effect = self.finish
        self.agent.native_rollout.side_effect = lambda: self.timeline.append(('rollout_path', None))
        self.agent.seal_native_lifecycle.side_effect = self.seal
        self.worker.open_backend = Mock(return_value=self.agent)
        self.save_patch = patch('assistant_mesh.worker.sessions.save', side_effect=self.save)
        self.saved = self.save_patch.start()
        self.addCleanup(self.save_patch.stop)

    def save(self, client, task, node, harness, state, *args):
        self.timeline.append(('session_save', copy.deepcopy(state)))

    def start(self, text, resume):
        self.worker.on_activity('native_start_intent', {'thread_id': resume['thread_id']})
        self.timeline.append(('native_resume_rpc', None))
        self.worker.on_activity('session_ready', {'thread_id': 'original-thread'})
        self.agent.turn_id = 'first-turn'
        self.worker.on_activity('native_turn_adopted', {'thread_id': 'original-thread', 'turn_id': 'first-turn'})
        return {'thread_id': 'original-thread', 'turn_id': 'first-turn'}

    def finish(self, **kwargs):
        self.worker.on_activity('turn_completed', {'threadId': 'original-thread',
            'turn': {'id': 'first-turn', 'status': 'completed'}})
        self.agent.turn_id = 'automatic-turn'
        self.worker.on_activity('native_turn_adopted', {'thread_id': 'original-thread', 'turn_id': 'automatic-turn'})
        self.worker.on_activity('plan_updated', {'threadId': 'original-thread', 'turnId': 'automatic-turn',
            'plan': [{'step': 'verify', 'status': 'completed'}]})
        self.worker.on_activity('turn_completed', {'threadId': 'original-thread',
            'turn': {'id': 'automatic-turn', 'status': 'completed'}})
        goal = {'threadId': 'original-thread', 'objective': 'fixture', 'status': 'complete'}
        self.worker.on_activity('native_goal_snapshot', {'thread_id': 'original-thread', 'goal': goal})
        self.agent.native_lifecycle.update(goal=goal, quiescent=True, turn_id='automatic-turn',
            turn_ids=['first-turn', 'automatic-turn'], completed_turn_ids=['first-turn', 'automatic-turn'])
        return 'fixture evidence'

    def seal(self):
        self.timeline.append(('seal', None))
        self.agent.native_lifecycle.update(settled=True, runtime_closed=True)

    def updates(self):
        return [body for route, body in self.client.calls if route == '/v1/task/update']

    def test_durable_guard_precedes_resume_even_for_read_only_and_settles_after_seal(self):
        self.worker.run_once()
        events = [name for name, value in self.timeline]
        resume = events.index('native_resume_rpc')
        before = [value for name, value in self.timeline[:resume] if name == 'task_update']
        self.assertTrue(before[-1]['checkpoint']['side_effect_started'])
        self.assertEqual(4, before[-1]['checkpoint']['native_execution_intent']['task_epoch'])
        self.assertLess(events.index('rollout_path'), events.index('seal'))
        snapshots = [value for name, value in self.timeline if name == 'session_save']
        self.assertTrue(snapshots[0]['side_effect_started'])
        self.assertFalse(snapshots[-1]['side_effect_started'])
        self.assertTrue(snapshots[-1]['native_lifecycle']['runtime_closed'])
        self.assertEqual('automatic-turn', snapshots[-1]['turn_id'])
        self.assertEqual('completed', self.updates()[-1]['status'])
        self.assertTrue(self.updates()[-1]['checkpoint']['native_execution_intent']['settled'])
        self.agent.goal.assert_not_called()  # No post-close native RPC.

    def test_lost_resume_reply_keeps_original_guard_and_does_not_replay(self):
        def lost(text, resume):
            self.worker.on_activity('native_start_intent', {'thread_id': resume['thread_id']})
            raise CodexError('codex_timeout')
        self.agent.start.side_effect = lost
        self.worker.run_once()
        self.assertTrue(self.task['checkpoint']['side_effect_started'])
        self.assertEqual('original-thread', self.task['checkpoint']['thread_id'])
        self.assertEqual('waiting_backend', self.updates()[-1]['status'])
        self.agent.start.assert_called_once()
        self.agent.finish.assert_not_called()
        self.agent.seal_native_lifecycle.assert_not_called()

    def test_missing_intent_receipt_does_not_reach_native_resume(self):
        def fail(route, body):
            if route == '/v1/task/update' and 'native_execution_intent' in body.get('checkpoint', {}):
                raise OSError('private lost authority')
        self.client.failure = fail
        self.worker.run_once()
        self.assertNotIn('native_resume_rpc', [name for name, value in self.timeline])
        self.assertTrue(self.task['checkpoint']['side_effect_started'])
        self.agent.finish.assert_not_called()

    def test_late_work_during_seal_preserves_guard_and_private_failure_is_not_reported(self):
        self.agent.seal_native_lifecycle.side_effect = CodexError('codex_native_late_work')
        self.worker.run_once()
        self.assertTrue(self.task['checkpoint']['side_effect_started'])
        self.assertEqual(1, self.saved.call_count)  # Only initial guarded state.
        report = self.updates()[-1]['checkpoint']['runtime_failure']
        self.assertEqual('native_settle', report['phase'])
        self.assertEqual('codex_native_late_work', report['category'])
        self.assertTrue(all(body['epoch'] == 4 and body['id'] == 'original-task' for body in self.updates()))

    def test_active_goal_cannot_settle_even_with_runtime_closed_flag(self):
        def unsafe_seal():
            self.seal()
            self.agent.native_lifecycle['goal']['status'] = 'active'
        self.agent.seal_native_lifecycle.side_effect = unsafe_seal
        self.worker.run_once()
        self.assertTrue(self.task['checkpoint']['side_effect_started'])
        self.assertEqual('waiting_backend', self.updates()[-1]['status'])
        self.assertEqual(1, self.saved.call_count)

    def test_paused_limited_or_blocked_goal_stays_needs_review_not_completed(self):
        for status in ('paused', 'blocked', 'budgetLimited', 'usageLimited'):
            with self.subTest(status=status):
                def terminal_seal():
                    self.seal()
                    self.agent.native_lifecycle['goal']['status'] = status
                self.agent.seal_native_lifecycle.side_effect = terminal_seal
                self.worker.run_once()
                self.assertEqual('needs_review', self.updates()[-1]['status'])
                self.assertEqual(status, self.updates()[-1]['checkpoint']['goal']['status'])

    def test_native_rpc_depth_suppresses_only_optional_steering_not_task_fence(self):
        self.worker.current = self.task
        self.worker.agent = self.agent
        self.agent.turn_id = 'current-turn'
        self.agent.rpc_depth = 1
        self.worker.tick(force=True)
        routes = [route for route, body in self.client.calls]
        self.assertIn('/v1/heartbeat', routes)
        self.assertIn('/v1/task/update', routes)
        self.assertNotIn('/v1/steering', routes)
        self.agent.steer.assert_not_called()

    def test_coordination_yield_boundary_preserves_active_goal_without_replay(self):
        self.worker.wait_children = True
        self.agent.finish.side_effect = CodexError('codex_goal_coordination_yield_unavailable')
        self.worker.run_once()
        self.assertTrue(self.task['checkpoint']['side_effect_started'])
        self.assertEqual('waiting_backend', self.updates()[-1]['status'])
        self.assertEqual('codex_goal_coordination_yield_unavailable',
                         self.updates()[-1]['checkpoint']['runtime_failure']['category'])
        self.agent.start.assert_called_once()
        self.agent.seal_native_lifecycle.assert_not_called()

    def test_new_turn_does_not_present_old_plan_and_old_turn_cannot_overwrite_state(self):
        self.worker.current = self.task
        self.task['checkpoint']['plan'] = {'threadId': 'original-thread', 'turnId': 'old-turn', 'plan': []}
        self.worker.tick = Mock()
        self.worker.on_activity('native_turn_adopted', {'thread_id': 'original-thread', 'turn_id': 'new-turn'})
        self.assertIsNone(self.task['checkpoint']['plan'])
        self.worker.on_activity('turn_completed', {'threadId': 'original-thread', 'turn': {'id': 'old-turn'}})
        self.assertTrue(self.worker._native_turn_live)
        self.assertEqual('new-turn', self.task['checkpoint']['turn_id'])

    def test_goal_snapshot_for_other_thread_cannot_replace_native_goal(self):
        self.worker.current = self.task
        self.worker.tick = Mock()
        self.worker.on_activity('native_goal_snapshot', {'thread_id': 'foreign-thread',
            'goal': {'threadId': 'foreign-thread', 'status': 'complete'}})
        self.assertNotIn('goal', self.task['checkpoint'])

    def drain_state(self):
        self.worker.current, self.worker.agent = self.task, self.agent
        self.agent.native_drain_active = True
        self.agent.process.pid = 424242
        self.agent.native_lifecycle.update(outcome='draining', drain_intent={
            'status': 'intent', 'thread_id': 'original-thread', 'turn_id': 'first-turn',
            'pid': 424242, 'signal': 'SIGTERM'})
        return copy.deepcopy(self.agent.native_lifecycle)

    def test_drain_intent_ack_requires_current_epoch_durable_checkpoint(self):
        lifecycle = self.drain_state()
        ack = self.worker.on_activity('native_drain_intent', lifecycle)
        self.assertEqual({'ok': True, 'thread_id': 'original-thread', 'pid': 424242}, ack)
        intent = self.updates()[-1]['checkpoint']['native_drain_intent']
        self.assertEqual('original-task', intent['task_id'])
        self.assertEqual(4, intent['task_epoch'])
        self.assertTrue(self.updates()[-1]['checkpoint']['side_effect_started'])
        self.assertIn('/v1/heartbeat', [route for route, body in self.client.calls])
        self.assertNotIn('/v1/steering', [route for route, body in self.client.calls])

    def test_false_checkpoint_ack_or_lost_fence_never_authorizes_drain(self):
        for failure in ('false_ack', 'lease_lost'):
            with self.subTest(failure=failure):
                lifecycle = self.drain_state()
                request = self.client.request

                def fail(route, body=None):
                    if failure == 'lease_lost' and route == '/v1/heartbeat':
                        raise ValueError('lease_lost')
                    response = request(route, body)
                    if failure == 'false_ack' and route == '/v1/task/update':
                        return {'ok': False}
                    return response

                with patch.object(self.client, 'request', side_effect=fail):
                    with self.assertRaises(ValueError):
                        self.worker.on_activity('native_drain_intent', lifecycle)
                self.assertTrue(self.task['checkpoint']['side_effect_started'])

    def test_wait_children_suppresses_optional_steer_but_not_heartbeat(self):
        self.worker.current, self.worker.agent = self.task, self.agent
        self.worker.wait_children = True
        self.agent.turn_id = 'current-turn'
        self.worker.tick(force=True)
        routes = [route for route, body in self.client.calls]
        self.assertIn('/v1/heartbeat', routes)
        self.assertNotIn('/v1/steering', routes)

    def test_drained_unverified_never_sends_post_close_rpc_or_releases_guard(self):
        def drained(**kwargs):
            lifecycle = self.drain_state()
            self.worker.on_activity('native_drain_intent', lifecycle)
            self.agent.native_lifecycle.update(outcome='drained_unverified', runtime_closed=True,
                                               settled=False, quiescent=False)
            self.worker.on_activity('native_drained', self.agent.native_lifecycle)
            return 'Local drain is not a yielded seal'

        self.agent.finish.side_effect = drained
        self.worker.run_once()
        self.agent.native_rollout.assert_not_called()
        self.agent.goal.assert_not_called()
        self.agent.seal_native_lifecycle.assert_not_called()
        self.assertEqual(1, self.saved.call_count)  # Initial guarded state only, no post-drain artifact.
        self.assertTrue(self.task['checkpoint']['side_effect_started'])
        self.assertEqual('waiting_backend', self.updates()[-1]['status'])
        self.assertEqual('codex_native_yield_seal_required',
                         self.updates()[-1]['checkpoint']['runtime_failure']['category'])
        self.assertTrue(all(body['id'] == 'original-task' and body['epoch'] == 4
                            for body in self.updates()))

    def test_fingerprint_failure_has_only_fixed_phase_scoped_diagnostic(self):
        from assistant_mesh.worker import _runtime_failure_code
        for code in ('native_session_fingerprint_invalid', 'native_session_fingerprint_unsupported',
                     'native_session_fingerprint_path_unsafe', 'native_session_fingerprint_changed',
                     'native_session_fingerprint_rollout_required'):
            self.assertEqual(code, _runtime_failure_code(ValueError(code), 'session_save'))
            self.assertEqual('worker_unavailable', _runtime_failure_code(ValueError(code), 'turn_wait'))

    def test_frozen_snapshot_failure_never_clears_guard_or_changes_task_identity(self):
        calls = []

        def save(*args):
            calls.append(True)
            if len(calls) == 1:
                return self.save(*args)
            raise ValueError('native_session_fingerprint_changed')

        self.saved.side_effect = save
        self.worker.run_once()
        self.agent.seal_native_lifecycle.assert_called_once()
        self.assertTrue(self.task['checkpoint']['side_effect_started'])
        self.assertEqual('waiting_backend', self.updates()[-1]['status'])
        self.assertEqual('native_session_fingerprint_changed',
                         self.updates()[-1]['checkpoint']['runtime_failure']['category'])


class GoalWaitingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'db', clock=lambda: 1000)
        self.store.heartbeat('node', ['leader', 'agent'])
        self.identifier = self.store.create_task('parent')
        self.task = self.store.claim('node')

    def test_waiting_children_cannot_invent_zero_effect_and_new_task_cannot_bypass(self):
        child = self.store.agent_action(self.identifier, 'node', self.task['epoch'], 'child-call',
            'delegate', {'input': 'child'})
        self.store.update_task(self.identifier, 'node', self.task['epoch'],
            checkpoint={'side_effect_started': True}, status='waiting_children')
        with self.store.transaction() as db:
            guard = json.loads(db.execute('SELECT checkpoint FROM tasks WHERE id=?', (self.identifier,)).fetchone()[0])
        self.assertTrue(guard['side_effect_started'])
        self.store.create_task('do not escape original unknown', task_id='new-parent')
        claimed = self.store.claim('node')
        self.assertEqual(child['id'], claimed['id'])
        self.store.update_task(child['id'], 'node', claimed['epoch'], status='completed')
        self.assertIsNone(self.store.claim('node'))
        self.assertEqual('needs_review', self.store.task_status(self.identifier)['status'])

    def test_explicit_settled_worker_checkpoint_still_wakes_same_parent(self):
        child = self.store.agent_action(self.identifier, 'node', self.task['epoch'], 'child-call',
            'delegate', {'input': 'child'})
        self.store.update_task(self.identifier, 'node', self.task['epoch'],
            checkpoint={'side_effect_started': False}, status='waiting_children')
        claimed = self.store.claim('node')
        self.store.update_task(child['id'], 'node', claimed['epoch'], status='completed')
        self.assertEqual(self.identifier, self.store.claim('node')['id'])


if __name__ == '__main__':
    unittest.main()
