"""Additional Mesh tools degrade; genuine task fences still stop their task."""
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock

from assistant_mesh.worker import Worker


PRIVATE = 'private-body-and-token-never-in-model-output'


def error(code):
    value = urllib.error.HTTPError('http://127.0.0.1/private', code, PRIVATE, {}, io.BytesIO())
    value.close()
    return value


class MeshToolFailureTests(unittest.TestCase):
    def worker(self, failure, core=None):
        client = Mock()
        def request(path, body=None):
            if path == '/v1/task/update':
                if isinstance(core, BaseException):
                    raise core
                return {'ok': True} if core is None else core
            raise failure
        client.request.side_effect = request
        worker = Worker({'codex': {}}, client=client)
        worker.current = {'id': 'same-task', 'epoch': 7, 'checkpoint': {}}
        worker.tick = Mock()
        return worker, client

    def call(self, worker, name):
        return worker.on_tool({'tool': name, 'callId': 'same-operation',
                               'arguments': {'peer': 'authorized-peer'} if name == 'mesh_remote_delegate' else {}})

    def value(self, result):
        return json.loads(result['contentItems'][0]['text'])

    def test_all_extra_mesh_tools_return_unknown_for_rpc_or_transport_failure(self):
        for name in ('mesh_resource', 'mesh_remote_delegate', 'mesh_delegate', 'mesh_children',
                     'mesh_wait_children', 'mesh_remember', 'mesh_recall', 'mesh_notify'):
            for failure in (error(500), urllib.error.URLError(PRIVATE), ValueError(PRIVATE)):
                with self.subTest(tool=name, failure=type(failure).__name__):
                    worker, client = self.worker(failure)
                    output = self.call(worker, name)
                    self.assertFalse(output['success'])
                    body = self.value(output)
                    self.assertEqual('unknown', body['outcome'])
                    self.assertFalse(body['automatic_retry'])
                    self.assertFalse(body['retry_with_new_id'])
                    self.assertFalse(body['native_tools_intercepted'])
                    self.assertNotIn(PRIVATE, json.dumps(body))
                    self.assertEqual(1, client.request.call_count)
                    self.assertFalse(worker.wait_children)

    def test_task_business_conflicts_recheck_same_fence_without_new_action(self):
        for name in ('mesh_remote_delegate', 'mesh_delegate'):
            worker, client = self.worker(error(409))
            output = self.call(worker, name)
            self.assertFalse(output['success'])
            self.assertEqual(409, self.value(output)['http_status'])
            self.assertEqual(2, client.request.call_count)
            request = client.request.call_args_list[-1]
            self.assertEqual(('/v1/task/update', {'id': 'same-task', 'epoch': 7}), request[0])
            first = client.request.call_args_list[0][0][1]
            self.assertEqual('same-task:same-operation', first['call_id'])

    def test_real_task_fence_409_remains_strict(self):
        for name in ('mesh_remote_delegate', 'mesh_delegate'):
            worker, client = self.worker(error(409), core=error(409))
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.call(worker, name)
            self.assertEqual(409, failure.exception.code)

    def test_unknown_or_invalid_core_fence_never_assumes_lease_is_current(self):
        for core in (error(500), OSError(PRIVATE), {}, {'ok': False}):
            worker, client = self.worker(error(409), core=core)
            with self.assertRaises((OSError, ValueError)):
                self.call(worker, 'mesh_delegate')

    def test_resource_business_409_is_not_a_task_lease_probe(self):
        worker, client = self.worker(error(409), core=error(409))
        output = self.call(worker, 'mesh_resource')
        self.assertFalse(output['success'])
        self.assertEqual('resource_authority_rejected', self.value(output)['error'])
        self.assertEqual(1, client.request.call_count)

    def test_native_rpc_can_receive_mesh_failure_and_continue(self):
        from assistant_mesh.codex import Codex
        import queue
        worker, client = self.worker(error(500))
        native = Codex.__new__(Codex)
        native.events = queue.Queue()
        native.on_tool, native.on_interaction, native.on_activity = worker.on_tool, None, None
        native.send = Mock()
        native.events.put({'id': 1, 'method': 'item/tool/call', 'params': {
            'tool': 'mesh_resource', 'callId': 'same-operation', 'arguments': {}}})
        native.events.put({'method': 'native-event-after-mesh-failure', 'params': {}})
        native.event(timeout=.1)
        reply = native.send.call_args[0][0]
        self.assertFalse(reply['result']['success'])
        self.assertEqual('native-event-after-mesh-failure', native.event(timeout=.1)['method'])

    def test_core_tick_failures_are_not_swallowed_by_mesh_degradation(self):
        worker, client = self.worker(error(500))
        worker.tick.side_effect = error(409)
        with self.assertRaises(urllib.error.HTTPError):
            self.call(worker, 'mesh_resource')
        client.request.assert_not_called()
