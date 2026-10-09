"""Sanitized OS metadata fixtures; not Internet or physical-host proof."""
import copy
import json
import os
import sys
import unittest
from unittest.mock import patch

from assistant_mesh import network_inventory as inventory


class FixtureRunner:
    def __init__(self):
        self.calls = []
        self.responses = {
            ('ip', '-j', 'link', 'show'): {'status': 'ok', 'stdout': json.dumps([
                {'ifname': 'lo', 'flags': ['LOOPBACK', 'UP', 'LOWER_UP'], 'operstate': 'UNKNOWN',
                 'link_type': 'loopback', 'address': 'DO_NOT_PRINT_MAC'},
                {'ifname': 'wlan0', 'flags': ['UP', 'LOWER_UP'], 'operstate': 'UP',
                 'link_type': 'ether', 'address': 'DO_NOT_PRINT_MAC'},
                {'ifname': 'eth0', 'flags': ['UP'], 'operstate': 'DOWN', 'link_type': 'ether',
                 'private-account': 'DO_NOT_PRINT_ACCOUNT'},
            ]).encode('utf8')},
            ('nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device', 'status'): {
                'status': 'ok', 'stdout': b'wlan0:wifi:connected\neth0:ethernet:unavailable\nlo:loopback:connected (externally)\n'},
            ('ip', '-j', '-4', 'route', 'show', 'default'): {'status': 'ok', 'stdout': json.dumps([
                {'dst': 'default', 'dev': 'wlan0', 'gateway': '192.168.43.1', 'prefsrc': '192.168.43.88',
                 'private-account': 'DO_NOT_PRINT_ACCOUNT'}]).encode('utf8')},
            ('ip', '-j', '-6', 'route', 'show', 'default'): {'status': 'ok', 'stdout': b'[]'},
        }

    def __call__(self, argv):
        self.calls.append(argv)
        return copy.deepcopy(self.responses[argv])


class NetworkInventoryTests(unittest.TestCase):
    def setUp(self):
        self.runner = FixtureRunner()

    def inspect(self, **kwargs):
        arguments = {'system': 'Linux', 'runner': self.runner, 'clock': lambda: 1000.0, 'environ': {}}
        arguments.update(kwargs)
        return inventory.inspect_network('fixture-laptop', **arguments)

    def test_actual_os_metadata_is_candidate_not_internet_or_permission_proof(self):
        report = self.inspect()
        self.assertEqual(inventory.FORMAT, report['format'])
        self.assertEqual('linux-os-metadata-v1', report['adapter'])
        self.assertEqual(1000.0, report['sample_time'])
        self.assertEqual(1030.0, report['deadline'])
        self.assertEqual(['ipv4', 'ipv6'], report['route_families_observed'])
        wireless = next(row for row in report['interfaces'] if row['id'] == 'wlan0')
        self.assertEqual('wifi', wireless['kind'])
        self.assertTrue(wireless['admin_up'])
        self.assertTrue(wireless['carrier_present'])
        self.assertTrue(wireless['manager_connected'])
        self.assertEqual('up', wireless['link_state'])
        wired = next(row for row in report['interfaces'] if row['id'] == 'eth0')
        self.assertEqual('ethernet', wired['kind'])
        self.assertFalse(wired['carrier_present'])
        self.assertEqual('down', wired['link_state'])
        self.assertEqual([{'interface': 'wlan0', 'family': 'ipv4', 'gateway_class': 'private', 'table': 'main'}],
                         report['default_routes'])
        self.assertEqual(1, len(report['candidates']))
        candidate = report['candidates'][0]
        self.assertEqual('network.egress', candidate['kind'])
        self.assertEqual('wifi', candidate['spec']['medium'])
        self.assertEqual(0, candidate['spec']['new_spend_minor'])
        observation = candidate['observations'][0]
        self.assertEqual('declared', observation['verification'])
        self.assertEqual('default_route_present', observation['metric'])
        self.assertTrue(observation['value'])
        self.assertFalse(observation['evidence']['independent_verification'])
        for key in ('network_configuration_changed', 'native_features_restricted', 'capabilities_advertised',
                    'allocation_authorized', 'internet_reachability_verified', 'independent_performance_verified',
                    'independent_physical_host_verified', 'independent_verification',
                    'new_paid_resource_provisioned', 'existing_network_billing_verified', 'proxy_application_verified'):
            self.assertIs(False, report[key])

    def test_no_ssid_address_mac_account_or_raw_route_is_disclosed(self):
        self.runner.responses[('ip', '-j', 'link', 'show')]['stdout'] = json.dumps([
            {'ifname': 'wlan0', 'flags': ['UP'], 'operstate': 'UP', 'link_type': 'ether',
             'address': 'DO_NOT_PRINT_MAC', 'SSID': 'DO_NOT_PRINT_SSID', 'auth': 'DO_NOT_PRINT_ACCOUNT',
             'addr_info': [{'local': '192.168.43.88'}]}]).encode('utf8')
        text = json.dumps(self.inspect())
        for secret in ('DO_NOT_PRINT', '192.168.43.1', '192.168.43.88', 'SSID', 'addr_info', 'prefsrc'):
            self.assertNotIn(secret, text)

    def test_proxy_membership_only_does_not_read_environment_values(self):
        class NamesOnly:
            def __contains__(self, name):
                return name in ('HTTP_PROXY', 'https_proxy', 'NO_PROXY')
            def __getitem__(self, name):
                raise AssertionError('DO_NOT_READ_PROXY_VALUE')
        report = self.inspect(environ=NamesOnly())
        self.assertEqual(['HTTP_PROXY', 'NO_PROXY', 'https_proxy'], report['proxy_environment_names'])
        self.assertFalse(report['proxy_endpoint_values_disclosed'])
        self.assertFalse(report['proxy_application_verified'])

    def test_only_four_fixed_metadata_commands_no_radio_or_route_change(self):
        self.inspect()
        self.assertEqual([
            ('ip', '-j', 'link', 'show'),
            ('nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device', 'status'),
            ('ip', '-j', '-4', 'route', 'show', 'default'),
            ('ip', '-j', '-6', 'route', 'show', 'default')], self.runner.calls)

    def test_absent_nmcli_keeps_interface_medium_unknown_without_assuming_wifi(self):
        self.runner.responses[('nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device', 'status')] = {'status': 'unavailable'}
        report = self.inspect()
        self.assertEqual('unknown', report['candidates'][0]['spec']['medium'])
        self.assertIn('network_manager_unavailable', report['diagnostics'])

    def test_absent_route_command_is_unknown_not_no_route_proof(self):
        self.runner.responses[('ip', '-j', '-4', 'route', 'show', 'default')] = {'status': 'unavailable'}
        report = self.inspect()
        self.assertEqual([], report['candidates'])
        self.assertEqual(['ipv6'], report['route_families_observed'])
        self.assertIn('default_ipv4_unavailable', report['diagnostics'])

    def test_failed_or_oversized_malformed_runner_output_never_becomes_healthy(self):
        for replacement in ({'status': 'timeout'}, {'status': 'too_large'}, {'status': 'failed'},
                            {'status': 'bad', 'stdout': b'DO_NOT_PRINT'},
                            {'status': 'ok', 'stdout': b'DO_NOT_PRINT_JSON'},
                            {'status': 'ok', 'stdout': b'x' * (inventory.MAX_COMMAND_BYTES + 1)}):
            with self.subTest(replacement=replacement['status']):
                self.runner.responses[('ip', '-j', '-4', 'route', 'show', 'default')] = replacement
                report = self.inspect()
                self.assertEqual([], report['candidates'])
                self.assertNotIn('DO_NOT_PRINT', json.dumps(report))
                self.assertNotIn('ipv4', report['route_families_observed'])

    def test_malformed_interface_name_and_invalid_metadata_are_not_disclosed(self):
        self.runner.responses[('ip', '-j', 'link', 'show')]['stdout'] = json.dumps([
            {'ifname': 'DO_NOT_PRINT\ncredential', 'flags': ['UP']},
            {'ifname': 'other0', 'flags': 'DO_NOT_PRINT_FLAGS'}]).encode('utf8')
        report = self.inspect()
        self.assertNotIn('DO_NOT_PRINT', json.dumps(report))
        self.assertIn('interface_metadata_invalid', report['diagnostics'])

    def test_gateway_classification_never_returns_addresses(self):
        pairs = [('192.168.5.1', 'private'), ('8.8.8.8', 'public'), ('127.0.0.1', 'loopback'),
                 ('fe80::1%wlan0', 'link_local'), ('0.0.0.0', 'unspecified'), (None, 'absent'),
                 ('DO_NOT_PRINT', 'unknown')]
        for value, expected in pairs:
            with self.subTest(value=value):
                self.assertEqual(expected, inventory._gateway(value))

    def test_route_duplicates_are_deduplicated_and_candidate_id_stable(self):
        key = ('ip', '-j', '-4', 'route', 'show', 'default')
        route = json.loads(self.runner.responses[key]['stdout'])[0]
        self.runner.responses[key]['stdout'] = json.dumps([route, route]).encode('utf8')
        one = self.inspect()
        two = self.inspect(clock=lambda: 1100.0)
        self.assertEqual(1, len(one['default_routes']))
        self.assertEqual(one['candidates'][0]['id'], two['candidates'][0]['id'])
        self.assertNotEqual(one['deadline'], two['deadline'])
        other = inventory.inspect_network('different-node', system='Linux', runner=self.runner,
                                          clock=lambda: 1000.0, environ={})
        self.assertNotEqual(one['candidates'][0]['id'], other['candidates'][0]['id'])

    def test_dual_stack_virtual_egress_does_not_become_physical_connectivity_proof(self):
        self.runner.responses[('nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device', 'status')]['stdout'] += b'tun0:tun:connected\n'
        self.runner.responses[('ip', '-j', '-6', 'route', 'show', 'default')]['stdout'] = json.dumps([
            {'dev': 'tun0', 'gateway': 'fe80::1', 'dst': 'default', 'table': 123}]).encode('utf8')
        report = self.inspect()
        tunnel = next(row for row in report['candidates'] if row['spec']['interface'] == 'tun0')
        self.assertEqual('virtual', tunnel['spec']['medium'])
        self.assertEqual(['ipv6'], tunnel['spec']['observed_default_route_families'])
        self.assertFalse(report['independent_physical_host_verified'])

    def test_non_linux_has_no_default_linux_commands_and_no_fake_route(self):
        for system in ('Windows', 'Darwin', 'HarmonyOS'):
            with self.subTest(system=system):
                report = self.inspect(system=system)
                self.assertFalse(report['adapter_configured'])
                self.assertEqual([], report['interfaces'])
                self.assertEqual([], report['candidates'])
                self.assertEqual(['os_adapter_not_configured'], report['diagnostics'])
        self.assertEqual([], self.runner.calls)

    def test_explicit_owner_adapter_can_discover_other_os_without_loading_source(self):
        class Adapter:
            name = 'owned-windows-metadata-v1'
            def collect(self, runner):
                return {'interfaces': [{'id': 'WiFi0', 'kind': 'wifi', 'admin_up': True,
                    'link_state': 'up', 'carrier_present': True, 'manager_connected': None}],
                    'default_routes': [{'interface': 'WiFi0', 'family': 'ipv4', 'gateway_class': 'private', 'table': 'main'}],
                    'route_families_observed': ['ipv4'], 'diagnostics': []}
        report = self.inspect(system='Windows', adapter=Adapter())
        self.assertTrue(report['adapter_configured'])
        self.assertEqual('owned-windows-metadata-v1', report['adapter'])
        self.assertEqual(1, len(report['candidates']))
        self.assertEqual([], self.runner.calls)

    def test_custom_adapter_cannot_add_raw_ssid_credentials_to_normalized_contract(self):
        class Adapter:
            name = 'owner-metadata'
            def collect(self, runner):
                return {'interfaces': [], 'default_routes': [], 'route_families_observed': [],
                        'diagnostics': [], 'ssid': 'DO_NOT_PRINT'}
        with self.assertRaisesRegex(inventory.NetworkInventoryError, 'adapter_contract_invalid'):
            self.inspect(adapter=Adapter())

    def test_adapter_failure_reports_fixed_category_no_exception_details(self):
        class Adapter:
            name = 'owner-metadata'
            def collect(self, runner):
                raise ValueError('DO_NOT_PRINT_CREDENTIAL')
        report = self.inspect(adapter=Adapter())
        self.assertEqual(['adapter_failed'], report['diagnostics'])
        self.assertNotIn('DO_NOT_PRINT', json.dumps(report))

    def test_collection_time_does_not_postdate_or_refresh_older_os_observations(self):
        values = iter((1000.0, 1035.0))
        report = self.inspect(clock=lambda: next(values))
        self.assertEqual(1000.0, report['sample_time'])
        self.assertEqual(1035.0, report['completed_time'])
        self.assertEqual(1030.0, report['deadline'])
        self.assertFalse(report['snapshot_fresh'])
        self.assertEqual(1000.0, report['candidates'][0]['observations'][0]['sample_time'])

    def test_freshness_clock_and_identity_invalid_inputs_rejected(self):
        for value in (True, 0, -1, 301, float('nan'), float('inf')):
            with self.subTest(value=value):
                with self.assertRaises(inventory.NetworkInventoryError):
                    self.inspect(freshness_seconds=value)
        for value in (True, -1, float('nan'), float('inf')):
            with self.subTest(clock=value):
                with self.assertRaises(inventory.NetworkInventoryError):
                    self.inspect(clock=lambda: value)
        values = iter((1000.0, 999.0))
        with self.assertRaises(inventory.NetworkInventoryError):
            self.inspect(clock=lambda: next(values))
        with self.assertRaises(inventory.NetworkInventoryError):
            inventory.inspect_network('bad\nnode', system='Linux', runner=self.runner)

    def test_private_environment_and_fixtures_not_mutated(self):
        environment = {'HTTP_PROXY': 'DO_NOT_PRINT_PROXY', 'other': 'DO_NOT_PRINT_ACCOUNT'}
        before = copy.deepcopy(self.runner.responses)
        with patch.dict(os.environ, environment, clear=True):
            snapshot = dict(os.environ)
            self.inspect(environ=environment)
            self.assertEqual(snapshot, dict(os.environ))
        self.assertEqual(before, self.runner.responses)

    def test_readonly_runner_invokes_argv_not_shell_and_bounds_finite_output(self):
        result = inventory.run_readonly((sys.executable, '-c', "print('fixture diagnostic')"))
        self.assertEqual('ok', result['status'])
        self.assertEqual(b'fixture diagnostic\n', result['stdout'])
        with self.assertRaises(inventory.NetworkInventoryError):
            inventory.run_readonly('ip -j link show')

    def test_readonly_runner_missing_command_nonzero_and_excess_output_fixed_categories(self):
        self.assertEqual({'status': 'unavailable'}, inventory.run_readonly(('/DO_NOT_EXIST/diagnostic',)))
        self.assertEqual({'status': 'failed'}, inventory.run_readonly((sys.executable, '-c', "raise SystemExit(3)")))
        code = "import os; os.write(1,b'x'*%d)" % (inventory.MAX_COMMAND_BYTES + 1024)
        self.assertEqual({'status': 'too_large'}, inventory.run_readonly((sys.executable, '-c', code)))

    def test_readonly_runner_timeout_ends_only_owned_diagnostic(self):
        with patch.object(inventory, 'COMMAND_TIMEOUT', 0.02):
            result = inventory.run_readonly((sys.executable, '-c', 'import time; time.sleep(2)'))
        self.assertEqual({'status': 'timeout'}, result)


if __name__ == '__main__':
    unittest.main()
