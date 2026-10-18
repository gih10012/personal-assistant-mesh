"""Actual SQLite/leases/interactions/steering and one authenticated loopback.

No model, real account, production Store, iLink, browser or external request.
"""
import hashlib
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error

from assistant_mesh.conversation import ConversationService
from assistant_mesh.conversation_authority import ConversationAuthority, ROUTE, attach
from assistant_mesh.conversation_runtime import ConversationBridge
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_http import LocalIngressBackend, RemoteIngressBackend
from assistant_mesh.server import serve
from assistant_mesh.store import Store
from assistant_mesh.worker import Client
from tests.test_mcp_http import SDK_AVAILABLE


PRINCIPAL = {'owner_id': 'fixture-owner', 'source': 'chatgpt-chat', 'subject': 'fixture-chat',
             'scopes': ['tasks:read', 'tasks:submit']}
PEER = {'role': 'ingress', 'ingress': PRINCIPAL}
BINDING = {'request_id': 'fixture-original', 'conversation_id': 'fixture-conversation'}


class LocalClient:
    def __init__(self, authority):
        self.authority, self.deliveries, self.lose_ack, self.block_receipts = authority, 0, False, False
    def request(self, path, body):
        assert path == ROUTE
        if body['action'] == 'receipt' and self.block_receipts:
            raise OSError('synthetic link unavailable')
        result = self.authority.dispatch(PEER, body)
        if body['action'] == 'deliver':
            self.deliveries += 1
            if self.lose_ack:
                raise OSError('synthetic ACK loss AFTER actual commit')
        return result


class ConversationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-conversation-runtime.')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = time.time()
        self.store = Store(self.root / 'native.sqlite', clock=lambda: self.now)
        self.ingress = Ingress(self.store, 'fixture-authority', {'owner_id': 'fixture-owner'})
        self.results = IngressResults(self.store, 'fixture-authority', {'owner_id': 'fixture-owner'})
        self.backend = LocalIngressBackend(self.ingress, self.results)
        self.backend.request(PRINCIPAL, 'submit', dict(request_id=BINDING['request_id'], input='original task'))
        self.store.heartbeat('leader', ['leader'], 10)
        self.task = self.store.claim('leader')
        self.private = self.root / 'private'
        self.private.mkdir(mode=0o700)
        self.service = ConversationService(self.private, PRINCIPAL['owner_id'], self.backend)
        self.service.request(PRINCIPAL, 'open', BINDING)
        self.authority = ConversationAuthority(self.ingress)
        self.client = LocalClient(self.authority)
        self.bridge = ConversationBridge(self.service, PRINCIPAL, self.client)

    def messages(self):
        return self.service.request(PRINCIPAL, 'receive', BINDING)['messages']

    def reply(self, identity, text='Continue the original task', reply_to=None):
        return self.service.request(PRINCIPAL, 'reply', dict(BINDING, message_id=identity,
            text=text, reply_to=reply_to))

    def test_actual_native_queue_and_original_worker_ack_not_journal_delivery_claim(self):
        self.bridge.step(BINDING)
        progress = self.messages()[0]
        self.reply('reply-one', reply_to=progress['message_id'])
        report = self.bridge.step(BINDING)
        receipt = report['deliveries'][0]['receipt']
        self.assertEqual('queued_for_native', receipt['state'])
        self.assertFalse(receipt['native_submission_acknowledged'])
        self.assertFalse(receipt['native_consumption_verified'])
        steering = self.store.poll_steering(self.task['id'], 'leader', self.task['epoch'])['steering']
        self.assertEqual('Continue the original task', steering['text'])
        self.store.poll_steering(self.task['id'], 'leader', self.task['epoch'], steering['id'], 'submitted')
        refreshed = self.bridge.step(BINDING)['deliveries'][0]['receipt']
        self.assertTrue(refreshed['native_submission_acknowledged'])
        self.assertFalse(refreshed['native_consumption_verified'])
        self.assertEqual(1, self.client.deliveries)
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM steering').fetchone()[0])

    def test_live_native_question_answer_reaches_original_interaction_not_an_approval(self):
        params = {'questions': [{'id': 'which', 'question': 'Which branch?'}]}
        self.store.interaction(self.task['id'], 'leader', self.task['epoch'], 'native-question', 'question', params)
        self.bridge.step(BINDING)
        question = next(item for item in self.messages() if item['kind'] == 'question')
        # The actual ASGI may have started BEFORE a private CLI opened this
        # conversation. A stale startup map is not permission to reject it or
        # reopen the journal recursively while its append transaction is held.
        self.bridge.requests.clear()
        self.reply('answer-one', 'feature-branch', question['message_id'])
        receipt = self.bridge.step(BINDING)['deliveries'][0]['receipt']
        self.assertEqual('native_answer_recorded', receipt['state'])
        self.assertTrue(receipt['native_interaction_effective'])
        self.assertFalse(receipt['human_consent_verified'])
        actual = self.store.interaction(self.task['id'], 'leader', self.task['epoch'], 'native-question')
        self.assertEqual({'answers': {'which': {'answers': ['feature-branch']}}}, actual['answer'])

    def test_closed_question_and_native_approval_never_get_a_fabricated_consent(self):
        params = {'questions': [{'id': 'which', 'question': 'Which branch?'}]}
        self.store.interaction(self.task['id'], 'leader', self.task['epoch'], 'question', 'question', params)
        self.store.interaction(self.task['id'], 'leader', self.task['epoch'], 'approval', 'approval', {'command': 'PRIVATE_COMMAND'})
        self.bridge.step(BINDING)
        self.assertNotIn('PRIVATE_COMMAND', json.dumps(self.messages()))
        question = next(item for item in self.messages() if item['kind'] == 'question')
        self.reply('late-answer', 'later', question['message_id'])
        answer = {'answers': {'which': {'answers': ['already answered elsewhere']}}}
        self.store.resolve_interaction('question', answer)
        report = self.bridge.step(BINDING)
        self.assertEqual('original_question_no_longer_open', report['deliveries'][0]['receipt']['state'])
        with self.store.transaction() as db:
            self.assertIsNone(db.execute('SELECT answer FROM interactions WHERE id=?', ('approval',)).fetchone()[0])
            self.assertEqual(answer, json.loads(db.execute('SELECT answer FROM interactions WHERE id=?', ('question',)).fetchone()[0]))

    def test_lost_ack_restart_queries_same_receipt_never_posts_original_again(self):
        self.reply('unknown-reply')
        self.client.lose_ack, self.client.block_receipts = True, True
        first = self.bridge.step(BINDING)['deliveries'][0]['receipt']
        self.assertEqual('delivery_unknown', first['state'])
        self.assertEqual(1, self.client.deliveries)
        self.client.lose_ack, self.client.block_receipts = False, False
        reconstructed = ConversationBridge(self.service, PRINCIPAL, self.client)
        second = reconstructed.step(BINDING)['deliveries'][0]['receipt']
        self.assertEqual('queued_for_native', second['state'])
        self.assertEqual(1, self.client.deliveries)

    def test_reply_to_old_native_epoch_cannot_retarget_reclaimed_task(self):
        self.bridge.step(BINDING)
        progress = self.messages()[0]
        self.reply('old-generation', reply_to=progress['message_id'])
        self.now += 100
        self.store.heartbeat('other-leader', ['leader'], 20)
        fresh = self.store.claim('other-leader')
        self.assertEqual(self.task['id'], fresh['id'])
        self.assertGreater(fresh['epoch'], self.task['epoch'])
        receipt = self.bridge.step(BINDING)['deliveries'][0]['receipt']
        self.assertEqual('stale_original_task_epoch', receipt['state'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM steering').fetchone()[0])

    def test_only_approved_result_projection_and_scoped_authority_access(self):
        raw = 'PRIVATE_NATIVE_RESULT_MUST_NOT_LEAK'
        self.store.update_task(self.task['id'], 'leader', self.task['epoch'], result=raw, status='completed')
        self.bridge.step(BINDING)
        self.assertNotIn(raw, json.dumps(self.messages()))
        self.results.publish({'role': 'operator'}, {'publication_id': 'approved-original',
            'source': PRINCIPAL['source'], 'subject': PRINCIPAL['subject'], 'request_id': BINDING['request_id'],
            'task_id': self.task['id'], 'task_epoch': self.task['epoch'],
            'task_result_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'reviewed': True,
            'result': {'summary': 'Selected approved summary', 'artifacts': []}})
        report = self.bridge.step(BINDING)
        self.assertTrue(report['approved_result_available'])
        self.assertIn('Selected approved summary', json.dumps(self.messages()))
        self.assertNotIn(raw, json.dumps(self.messages()))
        for peer in ({'role': 'operator'}, {'role': 'ingress', 'ingress': dict(PRINCIPAL, subject='other')},
                     {'role': 'ingress', 'ingress': dict(PRINCIPAL, source='foreign')}):
            with self.assertRaises((PermissionError, ValueError)):
                self.authority.dispatch(peer, {'action': 'observe', 'arguments': {'request_id': BINDING['request_id']}})


class ActualPrivateHTTPTests(unittest.TestCase):
    def test_original_ingress_credential_opt_in_hook_actual_native_queue_and_no_management(self):
        with tempfile.TemporaryDirectory(prefix='mesh-conversation-loopback.') as directory:
            root = Path(directory)
            token = root / 'ingress-token'
            token.write_text('SYNTHETIC_INGRESS_' + 'x' * 40)
            token.chmod(0o600)
            ready, state = threading.Event(), {}
            config = {'node_id': 'fixture-http-authority', 'port': 0, 'database': str(root / 'native.sqlite'),
                'ingress': {'owner_id': PRINCIPAL['owner_id']}, 'peers': [dict(PEER, token_file=str(token))]}
            def connected(http, api, channel):
                self.assertIsNone(channel)
                attach(api)
                state.update(http=http, api=api)
                ready.set()
            thread = threading.Thread(target=lambda: serve(config, ready=connected), daemon=True)
            thread.start()
            self.assertTrue(ready.wait(5))
            try:
                client = Client({'control_url': 'http://127.0.0.1:{}'.format(state['http'].server_address[1]), 'token_file': str(token)})
                backend = RemoteIngressBackend({PRINCIPAL['subject']: client}, PRINCIPAL['owner_id'])
                submitted = backend.request(PRINCIPAL, 'submit', {'request_id': BINDING['request_id'], 'input': 'actual scoped task'})
                store = state['api'].store
                store.heartbeat('leader', ['leader'], 10)
                task = store.claim('leader')
                self.assertEqual(submitted['task_id'], task['id'])
                private = root / 'private'
                private.mkdir(mode=0o700)
                service = ConversationService(private, PRINCIPAL['owner_id'], backend)
                service.request(PRINCIPAL, 'open', BINDING)
                bridge = ConversationBridge(service, PRINCIPAL, client)
                bridge.step(BINDING)
                service.request(PRINCIPAL, 'reply', dict(BINDING, message_id='http-original-reply', text='original HTTP owner reply'))
                self.assertEqual('queued_for_native', bridge.step(BINDING)['deliveries'][0]['receipt']['state'])
                self.assertEqual('original HTTP owner reply', store.poll_steering(task['id'], 'leader', task['epoch'])['steering']['text'])
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    client.request('/v1/task/status', {'id': task['id']})
                self.assertEqual(403, denied.exception.code)
                denied.exception.close()
                with self.assertRaises(urllib.error.HTTPError) as query:
                    client.request(ROUTE + '?operator=1', {'action': 'observe', 'arguments': {'request_id': BINDING['request_id']}})
                self.assertEqual(403, query.exception.code)
                query.exception.close()
            finally:
                state['http'].shutdown()
                thread.join(5)
            self.assertFalse(thread.is_alive())


@unittest.skipUnless(SDK_AVAILABLE, 'isolated real MCP 2.3 environment required; not account acceptance')
class RuntimeMCPTests(unittest.TestCase):
    def test_actual_asgi_hooks_write_only_original_new_reply_and_read_token_never_drains(self):
        import httpx2
        from mcp.server.auth.provider import AccessToken
        from assistant_mesh.mcp_http import create_server
        from tests.test_mcp_http import policy, ISSUER
        fixture = ConversationRuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        resource = 'http://127.0.0.1:8765/mcp'
        config = policy(resource)
        config['owner_binding'] = PRINCIPAL['owner_id']
        config['subjects'][0]['mesh_subject'] = PRINCIPAL['subject']
        full = AccessToken(token='SYNTHETIC_FULL', client_id='chatgpt-fixture-client', subject='subject-owner',
            scopes=['tasks:read', 'tasks:submit'], expires_at=int(time.time()) + 600,
            resource=resource, claims={'iss': ISSUER})
        class FixtureVerifier:
            async def verify_token(self, raw):
                if raw == 'SYNTHETIC_FULL':
                    return full
                if raw == 'SYNTHETIC_READ':
                    return full.model_copy(update={'scopes': ['tasks:read']})
        _, app = create_server(config, token_verifier=FixtureVerifier(), backend=fixture.backend,
            conversation_service=fixture.service, allow_loopback_for_tests=True)
        fixture.reply('pending-not-consumed-by-read')
        async def exercise():
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://127.0.0.1:8765') as client:
                    async def call(name, args, token='SYNTHETIC_FULL'):
                        response = await client.post('/mcp', headers={'Authorization': 'Bearer ' + token,
                            'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'},
                            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': name, 'arguments': args}})
                        self.assertEqual(200, response.status_code)
                        return response.json()['result']['structuredContent']
                    received = await call('mesh_conversation_receive', BINDING, token='SYNTHETIC_READ')
                    self.assertTrue(any(message['kind'] == 'progress' for message in received['data']['messages']))
                    self.assertEqual(0, fixture.client.deliveries)
                    denied = await call('mesh_conversation_reply', dict(BINDING, message_id='denied', text='not authorized'), token='SYNTHETIC_READ')
                    self.assertEqual('insufficient_scope', denied['error'])
                    self.assertEqual(0, fixture.client.deliveries)
                    args = dict(BINDING, message_id='asgi-original-reply', text='Native reply from original tool call')
                    replied = await call('mesh_conversation_reply', args)
                    self.assertEqual('queued_for_native', replied['runtime']['state'])
                    self.assertFalse(replied['runtime']['native_consumption_verified'])
                    self.assertEqual(1, fixture.client.deliveries)
                    same = await call('mesh_conversation_reply', args)
                    self.assertFalse(same['data']['new_message'])
                    self.assertEqual(1, fixture.client.deliveries)
                    receipt = await call('mesh_conversation_receipt', dict(BINDING, message_id='asgi-original-reply'), token='SYNTHETIC_READ')
                    self.assertEqual('queued_for_native', receipt['runtime']['state'])
                    self.assertEqual(1, fixture.client.deliveries)
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
