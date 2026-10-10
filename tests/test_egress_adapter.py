"""Generic adapter/real local allocation pipeline fixtures, not public egress proof."""
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

from assistant_mesh.egress_adapter import EgressAdapterError, execute, read_artifact
from assistant_mesh.provider_runtime import ProviderRuntime
from assistant_mesh.server import API


class FixtureTunnel:
    opened = 0
    def __init__(self, config):
        self.config = config
        self.process = self
        self.returncode = None

    def __enter__(self):
        type(self).opened += 1
        return self

    def __exit__(self, *args):
        self.returncode = 0

    def poll(self):
        return self.returncode

    def status(self):
        return {'owned_process_running': True}

    def _remaining(self):
        return 40

    @property
    def proxy_url(self):
        return 'socks5h://127.0.0.1:32123'


class BoundClient:
    def __init__(self, api):
        self.api = api

    def request(self, path, body=None):
        return self.api.dispatch('GET' if body is None else 'POST', path, body,
                                 {'role': 'worker', 'node': 'cloud'})


class EgressAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-https-adapter.')
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.ssh = self.root / 'ssh-config'
        self.write(self.ssh, 'Host owner-vps\n  HostName example.invalid\n')
        self.transport = self.root / 'transport.json'
        self.write(self.transport, json.dumps({'ssh_destination': 'owner-vps', 'ssh_config': str(self.ssh)}))
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir(mode=0o700)
        self.owner = self.root / 'https-owner.json'
        self.config = {'schema': 1, 'provider': 'node:cloud', 'capability_id': 'generic-https',
                       'capability_epoch': 1, 'action': 'network.https',
                       'transport_mode': 'ssh_socks5h', 'egress_node': 'ali-vps',
                       'transport_config': str(self.transport),
                       'transport_config_sha256': hashlib.sha256(self.transport.read_bytes()).hexdigest(),
                       'artifact_root': str(self.artifacts), 'allowed_origins': ['*'],
                       'expires_at': time.time() + 120, 'max_bytes': 65536, 'timeout_seconds': 20}
        self.save_config()
        self.context = {'provider': 'node:cloud', 'capability_id': 'generic-https', 'capability_epoch': 1,
                        'action': 'network.https', 'operation_id': 'owner-new-read', 'task_id': 'parent',
                        'task_epoch': 1, 'receipt_id': 'provider-exact-receipt', 'adapter_handle': 'installed-v1',
                        'invocation_nonce': 'a' * 32,
                        'scope': {'operation': 'get', 'url': 'https://docs.example.org/path'},
                        'workload': {'kind': 'https', 'max_bytes': 4096, 'timeout_seconds': 5}}
        FixtureTunnel.opened = 0
        self.body = b'arbitrary public HTTPS bytes, not a fixed README\n\x00\xff'

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, content):
        path.write_text(content, encoding='utf8')
        path.chmod(0o600)

    def save_config(self):
        self.write(self.owner, json.dumps(self.config))
        self.owner_sha = hashlib.sha256(self.owner.read_bytes()).hexdigest()

    def invoke(self, context=None, status=200):
        captured = self.body + ('\nMESH_EGRESS_METADATA:%d 0' % status).encode('ascii')
        with mock.patch('assistant_mesh.egress_adapter.SshSocksEgress', FixtureTunnel), \
                mock.patch('assistant_mesh.egress_adapter._bounded_capture', return_value=(0, captured)) as capture:
            result = execute(context or self.context, str(self.owner), self.owner_sha)
        return result, capture

    def test_generic_get_has_independently_readable_content_and_hash(self):
        result, capture = self.invoke()
        self.assertEqual('completed', result['outcome'])
        self.assertTrue(result['resource_quiescent'])
        evidence = result['evidence']
        self.assertEqual(hashlib.sha256(self.body).hexdigest(), evidence['response_sha256'])
        value = read_artifact(self.owner, self.owner_sha, evidence['artifact_id'], evidence['artifact_sha256'])
        self.assertEqual(self.body, base64.b64decode(value['response_base64']))
        self.assertEqual(self.context['operation_id'], value['operation_id'])
        self.assertEqual(self.context['invocation_nonce'], value['invocation_nonce'])
        self.assertEqual('body', value['observation']['response_kind'])
        self.assertNotIn('arbitrary public HTTPS bytes', json.dumps(result))
        self.assertNotIn(str(self.ssh), json.dumps(result))
        self.assertFalse(evidence['authentication_verified'])
        self.assertFalse(evidence['model_inference_verified'])
        path = self.artifacts / evidence['artifact_id']
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual('mesh-https-artifact:sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(),
                         result['result_reference'])
        self.assertIn('--noproxy', capture.call_args[0][0])
        self.assertNotIn('--location', capture.call_args[0][0])

    def test_nonsecret_query_parameters_are_encoded_not_executable_or_secret_url_metadata(self):
        self.context['scope']['query'] = {'search': 'mesh & agent', 'page': '2'}
        result, capture = self.invoke()
        self.assertEqual('https://docs.example.org/path?page=2&search=mesh+%26+agent', capture.call_args[0][0][-1])
        self.assertEqual('https://docs.example.org', result['evidence']['target_origin'])
        self.assertNotIn('search=', json.dumps(result))

    def test_probe_is_head_and_401_403_do_not_imply_authentication(self):
        self.context['scope']['operation'] = 'probe'
        for status in (401, 403):
            self.context['operation_id'] = 'probe-' + str(status)
            result, capture = self.invoke(status=status)
            self.assertIn('--head', capture.call_args[0][0])
            self.assertTrue(result['evidence']['tls_http_reachable'])
            self.assertFalse(result['evidence']['http_success'])
            self.assertFalse(result['evidence']['authentication_verified'])
            self.assertFalse(result['evidence']['model_inference_verified'])
            self.assertEqual('headers', result['evidence']['response_kind'])

    def test_private_config_and_transport_are_exact_sha_pinned(self):
        self.write(self.owner, json.dumps(dict(self.config, max_bytes=12345)))
        with self.assertRaisesRegex(EgressAdapterError, 'egress_private_config_changed'):
            self.invoke()
        self.save_config()
        self.write(self.transport, json.dumps({'ssh_destination': 'different-host', 'ssh_config': str(self.ssh)}))
        with self.assertRaisesRegex(EgressAdapterError, 'egress_private_config_changed'):
            self.invoke()
        self.assertEqual(0, FixtureTunnel.opened)

    def test_config_permissions_and_unsafe_artifact_directory_rejected(self):
        self.owner.chmod(0o644)
        with self.assertRaisesRegex(EgressAdapterError, 'egress_private_config_unavailable'):
            self.invoke()
        self.owner.chmod(0o600)
        self.artifacts.chmod(0o755)
        with self.assertRaisesRegex(EgressAdapterError, 'egress_artifact_root_invalid'):
            self.invoke()

    def test_scope_never_supplies_config_credentials_body_or_native_commands(self):
        for field in ('ssh_config', 'transport_config', 'transport_mode', 'egress_node',
                      'artifact_root', 'headers', 'api_key', 'command', 'body'):
            value = copy.deepcopy(self.context)
            value['scope'][field] = '/PRIVATE-NOT-OUTPUT'
            with self.subTest(field=field), self.assertRaisesRegex(EgressAdapterError, 'egress_scope_invalid'):
                self.invoke(value)
        self.assertEqual(0, FixtureTunnel.opened)

    def test_scope_url_has_no_embedded_query_userinfo_fragment_or_plain_http(self):
        for url in ('https://user:secret@example.org/', 'https://example.org/?api_key=secret',
                    'https://example.org/#fragment', 'http://example.org/', 'https://example.org:8443/',
                    'https://localhost/', 'https://127.0.0.1/', 'https://10.0.0.1/',
                    'https://example.org/\rsecret', 'https://example.org\\bad/'):
            value = copy.deepcopy(self.context)
            value['scope']['url'] = url
            with self.subTest(url=url), self.assertRaises(EgressAdapterError):
                self.invoke(value)
        self.assertEqual(0, FixtureTunnel.opened)

    def test_owner_origin_scope_is_exact_not_suffix_matching(self):
        self.config['allowed_origins'] = ['https://docs.example.org']
        self.save_config()
        value = copy.deepcopy(self.context)
        value['scope']['url'] = 'https://docs.example.org.evil.test/path'
        with self.assertRaisesRegex(EgressAdapterError, 'egress_target_outside_owner_scope'):
            self.invoke(value)
        self.assertEqual(0, FixtureTunnel.opened)
        self.invoke()

    def test_query_secret_fields_and_nonstring_values_rejected(self):
        for query in ({'api_key': 'secret'}, {'token': 'secret'}, {'q': ['unbounded', 'list']},
                      {'q': 'https://user:secret@example.org/'}, {'q': 'bad\x00value'}):
            value = copy.deepcopy(self.context)
            value['scope']['query'] = query
            with self.subTest(query=query), self.assertRaises(EgressAdapterError):
                self.invoke(value)
        self.assertEqual(0, FixtureTunnel.opened)

    def test_identity_admission_nonce_and_workload_limits_required(self):
        for key, value in (('capability_id', 'different'), ('provider', 'node:other'),
                           ('action', 'shell.exec'), ('capability_epoch', True),
                           ('invocation_nonce', 'new-model-made-up'), ('task_epoch', False)):
            context = copy.deepcopy(self.context)
            context[key] = value
            with self.subTest(key=key), self.assertRaises(EgressAdapterError):
                self.invoke(context)
        for key, value in (('max_bytes', 100000), ('timeout_seconds', 21),
                           ('timeout_seconds', float('nan')), ('kind', 'inference')):
            context = copy.deepcopy(self.context)
            context['workload'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(EgressAdapterError, 'egress_workload_outside_owner_limits'):
                self.invoke(context)
        self.assertEqual(0, FixtureTunnel.opened)

    def test_expired_contract_cannot_begin_effect_but_old_artifact_is_readable(self):
        result, _ = self.invoke()
        self.config['expires_at'] = time.time() - 1
        self.save_config()
        with self.assertRaisesRegex(EgressAdapterError, 'egress_owner_contract_expired'):
            self.invoke()
        value = read_artifact(self.owner, self.owner_sha, result['evidence']['artifact_id'],
                              result['evidence']['artifact_sha256'])
        self.assertEqual(self.context['operation_id'], value['operation_id'])

    def test_unknown_response_has_no_result_and_same_invocation_cannot_replay(self):
        with mock.patch('assistant_mesh.egress_adapter.SshSocksEgress', FixtureTunnel), \
                mock.patch('assistant_mesh.egress_adapter._bounded_capture', side_effect=TimeoutError('PRIVATE-NOT-OUTPUT')):
            with self.assertRaisesRegex(EgressAdapterError, '^egress_request_outcome_unknown$'):
                execute(self.context, self.owner, self.owner_sha)
        self.assertEqual([], list(self.artifacts.glob('*.json')))
        self.context['invocation_nonce'] = 'b' * 32
        with mock.patch('assistant_mesh.egress_adapter.SshSocksEgress', FixtureTunnel):
            with self.assertRaisesRegex(EgressAdapterError, 'egress_original_invocation_no_replay'):
                execute(self.context, self.owner, self.owner_sha)
        self.assertEqual(1, FixtureTunnel.opened)

    def direct_config(self):
        self.config['transport_mode'] = 'direct'
        del self.config['transport_config']
        del self.config['transport_config_sha256']
        self.save_config()

    def test_owner_fixed_direct_mode_bypasses_every_inherited_proxy_without_global_mutation(self):
        self.direct_config()
        inherited = {'ALL_PROXY': 'PRIVATE-PROXY', 'HTTPS_PROXY': 'PRIVATE-HTTPS',
                     'HTTP_PROXY': 'PRIVATE-HTTP', 'all_proxy': 'PRIVATE-lower', 'NO_PROXY': 'nothing'}
        with mock.patch.dict(os.environ, inherited):
            original = dict(os.environ)
            result, capture = self.invoke()
            self.assertEqual(original, dict(os.environ))
        command = capture.call_args[0][0]
        self.assertEqual('', command[command.index('--proxy') + 1])
        self.assertEqual('*', command[command.index('--noproxy') + 1])
        self.assertEqual('direct', result['evidence']['transport'])
        self.assertEqual('ali-vps', result['evidence']['egress_node'])
        self.assertEqual('owner_contract', result['evidence']['egress_node_verification'])
        self.assertFalse(result['evidence']['owned_ssh_reaped'])
        self.assertTrue(result['evidence']['owned_curl_reaped'])
        self.assertEqual(0, FixtureTunnel.opened)
        self.assertNotIn('PRIVATE-PROXY', json.dumps(result))

    def test_direct_mode_has_no_fictional_connection_proof_on_zero_http_status(self):
        self.direct_config()
        with mock.patch('assistant_mesh.egress_adapter._bounded_capture', return_value=(0, b'\nMESH_EGRESS_METADATA:000 0')):
            with self.assertRaisesRegex(EgressAdapterError, 'egress_request_outcome_unknown'):
                execute(self.context, self.owner, self.owner_sha)
        self.assertEqual([], list(self.artifacts.glob('*.json')))

    def test_direct_install_rejects_unused_model_or_ssh_credential_configuration(self):
        self.config['transport_mode'] = 'direct'
        self.save_config()
        with self.assertRaisesRegex(EgressAdapterError, 'egress_private_config_invalid'):
            self.invoke()

    def test_invalid_tls_nonzero_exit_malformed_reply_and_excess_body_stay_unknown(self):
        for code, reply in ((60, b'body\nMESH_EGRESS_METADATA:200 20'),
                            (28, b'partial\nMESH_EGRESS_METADATA:200 0'),
                            (0, b'no metadata'), (0, b'body\nMESH_EGRESS_METADATA:000 0'),
                            (0, b'x' * 4097 + b'\nMESH_EGRESS_METADATA:200 0')):
            self.context['operation_id'] = 'invalid-' + str(FixtureTunnel.opened)
            with mock.patch('assistant_mesh.egress_adapter.SshSocksEgress', FixtureTunnel), \
                    mock.patch('assistant_mesh.egress_adapter._bounded_capture', return_value=(code, reply)):
                with self.assertRaisesRegex(EgressAdapterError, 'egress_request_outcome_unknown'):
                    execute(self.context, self.owner, self.owner_sha)

    def test_artifact_reader_requires_exact_id_sha_and_preserves_contents(self):
        result, _ = self.invoke()
        evidence = result['evidence']
        with self.assertRaisesRegex(EgressAdapterError, 'egress_artifact_identity_invalid'):
            read_artifact(self.owner, self.owner_sha, '../private-file', evidence['artifact_sha256'])
        with self.assertRaisesRegex(EgressAdapterError, 'egress_artifact_hash_mismatch'):
            read_artifact(self.owner, self.owner_sha, evidence['artifact_id'], '0' * 64)
        path = self.artifacts / evidence['artifact_id']
        original = path.read_bytes()
        read_artifact(self.owner, self.owner_sha, evidence['artifact_id'], evidence['artifact_sha256'])
        self.assertEqual(original, path.read_bytes())

    def runtime(self):
        api = API({'database': str(self.root / 'authority.db'), 'peers': [],
                   'resources': {'trusted_verifiers': ['node:checker']}})
        api.store.clock = lambda: 1000.0
        api.store.heartbeat('cloud', ['leader', 'agent'])
        api.store.create_task('generic actual-scope fixture', task_id='parent')
        task = api.store.claim('cloud')
        wrapper = self.root / 'wrapper.py'
        self.write(wrapper, 'from assistant_mesh.egress_adapter import execute\n'
                   'def invoke(context):\n'
                   '    return execute(context, %r, %r)\n' % (str(self.owner), self.owner_sha))
        manifest = {'schema': 1, 'provider': 'node:cloud', 'authority_id': 'fixture-authority',
                    'journal': str(self.root / 'provider.db'),
                    'adapters': [{'capability_id': 'generic-https', 'handle': 'owner.generic.https',
                                  'version': '1', 'module': str(wrapper),
                                  'sha256': hashlib.sha256(wrapper.read_bytes()).hexdigest(),
                                  'function': 'invoke', 'capability_epoch': 1,
                                  'action': 'network.https', 'new_spend_minor': 0}]}
        manifest_path = self.root / 'runtime.json'
        self.write(manifest_path, json.dumps(manifest))
        runtime = ProviderRuntime(BoundClient(api), manifest_path)
        api.resources.advertise('node:cloud', 'generic-https', 'network.egress.https',
                                {'managed_adapter': runtime.describe_bindings()[0]['managed_adapter']})
        api.allocations.define_pool('operator', 'vps-shared', {'requests': {'capacity': 1, 'unit': 'request'}})
        api.allocations.bind_pool('operator', 'generic-https', 1, 'vps-shared', 1, 'requests', 1, 'request')
        api.resources.observe('node:checker', 'generic-https', 'reachability', 1, 'bool',
                              'independent scope fixture', epoch=1, verification='verified', observation_id='probe',
                              evidence={'scope': self.context['scope'], 'workload': self.context['workload']})
        selection = {'capability_id': 'generic-https', 'epoch': 1, 'action': 'network.https',
                     'scope': self.context['scope'], 'workload': self.context['workload'],
                     'observations': [{'observation_id': 'probe', 'metric': 'reachability',
                                       'unit': 'bool', 'max_age_seconds': 60}]}
        api.allocations.reserve('node:cloud', 'operation', 'parent', task['epoch'],
                                 {'target': 'generic-https', 'route': [], 'selections': [selection]}, 40)
        return api, runtime, manifest_path

    def test_existing_provider_runtime_executes_model_scope_and_settles_actual_artifact_once(self):
        api, runtime, manifest_path = self.runtime()
        with mock.patch('assistant_mesh.egress_adapter.SshSocksEgress', FixtureTunnel), \
                mock.patch('assistant_mesh.egress_adapter._bounded_capture', return_value=(
                    0, self.body + b'\nMESH_EGRESS_METADATA:200 0')) as capture:
            report = runtime.poll_once()
            self.assertEqual('settled', report['dispatch'][0]['state'])
            self.assertEqual('ok', report['status'])
            self.assertEqual('https://docs.example.org/path', capture.call_args[0][0][-1])
            self.assertEqual([], runtime.poll_once()['dispatch'])
            restarted = ProviderRuntime(BoundClient(api), manifest_path)
            self.assertEqual([], restarted.poll_once()['dispatch'])
            self.assertEqual(1, capture.call_count)
        allocation = api.allocations.inspect('node:cloud', 'operation')
        self.assertEqual('completed', allocation['dispatch'][0]['state'])
        self.assertEqual(0, api.allocations.pools('operator')['pools'][0]['dimensions']['requests']['held'])
        evidence = allocation['dispatch'][0]['settlement']['evidence']
        self.assertEqual(self.body, base64.b64decode(read_artifact(
            self.owner, self.owner_sha, evidence['artifact_id'], evidence['artifact_sha256'])['response_base64']))

    def test_existing_runtime_retains_unknown_without_reinvocation_after_restart(self):
        api, runtime, manifest_path = self.runtime()
        pending = api.allocations.pending('node:cloud')['dispatch'][0]
        with mock.patch('assistant_mesh.egress_adapter.SshSocksEgress', FixtureTunnel), \
                mock.patch('assistant_mesh.egress_adapter._bounded_capture', side_effect=TimeoutError('PRIVATE-NOT-OUTPUT')) as capture:
            report = runtime.poll_once()
            self.assertEqual('unknown', report['dispatch'][0]['state'])
            self.assertNotIn('PRIVATE-NOT-OUTPUT', json.dumps(report))
            runtime.bridge.run(pending)
            restarted = ProviderRuntime(BoundClient(api), manifest_path)
            restarted.poll_once()
            restarted.bridge.run(pending)
            self.assertEqual(1, capture.call_count)
        self.assertEqual(1, api.allocations.pools('operator')['pools'][0]['dimensions']['requests']['held'])

    def test_forged_pending_destination_fails_authority_comparison_before_adapter(self):
        api, runtime, _ = self.runtime()
        pending = api.allocations.pending('node:cloud')['dispatch'][0]
        pending['selection']['scope']['url'] = 'https://unapproved.example.net/path'
        with mock.patch('assistant_mesh.egress_adapter.SshSocksEgress', FixtureTunnel), \
                mock.patch('assistant_mesh.egress_adapter._bounded_capture') as capture:
            self.assertEqual('unknown', runtime.bridge.run(pending)['state'])
            capture.assert_not_called()
        self.assertEqual(0, FixtureTunnel.opened)


if __name__ == '__main__':
    unittest.main()
