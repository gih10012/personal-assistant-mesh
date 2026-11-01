"""Observed in-process OAuth writer quiescence, never a cross-node writer fence.

Only deployment code can freeze this object. No HTTP/model control endpoint or
caller-provided stopped flag is accepted. Shielded operations remain tracked
after cancellation/disconnect until their actual async I/O has finished.
"""
import asyncio
from contextvars import ContextVar
import functools
import hashlib
import importlib
from importlib.metadata import version
import inspect
from pathlib import Path
import secrets


VERSIONS = {'fastmcp-slim': '4.1.0', 'mcp': '2.3.0', 'py-key-value-aio': '0.4.6', 'cryptography': '50.0.2'}
SDK_PINS = {
    'fastmcp.server.auth.cimd': '290e7295de44a067be4d357a180b4b127a3f99c8f8f4c099716ec900d358dd99',
    'fastmcp.server.auth.identity_assertion': 'a5cceb4c985f2a8d657789e7ccdaa8adaaac8e1288a59a713acf399c37259a28',
    'fastmcp.server.auth.oauth_proxy.models': 'dd00e213028ccefe3ab4afc29646a34621bcf6ff938b00abf1b1f771bd275a53',
    'key_value.aio.adapters.pydantic.base': '9e1d7819708599ba934f7536799a4d8c7d3b06b4d998ff068f499c6bc8ebeb9f',
    'fastmcp.server.auth.oauth_proxy.proxy': 'a02c9f06e3a1bd57435aa26006bcddd5ea498f0535193007bc663a81ffae926f',
    'fastmcp.server.auth.oauth_proxy.consent': '26c130a469aeeee0ceaf2295ddd8628c3074155521c9f4487f1598c2898659e6',
    'fastmcp.server.auth.oauth_proxy.upstream': 'a7dfed182cd72300a09136f500738cafd81960807b474fd184143a24fa0898d9',
    'key_value.aio.stores.filetree.store': 'c278eb600d78fe01c1770b2352db910b1737f42b82104278b8bf2013a1bbcdab',
    'key_value.aio.stores.base': '8f6352bbad37d643a166e00bf06046c1fc7118038e1e80c474f1d1ed22a0736c',
    'key_value.aio.wrappers.encryption.base': '84a2c8b39e7dbbe90c75521939d575c1a1a4192bc725d1431b844403b848582f',
    'key_value.aio.wrappers.encryption.fernet': 'd4dd8a48308586abb1bbcd1d1d8f7fea979f0369606d393c6f3f8a7b2ccca18f',
    'key_value.aio.wrappers.base': 'fc042d46d3394540468f0c04c9387b777e3e6edabe71582614081d98b1c5c890',
    'key_value.aio._utils.managed_entry': '6393025e0503bfdbcb9cc9f574453d64ba9a7cb1ff6089056539069cea138740',
    'key_value.aio._utils.serialization': '114c280484bf625866cba7c87f024ec9f8fb12dd1e0b08928d98c4ff6307284e',
    'key_value.aio._utils.sanitization': '2ee4a192481995d645da814a8e27b5d85da12bd70ca8f421d0d889e4b9960d16',
    'key_value.aio._utils.sanitize': 'd7ef9b290b7558285892569a1a3cac36caa9892b38be673c002ac07fdf2729ce',
}
STAGING_MARKER = '.mesh-oauth-staging-only'


class WriterFrozen(ValueError):
    pass


def audit_sdk():
    installed = {name: version(name) for name in VERSIONS}
    hashes = {name: hashlib.sha256(Path(importlib.import_module(name).__file__).read_bytes()).hexdigest()
              for name in SDK_PINS}
    if installed != VERSIONS or hashes != SDK_PINS:
        raise ValueError('oauth_migration_unreviewed_sdk_sources')
    return {'versions': installed, 'module_sha256': hashes}


class FreezeLease:
    def __init__(self, boundary):
        self._boundary = boundary

    def evidence(self, state_root, bound, references):
        boundary = self._boundary
        if (boundary._lease is not self or boundary.phase != 'sealed' or boundary.http or boundary.ops
                or Path(state_root).absolute() != boundary.state_root
                or bound != boundary.binding or references != boundary.key_references):
            raise ValueError('oauth_migration_live_freeze_lease_required')
        return {'kind': 'observed_in_process_asgi_and_sdk_drain', 'controller_id': boundary.identity,
                'asgi_active': 0, 'sdk_active': 0, 'completed_operations': boundary.completed,
                'instrumented_async_methods': boundary.instrumented,
                'provider_state': boundary.provider_evidence(),
                'sdk_audit': boundary.audit, 'cross_process_writer_fenced': False,
                'activation_authorized': False}


class MigrationBoundary:
    def __init__(self, document, *, allow_loopback_for_tests=False):
        from .oauth_state_bundle import binding, key_references
        config, self.binding = binding(document, allow_loopback_for_tests)
        self.state_root = Path(config['state']['directory']).absolute()
        if (self.state_root / STAGING_MARKER).exists():
            raise ValueError('oauth_migration_staging_not_activated')
        self.audit = audit_sdk()
        self.key_references = key_references(config)
        self.identity = secrets.token_hex(16)
        self.phase, self.completed, self._lease = 'open', 0, None
        self.http, self.ops = {}, {}
        self.condition = asyncio.Condition()
        self._http_context = ContextVar('oauth_http_' + self.identity, default=None)
        self._op_context = ContextVar('oauth_sdk_' + self.identity, default=None)
        self.instrumented = {}
        self._provider = None

    def bind_provider(self, provider):
        if self._provider is not None:
            raise ValueError('oauth_migration_provider_already_bound')
        self._provider = provider

    def provider_evidence(self):
        from .oauth_state_bundle import COLLECTIONS
        provider = self._provider
        if provider is None:
            raise ValueError('oauth_migration_actual_provider_required')
        names = ('_upstream_token_store', '_client_store', '_transaction_store', '_consent_csrf_store',
                 '_code_store', '_jti_mapping_store', '_refresh_token_store')
        for name, collection in zip(names, COLLECTIONS):
            store = getattr(provider, name)
            if store._key_value is not provider._client_storage or store._default_collection != collection:
                raise ValueError('oauth_migration_persistent_store_binding_changed')
        filetree = provider._client_storage.key_value
        if (Path(str(filetree._data_directory)) != self.state_root
                or Path(str(filetree._metadata_directory)) != self.state_root):
            raise ValueError('oauth_migration_external_metadata_not_supported')
        if (provider._identity_assertion is not None or provider._identity_assertion_validator is not None
                or provider._revoked_id_jag_jtis):
            raise ValueError('oauth_migration_id_jag_state_not_supported')
        if any(lock.locked() for lock in provider._refresh_locks.values()):
            raise ValueError('oauth_migration_refresh_lock_still_live')
        manager = provider._cimd_manager
        if manager is not None and manager._assertion_validator._jti_cache:
            # This replay cache is process-local in 4.1.0. Never drop an active
            # private_key_jwt assertion fence while claiming a complete image.
            raise ValueError('oauth_migration_cimd_replay_state_not_supported')
        return {'persistent_collections': list(COLLECTIONS), 'identity_assertion': 'disabled',
                'id_jag_revocations': 0, 'cimd_assertion_replay_entries': 0,
                'refresh_locks': 'observed_unlocked_after_drain',
                'cimd_metadata_cache': 'cold_refetch_under_unchanged_approved_client_and_redirect_policy',
                'request_contexts': 'drained_not_transferred'}

    async def invoke(self, kind, function):
        context = self._http_context if kind == 'http' else self._op_context
        owned = self.http if kind == 'http' else self.ops
        current = context.get()
        if kind != 'http' and current in self.ops and self.ops[current] is asyncio.current_task():
            return await function()  # Awaited nested calls stay inside this tracked operation.
        async with self.condition:
            admitted_parent = self._op_context.get() in self.ops
            admitted_http = self._http_context.get() in self.http
            allowed = self.phase == 'open' or (kind != 'http' and (
                admitted_parent or (self.phase == 'draining' and admitted_http)))
            if not allowed:
                raise WriterFrozen('oauth_migration_writer_frozen')
            identity = object()

            async def execute():
                token = context.set(identity)
                try:
                    return await function()
                finally:
                    context.reset(token)
                    async with self.condition:
                        owned.pop(identity, None)
                        self.completed += 1
                        self.condition.notify_all()
            task = asyncio.create_task(execute())
            owned[identity] = task
        # Cancellation of the caller never turns still-running SDK/file I/O
        # into a false zero count. The actual operation owns its removal.
        return await asyncio.shield(task)

    def instrument(self, original, kind):
        boundary = self
        methods = {}
        for name, function in inspect.getmembers(original, inspect.iscoroutinefunction):
            if name.startswith('__'):
                continue
            def wrap(function):
                @functools.wraps(function)
                async def tracked(instance, *args, **kwargs):
                    return await boundary.invoke(kind, lambda: function(instance, *args, **kwargs))
                return tracked
            methods[name] = wrap(function)
        if not methods:
            raise ValueError('oauth_migration_sdk_async_boundary_missing')
        self.instrumented[kind] = sorted(methods)
        return type('Observed' + original.__name__, (original,), methods)

    def wrap_asgi(self, app):
        boundary = self
        class ObservedASGI:
            oauth_migration = boundary

            def __getattr__(self, name):
                return getattr(app, name)

            async def __call__(self, scope, receive, send):
                if scope['type'] == 'lifespan':
                    return await app(scope, receive, send)
                async def rejected():
                    if scope['type'] == 'websocket':
                        return await send({'type': 'websocket.close', 'code': 1013})
                    await send({'type': 'http.response.start', 'status': 503,
                                'headers': [(b'content-type', b'application/json'), (b'cache-control', b'no-store')]})
                    return await send({'type': 'http.response.body', 'body': b'{"error":"oauth_writer_frozen","outcome":"not_attempted"}'})
                if boundary.phase != 'open':
                    return await rejected()
                try:
                    return await boundary.invoke('http', lambda: app(scope, receive, send))
                except WriterFrozen:
                    return await rejected()
        return ObservedASGI()

    async def freeze(self, timeout=30):
        if not 0 < timeout <= 300:
            raise ValueError('oauth_migration_drain_timeout_invalid')
        async with self.condition:
            if self.phase == 'open':
                self.phase = 'draining'
        async def drain():
            async with self.condition:
                await self.condition.wait_for(lambda: not self.http)
                self.phase = 'sealed'
                await self.condition.wait_for(lambda: not self.ops)
                self.provider_evidence()
                if self._lease is None:
                    self._lease = FreezeLease(self)
                return self._lease
        # Timing out does not cancel the SDK operations or reopen admission.
        return await asyncio.wait_for(drain(), timeout)
