"""Real Codex.start protocol with synthetic transport; no inference or secrets."""
import copy
import json
import queue
import unittest
from unittest.mock import Mock

from assistant_mesh.codex import Codex, CodexError
from assistant_mesh.codex_provider import provider_metadata
from assistant_mesh.governance_worker import make_worker
from assistant_mesh.worker import Worker
import tests.test_model_recovery as fixtures


class ProtocolCodex(Codex):
    """Replace only process/stdio transport; retain the actual native start."""
    instances = []
    actual_goal = None
    response_override = None

    def __init__(self, config, **options):
        self.config, self.auth_home = config, config['auth_home']
        self.provider = provider_metadata(config)
        self.tools, self.on_activity = [], options.get('on_activity')
        self.deferred, self.events, self.calls, self.closed = [], queue.Queue(), [], False
        self.native_observations = {}
        self.goal_snapshot = copy.deepcopy(self.actual_goal)
        self.__class__.instances.append(self)

    def rpc(self, method, params, timeout=30):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'account/read':
            return {'account': {'type': 'chatgpt', 'planType': 'pro'}}
        if method == 'account/rateLimits/read':
            return {'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': 2000}}}
        if method == 'model/list':
            return {'data': [{'id': self.config['model'], 'supportedReasoningEfforts': [
                {'reasoningEffort': 'high'}, {'reasoningEffort': 'max'}]}]}
        if method == 'thread/resume':
            if getattr(self, 'resume_error', None) is not None:
                raise self.resume_error
            value = dict(thread={'id': params['threadId'], 'model': params['model']},
                model=params['model'], modelProvider=params['modelProvider'],
                reasoningEffort=params['config']['model_reasoning_effort'],
                collaborationMode={'settings': {'model': params['model'],
                    'reasoning_effort': params['config']['model_reasoning_effort']}})
            value.update(self.response_override or {})
            return value
        if method == 'thread/goal/get':
            return {'goal': copy.deepcopy(self.goal_snapshot)}
        raise AssertionError('Unexpected native RPC: ' + method)

    def close(self):
        self.closed = True


class NativeModelTransitionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModelRecoveryTests('test_standby_reads_no_accounts_catalog_or_inference_and_unknown_quota_is_ready')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.route = self.fixture.install_free()
        self.state, self.config = self.fixture.state, self.fixture.config
        custom = self.config['codex_accounts'][-1]
        # One existing private home, separate provider metadata; no auth copied.
        self.config['codex']['auth_home'] = custom['auth_home']
        self.config['codex_accounts'] = [{'auth_home': custom['auth_home']}, custom]
        self.config['node_id'] = 'synthetic-node'
        self.goal = dict(threadId='same-original-thread', objective='Original objective',
            status='active', createdAt=800, updatedAt=950, tokensUsed=123, timeUsedSeconds=45,
            tokenBudget=1000, timeBudgetSeconds=600)
        self.original = fixtures.task()
        self.original['context']['operation_id'] = 'same-original-operation'
        self.original['checkpoint'] = dict(thread_id=self.goal['threadId'],
            codex_provider_identity='openai', codex_auth_home=custom['auth_home'],
            codex_node=self.config['node_id'], side_effect_started=False, goal=copy.deepcopy(self.goal),
            native_execution_intent={'settled': True}, native_lifecycle=dict(
                thread_id=self.goal['threadId'], outcome='yielded', settled=True,
                runtime_closed=True, quiescent=True, goal_managed=True, goal=copy.deepcopy(self.goal)))
        ProtocolCodex.instances, ProtocolCodex.response_override = [], None
        ProtocolCodex.actual_goal = copy.deepcopy(self.goal)
        self.activities = []

    def open(self):
        cls = make_worker(Worker, ProtocolCodex, self.state, scopes=[self.original['scope']])
        worker = cls(self.config, client=Mock())
        worker.current = self.original
        # Isolate protocol evidence; original Worker unknown-effect fencing is
        # exercised independently by its real run_once in test_model_recovery.
        worker.on_activity = lambda kind, value: self.activities.append((kind, copy.deepcopy(value)))
        agent = worker.open_backend()
        self.addCleanup(agent.close)
        return worker, agent

    def test_real_worker_and_codex_resume_same_thread_on_calibrated_free_provider(self):
        worker, agent = self.open()
        source = copy.deepcopy(self.original['checkpoint'])
        state = agent.start('Continue original task', source)
        self.assertEqual('codex', worker.harness)
        self.assertEqual(2, len(ProtocolCodex.instances))
        params = next(p for method, p in agent.calls if method == 'thread/resume')
        self.assertEqual(self.goal['threadId'], params['threadId'])
        self.assertEqual('free-fixture', params['modelProvider'])
        self.assertEqual('model-fixture', params['model'])
        self.assertEqual('high', params['config']['model_reasoning_effort'])
        self.assertFalse(any(m in ('thread/start', 'turn/start', 'thread/goal/set', 'thread/goal/clear')
                             for m, _ in agent.calls))
        self.assertFalse(any(m.startswith('account/') for m, _ in agent.calls))
        transition = state['native_lifecycle']['model_route_transition']
        self.assertEqual('openai', transition['source']['provider_identity'])
        self.assertEqual(self.goal, transition['source']['goal'])
        self.assertEqual(agent.provider['identity'], transition['target']['provider_identity'])
        self.assertTrue(transition['native_configuration_verified'])
        self.assertFalse(transition['effective_inference_verified'])
        self.assertFalse(transition['goal_reset'])
        self.assertEqual(self.goal, state['native_lifecycle']['goal'])
        self.assertEqual(source, self.original['checkpoint'])
        intent = next(v for k, v in self.activities if k == 'native_start_intent')
        self.assertEqual('intent', intent['native_lifecycle']['model_route_transition']['status'])
        ready = next(v for k, v in self.activities if k == 'session_ready')
        self.assertEqual(agent.provider['identity'], ready['codex_provider_identity'])
        self.assertEqual('openai', ready['native_lifecycle']['model_route_transition']['source']['provider_identity'])
        attempts = self.state.attempts(self.original['id'])
        self.assertTrue(all(a['operation_id'] == 'same-original-operation' for a in attempts))
        self.assertEqual('unknown', attempts[-1]['outcome'])

    def test_task_json_approval_cannot_authorize_cross_provider_resume(self):
        native = dict(self.config['codex'], **self.config['codex_accounts'][-1])
        native.update(model=self.route['model'], reasoning_effort=self.route['effort'])
        agent = ProtocolCodex(native)
        agent.config['model_route_transition'] = {'approved': True}
        checkpoint = copy.deepcopy(self.original['checkpoint'])
        checkpoint['model_route_transition'] = {'approved': True, 'source': 'openai'}
        with self.assertRaisesRegex(CodexError, 'provider_resume_identity_mismatch'):
            agent.start('No trusted capability', checkpoint)
        self.assertEqual([], agent.calls)

    def test_source_changed_after_admission_or_unknown_checkpoint_never_transmits_history(self):
        _, agent = self.open()
        checkpoint = copy.deepcopy(self.original['checkpoint'])
        checkpoint['side_effect_started'] = True
        checkpoint['approved'] = True
        before = len(agent.calls)
        with self.assertRaisesRegex(ValueError, 'safe_source_required'):
            agent.start('Never transmit', checkpoint)
        self.assertEqual(before, len(agent.calls))
        self.original['checkpoint'].update(runtime_failure=dict(native_start_attempted=True, outcome='unknown'))
        self.assertEqual('hold_original_operation', self.state.recovery_plan(self.original, self.config)['status'])

    def test_native_provider_or_effort_mismatch_retains_intent_and_does_not_replay(self):
        for wrong in ({'modelProvider': 'openai'}, {'reasoningEffort': 'low'},
                      {'collaborationMode': {'settings': {'model': 'weaker', 'reasoning_effort': 'low'}}}):
            ProtocolCodex.response_override = wrong
            _, agent = self.open()
            with self.assertRaisesRegex(ValueError, 'native_configuration_mismatch'):
                agent.start('Original task', self.original['checkpoint'])
            self.assertFalse(any(m == 'turn/start' for m, _ in agent.calls))
            before = len(agent.calls)
            with self.assertRaisesRegex(ValueError, 'already_used'):
                agent.start('No replay', self.original['checkpoint'])
            self.assertEqual(before, len(agent.calls))
            failure = self.state.attempts(self.original['id'])[-1]
            self.assertEqual('unknown', failure['outcome'])
            self.assertFalse(failure['failure']['rescue_harness_allowed'])
            # Rebuild the fixture state only for this independent synthetic
            # mismatch case, avoiding an unavailable route from the prior one.
            self.state.route_available(failure['route'])

    def test_original_goal_identity_and_accounting_cannot_reset_on_provider_change(self):
        ProtocolCodex.actual_goal = dict(self.goal, tokensUsed=0)
        _, agent = self.open()
        before = copy.deepcopy(self.original['checkpoint'])
        with self.assertRaisesRegex(CodexError, 'goal_continuity_unverified'):
            agent.start('Original task', self.original['checkpoint'])
        self.assertEqual({k: v for k, v in before.items() if k != 'task_governance'},
                         {k: v for k, v in self.original['checkpoint'].items() if k != 'task_governance'})
        self.assertFalse(any(m in ('thread/goal/set', 'thread/goal/clear', 'turn/start') for m, _ in agent.calls))
        self.assertEqual('openai', agent.native_lifecycle['model_route_transition']['source']['provider_identity'])

    def test_plan_rejects_unsettled_source_even_with_task_json_approval(self):
        self.original['checkpoint'].pop('native_lifecycle')
        self.original['checkpoint'].pop('native_execution_intent')
        self.original['context']['approved'] = True
        plan = self.state.recovery_plan(self.original, self.config)
        self.assertFalse(any(r['provider'] == 'free-fixture' for r in plan['qualified_routes']))
        self.assertIn('native_provider_transition_safe_source_required',
                      [r['reason'] for r in plan['blocked_routes']])
        self.assertNotIn('auth_home', json.dumps(plan))

    def test_owned_framework_crash_is_distinct_from_model_capacity(self):
        _, agent = self.open()
        agent.process = Mock()
        agent.process.poll.return_value = 137
        agent.resume_error = CodexError('codex_disconnected')
        with self.assertRaisesRegex(CodexError, 'codex_disconnected'):
            agent.start('Original task', self.original['checkpoint'])
        attempt = self.state.attempts(self.original['id'])[-1]
        self.assertEqual('framework', attempt['failure']['category'])
        self.assertTrue(attempt['failure']['rescue_harness_allowed'])
        self.assertEqual('unknown', attempt['outcome'])
        self.assertFalse(attempt['automatic_operation_replay'])


if __name__ == '__main__':
    unittest.main()
