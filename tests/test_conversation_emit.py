"""Authenticated isolated task/lease/message boundaries; no native/model/SDK."""
import ast
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
import sys

from assistant_mesh import conversation_emit as emit
from assistant_mesh.conversation import ConversationService, utc
from assistant_mesh.conversation_authority import attach as attach_conversation
from assistant_mesh.conversation_runtime import ConversationBridge
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_http import LocalIngressBackend
from assistant_mesh.store import Store, Conflict


SOURCES = ('chatgpt-chat', 'openai-codex-cloud')
OWNER = 'synthetic-owner'


class ConversationEmitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-conversation-emit.')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.store = Store(self.root / 'original.sqlite', clock=lambda: self.now)
        self.ingress = Ingress(self.store, 'original-authority', {'owner_id': OWNER})
        self.results = IngressResults(self.store, 'original-authority', {'owner_id': OWNER})
        self.api = SimpleNamespace(store=self.store, ingress=self.ingress,
                                   dispatch=lambda *args: {'previous_dispatch': True})
        self.peer = {'role': 'worker', 'node': 'original-leader',
                     'capabilities': ['leader', emit.CAPABILITY]}

    def install(self):
        attach_conversation(self.api, sources=SOURCES)
        return emit.attach(self.api, enabled=True, sources=SOURCES)

    def request(self, route, payload, peer=None):
        return self.api.dispatch('POST', route, payload, peer or self.peer)

    def original(self, source=SOURCES[0]):
        principal = {'owner_id': OWNER, 'source': source, 'subject': 'fixed-' + source,
                     'scopes': ['tasks:read', 'tasks:submit']}
        peer = {'role': 'ingress', 'ingress': principal}
        receipt = self.ingress.submit(peer, {'request_id': 'same-original-request', 'input': 'owner task'})
        self.store.heartbeat('original-leader', ['leader'], 20)
        task = self.store.claim('original-leader')
        self.assertEqual(receipt['task_id'], task['id'])
        return principal, task

    def message(self, task, suffix='one', kind='message', text='原任务的自然回复'):
        return {'task_id': task['id'], 'epoch': task['epoch'], 'call_id': 'original-call-' + suffix,
                'message_id': 'original-message-' + suffix, 'kind': kind, 'text': text}

    def count(self):
        with self.store.transaction() as db:
            return db.execute('SELECT COUNT(*) FROM conversation_emits').fetchone()[0]

    def test_default_off_finite_worker_grant_original_lease_and_identity_fences(self):
        previous = self.api.dispatch
        self.assertIsNone(emit.attach(self.api))
        self.assertIs(previous, self.api.dispatch)
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_emits'").fetchone())
        self.install()
        principal, task = self.original()
        message = self.message(task)
        for peer in ({'role': 'operator', 'node': 'original-leader', 'capabilities': [emit.CAPABILITY]},
                     dict(self.peer, capabilities=['leader']), dict(self.peer, node='another-worker')):
            with self.subTest(peer=peer), self.assertRaises((PermissionError, Conflict)):
                self.request(emit.WRITE_ROUTE, message, peer)
        for change in ({'epoch': task['epoch'] + 1}, {'epoch': True}, {'source': SOURCES[1]},
                       {'subject': 'someone'}, {'kind': []}):
            with self.subTest(change=change), self.assertRaises((ValueError, PermissionError)):
                self.request(emit.WRITE_ROUTE, dict(message, **change))
        first = self.request(emit.WRITE_ROUTE, message)
        self.assertTrue(first['created'])
        self.assertFalse(first['human_reviewed'])
        self.assertFalse(self.request(emit.WRITE_ROUTE, message)['created'])
        with self.assertRaises(Conflict):
            self.request(emit.WRITE_ROUTE, dict(message, text='changed'))
        with self.assertRaises(Conflict):
            self.request(emit.WRITE_ROUTE, dict(message, call_id='another-call'))
        with self.assertRaises(PermissionError):
            self.request(emit.WRITE_ROUTE + '?extra=x', message)
        self.assertEqual(1, self.count())
        self.now += 91  # Real task/Leader deadlines expire; status alone stays running.
        with self.assertRaises(Conflict):
            self.request(emit.WRITE_ROUTE, self.message(task, 'after-expiry'))
        original = self.request(emit.RECEIPT_ROUTE,
                                {key: message[key] for key in ('task_id', 'epoch', 'call_id')})
        self.assertEqual(first['message'], original['message'])
        self.assertEqual(1, self.count())

    def test_chat_and_cloud_receive_authored_text_time_and_provenance_without_publish_or_step(self):
        self.install()
        for source in SOURCES:
            with self.subTest(source=source):
                self.now += 1
                principal, task = self.original(source)
                backend = LocalIngressBackend(self.ingress, self.results, source=source)
                folder = self.root / source
                folder.mkdir(mode=0o700)
                service = ConversationService(folder, OWNER, backend, source=source, clock=lambda: self.now)
                binding = {'conversation_id': 'conversation-' + source, 'request_id': 'same-original-request'}
                service.request(principal, 'open', binding)
                api = self.api
                class IngressClient:
                    def request(self, path, body):
                        return api.dispatch('POST', path, body, {'role': 'ingress', 'ingress': principal})
                bridge = ConversationBridge(service, principal, IngressClient())
                receipts = []
                created = utc(self.now)
                for kind in ('progress', 'message', 'final'):
                    receipts.append(self.request(emit.WRITE_ROUTE, self.message(task, kind, kind, source + ' ' + kind)))
                # Existing real native question path is unchanged and not
                # manufactured from an agent's ordinary question text.
                self.store.interaction(task['id'], 'original-leader', task['epoch'], 'native-question-' + source,
                    'question', {'questions': [{'id': 'branch', 'question': 'Which branch?'}]})
                bridge.before_receive(principal, binding)
                before = service.request(principal, 'receive', binding)['messages']
                self.assertTrue(any(item['kind'] == 'question' for item in before))
                self.assertFalse(any(item.get('provenance') for item in before))
                self.store.update_task(task['id'], 'original-leader', task['epoch'],
                                       result='PRIVATE_RAW_RESULT_MUST_NOT_EXPORT', status='completed')
                self.now += 100
                emit.enable_projection(bridge, enabled=True)
                report = bridge.before_receive(principal, binding)
                self.assertEqual(3, report['agent_messages_projected'])
                page = service.request(principal, 'receive', binding)
                authored = [item for item in page['messages'] if item.get('provenance')]
                self.assertEqual(['progress', 'message', 'final'], [item['kind'] for item in authored])
                for item, receipt in zip(authored, receipts):
                    self.assertEqual(created, item['original_time'])
                    self.assertEqual(utc(self.now), item['received_at'])
                    self.assertEqual(receipt['conversation_message_id'], item['message_id'])
                    self.assertEqual(receipt['message']['message_id'], item['provenance']['source_message_id'])
                    self.assertFalse(item['provenance']['human_reviewed'])
                    self.assertEqual('authenticated_task_agent', item['provenance']['producer'])
                self.assertNotIn('PRIVATE_RAW_RESULT_MUST_NOT_EXPORT', json.dumps(page))
                self.assertEqual(0, bridge.before_receive(principal, binding)['agent_messages_projected'])
                for wrong in (dict(principal, subject='another-subject'), dict(principal, owner_id='another-owner'),
                              dict(principal, source=SOURCES[1] if source == SOURCES[0] else SOURCES[0])):
                    with self.assertRaises(PermissionError):
                        self.request(emit.READ_ROUTE, {'request_id': binding['request_id'], 'after': 0, 'limit': 20},
                                     {'role': 'ingress', 'ingress': wrong})
                with self.store.transaction() as db:
                    self.assertEqual(0, db.execute('SELECT COUNT(*) FROM ingress_result_publications').fetchone()[0])

    def test_cli_ack_loss_uses_original_receipt_only_and_source_remains_python36_grammar(self):
        self.install()
        principal, task = self.original()
        payload = self.message(task)
        api = self.api
        peer = self.peer
        class LostAckClient:
            def __init__(self):
                self.writes = 0
            def request(self, route, value):
                result = api.dispatch('POST', route, value, peer)
                if route == emit.WRITE_ROUTE:
                    self.writes += 1
                    raise OSError('synthetic ACK lost AFTER real isolated commit')
                return result
        client = LostAckClient()
        config = self.root / 'worker.json'
        config.write_text(json.dumps({'control_url': 'http://127.0.0.1', 'token_file': '/PRIVATE/UNUSED'}))
        config.chmod(0o600)
        body = self.root / 'message.json'
        body.write_text(json.dumps({key: payload[key] for key in ('message_id', 'kind', 'text')}))
        body.chmod(0o600)
        argv = ['--config', str(config), '--task-id', task['id'], '--epoch', str(task['epoch']),
                '--call-id', payload['call_id']]
        with mock.patch('assistant_mesh.worker.Client', return_value=client), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(2, emit.main(argv + ['emit', '--payload-file', str(body)]))
            self.assertEqual(0, emit.main(argv + ['receipt']))
        self.assertEqual(1, client.writes)
        self.assertEqual(1, self.count())
        for module in (emit,):
            source = Path(module.__file__).read_text()
            ast.parse(source, feature_version=(3, 6)) if sys.version_info >= (3, 8) else ast.parse(source)


if __name__ == '__main__':
    unittest.main()
