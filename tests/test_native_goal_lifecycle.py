"""Offline goal/turn lifecycle boundaries; no account, process or live goal."""
import copy
import queue
import unittest
from unittest import mock

from assistant_mesh.codex import Codex, CodexError


class NativeGoalLifecycleTests(unittest.TestCase):
    thread = 'original-native-thread'
    objective = 'Verify the bounded owner outcome without replacing this goal'

    def goal(self, status='active', **changes):
        value = {'threadId': self.thread, 'objective': self.objective,
                 'status': status, 'tokensUsed': 123, 'timeUsedSeconds': 45,
                 'tokenBudget': None}
        value.update(changes)
        return value

    def event(self, method, **params):
        return {'method': method, 'params': dict(threadId=self.thread, **params)}

    def started(self, turn):
        return self.event('turn/started', turn={'id': turn, 'status': 'inProgress'})

    def completed(self, turn, status='completed'):
        return self.event('turn/completed', turn={'id': turn, 'status': status})

    def message(self, turn, text):
        return self.event('item/completed', turnId=turn,
                          item={'type': 'agentMessage', 'phase': 'final_answer', 'text': text})

    def runtime(self, requested=True, actual=None):
        agent = Codex.__new__(Codex)
        agent.config = {'workspace': '/fixture-workspace'}
        if requested:
            agent.config['goal'] = {'objective': self.objective, 'status': 'active'}
        agent.tools = []
        agent.on_tool = agent.on_interaction = None
        agent.deferred, agent.events = [], queue.Queue()
        agent.calls, agent.activities = [], []
        agent.actual_goal = copy.deepcopy(actual)
        agent.actual_status = {'type': 'idle'}
        agent.before_reply, agent.after_reply = {}, {}
        agent.on_activity = lambda name, params: agent.activities.append((name, copy.deepcopy(params), len(agent.calls)))

        def rpc(method, params, **kwargs):
            agent.calls.append((method, copy.deepcopy(params)))
            for value in agent.before_reply.pop(method, []):
                agent.deferred.append(copy.deepcopy(value))
            if method in ('thread/start', 'thread/resume'):
                response = {'thread': {'id': self.thread, 'model': 'fixture-model',
                                       'status': copy.deepcopy(agent.actual_status)}}
            elif method == 'turn/start':
                response = {'turn': {'id': 'owner-task-turn', 'status': 'inProgress'}}
            elif method == 'thread/goal/set':
                agent.actual_goal = dict(params, tokensUsed=0, timeUsedSeconds=0)
                response = {'goal': copy.deepcopy(agent.actual_goal)}
            elif method == 'thread/goal/get':
                response = {'goal': copy.deepcopy(agent.actual_goal)}
            elif method == 'thread/read':
                response = {'thread': {'id': self.thread, 'status': copy.deepcopy(agent.actual_status)}}
            else:
                response = {}
            for value in agent.after_reply.pop(method, []):
                agent.events.put(copy.deepcopy(value))
            return response

        agent.rpc = rpc
        agent.close = mock.Mock()
        agent.interrupt = mock.Mock()
        agent.process = mock.Mock()
        agent.process.poll.return_value = 0
        return agent

    def begin(self, agent, resume=False):
        return agent.start('owner task instruction', {'thread_id': self.thread} if resume else None)

    def done_controller(self, status='complete'):
        agent = self.runtime()
        self.begin(agent)
        agent._completed_turns.add(agent.turn_id)
        agent.native_lifecycle['completed_turn_ids'].append(agent.turn_id)
        agent.actual_goal = self.goal(status)
        return agent

    def test_intent_is_synchronous_before_resume_and_every_work_producing_rpc(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                agent = self.runtime(actual=None)
                self.begin(agent, resume=resume)
                for index, (method, params) in enumerate(agent.calls):
                    if method in ('thread/start', 'thread/resume', 'turn/start', 'thread/goal/set'):
                        self.assertTrue(any(name == 'native_start_intent' and body['method'] == method
                                            and count == index for name, body, count in agent.activities))
                ready = next(count for name, body, count in agent.activities if name == 'session_ready')
                self.assertLessEqual(ready, next(i for i, call in enumerate(agent.calls) if call[0] == 'turn/start'))

    def test_intent_checkpoint_failure_prevents_native_resume(self):
        agent = self.runtime()
        agent.on_activity = mock.Mock(side_effect=ValueError('lease lost'))
        with self.assertRaisesRegex(ValueError, 'lease lost'):
            self.begin(agent, resume=True)
        self.assertEqual([], agent.calls)

    def test_fresh_goal_task_turn_precedes_goal_set_without_idle_auto_race(self):
        agent = self.runtime()
        self.begin(agent)
        methods = [method for method, params in agent.calls]
        self.assertLess(methods.index('turn/start'), methods.index('thread/goal/set'))
        self.assertEqual(['owner-task-turn'], agent.native_lifecycle['turn_ids'])
        self.assertEqual('active', agent.native_lifecycle['goal']['status'])

    def test_checkpoint_goal_is_observation_never_set_or_reactivated(self):
        agent = self.runtime(requested=False, actual=None)
        agent.start('explicit one-off work', {'thread_id': self.thread,
                                              'goal': self.goal('active')})
        self.assertFalse(any(method == 'thread/goal/set' for method, params in agent.calls))
        self.assertFalse(agent.native_lifecycle['goal_managed'])

    def test_actual_active_goal_resumes_without_new_turn_or_reset_usage(self):
        agent = self.runtime(actual=self.goal())
        state = self.begin(agent, resume=True)
        self.assertIsNone(state['turn_id'])
        self.assertFalse(any(method in ('thread/goal/set', 'turn/start') for method, params in agent.calls))
        self.assertEqual(123, agent.native_lifecycle['goal']['tokensUsed'])
        self.assertEqual(self.thread, state['thread_id'])

    def test_active_goal_adopts_auto_turn_and_steers_task_input_exactly_once(self):
        agent = self.runtime(actual=self.goal())
        self.begin(agent, resume=True)
        agent._consume_finish_event(self.started('native-auto-1'), [])
        agent._consume_finish_event(self.started('native-auto-1'), [])
        self.assertEqual('native-auto-1', agent.turn_id)
        requests = [(method, params) for method, params in agent.calls if method == 'turn/steer']
        self.assertEqual(1, len(requests))
        self.assertEqual('native-auto-1', requests[0][1]['expectedTurnId'])
        self.assertIsNone(agent._pending_goal_input)

    def test_existing_nonactive_owner_goal_is_not_implicitly_resumed(self):
        for status in ('paused', 'blocked', 'usageLimited', 'budgetLimited', 'complete'):
            with self.subTest(status=status):
                agent = self.runtime(actual=self.goal(status))
                self.begin(agent, resume=True)
                agent.finish(timeout=.1)
                self.assertFalse(any(method in ('thread/goal/set', 'turn/start', 'turn/interrupt')
                                     for method, params in agent.calls))
                self.assertEqual(status, agent.native_lifecycle['goal']['status'])
                self.assertTrue(agent.native_lifecycle['quiescent'])
                self.assertFalse(agent.native_lifecycle['settled'])

    def test_existing_active_different_objective_is_unknown_not_replaced_or_steered(self):
        agent = self.runtime(actual=self.goal(objective='Other unfinished owner goal'))
        with self.assertRaisesRegex(CodexError, 'codex_native_goal_objective_conflict'):
            self.begin(agent, resume=True)
        self.assertFalse(any(method in ('thread/goal/set', 'turn/start', 'turn/steer')
                             for method, params in agent.calls))

    def test_invalid_goal_or_mode_rejected_before_any_native_rpc(self):
        for change in ({'goal': 'bad'}, {'goal': {'objective': ''}},
                       {'goal': {'objective': self.objective, 'status': 'completed'}},
                       {'mode': 'not-native-mode'}):
            with self.subTest(change=change):
                agent = self.runtime()
                agent.config.update(change)
                with self.assertRaises(CodexError):
                    self.begin(agent, resume=True)
                self.assertEqual([], agent.calls)

    def test_multi_turn_goal_records_all_native_turns_and_only_terminal_idle_quiesces(self):
        agent = self.runtime()
        self.begin(agent)
        first = agent.turn_id
        plan = self.event('turn/plan/updated', turnId='native-auto-2',
                          plan=[{'step': 'verify actual evidence', 'status': 'completed'}])
        agent.deferred = [self.started(first), self.message(first, 'first turn'), self.completed(first),
                          self.started('native-auto-2'), plan, self.message('native-auto-2', 'verified result'),
                          self.event('thread/goal/updated', goal=self.goal('complete')),
                          self.completed('native-auto-2')]
        agent.actual_goal = self.goal('complete')
        answer = agent.finish(timeout=1)
        self.assertEqual('first turn\n\nverified result', answer)
        self.assertEqual([first, 'native-auto-2'], agent.native_lifecycle['turn_ids'])
        self.assertEqual([first, 'native-auto-2'], agent.native_lifecycle['completed_turn_ids'])
        self.assertEqual(plan['params'], agent.native_observations['plan'])
        self.assertTrue(agent.native_lifecycle['quiescent'])
        self.assertFalse(agent.native_lifecycle['settled'])

    def test_fast_answer_consumed_before_start_returns_is_retained_by_finish(self):
        agent = self.runtime()
        agent.before_reply['thread/goal/set'] = [self.message('owner-task-turn', 'fast verified answer'),
                                               self.completed('owner-task-turn')]
        original = agent.rpc

        def rpc(method, params, **kwargs):
            response = original(method, params, **kwargs)
            if method == 'thread/goal/set':
                agent.actual_goal = self.goal('complete')
                return {'goal': copy.deepcopy(agent.actual_goal)}
            return response

        agent.rpc = rpc
        self.begin(agent)
        self.assertEqual('fast verified answer', agent.finish(timeout=1))

    def test_active_idle_first_turn_completion_is_not_goal_settlement(self):
        agent = self.done_controller('active')
        self.assertFalse(agent._goal_quiescence_barrier([]))
        self.assertFalse(agent.native_lifecycle['settled'])

    def test_terminal_goal_with_current_active_turn_is_not_quiescent(self):
        agent = self.runtime()
        self.begin(agent)
        agent.actual_goal = self.goal('complete')
        self.assertFalse(agent._goal_quiescence_barrier([]))

    def test_terminal_goal_with_native_thread_active_or_unknown_is_not_quiescent(self):
        for kind in ('active', 'notLoaded', 'systemError'):
            with self.subTest(kind=kind):
                agent = self.done_controller()
                agent.actual_status = {'type': kind}
                self.assertFalse(agent._goal_quiescence_barrier([]))

    def test_older_complete_notification_cannot_overwrite_newer_active_goal_read(self):
        agent = self.done_controller('active')
        agent.before_reply['thread/goal/get'] = [self.event('thread/goal/updated', goal=self.goal('complete'))]
        self.assertFalse(agent._goal_quiescence_barrier([]))
        self.assertEqual('active', agent.native_lifecycle['goal']['status'])

    def test_older_idle_notification_cannot_overwrite_newer_active_thread_read(self):
        agent = self.done_controller()
        agent.actual_status = {'type': 'active'}
        agent.before_reply['thread/read'] = [self.event('thread/status/changed', status={'type': 'idle'})]
        self.assertFalse(agent._goal_quiescence_barrier([]))
        self.assertEqual('active', agent.native_lifecycle['thread_status']['type'])

    def test_later_active_notification_overrides_terminal_goal_read(self):
        agent = self.done_controller()
        agent.after_reply['thread/read'] = [self.event('thread/goal/updated', goal=self.goal('active'))]
        self.assertFalse(agent._goal_quiescence_barrier([]))
        self.assertEqual('active', agent.native_lifecycle['goal']['status'])

    def test_later_started_turn_prevents_quiescence_even_if_goal_read_complete(self):
        agent = self.done_controller()
        agent.after_reply['thread/read'] = [self.started('native-auto-late')]
        self.assertFalse(agent._goal_quiescence_barrier([]))
        self.assertEqual('native-auto-late', agent.turn_id)

    def test_foreign_thread_and_unstarted_turn_items_do_not_change_controller(self):
        agent = self.runtime()
        self.begin(agent)
        foreign = self.started('foreign-turn')
        foreign['params']['threadId'] = 'another-thread'
        self.assertFalse(agent._observe_native_event(foreign))
        self.assertFalse(agent._observe_native_event(self.message('unstarted-turn', 'not ours')))
        self.assertEqual('owner-task-turn', agent.turn_id)

    def test_overlapping_same_thread_turn_retains_unknown(self):
        agent = self.runtime()
        self.begin(agent)
        with self.assertRaisesRegex(CodexError, 'codex_native_turn_overlap'):
            agent._observe_native_event(self.started('overlapping-turn'))
        self.assertFalse(agent.native_lifecycle['settled'])

    def test_malformed_goal_read_is_not_treated_as_clear_or_completion(self):
        for response in ({}, {'goal': {'threadId': 'foreign', 'status': 'complete'}},
                         {'goal': self.goal('completed')}):
            with self.subTest(response=response):
                agent = self.done_controller()
                original = agent.rpc
                agent.rpc = lambda method, params, **kwargs: response if method == 'thread/goal/get' else original(method, params, **kwargs)
                with self.assertRaisesRegex(CodexError, 'codex_native_goal_snapshot_invalid'):
                    agent._goal_quiescence_barrier([])
                self.assertFalse(agent.native_lifecycle['settled'])

    def test_actual_clear_snapshot_can_quiesce_but_does_not_invent_complete_goal(self):
        agent = self.done_controller()
        agent.actual_goal = None
        self.assertTrue(agent._goal_quiescence_barrier([]))
        self.assertIsNone(agent.native_lifecycle['goal'])

    def test_managed_timeout_never_pauses_or_interrupts_owner_goal(self):
        agent = self.runtime()
        self.begin(agent)
        with self.assertRaisesRegex(CodexError, 'codex_turn_timeout'):
            agent.finish(timeout=0)
        agent.interrupt.assert_not_called()
        self.assertEqual('active', agent.native_lifecycle['goal']['status'])
        self.assertFalse(agent.native_lifecycle['settled'])

    def test_active_goal_child_yield_is_explicitly_unsupported_without_pause_or_release(self):
        agent = self.done_controller('active')
        before = copy.deepcopy(agent.calls)
        with self.assertRaisesRegex(CodexError, 'codex_goal_coordination_yield_unavailable'):
            agent.finish(timeout=1, yield_requested=lambda: True)
        self.assertEqual(before, agent.calls)
        agent.interrupt.assert_not_called()
        self.assertEqual('active', agent.native_lifecycle['goal']['status'])
        self.assertFalse(agent.native_lifecycle['settled'])

    def test_terminal_goal_child_wait_does_not_require_unsupported_active_yield(self):
        agent = self.done_controller('complete')
        agent._observe_native_event(self.event('thread/goal/updated', goal=self.goal('complete')))
        agent.finish(timeout=1, yield_requested=lambda: True)
        self.assertTrue(agent.native_lifecycle['quiescent'])

    def test_lease_loss_tick_stops_before_more_native_work_or_goal_status_write(self):
        agent = self.runtime()
        self.begin(agent)
        before = copy.deepcopy(agent.calls)
        with self.assertRaisesRegex(ValueError, 'lease lost'):
            agent.finish(tick=mock.Mock(side_effect=ValueError('lease lost')), timeout=1)
        self.assertEqual(before, agent.calls)
        agent.interrupt.assert_not_called()

    def test_owner_interruption_and_actual_pause_can_settle_without_model_pause_write(self):
        agent = self.runtime()
        self.begin(agent)
        turn = agent.turn_id
        agent.deferred = [self.event('thread/goal/updated', goal=self.goal('paused')),
                          self.completed(turn, status='interrupted')]
        agent.actual_goal = self.goal('paused')
        agent.finish(timeout=1)
        self.assertTrue(agent.native_lifecycle['quiescent'])
        self.assertEqual('paused', agent.native_lifecycle['goal']['status'])
        agent.interrupt.assert_not_called()

    def test_model_created_goal_after_plain_turn_is_observed_before_plain_return(self):
        agent = self.runtime(requested=False)
        self.begin(agent)
        turn = agent.turn_id
        agent.deferred = [self.message(turn, 'plain answer'), self.completed(turn)]
        agent.actual_goal = self.goal('complete')
        agent.finish(timeout=1)
        self.assertTrue(agent.native_lifecycle['goal_managed'])
        self.assertTrue(agent.native_lifecycle['quiescent'])
        self.assertFalse(agent.native_lifecycle['settled'])

    def test_seal_requires_actual_quiescence_and_reaped_native_runtime(self):
        agent = self.done_controller()
        with self.assertRaisesRegex(CodexError, 'codex_native_not_quiescent'):
            agent.seal_native_lifecycle()
        agent._goal_quiescence_barrier([])
        state = agent.seal_native_lifecycle()
        agent.close.assert_called_once_with()
        self.assertTrue(state['settled'])
        self.assertTrue(state['runtime_closed'])
        self.assertEqual(self.thread, state['thread_id'])

    def test_seal_late_work_goal_or_request_never_executes_tool_and_retains_unknown(self):
        late = [self.started('late-turn'), self.event('item/started', turnId='late-turn', item={}),
                self.event('thread/goal/updated', goal=self.goal('active')),
                self.event('thread/status/changed', status={'type': 'active'}),
                dict(self.event('item/tool/call', turnId='late-turn', name='mesh_test'), id=99)]
        for value in late:
            with self.subTest(value=value['method']):
                agent = self.done_controller()
                agent._goal_quiescence_barrier([])
                agent.events.put(value)
                agent.on_tool = mock.Mock()
                with self.assertRaises(CodexError):
                    agent.seal_native_lifecycle()
                agent.on_tool.assert_not_called()
                self.assertFalse(agent.native_lifecycle['settled'])

    def test_seal_malformed_late_status_is_fixed_error_not_unsanitized_crash(self):
        for status in (None, [], 'idle'):
            with self.subTest(status=status):
                agent = self.done_controller()
                agent._goal_quiescence_barrier([])
                agent.events.put(self.event('thread/status/changed', status=status))
                with self.assertRaisesRegex(CodexError, 'codex_native_late_status'):
                    agent.seal_native_lifecycle()
                self.assertFalse(agent.native_lifecycle['settled'])

    def test_rollout_read_cannot_ignore_native_work_started_after_finish(self):
        agent = self.done_controller()
        agent._goal_quiescence_barrier([])
        agent.actual_status = {'type': 'active'}
        with self.assertRaisesRegex(CodexError, 'codex_native_not_quiescent'):
            agent.native_rollout()
        self.assertFalse(agent.native_lifecycle['quiescent'])

    def test_seal_reap_or_reader_failure_does_not_release_effects(self):
        for failure in ('process', 'reader'):
            with self.subTest(failure=failure):
                agent = self.done_controller()
                agent._goal_quiescence_barrier([])
                if failure == 'process':
                    agent.process.poll.return_value = None
                else:
                    agent.reader = mock.Mock()
                    agent.reader.is_alive.return_value = True
                with self.assertRaises(CodexError):
                    agent.seal_native_lifecycle()
                self.assertFalse(agent.native_lifecycle['settled'])

    def test_nested_rpc_keeps_outer_response_and_exposes_depth_for_worker_gate(self):
        agent = self.runtime(requested=False)
        agent.serial, agent.rpc_depth = 0, 0
        agent.rpc = Codex.rpc.__get__(agent)
        sent, depths = [], []
        agent.send = lambda value: sent.append(copy.deepcopy(value))

        def tool(params):
            depths.append(agent.rpc_depth)
            return agent.rpc('nested/read', {})

        agent.on_tool = tool
        for value in ({'id': 50, 'method': 'item/tool/call', 'params': {}},
                      {'id': 1, 'result': {'outer': True}}, {'id': 2, 'result': {'inner': True}}):
            agent.events.put(value)
        self.assertEqual({'outer': True}, agent.rpc('outer/read', {}))
        self.assertEqual([1], depths)
        self.assertEqual(0, agent.rpc_depth)
        self.assertEqual({'id': 50, 'result': {'inner': True}}, sent[-1])
        self.assertEqual({}, agent._rpc_responses)


if __name__ == '__main__':
    unittest.main()
