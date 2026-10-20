"""Four focused boundaries on actual original3198 + exact tiny source patches.

No SDK, model, network, production state, frozen Worker replacement or skips.
"""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from assistant_mesh import conversation_emit as emit, conversation_final as final
from assistant_mesh.conversation import ConversationService, utc
from assistant_mesh.conversation_authority import attach as attach_bridge
from assistant_mesh.conversation_runtime import ConversationBridge
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_http import LocalIngressBackend
from assistant_mesh.store import Conflict


ROOT = Path(__file__).resolve().parents[1]
BASE = '3198c877234a8fd0c6912fdbe218f47185effedc'
OWNER, NODE = 'synthetic-owner', 'original-leader'
PRINCIPAL = {'owner_id': OWNER, 'source': 'chatgpt-chat', 'subject': 'fixed-chat',
             'scopes': ['tasks:read', 'tasks:submit']}
PEER = {'role': 'ingress', 'ingress': PRINCIPAL}
WORKER = {'role': 'worker', 'node': NODE, 'capabilities': ['leader', emit.CAPABILITY, final.CAPABILITY]}


def frozen_store():
    spec = importlib.util.spec_from_file_location('_final_exact_patch', str(ROOT / 'deploy/build-legacy-fenced-receiver.py'))
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    source = subprocess.run(['git', 'show', BASE + ':assistant_mesh/store.py'], cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=10).stdout
    assert hashlib.sha256(source).hexdigest() == builder.BASE_STORE_SHA256
    source = builder.apply_exact_patch(source, (ROOT / 'deploy/legacy-store-carrier.patch').read_bytes(), 'store.py')
    assert hashlib.sha256(source).hexdigest() == builder.PATCHED_STORE_SHA256
    source = builder.apply_exact_patch(source, (ROOT / 'deploy/legacy-store-conversation-final.patch').read_bytes(), 'store.py')
    module = types.ModuleType('assistant_mesh._verified3198_final_store')
    module.__package__ = 'assistant_mesh'
    exec(compile(source, '<verified-original3198-plus-two-exact-patches>', 'exec'), module.__dict__)
    return module.Store, module.Conflict, source


class ConversationFinalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Store, cls.LegacyConflict, cls.source = frozen_store()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-conversation-final.')
        self.addCleanup(self.temp.cleanup)
        self.root, self.now = Path(self.temp.name), 1000
        # Match serve_entrance's unchanged passive constructor explicitly.
        self.store = self.Store(self.root / 'original.sqlite', clock=lambda: self.now, recover_inflight=False)
        self.ingress = Ingress(self.store, 'original-authority', {'owner_id': OWNER})
        self.results = IngressResults(self.store, 'original-authority', {'owner_id': OWNER})
        def dispatch(method, path, payload, peer):
            if method == 'POST' and path == '/v1/task/update' and peer.get('role') == 'worker':
                return self.store.update_task(payload['id'], peer['node'], payload['epoch'],
                    payload.get('checkpoint'), payload.get('result'), payload.get('status'))
            raise PermissionError('synthetic_original_dispatch_reject')
        self.api = types.SimpleNamespace(store=self.store, ingress=self.ingress, dispatch=dispatch)
        attach_bridge(self.api, sources=('chatgpt-chat', 'openai-codex-cloud'))
        emit.attach(self.api, enabled=True, sources=('chatgpt-chat', 'openai-codex-cloud'))

    def install(self):
        return final.attach(self.api, enabled=True,
            policies=({'source': PRINCIPAL['source'], 'subject': PRINCIPAL['subject'], 'node': NODE},))

    def submit(self, identity, peer=PEER):
        return self.ingress.submit(peer, {'request_id': identity, 'input': 'owner task', 'project_id': 'original-project'})

    def claim(self):
        self.store.heartbeat(NODE, ['leader'], 10)
        return self.store.claim(NODE)

    def complete(self, task, text='原任务代理的自然最终答复', peer=WORKER):
        return self.api.dispatch('POST', '/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
            'checkpoint': {'answer': text, 'side_effect_started': False},
            'result': 'PRIVATE_DIFFERENT_BOUNDED_RESULT', 'status': 'completed'}, peer)

    def bridge(self, request_id):
        directory = self.root / 'conversation'
        directory.mkdir(mode=0o700)
        backend = LocalIngressBackend(self.ingress, self.results)
        service = ConversationService(directory, OWNER, backend, clock=lambda: self.now)
        binding = {'conversation_id': 'same-chat', 'request_id': request_id}
        service.request(PRINCIPAL, 'open', binding)
        api = self.api
        class Client:
            def request(self, path, payload):
                return api.dispatch('POST', path, payload, PEER)
        bridge = ConversationBridge(service, PRINCIPAL, Client())
        emit.enable_projection(bridge, enabled=True)
        return service, bridge, binding

    def scalar(self, sql, args=()):
        with self.store.transaction() as db:
            return db.execute(sql, args).fetchone()[0]

    def test_default_off_new_only_chat_fixed_policy_and_authenticated_completion(self):
        previous = self.api.dispatch
        self.assertIsNone(final.attach(self.api))
        self.assertIs(previous, self.api.dispatch)
        old = self.submit('pre-policy')
        authority = self.install()
        self.assertIs(authority, self.install())
        self.ingress.status(PEER, {'request_id': 'pre-policy'})
        self.submit('pre-policy')  # Replay is NOT new admission.
        self.assertEqual(0, self.scalar('SELECT COUNT(*) FROM conversation_reply_policies'))
        self.complete(self.claim())
        self.assertEqual(0, self.scalar('SELECT COUNT(*) FROM conversation_emits'))
        for peer in (dict(PEER, ingress=dict(PRINCIPAL, source='openai-codex-cloud')),
                     dict(PEER, ingress=dict(PRINCIPAL, subject='another-subject'))):
            self.submit('not-matching', peer)
            self.complete(self.claim())
        self.assertEqual(0, self.scalar('SELECT COUNT(*) FROM conversation_reply_policies'))
        accepted = self.submit('new-chat')
        task = self.claim()
        self.assertEqual(task['id'], accepted['task_id'])
        for peer in (dict(WORKER, capabilities=['leader', emit.CAPABILITY]), dict(WORKER, node='foreign-worker')):
            with self.subTest(peer=peer), self.assertRaises((PermissionError, self.LegacyConflict)):
                self.complete(task, peer=peer)
        with self.assertRaises(PermissionError):
            self.store.update_task(task['id'], NODE, task['epoch'], {'answer': 'not-authenticated'}, status='completed')
        self.assertEqual('running', self.scalar('SELECT status FROM tasks WHERE id=?', (task['id'],)))
        self.complete(task)
        self.assertEqual(1, self.scalar('SELECT COUNT(*) FROM conversation_emits'))
        self.assertEqual(0, self.scalar('SELECT COUNT(*) FROM ingress_result_publications'))
        for source in (self.source, Path(final.__file__).read_bytes(), Path(emit.__file__).read_bytes()):
            ast.parse(source.decode('utf8'), feature_version=(3, 6)) if sys.version_info >= (3, 8) else ast.parse(source)

    def test_completion_ack_loss_closed_lease_and_projection_failure_only_restore_original_reply(self):
        authority = self.install()
        self.submit('new-chat')
        task = self.claim()
        service, bridge, binding = self.bridge('new-chat')
        natural = '原任务答复；不是完整history/tool stdout。' * 350  # Exercise deterministic bounded text.
        def lost_ack():
            self.complete(task, natural)
            raise OSError('synthetic ACK lost AFTER the completion transaction committed')
        with self.assertRaises(OSError):
            lost_ack()
        self.now += 100
        self.assertEqual('completed', self.scalar('SELECT status FROM tasks WHERE id=?', (task['id'],)))
        self.assertIsNone(self.claim())  # No business replay to repair delivery.
        payload = {'task_id': task['id'], 'epoch': task['epoch']}
        original = self.api.dispatch('POST', final.RECEIPT_ROUTE, payload, WORKER)
        self.assertTrue(original['found'])
        self.assertFalse(original['human_reviewed'])
        self.assertIn('truncated', original['message']['text'])
        self.assertLessEqual(len(original['message']['text'].encode('utf8')), 8192)
        with mock.patch.object(service, 'publish', side_effect=OSError('synthetic projection unavailable')):
            with self.assertRaises(OSError):
                bridge.before_receive(PRINCIPAL, binding)
        self.assertEqual(1, self.scalar('SELECT COUNT(*) FROM conversation_emits'))
        report = bridge.before_receive(PRINCIPAL, binding)
        messages = service.request(PRINCIPAL, 'receive', binding)['messages']
        finals = [message for message in messages if message['kind'] == 'final']
        self.assertEqual(1, report['agent_messages_projected'])
        self.assertEqual(1, len(finals))
        self.assertEqual(original['message']['text'], finals[0]['text'])
        self.assertEqual(original['message']['created_at'], finals[0]['original_time'])
        self.assertEqual(utc(self.now), finals[0]['received_at'])
        self.assertNotIn('PRIVATE_DIFFERENT_BOUNDED_RESULT', json.dumps(messages))
        self.assertEqual(original['conversation_message_id'], finals[0]['message_id'])
        self.assertEqual(0, bridge.before_receive(PRINCIPAL, binding)['agent_messages_projected'])
        self.assertEqual(original['event'], authority.completion_events()['events'][0])
        self.assertEqual('original-project', original['event']['project_id'])
        self.assertEqual(original, self.api.dispatch('POST', final.RECEIPT_ROUTE, payload, WORKER))
        with self.assertRaises(self.LegacyConflict):
            self.complete(task, natural)  # Terminal ACK loss does not authorize update replay.

    def test_insert_and_task_update_rollback_together_preserve_frozen_no_replay_guard(self):
        self.install()
        self.submit('new-chat')
        task = self.claim()
        self.store.update_task(task['id'], NODE, task['epoch'], {'side_effect_started': True})
        # Fail the task UPDATE AFTER both message INSERTs, proving the shared
        # transaction cannot leave either a committed final or completion alone.
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER synthetic_final_failure BEFORE UPDATE OF status ON tasks "
                       "WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete(task)
        self.assertEqual('running', self.scalar('SELECT status FROM tasks WHERE id=?', (task['id'],)))
        self.assertEqual(0, self.scalar('SELECT COUNT(*) FROM conversation_emits'))
        self.assertEqual(0, self.scalar('SELECT COUNT(*) FROM conversation_task_finals'))
        self.store.update_task(task['id'], NODE, task['epoch'], {'runtime_failure': {'phase': 'task_finalize'}},
                               result='unconfirmed finalize', status='waiting_backend')
        self.now += 100
        self.assertIsNone(self.claim())
        self.assertEqual('needs_review', self.scalar('SELECT status FROM tasks WHERE id=?', (task['id'],)))
        missing = self.api.dispatch('POST', final.RECEIPT_ROUTE,
            {'task_id': task['id'], 'epoch': task['epoch']}, WORKER)
        self.assertFalse(missing['found'])
        self.assertFalse(missing['retry_authorized'])

    def test_projected_authored_reply_retains_original_epoch_after_task_reclaim(self):
        self.submit('new-chat')
        task = self.claim()
        service, bridge, binding = self.bridge('new-chat')
        emitted = self.api.dispatch('POST', emit.WRITE_ROUTE,
            {'task_id': task['id'], 'epoch': task['epoch'], 'call_id': 'original-question',
             'message_id': 'original-question', 'kind': 'message', 'text': '请确认原任务分支。'}, WORKER)
        bridge.before_receive(PRINCIPAL, binding)
        target = emitted['conversation_message_id']
        self.store.update_task(task['id'], NODE, task['epoch'], {'side_effect_started': False}, status='continuing')
        self.now += 31
        reclaimed = self.claim()
        self.assertEqual(task['id'], reclaimed['id'])
        self.assertEqual(task['epoch'] + 1, reclaimed['epoch'])
        service.request(PRINCIPAL, 'reply', dict(binding, message_id='original-reply', text='原问题的答案', reply_to=target))
        receipt = bridge.after_reply(PRINCIPAL, binding, 'original-reply')
        self.assertEqual('stale_original_task_epoch', receipt['state'])
        self.assertEqual(task['epoch'], receipt['task_epoch'])
        self.assertEqual(0, self.scalar('SELECT COUNT(*) FROM steering'))
        identity, unused = bridge._context(binding)
        with service.transaction() as db:
            db.execute('DELETE FROM conversation_emit_projections')
            db.execute('UPDATE conversation_bridge_publications SET task_epoch=? WHERE identity=? AND message_id=?',
                       (reclaimed['epoch'], identity, target))
        with self.assertRaisesRegex(Conflict, 'original_publication_epoch_changed'):
            bridge.before_receive(PRINCIPAL, binding)


if __name__ == '__main__':
    unittest.main()
