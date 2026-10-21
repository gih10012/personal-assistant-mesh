"""Opt-in consumer for an unchanged frozen Worker; no daemon or model calls on import."""
import hashlib

from .leader_governance import GovernanceError
from .reasoning import make_codex


def make_worker(worker_class, codex_class, governance):
    """Use the original Worker claim/restore/finish/effect guards unchanged.

Pass the ORIGINAL frozen Codex class, not an already profiled class: governance
selects a qualified per-task model/effort rather than letting a role default
silently override a D3 quality floor. Its model/list validation is unchanged.
"""
    class GovernedWorker(worker_class):
        def __init__(self, config, **options):
            if 'backend' in options:
                raise GovernanceError('governance_original_codex_class_required')
            worker = self
            self._governance_profile, self._governance_choice = None, None
            self._quota_observed = []
            profiled = make_codex(codex_class, lambda agent: worker._governance_profile)

            class QuotaObservedCodex(profiled):
                def __init__(self, native_config, **kwargs):
                    # Account overrides cannot weaken this task's route.
                    if worker._governance_choice is None:
                        raise GovernanceError('governance_preflight_choice_required')
                    route = worker._governance_choice['route']
                    native_config = dict(native_config, model=route['model'])
                    super(QuotaObservedCodex, self).__init__(native_config, **kwargs)

                def rate_limits(self):
                    try:
                        raw = self.rpc('account/rateLimits/read', {})
                    except (OSError, ValueError):
                        raw = None
                    route = worker._governance_choice['route']
                    account_id = hashlib.sha256(str(self.auth_home).encode()).hexdigest()
                    snapshot = governance.observe(route['id'], account_id, raw)
                    worker._quota_observed.append(snapshot)
                    return snapshot

            # Let the original constructor retain its own backend decorators,
            # notably EntranceWorker's closed-failure/full-history preservation.
            super(GovernedWorker, self).__init__(config, backend=QuotaObservedCodex, **options)

        def open_backend(self):
            task = self.current
            self._quota_observed = []
            observed = self._quota_observed
            try:
                choice = governance.choose(task)
            except GovernanceError as exc:
                value = governance.record_decision(task, {'status': 'waiting_policy', 'reason': str(exc)})
                self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                    'checkpoint': {'task_governance': value}})
                raise
            route = choice['route']
            self._governance_choice = choice
            self._governance_profile = {'model': route['model'], 'reasoning_effort': route['effort'],
                                        'revision': choice['policy_revision']}
            original_config = self.config
            self.config = dict(self.config)
            self.config.pop('pi', None)
            agent = None
            try:
                value = governance.record_decision(task, dict(choice, status='preflight'))
                self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                    'checkpoint': {'task_governance': value}})
                agent = super(GovernedWorker, self).open_backend()
                value = governance.record_decision(task, dict(choice, status='backend_selected',
                    quota_observations=observed, actual_model_effort_verified=False))
                self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                    'checkpoint': {'task_governance': value}})
                task['checkpoint']['task_governance'] = value
                return agent
            except (OSError, ValueError):
                if agent is not None:
                    agent.close()  # A failed fenced checkpoint must not leak an unstarted runtime.
                count = len(self.config.get('codex_accounts') or [self.config['codex']])
                exhausted = len(observed) == count and all(s['status'] == 'exhausted' for s in observed)
                value = dict(choice, status='waiting_quota' if exhausted else 'waiting_backend',
                    quota_observations=observed, next_safe_retry=(min(s['retry_at'] for s in observed)
                        if exhausted else None), authority_deadline_hook_required=True)
                value = governance.record_decision(task, value)
                self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                    'checkpoint': {'task_governance': value}})
                task['checkpoint']['task_governance'] = value
                raise
            finally:
                self.config = original_config

    return GovernedWorker
