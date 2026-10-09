"""Offline failure diagnosis, not native execution/recovery proof."""
import copy
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from assistant_mesh.codex import CodexError
from assistant_mesh.worker import Worker


PRIVATE = 'PRIVATE_TOKEN https://private.invalid/?token=PRIVATE /private/account/history'


class FixtureClient:
    def __init__(self, task):
        self.task = task
        self.calls = []
        self.failure = None

    def request(self, route, body=None):
        self.calls.append((route, copy.deepcopy(body)))
        if self.failure:
            failure = self.failure(route, body)
            if failure:
                raise failure
        if route == '/v1/claim':
            return {'task': self.task}
        if route == '/v1/agent/action':
            return {'children': []}
        return {'ok': True}


class RuntimeFailureReportTests(unittest.TestCase):
    def setUp(self):
        self.task = {'id': 'original-task', 'epoch': 4, 'node': 'fixture-node',
                     'leader_epoch': 7, 'status': 'running', 'scope': 'leader:owner',
                     'input': 'Use any native Shell or network tools appropriate to my task.',
                     'context': {}, 'checkpoint': {
                         'thread_id': 'original-thread', 'turn_id': 'original-turn',
                         'side_effect_started': True, 'harness': 'codex',
                         'codex_node': 'previous-node', 'codex_auth_home': '/fixture/previous'},
                     'sessions': {'codex': {'harness': 'codex', 'node': 'previous-node',
                         'artifact': 'original-artifact', 'state': {
                             'thread_id': 'original-thread', 'turn_id': 'original-turn',
                             'codex_auth_home': '/fixture/previous', 'harness': 'codex'}}}}
        self.original = copy.deepcopy(self.task)
        self.client = FixtureClient(self.task)
        self.worker = Worker({'node_id': 'fixture-node', 'codex': {
            'auth_home': '/fixture/selected', 'sandbox': 'danger-full-access'}}, client=self.client)
        self.worker.harness = 'codex'
        self.agent = MagicMock()
        self.agent.__enter__.return_value = self.agent
        self.agent.auth_home = '/fixture/selected'
        self.agent.start.return_value = {'thread_id': 'original-thread', 'turn_id': 'new-turn'}
        self.agent.finish.return_value = 'fixture completed answer'
        self.agent.native_rollout.return_value = None
        self.agent.goal.return_value = None
        self.worker.open_backend = Mock(return_value=self.agent)
        self.worker.tick = Mock()
        self.worker.resource_reference = Mock(return_value={'native_tools_intercepted': False})
        self.restore = patch('assistant_mesh.worker.sessions.restore', return_value='/fixture/restored')
        self.save = patch('assistant_mesh.worker.sessions.save')
        self.restore_mock = self.restore.start()
        self.save_mock = self.save.start()
        self.addCleanup(self.restore.stop)
        self.addCleanup(self.save.stop)

    def updates(self):
        return [body for route, body in self.client.calls if route == '/v1/task/update']

    def failure(self, phase, category, attempted, status='waiting_backend'):
        update = self.updates()[-1]
        report = update['checkpoint']['runtime_failure']
        self.assertEqual({'phase': phase, 'category': category,
                          'attempt_epoch': 4,
                          'native_start_attempted': attempted, 'outcome': 'unknown',
                          'automatic_history_overwrite': False,
                          'retry_authorized_by_report': False, 'recovery_policy_changed': False,
                          'native_tools_intercepted': False}, report)
        self.assertEqual({'runtime_failure'}, set(update['checkpoint']))
        self.assertEqual('original-task', update['id'])
        self.assertEqual(4, update['epoch'])
        self.assertEqual(status, update['status'])
        self.assertNotIn(PRIVATE, json.dumps(self.client.calls))
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(report))
        self.assertNotIn('/fixture', json.dumps(report))
        self.assertIsNone(self.worker.current)
        self.assertIsNone(self.worker.agent)
        return report

    def assert_original_identity(self):
        for key in ('id', 'epoch', 'node', 'leader_epoch', 'status', 'scope'):
            self.assertEqual(self.original[key], self.task[key])
        for key in ('thread_id', 'turn_id', 'side_effect_started', 'codex_auth_home', 'codex_node'):
            self.assertEqual(self.original['checkpoint'][key], self.task['checkpoint'][key])
        self.assertEqual(self.original['sessions'], self.task['sessions'])

    def test_destination_conflict_diagnosed_without_start_finish_or_identity_change(self):
        self.restore_mock.side_effect = ValueError('native_session_destination_conflict')
        self.assertTrue(self.worker.run_once())
        self.failure('native_restore', 'native_session_destination_conflict', False)
        self.assert_original_identity()
        self.agent.start.assert_not_called()
        self.agent.finish.assert_not_called()
        self.save_mock.assert_not_called()
        self.restore_mock.assert_called_once()

    def test_restore_before_start_does_not_clear_false_or_true_effect_marker(self):
        self.task['checkpoint']['side_effect_started'] = False
        self.original = copy.deepcopy(self.task)
        self.restore_mock.side_effect = ValueError('native_session_artifact_empty')
        self.worker.run_once()
        self.failure('native_restore', 'native_session_artifact_empty', False)
        self.assert_original_identity()
        self.assertFalse(self.task['checkpoint']['side_effect_started'])

    def test_missing_migration_artifact_preserves_known_native_error(self):
        self.task['sessions']['codex']['artifact'] = None
        self.original = copy.deepcopy(self.task)
        self.worker.run_once()
        self.failure('native_restore', 'native_session_migration_unavailable', False)
        self.restore_mock.assert_not_called()
        self.agent.start.assert_not_called()
        self.assert_original_identity()

    def test_unknown_restore_error_is_generic_not_exception_text(self):
        self.restore_mock.side_effect = ValueError(PRIVATE)
        self.worker.run_once()
        self.failure('native_restore', 'worker_unavailable', False)
        self.assert_original_identity()

    def test_known_restore_prefix_with_private_suffix_is_not_whitelisted(self):
        self.restore_mock.side_effect = ValueError('native_session_destination_conflict ' + PRIVATE)
        self.worker.run_once()
        self.failure('native_restore', 'worker_unavailable', False)

    def test_preflight_category_is_reported_without_start_attempt(self):
        self.worker.open_backend.side_effect = CodexError('codex_usage_limit_exceeded')
        self.worker.run_once()
        self.failure('backend_preflight', 'codex_usage_limit_exceeded', False)
        self.restore_mock.assert_not_called()
        self.agent.start.assert_not_called()
        self.assert_original_identity()

    def test_no_local_backend_available_keeps_fixed_category_without_fake_inference(self):
        self.worker.open_backend.side_effect = CodexError('no_local_backend_available')
        self.worker.run_once()
        self.failure('backend_preflight', 'no_local_backend_available', False)
        self.assertIn('no_local_backend_available', self.updates()[-1]['result'])
        self.agent.start.assert_not_called()
        self.agent.finish.assert_not_called()
        self.restore_mock.assert_not_called()
        self.assert_original_identity()

    def test_no_local_backend_category_with_private_suffix_is_not_accepted(self):
        self.worker.open_backend.side_effect = CodexError('no_local_backend_available ' + PRIVATE)
        self.worker.run_once()
        self.failure('backend_preflight', 'worker_unavailable', False)
        self.assertNotIn(PRIVATE, self.updates()[-1]['result'])
        self.agent.start.assert_not_called()
        self.assert_original_identity()

    def test_unknown_codex_error_is_sanitized_in_result_and_checkpoint(self):
        self.worker.open_backend.side_effect = CodexError(PRIVATE)
        self.worker.run_once()
        self.failure('backend_preflight', 'worker_unavailable', False)
        self.assertNotIn('PRIVATE', self.updates()[-1]['result'])

    def test_safe_existing_rpc_code_retained_without_raw_details(self):
        self.worker.open_backend.side_effect = CodexError('codex_rpc_failed_account_read')
        self.worker.run_once()
        self.failure('backend_preflight', 'codex_rpc_failed_account_read', False)
        self.assertIn('codex_rpc_failed_account_read', self.updates()[-1]['result'])

    def test_existing_auth_wait_status_policy_is_not_reclassified_by_report(self):
        self.worker.open_backend.side_effect = CodexError('auth ' + PRIVATE)
        self.worker.run_once()
        self.failure('backend_preflight', 'worker_unavailable', False, status='waiting_auth')
        self.assertNotIn(PRIVATE, self.updates()[-1]['result'])

    def test_start_lost_reply_is_unknown_even_without_returned_checkpoint(self):
        self.agent.start.side_effect = OSError(PRIVATE)
        self.worker.run_once()
        self.failure('native_start', 'worker_unavailable', True)
        self.agent.start.assert_called_once()
        self.agent.finish.assert_not_called()
        self.save_mock.assert_not_called()
        self.assert_original_identity()

    def test_missing_effect_flag_is_not_proof_that_start_did_not_execute(self):
        self.task['checkpoint'].pop('side_effect_started')
        self.agent.start.side_effect = OSError(PRIVATE)
        self.worker.run_once()
        self.failure('native_start', 'worker_unavailable', True)
        self.assertNotIn('side_effect_started', self.task['checkpoint'])
        self.assertNotIn('side_effect_started', self.updates()[-1]['checkpoint'])

    def test_started_quota_failure_retains_effect_marker_and_never_falls_back(self):
        self.agent.finish.side_effect = CodexError('codex_usage_limit_exceeded')
        self.worker.run_once()
        self.failure('native_finish', 'codex_usage_limit_exceeded', True)
        self.assertTrue(self.task['checkpoint']['side_effect_started'])
        self.worker.open_backend.assert_called_once()
        self.agent.start.assert_called_once()
        self.agent.finish.assert_called_once()
        self.save_mock.assert_not_called()

    def test_session_save_failure_does_not_clear_actual_task_effect_marker(self):
        self.save_mock.side_effect = ValueError(PRIVATE)
        self.worker.run_once()
        self.failure('session_save', 'worker_unavailable', True)
        self.assertTrue(self.task['checkpoint']['side_effect_started'])
        self.agent.finish.assert_called_once()

    def test_post_native_coordination_failure_is_not_native_restore_failure(self):
        self.client.failure = lambda route, body: OSError(PRIVATE) if route == '/v1/agent/action' else None
        self.worker.run_once()
        self.failure('coordination', 'worker_unavailable', True)
        self.save_mock.assert_called_once()
        self.assertTrue(self.task['checkpoint']['side_effect_started'])

    def test_finalize_lost_reply_is_unknown_with_original_task_identity(self):
        self.client.failure = lambda route, body: (
            OSError(PRIVATE) if route == '/v1/task/update' and body.get('status') == 'completed' else None)
        self.worker.run_once()
        self.failure('task_finalize', 'worker_unavailable', True)
        self.assertEqual(2, len(self.updates()))
        self.assertTrue(all(body['id'] == 'original-task' and body['epoch'] == 4 for body in self.updates()))

    def test_diagnostic_ledger_failure_does_not_replay_start_or_restore(self):
        self.restore_mock.side_effect = ValueError('native_session_destination_conflict')
        self.client.failure = lambda route, body: OSError(PRIVATE) if route == '/v1/task/update' else None
        self.worker.run_once()
        self.failure('native_restore', 'native_session_destination_conflict', False)
        self.restore_mock.assert_called_once()
        self.agent.start.assert_not_called()
        self.assert_original_identity()

    def test_success_keeps_native_features_and_existing_finalization(self):
        self.worker.run_once()
        self.agent.start.assert_called_once()
        prompt, resume = self.agent.start.call_args[0]
        self.assertIn(self.task['input'], prompt)
        self.assertEqual('original-thread', resume['thread_id'])
        self.assertNotIn('runtime_failure', resume)
        self.agent.finish.assert_called_once()
        self.assertEqual('completed', self.updates()[-1]['status'])
        self.assertFalse(self.updates()[-1]['checkpoint']['side_effect_started'])
        self.assertEqual('fixture completed answer', self.updates()[-1]['result'])
        self.assertNotIn('runtime_failure', self.task['checkpoint'])

    def test_success_does_not_clear_prior_diagnosis_or_present_it_as_current_epoch(self):
        previous = {'phase': 'native_restore', 'category': 'native_session_destination_conflict',
                    'attempt_epoch': 2, 'native_start_attempted': False, 'outcome': 'unknown'}
        self.task['checkpoint']['runtime_failure'] = copy.deepcopy(previous)
        self.worker.run_once()
        self.assertEqual(previous, self.task['checkpoint']['runtime_failure'])
        self.assertEqual(4, self.updates()[-1]['epoch'])
        self.assertEqual('completed', self.updates()[-1]['status'])
        self.assertNotIn('runtime_failure', self.updates()[-1]['checkpoint'])


if __name__ == '__main__':
    unittest.main()
