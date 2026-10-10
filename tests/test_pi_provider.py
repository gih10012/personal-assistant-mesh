"""Offline Pi provider contracts; no native executable, credentials or inference."""
import copy
import io
import json
import os
from pathlib import Path
import queue
import tempfile
import time
import unittest
from unittest import mock

from assistant_mesh.codex import CodexError
from assistant_mesh.pi import Pi


class PiProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / 'pi-profile'
        self.profile.mkdir(mode=0o700)
        self.model = {'id': 'arbitrary-model', 'provider': 'owner-provider',
                      'api': 'openai-completions', 'name': 'Synthetic model'}
        self.config = {'cost_policy': 'local', 'model': self.model['id'],
                       'provider': self.model['provider'], 'agent_dir': str(self.profile),
                       'session_dir': str(self.root / 'sessions'), 'workspace': str(self.root)}

    def private_json(self, name, value):
        path = self.profile / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return str(path)

    def free_config(self, **updates):
        contract = dict(provider=self.model['provider'], models=[self.model['id']],
                        expires_at=time.time() + 3600, zero_cost=True,
                        paid_fallback=False, auto_reload=False)
        contract.update(updates)
        return dict(self.config, cost_policy='free_api',
                    cost_contract=self.private_json('free-cost.json', contract))

    def spawn(self, config=None, failure=None):
        process = mock.Mock(stdout=io.StringIO(''), stdin=io.StringIO())
        with mock.patch('assistant_mesh.pi.subprocess.Popen', return_value=process,
                        side_effect=failure) as popen, mock.patch('assistant_mesh.pi.threading.Thread'):
            agent = Pi(config or self.config)
        return agent, popen

    def facade(self, replies, config=None):
        agent = Pi.__new__(Pi)
        agent.config = config or self.config
        agent._run_state = 'idle'
        agent.serial = 2
        agent.on_activity = None
        agent.events, agent.deferred = queue.Queue(), []
        agent.rpc = mock.Mock(side_effect=replies)
        return agent

    def state(self, model=None, **updates):
        value = {'model': model or self.model, 'sessionId': 'same-pi-session',
                 'sessionFile': '/private/same-pi.jsonl', 'isStreaming': False,
                 'isCompacting': False, 'pendingMessageCount': 0}
        value.update(updates)
        return value

    def test_isolated_profile_only_changes_child_environment(self):
        models = self.private_json('models.json', {'providers': {}})
        self.private_json('auth.json', {'synthetic': {'key': 'never-read-or-log'}})
        previous = os.environ.get('PI_CODING_AGENT_DIR')
        config = dict(self.config, models_file=models)
        with mock.patch('pathlib.Path.read_text', side_effect=AssertionError('no auth read')):
            agent, popen = self.spawn(config)
        command = popen.call_args[0][0]
        self.assertEqual(str(self.profile), popen.call_args[1]['env']['PI_CODING_AGENT_DIR'])
        self.assertEqual(previous, os.environ.get('PI_CODING_AGENT_DIR'))
        self.assertEqual(self.model['id'], command[command.index('--model') + 1])
        self.assertEqual(self.model['provider'], command[command.index('--provider') + 1])
        self.assertNotIn('--api-key', command)
        self.assertNotIn('never-read-or-log', str(command))
        self.assertEqual('idle', agent._run_state)

    def test_private_models_location_cannot_silently_copy_or_override(self):
        path = self.private_json('not-models.json', {'providers': {}})
        with self.assertRaisesRegex(CodexError, '^pi_models_file_location_invalid$'):
            self.spawn(dict(self.config, models_file=path))
        self.assertFalse((self.profile / 'models.json').exists())

    def test_unsafe_profile_and_public_models_are_rejected_without_spawn(self):
        model_file = self.private_json('models.json', {'providers': {}})
        Path(model_file).chmod(0o644)
        with mock.patch('assistant_mesh.pi.subprocess.Popen') as popen:
            with self.assertRaisesRegex(CodexError, '^pi_private_configuration_required$'):
                Pi(self.config)
            popen.assert_not_called()
        Path(model_file).chmod(0o600)
        self.profile.chmod(0o755)
        with self.assertRaisesRegex(CodexError, '^pi_private_configuration_required$'):
            self.spawn()

    def test_symlink_profile_and_models_are_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.profile, target_is_directory=True)
        with self.assertRaisesRegex(CodexError, '^pi_private_configuration_required$'):
            self.spawn(dict(self.config, agent_dir=str(alias)))
        file = self.profile / 'models.json'
        file.symlink_to(self.root / 'nonexistent')
        with self.assertRaisesRegex(CodexError, '^pi_private_configuration_required$'):
            self.spawn()

    def test_public_native_auth_is_rejected_without_reading_or_launch(self):
        auth = self.private_json('auth.json', {'synthetic': {'key': 'never-read'}})
        Path(auth).chmod(0o644)
        with mock.patch('pathlib.Path.read_text', side_effect=AssertionError('no auth read')), \
                mock.patch('assistant_mesh.pi.subprocess.Popen') as launch:
            with self.assertRaisesRegex(CodexError, '^pi_private_configuration_required$'):
                Pi(self.config)
            launch.assert_not_called()

    def test_models_file_and_free_api_require_isolated_profile(self):
        config = self.free_config()
        config.pop('agent_dir')
        with self.assertRaisesRegex(CodexError, '^pi_agent_dir_required$'):
            self.spawn(config)
        config = dict(self.config, models_file='/private/models.json')
        config.pop('agent_dir')
        with self.assertRaisesRegex(CodexError, '^pi_agent_dir_required$'):
            self.spawn(config)

    def test_legacy_subscription_and_local_configuration_remain_supported(self):
        for policy in ('existing_subscription', 'local'):
            with self.subTest(policy=policy):
                config = dict(self.config, cost_policy=policy)
                config.pop('agent_dir')
                agent, popen = self.spawn(config)
                self.assertEqual(policy, agent.config['cost_policy'])
                self.assertEqual('pi', popen.call_args[0][0][0])

    def test_free_api_validates_actual_scope_before_process_start(self):
        agent, popen = self.spawn(self.free_config())
        self.assertEqual('free_api', agent.config['cost_policy'])
        self.assertEqual(1, popen.call_count)
        for updates in ({'expires_at': time.time() - 1}, {'paid_fallback': True},
                        {'auto_reload': True}, {'zero_cost': False}, {'models': ['different']}):
            with self.subTest(updates=updates), mock.patch('assistant_mesh.pi.subprocess.Popen') as launch:
                with self.assertRaises(CodexError):
                    Pi(self.free_config(**updates))
                launch.assert_not_called()

    def test_paid_api_or_unscoped_free_cannot_be_called_subscription(self):
        for policy in ('paid_api', 'free_api', 'unknown'):
            with self.subTest(policy=policy), mock.patch('assistant_mesh.pi.subprocess.Popen') as launch:
                with self.assertRaises(CodexError):
                    Pi(dict(self.config, cost_policy=policy))
                launch.assert_not_called()

    def test_catalog_projection_omits_endpoint_headers_and_credentials(self):
        raw = dict(self.model, baseUrl='https://private.invalid/key',
                   headers={'Authorization': 'Bearer SECRET'}, apiKey='SECRET', auth={'key': 'SECRET'},
                   contextWindow=16000, maxTokens=2048, reasoning=True, input=['text', 'image'],
                   cost={'input': 0, 'output': 0, 'arbitrary': 'SECRET'})
        original = copy.deepcopy(raw)
        agent = self.facade([{'models': [raw]}])
        catalog = agent.models()
        self.assertEqual('pi_configured_catalog', catalog['source'])
        self.assertFalse(catalog['live_auth_verified'])
        self.assertFalse(catalog['inference_verified'])
        self.assertNotIn('SECRET', json.dumps(catalog))
        self.assertNotIn('private.invalid', json.dumps(catalog))
        self.assertEqual({'input': 0, 'output': 0}, catalog['models'][0]['declared_cost'])
        self.assertEqual(original, raw)
        agent.rpc.assert_called_once_with('get_available_models')

    def test_malformed_catalog_is_a_fixed_safe_error(self):
        for reply in ({}, {'models': 'SECRET'}, {'models': [{'apiKey': 'SECRET'}]}):
            with self.subTest(reply=reply):
                agent = self.facade([reply])
                with self.assertRaisesRegex(CodexError, '^pi_model_catalog_invalid$'):
                    agent.models()

    def test_model_selection_uses_native_set_model_without_prompt_or_new_session(self):
        selected = dict(self.model, id='another-arbitrary-model')
        agent = self.facade([self.state(), selected])
        result = agent.select_model(selected['provider'], selected['id'])
        self.assertEqual(selected['id'], result['id'])
        self.assertEqual([mock.call('get_state'), mock.call('set_model',
            {'provider': selected['provider'], 'modelId': selected['id']})], agent.rpc.call_args_list)
        self.assertEqual('idle', agent._run_state)

    def test_selection_cannot_use_free_contract_for_a_paid_or_other_model(self):
        agent = self.facade([], self.free_config())
        with self.assertRaises(CodexError):
            agent.select_model(self.model['provider'], 'not-authorized')
        agent.rpc.assert_not_called()

    def test_configured_owner_route_cannot_select_another_provider(self):
        agent = self.facade([])
        with self.assertRaisesRegex(CodexError, '^pi_provider_selection_requires_config$'):
            agent.select_model('other-paid-provider', self.model['id'])
        agent.rpc.assert_not_called()

    def test_legacy_unconfigured_route_locks_native_current_provider(self):
        config = dict(self.config)
        config.pop('provider')
        agent = self.facade([self.state()], config)
        with self.assertRaisesRegex(CodexError, '^pi_provider_selection_requires_config$'):
            agent.select_model('other-paid-provider', self.model['id'])
        self.assertEqual(self.model['provider'], agent._bound_provider)
        self.assertEqual([mock.call('get_state')], agent.rpc.call_args_list)
        agent.rpc.reset_mock(side_effect=True)
        agent.rpc.side_effect = [self.state(), self.model]
        self.assertEqual(self.model['id'], agent.select_model(self.model['provider'], self.model['id'])['id'])

    def test_restored_other_provider_never_receives_a_prompt(self):
        agent = self.facade([{}, self.state(dict(self.model, provider='other-paid-provider'))])
        with self.assertRaisesRegex(CodexError, '^pi_provider_selection_requires_config$'):
            agent.start('continue', {'pi_session_file': '/private/same-pi.jsonl'})
        self.assertEqual([mock.call('switch_session', {'sessionPath': '/private/same-pi.jsonl'}),
                          mock.call('get_state')], agent.rpc.call_args_list)

    def test_running_unknown_or_pending_work_does_not_switch_models(self):
        for phase in ('running', 'prompt_unknown', 'selection_unknown'):
            agent = self.facade([])
            agent._run_state = phase
            with self.assertRaisesRegex(CodexError, '^pi_model_selection_while_active$'):
                agent.select_model(self.model['provider'], self.model['id'])
            agent.rpc.assert_not_called()
        for flag in ('isStreaming', 'isCompacting', 'pendingMessageCount'):
            agent = self.facade([self.state(**{flag: True})])
            with self.assertRaisesRegex(CodexError, '^pi_model_selection_while_active$'):
                agent.select_model(self.model['provider'], self.model['id'])
            self.assertEqual([mock.call('get_state')], agent.rpc.call_args_list)

    def test_unknown_set_model_receipt_never_retries_or_prompts(self):
        agent = self.facade([self.state(), CodexError('pi_disconnected')])
        with self.assertRaisesRegex(CodexError, '^pi_disconnected$'):
            agent.select_model(self.model['provider'], self.model['id'])
        self.assertEqual('selection_unknown', agent._run_state)
        with self.assertRaisesRegex(CodexError, '^pi_prompt_already_attempted$'):
            agent.start('must not run')
        self.assertEqual(2, agent.rpc.call_count)

    def test_mismatched_set_model_response_is_not_accepted(self):
        agent = self.facade([self.state(), dict(self.model, id='different')])
        with self.assertRaisesRegex(CodexError, '^pi_model_selection_mismatch$'):
            agent.select_model(self.model['provider'], self.model['id'])
        self.assertEqual('selection_unknown', agent._run_state)

    def test_contract_expiry_after_set_model_preserves_selection_uncertainty(self):
        config = self.free_config()
        def reply(method, parameters=None):
            if method == 'get_state':
                return self.state()
            self.free_config(expires_at=time.time() - 1)
            return self.model
        agent = self.facade([], config)
        agent.rpc.side_effect = reply
        with self.assertRaisesRegex(CodexError, '^model_provider_free_contract_invalid$'):
            agent.select_model(self.model['provider'], self.model['id'])
        self.assertEqual('selection_unknown', agent._run_state)
        with self.assertRaisesRegex(CodexError, '^pi_prompt_already_attempted$'):
            agent.start('must not run')
        self.assertEqual(2, agent.rpc.call_count)

    def test_resume_preserves_session_and_validates_restored_model(self):
        config = self.free_config()
        agent = self.facade([{}, self.state(), {'disposition': 'started'}], config)
        result = agent.start('continue once', {'pi_session_file': '/private/same-pi.jsonl'})
        self.assertEqual('same-pi-session', result['thread_id'])
        self.assertEqual('/private/same-pi.jsonl', result['pi_session_file'])
        self.assertEqual([mock.call('switch_session', {'sessionPath': '/private/same-pi.jsonl'}),
            mock.call('get_state'), mock.call('prompt', {'message': 'continue once'})], agent.rpc.call_args_list)
        self.assertEqual('running', agent._run_state)

    def test_restored_unauthorized_model_stops_before_prompt_without_overwriting_it(self):
        agent = self.facade([{}, self.state(dict(self.model, id='paid-restored'))], self.free_config())
        with self.assertRaises(CodexError):
            agent.start('continue', {'pi_session_file': '/private/same-pi.jsonl'})
        self.assertEqual([mock.call('switch_session', {'sessionPath': '/private/same-pi.jsonl'}),
                          mock.call('get_state')], agent.rpc.call_args_list)

    def test_absent_actual_model_or_pending_state_stops_before_prompt(self):
        for state in ({'sessionId': 'same'}, self.state(isStreaming=True)):
            agent = self.facade([state])
            with self.assertRaises(CodexError):
                agent.start('work')
            self.assertEqual([mock.call('get_state')], agent.rpc.call_args_list)

    def test_contract_expiry_in_session_ready_callback_prevents_inference(self):
        config = self.free_config()
        def expire(_name, _value):
            self.free_config(expires_at=time.time() - 1)
        agent = self.facade([self.state()], config)
        agent.on_activity = expire
        with self.assertRaises(CodexError):
            agent.start('work')
        self.assertEqual([mock.call('get_state')], agent.rpc.call_args_list)

    def test_ambiguous_prompt_ack_is_not_replayed_or_switched(self):
        for reply in (CodexError('pi_disconnected'), {'disposition': 'queued'}):
            agent = self.facade([self.state(), reply])
            with self.assertRaises(CodexError):
                agent.start('work once')
            self.assertEqual('prompt_unknown', agent._run_state)
            with self.assertRaises(CodexError):
                agent.start('work again')
            with self.assertRaises(CodexError):
                agent.select_model(self.model['provider'], self.model['id'])
            self.assertEqual(2, agent.rpc.call_count)

    def test_natural_settled_run_allows_next_work_in_same_session(self):
        agent = self.facade([self.state(), {'disposition': 'started'}, self.state(), self.model])
        agent.start('work')
        agent.deferred = [{'type': 'message_end', 'message': {'role': 'assistant',
            'stopReason': 'stop', 'content': [{'type': 'text', 'text': 'done'}]}},
            {'type': 'agent_settled'}]
        self.assertEqual('done', agent.finish(timeout=1))
        self.assertEqual('settled', agent._run_state)
        self.assertEqual(self.model['id'], agent.select_model(self.model['provider'], self.model['id'])['id'])

    def test_failed_or_timed_out_run_keeps_uncertainty_and_cannot_replay(self):
        for failed in (True, False):
            agent = self.facade([self.state(), {'disposition': 'started'}])
            agent.start('work')
            if failed:
                agent.deferred = [{'type': 'message_end', 'message': {'role': 'assistant',
                    'stopReason': 'error', 'content': []}}, {'type': 'agent_settled'}]
            with self.assertRaises(CodexError):
                agent.finish(timeout=1 if failed else 0)
            with self.assertRaises(CodexError):
                agent.start('must not replay')
            self.assertEqual(2, agent.rpc.call_count)


if __name__ == '__main__':
    unittest.main()
