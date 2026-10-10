"""Actual task authority and fake native boundaries; no accounts or processes."""
import copy
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from assistant_mesh.store import Store
from assistant_mesh.worker import Worker


class AuthorityClient:
    def __init__(self, store, node):
        self.store, self.node = store, node
        self.calls = []

    def request(self, route, body=None):
        self.calls.append((route, copy.deepcopy(body)))
        if route == '/v1/heartbeat':
            return self.store.heartbeat(self.node, body['capabilities'])
        if route == '/v1/task/update':
            return self.store.update_task(body['id'], self.node, body['epoch'],
                checkpoint=body.get('checkpoint'), result=body.get('result'), status=body.get('status'))
        if route == '/v1/agent/action':
            return self.store.agent_action(body['task_id'], self.node, body['epoch'],
                body['call_id'], body['action'], body['arguments'])
        if route == '/v1/steering':
            return {'steering': None}
        raise AssertionError('unexpected route ' + route)


class FakeCodexBoundary:
    """Observe only the callback deciding whether to drain this current turn."""
    def __init__(self):
        self.turn_id = 'original-turn'
        self.rpc_depth = 1
        self.native_drain_active = False
        self.native_lifecycle = {'thread_id': 'original-thread', 'settled': False,
            'runtime_closed': False, 'goal': {'status': 'active'}}
        self.drain_calls = 0
        self.consumed = []
        self.start = Mock()
        self.rpc = Mock()

    def after_tool_response(self, response, yield_requested):
        if yield_requested():
            self.drain_calls += 1
        else:
            self.consumed.append(json.loads(response['contentItems'][0]['text']))


class WaitChildrenReadyWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = 1000
        self.store = Store(Path(self.temp.name) / 'db', clock=lambda: self.clock)
        self.node = 'fixture-node'
        self.store.heartbeat(self.node, ['leader', 'agent'])
        self.parent = self.store.create_task('parent', task_id='original-parent')
        self.task = self.store.claim(self.node)
        self.task['checkpoint'].update(thread_id='original-thread', turn_id='original-turn',
            side_effect_started=True, wait_children_requested=True,
            native_execution_intent={'task_id': self.parent, 'task_epoch': self.task['epoch'], 'settled': False})
        self.store.update_task(self.parent, self.node, self.task['epoch'], checkpoint=self.task['checkpoint'])
        self.client = AuthorityClient(self.store, self.node)
        self.worker = Worker({'node_id': self.node, 'codex': {}}, client=self.client)
        self.worker.current = self.task
        self.worker.agent = FakeCodexBoundary()
        self.worker._native_turn_live = True
        self.worker.wait_children = True

    def child(self, identity, status=None, text=None):
        child = self.store.agent_action(self.parent, self.node, self.task['epoch'],
            'child:' + identity, 'delegate', {'input': identity, 'agent_id': identity})
        if status is not None:
            claimed = self.store.claim(self.node)
            self.assertEqual(child['id'], claimed['id'])
            self.store.update_task(child['id'], self.node, claimed['epoch'],
                result=text, status=status)
        return child['id']

    def call(self, identity='wait-1', gateway=False):
        call = {'tool': 'mesh', 'callId': identity,
                'arguments': {'action': 'wait_children', 'arguments': {}}} if gateway else {
            'tool': 'mesh_wait_children', 'callId': identity, 'arguments': {}}
        return self.worker.on_tool(call)

    def checkpoint(self):
        with self.store.transaction() as db:
            return json.loads(db.execute('SELECT checkpoint FROM tasks WHERE id=?', (self.parent,)).fetchone()[0])

    def assert_original_guard(self):
        for state in (self.task['checkpoint'], self.checkpoint()):
            self.assertTrue(state['side_effect_started'])
            self.assertFalse(state['native_execution_intent']['settled'])
        self.assertEqual('original-parent', self.task['id'])
        self.assertEqual('original-thread', self.task['checkpoint']['thread_id'])
        self.worker.agent.start.assert_not_called()
        self.worker.agent.rpc.assert_not_called()

    def test_all_children_ready_consume_real_results_in_same_turn_without_drain(self):
        child = self.child('specialist', 'completed', 'actual-child-marker')
        response = self.call()
        value = json.loads(response['contentItems'][0]['text'])
        self.assertIs(value['continue_after_children'], False)
        self.assertIn({'id': child, 'status': 'completed', 'result': 'actual-child-marker'}, value['tasks'])
        self.assertFalse(self.worker.wait_children)
        self.assertFalse(self.task['checkpoint']['wait_children_requested'])
        self.worker.agent.after_tool_response(response, lambda: self.worker.wait_children)
        self.assertEqual(0, self.worker.agent.drain_calls)
        self.assertEqual([value], self.worker.agent.consumed)
        self.worker.tick(force=True)
        self.assertFalse(self.checkpoint()['wait_children_requested'])
        self.assertEqual('running', self.store.task_status(self.parent)['status'])
        self.assert_original_guard()

    def test_gateway_wait_has_same_explicit_false_semantics(self):
        self.child('specialist', 'completed', 'gateway-marker')
        response = self.call(gateway=True)
        self.assertTrue(response['success'])
        self.assertFalse(self.worker.wait_children)
        self.assertFalse(self.task['checkpoint']['wait_children_requested'])
        with patch.object(self.client, 'request', return_value={'capabilities': []}):
            reference = self.worker.resource_reference()
        self.assertIn('false', reference['gateway']['wait_children'])
        self.assertIn('实际结果', reference['gateway']['wait_children'])
        self.assertIn('continue_after_children=true', reference['gateway']['wait_children'])
        self.assert_original_guard()

    def test_no_children_is_ready_not_an_empty_wait_resume_loop(self):
        response = self.call()
        value = json.loads(response['contentItems'][0]['text'])
        self.assertIs(value['continue_after_children'], False)
        self.assertEqual([], value['tasks'])
        self.assertFalse(self.worker.wait_children)
        self.assert_original_guard()

    def test_terminal_failed_or_needs_review_children_are_returned_not_hidden(self):
        for status in ('completed', 'failed', 'needs_review'):
            with self.subTest(status=status):
                child = self.child(status, status, 'child-result:' + status)
                response = self.call('wait:' + status)
                value = json.loads(response['contentItems'][0]['text'])
                self.assertIs(value['continue_after_children'], False)
                self.assertIn({'id': child, 'status': status, 'result': 'child-result:' + status}, value['tasks'])
                self.assertFalse(self.worker.wait_children)
                self.assert_original_guard()

    def test_pending_child_retains_explicit_true_wait_and_guard(self):
        self.child('pending-child')
        response = self.call()
        self.assertIs(json.loads(response['contentItems'][0]['text'])['continue_after_children'], True)
        self.assertTrue(self.worker.wait_children)
        self.assertTrue(self.task['checkpoint']['wait_children_requested'])
        self.worker.agent.after_tool_response(response, lambda: self.worker.wait_children)
        self.assertEqual(1, self.worker.agent.drain_calls)
        self.assert_original_guard()

    def test_running_or_backend_wait_child_is_not_guessed_complete(self):
        for status in ('running', 'waiting_backend', 'waiting_auth'):
            with self.subTest(status=status):
                child = self.child(status)
                claimed = self.store.claim(self.node)
                self.assertEqual(child, claimed['id'])
                if status != 'running':
                    self.store.update_task(child, self.node, claimed['epoch'], status=status)
                response = self.call('wait:' + status)
                self.assertIs(json.loads(response['contentItems'][0]['text'])['continue_after_children'], True)
                self.assertTrue(self.worker.wait_children)
                self.assert_original_guard()

    def test_explicit_false_only_not_false_like_or_legacy_missing(self):
        for value in ({}, {'continue_after_children': True}, {'continue_after_children': None},
                      {'continue_after_children': 0}, {'continue_after_children': ''},
                      {'continue_after_children': []}, {'continue_after_children': {}}, []):
            with self.subTest(value=value):
                self.worker.wait_children = False
                self.task['checkpoint']['wait_children_requested'] = False
                request = self.client.request
                def legacy(route, body=None):
                    if route == '/v1/agent/action':
                        return copy.deepcopy(value)
                    return request(route, body)
                with patch.object(self.client, 'request', side_effect=legacy):
                    self.call()
                self.assertTrue(self.worker.wait_children)
                self.assertTrue(self.task['checkpoint']['wait_children_requested'])
                self.assert_original_guard()

    def test_explicit_false_receipt_never_means_native_effects_are_zero(self):
        request = self.client.request
        response = {'continue_after_children': False, 'tasks': [
            {'id': 'original-child', 'status': 'completed', 'result': 'opaque result'}]}
        def ready(route, body=None):
            return response if route == '/v1/agent/action' else request(route, body)
        with patch.object(self.client, 'request', side_effect=ready):
            value = self.call()
        self.assertFalse(self.worker.wait_children)
        self.assertFalse(self.task['checkpoint']['wait_children_requested'])
        self.assertTrue(value['success'])
        self.assertFalse(self.worker.agent.native_lifecycle['settled'])
        self.assertFalse(self.worker.agent.native_lifecycle['runtime_closed'])
        self.assert_original_guard()

    def test_late_ready_false_does_not_cancel_already_admitted_native_drain(self):
        self.worker.agent.native_drain_active = True
        self.worker.agent.seal_native_yield = Mock()
        response = self.call('admitted-host-request')  # Actual Store/lease transaction.
        ready = json.loads(response['contentItems'][0]['text'])
        self.assertIs(ready['continue_after_children'], False)
        self.assertEqual([], ready['tasks'])
        self.assertFalse(self.checkpoint()['wait_children_requested'])
        self.assertTrue(self.worker.wait_children)
        self.assertTrue(self.task['checkpoint']['wait_children_requested'])
        self.worker.agent.after_tool_response(response, lambda: self.worker.wait_children)
        self.assertEqual(1, self.worker.agent.drain_calls)
        self.assert_original_guard()
        # Store recorded a new ready snapshot; the already-admitted original
        # wait is restored by the same forced checkpoint persistence used by
        # native_drained/seal/upload, without settling any effect guard.
        self.worker.tick(force=True)
        self.assertTrue(self.checkpoint()['wait_children_requested'])
        self.assert_original_guard()

        # A second admitted request may again obtain actual ready=False. If its
        # original lease is lost before the forced checkpoint, do not infer
        # settlement from the temporary DB/local wait disagreement or seal.
        response = self.call('admitted-late-before-lease-loss')
        self.assertIs(json.loads(response['contentItems'][0]['text'])['continue_after_children'], False)
        self.assertFalse(self.checkpoint()['wait_children_requested'])
        self.assertTrue(self.worker.wait_children)
        self.clock = 1100  # Actual parent lease and Leader fence have expired.
        with self.assertRaisesRegex(ValueError, 'stale_task_lease'):
            self.worker.tick(force=True)
        self.assertFalse(self.checkpoint()['wait_children_requested'])
        self.assertTrue(self.task['checkpoint']['wait_children_requested'])
        self.worker.agent.seal_native_yield.assert_not_called()
        self.assert_original_guard()

    def test_failed_managed_wait_never_clears_existing_wait_or_effect_guard(self):
        for initial in (True, False):
            with self.subTest(initial=initial):
                self.worker.wait_children = initial
                self.task['checkpoint']['wait_children_requested'] = initial
                request = self.client.request
                def lost(route, body=None):
                    if route == '/v1/agent/action':
                        raise OSError('private transport detail')
                    return request(route, body)
                with patch.object(self.client, 'request', side_effect=lost):
                    response = self.call()
                self.assertFalse(response['success'])
                self.assertIs(self.worker.wait_children, initial)
                self.assertIs(self.task['checkpoint']['wait_children_requested'], initial)
                self.assert_original_guard()

    def test_409_wait_with_lost_original_fence_raises_without_local_ready(self):
        request = self.client.request
        error = urllib.error.HTTPError('https://fixture', 409, 'conflict', {}, None)
        self.addCleanup(error.close)
        def conflict(route, body=None):
            if route == '/v1/agent/action':
                raise error
            if route == '/v1/task/update' and not body.get('checkpoint'):
                return {'ok': False}
            return request(route, body)
        with patch.object(self.client, 'request', side_effect=conflict):
            with self.assertRaisesRegex(ValueError, 'task_fence_response_invalid'):
                self.call()
        self.assertTrue(self.worker.wait_children)
        self.assertTrue(self.task['checkpoint']['wait_children_requested'])
        self.assert_original_guard()

    def test_child_finishing_after_wait_response_does_not_reinterpret_original_receipt(self):
        child = self.child('racing-child')
        response = self.call('original-wait')
        self.assertTrue(self.worker.wait_children)
        claimed = self.store.claim(self.node)
        self.assertEqual(child, claimed['id'])
        self.store.update_task(child, self.node, claimed['epoch'], status='completed', result='race-marker')
        self.worker.agent.after_tool_response(response, lambda: self.worker.wait_children)
        self.assertEqual(1, self.worker.agent.drain_calls)
        # An original action receipt is immutable. Re-reading it is not a new
        # authorization to decide that the same wait never happened.
        replay = self.call('original-wait')
        self.assertTrue(json.loads(replay['contentItems'][0]['text'])['continue_after_children'])
        self.assertTrue(self.worker.wait_children)
        ready = self.call('new-model-wait')
        self.assertIs(json.loads(ready['contentItems'][0]['text'])['continue_after_children'], False)
        self.assertFalse(self.worker.wait_children)
        self.assert_original_guard()


if __name__ == '__main__':
    unittest.main()
