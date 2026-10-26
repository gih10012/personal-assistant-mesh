"""Opt-in real Worker consumer for qualified Codex routes and local recovery."""
from .leader_governance import GovernanceError
from .model_recovery import native_authorization
from .reasoning import make_codex, read_model_catalog, validate_selection


def make_worker(worker_class, codex_class, governance, scopes=(), profile_loader=None,
                receipt_callback=None):
    """Retain original claim/restore/effect guards and backend decorators.

    Pass the ORIGINAL Codex class. Each safe preflight uses one actual
    model/effort/provider/account/egress binding from the existing account pool.
    Defaults/profiles express preference; all qualified routes remain eligible.
    Model failures neither invoke Pi nor replay a started native operation.
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
            self._governance_enabled = False
            self._governance_binding = self._governance_attempt = None
            self._governance_candidate = None

            def current_profile(agent):
                if not getattr(agent, '_route_binding', None):
                    return profile_loader(agent) if profile_loader is not None else None
                route = agent._route_binding['route']
                return dict(model=route['model'], reasoning_effort=route['effort'],
                            revision=governance.policy['revision'])

            profiled = make_codex(codex_class, current_profile, receipt_callback)

            class QuotaObservedCodex(profiled):
                def __init__(self, native_config, **kwargs):
                    self._route_binding = worker._governance_binding if worker._governance_enabled else None
                    self._route_attempt = worker._governance_attempt
                    self._route_native_started = False
                    self._route_failure_recorded = False
                    super(QuotaObservedCodex, self).__init__(native_config, **kwargs)
                    if self._route_binding:
                        worker._governance_candidate = self

                def account(self):
                    value = super(QuotaObservedCodex, self).account()
                    if (self._route_binding and value.get('type') == 'apiKey'
                            and self._route_binding['route']['cost_basis'] == 'included_subscription'):
                        raise GovernanceError('model_provider_cost_authorization_required')
                    return value

                def rate_limits(self):
                    if not self._route_binding:
                        return super(QuotaObservedCodex, self).rate_limits()
                    provider = getattr(self, 'provider', None)
                    if provider and not provider['requires_openai_auth']:
                        return {'status': 'not_applicable'}
                    try:
                        raw = self.rpc('account/rateLimits/read', {})
                    except (OSError, ValueError):
                        raw = None
                    identity = self._route_binding['identity']
                    self._route_quota = governance.observe(identity['route_id'], identity['account_id'], raw)
                    return self._route_quota

                def rpc(self, method, params, timeout=30):
                    producing = method in ('thread/start', 'thread/resume', 'turn/start',
                                          'turn/steer', 'thread/goal/set')
                    if self._route_binding and producing:
                        self._route_native_started = True
                        governance.update_attempt(self._route_attempt, status='native_started',
                            native_start_attempted=True, outcome='unknown')
                    try:
                        self.last_protocol_error = None
                        result = super(QuotaObservedCodex, self).rpc(method, params, timeout=timeout)
                    except (OSError, ValueError) as exc:
                        if self._route_binding and producing:
                            self._route_failed(exc, 'native_rpc')
                        raise
                    if self._route_binding and producing:
                        thread = result.get('thread', {}) if isinstance(result, dict) else {}
                        turn = result.get('turn', {}) if isinstance(result, dict) else {}
                        governance.update_attempt(self._route_attempt, status='native_accepted',
                            thread_id=thread.get('id') or params.get('threadId'), turn_id=turn.get('id'))
                    return result

                def _route_failed(self, exc, phase):
                    if self._route_failure_recorded:
                        return
                    self._route_failure_recorded = True
                    process = getattr(self, 'process', None)
                    if (str(exc) in ('codex_disconnected', 'codex_native_transport_read_failed')
                            and process is not None and process.poll() not in (None, 0)
                            and getattr(self, '_term_sent', False) is not True):
                        # An owned app-server actually crashed. A provider's
                        # 429/auth/network error alone never grants rescue.
                        phase = 'framework_runtime'
                    failure = governance.route_failure(self._route_binding['identity'], exc,
                        phase=phase, protocol=getattr(self, 'last_protocol_error', None))
                    governance.update_attempt(self._route_attempt, status='failed', failure=failure,
                        native_start_attempted=self._route_native_started,
                        outcome='unknown' if self._route_native_started else 'not_started')
                    worker._publish_model_decision(dict(status='hold_original_operation'
                        if self._route_native_started else 'waiting_model',
                        failure=failure, next_recheck_at=failure['retry_at'],
                        native_start_attempted=self._route_native_started,
                        model_outage_switches_harness=False, automatic_task_replay=False))

                def _consume_finish_event(self, value, replies):
                    if value.get('method') == 'turn/completed':
                        turn = (value.get('params') or {}).get('turn') or {}
                        if turn.get('status') == 'failed':
                            self.last_protocol_error = turn.get('error')
                    return super(QuotaObservedCodex, self)._consume_finish_event(value, replies)

                def start(self, *args, **kwargs):
                    try:
                        return super(QuotaObservedCodex, self).start(*args, **kwargs)
                    except (OSError, ValueError) as exc:
                        if self._route_binding:
                            self._route_failed(exc, 'native_start')
                        raise

                def finish(self, *args, **kwargs):
                    try:
                        result = super(QuotaObservedCodex, self).finish(*args, **kwargs)
                    except (OSError, ValueError) as exc:
                        if self._route_binding:
                            self._route_failed(exc, 'native_finish')
                        raise
                    if self._route_binding:
                        governance.route_available(self._route_binding['identity'])
                        governance.update_attempt(self._route_attempt, status='model_finished',
                            outcome='native_returned')
                    return result

            # EntranceWorker decorators still own closed-failure/full-history.
            super(GovernedWorker, self).__init__(config, backend=QuotaObservedCodex, **options)

        def model_recovery_state(self, task=None):
            """Supervisor-facing local read; never starts account/model RPCs."""
            task = self.current if task is None else task
            if task is None or task.get('scope') not in scopes:
                return dict(status='not_governed', next_recheck_at=None,
                            network_ready_independent=True, automatic_task_replay=False)
            role = profile_loader(None) if profile_loader is not None else None
            return governance.recovery_plan(task, self.config, role)

        def _publish_model_decision(self, value):
            task = self.current
            value = governance.record_decision(task, value)
            self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                'checkpoint': {'task_governance': value}})
            task.setdefault('checkpoint', {})['task_governance'] = value
            return value

        def open_backend(self):
            task = self.current
            self._governance_enabled = task.get('scope') in scopes
            if not self._governance_enabled:
                self._governance_binding = self._governance_attempt = None
                return super(GovernedWorker, self).open_backend()
            role = profile_loader(None) if profile_loader is not None else None
            plan = governance.recovery_plan(task, self.config, role, _bindings=True)
            bindings = plan.pop('_bindings', [])
            if plan['status'] != 'ready':
                self._publish_model_decision(plan)
                if plan['status'] == 'hold_original_operation':
                    raise GovernanceError('governance_original_task_not_safely_startable')
                raise GovernanceError('governance_' + plan['status'])
            governance.task_rating(task, persist=True)
            original_config, last_error = self.config, None
            try:
                for binding in bindings:
                    self._governance_binding = binding
                    self._governance_attempt = governance.begin_attempt(task, binding['identity'])
                    self._governance_candidate = None
                    self.config = dict(original_config, codex=binding['native_config'])
                    self.config.pop('codex_accounts', None)
                    self.config.pop('pi', None)
                    agent = None
                    stage = 'coordination'
                    try:
                        self._publish_model_decision(dict(plan, status='preflight',
                            selected_route=binding['identity'], model_attempt_id=self._governance_attempt))
                        stage = 'framework_start'
                        agent = super(GovernedWorker, self).open_backend()
                        stage = 'model_catalog'
                        # Catalog support is not entitlement/inference proof.
                        validate_selection(read_model_catalog(agent.rpc),
                            binding['route']['model'], binding['route']['effort'])
                        agent._native_route_authorization = native_authorization(
                            task, binding, self._governance_attempt, governance.policy_hash,
                            factory=getattr(agent, 'make_route_authorization', None))
                        governance.route_available(binding['identity'])
                        governance.update_attempt(self._governance_attempt, status='backend_selected')
                        stage = 'coordination'
                        self._publish_model_decision(dict(plan, status='backend_selected',
                            selected_route=binding['identity'], quality_basis=binding['quality_basis'],
                            route=binding['route'], policy_revision=governance.policy['revision'],
                            selection_basis='owner_role_profile' if role and role['model'] == binding['route']['model']
                                and role['reasoning_effort'] == binding['route']['effort'] else 'qualified_route_preference',
                            model_attempt_id=self._governance_attempt))
                        return agent
                    except (OSError, ValueError) as exc:
                        # Only preflight may try another binding. The original
                        # start/finish path retains every unknown-effect guard.
                        if agent is not None:
                            agent.close()
                        if stage == 'coordination':
                            raise
                        observed_agent = agent or self._governance_candidate
                        quota = getattr(observed_agent, '_route_quota', None)
                        diagnostics = task.get('checkpoint', {}).get('backend_candidates') or []
                        if quota is None and diagnostics and diagnostics[-1].get('retry_at'):
                            quota = {'retry_at': diagnostics[-1]['retry_at']}
                        if diagnostics and diagnostics[-1].get('cause') == 'account_rpc':
                            stage = 'account_rpc'
                        if (stage == 'framework_start' and isinstance(exc, (FileNotFoundError, PermissionError))
                                and getattr(exc, 'filename', None) != binding['native_config'].get('executable', 'codex')):
                            stage = 'provider_preflight'
                        failure = governance.route_failure(binding['identity'], exc, phase=stage,
                            protocol=getattr(observed_agent, 'last_protocol_error', None),
                            retry_at=(quota or {}).get('retry_at'))
                        governance.update_attempt(self._governance_attempt, status='failed_preflight',
                            failure=failure, outcome='not_started')
                        last_error = exc
                final = governance.recovery_plan(task, original_config, role)
                quota_only = final['blocked_routes'] and all(r.get('category') == 'quota'
                    for r in final['blocked_routes'])
                if quota_only:
                    final.update(status='waiting_quota', next_safe_retry=final['next_recheck_at'])
                self._publish_model_decision(final)
                raise last_error
            finally:
                self.config = original_config

    return GovernedWorker

