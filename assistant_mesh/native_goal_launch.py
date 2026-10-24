"""Thin owner-code composition for the existing receiver and worker launchers.

No policy, ledger, process, grant or listener is created on import. Keep this
module with the pinned scoped artifact, not on a live development import path.
"""
import threading

from .native_goal_scoped import (
    assemble_worker, attach_authority, make_store_class, validate_policy)


_RECEIVER_COMPOSITION = threading.Lock()


def load_policy(path=None):
    """An omitted owner file is default off; never invent an admission policy."""
    if path is None:
        return None
    from .config import private_json
    return validate_policy(private_json(path))


def compose_worker(original_worker, original_codex, legacy_factory, *,
                   policy=None, node=None, governance_factory=None,
                   governance=None, governance_scopes=(), profile_loader=None,
                   receipt_callback=None, diagnostics=None,
                   save_observer=None, worker_observer=None):
    """Return ONE class, then let the original launcher construct/run it.

    legacy_factory returns the launcher's original profiled/governed class;
    it is not called on a selected executor. Observer callbacks are the
    already-installed metadata helpers, not module names from configuration.
    A non-executor (including the existing Claw) keeps its original path.
    """
    if not callable(legacy_factory):
        raise ValueError('native_goal_launch_legacy_factory_required')
    policy = validate_policy(policy) if policy is not None else None
    enabled = policy is not None and policy['enabled']
    if enabled and (not isinstance(node, str) or not node):
        raise ValueError('native_goal_launch_existing_node_required')
    selected = enabled and node in policy['executors']
    if selected:
        worker = assemble_worker(original_worker, original_codex, policy,
            governance_factory=governance_factory, governance=governance,
            governance_scopes=governance_scopes, profile_loader=profile_loader,
            receipt_callback=receipt_callback)
    else:
        worker = legacy_factory()
    if not isinstance(worker, type):
        raise ValueError('native_goal_launch_worker_class_required')
    if any(value is not None for value in (diagnostics, save_observer, worker_observer)):
        if (diagnostics is None or not callable(save_observer)
                or not callable(worker_observer)):
            raise ValueError('native_goal_launch_existing_observers_required')
        from . import sessions
        save_observer(sessions, enabled=diagnostics.enabled)
        if selected:
            from .native_goal_runtime import sessions as goal_sessions
            save_observer(goal_sessions, enabled=diagnostics.enabled)
        worker = worker_observer(worker, diagnostics)
    return worker


def serve_entrance(config, socket_path, *, policy=None, ready=None):
    """Compose the original sole receiver without replacing its lifecycle.

    Its Entrance/Claw/passive factory still constructs the only Store with
    recover_inflight=False. Its existing ready hook creates ingress and keeps
    final/emit/feed/close hooks before the scoped authenticated dispatch wraps
    that SAME API. Restore our temporary class factory at ready and on errors.

    After a scope has ever been bound, rollback must keep an explicit disabled
    policy and this facade installed; omitting the file is only the never-bound
    legacy default, not a rollback path for an admitted scope.
    """
    from . import entrance_runtime
    if ready is not None and not callable(ready):
        raise ValueError('native_goal_launch_existing_ready_required')
    if policy is None:
        return entrance_runtime.serve_entrance(config, socket_path, ready=ready)
    policy = validate_policy(policy)
    if policy['enabled'] and ready is None:
        raise ValueError('native_goal_launch_existing_ready_required')
    from .native_goal_runtime.authority import AuthorityMethods
    if not _RECEIVER_COMPOSITION.acquire(False):
        raise ValueError('native_goal_launch_receiver_already_owned')
    previous = entrance_runtime.entrance_store_class

    def factory(original, *args, **kwargs):
        return make_store_class(previous(original, *args, **kwargs),
                                AuthorityMethods, policy)

    def connected(http_server, api, channel):
        if entrance_runtime.entrance_store_class is not factory:
            raise ValueError('native_goal_launch_source_factory_changed')
        entrance_runtime.entrance_store_class = previous
        if ready is not None:
            ready(http_server, api, channel)
        attach_authority(api, policy)

    try:
        entrance_runtime.entrance_store_class = factory
        return entrance_runtime.serve_entrance(config, socket_path, ready=connected)
    finally:
        if entrance_runtime.entrance_store_class is factory:
            entrance_runtime.entrance_store_class = previous
        _RECEIVER_COMPOSITION.release()
