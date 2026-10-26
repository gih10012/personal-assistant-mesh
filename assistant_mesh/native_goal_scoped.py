"""Owner-scoped native Goal assembly; no daemon, model or state on import.

The builder supplies the pinned, isolated ``native_goal_runtime`` components.
Frozen legacy members remain the base; only an explicit claimed-task seam is
added to Worker. Existing governance/profile/failure wrappers stay outside the
backend selector, and diagnostics may wrap the resulting Worker last.
"""
import copy
import hashlib
import json
import math
import threading

from .store import Conflict


FORMAT = 'mesh-native-goal-scopes/1'
CAPABILITY = 'owner.native-goal.lifecycle'
FLAG = '_mesh_native_goal_scope'


def _text(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value.encode('utf8')) > 256 or '*' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('native_goal_scope_identity_invalid')
    return value


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False)


def validate_policy(value):
    if (not isinstance(value, dict) or set(value) != {
            'format', 'enabled', 'revision', 'authority', 'owner_id',
            'scopes', 'executors', 'activated_at'} or value['format'] != FORMAT
            or type(value['enabled']) is not bool or type(value['revision']) is not int
            or value['revision'] < 1):
        raise ValueError('native_goal_scope_owner_policy_required')
    result = json.loads(_encoded(value))
    for key in ('authority', 'owner_id'):
        _text(result[key])
    for key in ('scopes', 'executors'):
        items = result[key]
        if (not isinstance(items, list) or not 1 <= len(items) <= 64
                or any(_text(item) != item for item in items) or len(set(items)) != len(items)):
            raise ValueError('native_goal_scope_owner_policy_required')
        result[key] = sorted(items)
    moment = result['activated_at']
    if type(moment) not in (int, float) or not math.isfinite(moment) or moment < 0:
        raise ValueError('native_goal_scope_activation_invalid')
    return result


def _profile(policy, scope, activated_at):
    return {'format': FORMAT, 'authority': policy['authority'], 'owner_id': policy['owner_id'],
            'scope': scope, 'activated_at': activated_at}


def make_backend(legacy_class, goal_class):
    """Rebase existing outer profile/failure decorators on a conditional base.

    Build governance/Entrance decorators around THIS class, not the reverse:
    wrapping an already assembled backend with goal_class would skip its init
    and closed-failure/profile MRO. Each instance fixes its selection once.
    """
    class ScopedBackend(legacy_class):
        _native_goal_backend_version = 1

        def __init__(self, config, *args, **kwargs):
            flag = config.get(FLAG, False)
            if type(flag) is not bool:
                raise ValueError('native_goal_scope_backend_selection_invalid')
            self._native_goal_selected = flag
            selected = dict(config)
            selected.pop(FLAG, None)
            if flag and (selected.get('native_transport') != 'unix'
                         or selected.get('native_goal_drain') is not True
                         or selected.get('native_goal_yield') is not True):
                raise ValueError('native_goal_scope_complete_runtime_required')
            target = goal_class if flag else legacy_class
            target.__init__(self, selected, *args, **kwargs)

    def method(name, goal_method, old_method):
        def call(self, *args, **kwargs):
            if self._native_goal_selected:
                return goal_method(self, *args, **kwargs)
            if old_method is None:
                raise AttributeError(name)
            return old_method(self, *args, **kwargs)
        call.__name__ = name
        return call

    for name, value in goal_class.__dict__.items():
        if name == '__init__' or not callable(value):
            continue
        setattr(ScopedBackend, name, method(name, value, getattr(legacy_class, name, None)))
    return ScopedBackend


def make_worker(existing_worker, backend_selector, strategy, policy):
    """Wrap the ALREADY governed/Entrance Worker, with a single real claim."""
    policy = validate_policy(policy)
    if not policy['enabled']:
        return existing_worker
    if getattr(existing_worker, '_native_goal_claim_seam_version', None) != 1:
        raise ValueError('native_goal_scope_claimed_task_seam_required')

    class ScopedWorker(existing_worker):
        def __init__(self, *args, **kwargs):
            super(ScopedWorker, self).__init__(*args, **kwargs)
            if not isinstance(self.backend, type) or not issubclass(self.backend, backend_selector):
                raise ValueError('native_goal_scope_backend_decorator_order_required')
            self._native_goal_selected = False
            self._native_starting, self._native_turn_live = False, None

        def run_claimed(self, task):
            binding = task.get('native_goal_profile')
            selected = task.get('scope') in policy['scopes']
            if not selected:
                if binding is not None:
                    raise ValueError('native_goal_scope_disabled_no_legacy_fallback')
                return super(ScopedWorker, self).run_claimed(task)
            if not isinstance(binding, dict):
                raise ValueError('native_goal_scope_original_claim_required')
            expected = _profile(policy, task['scope'], binding.get('activated_at'))
            if (binding != expected
                    or type(task.get('created')) not in (int, float)
                    or not math.isfinite(task['created'])
                    or task['created'] < binding['activated_at']
                    or task.get('checkpoint', {}).get('side_effect_started')):
                raise ValueError('native_goal_scope_original_claim_required')
            self._native_goal_selected = True
            task.setdefault('checkpoint', {})['native_goal_profile'] = copy.deepcopy(binding)
            try:
                return strategy.run_claimed(self, task)
            finally:
                self._native_goal_selected = False

        def open_backend(self):
            if not self._native_goal_selected:
                return super(ScopedWorker, self).open_backend()
            previous = self.config
            options = {FLAG: True, 'native_transport': 'unix',
                       'native_goal_drain': True, 'native_goal_yield': True}
            self.config = dict(previous, codex=dict(previous['codex'], **options))
            if previous.get('codex_accounts') is not None:
                self.config['codex_accounts'] = [dict(item, **options)
                                                 for item in previous['codex_accounts']]
            # The selected task's qualified Codex route may wait for quota, but
            # cannot silently enter a Pi/free/lower-quality runtime instead.
            self.config.pop('pi', None)
            try:
                agent = super(ScopedWorker, self).open_backend()
                if self.harness != 'codex' or getattr(agent, '_native_goal_selected', None) is not True:
                    agent.close()
                    raise ValueError('native_goal_scope_complete_runtime_required')
                return agent
            finally:
                self.config = previous

        def on_activity(self, kind, value):
            if self._native_goal_selected:
                return strategy.on_activity(self, kind, value)
            return super(ScopedWorker, self).on_activity(kind, value)

        def tick(self, force=False, require_ack=False):
            if self._native_goal_selected:
                return strategy.tick(self, force=force, require_ack=require_ack or force)
            return super(ScopedWorker, self).tick(force=force)

        def _native_yield_tick(self, force=False):
            return strategy._native_yield_tick(self, force=force)

        def _checked_native_yield_seal(self, agent, drained, value):
            return strategy._checked_native_yield_seal(self, agent, drained, value)

        def on_tool(self, call):
            result = super(ScopedWorker, self).on_tool(call)
            arguments = call.get('arguments', {})
            wait = call.get('tool') == 'mesh_wait_children' or (
                call.get('tool') == 'mesh' and isinstance(arguments, dict)
                and arguments.get('action') == 'wait_children')
            if (self._native_goal_selected and wait
                    and getattr(self.agent, 'native_drain_active', False) is not True):
                items = result.get('contentItems', []) if isinstance(result, dict) else []
                try:
                    output = json.loads(items[0]['text'])
                except (IndexError, KeyError, TypeError, ValueError):
                    output = None
                self.wait_children = not (isinstance(output, dict)
                                          and output.get('continue_after_children') is False)
                self.current['checkpoint']['wait_children_requested'] = self.wait_children
            return result

    return ScopedWorker


def assemble_worker(original_worker, original_codex, policy, *, governance_factory,
                    governance, governance_scopes, profile_loader=None,
                    receipt_callback=None, components=None):
    """Root's complete class assembly, retaining its existing governance owner.

    original_worker may be the original EntranceWorker. Governance builds its
    profile/closed-failure stack around the conditional Codex BASE, then scope
    routing surrounds that Worker. Pass the installed factory, never model JSON.
    Diagnostics is installed outside the returned class, including BOTH save
    modules if its phase observer is enabled.
    """
    policy = validate_policy(policy)
    if not callable(governance_factory) or governance is None or isinstance(governance_scopes, str):
        raise ValueError('native_goal_scope_existing_governance_required')
    scopes = tuple(governance_scopes)
    if policy['enabled'] and not set(policy['scopes']) <= set(scopes):
        raise ValueError('native_goal_scope_quality_policy_required')
    if not policy['enabled']:
        return governance_factory(original_worker, original_codex, governance, scopes,
                                  profile_loader, receipt_callback)
    if components is None:
        from .native_goal_runtime.codex import Codex
        from .native_goal_runtime.worker_strategy import WorkerStrategy
        components = Codex, WorkerStrategy
    goal_backend, strategy = components
    hybrid = make_backend(original_codex, goal_backend)
    governed = governance_factory(original_worker, hybrid, governance, scopes,
                                  profile_loader, receipt_callback)
    return make_worker(governed, hybrid, strategy, policy)


def make_store_class(original, methods, policy):
    """Same-ledger facade; no construction/recovery or state work on import."""
    policy = validate_policy(policy)
    class GoalScopeStore(original):
        _native_goal_scope_version = 1
        _yielded_active_goal_checkpoint = staticmethod(methods._yielded_active_goal_checkpoint)
        _yielded_goal_reserves_scope = classmethod(methods._yielded_goal_reserves_scope.__func__)

        def __init__(self, *args, **kwargs):
            super(GoalScopeStore, self).__init__(*args, **kwargs)
            self._native_goal_policy = copy.deepcopy(policy)
            self._native_goal_caller = threading.local()
            with self.transaction() as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_goal_scope_metadata'").fetchone()
                self._native_goal_store_bound = bool(exists) or policy['enabled']
                if not self._native_goal_store_bound:
                    return  # Disabled never creates metadata or changes the old path.
                db.execute('CREATE TABLE IF NOT EXISTS native_goal_scope_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS native_goal_scope_bindings(scope TEXT PRIMARY KEY,activated_at REAL NOT NULL)')
                expected = {'format': FORMAT, 'authority': policy['authority'], 'owner_id': policy['owner_id']}
                old = dict(db.execute('SELECT key,value FROM native_goal_scope_metadata'))
                if old and old != expected:
                    raise Conflict('native_goal_scope_authority_changed')
                if not old:
                    db.executemany('INSERT INTO native_goal_scope_metadata VALUES(?,?)', expected.items())
                if policy['enabled']:
                    for scope in policy['scopes']:
                        existing = db.execute('SELECT 1 FROM native_goal_scope_bindings WHERE scope=?', (scope,)).fetchone()
                        if existing is None and db.execute('SELECT 1 FROM native_sessions WHERE scope=?', (scope,)).fetchone():
                            raise Conflict('native_goal_scope_existing_session_requires_separate_review')
                        db.execute('INSERT OR IGNORE INTO native_goal_scope_bindings VALUES(?,?)',
                                   (scope, policy['activated_at']))

        def _native_goal_binding(self, db, task):
            if task is None or not self._native_goal_store_bound:
                return None
            row = db.execute('SELECT activated_at FROM native_goal_scope_bindings WHERE scope=?',
                             (task['scope'],)).fetchone()
            return _profile(policy, task['scope'], row[0]) if row else None

        def _native_goal_row_allowed(self, db, row, node):
            binding = self._native_goal_binding(db, row)
            if binding is None:
                return True
            return (policy['enabled'] and row['scope'] in policy['scopes'] and row['created'] >= binding['activated_at']
                    and getattr(self._native_goal_caller, 'node', None) == node)

        def claim(self, node, task_ids=None):
            if not self._native_goal_store_bound and task_ids is None:
                return super(GoalScopeStore, self).claim(node)
            return methods.claim(self, node, task_ids=task_ids)

        def _claim_in_db(self, db, node, task_ids=None):
            if not self._native_goal_store_bound and hasattr(original, '_claim_in_db'):
                return super(GoalScopeStore, self)._claim_in_db(db, node, task_ids=task_ids)
            return methods._claim_in_db(self, db, node, task_ids=task_ids)

        def _managed(self, task_id, node=None):
            with self.transaction() as db:
                return self._native_goal_managed_in_db(db, task_id, node)

        def _native_goal_managed_in_db(self, db, task_id, node=None):
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            binding = self._native_goal_binding(db, row)
            selected = binding is not None and row['created'] >= binding['activated_at']
            if selected and (not policy['enabled'] or row['scope'] not in policy['scopes']
                             or getattr(self._native_goal_caller, 'node', None) != node):
                raise PermissionError('native_goal_scope_authenticated_executor_required')
            return binding if selected else None

        def _native_goal_validate_checkpoint(self, db, row, checkpoint, status):
            binding = self._native_goal_binding(db, row)
            if 'native_goal_profile' not in checkpoint:
                checkpoint['native_goal_profile'] = binding  # Original authenticated scope, not caller authority.
            if checkpoint.get('native_goal_profile') != binding:
                raise Conflict('native_goal_scope_original_profile_changed')
            if checkpoint.get('side_effect_started') is not False:
                if status == 'completed':
                    raise Conflict('native_goal_scope_lifecycle_unsettled')
                return
            lifecycle = checkpoint.get('native_lifecycle')
            if lifecycle is None and checkpoint.get('native_execution_intent') is None and status in (None, 'waiting_backend', 'waiting_auth'):
                return  # Preflight metadata is not a started native lifecycle.
            if self._yielded_active_goal_checkpoint(checkpoint):
                return  # The extracted update checks the permitted waiting states.
            zero_turn = (isinstance(lifecycle, dict)
                         and lifecycle.get('zero_turn_terminal') is True
                         and lifecycle.get('goal_managed') is True
                         and lifecycle.get('turn_id') is None
                         and lifecycle.get('thread_status') == {'type': 'idle'}
                         and isinstance(lifecycle.get('goal'), dict)
                         and isinstance(lifecycle.get('history_fingerprint'), dict))
            if (not isinstance(lifecycle, dict)
                    or any(lifecycle.get(key) is not True for key in ('controller_enabled', 'settled', 'runtime_closed', 'quiescent'))
                    or type(lifecycle.get('goal_managed')) is not bool
                    or lifecycle.get('thread_id') != checkpoint.get('thread_id')
                    or not isinstance(lifecycle.get('turn_ids'), list)
                    or (not lifecycle['turn_ids'] and not zero_turn)
                    or set(lifecycle.get('completed_turn_ids', [])) != set(lifecycle['turn_ids'])
                    or lifecycle.get('completion_statuses') != {turn: 'completed' for turn in lifecycle['turn_ids']}
                    or (lifecycle.get('goal') is not None and lifecycle['goal'].get('status') not in ('complete', 'paused', 'blocked', 'budgetLimited', 'usageLimited'))
                    or checkpoint.get('goal') != lifecycle.get('goal')
                    or checkpoint.get('native_execution_intent', {}).get('settled') is not True):
                raise Conflict('native_goal_scope_lifecycle_unsettled')

        def _native_goal_continuation_source_settled(self, db, row, native, state):
            """Narrow exception to the old session guard, in its owner transaction.

            The session stays guarded. Only the original completed task/epoch's
            matching sealed proof and original committed artifact permit NEW
            owner input through the existing continuation protocol.
            """
            binding = self._native_goal_binding(db, row)
            if (binding is None or not policy['enabled'] or row['scope'] not in policy['scopes']
                    or row['created'] < binding['activated_at'] or row['status'] != 'completed'
                    or not isinstance(state, dict) or state.get('side_effect_started') is not True
                    or state.get('native_goal_profile') != binding
                    or state.get('harness') != 'codex'):
                return False
            checkpoint = json.loads(row['checkpoint'])
            intent = checkpoint.get('native_execution_intent')
            lifecycle = checkpoint.get('native_lifecycle')
            artifact = hashlib.sha256(json.dumps([row['id'], row['epoch'], 'codex'],
                separators=(',', ':')).encode()).hexdigest()
            if (not isinstance(intent, dict) or intent.get('task_id') != row['id']
                    or intent.get('task_epoch') != row['epoch'] or intent.get('harness') != 'codex'
                    or intent.get('thread_id') != checkpoint.get('thread_id')
                    or intent.get('settled') is not True
                    or state.get('native_execution_intent') != intent
                    or not isinstance(lifecycle, dict) or state.get('native_lifecycle') != lifecycle
                    or not isinstance(lifecycle.get('history_fingerprint'), dict)
                    or state.get('goal') != checkpoint.get('goal')
                    or state.get('thread_id') != checkpoint.get('thread_id')
                    or native['scope'] != row['scope'] or native['harness'] != 'codex'
                    or native['node'] != row['node'] or state.get('codex_node') != native['node']
                    or checkpoint.get('codex_node') != native['node'] or native['artifact'] != artifact):
                return False
            try:
                self._native_goal_validate_checkpoint(db, row, copy.deepcopy(checkpoint), 'completed')
                from .native_goal_runtime.sessions import _expected_fingerprint
                _expected_fingerprint(lifecycle['history_fingerprint'])
            except (ValueError, TypeError, KeyError, AttributeError):
                return False
            chunks = db.execute('SELECT COUNT(*),MIN(part),MAX(part) FROM session_chunks WHERE id=?',
                               (artifact,)).fetchone()
            return bool(chunks[0] and chunks[1] == 0 and chunks[2] == chunks[0] - 1)

        def continue_task(self, payload):
            if not self._native_goal_store_bound:
                return super(GoalScopeStore, self).continue_task(payload)
            return methods.continue_task(self, payload)

        def update_task(self, task_id, node, epoch, checkpoint=None, result=None, status=None):
            if self._managed(task_id, node) is None:
                return super(GoalScopeStore, self).update_task(task_id, node, epoch, checkpoint, result, status)
            return methods.update_task(self, task_id, node, epoch, checkpoint, result, status)

        def _update_task_in_db(self, db, task_id, node, epoch, checkpoint=None, result=None, status=None):
            binding = self._native_goal_managed_in_db(db, task_id, node)
            if binding is None and hasattr(original, '_update_task_in_db'):
                return super(GoalScopeStore, self)._update_task_in_db(
                    db, task_id, node, epoch, checkpoint, result, status)
            return methods._update_task_in_db(self, db, task_id, node, epoch, checkpoint, result, status)

        def agent_action(self, task_id, node, epoch, call_id, action, arguments):
            if self._managed(task_id, node) is None or action not in ('delegate', 'wait_children'):
                return super(GoalScopeStore, self).agent_action(task_id, node, epoch, call_id, action, arguments)
            return methods.agent_action(self, task_id, node, epoch, call_id, action, arguments)

        def session_action(self, task_id, node, epoch, action, payload):
            binding = self._managed(task_id, node)
            if binding is not None and action == 'commit':
                state = payload.get('state') if isinstance(payload, dict) else None
                if (not isinstance(state, dict) or state.get('side_effect_started') is not True
                        or state.get('native_goal_profile') != binding):
                    raise ValueError('native_goal_scope_guarded_session_required')
            result = super(GoalScopeStore, self).session_action(task_id, node, epoch, action, payload)
            if binding is not None and action == 'commit':
                if (not isinstance(result, dict) or result.get('saved') is not True
                        or result.get('scope') != binding['scope'] or result.get('ok', True) is not True):
                    raise Conflict('native_goal_scope_commit_unconfirmed')
                return dict(result, ok=True)
            return result

    return GoalScopeStore


def attach_authority(api, policy):
    """Ready on the ONE API; capability comes from its real authenticated peer."""
    policy = validate_policy(policy)
    if not policy['enabled']:
        return None
    store = api.store
    if (getattr(store, '_native_goal_policy', None) != policy
            or api.ingress.authority != policy['authority'] or api.ingress.owner_id != policy['owner_id']):
        raise ValueError('native_goal_scope_existing_authority_required')
    for node in policy['executors']:
        if not any(peer.get('role') == 'worker' and peer.get('node') == node
                   and isinstance(peer.get('capabilities'), list) and CAPABILITY in peer['capabilities']
                   for unused_secret, peer in api.peers):
            raise PermissionError('native_goal_scope_existing_worker_grant_required')
    old = getattr(api, '_native_goal_scope_attachment', None)
    if old is not None:
        if old != policy:
            raise Conflict('native_goal_scope_installed_policy_changed')
        return store
    previous = api.dispatch

    def dispatch(method, path, payload, peer):
        before = getattr(store._native_goal_caller, 'node', None)
        allowed = (isinstance(peer, dict) and peer.get('role') == 'worker'
                   and peer.get('node') in policy['executors']
                   and isinstance(peer.get('capabilities'), list) and CAPABILITY in peer['capabilities'])
        store._native_goal_caller.node = peer['node'] if allowed else None
        try:
            return previous(method, path, payload, peer)
        finally:
            store._native_goal_caller.node = before

    api.dispatch = dispatch
    api._native_goal_scope_attachment = copy.deepcopy(policy)
    return store
