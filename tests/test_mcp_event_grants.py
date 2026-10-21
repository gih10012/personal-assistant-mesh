"""New grant checks only. SQLite + source-shaped stores; no real account/IO.

The separately selected PinnedOAuthGrantCheck uses the existing pinned OAuth
test fixture with synthetic upstream HTTP, never the old eight/two suite.
"""
import asyncio
import contextlib
import hashlib
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

try:
    import jwt
    from starlette.applications import Starlette
    LEDGER_FIXTURE_AVAILABLE = True
except ImportError:
    LEDGER_FIXTURE_AVAILABLE = False

from assistant_mesh.mcp_event_grants import CurrentOAuthGrants
from assistant_mesh.mcp_http_auth import NotConfigured
from tests.test_mcp_http import policy


P = {'owner_id': 'owner-fixture', 'source': 'chatgpt-chat', 'subject': 'owner-main-chat', 'scopes': ['tasks:read']}


class StoreFixture:
    def __init__(self):
        self.rows = {}
    async def get(self, *, key):
        return self.rows.get(key)


class ProviderFixture:
    """Explicit synthetic provider. Actual engine integration is a separate test."""
    def __init__(self, now, shared=None, refresh=False):
        self.now, self.refresh = now, refresh
        self.state = shared or NS(upstream=StoreFixture(), jti=StoreFixture(), refresh=StoreFixture(), sequence=0)
        self._upstream_token_store, self._jti_mapping_store, self._refresh_token_store = self.state.upstream, self.state.jti, self.state.refresh
        self.identity = True; self.calls = {'code': 0, 'refresh': 0, 'revoke': 0, 'identity': 0}
        self._token_expiry_threshold_seconds = 0
        self.jwt_issuer = NS(verify_token=self.verify)
        self._token_validator = NS(verify_token=self.validate)
        self.locks = {}

    def verify(self, token, expected_token_use='access'):
        value = jwt.decode(token, b'synthetic-test-key-not-installed-anywhere', algorithms=['HS256'],
            audience=policy()['resource_url'], issuer=policy()['issuer_url'], options={'verify_exp': False})
        if value['token_use'] != expected_token_use:
            raise ValueError('synthetic wrong use')
        return value

    def sign(self, upstream_id, use):
        self.state.sequence += 1; jti = 'opaque-jti-' + str(self.state.sequence)
        raw = jwt.encode({'iss': policy()['issuer_url'], 'aud': policy()['resource_url'],
            'client_id': 'chatgpt-fixture-client', 'scope': 'tasks:read', 'exp': int(self.now[0]) + (900 if use == 'access' else 7200),
            'jti': jti, 'token_use': use, 'upstream_claims': {'owner_subject': 'subject-owner'}},
            b'synthetic-test-key-not-installed-anywhere', algorithm='HS256')
        self.state.jti.rows[jti] = NS(jti=jti, upstream_token_id=upstream_id)
        if use == 'refresh':
            self.state.refresh.rows[hashlib.sha256(raw.encode()).hexdigest()] = NS(
                client_id='chatgpt-fixture-client', scopes=['tasks:read'], expires_at=int(self.now[0]) + 7200)
        return raw

    async def validate(self, token):
        self.calls['identity'] += 1
        return NS(subject='subject-owner') if self.identity and token.startswith('UPSTREAM_') else None

    def _get_verification_token(self, value):
        return value.access_token

    def _get_refresh_lock(self, identity):
        return self.locks.setdefault(identity, asyncio.Lock())

    async def _try_transparent_refresh(self, upstream):
        upstream.expires_at = self.now[0] + 3600
        return upstream

    async def exchange_authorization_code(self, client, code):
        self.calls['code'] += 1; self.state.sequence += 1
        identity = 'opaque-upstream-' + str(self.state.sequence)
        self.state.upstream.rows[identity] = NS(upstream_token_id=identity, client_id='chatgpt-fixture-client',
            access_token='UPSTREAM_' + identity, refresh_token='UPSTREAM_REFRESH' if self.refresh else None,
            refresh_token_expires_at=self.now[0] + 7200 if self.refresh else None,
            expires_at=self.now[0] + 86400, scope='tasks:read', created_at=self.now[0])
        return NS(access_token=self.sign(identity, 'access'),
                  refresh_token=self.sign(identity, 'refresh') if self.refresh else None, expires_in=900)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.calls['refresh'] += 1
        claims = self.verify(refresh_token.token, 'refresh')
        identity = self.state.jti.rows.pop(claims['jti']).upstream_token_id
        self.state.refresh.rows.pop(hashlib.sha256(refresh_token.token.encode()).hexdigest())
        return NS(access_token=self.sign(identity, 'access'), refresh_token=self.sign(identity, 'refresh'), expires_in=900)

    async def revoke_token(self, token):
        self.calls['revoke'] += 1
        self.state.refresh.rows.pop(hashlib.sha256(token.token.encode()).hexdigest(), None)


@unittest.skipUnless(LEDGER_FIXTURE_AVAILABLE, 'optional signed-token/ASGI fixture environment absent; not grant verification')
class GrantLedgerChecks(getattr(unittest, 'IsolatedAsyncioTestCase', unittest.TestCase)):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-events-grants.')
        self.addCleanup(self.temp.cleanup); self.now = [time.time()]

    def install(self, provider, directory=None):
        gate = CurrentOAuthGrants(directory or self.temp.name, policy(), clock=lambda: self.now[0])
        calls = []
        @contextlib.asynccontextmanager
        async def original(app):
            calls.append('start')
            try:
                yield {'retained_context': True}
            finally:
                calls.append('close')
        app, server = Starlette(lifespan=original), NS(middleware=[])
        gate.install(provider, server, app)
        return gate, app, calls

    async def active(self, gate):
        return await asyncio.to_thread(gate.account_active, P)

    async def test_no_refresh_expired_jwt_restart_real_identity_and_explicit_revocation(self):
        provider = ProviderFixture(self.now)
        tokens = await provider.exchange_authorization_code(None, None)
        gate, app, calls = self.install(provider)
        async with app.router.lifespan_context(app) as state:
            self.assertTrue(state['retained_context'])
            self.assertTrue(await gate.observe_access(tokens.access_token))
            claims = provider.verify(tokens.access_token)
            provider.state.jti.rows.pop(claims['jti']); self.now[0] += 901
            self.assertTrue(await self.active(gate))  # NOT the old JWT's 900s lease.
        self.assertEqual(['start', 'close'], calls); self.assertFalse(gate.account_active(P))
        provider2 = ProviderFixture(self.now, provider.state)
        restarted, app2, unused = self.install(provider2)
        async with app2.router.lifespan_context(app2):
            self.assertTrue(await self.active(restarted))  # Persisted opaque ref, fresh identity.
            provider2.identity = False; self.now[0] += 31
            self.assertFalse(await self.active(restarted))
            provider2.identity = True
            restarted.revoke_subject(P)
            self.assertFalse(await self.active(restarted))
            existing_id = next(iter(provider2.state.upstream.rows))
            another_access = provider2.sign(existing_id, 'access')
            self.assertFalse(await restarted.observe_access(another_access))
            self.now[0] += 1
            fresh = await provider2.exchange_authorization_code(None, None)
            self.assertEqual(900, fresh.expires_in); self.assertTrue(await self.active(restarted))
            self.assertEqual(1, provider2.calls['code'])
        stored = restarted.database.read_bytes()
        for secret in (tokens.access_token, another_access, fresh.access_token, 'UPSTREAM_' + existing_id):
            self.assertNotIn(secret.encode(), stored)

    async def test_refresh_pair_rotation_hash_revoke_unknown_and_original_call_counts(self):
        provider = ProviderFixture(self.now, refresh=True)
        initial = await provider.exchange_authorization_code(None, None)
        gate, app, unused = self.install(provider)
        async with app.router.lifespan_context(app):
            self.assertFalse(await gate.observe_access(initial.access_token))  # No guessed hash->JTI association.
            registered = await provider.exchange_refresh_token(None, NS(token=initial.refresh_token), ['tasks:read'])
            self.assertEqual(900, registered.expires_in); self.assertTrue(await self.active(gate))
            self.assertEqual(1, provider.calls['refresh'])
            hashed = hashlib.sha256(registered.refresh_token.encode()).hexdigest()
            metadata = provider.state.refresh.rows.pop(hashed)
            self.assertFalse(await self.active(gate))  # Upstream still exists; refresh revocation wins cache.
            provider.state.refresh.rows[hashed] = metadata; metadata.client_id = 'foreign-client'
            self.assertFalse(await self.active(gate)); metadata.client_id = 'chatgpt-fixture-client'
            self.assertTrue(await self.active(gate))
            await provider.revoke_token(NS(token=registered.refresh_token))
            self.assertEqual(1, provider.calls['revoke']); self.assertFalse(await self.active(gate))
            self.now[0] += 1
            with patch.object(gate, '_register', side_effect=OSError('synthetic metadata failure')):
                result = await provider.exchange_authorization_code(None, None)
            self.assertEqual(900, result.expires_in); self.assertEqual(2, provider.calls['code'])
            with patch.object(gate, '_revoke_token', side_effect=OSError('synthetic metadata failure')):
                with self.assertRaises(NotConfigured):
                    await provider.revoke_token(NS(token=result.access_token))
            self.assertEqual(2, provider.calls['revoke']); self.assertFalse(await self.active(gate))
        stored = gate.database.read_bytes()
        for raw in (registered.refresh_token, registered.access_token, initial.access_token, 'UPSTREAM_REFRESH'):
            self.assertNotIn(raw.encode(), stored)


try:
    from tests import test_chat_github_oauth as oauth_fixture
    AVAILABLE = oauth_fixture.AVAILABLE
except ImportError:
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, 'requires actual pinned OAuth env; not covered by source-shaped checks')
class PinnedOAuthGrantCheck(getattr(unittest, 'IsolatedAsyncioTestCase', unittest.TestCase)):
    async def test_existing_no_refresh_connection_is_adopted_on_authenticated_mcp_only(self):
        fixture = oauth_fixture.ActualOAuthTests('test_refresh_uses_component_and_cannot_add_mesh_scopes_or_expose_upstream_tokens')
        await fixture.asyncSetUp()
        try:
            fixture.idp_refresh = False
            # Existing connection: its normal successful code exchange predates
            # this Events installation. No new login/code/refresh is requested.
            code, verifier = await fixture.authorization_code('tasks:read')
            response = await fixture.http.post('/token', data={'grant_type': 'authorization_code',
                'client_id': oauth_fixture.CLIENT, 'code': code, 'code_verifier': verifier,
                'redirect_uri': fixture.redirect_uri, 'resource': oauth_fixture.ORIGIN + '/mcp'})
            self.assertEqual(200, response.status_code, response.text)
            tokens = response.json()
            directory = fixture.directory / 'event-grants'; directory.mkdir(mode=0o700)
            gate = CurrentOAuthGrants(directory, fixture.config['policy'])
            gate.install(fixture.provider, fixture.server, fixture.app)
            principal = {'owner_id': 'synthetic-owner', 'source': 'chatgpt-chat',
                         'subject': 'fixed-owner-main-chat', 'scopes': ['tasks:read']}
            async with fixture.app.router.lifespan_context(fixture.app):
                self.assertFalse(await asyncio.to_thread(gate.account_active, principal))
                unauthorized = await fixture.rpc('NOT_A_BEARER', 'tools/list')
                self.assertEqual(401, unauthorized.status_code)
                self.assertFalse(await asyncio.to_thread(gate.account_active, principal))
                original_calls = len(fixture.idp_requests)
                listed = await fixture.rpc(tokens['access_token'], 'tools/list')
                self.assertEqual(200, listed.status_code, listed.text)
                self.assertTrue(await asyncio.to_thread(gate.account_active, principal))
                # Only identity validation; no /token exchange or business call.
                self.assertTrue(all(url.endswith('/user') for unused_method, url
                                    in fixture.idp_requests[original_calls:]))
                gate.revoke_subject(principal)
                self.assertEqual(200, (await fixture.rpc(tokens['access_token'], 'tools/list')).status_code)
                self.assertFalse(await asyncio.to_thread(gate.account_active, principal))
        finally:
            await fixture.asyncTearDown()

    async def test_real_pinned_engine_registration_rotation_and_revocation(self):
        fixture = oauth_fixture.ActualOAuthTests('test_refresh_uses_component_and_cannot_add_mesh_scopes_or_expose_upstream_tokens')
        await fixture.asyncSetUp()
        try:
            directory = fixture.directory / 'event-grants'; directory.mkdir(mode=0o700)
            gate = CurrentOAuthGrants(directory, fixture.config['policy'])
            gate.install(fixture.provider, fixture.server, fixture.app)
            principal = {'owner_id': 'synthetic-owner', 'source': 'chatgpt-chat',
                         'subject': 'fixed-owner-main-chat', 'scopes': ['tasks:read']}
            async with fixture.app.router.lifespan_context(fixture.app):
                tokens = await fixture.tokens('tasks:read')
                self.assertTrue(await asyncio.to_thread(gate.account_active, principal))
                refreshed = await fixture.http.post('/token', data={'grant_type': 'refresh_token',
                    'client_id': oauth_fixture.CLIENT, 'refresh_token': tokens['refresh_token'], 'scope': 'tasks:read', 'resource': oauth_fixture.ORIGIN + '/mcp'})
                self.assertEqual(200, refreshed.status_code, refreshed.text)
                self.assertTrue(await asyncio.to_thread(gate.account_active, principal))
                client = await fixture.provider.get_client(oauth_fixture.CLIENT)
                refresh = await fixture.provider.load_refresh_token(client, refreshed.json()['refresh_token'])
                await fixture.provider.revoke_token(refresh)
                self.assertFalse(await asyncio.to_thread(gate.account_active, principal))
                # Exact actual engine's no-refresh path: no downstream refresh
                # token and no need to keep the short access JTI alive forever.
                fixture.idp_refresh = False
                code, verifier = await fixture.authorization_code('tasks:read')
                response = await fixture.http.post('/token', data={'grant_type': 'authorization_code',
                    'client_id': oauth_fixture.CLIENT, 'code': code, 'code_verifier': verifier,
                    'redirect_uri': fixture.redirect_uri, 'resource': oauth_fixture.ORIGIN + '/mcp'})
                self.assertEqual(200, response.status_code, response.text)
                no_refresh = response.json()
                self.assertIsNone(no_refresh.get('refresh_token'))
                claims = fixture.provider.jwt_issuer.verify_token(no_refresh['access_token'])
                await fixture.provider._jti_mapping_store.delete(key=claims['jti'])
                self.assertTrue(await asyncio.to_thread(gate.account_active, principal))
        finally:
            await fixture.asyncTearDown()


if __name__ == '__main__':
    unittest.main()
