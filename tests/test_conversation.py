"""Private real SQLite/Ingress + optional real MCP, no account/network/model."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import socket
import threading
import time
import unittest
import urllib.request
import zipfile

from assistant_mesh.conversation import ConversationService, arguments, digest, dispatch, store_interaction_validator
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_http import LocalIngressBackend, create_server
from assistant_mesh.mcp_http_auth import OwnerTokenVerifier, NotConfigured, validate_policy
from assistant_mesh.store import Store, Conflict
from tests import test_mcp_http as mcp_tests
from tests.test_mcp_http import Delegate, policy, token, SDK_AVAILABLE


ROOT = Path(__file__).resolve().parents[1]
BINDING = {'conversation_id': 'fixture-conversation', 'request_id': 'fixture-request'}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-conversation.')
        self.root = Path(self.temp.name)
        self.directory = self.root / 'private'
        self.directory.mkdir(mode=0o700)
        self.store = Store(self.root / 'fixture.sqlite')
        self.ingress = Ingress(self.store, 'fixture-authority', {'owner_id': 'owner-fixture'})
        self.backend = LocalIngressBackend(self.ingress, IngressResults(self.store, 'fixture-authority', {'owner_id': 'owner-fixture'}))
        self.verifier = OwnerTokenVerifier(Delegate(), validate_policy(policy()))
        self.principal = self.verifier.principal(token())
        self.task = self.backend.request(self.principal, 'submit', {'request_id': BINDING['request_id'], 'input': 'Original fixture task'})['task_id']
        self.service = ConversationService(self.directory, 'owner-fixture', self.backend)
        self.service.request(self.principal, 'open', BINDING)

    def tearDown(self):
        self.temp.cleanup()

    def test_same_owner_original_request_task_and_no_extra_task(self):
        result = self.service.request(self.principal, 'open', BINDING)
        self.assertEqual(self.task, result['task_id'])
        self.assertFalse(result['native_interaction_effective'])
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
        self.backend.request(self.principal, 'submit', {'request_id': 'different', 'input': 'different original'})
        with self.assertRaises(Conflict):
            self.service.request(self.principal, 'open', dict(BINDING, request_id='different'))

    def test_foreign_owner_subject_scope_and_json_role_never_publish_or_read(self):
        for principal in (dict(self.principal, owner_id='foreign'), dict(self.principal, subject='foreign'),
                          dict(self.principal, source='operator'), dict(self.principal, scopes=[])):
            with self.subTest(principal=principal), self.assertRaises((PermissionError, ValueError)):
                self.service.request(principal, 'receive', BINDING)
        for extra in ({'role': 'operator'}, {'task_id': self.task}, {'scope': 'native:other'}, {'owner_id': 'owner-fixture'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                arguments('reply', dict(BINDING, message_id='one', text='hello', **extra))
        denied = dispatch(self.service, self.verifier, token(scopes=['tasks:read']), 'reply', dict(BINDING, message_id='one', text='hello'))
        self.assertEqual('insufficient_scope', denied['error'])

    def test_two_directions_full_bytes_exact_id_receipt_and_restart_cursor(self):
        outgoing = dict(BINDING, message_id='out-1', text='  进展\n\n```text\n原始文本\n```\n', kind='progress')
        first = self.service.publish(self.principal, outgoing)
        incoming = dict(BINDING, message_id='in-1', text='继续这项工作', reply_to='out-1', original_time='2026-10-16T16:01:02+08:00')
        reply = self.service.request(self.principal, 'reply', incoming)
        self.assertEqual('to_mesh', reply['message']['direction'])
        self.assertEqual('2026-10-16T08:01:02Z', reply['message']['original_time'])
        self.assertIsNone(reply['message']['sent_at'])
        replay = self.service.request(self.principal, 'reply', incoming)
        self.assertEqual(reply['message'], replay['message'])
        self.assertTrue(reply['new_message'])
        self.assertFalse(replay['new_message'])
        # Simulate lost write ACK: the next operation is a read-only same-ID lookup.
        receipt = self.service.request(self.principal, 'receipt', dict(BINDING, message_id='in-1'))
        self.assertEqual(reply['message'], receipt['message'])
        reopened = ConversationService(self.directory, 'owner-fixture', self.backend)
        page = reopened.request(self.principal, 'receive', dict(BINDING, after=0, limit=1))
        self.assertEqual(outgoing['text'], page['messages'][0]['text'])
        self.assertEqual(first['message'], page['messages'][0])
        self.assertTrue(page['has_more'])
        following = reopened.request(self.principal, 'receive', dict(BINDING, after=page['cursor']))
        self.assertEqual([reply['message']], following['messages'])
        self.assertFalse(following['has_more'])
        self.assertFalse(following['background_wake_supported'])

    def test_same_message_changed_body_or_direction_never_replaced(self):
        payload = dict(BINDING, message_id='immutable', text='original')
        self.service.request(self.principal, 'reply', payload)
        with self.assertRaises(Conflict):
            self.service.request(self.principal, 'reply', dict(payload, text='replacement'))
        with self.assertRaises(Conflict):
            self.service.publish(self.principal, payload)
        result = self.service.request(self.principal, 'receive', BINDING)
        self.assertEqual(['original'], [message['text'] for message in result['messages']])

    def test_bounded_connected_wait_observes_owner_publication_without_submission(self):
        ready = threading.Event()
        original_wait = self.service.changed.wait
        def waiting(timeout):
            ready.set()
            return original_wait(timeout)
        self.service.changed.wait = waiting
        def publish():
            if not ready.wait(1):
                return
            self.service.publish(self.principal, dict(BINDING, message_id='during-wait', text='Leader progress', kind='progress'))
        worker = threading.Thread(target=publish)
        worker.start()
        result = self.service.request(self.principal, 'receive', dict(BINDING, wait_ms=1000))
        worker.join(2)
        self.assertTrue(ready.is_set())
        self.assertFalse(worker.is_alive())
        self.assertEqual('during-wait', result['messages'][0]['message_id'])
        for extra in ({'after': True}, {'after': -1}, {'limit': 21}, {'wait_ms': 20001}, {'wait_ms': 0.1}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                arguments('receive', dict(BINDING, **extra))
        with self.assertRaises(Conflict):
            self.service.request(self.principal, 'receive', dict(BINDING, after=999))

    def test_questions_require_original_live_task_epoch_id_hash_and_are_not_native_answers(self):
        question = dict(BINDING, message_id='question-1', text='Which option?', kind='question', interaction={
            'task_id': self.task, 'task_epoch': 1, 'interaction_id': 'fixture-interaction', 'params_sha256': 'a' * 64})
        with self.assertRaises(NotConfigured):
            self.service.publish(self.principal, question)
        live = {'value': True}
        self.service.validator = lambda binding: live['value'] and binding == question['interaction']
        self.service.publish(self.principal, question)
        self.assertEqual('question-1', self.service.question(self.principal, dict(BINDING, message_id='question-1'))['message']['message_id'])
        live['value'] = False
        answer = dict(BINDING, message_id='answer-1', text='Option one', reply_to='question-1')
        with self.assertRaises(Conflict):
            self.service.request(self.principal, 'reply', answer)
        self.assertFalse(self.service.request(self.principal, 'receipt', dict(BINDING, message_id='answer-1'))['found'])
        live['value'] = True
        result = self.service.request(self.principal, 'reply', answer)
        self.assertEqual(question['interaction'], result['message']['interaction'])
        self.assertFalse(result['native_interaction_effective'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM interactions').fetchone()[0])

    def test_existing_store_live_validator_does_not_resolve_original_question(self):
        # No fabricated native task: claim this real original ingress task.
        self.store.heartbeat('fixture-node', ['leader'], 1)
        claim = self.store.claim('fixture-node')
        self.assertEqual(self.task, claim['id'])
        params = {'questions': [{'id': 'question', 'question': 'Which option?'}]}
        self.store.interaction(self.task, 'fixture-node', claim['epoch'], 'fixture-live-question', 'question', params)
        validator = store_interaction_validator(self.store)
        binding = {'task_id': self.task, 'task_epoch': claim['epoch'],
                   'interaction_id': 'fixture-live-question', 'params_sha256': digest(params)}
        self.assertTrue(validator(binding))
        self.assertFalse(validator(dict(binding, task_epoch=claim['epoch'] + 1)))
        self.assertFalse(validator(dict(binding, params_sha256='f' * 64)))
        with self.store.transaction() as db:
            self.assertIsNone(db.execute('SELECT answer FROM interactions WHERE id=?', ('fixture-live-question',)).fetchone()[0])

    def test_expiry_after_wait_is_not_data_disclosure_or_a_retried_write(self):
        now = [time.time()]
        verifier = OwnerTokenVerifier(Delegate(), validate_policy(policy()), clock=lambda: now[0])
        auth = token(expires_at=int(now[0]) + 10)
        real = self.service.request
        def expires(principal, action, payload):
            result = real(principal, action, payload)
            now[0] += 20
            return result
        self.service.request = expires
        result = dispatch(self.service, verifier, auth, 'receive', BINDING)
        self.assertFalse(result['ok'])
        self.assertNotIn('data', result)

    def test_private_journal_permissions_namespace_and_message_bounds(self):
        self.service.database.chmod(0o644)
        with self.assertRaises(ValueError):
            self.service.request(self.principal, 'receive', BINDING)
        self.service.database.chmod(0o600)
        with self.assertRaises(Conflict):
            ConversationService(self.directory, 'other-owner', self.backend)
        for text in ('', ' ', 'x' * 8193):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                arguments('reply', dict(BINDING, message_id='one', text=text))
        with self.assertRaises(ValueError):
            arguments('reply', dict(BINDING, message_id='one', text='hello', original_time='2026-10-16'))

    def test_remote_package_exact_only_no_local_auth_or_stdio(self):
        spec = importlib.util.spec_from_file_location('package_remote', ROOT / 'scripts/package_remote_plugin.py')
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        receipt = package.build(self.root / 'remote.zip')
        self.assertFalse(receipt['connected'])
        with zipfile.ZipFile(receipt['archive']) as archive:
            self.assertCountEqual(['personal-assistant-mesh-remote/plugin.json', 'personal-assistant-mesh-remote/mcp.json'], archive.namelist())
            config = json.loads(archive.read('personal-assistant-mesh-remote/mcp.json'))
            server = config['mcpServers']['personal-assistant-mesh-public']
            self.assertEqual('streamable-http', server['type'])
            self.assertEqual('https://47.84.73.177/mcp', server['url'])
            self.assertNotIn('headers', server)
            self.assertNotIn('command', server)
            self.assertEqual({'mode': 'cimd'}, server['extensions']['com.openai']['auth']['client'])
        with self.assertRaises(FileExistsError):
            package.build(self.root / 'remote.zip')


@unittest.skipUnless(SDK_AVAILABLE, 'isolated MCP SDK 2.3 required; not Chat acceptance')
class ConversationSDKTests(unittest.TestCase):
    http = mcp_tests.ActualSDKHTTPTests.http
    rpc = staticmethod(mcp_tests.ActualSDKHTTPTests.rpc)

    def test_real_authenticated_http_eight_tools_two_directions_and_modern_client(self):
        import uvicorn
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared.exceptions import MCPError
        import httpx2
        fixture = ConversationTests()
        fixture.setUp()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        self.resource = 'http://127.0.0.1:{}/mcp'.format(sock.getsockname()[1])
        auth = token(resource=self.resource)
        class SyntheticVerifier:
            async def verify_token(self, raw):
                return auth if raw == 'SYNTHETIC_VALID' else None
        _, app = create_server(policy(self.resource), token_verifier=SyntheticVerifier(),
            backend=fixture.backend, conversation_service=fixture.service, allow_loopback_for_tests=True)
        server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False,
                                             lifespan='on', timeout_graceful_shutdown=2))
        thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.started)
            self.assertEqual(401, self.http(payload=self.rpc('tools/list'), bearer=None)[0])
            tools = self.http(payload=self.rpc('tools/list'))[2]['result']['tools']
            self.assertEqual(8, len(tools))
            self.assertIn('mesh_approved_result', {tool['name'] for tool in tools})
            for tool in tools:
                self.assertEqual(tool['securitySchemes'], tool['_meta']['securitySchemes'])
            async def client_calls():
                async with httpx2.AsyncClient(headers={'Authorization': 'Bearer SYNTHETIC_VALID'}, timeout=5, trust_env=False) as http:
                    async with Client(streamable_http_client(self.resource, http_client=http), read_timeout_seconds=5) as client:
                        self.assertEqual('2026-07-28', client.protocol_version)
                        opened = await client.call_tool('mesh_conversation_open', BINDING)
                        self.assertFalse(opened.is_error)
                        fixture.service.publish(fixture.principal, dict(BINDING, message_id='leader-progress', text='One live update', kind='progress'))
                        received = await client.call_tool('mesh_conversation_receive', dict(BINDING, wait_ms=1000))
                        data = received.structured_content['data']
                        self.assertEqual('leader-progress', data['messages'][0]['message_id'])
                        reply = await client.call_tool('mesh_conversation_reply', dict(BINDING,
                            message_id='client-answer', text='Keep going', reply_to='leader-progress'))
                        self.assertFalse(reply.is_error)
                        receipt = await client.call_tool('mesh_conversation_receipt', dict(BINDING, message_id='client-answer'))
                        self.assertEqual('to_mesh', receipt.structured_content['data']['message']['direction'])
                        self.assertFalse(receipt.structured_content['data']['native_interaction_effective'])
                        entered = threading.Event()
                        original_wait = fixture.service.changed.wait
                        def waiting(timeout):
                            entered.set()
                            return original_wait(timeout)
                        fixture.service.changed.wait = waiting
                        def publish_during_wait():
                            if entered.wait(2):
                                fixture.service.publish(fixture.principal, dict(BINDING,
                                    message_id='during-http-wait', text='Reply over the connected request', kind='progress'))
                        publisher = threading.Thread(target=publish_during_wait)
                        publisher.start()
                        updated = await client.call_tool('mesh_conversation_receive', dict(BINDING, after=2, wait_ms=1000))
                        publisher.join(3)
                        self.assertTrue(entered.is_set())
                        self.assertFalse(publisher.is_alive())
                        self.assertEqual('during-http-wait', updated.structured_content['data']['messages'][0]['message_id'])
                        with self.assertRaises(MCPError) as error:
                            await client.call_tool('mesh_conversation_reply', dict(BINDING,
                                message_id='injected', text='hello', role='operator'))
                        self.assertEqual(-32602, error.exception.code)
                        with self.assertRaises(MCPError):
                            await client.call_tool('mesh_conversation_receive', dict(BINDING, wait_ms=20001))
            asyncio.run(client_calls())
        finally:
            server.should_exit = True
            thread.join(5)
            fixture.tearDown()
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
