"""Local routing only; no credential reads, network writes or real probes."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from assistant_mesh.cli import main
from assistant_mesh.network_inventory import NetworkInventoryError


class NetworkInventoryCLITests(unittest.TestCase):
    def invoke(self, argv):
        output, errors, code = io.StringIO(), io.StringIO(), 0
        with patch('sys.argv', ['mesh'] + argv), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                main()
            except SystemExit as error:
                code = error.code
        self.assertNotIn('DO_NOT_PRINT', output.getvalue() + errors.getvalue())
        return code, json.loads(output.getvalue()) if output.getvalue() else None

    def test_local_inventory_needs_no_config_client_or_credentials(self):
        with patch('assistant_mesh.network_inventory.inspect_network', return_value={'capabilities_advertised': False}) as inspect:
            with patch('assistant_mesh.cli.private_json') as config, patch('assistant_mesh.worker.Client') as client:
                code, report = self.invoke(['network-inventory', '--node-id', 'local-label'])
        self.assertEqual(0, code)
        self.assertFalse(report['capabilities_advertised'])
        inspect.assert_called_once_with('local-label', freshness_seconds=30)
        config.assert_not_called()
        client.assert_not_called()

    def test_ttl_is_explicitly_passed_not_a_managed_lease(self):
        with patch('assistant_mesh.network_inventory.inspect_network', return_value={'allocation_authorized': False}) as inspect:
            self.assertEqual(0, self.invoke(['network-inventory', '--node-id', 'local-label', '--freshness-seconds', '45'])[0])
        inspect.assert_called_once_with('local-label', freshness_seconds=45.0)

    def test_foreign_arguments_cannot_trigger_reads_or_managed_actions(self):
        cases = [[], ['--config', 'DO_NOT_PRINT'], ['--action', 'publish'],
                 ['--payload-file', 'DO_NOT_PRINT'], ['--owner-config', 'DO_NOT_PRINT'],
                 ['--include-unavailable'], ['--task-id', 'DO_NOT_PRINT'],
                 ['--not-a-real-argument', 'DO_NOT_PRINT']]
        with patch('assistant_mesh.network_inventory.inspect_network') as inspect, patch('assistant_mesh.cli.private_json') as config:
            for extra in cases:
                args = ['network-inventory'] if not extra else ['network-inventory', '--node-id', 'local-label'] + extra
                with self.subTest(extra=extra):
                    code, report = self.invoke(args)
                    self.assertEqual(2, code)
                    self.assertEqual(['network_arguments_invalid'], report['diagnostics'])
            inspect.assert_not_called()
            config.assert_not_called()

    def test_module_errors_are_redacted(self):
        for error, category in ((NetworkInventoryError('DO_NOT_PRINT'), 'network_inventory_invalid'),
                                (RuntimeError('DO_NOT_PRINT'), 'network_inventory_failed')):
            with self.subTest(category=category), patch('assistant_mesh.network_inventory.inspect_network', side_effect=error):
                code, report = self.invoke(['network-inventory', '--node-id', 'local-label'])
                self.assertEqual(2, code)
                self.assertEqual([category], report['diagnostics'])
                self.assertFalse(report['network_configuration_changed'])
                self.assertFalse(report['native_features_restricted'])

    def test_bad_number_is_redacted_and_other_commands_still_require_config(self):
        self.assertEqual(2, self.invoke(['network-inventory', '--node-id', 'local-label', '--freshness-seconds', 'DO_NOT_PRINT'])[0])
        for command in ('status', 'serve', 'worker', 'node', 'provider', 'tool-release'):
            with self.subTest(command=command):
                self.assertEqual(2, self.invoke([command])[0])

    def test_other_commands_reject_inventory_options_before_config_reads(self):
        with patch('assistant_mesh.cli.private_json') as config:
            code, _ = self.invoke(['--config', '/not-read/private.json', 'status', '--node-id', 'local-label'])
        self.assertEqual(2, code)
        config.assert_not_called()


if __name__ == '__main__':
    unittest.main()
