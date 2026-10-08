import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.codex import Codex, CodexError
from assistant_mesh.pi import Pi


class ProtocolTests(unittest.TestCase):
    def runtime(self, config=None):
        agent = Codex.__new__(Codex)
        agent.config = dict(workspace='/test-workspace', **(config or {}))
        agent.tools = [{'name': 'mesh_recall'}]
        agent.on_activity = None
        agent.on_interaction = None
        agent.on_tool = None
        agent.calls = []

        def rpc(method, params, **kwargs):
            agent.calls.append((method, params))
            if method in ('thread/start', 'thread/resume'):
                return {'thread': {'id': 'native-thread', 'model': 'account-model'}}
            if method == 'turn/start':
                return {'turn': {'id': 'native-turn'}}
            if method == 'model/list':
                return {'data': [{'id': 'catalog-model'}]}
            return {}

        agent.rpc = rpc
        return agent

    def test_resume_reuses_native_thread_and_preserves_rollout(self):
        agent = self.runtime({'model_policy': 'catalog-first'})
        state = agent.start('continue', {'thread_id': 'native-thread',
                                        'native_rollout_path': '/private/selected.jsonl'})
        request = next(params for method, params in agent.calls if method == 'thread/resume')
        self.assertEqual('native-thread', request['threadId'])
        self.assertEqual('/private/selected.jsonl', request['path'])
        self.assertEqual('catalog-model', request['model'])
        self.assertNotIn('dynamicTools', request)
        self.assertFalse(any(method == 'thread/start' for method, _ in agent.calls))
        self.assertEqual('native-thread', state['thread_id'])

    def test_plan_and_default_are_actual_native_collaboration_modes(self):
        for mode in ('plan', 'default'):
            with self.subTest(mode=mode):
                agent = self.runtime({'mode': mode})
                state = agent.start('input')
                turn = agent.calls[-1][1]
                self.assertEqual(mode, turn['collaborationMode']['mode'])
                self.assertIsNone(turn['collaborationMode']['settings']['developer_instructions'])
                self.assertEqual(mode, state['mode'])

    def test_goal_is_set_through_native_rpc_before_turn_starts(self):
        goal = {'objective': 'verify a result', 'status': 'active'}
        agent = self.runtime({'goal': goal})
        agent.start('work')
        methods = [method for method, _ in agent.calls]
        self.assertLess(methods.index('thread/goal/set'), methods.index('turn/start'))
        request = agent.calls[methods.index('thread/goal/set')][1]
        self.assertEqual(dict(goal, threadId='native-thread'), request)

    def test_steering_targets_current_turn_not_another_process(self):
        agent = self.runtime()
        agent.start('work')
        agent.steer('new detail')
        method, params = agent.calls[-1]
        self.assertEqual('turn/steer', method)
        self.assertEqual('native-turn', params['expectedTurnId'])
        self.assertEqual('native-thread', params['threadId'])

    def test_native_question_callback_forwards_actual_owner_answer(self):
        agent = self.runtime()
        agent.events = queue.Queue()
        sent = []
        answer = {'answers': {'choice': {'answers': ['A']}}}
        agent.on_interaction = mock.Mock(return_value=answer)
        agent.send = sent.append
        params = {'questions': [{'id': 'choice', 'question': 'Pick one'}]}
        agent.events.put({'id': 9, 'method': 'item/tool/requestUserInput', 'params': params})
        agent.event(timeout=.1)
        agent.on_interaction.assert_called_once_with('item/tool/requestUserInput', params)
        self.assertEqual([{'id': 9, 'result': answer}], sent)

    def test_permission_approval_is_not_reported_as_granted(self):
        agent = self.runtime()
        agent.events = queue.Queue()
        sent, activities = [], []
        agent.send = sent.append
        agent.on_activity = lambda name, params: activities.append(name)
        agent.events.put({'id': 10, 'method': 'item/permissions/requestApproval', 'params': {}})
        agent.event(timeout=.1)
        self.assertNotEqual({'decision': 'accept'}, sent[0].get('result'))
        self.assertEqual(['approval_required'], activities)

    def test_finish_ignores_commentary_and_other_thread_results(self):
        agent = self.runtime()
        agent.start('work')
        agent.deferred = [
            {'method': 'item/completed', 'params': {'threadId': 'another',
                'item': {'type': 'agentMessage', 'text': 'unrelated'}}},
            {'method': 'item/completed', 'params': {'threadId': 'native-thread',
                'item': {'type': 'agentMessage', 'phase': 'commentary', 'text': 'progress'}}},
            {'method': 'item/completed', 'params': {'threadId': 'native-thread',
                'item': {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'verified'}}},
            {'method': 'turn/completed', 'params': {'threadId': 'native-thread',
                'turn': {'id': 'native-turn', 'status': 'completed'}}},
        ]
        self.assertEqual('verified', agent.finish(timeout=1))

    def test_pi_grant_cleanup_on_spawn_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'cost_policy': 'local', 'model': 'local-model', 'workspace': directory,
                      'session_dir': directory, 'mesh_grant': {'opaque': 'test'}}
            with mock.patch('assistant_mesh.pi.subprocess.Popen', side_effect=OSError('missing')):
                with self.assertRaises(OSError):
                    Pi(config, tools=[{}])
            self.assertEqual([], list(Path(directory).glob('mesh-grant-*.json')))

    def test_pi_started_stream_enables_native_steering_gate(self):
        agent = Pi.__new__(Pi)
        agent.serial = 2
        agent.on_activity = None
        agent.rpc = mock.Mock(side_effect=[{'sessionId': 'session', 'sessionFile': '/private/pi.jsonl'},
                                           {'disposition': 'started'}])
        state = agent.start('work')
        self.assertEqual('pi-stream-2', agent.turn_id)
        self.assertEqual(agent.turn_id, state['turn_id'])


if __name__ == '__main__':
    unittest.main()
