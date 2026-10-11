"""CLI/native-tool binding, no provider execution, account or model call."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.cli import main
from assistant_mesh.worker import Worker


class CapabilityCallEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.config = {'node_id': 'laptop', 'capability_calls': {
            'authority': 'cloud', 'journal_path': str(self.root / 'calls.db')}}
        self.config_path = self.private('worker.json', self.config)
        self.arguments = {'decision_id': 'original-decision', 'selection': {'fixture': True},
                          'candidates': [], 'rationale': 'model choice'}
        self.payload = self.private('arguments.json', self.arguments)
        self.client_patch = mock.patch('assistant_mesh.worker.Client')
        self.client_class = self.client_patch.start()
        self.calls_patch = mock.patch('assistant_mesh.capability_call.TaskCapabilityCalls')
        self.calls_class = self.calls_patch.start()
        self.calls = self.calls_class.return_value
        for method in ('call', 'status', 'result'):
            getattr(self.calls, method).return_value = {'phase': 'observed', 'automatic_invocation_replay': False}

    def tearDown(self):
        self.calls_patch.stop()
        self.client_patch.stop()
        self.tmp.cleanup()

    def private(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def cli(self, command='capability-call', extra=None, payload=True):
        argv = ['mesh', '--config', str(self.config_path), command, '--task-id', 'original-task',
                '--epoch', '7', '--operation-id', 'original-operation']
        if payload:
            argv += ['--payload-file', str(self.payload)]
        argv += extra or []
        output = io.StringIO()
        with mock.patch('sys.argv', argv), contextlib.redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def worker(self, config=None):
        worker = Worker(config or self.config, client=self.client_class.return_value)
        worker.tick = mock.Mock()
        worker.current = {'id': 'original-task', 'epoch': 7, 'checkpoint': {}}
        return worker

    @staticmethod
    def value(result):
        return json.loads(result['contentItems'][0]['text'])

    def test_cli_call_uses_owned_config_and_flag_task_metadata_only(self):
        self.assertEqual('observed', self.cli()['phase'])
        self.calls_class.assert_called_once_with(self.client_class.return_value,
            str(self.root / 'calls.db'), 'cloud', 'laptop', 'original-task', 7)
        self.calls.call.assert_called_once_with(operation_id='original-operation', **self.arguments)

    def test_cli_status_result_do_not_load_or_submit_model_request(self):
        for command, method in (('capability-status', 'status'), ('capability-result', 'result')):
            self.cli(command, payload=False)
            getattr(self.calls, method).assert_called_once_with('original-operation')
        self.calls.call.assert_not_called()

    def test_cli_model_cannot_override_owner_or_task_binding(self):
        for key in ('operation_id', 'task_id', 'task_epoch', 'node', 'authority', 'journal_path', 'actor'):
            self.payload = self.private('arguments.json', dict(self.arguments, **{key: 'forged'}))
            with self.subTest(key=key), self.assertRaises(SystemExit) as result:
                self.cli()
            self.assertEqual(2, result.exception.code)
        self.calls.call.assert_not_called()

    def test_cli_rejects_missing_payload_or_read_with_payload_before_client(self):
        for command, payload in (('capability-call', False), ('capability-status', True)):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                self.cli(command, payload=payload)
        self.client_class.assert_not_called()

    def test_cli_native_features_unchanged_when_not_configured(self):
        self.config_path = self.private('worker.json', {'node_id': 'laptop'})
        with self.assertRaises(SystemExit):
            self.cli()
        self.calls.call.assert_not_called()

    def test_cli_transport_error_has_fixed_diagnostic_without_replay_or_secret(self):
        self.calls.call.side_effect = OSError('PRIVATE_TOKEN PRIVATE_CHAT PRIVATE_PATH')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            self.cli()
        self.assertNotIn('PRIVATE_', output.getvalue())
        self.assertEqual(1, self.calls.call.call_count)

    def test_native_tool_uses_current_authenticated_worker_binding(self):
        worker = self.worker()
        response = worker.on_tool({'tool': 'mesh', 'callId': 'native-tool-call',
            'arguments': {'action': 'capability_call', 'arguments': dict(self.arguments, operation_id='original-operation')}})
        self.assertTrue(response['success'])
        self.calls_class.assert_called_once_with(self.client_class.return_value,
            str(self.root / 'calls.db'), 'cloud', 'laptop', 'original-task', 7)
        self.calls.call.assert_called_once_with(operation_id='original-operation', **self.arguments)

    def test_native_tool_rejects_runtime_identity_override(self):
        worker = self.worker()
        for key in ('task_id', 'task_epoch', 'node', 'authority', 'journal_path', 'actor'):
            response = worker.on_tool({'tool': 'mesh', 'callId': 'native-tool-call', 'arguments': {
                'action': 'capability_call', 'arguments': dict(self.arguments, operation_id='original-operation', **{key: 'forged'})}})
            self.assertFalse(response['success'])
        self.calls.call.assert_not_called()

    def test_native_status_result_preserve_original_operation_and_no_new_call(self):
        worker = self.worker()
        for action, method in (('capability_status', 'status'), ('capability_result', 'result')):
            response = worker.on_tool({'tool': 'mesh', 'callId': action, 'arguments': {
                'action': action, 'arguments': {'operation_id': 'original-operation'}}})
            self.assertTrue(response['success'])
            getattr(self.calls, method).assert_called_once_with(operation_id='original-operation')
        self.calls.call.assert_not_called()

    def test_native_missing_config_does_not_intercept_native_tooling(self):
        worker = self.worker({'node_id': 'laptop'})
        response = worker.on_tool({'tool': 'mesh', 'callId': 'call', 'arguments': {
            'action': 'capability_status', 'arguments': {'operation_id': 'original-operation'}}})
        self.assertFalse(response['success'])
        self.assertFalse(self.value(response)['native_tools_intercepted'])
        self.calls_class.assert_not_called()
