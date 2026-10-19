"""Idle-owner entrance over actual SQLite/Ingress; no provider/model/account."""
import hashlib
import json
from http.server import HTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from assistant_mesh.cloud_entrance import CloudEntrance, FORMAT, GRANT_FORMAT, REQUEST_FORMAT, assemble, identity
from assistant_mesh.cloud_http import make_http_handler
from assistant_mesh.cloud_membership import DISPLAY_ID
from assistant_mesh.conversation_authority import ConversationAuthority, ROUTE
from assistant_mesh.conversation_sync import sync_original_message
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_http import RemoteIngressBackend
from assistant_mesh.store import Conflict, Store


TOKEN = 'SYNTHETIC_IDLE_CLOUD_' + 'x' * 40
SECOND_TOKEN = 'SYNTHETIC_REAUTHORIZED_CLOUD_' + 'y' * 40
OWNER, SUBJECT = 'synthetic-owner', 'owner-cloud-entrance'
PRINCIPAL = {'owner_id': OWNER, 'source': DISPLAY_ID, 'subject': SUBJECT,
             'scopes': ['tasks:read', 'tasks:submit']}
PEER = {'role': 'ingress', 'ingress': PRINCIPAL}


class CloudEntranceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-cloud-idle-entrance.')
        self.addCleanup(self.temporary.cleanup)
        self.root, self.now = Path(self.temporary.name), 1000
        self.store = Store(self.root / 'native.sqlite', clock=lambda: self.now)
        self.ingress = Ingress(self.store, 'actual-authority', {'owner_id': OWNER})
        self.results = IngressResults(self.store, 'actual-authority', {'owner_id': OWNER})
        self.authority = ConversationAuthority(self.ingress, sources=(DISPLAY_ID,))
        self.policy = {'format': FORMAT, 'owner_binding': OWNER, 'subject': SUBJECT,
                       'project_id': 'fixed-project', 'callback_url': 'https://mesh.example.test/cloud/node'}
        fixture = self
        class Client:
            submissions = 0
            deliveries = 0
            lose_submit = False
            lose_deliver = False
            block_receipt = False
            after_submit = None
            def request(client, path, body):
                if path == '/v1/ingress/tasks':
                    result = fixture.ingress.submit(PEER, body)
                    client.submissions += 1
                    if client.after_submit:
                        client.after_submit()
                    if client.lose_submit:
                        raise OSError('synthetic ACK loss after actual task commit')
                    return result
                if path == '/v1/ingress/task/status':
                    return fixture.ingress.status(PEER, body)
                if path == '/v1/ingress/task/result':
                    return fixture.results.read(PEER, body)
                fixture.assertEqual(ROUTE, path)
                if body['action'] == 'receipt' and client.block_receipt:
                    raise OSError('synthetic original receipt unavailable')
                result = fixture.authority.dispatch(PEER, body)
                if body['action'] == 'deliver':
                    client.deliveries += 1
                    if client.lose_deliver:
                        raise OSError('synthetic native ACK loss after actual commit')
                return result
        self.client = Client()
        self.profile = self.reopen()
        self.grant = self.authorization('original-owner-authorization')

    def reopen(self, policy=None):
        return assemble(self.root / 'entrance', self.root / 'conversation', policy or self.policy,
                        self.client, clock=lambda: self.now)

    def authorization(self, grant_id, expires_at=2000, readonly=False):
        return {'format': GRANT_FORMAT, 'grant_id': grant_id, 'entrance_id': identity(self.policy)['entrance_id'],
                'expires_at': expires_at, 'scopes': ['node:hello', 'tasks:read'] + ([] if readonly else ['tasks:submit'])}

    def install(self, grant=None, token=TOKEN):
        return self.profile.install(grant or self.grant, token_sha256=hashlib.sha256(token.encode()).hexdigest(), authorized=True)

    def envelope(self, action, arguments=None, request_id=None):
        return dict(identity(self.policy), format=REQUEST_FORMAT, action=action,
                    arguments=arguments or {}, request_id=request_id or action)

    def call(self, action, arguments=None, request_id=None, token=TOKEN):
        return self.profile.handle(token, self.envelope(action, arguments, request_id))

    def active(self):
        self.install()
        self.call('hello')

    def tasks(self):
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute('SELECT id,status,node,epoch FROM tasks')]

    def test_idle_http_join_submit_status_need_no_task_claim_or_keeper(self):
        self.assertEqual([], self.tasks())
        self.install()
        self.assertEqual([], self.tasks())
        handler = make_http_handler(self.profile, self.policy['callback_url'],
            trusted_proxy_peers=('127.0.0.1',), max_request_bytes=65536)
        server = HTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(action, arguments=None, request_id=None):
            request = urllib.request.Request('http://127.0.0.1:{}/cloud/node'.format(server.server_port),
                data=json.dumps(self.envelope(action, arguments, request_id)).encode(),
                headers={'Host': 'mesh.example.test', 'X-Forwarded-Proto': 'https',
                         'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
                return json.load(response)
        try:
            hello = post('hello')
            self.assertFalse(hello['provider_identity_verified'])
            self.assertEqual([], self.tasks())
            submitted = post('task_submit', {'input': 'owner work while Leader is idle'}, 'original-entry')
            same = post('task_submit', {'input': 'owner work while Leader is idle'}, 'original-entry')
            self.assertEqual(submitted, same)
            status = post('task_status', {'entry_request_id': 'original-entry'})
            self.assertEqual(submitted['data']['task_id'], status['data']['task_id'])
            self.assertEqual('pending', status['data']['status'])
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()
        self.assertEqual(1, len(self.tasks()))
        self.assertIsNone(self.tasks()[0]['node'])
        self.assertEqual(0, self.tasks()[0]['epoch'])
        self.assertEqual(1, self.client.submissions)
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0])
        bootstrap = self.profile.bootstrap(self.grant['grant_id'])
        self.assertFalse(bootstrap['credentials_included'])
        self.assertNotIn(TOKEN, json.dumps(bootstrap))
        self.assertEqual('owner_authorized_unattested_entrance', bootstrap['provenance'])
        self.assertEqual('cloud-entrances.sqlite', self.profile.database.name)
        with self.assertRaises(ValueError):
            self.profile.handle(TOKEN, dict(self.envelope('status'), task_id='fake', epoch=1))

    def test_submit_unknown_survives_reopen_and_new_authorization_without_reposting(self):
        self.active()
        self.client.lose_submit = True
        with self.assertRaises(OSError):
            self.call('task_submit', {'input': 'original work'}, 'original-entry')
        self.assertEqual(1, len(self.tasks()))
        self.client.lose_submit = False
        self.profile = self.reopen()
        replay = self.call('task_submit', {'input': 'original work'}, 'original-entry')
        self.assertEqual('unknown', replay['data']['state'])
        self.assertTrue(self.call('task_status', {'entry_request_id': 'original-entry'})['data']['accepted'])
        new_grant = self.authorization('explicit-second-authorization', expires_at=3000)
        self.install(new_grant, SECOND_TOKEN)
        self.call('hello', token=SECOND_TOKEN)
        replay = self.call('task_submit', {'input': 'original work'}, 'original-entry', token=SECOND_TOKEN)
        self.assertEqual('unknown', replay['data']['state'])
        self.assertEqual(1, self.client.submissions)
        with self.assertRaises(Conflict):
            self.call('task_submit', {'input': 'changed original work'}, 'original-entry', token=SECOND_TOKEN)

    def test_fixed_identity_scopes_and_route_allowlist_never_become_worker_or_operator(self):
        wrong_source = RemoteIngressBackend({SUBJECT: self.client}, OWNER)  # default Chat source
        with self.assertRaises(ValueError):
            CloudEntrance(self.root / 'wrong-source', self.policy, wrong_source)
        self.assertFalse((self.root / 'wrong-source').exists())
        with self.assertRaises(PermissionError):
            self.profile.install(self.grant, token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest())
        self.active()
        self.call('task_submit', {'input': 'original work'}, 'original-entry')
        context = {'conversation_id': 'original-conversation', 'request_id': 'original-entry'}
        self.call('conversation_open', context)
        readonly = self.authorization('explicit-readonly', readonly=True)
        self.install(readonly, SECOND_TOKEN)
        self.call('hello', request_id='read-hello', token=SECOND_TOKEN)
        self.assertTrue(self.call('task_status', {'entry_request_id': 'original-entry'}, token=SECOND_TOKEN)['data']['accepted'])
        self.call('conversation_receive', context, token=SECOND_TOKEN)
        for action, arguments in (('task_submit', {'input': 'denied'}),
                                  ('conversation_reply', dict(context, message_id='denied', text='denied')),
                                  ('call', {'capability_id': 'shell', 'input': {}}),
                                  ('revoke', {'grant_id': readonly['grant_id']})):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                self.call(action, arguments, request_id='denied-' + action, token=SECOND_TOKEN)
        with self.assertRaises(ValueError):
            self.profile.handle(TOKEN, dict(self.envelope('status'), node_id='cloud'))
        with self.assertRaises(ValueError):
            self.call('conversation_receive', dict(context, source='chatgpt-chat'))
        changed = dict(self.policy, subject='different-owner-subject')
        with self.assertRaises(Conflict):
            self.reopen(changed)
        self.assertEqual(1, self.client.submissions)
        self.assertEqual(0, self.client.deliveries)

    def test_expiry_revocation_and_receive_recheck_do_not_renew_or_disclose(self):
        self.active()
        self.now = 2100
        with self.assertRaises(PermissionError):
            self.call('task_submit', {'input': 'expired'}, 'expired-entry')
        self.assertEqual([], self.tasks())
        second = self.authorization('explicit-second', expires_at=3000)
        self.install(second, SECOND_TOKEN)
        self.call('hello', token=SECOND_TOKEN)
        self.profile.revoke(second['grant_id'], authorized=True)
        with self.assertRaises(PermissionError):
            self.call('status', token=SECOND_TOKEN)
        self.assertEqual('revoked', self.install(second, SECOND_TOKEN)['grant_state'])
        third_token = 'SYNTHETIC_THIRD_AUTHORIZATION_' + 'z' * 40
        self.install(self.authorization('explicit-third', expires_at=4000), third_token)
        self.call('hello', token=third_token)
        self.call('task_submit', {'input': 'current owner work'}, 'current-entry', token=third_token)
        context = {'conversation_id': 'current-conversation', 'request_id': 'current-entry'}
        self.call('conversation_open', context, token=third_token)
        service = self.profile.conversation
        original = service.request
        def expire_after_receive(principal, action, args):
            result = original(principal, action, args)
            if action == 'receive':
                self.now = 4100
            return result
        service.request = expire_after_receive
        with self.assertRaises(PermissionError):
            self.call('conversation_receive', context, token=third_token)

    def test_actual_question_answer_and_lost_native_ack_use_original_receipt(self):
        self.active()
        submitted = self.call('task_submit', {'input': 'original work'}, 'original-entry')
        context = {'conversation_id': 'original-conversation', 'request_id': 'original-entry'}
        self.call('conversation_open', context)
        pending = self.call('conversation_receive', context)
        self.assertFalse(pending['data']['runtime']['approved_result_available'])
        self.store.heartbeat('actual-leader', ['leader'], 10)
        task = self.store.claim('actual-leader')
        self.assertEqual(submitted['data']['task_id'], task['id'])
        self.store.interaction(task['id'], 'actual-leader', task['epoch'], 'original-question', 'question',
                               {'questions': [{'id': 'which', 'question': 'Which branch?'}]})
        page = self.call('conversation_receive', context)
        question = next(message for message in page['data']['messages'] if message['kind'] == 'question')
        answer = dict(context, message_id='original-answer', text='same branch', reply_to=question['message_id'])
        replied = self.call('conversation_reply', answer)
        self.assertEqual('native_answer_recorded', replied['data']['runtime']['state'])
        self.assertEqual({'answers': {'which': {'answers': ['same branch']}}},
            self.store.interaction(task['id'], 'actual-leader', task['epoch'], 'original-question')['answer'])
        self.client.lose_deliver = self.client.block_receipt = True
        steering = dict(context, message_id='original-steering', text='continue the same work')
        self.call('conversation_reply', steering, 'original-reply')
        self.client.lose_deliver = self.client.block_receipt = False
        delivered = self.client.deliveries
        receipt = self.call('conversation_receipt', dict(context, message_id='original-steering'))
        self.assertEqual('queued_for_native', receipt['data']['runtime']['state'])
        self.call('conversation_reply', steering, 'original-reply')
        self.call('conversation_reply', steering, 'same-message-another-envelope')
        self.assertEqual(delivered, self.client.deliveries)
        self.assertFalse(receipt['execution_verified'])
        self.assertFalse(receipt['data']['runtime']['native_consumption_verified'])

    def test_result_without_live_claim_is_only_original_owner_approved_publication(self):
        self.active()
        self.call('task_submit', {'input': 'original work'}, 'original-entry')
        self.store.heartbeat('actual-leader', ['leader'], 10)
        task = self.store.claim('actual-leader')
        private = 'PRIVATE_NATIVE_RESULT_NEVER_RETURN'
        self.store.update_task(task['id'], 'actual-leader', task['epoch'],
                               {'side_effect_started': False}, private, 'completed')
        with self.assertRaises(PermissionError):
            self.call('task_result', {'entry_request_id': 'original-entry'})
        self.results.publish({'role': 'operator'}, {'publication_id': 'original-reviewed-publication',
            'source': DISPLAY_ID, 'subject': SUBJECT, 'request_id': 'original-entry',
            'task_id': task['id'], 'task_epoch': task['epoch'],
            'task_result_sha256': hashlib.sha256(private.encode()).hexdigest(), 'reviewed': True,
            'result': {'summary': 'Owner approved result', 'artifacts': []}})
        result = self.call('task_result', {'entry_request_id': 'original-entry'})
        self.assertTrue(result['data']['published'])
        self.assertNotIn(private, json.dumps(result))
        context = {'conversation_id': 'completed-conversation', 'request_id': 'original-entry'}
        self.call('conversation_open', context)
        page = self.call('conversation_receive', context)
        self.assertTrue(any(message['kind'] == 'result' for message in page['data']['messages']))
        self.assertNotIn(private, json.dumps(page))

    def test_pending_to_natural_claim_sync_original_ack_and_unknown_never_repost(self):
        self.active()
        submitted = self.call('task_submit', {'input': 'original work'}, 'original-entry')
        context = {'conversation_id': 'original-conversation', 'request_id': 'original-entry'}
        self.call('conversation_open', context)
        for message_id in ('original-pending-one', 'original-pending-two', 'original-pending-three'):
            reply = self.call('conversation_reply', dict(context, message_id=message_id,
                              text='original instruction ' + message_id), 'reply-' + message_id)
            self.assertEqual('awaiting_original_live_task', reply['data']['runtime']['state'])
        original = dict(context, message_id='original-pending-one')
        pending = self.call('conversation_sync', original, 'sync-before-claim')
        self.assertEqual('awaiting_original_live_task', pending['data']['runtime']['state'])
        self.assertFalse(pending['data']['runtime']['post_attempted'])
        self.install(self.authorization('explicit-readonly', readonly=True), SECOND_TOKEN)
        self.call('hello', request_id='read-hello', token=SECOND_TOKEN)
        self.call('conversation_receive', context, token=SECOND_TOKEN)
        self.call('conversation_receipt', original, token=SECOND_TOKEN)
        with self.assertRaises(PermissionError):
            self.call('conversation_sync', original, 'denied-sync', token=SECOND_TOKEN)
        with self.assertRaises(PermissionError):
            sync_original_message(self.profile.conversation, dict(PRINCIPAL, scopes=['tasks:read']), original)
        self.assertEqual(0, self.client.deliveries)

        # Only the existing normal worker claim progresses pending business;
        # the entrance creates no claim/task/keeper to enable its own grant.
        self.store.heartbeat('actual-leader', ['leader'], 10)
        task = self.store.claim('actual-leader')
        self.assertEqual(submitted['data']['task_id'], task['id'])
        self.call('conversation_receive', context)
        self.call('conversation_receipt', original)
        self.assertEqual(0, self.client.deliveries)  # Even a write token's read cannot drain.
        queued = self.call('conversation_sync', original, 'sync-after-claim')
        self.assertEqual('queued_for_native', queued['data']['runtime']['state'])
        self.assertEqual(1, self.client.deliveries)
        steering = self.store.poll_steering(task['id'], 'actual-leader', task['epoch'])['steering']
        self.assertEqual('original instruction original-pending-one', steering['text'])
        self.store.poll_steering(task['id'], 'actual-leader', task['epoch'], steering['id'], 'submitted')
        ack = self.call('conversation_receipt', original)
        self.assertTrue(ack['data']['runtime']['native_submission_acknowledged'])
        self.assertFalse(ack['data']['runtime']['native_consumption_verified'])
        self.call('conversation_sync', original, 'sync-after-claim')
        self.call('conversation_sync', original, 'same-message-sync-receipt')
        self.assertEqual(1, self.client.deliveries)

        second = dict(context, message_id='original-pending-two')
        self.client.lose_deliver = self.client.block_receipt = True
        unknown = self.call('conversation_sync', second, 'original-second-sync')
        self.assertEqual('delivery_unknown', unknown['data']['runtime']['state'])
        self.assertEqual(2, self.client.deliveries)
        self.client.lose_deliver = self.client.block_receipt = False
        self.profile = self.reopen()
        self.call('conversation_sync', second, 'original-second-sync')
        observed = self.call('conversation_sync', second, 'original-second-receipt-sync')
        self.assertEqual('queued_for_native', observed['data']['runtime']['state'])
        steering = self.store.poll_steering(task['id'], 'actual-leader', task['epoch'])['steering']
        self.assertEqual('original instruction original-pending-two', steering['text'])
        self.store.poll_steering(task['id'], 'actual-leader', task['epoch'], steering['id'], 'submitted')
        self.assertTrue(self.call('conversation_receipt', second)['data']['runtime']['native_submission_acknowledged'])
        self.assertEqual(2, self.client.deliveries)
        self.assertEqual(1, self.client.submissions)
        self.assertEqual(1, len(self.tasks()))

        # A process/bridge failure BEFORE native intent is also unknown. A new
        # sync envelope must not treat missing native receipt as replay permission.
        third = dict(context, message_id='original-pending-three')
        bridge = self.profile.conversation.runtime
        original_after_reply = bridge.after_reply
        def stop_before_native(*args, **kwargs):
            raise OSError('synthetic process interruption before bridge/native intent')
        bridge.after_reply = stop_before_native
        with self.assertRaises(OSError):
            self.call('conversation_sync', third, 'original-third-sync')
        bridge.after_reply = original_after_reply
        self.assertEqual('unknown', self.call('conversation_sync', third, 'original-third-sync')['data']['state'])
        self.profile = self.reopen()
        unknown = self.call('conversation_sync', third, 'original-third-receipt-sync')
        self.assertEqual('sync_delivery_unknown', unknown['data']['runtime']['state'])
        self.assertEqual(2, self.client.deliveries)

    def test_revocation_during_submit_keeps_committed_original_for_new_grant(self):
        self.active()
        self.client.after_submit = lambda: self.profile.revoke(self.grant['grant_id'], authorized=True)
        with self.assertRaises(PermissionError):
            self.call('task_submit', {'input': 'original work'}, 'original-entry')
        self.assertEqual(1, len(self.tasks()))
        self.client.after_submit = None
        self.install(self.authorization('explicit-second', expires_at=3000), SECOND_TOKEN)
        self.call('hello', token=SECOND_TOKEN)
        response = self.call('task_submit', {'input': 'original work'}, 'original-entry', token=SECOND_TOKEN)
        self.assertTrue(response['data']['accepted'])
        self.assertEqual(1, self.client.submissions)


if __name__ == '__main__':
    unittest.main()
