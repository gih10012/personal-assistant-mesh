"""Real pinned SDK ASGI request fixtures, never ChatGPT/account acceptance."""
import contextlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from assistant_mesh.conversation import ConversationService
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_events import MCPEvents
from assistant_mesh.mcp_http import LocalIngressBackend, create_server
from assistant_mesh.store import Store
from tests.test_mcp_http import ISSUER, RESOURCE, policy
from tests.test_mcp_events import SECRET, URL, Transport

try:
    import httpx2
    from mcp.server.auth.provider import AccessToken
    from importlib.metadata import version
    SDK = version('mcp') == '2.3.0'
except ImportError:
    SDK = False


@unittest.skipUnless(SDK, 'requires the existing isolated MCP2.3 environment')
class EventExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-events-sdk.')
        self.root = Path(self.temp.name)
        store = Store(self.root / 'fresh.sqlite')
        ingress = Ingress(store, 'fixture-authority', {'owner_id': 'owner-fixture'})
        self.backend = LocalIngressBackend(ingress, IngressResults(store, 'fixture-authority', {'owner_id': 'owner-fixture'}))
        for name in ('events', 'messages'):
            (self.root / name).mkdir(mode=0o700)
        self.messages = ConversationService(self.root / 'messages', 'owner-fixture', self.backend)
        self.transport = Transport()
        self.events = MCPEvents(self.root / 'events', 'owner-fixture', transport=self.transport,
            authorize=lambda peer, name, args: peer['subject'] == 'owner-main-chat'
                and (name is None or args.get('project_id') == 'project-one'))
        self.valid = AccessToken(token='SYNTHETIC', client_id='chatgpt-fixture-client', subject='subject-owner',
            scopes=['tasks:read', 'tasks:submit'], expires_at=int(time.time()) + 600,
            resource=RESOURCE, claims={'iss': ISSUER})
        valid = self.valid
        class Delegate:
            async def verify_token(self, raw):
                return valid if raw == 'SYNTHETIC' else None
        self.delegate = Delegate()

    async def asyncTearDown(self):
        self.temp.cleanup()

    @contextlib.asynccontextmanager
    async def client(self, enabled=True):
        unused_server, app = create_server(policy(), token_verifier=self.delegate, backend=self.backend,
                                           conversation_service=self.messages,
                                           event_service=self.events if enabled else None)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='https://mesh.example.test') as client:
                yield client

    async def rpc(self, client, method, params=None, *, version='2026-07-28', auth=True):
        value = dict(params or {})
        if version == '2026-07-28':
            value['_meta'] = {'io.modelcontextprotocol/protocolVersion': version,
                'io.modelcontextprotocol/clientInfo': {'name': 'synthetic-event-fixture', 'version': '1'},
                'io.modelcontextprotocol/clientCapabilities': {}}
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': version}
        if version == '2026-07-28':
            headers['mcp-method'] = method
        if auth:
            headers['Authorization'] = 'Bearer SYNTHETIC'
        response = await client.post('/mcp', headers=headers,
            json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': value})
        return response

    async def test_real_extension_modern_discover_methods_challenge_and_eight_tools(self):
        async with self.client() as client:
            discovered = await self.rpc(client, 'server/discover')
            self.assertEqual(200, discovered.status_code, discovered.text)
            self.assertEqual({}, discovered.json()['result']['capabilities']['events'])
            listed = await self.rpc(client, 'events/list')
            self.assertEqual({'task.completed', 'leader.attention_required'},
                             {entry['name'] for entry in listed.json()['result']['events']})
            tools = await self.rpc(client, 'tools/list')
            self.assertEqual(8, len(tools.json()['result']['tools']))
            params = {'name': 'task.completed', 'arguments': {'project_id': 'project-one'},
                      'delivery': {'mode': 'webhook', 'url': URL, 'secret': SECRET}}
            subscribed = await self.rpc(client, 'events/subscribe', params)
            self.assertIn('id', subscribed.json()['result'], subscribed.text)
            self.assertEqual(1, len(self.transport.calls))
            original = subscribed.json()['result']['id']
            refreshed = await self.rpc(client, 'events/subscribe', dict(params, ttlMs=120000))
            self.assertEqual(original, refreshed.json()['result']['id'])
            self.assertEqual(1, len(self.transport.calls))
            invalid = await self.rpc(client, 'events/subscribe', dict(params, owner_id='forged'))
            self.assertEqual(-32602, invalid.json()['error']['code'])
            denied = await self.rpc(client, 'events/subscribe', dict(params, arguments={'project_id': 'foreign'}))
            self.assertEqual(-32001, denied.json()['error']['code'])
            params['delivery'].pop('secret')
            cancelled = await self.rpc(client, 'events/unsubscribe', params)
            self.assertIn('result', cancelled.json(), cancelled.text)
            self.assertEqual(401, (await self.rpc(client, 'events/list', auth=False)).status_code)
            legacy = await self.rpc(client, 'events/list', version='2025-11-25')
            self.assertEqual(-32601, legacy.json()['error']['code'])

    async def test_default_off_and_categorized_callback_failure(self):
        async with self.client(enabled=False) as client:
            discover = await self.rpc(client, 'server/discover')
            self.assertNotIn('events', discover.json()['result']['capabilities'])
            self.assertEqual(-32601, (await self.rpc(client, 'events/list')).json()['error']['code'])
        self.transport.challenge_ok = False
        async with self.client() as client:
            response = await self.rpc(client, 'events/subscribe', {'name': 'task.completed',
                'arguments': {'project_id': 'project-one'},
                'delivery': {'mode': 'webhook', 'url': URL, 'secret': SECRET}})
            self.assertEqual(-32015, response.json()['error']['code'])
            self.assertEqual({'reason': 'challenge_failed'}, response.json()['error']['data'])
            self.assertNotIn(SECRET, response.text)


if __name__ == '__main__':
    unittest.main()
