"""Thin assembly around an incumbent worker launcher, not an authority.

Imports perform no configuration/account/ledger/process/model operation. The
source builder supplies a pinned modern backend and execution strategy.
"""
import json
from pathlib import Path


FORMAT = 'amita-native-worker-release/1'
NATIVE_OPTIONS = {'native_transport': 'unix', 'native_goal_drain': True, 'native_goal_yield': True}


def policy(value):
    if (not isinstance(value, dict) or set(value) != {'format', 'revision', 'additional_profiles'}
            or value['format'] != FORMAT or type(value['revision']) is not int or value['revision'] < 1
            or not isinstance(value['additional_profiles'], list) or len(value['additional_profiles']) > 64):
        raise ValueError('native_worker_release_explicit_policy_required')
    result, seen = [], set()
    for row in value['additional_profiles']:
        if not isinstance(row, dict) or set(row) != {'scope', 'profile_path'}:
            raise ValueError('native_worker_release_profile_binding_invalid')
        scope, path = row['scope'], row['profile_path']
        if (not isinstance(scope, str) or not scope.strip() or scope != scope.strip() or '*' in scope
                or len(scope.encode('utf8')) > 256 or any(ord(c) < 32 for c in scope)
                or scope in seen or not isinstance(path, str) or not Path(path).is_absolute()
                or '..' in Path(path).parts):
            raise ValueError('native_worker_release_profile_binding_invalid')
        seen.add(scope); result.append(dict(row))
    return dict(format=FORMAT, revision=value['revision'], additional_profiles=result)


def profile_factory(original, private_reader, additional):
    """Keep the original phone loader; only explicit additional scopes route.

    Model/effort values stay in the original owner's mutable private profile
    files. No baked-in model list, fallback or inference about actual effort.
    """
    if not callable(original) or not callable(private_reader):
        raise TypeError('native_worker_release_original_profile_loader_required')

    def factory(profile_loader, scopes):
        original_scopes = tuple(scopes)
        if any(row['scope'] in original_scopes for row in additional):
            raise ValueError('native_worker_release_cannot_replace_original_profile_binding')
        primary = original(profile_loader, original_scopes)
        routed = {}
        for row in additional:
            path = row['profile_path']
            routed[row['scope']] = original(lambda agent, path=path: private_reader(path), (row['scope'],))

        class BoundProfiles:
            worker = None

            def bind(self, worker):
                if self.worker is not None and self.worker is not worker:
                    raise ValueError('native_worker_release_profile_worker_changed')
                primary.bind(worker)
                for item in routed.values():
                    item.bind(worker)
                self.worker = worker
                return worker

            def __call__(self, agent):
                task = getattr(self.worker, 'current', None)
                scope = task.get('scope') if isinstance(task, dict) else None
                return routed[scope](agent) if scope in routed else primary(agent)

        return BoundProfiles()
    return factory


def make_worker(original, strategy):
    """Same original constructor/client/tools; modern claimed-turn lifecycle.

    The trusted native options affect this version's backend process only.
    Original config dictionaries, account paths, Goal request and state stay
    untouched. Never broaden the old role or start another harness on quota.
    """
    needed = ('run_claimed', 'on_activity', 'tick', '_native_yield_tick', '_checked_native_yield_seal')
    if not isinstance(original, type) or not all(callable(getattr(strategy, name, None)) for name in needed):
        raise TypeError('native_worker_release_pinned_strategy_required')

    class ModernWorker(original):
        _native_worker_release_version = 1

        def __init__(self, config, *args, **kwargs):
            if config.get('pi'):
                raise ValueError('native_worker_release_codex_only_config_required')
            super(ModernWorker, self).__init__(config, *args, **kwargs)
            self._native_starting, self._native_turn_live = False, None

        def run_once(self):
            self.tick(force=True)
            task = self.client.request('/v1/claim', {})['task']
            if not task:
                return False
            return strategy.run_claimed(self, task)

        def open_backend(self):
            previous = self.config
            options = dict(previous, codex=dict(previous['codex'], **NATIVE_OPTIONS))
            if previous.get('codex_accounts') is not None:
                options['codex_accounts'] = [dict(row, **NATIVE_OPTIONS) for row in previous['codex_accounts']]
            self.config = options
            try:
                return super(ModernWorker, self).open_backend()
            finally:
                self.config = previous

        def on_activity(self, kind, value):
            return strategy.on_activity(self, kind, value)

        def tick(self, force=False, require_ack=False):
            return strategy.tick(self, force=force, require_ack=require_ack)

        def _native_yield_tick(self, force=False):
            return strategy._native_yield_tick(self, force=force)

        def _checked_native_yield_seal(self, agent, drained, value):
            return strategy._checked_native_yield_seal(self, agent, drained, value)

        def on_tool(self, call):
            output = super(ModernWorker, self).on_tool(call)
            arguments = call.get('arguments', {})
            waiting = call.get('tool') == 'mesh_wait_children' or (
                call.get('tool') == 'mesh' and isinstance(arguments, dict) and arguments.get('action') == 'wait_children')
            if waiting and getattr(self.agent, 'native_drain_active', False) is not True:
                try:
                    body = json.loads(output['contentItems'][0]['text'])
                except (KeyError, IndexError, TypeError, ValueError):
                    body = None
                self.wait_children = not (isinstance(body, dict) and body.get('continue_after_children') is False)
                self.current['checkpoint']['wait_children_requested'] = self.wait_children
            return output

    return ModernWorker


def install(incumbent, settings, modern_codex, strategy, modern_sessions):
    """Change bindings consumed by the SAME original launcher.main exactly once.

    Its Governance instance, original scopes/receipt callback, metadata observer,
    profile paths, factories and rolling run_worker call remain owned there.
    """
    selected = policy(settings)
    if getattr(incumbent, '_native_worker_release_installed', False):
        raise ValueError('native_worker_release_launcher_already_composed')
    incumbent.Worker = make_worker(incumbent.Worker, strategy)
    incumbent.Codex = modern_codex
    incumbent.TaskScopedProfile = profile_factory(incumbent.TaskScopedProfile, incumbent.private_json,
                                                selected['additional_profiles'])
    original_save_observer = incumbent.install_save_observer

    def observe_saves(original_sessions, enabled=False):
        result = original_save_observer(original_sessions, enabled=enabled)
        original_save_observer(modern_sessions, enabled=enabled)
        return result
    incumbent.install_save_observer = observe_saves
    incumbent._native_worker_release_installed = True
    return {'format': FORMAT, 'revision': selected['revision'], 'original_governance_preserved': True,
            'original_account_configuration_preserved': True, 'model_called': False,
            'effective_model_effort_verified': False}
