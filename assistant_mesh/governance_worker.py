"""Opt-in consumer for an unchanged frozen Worker; no daemon or model calls on import."""
import hashlib

from .leader_governance import GovernanceError
from .reasoning import make_codex


def make_worker(worker_class, codex_class, governance, scopes=(), profile_loader=None,
                receipt_callback=None):
    """Use the original Worker claim/restore/finish/effect guards unchanged.

Pass the ORIGINAL frozen Codex class, not an already profiled class: governance
    selects a qualified per-task model/effort while retaining an owner role
    profile when it fits that floor. Empty scopes disables governance; old
    scopes retain the profile/normal backend behavior and are not reclassified.
"""
    if isinstance(scopes, str):
        raise GovernanceError('governance_scope_allowlist_required')
    scopes = frozenset(scopes)
    if (not all(isinstance(scope, str) and scope for scope in scopes)
            or (profile_loader is not None and not callable(profile_loader))
            or (receipt_callback is not None and not callable(receipt_callback))):
        raise GovernanceError('governance_scope_or_profile_loader_invalid')

    class GovernedWorker(worker_class):
        def __init__(self, config, **options):
            if 'backend' in options:
                raise GovernanceError('governance_original_codex_class_required')
            worker = self
            self._governance_profile, self._governance_choice = None, None
            self._governance_enabled = False
            self._quota_observed = []

            def current_profile(agent):
                role = profile_loader(agent) if profile_loader is not None else None
                if not worker._governance_enabled:
                    return role
                route = governance.select_route(worker._governance_choice['rating'], role)
                if (route['model'], route['effort']) != (worker._governance_profile['model'],
                                                        worker._governance_profile['reasoning_effort']):
                    raise GovernanceError('governance_profile_changed_after_preflight')
                return worker._governance_profile

            profiled = make_codex(codex_class, current_profile, receipt_callback)

            class QuotaObservedCodex(profiled):
                def __init__(self, native_config, **kwargs):
                    # Account overrides cannot weaken this task's route.
                    if worker._governance_enabled and worker._governance_choice is None:
                        raise GovernanceError('governance_preflight_choice_required')
                    if worker._governance_enabled:
                        route = worker._governance_choice['route']
                        native_config = dict(native_config, model=route['model'])
                    super(QuotaObservedCodex, self).__init__(native_config, **kwargs)

                def rate_limits(self):
                    if not worker._governance_enabled:
                        return super(QuotaObservedCodex, self).rate_limits()
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
            self._governance_enabled = task.get('scope') in scopes
            if not self._governance_enabled:
                self._governance_choice, self._governance_profile = None, None
                return super(GovernedWorker, self).open_backend()
            self._quota_observed = []
            observed = self._quota_observed
            try:
                choice = governance.choose(task, profile_loader(None) if profile_loader is not None else None)
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
