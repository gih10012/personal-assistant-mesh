import json
import unittest
from unittest.mock import Mock, patch

from assistant_mesh.codex import Codex, CodexError
from assistant_mesh.worker import Worker


class QuotaPreflightTests(unittest.TestCase):
    def quota(self, value):
        native = Codex.__new__(Codex)
        native.rpc = Mock(return_value=value)
        with patch('assistant_mesh.codex.time.time', return_value=1000):
            result = native.rate_limits()
        native.rpc.assert_called_once_with('account/rateLimits/read', {})
        return result

    def test_only_codex_bucket_is_selected(self):
        snapshot = {'primary': {'usedPercent': 100, 'resetsAt': 2000}}
        self.assertEqual({'status': 'exhausted', 'retry_at': 2000},
                         self.quota({'rateLimitsByLimitId': {'codex': snapshot}}))
        self.assertEqual({'status': 'available'}, self.quota({'rateLimitsByLimitId': {
            'codex': {'primary': {'usedPercent': 13}}, 'other_model': snapshot}}))
        self.assertEqual({'status': 'unknown'}, self.quota({'rateLimits': dict(snapshot, limitId='other_model')}))

    def test_legacy_secondary_and_unknown_snapshots(self):
        self.assertEqual({'status': 'exhausted', 'retry_at': 2000}, self.quota({'rateLimits': {
            'primary': {'usedPercent': 15}, 'secondary': {'usedPercent': 100, 'resetsAt': 2000}}}))
        for value in (None, {}, {'rateLimits': {'primary': {'usedPercent': True}}},
                      {'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': 999}}},
                      {'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': 999},
                                      'secondary': {'usedPercent': 15}}},
                      {'rateLimits': {'primary': {'usedPercent': 100}}}):
            self.assertEqual({'status': 'unknown'}, self.quota(value))

    def worker(self, agents):
        config = {'codex': {'workspace': '/private/workspace'}, 'codex_accounts': [
            {'auth_home': '/private/first'}, {'auth_home': '/private/second'}]}
        worker = Worker(config, client=Mock(), backend=Mock(side_effect=agents))
        worker.current = {'context': {}}
        return worker

    def agents(self):
        agents = [Mock(), Mock()]
        for agent in agents:
            agent.account.return_value = {'authenticated': True}
            agent.rate_limits.return_value = {'status': 'available'}
        return agents

    def test_exhausted_profile_falls_back_only_before_start(self):
        first, second = self.agents()
        first.rate_limits.return_value = {'status': 'exhausted', 'retry_at': 2000}
        worker = self.worker([first, second])
        self.assertIs(second, worker.open_backend())
        first.close.assert_called_once()
        first.start.assert_not_called()
        second.start.assert_not_called()
        self.assertEqual([
            {'index': 0, 'harness': 'codex', 'state': 'quota_exhausted',
             'cause': 'quota', 'quota_status': 'exhausted', 'retry_at': 2000},
            {'index': 1, 'harness': 'codex', 'state': 'selected',
             'cause': 'none', 'quota_status': 'available'}], worker.current['checkpoint']['backend_candidates'])

    def test_optional_quota_api_failure_does_not_gate_native_profile(self):
        first, second = self.agents()
        first.rate_limits.side_effect = CodexError('codex_rpc_failed_account_rateLimits_read')
        worker = self.worker([first, second])
        self.assertIs(first, worker.open_backend())
        second.account.assert_not_called()
        self.assertEqual([{'index': 0, 'harness': 'codex', 'state': 'selected',
            'cause': 'none', 'quota_status': 'unknown'}], worker.current['checkpoint']['backend_candidates'])

    def test_all_exhausted_returns_quota_reason_not_authentication_reason(self):
        agents = self.agents()
        for agent in agents:
            agent.rate_limits.return_value = {'status': 'exhausted'}
        with self.assertRaisesRegex(CodexError, '^codex_usage_limit_exceeded$'):
            self.worker(agents).open_backend()
        for agent in agents:
            agent.close.assert_called_once()
            agent.start.assert_not_called()

    def test_failed_started_turn_quota_is_classified_without_private_error_or_retry(self):
        native = Codex.__new__(Codex)
        native.thread_id, native.turn_id = 'thread', 'turn'
        native.deferred = [{'method': 'turn/completed', 'params': {'threadId': 'thread',
            'turn': {'id': 'turn', 'status': 'failed', 'error': {
                'codexErrorInfo': 'usageLimitExceeded', 'message': 'private-provider-details'}}}}]
        with self.assertRaisesRegex(CodexError, '^codex_usage_limit_exceeded$'):
            native.finish(timeout=1)

    def test_quota_then_account_rpc_failure_retains_sanitized_chain_and_original_last_error(self):
        first, second = self.agents()
        first.rate_limits.return_value = {'status': 'exhausted', 'retry_at': 2000,
            'email': 'PRIVATE@example.invalid', 'secret': 'PRIVATE-QUOTA-SECRET'}
        final_error = CodexError('PRIVATE-ACCOUNT-ERROR /private/profile PRIVATE@example.invalid')
        second.account.side_effect = final_error
        worker = self.worker([first, second])
        worker.current.update(id='fixture-task', epoch=3)
        with self.assertRaises(CodexError) as failure:
            worker.open_backend()
        self.assertIs(final_error, failure.exception)
        diagnostics = worker.current['checkpoint']['backend_candidates']
        self.assertEqual('quota_exhausted', diagnostics[0]['state'])
        self.assertEqual(2000, diagnostics[0]['retry_at'])
        self.assertEqual({'index': 1, 'harness': 'codex', 'state': 'account_unavailable',
            'cause': 'account_rpc', 'quota_status': 'not_checked'}, diagnostics[1])
        checkpoint_writes = [call[0][1] for call in worker.client.request.call_args_list]
        self.assertEqual({'id': 'fixture-task', 'epoch': 3,
            'checkpoint': {'backend_candidates': diagnostics}}, checkpoint_writes[-1])
        serialized = json.dumps(checkpoint_writes)
        for private in ('PRIVATE', '@', '/private/', 'auth_home', 'secret'):
            self.assertNotIn(private, serialized)
        first.close.assert_called_once()
        second.close.assert_called_once()
        first.start.assert_not_called()
        second.start.assert_not_called()

    def test_false_authentication_and_constructor_failure_use_only_fixed_categories(self):
        first, second = self.agents()
        first.account.return_value = {'authenticated': False, 'type': 'PRIVATE ACCOUNT TYPE'}
        failure = OSError('PRIVATE STARTUP /private/executable')
        worker = self.worker([first, failure])
        with self.assertRaises(OSError) as raised:
            worker.open_backend()
        self.assertIs(failure, raised.exception)
        self.assertEqual([
            {'index': 0, 'harness': 'codex', 'state': 'authentication_required',
             'cause': 'authentication', 'quota_status': 'not_checked'},
            {'index': 1, 'harness': 'codex', 'state': 'runtime_unavailable',
             'cause': 'initialization', 'quota_status': 'not_checked'}],
            worker.current['checkpoint']['backend_candidates'])
        self.assertNotIn('PRIVATE', json.dumps(worker.current['checkpoint']))
        first.close.assert_called_once()
        first.rate_limits.assert_not_called()

    def test_repeated_preflight_and_different_task_replace_prior_failure_chain(self):
        first, second = self.agents()
        first.rate_limits.return_value = {'status': 'exhausted', 'retry_at': 2000}
        worker = self.worker([first, second, self.agents()[0], self.agents()[0]])
        worker.current['checkpoint'] = {'backend_candidates': [{'secret': 'STALE-PRIOR-CHAIN'}],
                                        'thread_id': 'retain-native-thread'}
        worker.open_backend()
        previous = worker.current['checkpoint']['backend_candidates']
        worker.open_backend()
        self.assertEqual(2, len(previous))
        self.assertEqual([{'index': 0, 'harness': 'codex', 'state': 'selected',
            'cause': 'none', 'quota_status': 'available'}], worker.current['checkpoint']['backend_candidates'])
        self.assertEqual('retain-native-thread', worker.current['checkpoint']['thread_id'])
        worker.current = {'id': 'different-task', 'epoch': 7, 'context': {},
            'checkpoint': {'backend_candidates': previous}}
        worker.open_backend()
        self.assertEqual(1, len(worker.current['checkpoint']['backend_candidates']))
        self.assertIsNot(previous, worker.current['checkpoint']['backend_candidates'])
        self.assertEqual('different-task', worker.client.request.call_args[0][1]['id'])
        self.assertEqual(7, worker.client.request.call_args[0][1]['epoch'])

    def test_optional_checkpoint_write_failure_does_not_block_native_selection_or_replace_last_error(self):
        first, second = self.agents()
        worker = self.worker([first, second])
        worker.current.update(id='fixture-task', epoch=1)
        worker.client.request.side_effect = ValueError('PRIVATE TRANSPORT FAILURE')
        self.assertIs(first, worker.open_backend())
        first.start.assert_not_called()
        second.account.assert_not_called()
        first.close.assert_not_called()
        first.rate_limits.return_value = {'status': 'exhausted'}
        second.rate_limits.return_value = {'status': 'exhausted'}
        worker.backend.side_effect = [first, second]
        with self.assertRaisesRegex(CodexError, '^codex_usage_limit_exceeded$'):
            worker.open_backend()
        self.assertNotIn('PRIVATE', json.dumps(worker.current['checkpoint']))

    def test_retry_metadata_rejects_non_numeric_boolean_nonfinite_and_private_values(self):
        for retry in (None, True, -1, 0, float('inf'), float('-inf'), float('nan'),
                      'PRIVATE /private/auth.json', {'token': 'PRIVATE'}):
            with self.subTest(retry=retry):
                first, second = self.agents()
                first.rate_limits.return_value = {'status': 'exhausted', 'retry_at': retry}
                worker = self.worker([first, second])
                self.assertIs(second, worker.open_backend())
                diagnostics = worker.current['checkpoint']['backend_candidates']
                self.assertNotIn('retry_at', diagnostics[0])
                self.assertNotIn('PRIVATE', json.dumps(diagnostics))

    def test_unknown_quota_snapshots_and_no_optional_method_do_not_gate_native(self):
        for quota in (None, {'status': 'unknown'}, {'status': 'PRIVATE-STATUS', 'retry_at': 'PRIVATE'}):
            with self.subTest(quota=quota):
                first, second = self.agents()
                first.rate_limits.return_value = quota
                worker = self.worker([first, second])
                self.assertIs(first, worker.open_backend())
                self.assertEqual('unknown', worker.current['checkpoint']['backend_candidates'][0]['quota_status'])
                second.account.assert_not_called()
                first.start.assert_not_called()
        first, second = self.agents()
        first.rate_limits = None
        worker = self.worker([first, second])
        self.assertIs(first, worker.open_backend())
        self.assertEqual('unknown', worker.current['checkpoint']['backend_candidates'][0]['quota_status'])

    def test_configured_pi_fallback_has_safe_harness_identity_and_never_starts_a_turn(self):
        first, second = self.agents()
        for candidate in (first, second):
            candidate.rate_limits.return_value = {'status': 'exhausted'}
        worker = self.worker([first, second])
        worker.current.update(id='fixture-task', epoch=1)
        worker.config.update(control_url='http://127.0.0.1:17680', token_file='/private/token',
                             pi={'workspace': '/private/pi', 'cost_policy': 'local'})
        pi = Mock()
        pi.account.return_value = {'authenticated': True, 'type': 'PRIVATE PI CONFIGURATION'}
        with patch('assistant_mesh.pi.Pi', return_value=pi):
            self.assertIs(pi, worker.open_backend())
        self.assertEqual('pi', worker.harness)
        self.assertEqual({'index': 2, 'harness': 'pi', 'state': 'selected',
            'cause': 'none', 'quota_status': 'not_applicable'}, worker.current['checkpoint']['backend_candidates'][2])
        pi.start.assert_not_called()
        pi.rate_limits.assert_not_called()
        self.assertNotIn('/private', json.dumps(worker.current['checkpoint']))

    def test_preflight_diagnostics_do_not_mutate_native_identity_or_effect_marker(self):
        for marker in (True, False):
            first, second = self.agents()
            worker = self.worker([first, second])
            worker.current['checkpoint'] = {'thread_id': 'existing-native-thread',
                'turn_id': 'existing-native-turn', 'side_effect_started': marker}
            worker.current.update(id='fixture-task', epoch=4, status='running', leader_epoch=7)
            original = dict(worker.current)
            self.assertIs(first, worker.open_backend())
            self.assertIs(marker, worker.current['checkpoint']['side_effect_started'])
            self.assertEqual('existing-native-thread', worker.current['checkpoint']['thread_id'])
            self.assertEqual('existing-native-turn', worker.current['checkpoint']['turn_id'])
            for key in ('id', 'epoch', 'status', 'leader_epoch'):
                self.assertEqual(original[key], worker.current[key])
            self.assertEqual({'backend_candidates'}, set(worker.client.request.call_args[0][1]['checkpoint']))
            self.assertEqual({'id', 'epoch', 'checkpoint'}, set(worker.client.request.call_args[0][1]))

    def test_managed_worker_failure_saves_candidate_evidence_without_starting_or_replaying(self):
        first, second = self.agents()
        first.rate_limits.return_value = {'status': 'exhausted', 'retry_at': 2000}
        second.account.side_effect = CodexError('codex_rpc_failed_account_read')
        worker = self.worker([first, second])
        task = {'id': 'fixture-task', 'epoch': 2, 'checkpoint': {}, 'context': {}, 'input': 'fixture task'}
        worker.current = None  # run_once starts idle; the helper above prepares direct preflight calls.
        def request(route, body=None):
            if route == '/v1/claim':
                return {'task': task}
            if route in ('/v1/heartbeat', '/v1/task/update'):
                return {'ok': True}
            raise AssertionError('preflight must not execute a native session: ' + route)
        worker.client.request.side_effect = request
        self.assertTrue(worker.run_once())
        updates = [call[0][1] for call in worker.client.request.call_args_list if call[0][0] == '/v1/task/update']
        diagnostics = updates[-2]['checkpoint']['backend_candidates']
        self.assertEqual(['quota_exhausted', 'account_unavailable'], [row['state'] for row in diagnostics])
        self.assertEqual(2000, diagnostics[0]['retry_at'])
        self.assertEqual('waiting_backend', updates[-1]['status'])
        self.assertIn('codex_rpc_failed_account_read', updates[-1]['result'])
        self.assertTrue(all(update['id'] == 'fixture-task' and update['epoch'] == 2 for update in updates))
        self.assertTrue(all('side_effect_started' not in update.get('checkpoint', {}) for update in updates))
        self.assertIsNone(worker.current)
        first.start.assert_not_called()
        second.start.assert_not_called()
