"""Real pinned OAuth/PKCE/consent/JWT machinery + synthetic IdP transport.

No GitHub App, real account, key, external HTTP or production Store is used.
Optional SDK tests must run in the separate pinned environment; a skip is not
proof that ordinary Chat OAuth works. Pure policy tests remain core-compatible.
"""
import base64
import asyncio
import copy
import functools
import hashlib
import json
import logging
import secrets
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest import mock

from assistant_mesh.chat_github_oauth import create_app, validate_config
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_http import LocalIngressBackend
from assistant_mesh.mcp_http_auth import NotConfigured
from assistant_mesh.store import Store


CLIENT = 'synthetic-approved-chat-client'
ORIGIN = 'http://127.0.0.1:8123'
CALLBACK = 'http://127.0.0.1:9123/callback'
UPSTREAM_TOKEN = 'SYNTHETIC_IDP_OPAQUE_TOKEN_NOT_A_MESH_BEARER'
UPSTREAM_REFRESH = 'SYNTHETIC_IDP_REFRESH_NOT_A_MESH_BEARER'


def fixture(directory, origin='https://mesh.example.test'):
    directory = Path(directory)
    state = directory / 'oauth-state'
    state.mkdir(mode=0o700)
    # Test keys are generated here only for the isolated synthetic authority.
    for name, raw in [('github-secret', b'SYNTHETIC_APP_SECRET_' + b'x' * 40),
                      ('jwt-key', secrets.token_urlsafe(48).encode()),
                      ('encryption-key', base64.urlsafe_b64encode(secrets.token_bytes(32)))]:
        path = directory / name
        path.write_bytes(raw)
        path.chmod(0o600)
    return {'format': 'mesh-chat-github-oauth/1', 'policy': {
        'format': 'mesh-mcp-http/1', 'resource_url': origin + '/mcp', 'issuer_url': origin + '/',
        'issuer_metadata': {'issuer': origin + '/', 'authorization_endpoint': origin + '/authorize',
            'token_endpoint': origin + '/token', 'code_challenge_methods_supported': ['S256'],
            'token_endpoint_auth_methods_supported': ['none']}, 'owner_binding': 'synthetic-owner',
        'subjects': [{'issuer_subject': '12345', 'mesh_subject': 'fixed-owner-main-chat',
                      'client_ids': [CLIENT], 'scopes': ['tasks:read', 'tasks:submit']}]},
        'github': {'client_id': 'SYNTHETIC_UPSTREAM_APP', 'client_secret_file': str(directory / 'github-secret'),
                   'owner_id': '12345'}, 'state': {'directory': str(state),
            'jwt_key_file': str(directory / 'jwt-key'), 'encryption_key_file': str(directory / 'encryption-key')},
        'redirect_uris': ['https://chatgpt.com/connector_platform_oauth_redirect']}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-chat-oauth-policy.')
        self.directory = Path(self.temp.name)
        self.config = fixture(self.directory)

    def tearDown(self):
        self.temp.cleanup()

    def test_detached_private_policy_no_new_server_or_keys(self):
        before = sorted(str(path.relative_to(self.directory)) for path in self.directory.rglob('*'))
        value = validate_config(self.config)
        self.config['policy']['subjects'][0]['scopes'].clear()
        self.assertEqual(['tasks:read', 'tasks:submit'], value['policy']['subjects'][0]['scopes'])
        self.assertEqual(before, sorted(str(path.relative_to(self.directory)) for path in self.directory.rglob('*')))

    def test_wrong_owner_foreign_issuer_extra_github_scope_and_wildcard_client_fail_closed(self):
        mutations = [lambda v: v['github'].update(owner_id='67890'),
            lambda v: v['github'].update(scopes=['repo']),
            lambda v: v['policy'].update(issuer_url='https://foreign.test'),
            lambda v: v['policy']['subjects'][0].update(client_ids=['*']),
            lambda v: v['policy']['subjects'][0]['scopes'].append('operator'),
            lambda v: v['state'].update(directory='/PRIVATE/../elsewhere')]
        for mutation in mutations:
            changed = copy.deepcopy(self.config)
            mutation(changed)
            with self.subTest(change=mutation), self.assertRaises((ValueError, NotConfigured)):
                validate_config(changed)

    def test_no_test_plaintext_arbitrary_redirect_or_scope_escalation_in_public_config(self):
        for redirect in ['https://attacker.test/callback', 'https://chatgpt.com/*',
                         'https://chatgpt.com/connector_platform_oauth_redirect?evil=x', CALLBACK]:
            changed = copy.deepcopy(self.config)
            changed['redirect_uris'] = [redirect]
            with self.subTest(redirect=redirect), self.assertRaises(NotConfigured):
                validate_config(changed)


try:
    from contextlib import AsyncExitStack
    from importlib.metadata import version
    import httpx2
    from mcp.shared.auth import OAuthClientInformationFull
    from fastmcp.server.auth.jwt_issuer import JWTIssuer
    from fastmcp.server.auth.oauth_proxy.upstream import AsyncOAuth2Client
    AVAILABLE = version('fastmcp-slim') == '4.1.0' and version('mcp') == '2.3.0'
except ImportError:
    AVAILABLE = False


class Inputs(HTMLParser):
    def __init__(self, raw):
        super().__init__()
        self.values = {}
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'input' and 'name' in values:
            self.values[values['name']] = values.get('value', '')


def component_lifespan(method):
    @functools.wraps(method)
    async def run(self):
        # AnyIO cancel scopes must enter/exit in the SAME task. unittest's
        # asyncSetUp/test/asyncTearDown are distinct tasks on modern Python.
        async with self.app.router.lifespan_context(self.app):
            return await method(self)
    return run


@unittest.skipUnless(AVAILABLE, 'optional pinned OAuth environment absent; not Chat acceptance')
class ActualOAuthTests(getattr(unittest, 'IsolatedAsyncioTestCase', unittest.TestCase)):
    async def asyncSetUp(self):
        self.stack = AsyncExitStack()
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-chat-oauth-actual.')
        self.directory = Path(self.temp.name)
        self.config = fixture(self.directory, ORIGIN)
        self.config['redirect_uris'] = [CALLBACK]
        self.idp_requests = []
        self.cimd_requests = []
        self.identity_id = 12345
        self.identity_scope = ''
        self.idp_refresh = True
        self.oauth_client_id = CLIENT
        self.redirect_uri = CALLBACK
        self.store = Store(self.directory / 'fresh.sqlite')
        ingress = Ingress(self.store, 'synthetic-authority', {'owner_id': 'synthetic-owner'})
        results = IngressResults(self.store, 'synthetic-authority', {'owner_id': 'synthetic-owner'})
        self.backend = LocalIngressBackend(ingress, results)
        self.real_client = httpx2.AsyncClient
        self.identity_client = await self.stack.enter_async_context(
            self.real_client(transport=httpx2.MockTransport(self.idp), follow_redirects=False))
        self.provider, self.server, self.app = self.make_app()
        self.http = await self.stack.enter_async_context(self.real_client(
            transport=httpx2.ASGITransport(app=self.app), base_url=ORIGIN, follow_redirects=False))
        # Only the upstream HTTP TRANSPORT is synthetic. The actual pinned
        # OAuth client/proxy parses exchanges, manages state and signs JWTs.
        self.stack.enter_context(mock.patch(
            'fastmcp.server.auth.oauth_proxy.upstream.httpx2.AsyncClient',
            side_effect=lambda **kwargs: self.real_client(transport=httpx2.MockTransport(self.idp), **kwargs)))
        client = OAuthClientInformationFull(client_id=CLIENT, redirect_uris=[CALLBACK],
            grant_types=['authorization_code', 'refresh_token'], token_endpoint_auth_method='none',
            application_type='native', scope='tasks:read tasks:submit')
        await self.provider.register_client(client)

    async def asyncTearDown(self):
        await self.stack.aclose()
        self.temp.cleanup()

    def make_app(self):
        return create_app(self.config, backend=self.backend, http_client=self.identity_client,
                          allow_loopback_for_tests=True)

    def idp(self, request):
        self.idp_requests.append((request.method, str(request.url)))
        if request.headers.get('host') == 'chatgpt.com' and request.url.path == '/oauth/client.json':
            self.cimd_requests.append((request.method, request.headers['host'], request.url.path))
            return httpx2.Response(200, json={'client_id': 'https://chatgpt.com/oauth/client.json',
                'client_name': 'Synthetic CIMD client, NOT a real Chat connection',
                'redirect_uris': [self.redirect_uri], 'token_endpoint_auth_method': 'none',
                'application_type': 'web', 'grant_types': ['authorization_code', 'refresh_token'],
                'response_types': ['code']})
        if str(request.url) == 'https://api.github.com/user':
            return httpx2.Response(200, headers={'X-OAuth-Scopes': self.identity_scope}, json={
                'id': self.identity_id, 'login': 'mutable-display-label-not-authority',
                'email': 'SYNTHETIC_NOT_EXPORTED', 'unneeded_profile_field': 'NOT_EXPORTED'})
        if str(request.url) == 'https://github.com/login/oauth/access_token':
            self.assertEqual('POST', request.method)
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            self.assertNotIn('tasks:', form.get('scope', [''])[0])
            self.assertNotIn('repo', form.get('scope', [''])[0])
            self.assertNotIn('offline_access', form.get('scope', [''])[0])
            self.assertIn(form['grant_type'][0], ('authorization_code', 'refresh_token'))
            value = {'access_token': UPSTREAM_TOKEN, 'token_type': 'Bearer', 'scope': ''}
            if self.idp_refresh:
                value.update(expires_in=3600, refresh_token=UPSTREAM_REFRESH, refresh_expires_in=7200)
            return httpx2.Response(200, json=value)
        raise AssertionError('unexpected synthetic IdP endpoint; no repo/email/network calls allowed')

    async def consent_start(self, scopes='tasks:read tasks:submit'):
        verifier = 'SYNTHETIC_CLIENT_PKCE_' + 'x' * 48
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        started = await self.http.get('/authorize', params={'client_id': self.oauth_client_id,
            'redirect_uri': self.redirect_uri, 'response_type': 'code', 'scope': scopes,
            'code_challenge': challenge, 'code_challenge_method': 'S256', 'state': 'original-client-state',
            'resource': ORIGIN + '/mcp'})
        self.assertIn(started.status_code, (302, 303), started.text)
        consent = await self.http.get(started.headers['location'])
        self.assertEqual(200, consent.status_code)
        fields = Inputs(consent.text).values
        return verifier, fields

    async def authorization_code(self, scopes='tasks:read tasks:submit'):
        verifier, fields = await self.consent_start(scopes)
        approved = await self.http.post('/consent', data={'txn_id': fields['txn_id'],
            'csrf_token': fields['csrf_token'], 'action': 'approve'}, headers={'Origin': ORIGIN})
        self.assertIn(approved.status_code, (302, 303), approved.text)
        upstream = parse_qs(urlsplit(approved.headers['location']).query, keep_blank_values=True)
        self.assertEqual([''], upstream['scope'])
        self.assertEqual([ORIGIN + '/auth/callback'], upstream['redirect_uri'])
        self.assertEqual(['S256'], upstream['code_challenge_method'])
        callback = await self.http.get('/auth/callback', params={
            'state': upstream['state'][0], 'code': 'SYNTHETIC_UPSTREAM_AUTHORIZATION_CODE'})
        self.assertIn(callback.status_code, (302, 303), callback.text)
        downstream = parse_qs(urlsplit(callback.headers['location']).query)
        self.assertEqual(['original-client-state'], downstream['state'])
        self.assertEqual([ORIGIN + '/'], downstream['iss'])
        return downstream['code'][0], verifier

    async def tokens(self, scopes='tasks:read tasks:submit'):
        code, verifier = await self.authorization_code(scopes)
        response = await self.http.post('/token', data={'grant_type': 'authorization_code', 'client_id': self.oauth_client_id,
            'code': code, 'code_verifier': verifier, 'redirect_uri': self.redirect_uri, 'resource': ORIGIN + '/mcp'})
        self.assertEqual(200, response.status_code, response.text)
        value = response.json()
        self.assertNotEqual(UPSTREAM_TOKEN, value['access_token'])
        self.assertNotEqual(UPSTREAM_REFRESH, value['refresh_token'])
        self.assertEqual(set(scopes.split()), set(value['scope'].split()))
        return value

    async def rpc(self, token, method, params=None, http=None):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': method}
        if params is not None:
            body['params'] = params
        return await (http or self.http).post('/mcp', json=body, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-11-25'})

    @component_lifespan
    async def test_actual_discovery_pkce_consent_own_signed_token_three_original_tools_and_once_ingress(self):
        meta = (await self.http.get('/.well-known/oauth-authorization-server')).json()
        self.assertIn('S256', meta['code_challenge_methods_supported'])
        self.assertTrue(meta['client_id_metadata_document_supported'])
        self.assertTrue(meta['authorization_response_iss_parameter_supported'])
        self.assertIn('none', meta['token_endpoint_auth_methods_supported'])
        tokens = await self.tokens()
        signed = self.provider.jwt_issuer.verify_token(tokens['access_token'])
        self.assertEqual(ORIGIN + '/mcp', signed['aud'])
        self.assertEqual(CLIENT, signed['client_id'])
        self.assertNotIn('SYNTHETIC_NOT_EXPORTED', json.dumps(signed))
        tools = await self.rpc(tokens['access_token'], 'tools/list')
        self.assertEqual(200, tools.status_code, tools.text)
        self.assertEqual({'mesh_submit_task', 'mesh_task_status', 'mesh_approved_result'},
                         {item['name'] for item in tools.json()['result']['tools']})
        args = {'name': 'mesh_submit_task', 'arguments': {'request_id': 'original-owner-request', 'input': 'SYNTHETIC_TASK'}}
        first = await self.rpc(tokens['access_token'], 'tools/call', args)
        second = await self.rpc(tokens['access_token'], 'tools/call', args)
        self.assertTrue(first.json()['result']['structuredContent']['data']['task_created'])
        self.assertFalse(second.json()['result']['structuredContent']['data']['task_created'])
        with self.store.transaction() as db:
            row = db.execute('SELECT source,subject,request_id FROM ingress_requests').fetchone()
            self.assertEqual(('chatgpt-chat', 'fixed-owner-main-chat', 'original-owner-request'), tuple(row))

    @component_lifespan
    async def test_missing_or_wrong_pkce_never_exchanges_the_original_code(self):
        code, verifier = await self.authorization_code()
        wrong = await self.http.post('/token', data={'grant_type': 'authorization_code', 'client_id': CLIENT,
            'code': code, 'code_verifier': 'wrong-verifier', 'redirect_uri': CALLBACK, 'resource': ORIGIN + '/mcp'})
        self.assertIn(wrong.status_code, (400, 401), wrong.text)
        self.assertEqual('invalid_grant', wrong.json()['error'])

    @component_lifespan
    async def test_restart_reuses_encrypted_state_and_real_signed_token_not_ephemeral_keys(self):
        tokens = await self.tokens()
        provider, server, app = self.make_app()
        client = await self.stack.enter_async_context(self.real_client(
            transport=httpx2.ASGITransport(app=app), base_url=ORIGIN))
        self.assertIsNotNone(await provider.get_client(CLIENT))
        async with app.router.lifespan_context(app):
            result = await self.rpc(tokens['access_token'], 'tools/list', http=client)
        self.assertEqual(200, result.status_code, result.text)
        raw = b'\n'.join(path.read_bytes() for path in Path(self.config['state']['directory']).rglob('*') if path.is_file())
        self.assertNotIn(UPSTREAM_TOKEN.encode(), raw)
        self.assertNotIn(UPSTREAM_REFRESH.encode(), raw)

    @component_lifespan
    async def test_refresh_uses_component_and_cannot_add_mesh_scopes_or_expose_upstream_tokens(self):
        tokens = await self.tokens('tasks:read')
        refreshed = await self.http.post('/token', data={'grant_type': 'refresh_token', 'client_id': CLIENT,
            'refresh_token': tokens['refresh_token'], 'scope': 'tasks:read', 'resource': ORIGIN + '/mcp'})
        self.assertEqual(200, refreshed.status_code, refreshed.text)
        self.assertEqual('tasks:read', refreshed.json()['scope'])
        self.assertNotEqual(UPSTREAM_TOKEN, refreshed.json()['access_token'])
        widened = await self.http.post('/token', data={'grant_type': 'refresh_token', 'client_id': CLIENT,
            'refresh_token': refreshed.json()['refresh_token'], 'scope': 'tasks:read tasks:submit', 'resource': ORIGIN + '/mcp'})
        self.assertEqual(400, widened.status_code)

    @component_lifespan
    async def test_bad_signature_foreign_resource_issuer_client_refresh_type_and_github_bearer_denied(self):
        tokens = await self.tokens()
        original = tokens['access_token']
        claims = self.provider.jwt_issuer.verify_token(original)
        key = (self.directory / 'jwt-key').read_bytes()
        foreign_resource = JWTIssuer(ORIGIN + '/', 'https://foreign.test/mcp', key).issue_access_token(
            CLIENT, ['tasks:read'], claims['jti'])
        foreign_issuer = JWTIssuer('https://foreign.test', ORIGIN + '/mcp', key).issue_access_token(
            CLIENT, ['tasks:read'], claims['jti'])
        foreign_client = self.provider.jwt_issuer.issue_access_token('unapproved-client', ['tasks:read'], claims['jti'])
        expired = self.provider.jwt_issuer.issue_access_token(CLIENT, ['tasks:read'], claims['jti'],
            extra_claims={'exp': int(time.time()) - 60})
        for token in (original + 'broken', foreign_resource, foreign_issuer, foreign_client,
                      expired, tokens['refresh_token'], UPSTREAM_TOKEN):
            with self.subTest(case=token[:8]):
                response = await self.rpc(token, 'tools/list')
                self.assertEqual(401, response.status_code)
        self.identity_id = 67890
        self.assertEqual(401, (await self.rpc(original, 'tools/list')).status_code)
        self.identity_id = 12345
        self.identity_scope = 'repo'
        self.assertEqual(401, (await self.rpc(original, 'tools/list')).status_code)

    @component_lifespan
    async def test_dcr_is_supported_but_unapproved_client_and_foreign_cimd_never_grant_mesh(self):
        registered = await self.http.post('/register', json={'redirect_uris': [CALLBACK],
            'application_type': 'native', 'grant_types': ['authorization_code'],
            'token_endpoint_auth_method': 'none', 'scope': 'tasks:read'})
        self.assertEqual(201, registered.status_code, registered.text)
        client_id = registered.json()['client_id']
        self.assertIsNone(await self.provider.get_client(client_id))
        before = list(self.idp_requests)
        self.assertIsNone(await self.provider.get_client('https://attacker.test/client.json'))
        self.assertEqual(before, self.idp_requests)

    @component_lifespan
    async def test_real_consent_requires_same_browser_binding_not_just_form_csrf(self):
        verifier, fields = await self.consent_start()
        other = await self.stack.enter_async_context(self.real_client(
            transport=httpx2.ASGITransport(app=self.app), base_url=ORIGIN))
        denied = await other.post('/consent', data={'txn_id': fields['txn_id'],
            'csrf_token': fields['csrf_token'], 'action': 'approve'}, headers={'Origin': ORIGIN})
        self.assertEqual(403, denied.status_code)
        self.assertEqual([], self.idp_requests)

    @component_lifespan
    async def test_original_authorization_code_cannot_be_exchanged_twice_concurrently(self):
        code, verifier = await self.authorization_code()
        async def exchange():
            return await self.http.post('/token', data={'grant_type': 'authorization_code', 'client_id': CLIENT,
                'code': code, 'code_verifier': verifier, 'redirect_uri': CALLBACK, 'resource': ORIGIN + '/mcp'})
        replies = await asyncio.gather(exchange(), exchange())
        self.assertEqual(1, sum(response.status_code == 200 for response in replies),
                         [response.status_code for response in replies])

    @component_lifespan
    async def test_owner_secret_permissions_separate_keys_and_private_auth_logs(self):
        logger = logging.getLogger('fastmcp.server.auth.oauth_proxy.proxy')
        with self.assertLogs(logger, level='ERROR') as output:
            logger.error('private code=%s token=%s', 'SYNTHETIC_PRIVATE_CODE', UPSTREAM_TOKEN)
        self.assertNotIn('SYNTHETIC_PRIVATE_CODE', '\n'.join(output.output))
        self.assertNotIn(UPSTREAM_TOKEN, '\n'.join(output.output))
        changed = copy.deepcopy(self.config)
        changed['state']['jwt_key_file'] = changed['github']['client_secret_file']
        with self.assertRaisesRegex(NotConfigured, 'distinct_private_keys'):
            create_app(changed, backend=self.backend, allow_loopback_for_tests=True)
        (self.directory / 'github-secret').chmod(0o644)
        with self.assertRaisesRegex(NotConfigured, 'secret_unavailable'):
            self.make_app()

    @component_lifespan
    async def test_github_common_no_expiry_no_refresh_shape_requires_reauth_after_short_access_token(self):
        self.idp_refresh = False
        code, verifier = await self.authorization_code()
        response = await self.http.post('/token', data={'grant_type': 'authorization_code', 'client_id': CLIENT,
            'code': code, 'code_verifier': verifier, 'redirect_uri': CALLBACK, 'resource': ORIGIN + '/mcp'})
        self.assertEqual(200, response.status_code, response.text)
        tokens = response.json()
        self.assertFalse(tokens.get('refresh_token'))
        self.assertEqual(900, tokens['expires_in'])
        signed = self.provider.jwt_issuer.verify_token(tokens['access_token'])
        self.assertEqual(900, signed['exp'] - signed['iat'])
        self.assertEqual(200, (await self.rpc(tokens['access_token'], 'tools/list')).status_code)

    @component_lifespan
    async def test_original_code_is_not_consumed_by_wrong_or_missing_token_resource(self):
        code, verifier = await self.authorization_code()
        form = {'grant_type': 'authorization_code', 'client_id': CLIENT, 'code': code,
                'code_verifier': verifier, 'redirect_uri': CALLBACK}
        for resource in (None, 'https://foreign.test/mcp'):
            payload = dict(form)
            if resource is not None:
                payload['resource'] = resource
            denied = await self.http.post('/token', data=payload)
            self.assertEqual(400, denied.status_code)
            self.assertEqual('invalid_target', denied.json()['error'])
        duplicate = await self.http.post('/token', content=urlencode(list(form.items()) + [
            ('resource', ORIGIN + '/mcp'), ('resource', 'https://foreign.test/mcp')]),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(400, duplicate.status_code)
        oversized = await self.http.post('/token', content=b'x' * 65537,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(413, oversized.status_code)
        preflight = await self.http.options('/token', headers={
            'Origin': 'https://chatgpt.com', 'Access-Control-Request-Method': 'POST'})
        self.assertEqual(200, preflight.status_code)
        accepted = await self.http.post('/token', data=dict(form, resource=ORIGIN + '/mcp'))
        self.assertEqual(200, accepted.status_code)
        refresh = accepted.json()['refresh_token']
        rejected = await self.http.post('/token', data={'grant_type': 'refresh_token', 'client_id': CLIENT,
            'refresh_token': refresh, 'scope': 'tasks:read', 'resource': 'https://foreign.test/mcp'})
        self.assertEqual(400, rejected.status_code)

    @component_lifespan
    async def test_pinned_cimd_document_uses_actual_component_flow_without_dcr_or_github_scope(self):
        self.oauth_client_id = 'https://chatgpt.com/oauth/client.json'
        self.redirect_uri = 'https://chatgpt.com/connector_platform_oauth_redirect'
        self.config['policy']['subjects'][0]['client_ids'] = [self.oauth_client_id]
        self.config['redirect_uris'] = [self.redirect_uri]
        self.provider, self.server, app = self.make_app()
        self.http = await self.stack.enter_async_context(self.real_client(
            transport=httpx2.ASGITransport(app=app), base_url=ORIGIN, follow_redirects=False))
        async with app.router.lifespan_context(app):
            # Synthetic DNS + HTTP transport only; retain the actual component's
            # URL/IP validation, Host/SNI pinning, document parse and protocol.
            with mock.patch('fastmcp.server.auth.ssrf.resolve_hostname',
                            new=mock.AsyncMock(return_value=['93.184.216.34'])):
                tokens = await self.tokens()
            claims = self.provider.jwt_issuer.verify_token(tokens['access_token'])
            self.assertEqual(self.oauth_client_id, claims['client_id'])
            self.assertEqual(200, (await self.rpc(tokens['access_token'], 'tools/list')).status_code)
        self.assertIn(('GET', 'chatgpt.com', '/oauth/client.json'), self.cimd_requests)


if __name__ == '__main__':
    unittest.main()
