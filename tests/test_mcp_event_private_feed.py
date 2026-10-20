"""Two local boundaries: source-scoped SQLite feed and actual loopback Client.

Uses reviewed final/emit classes from their exact public dependency commit to
seed receipts; does NOT rerun completion/native/business acceptance tests.
"""
import asyncio
import contextlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from assistant_mesh.conversation import ConversationService
from assistant_mesh.conversation_authority import attach as attach_bridge, digest
from assistant_mesh.conversation_event_feed import attach, ROUTE
from assistant_mesh.conversation_runtime import ConversationBridge
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_event_sources import ProjectEventAccess, RemoteReceiptSource, attach_lifecycle
from assistant_mesh.mcp_events import MCPEvents, EventDeliveryLoop
from assistant_mesh.mcp_http import LocalIngressBackend
from assistant_mesh.store import Store
from assistant_mesh.worker import Client
from tests.test_mcp_events import SECRET, URL, Transport
from tests.test_mcp_http import policy


DEPENDENCY = 'a444ce42d4913c394fe550975c247402de5dfb8a'
OWNER, SUBJECT, NODE = 'owner-fixture', 'owner-main-chat', 'fixture-leader'
PRINCIPAL = {'owner_id': OWNER, 'source': 'chatgpt-chat', 'subject': SUBJECT,
             'scopes': ['tasks:read', 'tasks:submit']}
PEER = {'role': 'ingress', 'ingress': PRINCIPAL}
POLICIES = [{'source': 'chatgpt-chat', 'subject': SUBJECT, 'projects': ['project-one']}]


def dependency_classes():
    root = Path(__file__).resolve().parents[1]
    modules = {}
    with patch.dict(sys.modules):
        for name in ('conversation_emit', 'conversation_final'):
            source = subprocess.run(['git', 'show', DEPENDENCY + ':assistant_mesh/' + name + '.py'],
                cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=5).stdout
            module = types.ModuleType('assistant_mesh.' + name); module.__package__ = 'assistant_mesh'
            sys.modules[module.__name__] = module
            exec(compile(source, '<reviewed-' + DEPENDENCY + '-' + name + '>', 'exec'), module.__dict__)
            modules[name] = module
    return modules['conversation_emit'], modules['conversation_final']


class PrivateFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emit_module, cls.final_module = dependency_classes()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-private-event-feed.'); self.addCleanup(self.temp.cleanup)
        self.root, self.now = Path(self.temp.name), 1000
        self.store = Store(self.root / 'fresh.sqlite', clock=lambda: self.now)
        self.ingress = Ingress(self.store, 'fixture-authority', {'owner_id': OWNER})
        self.results = IngressResults(self.store, 'fixture-authority', {'owner_id': OWNER})
        self.api = types.SimpleNamespace(store=self.store, ingress=self.ingress,
            dispatch=lambda *args: (_ for _ in ()).throw(PermissionError('fixture_default_deny')))
        attach_bridge(self.api)
        self.emits = self.emit_module.ConversationEmits(self.ingress, ('chatgpt-chat',))
        self.api._conversation_emits = self.emits
        self.finals = self.final_module.ConversationFinals(self.api,
            {('chatgpt-chat', SUBJECT): NODE, ('chatgpt-chat', 'other-subject'): NODE})
        self.api._conversation_finals = self.finals
        self.account_live = True

    def final_row(self, request, *, subject=SUBJECT, project='project-one'):
        peer = {'role': 'ingress', 'ingress': dict(PRINCIPAL, subject=subject)}
        accepted = self.ingress.submit(peer, {'request_id': request, 'input': 'fixture original work', 'project_id': project})
        with self.store.transaction() as db:
            binding = self.emits._binding(db, accepted['task_id']); self.finals.admission(db, binding)
            value = {'task_id': accepted['task_id'], 'epoch': 1, 'call_id': self.final_module.CALL_ID,
                'message_id': 'mesh-auto-final-fixture-' + request, 'kind': 'final', 'text': 'PRIVATE_FINAL_TEXT'}
            receipt = self.emits._append_in_db(db, binding, NODE, value)
            operation = receipt['message']['operation_id']
            db.execute('INSERT INTO conversation_task_finals VALUES(?,?,?,?,?,?,?,?,?)',
                (operation, binding['request_key'], accepted['task_id'], 1, NODE, 'completed',
                 digest([self.final_module.FORMAT, 'completed', value]), project, self.now))
            db.execute("UPDATE tasks SET status='completed',epoch=1 WHERE id=?", (accepted['task_id'],))
        return accepted

    def snapshot(self):
        with self.store.transaction() as db:
            return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name)] for name in
                ('tasks', 'native_sessions', 'session_chunks', 'interactions', 'conversation_task_finals', 'conversation_emits')}

    def test_source_sql_before_limit_live_question_reference_and_narrow_route(self):
        previous = self.api.dispatch
        self.assertIsNone(attach(self.api, self.finals)); self.assertIs(previous, self.api.dispatch)
        # More foreign rows than a page: a global-page-then-filter would starve.
        for number in range(24):
            self.final_row('hidden-' + str(number), subject='other-subject')
        self.final_row('wrong-project', project='project-two')
        visible = self.final_row('visible-final')
        accepted = self.ingress.submit(PEER, {'request_id': 'live-question', 'input': 'new fixture work', 'project_id': 'project-one'})
        self.store.heartbeat(NODE, ['leader']); task = self.store.claim(NODE)
        self.assertEqual(accepted['task_id'], task['id'])
        params = {'questions': [{'id': 'q', 'question': 'PUBLIC_QUESTION_DO_NOT_EXPORT_IN_FEED', 'options': []}]}
        self.store.interaction(task['id'], NODE, task['epoch'], 'original-question', 'question', params)
        feed = attach(self.api, self.finals, enabled=True, policies=POLICIES)
        payload = {'after': None, 'limit': 2, 'projects': ['project-one']}
        before = self.snapshot()
        page = self.api.dispatch('POST', ROUTE, payload, PEER)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(2, len(page['events']))
        self.assertEqual(visible['task_id'], page['events'][0]['task_id'])
        question = page['events'][1]
        target = {'interaction_id': 'original-question', 'task_id': task['id'],
                  'task_epoch': task['epoch'], 'params_sha256': digest(params)}
        self.assertEqual('leader.attention_required', question['name'])
        self.assertEqual('mesh-question-' + digest(target), question['message_id'])
        encoded = json.dumps(page)
        for forbidden in ('PRIVATE_FINAL_TEXT', 'PUBLIC_QUESTION_DO_NOT_EXPORT_IN_FEED', 'params_sha256', 'params', 'input'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(question['event_id'], feed.read(PEER, payload)['events'][1]['event_id'])
        # The existing eight-tool receive's actual bridge projects SAME ID.
        directory = self.root / 'messages'; directory.mkdir(mode=0o700)
        backend = LocalIngressBackend(self.ingress, self.results)
        messages = ConversationService(directory, OWNER, backend, clock=lambda: self.now)
        binding = {'request_id': 'live-question', 'conversation_id': 'original-work-chat'}
        messages.request(PRINCIPAL, 'open', binding); api = self.api
        class BoundClient:
            def request(self, path, body):
                return api.dispatch('POST', path, body, PEER)
        bridge = ConversationBridge(messages, PRINCIPAL, BoundClient())
        bridge.before_receive(PRINCIPAL, binding)
        received = messages.request(PRINCIPAL, 'receive', dict(binding, after=0, limit=20))
        self.assertIn(question['message_id'], {item['message_id'] for item in received['messages']})
        self.now += 91
        self.assertEqual(1, len(feed.read(PEER, payload)['events']))  # Stale lease is NOT attention.
        for method, path, changed, peer in (
            ('GET', ROUTE, payload, PEER), ('POST', ROUTE + '?q=1', payload, PEER),
            ('POST', ROUTE, payload, {'role': 'operator'}),
            ('POST', ROUTE, payload, {'role': 'ingress', 'ingress': dict(PRINCIPAL, scopes=[])}),
            ('POST', ROUTE, payload, {'role': 'ingress', 'ingress': dict(PRINCIPAL, source='openai-codex-cloud')}),
            ('POST', ROUTE, dict(payload, projects=['project-two']), PEER),
            ('POST', ROUTE, dict(payload, subject='other-subject'), PEER),
            ('POST', ROUTE, dict(payload, limit=21), PEER)):
            with self.subTest(path=path, method=method, changed=changed), self.assertRaises((ValueError, PermissionError)):
                self.api.dispatch(method, path, changed, peer)

    def test_actual_client_remote_authorization_owned_lifecycle_and_feed_outage(self):
        self.final_row('original-final')
        attach(self.api, self.finals, enabled=True, policies=POLICIES); api = self.api
        token = self.root / 'ingress.token'; token.write_text('SYNTHETIC_PRIVATE_INGRESS_TOKEN_FIXED_FIXTURE'); token.chmod(0o600)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer SYNTHETIC_PRIVATE_INGRESS_TOKEN_FIXED_FIXTURE'):
                    self.send_error(401); return
                try:
                    value = api.dispatch('POST', self.path, json.loads(self.rfile.read(int(self.headers['Content-Length']))), PEER)
                    raw = json.dumps(value).encode(); self.send_response(200)
                    self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
                except PermissionError:
                    self.send_error(403)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = Client({'control_url': 'http://127.0.0.1:' + str(server.server_address[1]), 'token_file': str(token)})
            access = ProjectEventAccess(policy(), POLICIES, account_active=lambda peer: self.account_live)
            source = RemoteReceiptSource(client, PRINCIPAL, access)
            page = source(None, 20); self.assertEqual(1, len(page['receipts']))
            directory = self.root / 'events'; directory.mkdir(mode=0o700)
            events = MCPEvents(directory, OWNER, authorize=access, transport=Transport())
            events.subscribe(PRINCIPAL, name='task.completed', arguments={'project_id': 'project-one'},
                delivery={'mode': 'webhook', 'url': URL, 'secret': SECRET})
            loop = EventDeliveryLoop(events, receipt_source=source)
            self.assertEqual('accepted', loop.step()['state'])
            events.record_task_event(**dict(page['receipts'][0], event_id='evt-existing-durable'))
            with events.transaction() as db:
                cursor = db.execute("SELECT value FROM event_metadata WHERE key='receipt_cursor'").fetchone()[0]
            with patch.object(client, 'request', side_effect=OSError('fixture feed unavailable')):
                outcome = loop.step()
            self.assertTrue(outcome['receipt_feed_unavailable']); self.assertEqual('accepted', outcome['state'])
            with events.transaction() as db:
                self.assertEqual(cursor, db.execute("SELECT value FROM event_metadata WHERE key='receipt_cursor'").fetchone()[0])
            from starlette.applications import Starlette
            calls = []
            @contextlib.asynccontextmanager
            async def original(app):
                calls.append('startup')
                try:
                    yield {'original': True}
                finally:
                    calls.append('shutdown')
            app = Starlette(lifespan=original); previous = app.router.lifespan_context
            self.assertIsNone(attach_lifecycle(app, events, source)); self.assertIs(previous, app.router.lifespan_context)
            installed = attach_lifecycle(app, events, source, enabled=True)
            async def owned_lifecycle():
                async with app.router.lifespan_context(app) as state:
                    self.assertTrue(state['original']); self.assertTrue(installed.thread.is_alive())
            asyncio.run(owned_lifecycle())
            self.assertFalse(installed.thread.is_alive()); self.assertEqual(['startup', 'shutdown'], calls)
            reopened = MCPEvents(directory, OWNER, authorize=access, transport=Transport())
            with reopened.transaction() as db:
                self.assertEqual(cursor, db.execute("SELECT value FROM event_metadata WHERE key='receipt_cursor'").fetchone()[0])
            self.account_live = False
            with self.assertRaises(PermissionError):
                source(None, 20)
            self.assertFalse(access(PRINCIPAL, 'task.completed', {'project_id': 'project-one'}))
            self.account_live = 1  # Truthy is not an authority acknowledgement.
            self.assertEqual((), access.projects(PRINCIPAL))
            with self.assertRaises(ValueError):
                ProjectEventAccess(policy(), [dict(POLICIES[0], projects=['*'])], account_active=lambda peer: True)
        finally:
            server.shutdown(); server.server_close(); thread.join(3)
            self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
