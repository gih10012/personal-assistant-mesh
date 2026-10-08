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

    def test_optional_quota_api_failure_does_not_gate_native_profile(self):
        first, second = self.agents()
        first.rate_limits.side_effect = CodexError('codex_rpc_failed_account_rateLimits_read')
        worker = self.worker([first, second])
        self.assertIs(first, worker.open_backend())
        second.account.assert_not_called()

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
