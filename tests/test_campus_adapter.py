"""Campus network/callback contracts; all network replies are synthetic."""
import base64
import copy
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.campus_adapter import CampusAdapterError, execute, read_artifact
from assistant_mesh.provider_runtime import ProviderRuntime
from assistant_mesh.server import API


class BoundClient:
    def __init__(self, api):
        self.api = api

    def request(self, path, body=None):
        return self.api.dispatch('GET' if body is None else 'POST', path, body,
                                 {'role': 'worker', 'node': 'campus-node'})


class CampusAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-campus-fixture.')
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir(mode=0o700)
        self.owner = self.root / 'campus-owner.json'
        self.config = {'schema': 1, 'provider': 'node:campus-node', 'capability_id': 'campus-selective',
            'capability_epoch': 1, 'action': 'network.campus.http', 'egress_node': 'campus-node',
            'artifact_root': str(self.artifacts), 'allowed_networks': ['10.42.0.0/16', 'fd42::/64'],
            'allowed_ports': [[80, 80], [443, 443], [8080, 8082]], 'allowed_schemes': ['http', 'https'],
            'expires_at': time.time() + 120, 'max_bytes': 65536, 'timeout_seconds': 20}
        self.save()
        self.context = {'provider': 'node:campus-node', 'capability_id': 'campus-selective',
            'capability_epoch': 1, 'action': 'network.campus.http', 'operation_id': 'original-op',
            'task_id': 'parent', 'task_epoch': 1, 'receipt_id': 'original-receipt',
            'adapter_handle': 'owner.campus.v1', 'invocation_nonce': 'a' * 32,
            'scope': {'operation': 'get', 'url': 'http://10.42.1.5/entry'},
            'workload': {'kind': 'campus_http', 'max_bytes': 4096, 'timeout_seconds': 5}}
        self.body = b'fixture response, not a verified campus page\n\x00\xff'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, text):
        path.write_text(text, encoding='utf8')
        path.chmod(0o600)

    def save(self):
        self.write(self.owner, json.dumps(self.config))
        self.sha = hashlib.sha256(self.owner.read_bytes()).hexdigest()

    def invoke(self, context=None, status=200, address='10.42.1.5', tls=0, code=0, body=None):
        body = self.body if body is None else body
        reply = body + ('\nMESH_CAMPUS_METADATA:%d %s %d' % (status, address, tls)).encode('ascii')
        with mock.patch('assistant_mesh.campus_adapter._bounded_capture', return_value=(code, reply)) as capture:
            result = execute(self.context if context is None else context, self.owner, self.sha)
        return result, capture

    def test_http_private_network_is_allowed_and_artifact_hashes_match(self):
        result, capture = self.invoke()
        evidence = result['evidence']
        self.assertEqual('completed', result['outcome'])
        self.assertFalse(evidence['tls_used'])
        self.assertFalse(evidence['tls_verified'])
        self.assertIsNone(evidence['tls_verify_result'])
        value = read_artifact(self.owner, self.sha, evidence['artifact_id'], evidence['artifact_sha256'])
        self.assertEqual(self.body, base64.b64decode(value['response_base64']))
        self.assertEqual('original-op', value['operation_id'])
        self.assertEqual('original-receipt', value['receipt_id'])
        self.assertEqual('a' * 32, value['invocation_nonce'])
        path = self.artifacts / evidence['artifact_id']
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual('mesh-campus-http-artifact:sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(),
                         result['result_reference'])
        self.assertNotIn('10.42.', json.dumps(result))
        self.assertNotIn(str(self.owner), json.dumps(result))
        self.assertNotIn('fixture response', json.dumps(result))

    def test_model_changes_target_within_one_owner_network_without_reconfiguration(self):
        self.invoke()
        context = copy.deepcopy(self.context)
        context['operation_id'] = 'another-explicit-work'
        context['scope']['url'] = 'http://10.42.9.18:8081/another/path'
        result, capture = self.invoke(context, address='10.42.9.18')
        self.assertEqual(context['scope']['url'], capture.call_args[0][0][-1])
        self.assertEqual('http', result['evidence']['scheme'])
        self.assertEqual(self.sha, hashlib.sha256(self.owner.read_bytes()).hexdigest())

    def test_dns_hostname_uses_model_scoped_address_pin_and_retains_tls_name(self):
        self.context['scope'] = {'operation': 'get', 'url': 'https://portal.example.edu/entry', 'address': '10.42.1.5'}
        result, capture = self.invoke()
        command = capture.call_args[0][0]
        self.assertEqual('portal.example.edu:443:10.42.1.5', command[command.index('--resolve') + 1])
        self.assertEqual('https://portal.example.edu/entry', command[-1])
        self.assertTrue(result['evidence']['tls_verified'])
        self.assertFalse(result['evidence']['authentication_verified'])
        self.assertFalse(result['evidence']['target_content_independently_verified'])
        self.assertNotIn('--insecure', command)

    def test_dns_without_scoped_address_does_not_resolve_or_fetch(self):
        self.context['scope']['url'] = 'http://portal.example.edu/entry'
        with mock.patch('assistant_mesh.campus_adapter._bounded_capture') as capture:
            with self.assertRaisesRegex(CampusAdapterError, 'requires_scoped_address'):
                execute(self.context, self.owner, self.sha)
            capture.assert_not_called()
        self.assertEqual([], list(self.artifacts.iterdir()))

    def test_ipv6_literal_with_port_within_owner_range(self):
        self.context['scope']['url'] = 'http://[fd42::15]:8080/entry'
        result, capture = self.invoke(address='fd42::15')
        command = capture.call_args[0][0]
        self.assertNotIn('--resolve', command)  # literal URL needs no DNS at all
        self.assertEqual('http://[fd42::15]:8080/entry', command[-1])
        self.assertTrue(result['evidence']['connected_address_matches_owner_pin'])

    def test_address_network_and_port_mismatch_rejected_before_any_intent(self):
        for url in ('http://10.43.1.5/entry', 'http://10.42.1.5:22/entry',
                    'http://127.0.0.1/entry', 'http://169.254.169.254/entry',
                    'http://[fd43::15]/entry'):
            context = copy.deepcopy(self.context)
            context['scope']['url'] = url
            with mock.patch('assistant_mesh.campus_adapter._bounded_capture') as capture:
                with self.assertRaises(CampusAdapterError):
                    execute(context, self.owner, self.sha)
                capture.assert_not_called()
        self.assertEqual([], list(self.artifacts.iterdir()))

    def test_dns_pin_outside_network_and_literal_pin_disagreement_rejected(self):
        for scope in ({'operation': 'get', 'url': 'http://portal.example.edu/', 'address': '8.8.8.8'},
                      {'operation': 'get', 'url': 'http://10.42.1.5/', 'address': '10.42.1.6'}):
            context = dict(self.context, scope=scope)
            with self.assertRaises(CampusAdapterError):
                self.invoke(context)

    def test_ambiguous_url_and_other_schemes_cannot_bypass_scope(self):
        for url in ('http://name:SECRET@10.42.1.5/', 'http://10.42.1.5/?secret=x',
                    'http://10.42.1.5/#x', 'ftp://10.42.1.5/', 'file:///etc/passwd',
                    'http://10.42.1.5\\@8.8.8.8/', 'http://10.42.1.5:/',
                    'http://10.42.1.5:080/', 'http://127.1/', 'http://0x7f000001/'):
            with self.subTest(url=url):
                context = copy.deepcopy(self.context)
                context['scope']['url'] = url
                with self.assertRaises(CampusAdapterError):
                    self.invoke(context)
        self.assertEqual([], list(self.artifacts.iterdir()))

    def test_nonsecret_query_encoded_and_never_disclosed_in_receipt(self):
        self.context['scope']['query'] = {'search': 'mesh & agent', 'page': '2'}
        result, capture = self.invoke()
        self.assertEqual('http://10.42.1.5/entry?page=2&search=mesh+%26+agent', capture.call_args[0][0][-1])
        self.assertNotIn('search=', json.dumps(result))

    def test_query_credentials_and_scope_config_overrides_rejected(self):
        for extra in ({'query': {'cookie': 'PRIVATE'}}, {'query': {'access_token': 'PRIVATE'}},
                      {'query': {'search': 'https://user:SECRET@private.example/'}},
                      {'owner_config_path': '/private/config'}, {'headers': {'Cookie': 'PRIVATE'}},
                      {'method': 'POST'}, {'proxy': 'http://private.example/'}):
            context = copy.deepcopy(self.context)
            context['scope'].update(extra)
            with self.assertRaises(CampusAdapterError):
                self.invoke(context)
        self.assertEqual([], list(self.artifacts.iterdir()))

    def test_inherited_proxy_bypassed_and_no_redirect_retry_auth_configuration(self):
        inherited = {'ALL_PROXY': 'PRIVATE', 'HTTP_PROXY': 'PRIVATE', 'HTTPS_PROXY': 'PRIVATE',
                     'all_proxy': 'PRIVATE', 'NO_PROXY': 'nothing'}
        with mock.patch.dict(os.environ, inherited):
            before = dict(os.environ)
            result, capture = self.invoke(status=302)
            self.assertEqual(before, dict(os.environ))
        command = capture.call_args[0][0]
        self.assertEqual('', command[command.index('--proxy') + 1])
        self.assertEqual('*', command[command.index('--noproxy') + 1])
        self.assertEqual('--disable', command[1])
        for option in ('--location', '--retry', '--cookie', '--user', '--netrc', '--insecure'):
            self.assertNotIn(option, command)
        self.assertEqual(302, result['evidence']['http_status'])
        self.assertFalse(result['evidence']['redirects_followed'])
        self.assertEqual(1, capture.call_count)

    def test_probe_headers_private_and_never_authentication_evidence(self):
        self.context['scope']['operation'] = 'probe'
        result, capture = self.invoke(body=b'HTTP/1.1 401 Unauthorized\r\nSet-Cookie: PRIVATE\r\n', status=401)
        self.assertIn('--head', capture.call_args[0][0])
        self.assertEqual('headers', result['evidence']['response_kind'])
        self.assertFalse(result['evidence']['authentication_verified'])
        self.assertFalse(result['evidence']['http_success'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_mismatched_actual_ip_failed_tls_timeout_and_oversize_unknown(self):
        for index, values in enumerate(({'address': '10.42.1.6'}, {'address': '8.8.8.8'},
                                      {'code': 28}, {'code': 60, 'tls': 20},
                                      {'tls': 20}, {'status': 0}, {'body': b'x' * 4097})):
            self.context['operation_id'] = 'fault-%d' % index
            with self.assertRaisesRegex(CampusAdapterError, 'request_outcome_unknown'):
                self.invoke(**values)
        self.assertEqual([], list(self.artifacts.glob('*.json')))
        self.assertEqual(7, len(list(self.artifacts.glob('*.intent'))))

    def test_unknown_and_new_nonce_do_not_replay_original_operation(self):
        with mock.patch('assistant_mesh.campus_adapter._bounded_capture', side_effect=TimeoutError('PRIVATE')) as capture:
            with self.assertRaisesRegex(CampusAdapterError, 'request_outcome_unknown'):
                execute(self.context, self.owner, self.sha)
            self.context['invocation_nonce'] = 'b' * 32
            with self.assertRaisesRegex(CampusAdapterError, 'original_invocation_no_replay'):
                execute(self.context, self.owner, self.sha)
            self.assertEqual(1, capture.call_count)

    def test_expired_owner_and_excess_workload_fail_before_intent(self):
        self.config['expires_at'] = time.time() - 1
        self.save()
        with self.assertRaisesRegex(CampusAdapterError, 'owner_contract_expired'):
            self.invoke()
        self.config['expires_at'] = time.time() + 120
        self.save()
        for key, value in (('max_bytes', 65537), ('timeout_seconds', 21), ('kind', 'shell'), ('max_bytes', True)):
            context = copy.deepcopy(self.context)
            context['workload'][key] = value
            with self.assertRaisesRegex(CampusAdapterError, 'workload_outside_owner_limits'):
                self.invoke(context)
        self.assertEqual([], list(self.artifacts.iterdir()))

    def test_provider_capability_action_and_task_identity_validation(self):
        for key, value in (('provider', 'node:other'), ('capability_id', 'other'), ('capability_epoch', True),
                           ('action', 'shell'), ('task_epoch', True), ('invocation_nonce', 'not-a-nonce')):
            with self.assertRaises(CampusAdapterError):
                self.invoke(dict(self.context, **{key: value}))

    def test_owner_config_integrity_private_mode_and_network_range_validation(self):
        self.owner.chmod(0o644)
        with self.assertRaisesRegex(CampusAdapterError, 'private_config'):
            self.invoke()
        self.owner.chmod(0o600)
        with self.assertRaisesRegex(CampusAdapterError, 'private_config'):
            execute(self.context, self.owner, '0' * 64)
        for key, value in (('allowed_networks', ['0.0.0.0/0']), ('allowed_networks', ['10.42.1.5/16']),
                           ('allowed_ports', [[80, True]]), ('allowed_schemes', ['ftp'])):
            original = self.config[key]
            self.config[key] = value
            self.save()
            with self.assertRaises(CampusAdapterError):
                self.invoke()
            self.config[key] = original
        self.save()

    def test_artifact_exact_sha_identity_and_body_hash_verified_without_network(self):
        result, _ = self.invoke()
        evidence = result['evidence']
        with self.assertRaisesRegex(CampusAdapterError, 'artifact_identity_invalid'):
            read_artifact(self.owner, self.sha, '../private', evidence['artifact_sha256'])
        with self.assertRaisesRegex(CampusAdapterError, 'artifact_hash_mismatch'):
            read_artifact(self.owner, self.sha, evidence['artifact_id'], '0' * 64)
        path = self.artifacts / evidence['artifact_id']
        value = json.loads(path.read_text())
        value['response_base64'] = base64.b64encode(b'changed').decode('ascii')
        self.write(path, json.dumps(value))
        with self.assertRaisesRegex(CampusAdapterError, 'body_hash_mismatch'):
            read_artifact(self.owner, self.sha, evidence['artifact_id'], hashlib.sha256(path.read_bytes()).hexdigest())

    def runtime(self):
        api = API({'database': str(self.root / 'authority.db'), 'peers': [],
                   'resources': {'trusted_verifiers': ['checker']}})
        api.store.clock = lambda: 1000.0
        api.store.heartbeat('campus-node', ['leader', 'agent'])
        api.store.create_task('campus bounded fixture', task_id='parent')
        task = api.store.claim('campus-node')
        wrapper = self.root / 'wrapper.py'
        self.write(wrapper, 'from assistant_mesh.campus_adapter import execute\n'
            'def invoke(context):\n    return execute(context, %r, %r)\n' % (str(self.owner), self.sha))
        manifest = {'schema': 1, 'provider': 'node:campus-node', 'authority_id': 'fixture-authority',
            'journal': str(self.root / 'provider.db'), 'adapters': [{
                'capability_id': 'campus-selective', 'handle': 'owner.campus.v1', 'version': '1',
                'module': str(wrapper), 'sha256': hashlib.sha256(wrapper.read_bytes()).hexdigest(),
                'function': 'invoke', 'capability_epoch': 1, 'action': 'network.campus.http', 'new_spend_minor': 0}]}
        manifest_path = self.root / 'runtime.json'
        self.write(manifest_path, json.dumps(manifest))
        runtime = ProviderRuntime(BoundClient(api), manifest_path)
        api.resources.advertise('node:campus-node', 'campus-selective', 'network.egress.campus',
                                {'managed_adapter': runtime.describe_bindings()[0]['managed_adapter']})
        api.allocations.define_pool('operator', 'campus-pool', {'requests': {'capacity': 1, 'unit': 'request'}})
        api.allocations.bind_pool('operator', 'campus-selective', 1, 'campus-pool', 1, 'requests', 1, 'request')
        api.resources.observe('checker', 'campus-selective', 'reachability', 1, 'bool', 'scope fixture',
            epoch=1, verification='verified', observation_id='probe',
            evidence={'scope': self.context['scope'], 'workload': self.context['workload']})
        selection = {'capability_id': 'campus-selective', 'epoch': 1, 'action': 'network.campus.http',
            'scope': self.context['scope'], 'workload': self.context['workload'], 'observations': [{
                'observation_id': 'probe', 'metric': 'reachability', 'unit': 'bool', 'max_age_seconds': 60}]}
        api.allocations.reserve('node:campus-node', 'operation', 'parent', task['epoch'],
                                {'target': 'campus-selective', 'route': [], 'selections': [selection]}, 40)
        return api, runtime, manifest_path

    def test_existing_runtime_admits_actual_scope_settles_once_and_releases_capacity(self):
        api, runtime, manifest = self.runtime()
        with mock.patch('assistant_mesh.campus_adapter._bounded_capture', return_value=(
            0, self.body + b'\nMESH_CAMPUS_METADATA:200 10.42.1.5 0')) as capture:
            report = runtime.poll_once()
            self.assertEqual('settled', report['dispatch'][0]['state'])
            self.assertEqual([], runtime.poll_once()['dispatch'])
            self.assertEqual([], ProviderRuntime(BoundClient(api), manifest).poll_once()['dispatch'])
            self.assertEqual(1, capture.call_count)
        allocation = api.allocations.inspect('node:campus-node', 'operation')
        self.assertEqual('completed', allocation['state'])
        self.assertEqual(0, api.allocations.pools('operator')['pools'][0]['dimensions']['requests']['held'])
        evidence = allocation['dispatch'][0]['settlement']['evidence']
        self.assertEqual(self.body, base64.b64decode(read_artifact(self.owner, self.sha,
            evidence['artifact_id'], evidence['artifact_sha256'])['response_base64']))

    def test_runtime_unknown_restart_and_forged_pending_never_reinvoke(self):
        api, runtime, manifest = self.runtime()
        pending = api.allocations.pending('node:campus-node')['dispatch'][0]
        with mock.patch('assistant_mesh.campus_adapter._bounded_capture', side_effect=TimeoutError('PRIVATE')) as capture:
            self.assertEqual('unknown', runtime.poll_once()['dispatch'][0]['state'])
            restarted = ProviderRuntime(BoundClient(api), manifest)
            restarted.poll_once()
            restarted.bridge.run(pending)
            self.assertEqual(1, capture.call_count)
        self.assertEqual(1, api.allocations.pools('operator')['pools'][0]['dimensions']['requests']['held'])

    def test_forged_pending_scope_fails_existing_authority_before_callback(self):
        api, runtime, _ = self.runtime()
        pending = api.allocations.pending('node:campus-node')['dispatch'][0]
        pending['selection']['scope']['url'] = 'http://10.42.1.99/unadmitted'
        with mock.patch('assistant_mesh.campus_adapter._bounded_capture') as capture:
            self.assertEqual('unknown', runtime.bridge.run(pending)['state'])
            capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
