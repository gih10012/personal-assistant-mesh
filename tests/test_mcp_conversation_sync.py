"""Explicit original-message sync using actual SQLite claim/intent/receipt state.

No model, account, production database, external HTTP or keeper is used.
"""
import asyncio
from pathlib import Path
import tempfile
import time
import unittest

from assistant_mesh.conversation import ConversationService, dispatch as conversation_dispatch
from assistant_mesh.conversation_authority import ConversationAuthority, ROUTE
from assistant_mesh.conversation_runtime import ConversationBridge
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_http import LocalIngressBackend, dispatch_conversation_sync
from assistant_mesh.mcp_http_auth import OwnerTokenVerifier, validate_policy
from assistant_mesh.store import Store
from tests.test_mcp_http import Delegate, SDK_AVAILABLE, policy, token


BINDING = {'request_id': 'fixture-original', 'conversation_id': 'fixture-conversation'}


class SyncFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-mcp-conversation-sync.')
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.store = Store(root / 'native.sqlite')
        self.ingress = Ingress(self.store, 'sync-authority', {'owner_id': 'owner-fixture'})
        self.results = IngressResults(self.store, 'sync-authority', {'owner_id': 'owner-fixture'})
        self.backend = LocalIngressBackend(self.ingress, self.results)
        self.verifier = OwnerTokenVerifier(Delegate(), validate_policy(policy()))
        self.principal = self.verifier.principal(token())
        self.backend.request(self.principal, 'submit', dict(request_id=BINDING['request_id'], input='original task'))
        private = root / 'private'
        private.mkdir(mode=0o700)
        self.service = ConversationService(private, self.principal['owner_id'], self.backend)
        self.service.request(self.principal, 'open', BINDING)
        authority = ConversationAuthority(self.ingress)
        principal = self.principal
        class BoundClient:
            def __init__(self):
                self.deliveries, self.observations = 0, 0
                self.lose_ack, self.block_receipts = False, False
            def request(self, path, body):
                assert path == ROUTE
                if body['action'] == 'receipt' and self.block_receipts:
                    raise OSError('synthetic link unavailable')
                if body['action'] == 'observe':
                    self.observations += 1
                result = authority.dispatch({'role': 'ingress', 'ingress': principal}, body)
                if body['action'] == 'deliver':
                    self.deliveries += 1
                    if self.lose_ack:
                        raise OSError('synthetic ACK loss AFTER actual commit')
                return result
        self.client = BoundClient()
        self.bridge = ConversationBridge(self.service, self.principal, self.client)

    def claim(self):
        self.store.heartbeat('original-leader', ['leader'], 30)
        self.task = self.store.claim('original-leader')
        self.assertIsNotNone(self.task)

    def record(self, message_id='original-client-message', text='Continue the original task'):
        self.service.request(self.principal, 'reply', dict(BINDING, message_id=message_id, text=text))
        return dict(BINDING, message_id=message_id)

    def sync(self, arguments, auth=None):
        return dispatch_conversation_sync(self.service, self.verifier, auth or token(), arguments)

    def assert_single_original_effect(self):
        self.assertEqual(1, self.client.deliveries)
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM steering').fetchone()[0])
        with self.service.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM conversation_bridge_intents').fetchone()[0])


class ConversationSyncTests(SyncFixture, unittest.TestCase):
    def test_reply_before_real_claim_advances_only_on_explicit_write_sync(self):
        arguments = self.record()
        waiting = self.bridge.after_reply(self.principal, BINDING, arguments['message_id'])
        self.assertEqual('awaiting_original_live_task', waiting['state'])
        self.assertFalse(waiting['post_attempted'])
        still_waiting = self.sync(arguments)
        self.assertTrue(still_waiting['ok'])
        self.assertEqual('awaiting_original_live_task', still_waiting['runtime']['state'])
        self.assertEqual(0, self.client.deliveries)
        self.claim()
        read_token = token(scopes=['tasks:read'])
        self.bridge.before_receive(self.verifier.principal(read_token), BINDING)
        for action, payload in (('receive', BINDING), ('receipt', arguments)):
            self.assertTrue(conversation_dispatch(self.service, self.verifier, read_token, action, payload)['ok'])
        self.assertEqual('not_admitted_to_native', self.bridge.original_receipt(
            self.verifier.principal(read_token), BINDING, arguments['message_id'])['state'])
        self.assertEqual(0, self.client.deliveries)
        delivered = self.sync(arguments)
        self.assertTrue(delivered['ok'])
        self.assertTrue(delivered['data']['sync_of_original_message'])
        self.assertEqual(arguments['message_id'], delivered['data']['message']['message_id'])
        self.assertEqual('queued_for_native', delivered['runtime']['state'])
        self.assertFalse(delivered['runtime']['native_consumption_verified'])
        self.assertFalse(delivered['runtime']['human_consent_verified'])
        steering = self.store.poll_steering(self.task['id'], 'original-leader', self.task['epoch'])['steering']
        self.assertEqual('Continue the original task', steering['text'])
        self.assertEqual('queued_for_native', self.sync(arguments)['runtime']['state'])
        self.assert_single_original_effect()

    def test_native_ack_loss_reconstruction_only_reads_original_receipt(self):
        self.claim()
        arguments = self.record()
        self.client.lose_ack, self.client.block_receipts = True, True
        unknown = self.sync(arguments)
        self.assertTrue(unknown['ok'])
        self.assertEqual('delivery_unknown', unknown['runtime']['state'])
        self.assert_single_original_effect()
        unknown_again = self.sync(arguments)
        self.assertFalse(unknown_again['ok'])
        self.assertEqual('unknown', unknown_again['outcome'])
        self.assert_single_original_effect()
        self.client.lose_ack, self.client.block_receipts = False, False
        self.bridge = ConversationBridge(self.service, self.principal, self.client)
        observations = self.client.observations
        original = self.sync(arguments)
        self.assertEqual('queued_for_native', original['runtime']['state'])
        self.assertEqual(observations, self.client.observations)
        self.assertEqual('queued_for_native', self.sync(arguments)['runtime']['state'])
        self.assert_single_original_effect()

    def test_current_verifier_write_scope_closed_arguments_and_original_visibility(self):
        self.claim()
        arguments = self.record(text='approve')
        denied = self.sync(arguments, token(scopes=['tasks:read']))
        self.assertEqual('insufficient_scope', denied['error'])
        self.assertEqual('not_attempted', denied['outcome'])
        for auth in (token(subject='subject-other'), token(expires_at=int(time.time()) - 1),
                     token(client_id='not-approved'), token(resource='https://foreign.test/mcp')):
            with self.subTest(auth=auth):
                self.assertEqual('not_authorized_or_not_visible', self.sync(arguments, auth)['error'])
        for field in ('source', 'subject', 'owner_id', 'role', 'task_id', 'text', 'extra'):
            with self.subTest(field=field):
                rejected = self.sync(dict(arguments, **{field: 'caller-chosen'}))
                self.assertEqual('invalid_conversation_arguments', rejected['error'])
                self.assertEqual('not_attempted', rejected['outcome'])
        self.assertFalse(self.sync(dict(arguments, message_id='missing-original'))['ok'])
        self.service.publish(self.principal, dict(BINDING, message_id='original-outbound', text='progress', kind='progress'))
        self.assertFalse(self.sync(dict(arguments, message_id='original-outbound'))['ok'])
        self.assertFalse(self.sync(dict(arguments, conversation_id='other-conversation'))['ok'])
        self.assertEqual(0, self.client.deliveries)
        with self.service.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM conversation_bridge_intents').fetchone()[0])
        receipt = self.sync(arguments)['runtime']
        self.assertFalse(receipt['human_consent_verified'])
        self.assertFalse(receipt['native_interaction_effective'])
        self.assert_single_original_effect()

    def test_absent_or_non_bridge_runtime_is_explicitly_unavailable(self):
        arguments = self.record()
        for runtime in (None, object()):
            self.service.runtime = runtime
            result = self.sync(arguments)
            self.assertFalse(result['ok'])
            self.assertEqual('conversation_sync_runtime_unavailable', result['error'])
            self.assertEqual('not_attempted', result['outcome'])
            self.assertNotIn('runtime', result)
        self.assertEqual(0, self.client.deliveries)


@unittest.skipUnless(SDK_AVAILABLE, 'isolated real MCP 2.3 required; absence is not a passed SDK check')
class SyncMCPTests(SyncFixture, unittest.TestCase):
    def test_authenticated_eighth_tool_metadata_strict_scope_pending_claim_and_receipt_only(self):
        import httpx2
        from assistant_mesh.mcp_http import create_server
        from mcp.server.auth.provider import AccessToken
        from tests.test_mcp_http import ISSUER
        resource = 'http://127.0.0.1:8765/mcp'
        full = AccessToken(token='SYNTHETIC_FULL', client_id='chatgpt-fixture-client', subject='subject-owner',
            scopes=['tasks:read', 'tasks:submit'], expires_at=int(time.time()) + 600,
            resource=resource, claims={'iss': ISSUER})
        class SyntheticVerifier:
            async def verify_token(self, raw):
                if raw == 'SYNTHETIC_FULL':
                    return full
                if raw == 'SYNTHETIC_READ':
                    return full.model_copy(update={'scopes': ['tasks:read']})
                if raw == 'SYNTHETIC_OTHER':
                    return full.model_copy(update={'subject': 'subject-other'})
        _, app = create_server(policy(resource), token_verifier=SyntheticVerifier(), backend=self.backend,
            conversation_service=self.service, allow_loopback_for_tests=True)
        async def exercise():
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://127.0.0.1:8765') as client:
                    async def rpc(method, params=None, raw='SYNTHETIC_FULL'):
                        response = await client.post('/mcp', headers={'Authorization': 'Bearer ' + raw,
                            'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'},
                            json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
                        self.assertEqual(200, response.status_code)
                        return response.json()
                    async def call(name, arguments, raw='SYNTHETIC_FULL'):
                        return await rpc('tools/call', {'name': name, 'arguments': arguments}, raw)
                    listed = (await rpc('tools/list'))['result']['tools']
                    self.assertEqual(8, len(listed))
                    descriptor = next(item for item in listed if item['name'] == 'mesh_conversation_sync')
                    self.assertFalse(descriptor['annotations']['readOnlyHint'])
                    self.assertEqual([{'type': 'oauth2', 'scopes': ['tasks:read', 'tasks:submit']}], descriptor['securitySchemes'])
                    self.assertEqual(descriptor['securitySchemes'], descriptor['_meta']['securitySchemes'])
                    arguments = dict(BINDING, message_id='actual-mcp-original')
                    original = await call('mesh_conversation_reply', dict(arguments, text='original retained reply'))
                    self.assertEqual('awaiting_original_live_task', original['result']['structuredContent']['runtime']['state'])
                    denied = (await call('mesh_conversation_sync', arguments, 'SYNTHETIC_READ'))['result']
                    self.assertTrue(denied['isError'])
                    self.assertEqual('insufficient_scope', denied['structuredContent']['error'])
                    self.assertIn('tasks:submit', denied['_meta']['mcp/www_authenticate'][0])
                    other = await call('mesh_conversation_sync', arguments, 'SYNTHETIC_OTHER')
                    self.assertEqual('not_authorized_or_not_visible', other['result']['structuredContent']['error'])
                    for field in ('source', 'subject', 'owner_id', 'role', 'extra'):
                        bad = await call('mesh_conversation_sync', dict(arguments, **{field: 'caller'}))
                        self.assertEqual(-32602, bad['error']['code'])
                    self.claim()
                    await call('mesh_conversation_receive', BINDING, 'SYNTHETIC_READ')
                    await call('mesh_conversation_receipt', arguments, 'SYNTHETIC_READ')
                    await call('mesh_task_status', {'request_id': BINDING['request_id']}, 'SYNTHETIC_READ')
                    self.assertEqual(0, self.client.deliveries)
                    synced = await call('mesh_conversation_sync', arguments)
                    self.assertEqual('queued_for_native', synced['result']['structuredContent']['runtime']['state'])
                    await call('mesh_conversation_sync', arguments)
                    await call('mesh_conversation_receipt', arguments)
                    self.assert_single_original_effect()
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
