"""Optional HTTP SDK tests in an isolated modern environment, no real OAuth.

Pure business/config tests run without the SDK. Explicit SDK-only tests report a
skip when its separate dependency environment is absent; those skips are not
HTTP/Chat acceptance. Loopback uses actual SDK ASGI/TCP, synthetic IdP tokens.
"""
import asyncio
import copy
import hashlib
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_http import LocalIngressBackend, RemoteIngressBackend, create_server, dispatch
from assistant_mesh.mcp_http_auth import IdPJWTVerifier, NotConfigured, OwnerTokenVerifier, validate_policy
from assistant_mesh.store import Store


ISSUER = 'https://idp.example.test'
RESOURCE = 'https://mesh.example.test/mcp'


def policy(resource=RESOURCE):
    return {'format': 'mesh-mcp-http/1', 'resource_url': resource, 'issuer_url': ISSUER,
            'issuer_metadata': {'issuer': ISSUER, 'authorization_endpoint': ISSUER + '/authorize',
                                'token_endpoint': ISSUER + '/token',
                                'code_challenge_methods_supported': ['S256'],
                                'token_endpoint_auth_methods_supported': ['none']},
            'owner_binding': 'owner-fixture', 'subjects': [
                {'issuer_subject': 'subject-owner', 'mesh_subject': 'owner-main-chat',
                 'client_ids': ['chatgpt-fixture-client'], 'scopes': ['tasks:read', 'tasks:submit']},
                {'issuer_subject': 'subject-other', 'mesh_subject': 'owner-other-chat',
                 'client_ids': ['chatgpt-fixture-client'], 'scopes': ['tasks:read', 'tasks:submit']}]}


def token(**changes):
    base = {'claims': {'iss': ISSUER}, 'resource': RESOURCE, 'expires_at': int(time.time()) + 600,
            'subject': 'subject-owner', 'client_id': 'chatgpt-fixture-client',
            'scopes': ['tasks:read', 'tasks:submit']}
    base.update(changes)
    return SimpleNamespace(**base)


class Delegate:
    async def verify_token(self, raw):
        raise AssertionError('no real IdP call intended')


class PolicyAndBusinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-mcp-http-business.')
        self.store = Store(Path(self.temp.name) / 'fresh.sqlite')
        self.ingress = Ingress(self.store, 'authority-fixture', {'owner_id': 'owner-fixture'})
        self.results = IngressResults(self.store, 'authority-fixture', {'owner_id': 'owner-fixture'})
        self.backend = LocalIngressBackend(self.ingress, self.results)
        self.verifier = OwnerTokenVerifier(Delegate(), validate_policy(policy()))

    def tearDown(self):
        self.temp.cleanup()

    def call(self, action, payload, auth=None, backend=None):
        return dispatch(backend or self.backend, self.verifier, auth or token(), action, payload)

    def test_policy_is_detached_and_missing_idp_is_not_configured(self):
        config = policy()
        parsed = validate_policy(config)
        config['subjects'][0]['scopes'].clear()
        self.assertEqual(['tasks:read', 'tasks:submit'], parsed['subjects'][0]['scopes'])
        with self.assertRaises(NotConfigured):
            validate_policy(None)
        with self.assertRaises(NotConfigured):
            OwnerTokenVerifier(None, parsed)
        with self.assertRaises(NotConfigured):
            create_server(policy(), token_verifier=Delegate())

    def test_policy_checks_existing_idp_pkce_and_exact_issuer(self):
        for change in ({'code_challenge_methods_supported': ['plain']}, {'issuer': ISSUER + '/'}):
            changed = policy()
            changed['issuer_metadata'].update(change)
            with self.subTest(change=change), self.assertRaises(NotConfigured):
                validate_policy(changed)

    def test_config_cannot_install_operator_scopes_or_choose_source(self):
        for change in ({'source': 'operator'}, {'resource_url': 'http://mesh.example.test/mcp'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_policy(dict(policy(), **change))
        changed = policy()
        changed['subjects'][0]['scopes'].append('operator')
        with self.assertRaises(ValueError):
            validate_policy(changed)
        changed = policy()
        changed['subjects'][0]['client_ids'] = ['*']
        with self.assertRaises(ValueError):
            validate_policy(changed)

    def test_issuer_audience_expiry_subject_client_and_scopes_all_bind(self):
        invalid = [token(claims={'iss': 'https://other.test'}), token(resource=RESOURCE + '/'),
                   token(expires_at=int(time.time()) - 1), token(expires_at=None),
                   token(subject='foreign'), token(client_id='unapproved'), token(scopes=['operator'])]
        for auth in invalid:
            with self.subTest(field_changes=auth.__dict__), self.assertRaises(PermissionError):
                self.verifier.principal(auth)
        principal = self.verifier.principal(token(scopes=['tasks:read', 'operator']))
        self.assertEqual(['tasks:read'], principal['scopes'])
        self.assertEqual('chatgpt-chat', principal['source'])

    def test_three_tools_use_same_owner_request_and_do_not_change_native_scope(self):
        args = {'request_id': 'request-one', 'input': 'Create new mesh documentation', 'project_id': 'mesh-project'}
        first = self.call('submit', args)
        second = self.call('submit', args)
        self.assertTrue(first['ok'])
        self.assertTrue(first['data']['task_created'])
        self.assertFalse(second['data']['task_created'])
        self.assertEqual(first['data']['task_id'], second['data']['task_id'])
        status = self.call('status', {'request_id': 'request-one'})
        self.assertEqual(first['data']['task_id'], status['data']['task_id'])
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (first['data']['task_id'],)).fetchone()
            self.assertEqual('leader:owner', row['scope'])
            self.assertEqual('chatgpt-chat', json.loads(row['context'])['ingress']['source'])
            self.assertEqual('mesh-project', json.loads(row['context'])['project_id'])

    def test_readonly_scope_does_not_submit_and_foreign_source_cannot_read(self):
        self.call('submit', {'request_id': 'request-one', 'input': 'new owner task'})
        rejected = self.call('submit', {'request_id': 'request-two', 'input': 'other'}, token(scopes=['tasks:read']))
        self.assertEqual('not_attempted', rejected['outcome'])
        self.assertEqual('insufficient_scope', rejected['error'])
        foreign = self.call('status', {'request_id': 'request-one'}, token(subject='subject-other'))
        self.assertFalse(foreign['ok'])
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])

    def test_no_operator_peer_fields_raw_shell_or_global_task_lookup(self):
        for action, args in [('shell', {'command': 'pwd'}), ('status', {'task_id': 'global'}),
                             ('submit', {'request_id': 'a', 'input': 'b', 'role': 'operator'}),
                             ('submit', {'request_id': 'a', 'input': 'b', 'scope': 'native:foreign'})]:
            with self.subTest(action=action, fields=sorted(args)):
                self.assertFalse(self.call(action, args)['ok'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])

    def test_only_owner_approved_publication_not_raw_results_is_returned(self):
        receipt = self.call('submit', {'request_id': 'request-one', 'input': 'new task'})
        self.store.heartbeat('fixture-worker', ['leader'])
        task = self.store.claim('fixture-worker')
        self.store.update_task(task['id'], 'fixture-worker', task['epoch'],
                               {'side_effect_started': False}, 'PRIVATE_RAW_NATIVE_RESULT', 'completed')
        self.assertFalse(self.call('result', {'request_id': 'request-one'})['ok'])
        self.results.publish({'role': 'operator'}, {
            'publication_id': 'owner-review-one', 'source': 'chatgpt-chat', 'subject': 'owner-main-chat',
            'request_id': 'request-one', 'task_id': receipt['data']['task_id'], 'task_epoch': task['epoch'],
            'task_result_sha256': hashlib.sha256(b'PRIVATE_RAW_NATIVE_RESULT').hexdigest(), 'reviewed': True,
            'result': {'summary': 'Owner approved public summary', 'artifacts': []}})
        approved = self.call('result', {'request_id': 'request-one'})
        self.assertTrue(approved['ok'])
        self.assertNotIn('PRIVATE_RAW', json.dumps(approved))
        self.assertFalse(approved['data']['execution_verified'])

    def test_bad_or_lost_authority_reply_never_retries_or_echoes_secrets(self):
        calls = []
        class Backend:
            def request(self, *args):
                calls.append(args)
                raise RuntimeError('PRIVATE_BEARER_AND_BACKEND_URL')
        result = self.call('submit', {'request_id': 'stable-one', 'input': 'new work'}, backend=Backend())
        self.assertEqual(1, len(calls))
        self.assertEqual('unknown', result['outcome'])
        self.assertFalse(result['retry_with_new_id'])
        self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_remote_backend_routes_are_fixed_and_oauth_token_is_not_forwarded(self):
        calls = []
        class Client:
            def request(self, route, payload):
                calls.append((route, payload))
                return {}
        remote = RemoteIngressBackend({'owner-main-chat': Client()}, 'owner-fixture')
        principal = self.verifier.principal(token())
        remote.request(principal, 'status', {'request_id': 'original'})
        self.assertEqual([('/v1/ingress/task/status', {'request_id': 'original'})], calls)
        with self.assertRaises(PermissionError):
            remote.request(principal, 'operator', {})


try:
    import mcp
    import uvicorn
    import jwt
    from mcp.server.auth.provider import AccessToken
    from importlib.metadata import version
    SDK_AVAILABLE = version('mcp') == '2.3.0'
except ImportError:
    SDK_AVAILABLE = False


@unittest.skipUnless(SDK_AVAILABLE, 'isolated modern MCP SDK 2.3.0 environment is not installed')
class ActualSDKHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-mcp-http-actual-sdk.')
        self.store = Store(Path(self.temp.name) / 'fresh.sqlite')
        self.ingress = Ingress(self.store, 'authority-fixture', {'owner_id': 'owner-fixture'})
        self.results = IngressResults(self.store, 'authority-fixture', {'owner_id': 'owner-fixture'})
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        self.resource = 'http://127.0.0.1:{}/mcp'.format(sock.getsockname()[1])
        self.config = policy(self.resource)
        valid = AccessToken(token='SYNTHETIC_VALID', client_id='chatgpt-fixture-client', scopes=['tasks:read', 'tasks:submit'],
                            expires_at=int(time.time()) + 600, resource=self.resource,
                            subject='subject-owner', claims={'iss': ISSUER})
        self.tokens = {
            'SYNTHETIC_VALID': valid,
            'SYNTHETIC_READ': valid.model_copy(update={'scopes': ['tasks:read']}),
            'SYNTHETIC_ISSUER': valid.model_copy(update={'claims': {'iss': ISSUER + '/wrong'}}),
            'SYNTHETIC_AUDIENCE': valid.model_copy(update={'resource': 'https://another.test/mcp'}),
            'SYNTHETIC_SCOPE': valid.model_copy(update={'scopes': ['operator']}),
            'SYNTHETIC_OTHER': valid.model_copy(update={'subject': 'subject-other'}),
        }
        tokens = self.tokens
        class FixtureVerifier:
            async def verify_token(self, raw):
                return tokens.get(raw)
        self.sdk, app = create_server(self.config, token_verifier=FixtureVerifier(),
                                     backend=LocalIngressBackend(self.ingress, self.results),
                                     allow_loopback_for_tests=True)
        self.http_server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False,
                                                       lifespan='on', timeout_graceful_shutdown=3))
        self.thread = threading.Thread(target=lambda: asyncio.run(self.http_server.serve(sockets=[sock])), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.http_server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.http_server.started, 'isolated SDK listener failed to start')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.http_server.should_exit = True
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive(), 'owned loopback listener did not stop')
        self.temp.cleanup()

    def http(self, path='/mcp', payload=None, bearer='SYNTHETIC_VALID', headers=None):
        request_headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                           'MCP-Protocol-Version': '2025-11-25'}
        if bearer is not None:
            request_headers['Authorization'] = 'Bearer ' + bearer
        request_headers.update(headers or {})
        url = self.resource[:-4] + path
        req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(), headers=request_headers)
        try:
            with self.opener.open(req, timeout=5) as result:
                raw = result.read()
                return result.status, dict(result.headers), json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            try:
                return error.code, dict(error.headers), error.read().decode()
            finally:
                error.close()

    @staticmethod
    def rpc(method, params=None, request_id=1):
        value = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
        if params is not None:
            value['params'] = params
        return value

    def test_real_sdk_initialization_discovery_three_tools_and_original_request(self):
        initialized = self.http(payload=self.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'mesh-synthetic-loopback-client', 'version': '1'}}))
        self.assertEqual(200, initialized[0])
        self.assertEqual('2025-11-25', initialized[2]['result']['protocolVersion'])
        tools = self.http(payload=self.rpc('tools/list'))
        self.assertEqual({'mesh_submit_task', 'mesh_task_status', 'mesh_approved_result'},
                         {tool['name'] for tool in tools[2]['result']['tools']})
        for descriptor in tools[2]['result']['tools']:
            self.assertEqual(descriptor['securitySchemes'], descriptor['_meta']['securitySchemes'])
        args = {'request_id': 'same-original-http-request', 'input': 'Synthetic documentation work'}
        submitted = self.http(payload=self.rpc('tools/call', {'name': 'mesh_submit_task', 'arguments': args}))[2]['result']
        self.assertFalse(submitted['isError'])
        receipt = submitted['structuredContent']['data']
        status = self.http(payload=self.rpc('tools/call', {'name': 'mesh_task_status',
            'arguments': {'request_id': args['request_id']}}))[2]['result']['structuredContent']
        self.assertEqual(receipt['task_id'], status['data']['task_id'])
        self.assertFalse(self.http(payload=self.rpc('tools/call', {'name': 'mesh_submit_task', 'arguments': args}))[2]
                         ['result']['structuredContent']['data']['task_created'])
        self.store.heartbeat('fixture-worker', ['leader'])
        task = self.store.claim('fixture-worker')
        self.store.update_task(task['id'], 'fixture-worker', task['epoch'],
                               {'side_effect_started': False}, 'PRIVATE_HTTP_NATIVE_RESULT', 'completed')
        self.results.publish({'role': 'operator'}, {
            'publication_id': 'http-owner-reviewed', 'source': 'chatgpt-chat', 'subject': 'owner-main-chat',
            'request_id': args['request_id'], 'task_id': task['id'], 'task_epoch': task['epoch'],
            'task_result_sha256': hashlib.sha256(b'PRIVATE_HTTP_NATIVE_RESULT').hexdigest(), 'reviewed': True,
            'result': {'summary': 'Owner approved HTTP summary', 'artifacts': []}})
        approved = self.http(payload=self.rpc('tools/call', {'name': 'mesh_approved_result',
            'arguments': {'request_id': args['request_id']}}))[2]['result']
        self.assertFalse(approved['isError'])
        self.assertEqual('Owner approved HTTP summary', approved['structuredContent']['data']['result']['summary'])
        self.assertEqual(receipt['task_id'], approved['structuredContent']['data']['task_id'])
        self.assertNotIn('PRIVATE_HTTP_NATIVE_RESULT', json.dumps(approved))

    def test_sdk_rejects_unauth_wrong_issuer_audience_scope_and_advertises_metadata(self):
        for bearer in (None, 'SYNTHETIC_INVALID', 'SYNTHETIC_ISSUER', 'SYNTHETIC_AUDIENCE', 'SYNTHETIC_SCOPE'):
            with self.subTest(token_kind=bearer):
                status, headers, body = self.http(payload=self.rpc('tools/list'), bearer=bearer)
                self.assertEqual(401, status)
                self.assertIn('resource_metadata=', {k.lower(): v for k, v in headers.items()}['www-authenticate'])
                self.assertNotIn('SYNTHETIC', body)
        status, _, metadata = self.http('/.well-known/oauth-protected-resource/mcp', bearer=None)
        self.assertEqual(200, status)
        self.assertEqual(self.resource, metadata['resource'])
        self.assertEqual([ISSUER], metadata['authorization_servers'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])

    def test_sdk_readonly_write_and_foreign_request_are_rejected(self):
        args = {'name': 'mesh_submit_task', 'arguments': {'request_id': 'original', 'input': 'new task'}}
        denied = self.http(payload=self.rpc('tools/call', args), bearer='SYNTHETIC_READ')[2]['result']
        self.assertTrue(denied['isError'])
        self.assertEqual('insufficient_scope', denied['structuredContent']['error'])
        challenge = denied['_meta']['mcp/www_authenticate'][0]
        self.assertIn('error="insufficient_scope"', challenge)
        self.assertIn('scope="tasks:read tasks:submit"', challenge)
        self.http(payload=self.rpc('tools/call', args))
        foreign = self.http(payload=self.rpc('tools/call', {'name': 'mesh_task_status',
            'arguments': {'request_id': 'original'}}), bearer='SYNTHETIC_OTHER')[2]['result']
        self.assertTrue(foreign['isError'])

    def test_sdk_host_origin_and_body_limits_not_disabled(self):
        self.assertEqual(421, self.http(payload=self.rpc('tools/list'), headers={'Host': 'attacker.example'})[0])
        self.assertEqual(403, self.http(payload=self.rpc('tools/list'), headers={'Origin': 'https://attacker.example'})[0])
        self.assertEqual(413, self.http(payload=self.rpc('ping', {'large': 'x' * 70000}))[0])

    def test_actual_modern_sdk_client_discovers_calls_and_rejects_unknown_arguments(self):
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared.exceptions import MCPError
        import httpx2
        async def actual_client():
            async with httpx2.AsyncClient(headers={'Authorization': 'Bearer SYNTHETIC_VALID'},
                                          timeout=5, trust_env=False) as http_client:
                transport = streamable_http_client(self.resource, http_client=http_client)
                async with Client(transport, read_timeout_seconds=5) as client:
                    self.assertEqual('2026-07-28', client.protocol_version)
                    self.assertEqual(3, len((await client.list_tools()).tools))
                    reply = await client.call_tool('mesh_submit_task',
                                                  {'request_id': 'modern-original', 'input': 'new scoped task'})
                    self.assertFalse(reply.is_error)
                    self.assertTrue(reply.structured_content['data']['task_created'])
                    with self.assertRaises(MCPError) as denied:
                        await client.call_tool('mesh_submit_task',
                            {'request_id': 'modern-injection', 'input': 'new task', 'role': 'operator'})
                    self.assertEqual(-32602, denied.exception.code)
        asyncio.run(actual_client())

    def test_real_jwt_signature_issuer_and_audience_validation_without_idp_calls(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        verifier = IdPJWTVerifier(issuer_url=ISSUER, resource_url=RESOURCE,
                                 jwks_url=ISSUER + '/.well-known/jwks.json')
        # Synthetic in-memory key provider; no login/key creation at any IdP.
        verifier.keys = SimpleNamespace(get_signing_key_from_jwt=lambda raw: SimpleNamespace(key=key.public_key()))
        claims = {'iss': ISSUER, 'aud': RESOURCE, 'sub': 'subject-owner', 'client_id': 'chatgpt-fixture-client',
                  'exp': int(time.time()) + 600, 'scope': 'tasks:read tasks:submit'}
        valid = jwt.encode(claims, key, algorithm='RS256')
        self.assertEqual('subject-owner', asyncio.run(verifier.verify_token(valid)).subject)
        for change in ({'iss': ISSUER + '/wrong'}, {'aud': 'https://another.test'}, {'exp': int(time.time()) - 1}):
            with self.subTest(claim_change=list(change)):
                raw = jwt.encode(dict(claims, **change), key, algorithm='RS256')
                self.assertIsNone(asyncio.run(verifier.verify_token(raw)))
        self.assertIsNone(asyncio.run(verifier.verify_token(valid[:-8] + 'AAAAAAAA')))


if __name__ == '__main__':
    unittest.main()
