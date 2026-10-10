import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.cli import main


class TaskAllocationCLITests(unittest.TestCase):
    def setUp(self):
        self.original_umask = os.umask(0o077)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.write_private('client.json', {'private_marker': 'not-a-log-value'})
        self.client_patch = mock.patch('assistant_mesh.worker.Client')
        self.client = self.client_patch.start().return_value
        self.client.request.return_value = {'state': 'reserved'}

    def tearDown(self):
        self.client_patch.stop()
        self.temp.cleanup()
        os.umask(self.original_umask)

    def write_private(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def cli(self, payload, *flags):
        path = self.write_private('arguments.json', payload)
        argv = ['mesh', '--config', str(self.config), 'allocation',
                '--payload-file', str(path)] + list(flags)
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch('sys.argv', argv), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            main()
        return json.loads(output.getvalue())

    def rejected(self, payload, *flags):
        with self.assertRaises(SystemExit) as failure:
            self.cli(payload, *flags)
        self.assertEqual(2, failure.exception.code)
        self.client.request.assert_not_called()

    def test_reserve_flags_inject_only_task_metadata_and_preserve_operation(self):
        arguments = {'operation_id': 'original-operation', 'lease_seconds': 60,
                     'plan': {'selections': [{'capability_id': 'cap', 'epoch': 1}]}}
        self.assertEqual({'state': 'reserved'}, self.cli(arguments, '--action', 'reserve',
                                                      '--task-id', 'original-task', '--epoch', '7'))
        self.client.request.assert_called_once_with('/v1/allocation/action', {
            'action': 'reserve', 'arguments': dict(arguments, task_id='original-task', task_epoch=7)})

    def test_complete_reserve_payload_uses_selected_action(self):
        payload = {'action': 'reserve', 'arguments': {'operation_id': 'same-operation', 'plan': {}}}
        self.cli(payload, '--task-id', 'same-task', '--epoch', '1')
        self.assertEqual({'action': 'reserve', 'arguments': {'operation_id': 'same-operation',
                         'plan': {}, 'task_id': 'same-task', 'task_epoch': 1}},
                         self.client.request.call_args[0][1])

    def test_json_task_metadata_is_rejected_even_when_matching_flags(self):
        for key, value in (('task_id', 'task'), ('task_id', 'other-task'),
                           ('task_epoch', 2), ('task_epoch', 3)):
            with self.subTest(key=key, value=value):
                self.rejected({'operation_id': 'original-operation', key: value},
                              '--action', 'reserve', '--task-id', 'task', '--epoch', '2')

    def test_missing_flag_pair_is_rejected_before_any_rpc(self):
        for flags in (('--task-id', 'task'), ('--epoch', '1')):
            with self.subTest(flags=flags):
                self.rejected({'operation_id': 'original-operation'}, '--action', 'reserve', *flags)

    def test_empty_task_metadata_is_rejected(self):
        for task_id in ('', '   ', '\t'):
            with self.subTest(task_id=task_id):
                self.rejected({}, '--action', 'reserve', '--task-id', task_id, '--epoch', '1')

    def test_invalid_task_epochs_are_rejected_before_any_rpc(self):
        for epoch in ('0', '-1', '1.0', 'not-an-epoch'):
            with self.subTest(epoch=epoch):
                self.rejected({}, '--action', 'reserve', '--task-id', 'task', '--epoch', epoch)

    def test_other_actions_validate_flags_without_injecting_unsupported_fields(self):
        for action, arguments in (('inspect', {'operation_id': 'original-operation'}),
                                  ('cancel', {'operation_id': 'original-operation', 'epoch': 3}),
                                  ('define_pool', {'id': 'pool', 'dimensions': {}})):
            with self.subTest(action=action):
                self.client.request.reset_mock()
                self.cli(arguments, '--action', action, '--task-id', 'task', '--epoch', '2')
                self.client.request.assert_called_once_with('/v1/allocation/action', {
                    'action': action, 'arguments': arguments})

    def test_other_actions_reject_incomplete_or_invalid_metadata_pair(self):
        for flags in (('--task-id', 'task'), ('--epoch', '1'),
                      ('--task-id', 'task', '--epoch', '0')):
            with self.subTest(flags=flags):
                self.rejected({'operation_id': 'original-operation'}, '--action', 'inspect', *flags)

    def test_legacy_reserve_and_owner_json_contracts_remain_unchanged(self):
        for action, arguments in (('reserve', {'operation_id': 'original-operation',
                                               'task_id': 'legacy-task', 'task_epoch': 9, 'plan': {}}),
                                  ('define_pool', {'id': 'owner-pool', 'dimensions': {}})):
            with self.subTest(action=action):
                self.client.request.reset_mock()
                self.cli({'action': action, 'arguments': arguments})
                self.client.request.assert_called_once_with('/v1/allocation/action', {
                    'action': action, 'arguments': arguments})

    def test_failed_rpc_has_no_automatic_retry_or_new_operation_identity(self):
        self.client.request.side_effect = ValueError('authority_unavailable')
        with self.assertRaises(ValueError):
            self.cli({'operation_id': 'original-operation', 'plan': {}}, '--action', 'reserve',
                     '--task-id', 'original-task', '--epoch', '1')
        self.client.request.assert_called_once()
        self.assertEqual('original-operation', self.client.request.call_args[0][1]['arguments']['operation_id'])


if __name__ == '__main__':
    unittest.main()
