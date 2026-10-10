"""Native notification fixtures only: no process, auth, model, or goal writes."""
import copy
import queue
import unittest

from assistant_mesh.codex import Codex


class NativePlanEventTests(unittest.TestCase):
    def setUp(self):
        self.agent = Codex.__new__(Codex)
        self.agent.thread_id, self.agent.turn_id = 'fixture-thread', 'fixture-turn'
        self.agent.native_observations = {'thread_id': self.agent.thread_id,
                                          'turn_id': self.agent.turn_id}
        self.agent.on_tool = self.agent.on_interaction = None
        self.activities, self.sent = [], []
        self.agent.on_activity = lambda name, params: self.activities.append((name, params))
        self.agent.send = self.sent.append
        self.agent.serial, self.agent.deferred, self.agent.events = 0, [], queue.Queue()

    def plan(self, **changes):
        params = {'threadId': self.agent.thread_id, 'turnId': self.agent.turn_id,
                  'explanation': 'Actual fixture notification, not a prompt-derived plan.',
                  'plan': [{'step': 'inspect', 'status': 'completed'},
                           {'step': 'verify', 'status': 'inProgress'},
                           {'step': 'handoff', 'status': 'pending'}]}
        params.update(changes)
        return {'method': 'turn/plan/updated', 'params': params}

    def goal(self, **changes):
        params = {'threadId': self.agent.thread_id, 'turnId': self.agent.turn_id,
                  'goal': {'threadId': self.agent.thread_id, 'objective': 'fixture native objective',
                           'status': 'active', 'tokenBudget': None, 'tokensUsed': 123,
                           'timeUsedSeconds': 45, 'createdAt': 100, 'updatedAt': 200}}
        params.update(changes)
        return {'method': 'thread/goal/updated', 'params': params}

    def completed(self, **changes):
        params = {'threadId': self.agent.thread_id,
                  'turn': {'id': self.agent.turn_id, 'status': 'completed', 'items': []}}
        params.update(changes)
        return {'method': 'turn/completed', 'params': params}

    def message(self, text, **changes):
        params = {'threadId': self.agent.thread_id, 'turnId': self.agent.turn_id,
                  'item': {'id': 'fixture-message', 'type': 'agentMessage',
                           'phase': 'final_answer', 'text': text}}
        params.update(changes)
        return {'method': 'item/completed', 'params': params}

    def test_actual_plan_and_native_status_spelling_preserved_verbatim(self):
        event = self.plan()
        before = copy.deepcopy(event)
        self.assertTrue(self.agent._observe_native_event(event))
        self.assertEqual(before['params'], self.agent.native_observations['plan'])
        self.assertEqual(('plan_updated', before['params']), self.activities[-1])
        self.assertEqual(['completed', 'inProgress', 'pending'],
                         [item['status'] for item in self.agent.native_observations['plan']['plan']])
        self.assertEqual(before, event)
        event['params']['plan'][0]['step'] = 'mutated incoming fixture'
        self.activities[-1][1]['plan'][1]['step'] = 'mutated callback copy'
        self.assertEqual(before['params'], self.agent.native_observations['plan'])

    def test_previous_turn_plan_cannot_replace_current_native_plan(self):
        current = self.plan()
        self.agent._observe_native_event(current)
        self.assertFalse(self.agent._observe_native_event(self.plan(turnId='previous-turn')))
        self.assertEqual(current['params'], self.agent.native_observations['plan'])
        self.assertEqual(1, len(self.activities))

    def test_foreign_or_missing_thread_turn_id_never_claims_plan_observation(self):
        for changes in ({'threadId': 'foreign-thread'}, {'threadId': None}, {'turnId': None}):
            with self.subTest(changes=changes):
                self.assertFalse(self.agent._observe_native_event(self.plan(**changes)))
        for key in ('threadId', 'turnId'):
            event = self.plan()
            del event['params'][key]
            self.assertFalse(self.agent._observe_native_event(event))
        self.assertNotIn('plan', self.agent.native_observations)
        self.assertEqual([], self.activities)

    def test_malformed_or_invented_snake_case_plan_not_accepted_as_native(self):
        for plan in (None, {}, [None], [{'step': 'bad', 'status': 'in_progress'}],
                     [{'step': 3, 'status': 'pending'}]):
            with self.subTest(plan=plan):
                self.assertFalse(self.agent._observe_native_event(self.plan(plan=plan)))
        self.assertTrue(self.agent._observe_native_event(self.plan(plan=[])))
        self.assertEqual([], self.agent.native_observations['plan']['plan'])

    def test_current_turn_started_preserves_raw_metadata_without_adopting_other_turn(self):
        event = {'method': 'turn/started', 'params': {'threadId': self.agent.thread_id,
                 'turn': {'id': self.agent.turn_id, 'status': 'inProgress', 'items': [],
                          'rootTurnId': 'fixture-root-turn', 'startedAt': 300}}}
        before = copy.deepcopy(event)
        self.assertTrue(self.agent._observe_native_event(event))
        self.assertEqual(before['params'], self.agent.native_observations['turn_started'])
        self.assertEqual(('turn_started', before['params']), self.activities[-1])
        other = copy.deepcopy(event)
        other['params']['turn']['id'] = 'other-goal-turn'
        self.assertFalse(self.agent._observe_native_event(other))
        self.assertEqual('fixture-turn', self.agent.turn_id)
        self.assertEqual(before['params'], self.agent.native_observations['turn_started'])

    def test_native_item_activity_keeps_existing_item_callback_shape_and_exact_identity(self):
        event = {'method': 'item/started', 'params': {'threadId': self.agent.thread_id,
                 'turnId': self.agent.turn_id, 'item': {'id': 'fixture-item', 'type': 'commandExecution'}}}
        self.assertTrue(self.agent._observe_native_event(event))
        self.assertEqual(('native_item', event['params']['item']), self.activities[-1])
        for key in ('threadId', 'turnId'):
            invalid = copy.deepcopy(event)
            del invalid['params'][key]
            self.assertFalse(self.agent._observe_native_event(invalid))
        self.assertEqual(1, len(self.activities))

    def test_goal_update_observes_real_full_snapshot_including_thread_level_update(self):
        for turn in (self.agent.turn_id, None):
            event = self.goal(turnId=turn)
            self.assertTrue(self.agent._observe_native_event(event))
            self.assertEqual(event['params']['goal'], self.agent.native_observations['goal'])
            self.assertEqual(event['params'], self.agent.native_observations['goal_updated'])
            self.assertEqual(('goal_updated', event['params']), self.activities[-1])
        event = self.goal()
        del event['params']['turnId']
        self.assertTrue(self.agent._observe_native_event(event))
        self.assertEqual([], self.sent)  # Observation is not thread/goal/set/get.

    def test_goal_foreign_thread_inner_identity_or_previous_turn_does_not_pollute_snapshot(self):
        for event in (self.goal(threadId='foreign-thread'), self.goal(threadId=None),
                      self.goal(turnId='previous-turn'), self.goal(goal={'threadId': 'foreign-thread'}),
                      self.goal(goal=None)):
            self.assertFalse(self.agent._observe_native_event(event))
        self.assertNotIn('goal', self.agent.native_observations)
        self.assertEqual([], self.activities)

    def test_goal_cleared_is_actual_thread_event_not_fabricated_completion(self):
        updated = self.goal()
        self.agent._observe_native_event(updated)
        event = {'method': 'thread/goal/cleared', 'params': {'threadId': self.agent.thread_id}}
        self.assertTrue(self.agent._observe_native_event(event))
        self.assertIsNone(self.agent.native_observations['goal'])
        self.assertEqual(event['params'], self.agent.native_observations['goal_cleared'])
        self.assertEqual(updated['params'], self.agent.native_observations['goal_updated'])
        self.assertEqual(('goal_cleared', event['params']), self.activities[-1])
        before = copy.deepcopy(self.agent.native_observations)
        for params in ({'threadId': 'foreign-thread'}, {}):
            self.assertFalse(self.agent._observe_native_event(
                {'method': 'thread/goal/cleared', 'params': params}))
        self.assertEqual(before, self.agent.native_observations)
        self.assertEqual([], self.sent)

    def test_rpc_deferred_notifications_keep_order_and_raw_events_until_finish(self):
        started = {'method': 'turn/started', 'params': {'threadId': self.agent.thread_id,
                   'turn': {'id': self.agent.turn_id, 'status': 'inProgress', 'items': []}}}
        notifications = [started, self.plan(), self.goal(),
                         {'method': 'thread/goal/cleared', 'params': {'threadId': self.agent.thread_id}}]
        before = copy.deepcopy(notifications)
        for event in notifications + [{'id': 1, 'result': {'read_only_fixture': True}},
                                      self.message('actual current answer'), self.completed()]:
            self.agent.events.put(event)
        self.assertEqual({'read_only_fixture': True}, self.agent.rpc('fixture/read', {}))
        self.assertEqual(before, self.agent.deferred)
        self.assertEqual([], self.activities)  # No recursive tick/native RPC in rpc wait.
        self.assertEqual('actual current answer', self.agent.finish(timeout=1))
        self.assertEqual(['turn_started', 'plan_updated', 'goal_updated', 'goal_cleared', 'turn_completed'],
                         [name for name, value in self.activities])
        self.assertEqual(before, notifications)
        self.assertEqual([], self.agent.deferred)
        self.assertEqual(1, len(self.sent))
        self.assertEqual('fixture/read', self.sent[0]['method'])

    def test_stale_foreign_or_unidentified_answers_and_completions_do_not_end_current_turn(self):
        self.agent.deferred = [self.message('previous answer', turnId='previous-turn'),
            self.message('foreign answer', threadId='foreign-thread'), self.message('unidentified', turnId=None),
            self.completed(turn={'id': 'previous-turn', 'status': 'completed', 'items': []}),
            self.completed(threadId='foreign-thread'), self.message('current'), self.completed()]
        self.assertEqual('current', self.agent.finish(timeout=1))
        self.assertEqual(self.completed()['params'], self.agent.native_observations['turn_completed'])
        self.assertNotIn('plan', self.agent.native_observations)
        self.assertNotIn('goal', self.agent.native_observations)

    def test_observer_without_optional_activity_callback_keeps_native_completion(self):
        del self.agent.on_activity
        self.agent.deferred = [self.message('current'), self.completed()]
        self.assertEqual('current', self.agent.finish(timeout=1))
        self.assertEqual(self.completed()['params'], self.agent.native_observations['turn_completed'])


if __name__ == '__main__':
    unittest.main()
