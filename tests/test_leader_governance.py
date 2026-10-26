import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from assistant_mesh.codex import CodexError
from assistant_mesh.governance_worker import make_worker
from assistant_mesh.leader_governance import Governance, GovernanceError, quota_snapshot
from assistant_mesh.worker import Worker


POLICY = {'revision': 'unit-owner-v1', 'defaults': {'D1': 'ordinary', 'D2': 'ordinary', 'D3': 'strong'},
    'routes': [
        {'id': 'ordinary', 'model': 'gpt-6-luna', 'effort': 'max', 'harness': 'codex',
         'quality': 'standard', 'cost_basis': 'included_subscription', 'difficulties': ['D1', 'D2']},
        {'id': 'strong', 'model': 'gpt-6.1-sol', 'effort': 'high', 'harness': 'codex',
         'quality': 'strong', 'cost_basis': 'included_subscription', 'difficulties': ['D3']}]}


def rating(difficulty='D3', **extra):
    return dict({'difficulty': difficulty, 'rationale': 'bounded unit fixture', 'risk': 'high', 'priority': 7}, **extra)


def task(difficulty='D3'):
    return {'id': 'original-task', 'epoch': 3, 'status': 'running', 'checkpoint': {},
            'scope': 'leader:project:personal-assistant-workspace',
            'context': {'governance': rating(difficulty)}}


class FakeCodex:
    instances = []
    quotas = {}

    def __init__(self, config, **unused):
        self.config, self.auth_home, self.model = config, config['auth_home'], config['model']
        self.calls, self.closed = [], False
        self.__class__.instances.append(self)

    def account(self):
        return {'authenticated': True}

    def rate_limits(self):
        return {'status': 'unknown'}

    def rpc(self, method, params, timeout=30):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'account/rateLimits/read':
            return self.quotas.get(self.auth_home)
        if method == 'model/list':
            return {'data': [{'id': self.model, 'supportedReasoningEfforts': [
                {'reasoningEffort': 'high'}, {'reasoningEffort': 'max'}]}]}
        if method == 'turn/start':
            return {'turn': {'id': 'fake-turn-no-model-call'}}
        raise AssertionError(method)

    def close(self):
        self.closed = True


class LeaderGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = 1000
        self.state = Governance(str(Path(self.temporary.name) / 'governance.sqlite'), POLICY,
                                clock=lambda: self.now)
        FakeCodex.instances, FakeCodex.quotas = [], {}

    def worker(self):
        cls = make_worker(Worker, FakeCodex, self.state, scopes=[task()['scope']])
        worker = cls({'codex': {'workspace': '/not-used', 'auth_home': 'account-a'},
                      'codex_accounts': [{'auth_home': 'account-a'}, {'auth_home': 'account-b'}],
                      'pi': {'must_not_initialize': True}}, client=Mock())
        worker.current = task()
        return worker

    def test_rating_required_and_advanced_floor_survive_route_changes(self):
        self.assertEqual('max', self.state.choose(task('D1'))['route']['effort'])
        other = task(); other['id'] = 'different-task'
        self.assertEqual('gpt-6.1-sol', self.state.choose(other)['route']['model'])
        for route in ({'quality': 'standard'},):
            policy = copy.deepcopy(POLICY); policy['routes'][1].update(route)
            state = Governance(str(Path(self.temporary.name) / 'other.sqlite'), policy)
            with self.assertRaisesRegex(GovernanceError, 'advanced_quality_floor'):
                state.choose(task())
        ungraded = task(); ungraded['context'] = {}
        ungraded['id'] = 'ungraded'
        with self.assertRaisesRegex(GovernanceError, 'task_rating_required'):
            self.state.choose(ungraded)

    def test_unknown_paused_and_effects_cannot_become_fresh_work(self):
        for changes in ({'status': 'paused'}, {'status': 'needs_review'},
                        {'checkpoint': {'side_effect_started': True}},
                        {'checkpoint': {'goal': {'status': 'paused'}}},
                        {'checkpoint': {'runtime_failure': {'native_start_attempted': True, 'outcome': 'unknown'}}}):
            value = task(); value.update(changes)
            with self.assertRaisesRegex(GovernanceError, 'not_safely_startable'):
                self.state.choose(value)
            self.assertEqual(changes, {k: value[k] for k in changes})

    def test_quota_windows_are_observed_not_invented_or_redeemed(self):
        raw = {'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 20,
            'windowDurationMins': 300, 'resetsAt': 2000}, 'secondary': {'usedPercent': 100,
            'windowDurationMins': 10080, 'resetsAt': 3000}}}, 'rateLimitResetCredits': {'availableCount': 2}}
        snap = quota_snapshot(raw, self.now)
        self.assertEqual(('exhausted', 3000), (snap['status'], snap['retry_at']))
        self.assertEqual([False, True], [w['weekly'] for w in snap['windows']])
        self.assertNotIn('rateLimitResetCredits', str(snap))
        for unknown in (None, {}, {'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': 999}}},
                        {'rateLimitsByLimitId': {'other': raw['rateLimitsByLimitId']['codex']}}):
            self.assertEqual('unknown', quota_snapshot(unknown, self.now)['status'])

    def test_real_worker_consumer_keeps_quality_and_uses_native_parameter_seam(self):
        FakeCodex.quotas = {'account-a': {'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': 2000}}}}
        worker = self.worker()
        original = worker.config
        native = worker.open_backend()
        self.assertTrue(FakeCodex.instances[0].closed)
        self.assertEqual('gpt-6.1-sol', native.config['model'])
        self.assertIs(original, worker.config)
        native.rpc('turn/start', {'threadId': 'same-thread', 'input': [],
                   'collaborationMode': {'settings': {'model': 'weaker-default'}}})
        sent = native.calls[-1][1]
        self.assertEqual(('gpt-6.1-sol', 'high'), (sent['model'], sent['effort']))
        self.assertEqual('high', sent['collaborationMode']['settings']['reasoning_effort'])
        self.assertEqual('same-thread', sent['threadId'])
        self.assertEqual('backend_selected', self.state.decision('original-task', 3)['status'])
        self.assertEqual(1, len([c for c in native.calls if c[0] == 'account/rateLimits/read']))

    def test_exhaustion_waits_without_free_pi_or_started_turn(self):
        for name, reset in [('account-a', 2000), ('account-b', 3000)]:
            FakeCodex.quotas[name] = {'rateLimits': {'secondary': {'usedPercent': 100,
                'windowDurationMins': 10080, 'resetsAt': reset}}}
        worker = self.worker()
        with self.assertRaisesRegex(CodexError, 'usage_limit_exceeded'):
            worker.open_backend()
        decision = self.state.decision('original-task', 3)
        self.assertEqual(('waiting_quota', 2000), (decision['status'], decision['next_safe_retry']))
        self.assertEqual(2000, decision['next_recheck_at'])
        self.assertEqual(2, len(FakeCodex.instances))
        self.assertTrue(all(n.closed for n in FakeCodex.instances))
        self.assertFalse(any(c[0] == 'turn/start' for n in FakeCodex.instances for c in n.calls))
        self.assertNotIn('side_effect_started', worker.current['checkpoint'])

    def test_original_constructor_backend_decorator_is_retained(self):
        class DecoratedWorker(Worker):
            def __init__(self, config, backend, **options):
                class PreservedFailureBackend(backend):
                    original_failure_wrapper = True
                super(DecoratedWorker, self).__init__(config, backend=PreservedFailureBackend, **options)
        cls = make_worker(DecoratedWorker, FakeCodex, self.state, scopes=[task()['scope']])
        worker = cls({'codex': {'workspace': '/not-used', 'auth_home': 'only-account'}}, client=Mock())
        worker.current = task()
        native = worker.open_backend()
        self.assertTrue(native.original_failure_wrapper)
        native.close()

    def test_old_unrated_scope_preserves_existing_role_profile(self):
        profile = {'model': 'gpt-6.1-sol', 'reasoning_effort': 'max', 'revision': 4}
        cls = make_worker(Worker, FakeCodex, self.state, scopes=[task()['scope']],
                          profile_loader=lambda agent: profile)
        worker = cls({'codex': {'workspace': '/not-used', 'auth_home': 'only-account',
                               'model': 'gpt-6.1-sol'}}, client=Mock())
        worker.current = task(); worker.current['scope'] = 'original-unrated-owner-scope'
        worker.current['context'] = {}
        native = worker.open_backend()
        native.rpc('turn/start', {'threadId': 'original-owner-thread', 'input': []})
        self.assertEqual('max', native.calls[-1][1]['effort'])
        self.assertIsNone(self.state.decision('original-task', 3))

    def test_qualified_profile_is_preferred_and_weaker_profile_does_not_gate_routes(self):
        policy = copy.deepcopy(POLICY)
        policy['routes'].append(dict(policy['routes'][1], id='strong-max', effort='max'))
        self.state = Governance(str(Path(self.temporary.name) / 'profile.sqlite'), policy, clock=lambda: self.now)
        profile = {'model': 'gpt-6.1-sol', 'reasoning_effort': 'max', 'revision': 4}
        cls = make_worker(Worker, FakeCodex, self.state, scopes=[task()['scope']],
                          profile_loader=lambda agent: profile)
        worker = cls({'codex': {'workspace': '/not-used', 'auth_home': 'only-account'}}, client=Mock())
        worker.current = task()
        native = worker.open_backend()
        native.rpc('turn/start', {'threadId': 'same-thread', 'input': []})
        self.assertEqual('max', native.calls[-1][1]['effort'])
        self.assertEqual('owner_role_profile', self.state.decision('original-task', 3)['selection_basis'])
        profile.update(model='gpt-6-luna', reasoning_effort='max')
        worker.current = task(); worker.current['epoch'] = 4
        count = len(FakeCodex.instances)
        native = worker.open_backend()
        self.assertEqual(count + 1, len(FakeCodex.instances))
        self.assertEqual('gpt-6.1-sol', native.config['model'])
        native.close()

    def test_weekly_review_queue_is_idempotent_cancellable_and_never_execution_authority(self):
        value = {'project_id': 'project', 'task_id': 'original-task', 'kind': 'integration',
                 'code_revision': 'revision', 'operation_id': 'original-operation', 'required_nodes': ['node'],
                 'model': 'qualified-model', 'owner_cooperation': False, 'quota_budget': 'within existing quota'}
        self.state.enqueue_review('review', value)
        self.assertTrue(self.state.enqueue_review('review', value)['replayed'])
        self.state.observe('strong', 'opaque-account', {'rateLimits': {'secondary': {
            'usedPercent': 5, 'windowDurationMins': 10080, 'resetsAt': 200000}}})
        no_resources = self.state.schedule_reviews('leader', False)
        self.assertEqual(0, no_resources['scheduled'])
        scheduled = self.state.schedule_reviews('leader', True)
        self.assertEqual(113600, scheduled['scheduled_at'])
        self.assertFalse(scheduled['execution_authorized'])
        self.assertEqual('scheduled', self.state.reviews()[0]['status'])
        self.assertEqual('leader', self.state.reviews()[0]['plan']['leader_id'])
        self.assertEqual('available', self.state.quotas()[0]['current_status'])
        self.now += 301
        self.assertEqual('unknown', self.state.quotas()[0]['current_status'])
        self.assertTrue(self.state.cancel_review('review')['cancelled'])
        self.assertEqual(0, self.state.schedule_reviews('leader', True)['scheduled'])


if __name__ == '__main__':
    unittest.main()
