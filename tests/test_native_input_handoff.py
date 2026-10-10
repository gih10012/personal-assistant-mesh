"""Input delivery evidence and resume races; never calls a native model."""
import copy
import hashlib
import unittest
from unittest import mock

from assistant_mesh.codex import Codex, CodexError
from tests import test_native_goal_lifecycle as goal_fixture
from tests import test_native_goal_worker as worker_fixture


class NativeInputHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = goal_fixture.NativeGoalLifecycleTests()

    def resumed(self):
        agent = self.fixture.runtime(actual=self.fixture.goal())
        self.fixture.begin(agent, resume=True)
        return agent

    def test_actual_steer_receipt_and_hash_are_durable_before_host_request(self):
        agent = self.fixture.runtime(actual=self.fixture.goal())
        del agent.rpc  # Exercise the real message pump, not a stubbed callback.
        agent.serial, agent.rpc_depth = 0, 0
        timeline = []
        agent.send = lambda value: timeline.append(('wire', copy.deepcopy(value)))
        agent.on_activity = lambda name, value: timeline.append((name, copy.deepcopy(value)))
        agent.on_tool = lambda params: timeline.append(('tool', params)) or {'ok': True}
        agent.events.put(self.fixture.started('native-auto-1'))
        request = {'id': 'host-1', 'method': 'item/tool/call', 'params': {
            'threadId': self.fixture.thread, 'turnId': 'native-auto-1', 'name': 'mesh_wait_children'}}
        agent.events.put(request)
        agent.events.put({'id': 1, 'result': {'thread': {'id': self.fixture.thread}}})
        agent.events.put({'id': 2, 'result': {'goal': self.fixture.goal()}})
        agent.events.put({'id': 3, 'result': {'turnId': 'native-auto-1'}})
        self.fixture.begin(agent, resume=True)
        receipt = agent.native_lifecycle['native_input']
        self.assertEqual('submitted', receipt['status'])
        self.assertEqual(hashlib.sha256(b'owner task instruction').hexdigest(), receipt['sha256'])
        names = [name for name, value in timeline]
        self.assertLess(names.index('native_input_receipt'), names.index('tool'))
        requests = [v for name, v in timeline if name == 'wire' and v.get('method') == 'turn/steer']
        self.assertEqual(1, len(requests))
        self.assertEqual([('tool', request['params'])], [v for v in timeline if v[0] == 'tool'])
        self.assertFalse(agent._held_host_requests)
        self.assertFalse(agent._hold_host_requests)

    def test_missing_wrong_or_malformed_receipt_is_unknown_and_never_retried(self):
        for response in ({}, {'turnId': 'other'}, None, {'turnId': None}):
            with self.subTest(response=response):
                agent = self.resumed()
                rpc = agent.rpc
                def changed(method, params, **kwargs):
                    result = rpc(method, params, **kwargs)
                    return response if method == 'turn/steer' else result
                agent.rpc = changed
                agent.on_tool = mock.Mock()
                agent._held_host_requests.append({'id': 9, 'method': 'item/tool/call', 'params': {}})
                with self.assertRaisesRegex(CodexError, 'codex_native_input_receipt_invalid'):
                    agent._consume_finish_event(self.fixture.started('native-auto-1'), [])
                self.assertEqual('unknown', agent.native_lifecycle['native_input']['status'])
                agent._consume_finish_event(self.fixture.started('native-auto-1'), [])
                self.assertEqual(1, sum(method == 'turn/steer' for method, params in agent.calls))
                agent.on_tool.assert_not_called()
                self.assertTrue(agent._hold_host_requests)

    def test_input_intent_persist_failure_prevents_steer(self):
        agent = self.resumed()
        def fail(name, value):
            if name == 'native_start_intent' and value.get('method') == 'turn/steer':
                raise ValueError('fixture fence lost')
        agent.on_activity = fail
        with self.assertRaisesRegex(ValueError, 'fixture fence lost'):
            agent._consume_finish_event(self.fixture.started('native-auto-1'), [])
        self.assertFalse(any(method == 'turn/steer' for method, params in agent.calls))
        self.assertIsNone(agent._pending_goal_input)

    def test_existing_conflicting_goal_cannot_receive_early_steer(self):
        agent = self.fixture.runtime(actual=self.fixture.goal(objective='another objective'))
        agent.before_reply['thread/resume'] = [self.fixture.started('native-auto-1')]
        with self.assertRaisesRegex(CodexError, 'codex_native_goal_objective_conflict'):
            self.fixture.begin(agent, resume=True)
        self.assertFalse(any(method == 'turn/steer' for method, params in agent.calls))

    def test_goal_read_deferred_turn_receives_input_only_once(self):
        agent = self.fixture.runtime(actual=self.fixture.goal())
        agent.before_reply['thread/goal/get'] = [self.fixture.started('native-auto-1')]
        self.fixture.begin(agent, resume=True)
        agent._consume_finish_event(self.fixture.started('native-auto-1'), [])
        self.assertEqual(1, sum(method == 'turn/steer' for method, params in agent.calls))
        self.assertEqual('submitted', agent.native_lifecycle['native_input']['status'])

    def checkpoint(self):
        return {'thread_id': self.fixture.thread, 'native_lifecycle': {
            'outcome': 'yielded', 'thread_id': self.fixture.thread,
            'runtime_closed': True, 'settled': True,
            'goal': self.fixture.goal(createdAt=1000)}}

    def test_missing_migrated_goal_never_creates_new_goal_or_turn(self):
        agent = self.fixture.runtime(actual=None)
        with self.assertRaisesRegex(CodexError, 'codex_native_goal_continuity_unverified'):
            agent.start('resume work', self.checkpoint())
        self.assertFalse(any(method in ('thread/goal/set', 'turn/start', 'turn/steer')
                             for method, params in agent.calls))

    def test_live_worker_checkpoint_mutation_cannot_erase_prior_goal_continuity(self):
        for actual in (None, self.fixture.goal(createdAt=1000, tokensUsed=0)):
            with self.subTest(actual=actual):
                agent = self.fixture.runtime(actual=actual)
                checkpoint = self.checkpoint()
                def activity(name, value):
                    # Model the real Worker's synchronous replacement of the
                    # same checkpoint object supplied to start(), not a copy.
                    if isinstance(value.get('native_lifecycle'), dict):
                        checkpoint['native_lifecycle'] = copy.deepcopy(value['native_lifecycle'])
                agent.on_activity = activity
                with self.assertRaisesRegex(CodexError, 'codex_native_goal_continuity_unverified'):
                    agent.start('resume work', checkpoint)
                self.assertIsNone(checkpoint['native_lifecycle'].get('outcome'))
                self.assertFalse(any(method in ('thread/goal/set', 'turn/start', 'turn/steer')
                                     for method, params in agent.calls))

    def test_goal_accounting_reset_or_other_identity_is_not_continuity(self):
        for changes in ({'createdAt': 1001}, {'tokensUsed': 0}, {'timeUsedSeconds': 0},
                        {'tokensUsed': float('nan')}, {'tokensUsed': True}):
            with self.subTest(changes=changes):
                goal = self.fixture.goal(createdAt=1000)
                goal.update(changes)
                agent = self.fixture.runtime(actual=goal)
                with self.assertRaisesRegex(CodexError, 'codex_native_goal_continuity_unverified'):
                    agent.start('resume work', self.checkpoint())
                self.assertFalse(any(method in ('thread/goal/set', 'turn/start', 'turn/steer')
                                     for method, params in agent.calls))

    def test_actual_paused_yielded_goal_is_not_reactivated_without_config_goal(self):
        agent = self.fixture.runtime(requested=False, actual=self.fixture.goal('paused', createdAt=1000))
        agent.start('children results', self.checkpoint())
        self.assertFalse(any(method in ('thread/goal/set', 'turn/start', 'turn/steer')
                             for method, params in agent.calls))
        self.assertEqual('paused', agent.native_lifecycle['goal']['status'])

    def test_unconfirmed_pending_input_cannot_be_terminal_sealed(self):
        agent = self.resumed()
        agent.actual_goal = self.fixture.goal('complete')
        with self.assertRaisesRegex(CodexError, 'codex_native_resume_input_unconfirmed'):
            agent._goal_quiescence_barrier([])
        self.assertFalse(agent.native_lifecycle['settled'])

    def test_held_requests_are_bounded_without_invoking_callback(self):
        agent = self.resumed()
        agent.on_tool = mock.Mock()
        agent._held_host_requests = [{}] * 128
        agent.events.put({'id': 1, 'method': 'item/tool/call', 'params': {}})
        with self.assertRaisesRegex(CodexError, 'codex_native_resume_request_overflow'):
            agent.event(timeout=.1)
        agent.on_tool.assert_not_called()

    def test_other_turn_held_request_does_not_borrow_current_input_receipt(self):
        agent = self.resumed()
        agent.on_tool = mock.Mock()
        agent._held_host_requests.append({'id': 1, 'method': 'item/tool/call',
            'params': {'threadId': self.fixture.thread, 'turnId': 'other-turn'}})
        with self.assertRaisesRegex(CodexError, 'codex_native_resume_unexpected_work'):
            agent._consume_finish_event(self.fixture.started('native-auto-1'), [])
        agent.on_tool.assert_not_called()


class InputWorkerReceiptTests(unittest.TestCase):
    def test_worker_persists_submitted_and_unknown_receipts_without_clearing_guard(self):
        fixture = worker_fixture.GoalWorkerTests()
        fixture.setUp()
        try:
            worker = fixture.worker
            worker.current, worker.agent = fixture.task, fixture.agent
            for status in ('submitted', 'unknown'):
                value = copy.deepcopy(fixture.agent.native_lifecycle)
                value['native_input'] = {'status': status, 'sha256': 'a' * 64}
                worker.on_activity('native_input_receipt', value)
                update = fixture.updates()[-1]['checkpoint']
                self.assertTrue(update['side_effect_started'])
                self.assertEqual(status, update['native_lifecycle']['native_input']['status'])
        finally:
            fixture.doCleanups()
