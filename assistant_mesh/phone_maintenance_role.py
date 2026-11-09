"""Exact-device D1 backend composition for the incumbent claimed Worker.

No claims, processes, models, configuration reads or ledger opens on import.
Root installs this in a pinned additive release and activates its ordinary
rolling runner. Other scopes retain the original composed Worker unchanged.
"""
import copy
import hashlib
import json


DIAGNOSTICS = frozenset(('signed_data_sync', 'receipt_reconciliation', 'node_connection'))
REPAIRS = frozenset(('diagnose', 'recommend_signed_data_sync', 'reconcile_original_receipts'))


class RoleHold(ValueError):
    pass


def validate_settings(value):
    fields = {'format', 'scope', 'workspace', 'profile_path', 'governance_database', 'governance_policy'}
    if (not isinstance(value, dict) or set(value) != fields or value['format'] != 'amita-phone-maintenance-role/1'
            or not isinstance(value['scope'], str) or not value['scope'].startswith('phone-maintenance:')
            or len(value['scope']) != len('phone-maintenance:') + 40
            or any(c not in '0123456789abcdef' for c in value['scope'].split(':', 1)[1])
            or any(not isinstance(value[k], str) or not value[k].startswith('/') or '/..' in value[k]
                   for k in fields - {'format', 'scope'})):
        raise ValueError('phone_role_owner_settings_invalid')
    return dict(value)


def guard_task(task, scope, rating):
    context, cp = task.get('context') or {}, task.get('checkpoint') or {}
    maintenance, origin = context.get('phone_maintenance') or {}, context.get('origin') or {}
    node = maintenance.get('node_id')
    if (task.get('scope') != scope or not isinstance(node, str)
            or 'phone-maintenance:' + hashlib.sha256(node.encode()).hexdigest()[:40] != scope
            or context.get('session_scope') != scope or context.get('memory_scope') != scope
            or origin.get('kind') != 'node-maintenance' or origin.get('peer') != node
            or context.get('project_id') != 'phone-maintenance'
            or set(task.get('required') or []) != {'agent', 'phone-maintenance'}
            or maintenance.get('snapshot', {}).get('diagnostic') not in DIAGNOSTICS
            or not set(maintenance.get('allowed_repairs') or []) <= REPAIRS
            or maintenance.get('operator_granted') is not False):
        raise RoleHold('phone_role_exact_device_diagnostic_required')
    if rating.get('difficulty') != 'D1' or rating.get('risk') != 'low' or context.get('goal'):
        raise RoleHold('phone_role_escalate_leader')
    if (task.get('status') in ('paused', 'needs_review', 'completed', 'failed', 'cancelled')
            or cp.get('side_effect_started') is True
            or (cp.get('runtime_failure') or {}).get('native_start_attempted') is True
               and (cp.get('runtime_failure') or {}).get('outcome') == 'unknown'
            or (cp.get('native_execution_intent') and cp['native_execution_intent'].get('settled') is not True)):
        raise RoleHold('phone_role_original_operation_hold')


def resume_identity(task):
    """Use only authority checkpoint/session native configuration receipts."""
    cp = task.get('checkpoint') or {}
    session = (task.get('sessions') or {}).get('codex') or task.get('session')
    states = [cp]
    if isinstance(session, dict):
        if session.get('harness') != 'codex':
            raise RoleHold('phone_role_original_native_harness_hold')
        states.append(session.get('state') or {})
    existing = [s for s in states if s.get('thread_id')]
    if not existing:
        return None
    state = existing[0]
    lifecycle = state.get('native_lifecycle') or {}
    selection = lifecycle.get('model_route_selection') or {}
    if (state.get('side_effect_started') is not False or lifecycle.get('settled') is not True
            or lifecycle.get('runtime_closed') is not True
            or lifecycle.get('thread_id') != state['thread_id']
            or any(not isinstance(selection.get(k), str) or not selection[k]
                   for k in ('model', 'provider', 'reasoning_effort'))
            or state.get('codex_provider_identity') is None):
        raise RoleHold('phone_role_original_native_configuration_unverified')
    # Conflicting task and shared-scope sessions are retained, never rewritten.
    for other in existing[1:]:
        pair = (other.get('native_lifecycle') or {}).get('model_route_selection') or {}
        if other['thread_id'] != state['thread_id'] or pair != selection:
            raise RoleHold('phone_role_original_native_session_conflict')
    return dict(thread_id=state['thread_id'], model=selection['model'], provider=selection['provider'],
                effort=selection['reasoning_effort'], provider_identity=state['codex_provider_identity'])


def filter_plan(plan, original):
    if original is None:
        return plan
    def matches(identity):
        return (identity.get('model') == original['model'] and identity.get('effort') == original['effort']
                and identity.get('provider') == original['provider']
                and identity.get('provider_identity') == original['provider_identity'])
    result = dict(plan)
    result['qualified_routes'] = [r for r in plan.get('qualified_routes', []) if matches(r)]
    if '_bindings' in result:
        result['_bindings'] = [b for b in plan['_bindings'] if matches(b['identity'])]
    if not result['qualified_routes']:
        result.update(status='hold_original_operation', reason='phone_role_original_model_effort_unavailable')
    return result


def guard_resume(params, original):
    if original is None or params.get('threadId') != original['thread_id']:
        raise RoleHold('phone_role_original_thread_required')
    if (params.get('model', original['model']) != original['model']
            or params.get('modelProvider', original['provider']) != original['provider']
            or (params.get('config') or {}).get('model_reasoning_effort', original['effort']) != original['effort']):
        raise RoleHold('phone_role_original_model_effort_hold')
    result = copy.deepcopy(params)
    # Never use thread/resume to modify a persisted native model or effort.
    result.pop('model', None); result.pop('modelProvider', None)
    config = result.get('config')
    if isinstance(config, dict):
        config.pop('model_reasoning_effort', None)
        if not config: result.pop('config', None)
    return result


def scoped_governance(governance_class, scope):
    class PhoneGovernance(governance_class):
        def recovery_plan(self, task, config, role_profile=None, _bindings=False):
            try:
                guard_task(task, scope, self.task_rating(task))
                original = resume_identity(task)
            except (RoleHold, ValueError) as error:
                return dict(status='hold_original_operation', reason=str(error), qualified_routes=[],
                            blocked_routes=[], next_recheck_at=None, _bindings=[])
            plan = super(PhoneGovernance, self).recovery_plan(task, config, role_profile, _bindings)
            return filter_plan(plan, original)
    return PhoneGovernance


def install(incumbent, modern_codex, settings, private_reader, governance_class, governed_factory):
    """Route ONLY backend construction; the original Strategy still executes.

    A backend-only facade shares the original claimed task, Client and callback
    owner. It never calls run_once/claim/heartbeat or starts a second Worker loop.
    """
    selected = validate_settings(settings)
    original_factory, modern_worker = incumbent.make_worker, incumbent.Worker
    if getattr(incumbent, '_phone_maintenance_role_installed', False):
        raise ValueError('phone_role_already_installed')
    scope = selected['scope']

    PhoneGovernance = scoped_governance(governance_class, scope)

    def factory(*args, **kwargs):
        legacy = original_factory(*args, **kwargs)
        class RoutedWorker(legacy):
            def open_backend(self):
                task = self.current
                if not isinstance(task, dict) or task.get('scope') != scope:
                    return super(RoutedWorker, self).open_backend()
                governance = PhoneGovernance(selected['governance_database'], private_reader(selected['governance_policy']))
                try:
                    guard_task(task, scope, governance.task_rating(task))
                    original = resume_identity(task)
                except (RoleHold, ValueError) as error:
                    self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                        'checkpoint': {'phone_maintenance_role': {'status': 'hold', 'reason': str(error),
                                                                 'leader_review_required': True}}})
                    raise
                callback_owner = self
                class PhoneCodex(modern_codex):
                    def rpc(self, method, params, timeout=30):
                        if method == 'thread/resume':
                            params = guard_resume(params, original)
                        return super(PhoneCodex, self).rpc(method, params, timeout=timeout)
                preference = lambda agent: private_reader(selected['profile_path'])
                executor_class = governed_factory(modern_worker, PhoneCodex, governance,
                    scopes=(scope,), profile_loader=preference)
                config = dict(self.config, codex=dict(self.config['codex'], workspace=selected['workspace']))
                config['codex'].pop('goal', None)
                if config.get('codex_accounts') is not None:
                    config['codex_accounts'] = [dict(a, workspace=selected['workspace']) for a in config['codex_accounts']]
                    for account in config['codex_accounts']: account.pop('goal', None)
                executor = executor_class(config, client=self.client, config_path=self.config_path)
                executor.current = task
                # All native activity/interaction and tool callbacks belong to
                # the original Strategy runner and its original claim fences.
                for name in ('on_tool', 'on_activity', 'on_interaction', 'tick'):
                    setattr(executor, name, getattr(callback_owner, name))
                agent = executor.open_backend()
                self.harness = executor.harness
                self._phone_backend = executor
                return agent
        return RoutedWorker
    incumbent.make_worker = factory
    incumbent._phone_maintenance_role_installed = True
    return {'scope': scope, 'installed': True, 'model_called': False, 'second_claim': False}
