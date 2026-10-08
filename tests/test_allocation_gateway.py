"""Additional Mesh admission routing; fixtures do not execute native tools."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.cli import main
from assistant_mesh.worker import Worker


class AllocationGatewayTests(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.client.request.return_value = {'execution_verified': False}
        self.worker = Worker({'node_id': 'fixture'}, client=self.client)
        self.worker.current = {'id': 'actual-parent', 'epoch': 7}
        self.worker.tick = mock.Mock()

    def invoke(self, action, arguments):
        return self.worker.on_tool({'tool': 'mesh', 'callId': 'stable-call', 'arguments': {
            'action': 'allocation', 'arguments': {'action': action, 'arguments': arguments}}})

    def test_reserve_binds_actual_parent_without_mutating_model_arguments(self):
        arguments = {'operation_id': 'stable-op', 'plan': {'selections': []}}
        response = self.invoke('reserve', arguments)
        self.assertTrue(response['success'])
        self.client.request.assert_called_once_with('/v1/allocation/action', {
            'action': 'reserve', 'arguments': dict(arguments, task_id='actual-parent', task_epoch=7)})
        self.assertNotIn('task_id', arguments)
        self.assertFalse(json.loads(response['contentItems'][0]['text'])['execution_verified'])

    def test_model_cannot_supply_even_matching_task_identity(self):
        for field, value in (('task_id', 'actual-parent'), ('task_epoch', 7)):
            response = self.invoke('reserve', {field: value})
            self.assertFalse(response['success'])
        self.client.request.assert_not_called()

    def test_settlement_and_read_do_not_require_live_parent_identity(self):
        # Historical outcomes must remain reconcilable, not start new effects.
        for action in ('inspect', 'settle', 'unknown'):
            self.client.reset_mock()
            payload = {'operation_id': 'old-op'}
            self.assertTrue(self.invoke(action, payload)['success'])
            self.client.request.assert_called_once_with('/v1/allocation/action', {
                'action': action, 'arguments': payload})

    def test_transport_unknown_does_not_retry_or_assume_non_execution(self):
        self.client.request.side_effect = OSError('fixture transport error')
        response = self.invoke('start', {'operation_id': 'same-op'})
        value = json.loads(response['contentItems'][0]['text'])
        self.assertFalse(response['success'])
        self.assertEqual('unknown', value['outcome'])
        self.assertFalse(value['automatic_retry'])
        self.assertFalse(value['native_tools_intercepted'])
        self.assertEqual(1, self.client.request.call_count)

    def test_malformed_wrapper_never_reaches_api(self):
        for payload in ({'action': 'reserve', 'arguments': []}, {'action': 1}, {'action': 'reserve', 'actor': 'operator'}):
            response = self.worker.on_tool({'tool': 'mesh', 'callId': 'bad', 'arguments': {
                'action': 'allocation', 'arguments': payload}})
            self.assertFalse(response['success'])
        self.client.request.assert_not_called()

    def test_cli_uses_private_payload_and_allocation_endpoint(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            config, payload = root / 'worker.json', root / 'args.json'
            config.write_text(json.dumps({'control_url': 'http://127.0.0.1:17682', 'token_file': '/fixture/token'}))
            payload.write_text(json.dumps({'operation_id': 'old-op'}))
            config.chmod(0o600)
            payload.chmod(0o600)
            with mock.patch('sys.argv', ['mesh', '--config', str(config), 'allocation', '--action', 'inspect', '--payload-file', str(payload)]), \
                    mock.patch('assistant_mesh.worker.Client', return_value=self.client), \
                    mock.patch('sys.stdout', new=io.StringIO()):
                main()
            self.client.request.assert_called_once_with('/v1/allocation/action', {
                'action': 'inspect', 'arguments': {'operation_id': 'old-op'}})


if __name__ == '__main__':
    unittest.main()
