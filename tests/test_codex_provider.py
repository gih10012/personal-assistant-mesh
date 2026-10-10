"""Provider configuration/continuity tests; no model or real credentials used."""
import json
import os
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from assistant_mesh.codex import Codex, CodexError
from assistant_mesh.codex_provider import custom_provider, provider_overrides
from assistant_mesh.model_provider_policy import validate_provider_cost


class CodexProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.config = {'auth_home': str(self.root), 'workspace': str(self.root),
                       'model_provider': 'owner-relay', 'model': 'test-model',
                       'cost_policy': 'local',
                       'provider_file': str(self.root / 'provider.json')}
        self.provider = {'name': 'Owner test relay', 'base_url': 'http://127.0.0.1:9999/v1',
                         'wire_api': 'responses', 'env_key': 'MESH_TEST_KEY',
                         'env_http_headers': {'X-Owner': 'MESH_TEST_HEADER'}}
        self.env = {'MESH_TEST_KEY': 'synthetic-no-credential', 'MESH_TEST_HEADER': 'synthetic-header'}
        self.write(self.config['provider_file'], self.provider)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = Path(name)
        path.write_text(json.dumps(value), encoding='utf8')
        path.chmod(0o600)

    def resolve(self):
        return custom_provider(self.config, self.env)

    def launch(self):
        child = MagicMock()
        child.stdout = []
        with patch.dict(os.environ, self.env), patch('assistant_mesh.codex.subprocess.Popen', return_value=child) as launch, \
                patch.object(Codex, 'rpc', return_value={}), patch.object(Codex, 'send'), \
                patch('assistant_mesh.codex.discover_codex_auth') as discovery:
            agent = Codex(self.config)
        return agent, launch.call_args, discovery

    def test_custom_provider_uses_explicit_child_home_and_never_discovers_chatgpt(self):
        before = dict(os.environ)
        agent, call, discovery = self.launch()
        discovery.assert_not_called()
        command = call[0][0]
        self.assertIn('model_provider="owner-relay"', command)
        self.assertIn('model_providers.owner-relay.wire_api="responses"', command)
        self.assertIn('model_providers.owner-relay.env_http_headers={"X-Owner" = "MESH_TEST_HEADER"}', command)
        self.assertEqual(str(self.root), call[1]['env']['CODEX_HOME'])
        for value in self.env.values():
            self.assertNotIn(value, ' '.join(command))
        self.assertEqual(before, dict(os.environ))
        self.assertEqual('configured_provider', agent.account()['type'])
        self.assertFalse(agent.account()['live_auth_verified'])
        self.assertEqual({'status': 'not_applicable'}, agent.rate_limits())

    def test_secrets_are_only_loaded_into_selected_child_environment(self):
        self.config['provider_env_file'] = str(self.root / 'keys.json')
        self.write(self.config['provider_env_file'], self.env)
        with patch.dict(os.environ, {}, clear=True):
            agent, call, _ = self.launch()
        self.assertEqual(self.env['MESH_TEST_KEY'], call[1]['env']['MESH_TEST_KEY'])
        self.assertNotIn(self.env['MESH_TEST_KEY'], str(provider_overrides(agent.provider)))

    def test_default_keeps_existing_subscription_auth_path(self):
        self.assertIsNone(custom_provider({}, {}))
        for name in ('provider_file', 'provider_env_file'):
            with self.assertRaisesRegex(ValueError, '^codex_builtin_provider_override_forbidden$'):
                custom_provider({name: '/not-read'}, {})

    def test_custom_openai_auth_cannot_discover_an_unrelated_profile(self):
        self.config['cost_policy'] = 'existing_subscription'
        self.write(self.config['provider_file'], {
            'name': 'Owner auth gateway', 'base_url': 'https://gateway.example/v1',
            'requires_openai_auth': True})
        with patch('assistant_mesh.codex.discover_codex_auth',
                   side_effect=ValueError('codex_auth_missing')) as discovery, \
                patch('assistant_mesh.codex.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'codex_auth_missing'):
                Codex(self.config)
            discovery.assert_called_once_with(str(self.root), strict=True)
            launch.assert_not_called()

    def test_unrelated_openai_credentials_are_not_inherited_by_custom_child(self):
        with patch.dict(os.environ, OPENAI_API_KEY='unrelated-host-key', CODEX_API_KEY='other-host-key'):
            _, call, _ = self.launch()
        self.assertNotIn('OPENAI_API_KEY', call[1]['env'])
        self.assertNotIn('CODEX_API_KEY', call[1]['env'])

    def test_null_optional_key_is_omitted_from_native_toml(self):
        self.write(self.config['provider_file'], dict(self.provider, env_key=None))
        selected = self.resolve()
        self.assertNotIn('env_key', selected['settings'])
        self.assertNotIn('null', ' '.join(provider_overrides(selected)))

    def test_rejects_protocol_alias_inline_secret_and_invalid_provider_before_spawn(self):
        for update in ({'wire_api': 'chat'}, {'wire_api': 'messages'},
                       {'experimental_bearer_token': 'never-log'},
                       {'http_headers': {'Authorization': 'Bearer never-log'}},
                       {'requires_openai_auth': 'false'},
                       {'env_key': 'HOME'}, {'base_url': 'https://user:never-log@example.com/v1'},
                       {'base_url': 'http://remote.example/v1'},
                       {'base_url': 'https://[invalid/v1'},
                       {'base_url': 'https://example.com:invalid/v1'},
                       {'base_url': 'http://127.0.0.1/\ninvalid'}):
            with self.subTest(update=update):
                self.write(self.config['provider_file'], dict(self.provider, **update))
                with patch('assistant_mesh.codex.subprocess.Popen') as launch:
                    with self.assertRaises(ValueError) as error:
                        Codex(self.config)
                    self.assertNotIn('never-log', str(error.exception))
                    launch.assert_not_called()

    def test_requires_private_config_explicit_model_and_profile(self):
        with patch.dict(os.environ, self.env), patch('assistant_mesh.codex.subprocess.Popen') as launch:
            self.config['model'] = None
            with self.assertRaisesRegex(ValueError, 'codex_custom_provider_model_required'):
                Codex(self.config)
            self.config['model'] = 'test-model'
            Path(self.config['provider_file']).chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'configuration_invalid'):
                Codex(self.config)
            Path(self.config['provider_file']).chmod(0o600)
            self.config.pop('auth_home')
            with self.assertRaisesRegex(ValueError, 'profile_required'):
                Codex(self.config)
            launch.assert_not_called()

    def test_unreferenced_env_variable_cannot_change_native_environment(self):
        self.config['provider_env_file'] = str(self.root / 'keys.json')
        self.write(self.config['provider_env_file'], dict(self.env, PATH='/override'))
        with self.assertRaisesRegex(ValueError, 'environment_invalid'):
            self.resolve()

    def test_local_policy_cannot_admit_public_api(self):
        self.write(self.config['provider_file'], dict(self.provider, base_url='https://models.example/v1'))
        with self.assertRaisesRegex(ValueError, 'local_endpoint_required'):
            self.resolve()

    def test_free_contract_is_private_expiring_and_exact_model_scoped(self):
        self.config['cost_policy'] = 'free_api'
        self.config['cost_contract'] = str(self.root / 'cost.json')
        contract = {'provider': 'owner-relay', 'models': ['test-model'],
                    'expires_at': time.time() + 600, 'zero_cost': True,
                    'paid_fallback': False, 'auto_reload': False}
        self.write(self.config['cost_contract'], contract)
        self.assertFalse(validate_provider_cost(self.config, 'owner-relay', 'test-model')['live_cost_verified'])
        for update in ({'expires_at': 0}, {'expires_at': True}, {'expires_at': float('inf')},
                       {'expires_at': 10 ** 1000},
                       {'zero_cost': 1}, {'paid_fallback': True}, {'auto_reload': True},
                       {'provider': 'other'}, {'models': ['other-model']}):
            with self.subTest(update=update):
                self.write(self.config['cost_contract'], dict(contract, **update))
                with self.assertRaisesRegex(ValueError, 'free_contract_invalid'):
                    self.resolve()
        self.write(self.config['cost_contract'], contract)
        Path(self.config['cost_contract']).chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'free_contract_invalid'):
            self.resolve()

    def test_custom_api_not_labeled_subscription_or_unapproved_paid(self):
        for policy in ('existing_subscription', 'paid_api', None):
            with self.subTest(policy=policy):
                self.config['cost_policy'] = policy
                with self.assertRaisesRegex(ValueError, 'cost_authorization_required'):
                    self.resolve()

    def test_cost_is_rechecked_at_native_inference_send_boundary(self):
        provider = self.resolve()
        self.config['cost_policy'] = 'free_api'
        self.config['cost_contract'] = str(self.root / 'cost.json')
        self.write(self.config['cost_contract'], {
            'provider': 'owner-relay', 'models': ['test-model'],
            'expires_at': 0, 'zero_cost': True, 'paid_fallback': False,
            'auto_reload': False})
        agent = Codex.__new__(Codex)
        agent.provider, agent.config = provider, self.config
        agent.send = MagicMock()
        for method in ('thread/start', 'thread/resume', 'turn/start', 'turn/steer', 'thread/goal/set'):
            with self.subTest(method=method):
                with self.assertRaisesRegex(ValueError, 'free_contract_invalid'):
                    agent.rpc(method, {})
                agent.send.assert_not_called()

    def fixture(self, provider=None):
        agent = Codex.__new__(Codex)
        agent.config = self.config if provider else {'workspace': str(self.root)}
        agent.provider = provider
        agent.tools, agent.on_activity = [], None
        agent.deferred, agent.events = [], queue.Queue()
        agent.calls = []
        def rpc(method, params, **kwargs):
            agent.calls.append((method, params))
            if method in ('thread/start', 'thread/resume'):
                return {'thread': {'id': 'same-thread', 'model': 'test-model'}}
            if method == 'turn/start':
                return {'turn': {'id': 'same-turn'}}
            return {'goal': None}
        agent.rpc = rpc
        return agent

    def test_resume_requires_same_provider_even_if_native_thread_id_matches(self):
        provider = self.resolve()
        for checkpoint in ({'thread_id': 'same-thread'},
                           {'thread_id': 'same-thread', 'codex_provider_identity': 'openai'},
                           {'thread_id': 'same-thread', 'codex_provider_identity': 'another'}):
            agent = self.fixture(provider)
            with self.assertRaisesRegex(CodexError, 'provider_resume_identity_mismatch'):
                agent.start('do not transmit old memory', checkpoint)
            self.assertEqual([], agent.calls)
        agent = self.fixture(provider)
        agent.start('continue', {'thread_id': 'same-thread', 'codex_provider_identity': provider['identity']})
        self.assertEqual('thread/resume', agent.calls[0][0])

    def test_default_cannot_silently_resume_third_party_native_context(self):
        agent = self.fixture()
        with self.assertRaisesRegex(CodexError, 'provider_resume_identity_mismatch'):
            agent.start('continue', {'thread_id': 'same-thread', 'codex_provider_identity': self.resolve()['identity']})
        self.assertEqual([], agent.calls)

    def test_legacy_openai_resume_remains_compatible_and_new_binding_persisted(self):
        agent = self.fixture()
        activities = []
        agent.on_activity = lambda name, value: activities.append((name, value))
        agent.start('continue', {'thread_id': 'same-thread'})
        ready = next(value for name, value in activities if name == 'session_ready')
        self.assertEqual('openai', ready['codex_provider_identity'])


if __name__ == '__main__':
    unittest.main()
