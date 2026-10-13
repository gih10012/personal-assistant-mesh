"""Real bounded loopback transport; fake provider CLI, not official Cloud.

Tests explicitly model an owner-installed local TLS reverse proxy. No public
service, model request, login, paid key or production ledger is touched.
"""
import base64
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

from assistant_mesh.cloud_http import CloudHTTPClient, CloudTransportError, make_http_handler
from assistant_mesh.cloud_jobs import CloudJobs, CommandResult
from assistant_mesh.cloud_membership import CloudMembership, PROTOCOL, REQUEST_PROTOCOL, node_id


TOKEN = 'SYNTHETIC_CLOUD_NODE_CREDENTIAL_' + 'a' * 40
RESOURCE = 'https://mesh.example.test/cloud/node'


class CloudHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-cloud-http.')
        self.now = 1000
        self.effects = []
        self.jobs = CloudJobs(Path(self.temp.name) / 'private', 'fixture-owner',
            {'env-fixture': ['main']}, executable='/fixture/codex',
            runner=lambda *args, **kwargs: CommandResult(0,
                b'https://chatgpt.com/codex/tasks/task_fixture', b''), clock=lambda: self.now)
        self.job = self.jobs.intent({'mesh_task_id': 'original-task', 'mesh_epoch': 3,
            'request_id': 'original-request', 'environment_id': 'env-fixture',
            'branch': 'main', 'prompt': 'SYNTHETIC_PUBLIC_TASK'})['job_id']
        self.grant = {'format': PROTOCOL, 'mesh_task_id': 'original-task', 'mesh_epoch': 3,
            'lease_id': 'original-lease', 'expires_at': 2000, 'request_id': 'original-request',
            'callback_url': RESOURCE, 'scopes': ['node:hello', 'task:read', 'task:report',
                'artifact:write', 'capabilities:read', 'capabilities:call'],
            'capabilities': ['public-fixture'],
            'enrollment': {'format': 'mesh-enrollment/1', 'mesh_id': 'mesh-fixture',
                'node_id': node_id(self.job), 'owner_binding': 'fixture-owner',
                'repo_pin': {'repository': 'https://github.com/example/mesh', 'commit': 'a' * 40},
                'runtime': {'kind': 'temporary', 'expires_at': 2000, 'state_ref': 'mesh:state'},
                'join': {'peer_id': 'owner', 'authority_id': 'authority-fixture',
                         'route_ref': 'route-fixture', 'credential_ref': 'cloud-secret',
                         'trust_ref': 'owner-root'},
                'continuation': {'project_id': 'mesh-maintenance', 'session_ref': 'native-session',
                                 'state_ref': 'mesh:state'}}}
        def gateway(binding, action, arguments):
            self.effects.append((binding['job_id'], action, arguments))
            return {'accepted': True, 'execution_verified': False}
        self.membership = CloudMembership(self.jobs, capability_gateway=gateway,
                                          lease_validator=lambda binding: True)
        self.membership.prepare(self.job, self.grant,
            token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(), authorized=True)
        self.jobs.submit(self.job, authorized=True)
        self.server = HTTPServer(('127.0.0.1', 0), make_http_handler(self.membership,
            RESOURCE, trusted_proxy_peers=['127.0.0.1']))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def envelope(self, action='hello', arguments=None, request_id=None):
        return {'format': REQUEST_PROTOCOL, 'job_id': self.job, 'task_id': 'original-task',
                'epoch': 3, 'lease_id': 'original-lease', 'action': action,
                'request_id': request_id or 'http-' + action, 'arguments': arguments or {}}

    def post(self, envelope=None, *, raw=None, headers=None, path='/cloud/node', method='POST'):
        payload = raw if raw is not None else json.dumps(envelope or self.envelope()).encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        supplied = {'Host': 'mesh.example.test', 'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + TOKEN, 'X-Forwarded-Proto': 'https',
                    'Content-Length': str(len(payload))}
        supplied.update(headers or {})
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in supplied.items():
            if value is not None:
                for item in value if isinstance(value, list) else [value]:
                    connection.putheader(key, item)
        connection.endheaders(payload)
        try:
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def request_count(self):
        with self.jobs._transaction() as db:
            return db.execute('SELECT COUNT(*) FROM cloud_node_requests').fetchone()[0]

    def test_full_artifact_report_and_owner_offline_read(self):
        self.assertTrue(self.post()[1]['authenticated_hello'])
        raw = bytes(range(256)) * 600
        args = {'name': 'complete.bin', 'encoding': 'base64', 'bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(), 'media_type': 'application/octet-stream',
                'content_b64': base64.b64encode(raw).decode()}
        status, saved = self.post(self.envelope('artifact', args))
        self.assertEqual(200, status)
        result = '完整结果\n' * 12000
        self.assertEqual(200, self.post(self.envelope('report', {'status': 'completed',
            'result': result, 'artifacts': [saved['sha256']]}))[0])
        self.membership.close(self.job, authorized=True)
        self.now = 2100
        self.assertEqual(raw, self.membership.read_artifact(self.job, 'complete.bin'))
        self.assertEqual(result, self.membership.saved_result(self.job)['result'])
        self.assertEqual([], self.effects)

    def test_plain_http_is_denied_by_default_even_with_forwarded_header(self):
        isolated = HTTPServer(('127.0.0.1', 0), make_http_handler(self.membership, RESOURCE))
        thread = threading.Thread(target=isolated.serve_forever, daemon=True)
        thread.start()
        original = self.server
        try:
            self.server = isolated
            self.assertEqual(403, self.post()[0])
            self.assertEqual(0, self.request_count())
        finally:
            self.server = original
            isolated.shutdown()
            isolated.server_close()
            thread.join(5)

    def test_invalid_transport_headers_never_reach_facade(self):
        cases = [({'Host': 'attacker.test'}, 400), ({'Host': ['mesh.example.test'] * 2}, 400),
                 ({'X-Forwarded-Proto': 'http'}, 403),
                 ({'X-Forwarded-Proto': ['https'] * 2}, 403),
                 ({'Authorization': None}, 401),
                 ({'Authorization': ['Bearer ' + TOKEN] * 2}, 401),
                 ({'Content-Length': None}, 411), ({'Content-Length': ['2', '2']}, 411),
                 ({'Content-Length': '9' * 100}, 411),
                 ({'Content-Length': str(25 * 1024 * 1024)}, 413),
                 ({'Content-Type': 'text/plain'}, 415), ({'Transfer-Encoding': 'chunked'}, 400),
                 ({'Content-Encoding': 'gzip'}, 400), ({'Expect': '100-continue'}, 400),
                 ({'Origin': 'https://attacker.test'}, 400), ({'Cookie': 'auth=secret'}, 400)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                status, value = self.post(headers=headers)
                self.assertEqual(expected, status)
                self.assertEqual('not_attempted', value['outcome'])
                self.assertNotIn(TOKEN, json.dumps(value))
        self.assertEqual(0, self.request_count())

    def test_wrong_path_and_method_do_not_dispatch(self):
        for path in ['/cloud/node?x=1', '/cloud/%6eode', '/v1/tasks', '/cloud/node/']:
            self.assertEqual(404, self.post(path=path)[0])
        self.assertEqual(405, self.post(method='GET')[0])
        self.assertEqual(0, self.request_count())

    def test_duplicate_invalid_json_and_lone_surrogate_are_rejected_before_intent(self):
        for raw in [b'{"action":"hello","action":"call"}', b'[]', b'not JSON',
                    b'{"a":NaN}', b'{"a":Infinity}', b'{"a":"\\ud800"}', b'\xff']:
            self.assertEqual(400, self.post(raw=raw)[0])
        self.assertEqual(0, self.request_count())

    def test_wrong_token_and_task_scope_have_no_dispatched_effect(self):
        self.assertEqual(403, self.post(headers={'Authorization': 'Bearer ' + TOKEN + 'wrong'})[0])
        changed = dict(self.envelope(), epoch=4)
        self.assertEqual(403, self.post(changed)[0])
        self.assertEqual(0, self.request_count())
        self.assertEqual([], self.effects)

    def test_callback_unknown_keeps_same_intent_and_never_replays(self):
        self.post()
        def failing(*args):
            self.effects.append('one-actual-dispatch')
            raise RuntimeError('PRIVATE_SECRET_' + TOKEN)
        self.membership.gateway = failing
        envelope = self.envelope('call', {'capability_id': 'public-fixture', 'input': {}}, 'same-call')
        status, failure = self.post(envelope)
        self.assertEqual(503, status)
        self.assertEqual('unconfirmed', failure['outcome'])
        self.assertFalse(failure['automatic_retry'])
        self.assertNotIn('PRIVATE_SECRET', json.dumps(failure))
        status, original = self.post(envelope)
        self.assertEqual(200, status)
        self.assertEqual('unknown', original['state'])
        self.assertEqual(['one-actual-dispatch'], self.effects)

    def test_policy_refuses_noncanonical_url_and_remote_proxy(self):
        for url in ['http://mesh.example.test/cloud/node', 'https://mesh.example.test:8443/cloud/node',
                    'https://user:password@mesh.example.test/cloud/node',
                    'https://mesh.example.test/cloud/node#fragment',
                    'https://mesh.example.test/cloud/%6eode', 'https://mesh.example.test/',
                    'https://mesh.example.test/cloud\\node',
                    'https://*.example.test/cloud/node', 'https://mesh.example.test/cloud/../node']:
            with self.assertRaises(ValueError):
                make_http_handler(self.membership, url)
            with self.assertRaises(ValueError):
                CloudHTTPClient(url, TOKEN)
        with self.assertRaises(ValueError):
            make_http_handler(self.membership, RESOURCE, trusted_proxy_peers=['10.0.0.1'])

    def test_client_uses_verified_https_proxy_defaults_and_no_redirect(self):
        client = CloudHTTPClient(RESOURCE, TOKEN)
        names = {type(handler).__name__ for handler in client.opener.handlers}
        self.assertIn('HTTPSHandler', names)
        self.assertIn('_NoRedirect', names)
        with mock.patch.object(client.opener, 'open', side_effect=OSError('SECRET ' + TOKEN)) as call:
            with self.assertRaises(CloudTransportError) as failure:
                client.request(self.envelope())
        self.assertEqual('cloud_http_outcome_unconfirmed', str(failure.exception))
        self.assertEqual(1, call.call_count)
        request = call.call_args[0][0]
        self.assertEqual(RESOURCE, request.full_url)
        self.assertEqual('Bearer ' + TOKEN, request.get_header('Authorization'))
        self.assertEqual('PLACEHOLDER', CloudHTTPClient(RESOURCE, 'PLACEHOLDER').token)


if __name__ == '__main__':
    unittest.main()
