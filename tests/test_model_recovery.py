"""Synthetic local recovery/Worker acceptance; no real inference or credentials."""
import copy
import errno
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

from assistant_mesh.codex import CodexError
from assistant_mesh.codex_provider import provider_metadata
from assistant_mesh.governance_worker import make_worker
from assistant_mesh.leader_governance import Governance
from assistant_mesh.model_recovery import classify_failure
from assistant_mesh.worker import Worker
from tests.test_leader_governance import FakeCodex, POLICY, task
from tests.test_model_frontier import candidate, pair, point, snapshot


class ModelRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root, self.now = Path(self.temp.name), 1000
        self.policy = copy.deepcopy(POLICY)
        self.config = {'codex': {'workspace': '/not-used', 'auth_home': 'account-a'},
            'codex_accounts': [{'auth_home': 'account-a'}, {'auth_home': 'account-b'}],
            'pi': {'never_launch_for_model_failure': True}}
        self.state = self.governance()
        FakeCodex.instances, FakeCodex.quotas = [], {}

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return str(path)

    def governance(self):
        return Governance(str(self.root / 'governance.sqlite'), self.policy, clock=lambda: self.now)

    def install_free(self):
        profile = self.root / 'free-profile'
        profile.mkdir(mode=0o700)
        provider = self.write('provider.json', dict(name='Fixture free provider',
            base_url='https://provider.example.invalid/v1', wire_api='responses',
            requires_openai_auth=False))
        contract = self.write('cost.json', dict(provider='free-fixture', models=['model-fixture'],
            zero_cost=True, paid_fallback=False, auto_reload=False, expires_at=time.time() + 86400))
        self.config['codex_accounts'].append(dict(auth_home=str(profile), model_provider='free-fixture',
            provider_file=provider, cost_policy='free_api', cost_contract=contract,
            endpoint_id='fixture-endpoint', account_id='fixture-account', egress_id='fixture-egress'))
        route = dict(id='calibrated-free', provider='free-fixture', model='model-fixture', effort='high',
            harness='codex', quality='strong', cost_basis='free', difficulties=['D1', 'D2', 'D3'],
            endpoint_id='fixture-endpoint', allowed_scopes=['owner_private'],
            capabilities=['tools', 'vision', 'text'], context_tokens=32000)
        route['quality_evidence'] = dict(verified=True, quality='strong',
            pair=pair('model-fixture', 'high', 'free-fixture', 'fixture-endpoint'),
            evidence_ref='synthetic:observed-qualified-task', observed_at=900, expires_at=3000)
        self.policy['routes'].append(route)
        self.state = self.governance()
        return route

    def worker(self, codex=FakeCodex):
        cls = make_worker(Worker, codex, self.state, scopes=[task()['scope']])
        worker = cls(self.config, client=Mock())
        worker.current = task()
        return worker

    def exhaust_openai(self):
        FakeCodex.quotas = {name: {'rateLimits': {'primary': {'usedPercent': 100,
            'resetsAt': reset}}} for name, reset in [('account-a', 2000), ('account-b', 2500)]}

    def test_failure_categories_do_not_treat_429_or_auth_as_framework(self):
        cases = [(CodexError('codex_usage_limit_exceeded'), None, 'quota'),
            (CodexError('codex_rpc_failed_turn_start'), {'data': {'status': 429}}, 'capacity'),
            (CodexError('codex_rpc_failed_account_read'), {'data': {'http_status': 401}}, 'auth'),
            (TimeoutError('private server detail'), None, 'network'),
            (CodexError('codex_reasoning_effort_unsupported'), None, 'provider'),
            (FileNotFoundError(errno.ENOENT, 'private detail', '/missing/codex'), None, 'framework')]
        for error, protocol, category in cases:
            result = classify_failure(error, self.now, 'framework_start', protocol)
            self.assertEqual(category, result['category'])
            self.assertEqual(category == 'framework', result['rescue_harness_allowed'])
            self.assertNotIn('private', json.dumps(result))
        retry = urllib.error.HTTPError('https://example.invalid', 429, '', {'Retry-After': '17'}, None)
        self.assertEqual(1017, classify_failure(retry, self.now)['retry_at'])

    def test_standby_reads_no_accounts_catalog_or_inference_and_unknown_quota_is_ready(self):
        worker = self.worker()
        before = copy.deepcopy(worker.current)
        plan = worker.model_recovery_state()
        self.assertEqual('ready', plan['status'])
        self.assertEqual([], FakeCodex.instances)
        self.assertEqual(before, worker.current)
        self.assertTrue(plan['network_ready_independent'])
        self.assertTrue(all(r['quota_status'] == 'unknown' for r in plan['qualified_routes']))
        self.assertNotIn('auth_home', json.dumps(plan))

    def test_independent_persistent_route_failure_and_reset_reassessment(self):
        original = task()
        plan = self.state.recovery_plan(original, self.config)
        first, second = plan['qualified_routes']
        identity = {k: v for k, v in first.items() if k not in
                    ('quality_basis', 'quota_status', 'availability')}
        self.state.route_failure(identity, CodexError('codex_usage_limit_exceeded'), retry_at=2000)
        self.state = self.governance()
        plan = self.state.recovery_plan(original, self.config)
        self.assertEqual([second['account_id']], [r['account_id'] for r in plan['qualified_routes']])
        self.assertFalse(plan['rescue_harness_allowed'])
        self.assertEqual(2000, plan['next_recheck_at'])
        self.now = 2000
        self.assertEqual(2, len(self.state.recovery_plan(original, self.config)['qualified_routes']))
        self.assertEqual([], FakeCodex.instances)

    def test_unknown_and_owner_pause_hold_only_original_task(self):
        for checkpoint in ({'side_effect_started': True}, {'goal': {'status': 'paused'}},
                {'runtime_failure': {'native_start_attempted': True, 'outcome': 'unknown'}},
                {'native_execution_intent': {'settled': False}}):
            original = task(); original['checkpoint'] = checkpoint
            before = copy.deepcopy(original)
            self.assertEqual('hold_original_operation', self.state.recovery_plan(original, self.config)['status'])
            self.assertEqual(before, original)
            independent = task(); independent['id'] = 'independent-original-task'
            self.assertEqual('ready', self.state.recovery_plan(independent, self.config)['status'])

    def test_bad_account_configuration_does_not_block_other_accounts(self):
        self.config['codex_accounts'][0]['cost_policy'] = 'paid_api'
        plan = self.state.recovery_plan(task(), self.config)
        self.assertEqual('ready', plan['status'])
        self.assertEqual(1, len(plan['qualified_routes']))
        native = self.worker().open_backend()
        self.assertEqual('account-b', native.auth_home)
        native.close()

    def test_reliable_new_quota_observation_and_exit_change_reassess_independently(self):
        plan = self.state.recovery_plan(task(), self.config)
        identity = {k: v for k, v in plan['qualified_routes'][0].items()
                    if k not in ('quality_basis', 'quota_status', 'availability')}
        self.state.route_failure(identity, CodexError('codex_usage_limit_exceeded'), retry_at=2000)
        self.state.observe(identity['route_id'], identity['account_id'],
            {'rateLimits': {'primary': {'usedPercent': 20, 'resetsAt': 2100}}})
        self.assertEqual(2, len(self.state.recovery_plan(task(), self.config)['qualified_routes']))
        self.state.route_failure(identity, TimeoutError(), retry_at=1500)
        self.config['codex_accounts'][0]['egress_id'] = 'changed-observed-exit'
        self.assertEqual(2, len(self.state.recovery_plan(task(), self.config)['qualified_routes']))

    def test_real_worker_routes_advanced_work_to_calibrated_free_in_codex(self):
        self.install_free()
        self.exhaust_openai()

        class ProviderCodex(FakeCodex):
            def __init__(self, config, **options):
                super(ProviderCodex, self).__init__(config, **options)
                self.provider = provider_metadata(config)

        worker = self.worker(ProviderCodex)
        worker.current['context']['operation_id'] = 'original-operation'
        before = copy.deepcopy(worker.current)
        native = worker.open_backend()
        self.assertEqual('codex', worker.harness)
        self.assertEqual('free-fixture', native.config['model_provider'])
        self.assertEqual('model-fixture', native.config['model'])
        self.assertFalse(any(method.startswith('account/') for method, _ in native.calls))
        native.rpc('turn/start', {'threadId': 'original-thread', 'input': []})
        self.assertEqual(('model-fixture', 'high'), (native.calls[-1][1]['model'], native.calls[-1][1]['effort']))
        self.assertNotIn('thread_id', worker.current['checkpoint'])
        self.assertEqual(before['id'], worker.current['id'])
        attempts = self.state.attempts(worker.current['id'])
        self.assertEqual(3, len(attempts))
        self.assertTrue(all(a['operation_id'] == 'original-operation' for a in attempts))
        self.assertTrue(all(not a['native_start_attempted'] for a in attempts[:2]))
        self.assertEqual('unknown', attempts[-1]['outcome'])
        self.assertNotIn('side_effect_started', worker.current['checkpoint'])
        self.assertEqual('route_calibration', self.state.decision(before['id'], before['epoch'])['quality_basis'])
        native.close()

    def test_same_model_name_wrong_effort_or_provider_cannot_inherit_calibration(self):
        route = self.install_free()
        for field in ('provider', 'reasoning_effort', 'model', 'endpoint_id', 'fallback_mode'):
            old = route['quality_evidence']['pair'][field]
            route['quality_evidence']['pair'][field] = 'different'
            self.state = self.governance()
            plan = self.state.recovery_plan(task(), self.config)
            self.assertFalse(any(r['provider'] == 'free-fixture' for r in plan['qualified_routes']))
            self.assertIn('route_quality_not_calibrated', [r['reason'] for r in plan['blocked_routes']])
            route['quality_evidence']['pair'][field] = old

    def test_task_tools_context_intelligence_and_privacy_requirements_are_not_relaxed(self):
        self.install_free()
        requirements = [dict(required_capabilities=['audio']), dict(min_context_tokens=64000),
                        dict(min_intelligence_score=99), dict(data_scope='not-authorized')]
        for needs in requirements:
            original = task(); original['context']['governance']['requirements'] = needs
            plan = self.state.recovery_plan(original, self.config)
            self.assertEqual('waiting_model', plan['status'])
            self.assertFalse(plan['rescue_harness_allowed'])

    def test_existing_frontier_evidence_reused_but_assumed_equivalence_is_not_execution_quality(self):
        route = self.install_free()
        route.pop('quality_evidence')
        route['frontier_candidate_id'] = 'measured-fixture'
        physical = pair('model-fixture', 'high', 'free-fixture', 'fixture-endpoint')
        benchmark = point('measured-fixture', score=70, identity_pair=physical)
        installed = candidate('measured-fixture', identity_pair=physical, policy='free_api')
        installed['privacy']['allowed_scopes'].append('owner_private')
        evidence = dict(schema=1, snapshot=snapshot(benchmark), candidates=[installed], requirements={}, policy={})
        self.config['model_advice_file'] = self.write('evidence.json', evidence)
        self.state = self.governance()
        original = task(); original['context']['governance']['requirements'] = {'min_intelligence_score': 60}
        plan = self.state.recovery_plan(original, self.config)
        free = [r for r in plan['qualified_routes'] if r['provider'] == 'free-fixture']
        self.assertEqual('model_frontier_calibration', free[0]['quality_basis'])
        benchmark['pair'] = dict(physical, endpoint_id='other-endpoint')
        installed['equivalence'] = dict(state='assumed', evidence_ref='synthetic:assumption')
        self.write('evidence.json', evidence)
        plan = self.state.recovery_plan(original, self.config)
        self.assertFalse(any(r['provider'] == 'free-fixture' for r in plan['qualified_routes']))

    def test_account_rpc_failure_classifies_original_protocol_not_framework(self):
        class LimitedCodex(FakeCodex):
            def account(self):
                self.last_protocol_error = {'data': {'status': 429}, 'message': 'private failure detail'}
                raise CodexError('codex_rpc_failed_account_read')
        worker = self.worker(LimitedCodex)
        with self.assertRaises(CodexError):
            worker.open_backend()
        plan = worker.model_recovery_state()
        self.assertEqual('waiting_model', plan['status'])
        self.assertEqual({'capacity'}, {r['category'] for r in plan['blocked_routes']})
        self.assertFalse(plan['rescue_harness_allowed'])
        count = len(FakeCodex.instances)
        with self.assertRaises(ValueError):
            worker.open_backend()
        self.assertEqual(count, len(FakeCodex.instances))
        self.assertNotIn('private', json.dumps(plan))

    def test_original_run_once_keeps_unknown_native_attempt_and_never_tries_second_route(self):
        class NativeFailureCodex(FakeCodex):
            def __enter__(self):
                return self

            def __exit__(self, *unused):
                self.close()

            def start(self, text, checkpoint):
                return self.rpc('turn/start', {'threadId': checkpoint['thread_id'], 'input': []})

            def rpc(self, method, params, timeout=30):
                if method == 'turn/start':
                    self.last_protocol_error = {'data': {'status': 429}}
                    raise CodexError('codex_rpc_failed_turn_start')
                return super(NativeFailureCodex, self).rpc(method, params, timeout)

        class LocalWorker(Worker):
            def tick(self, **unused):
                pass

            def resource_reference(self):
                return {}

        cls = make_worker(LocalWorker, NativeFailureCodex, self.state, scopes=[task()['scope']])
        original = task()
        original['input'] = 'Synthetic original task'
        original['checkpoint']['thread_id'] = 'same-original-thread'
        original['context']['operation_id'] = 'same-original-operation'
        client = Mock()
        client.request.side_effect = lambda path, body: {'task': original} if path == '/v1/claim' else {'ok': True}
        worker = cls(self.config, client=client)
        self.assertTrue(worker.run_once())
        self.assertEqual(1, len(FakeCodex.instances))
        self.assertTrue(original['checkpoint']['runtime_failure']['native_start_attempted'])
        self.assertEqual('unknown', original['checkpoint']['runtime_failure']['outcome'])
        self.assertEqual('same-original-thread', original['checkpoint']['thread_id'])
        self.assertEqual('hold_original_operation', worker.model_recovery_state(original)['status'])
        attempts = self.state.attempts(original['id'])
        self.assertEqual(1, len(attempts))
        self.assertEqual('same-original-operation', attempts[0]['operation_id'])
        self.assertEqual(('capacity', 'unknown'), (attempts[0]['failure']['category'], attempts[0]['outcome']))
        other = task(); other['id'] = 'independent-task'
        self.assertEqual('ready', worker.model_recovery_state(other)['status'])

    def test_only_real_codex_launch_failure_allows_rescue_hint(self):
        class BrokenCodex(FakeCodex):
            def __init__(self, *args, **kwargs):
                raise FileNotFoundError(errno.ENOENT, 'missing executable', '/missing/codex')
        worker = self.worker(BrokenCodex)
        self.config['codex']['executable'] = '/missing/codex'
        with self.assertRaises(FileNotFoundError):
            worker.open_backend()
        plan = worker.model_recovery_state()
        self.assertTrue(plan['rescue_harness_allowed'])
        self.assertEqual({'framework'}, {r['category'] for r in plan['blocked_routes']})
        self.assertFalse(plan['model_outage_switches_harness'])


if __name__ == '__main__':
    unittest.main()
