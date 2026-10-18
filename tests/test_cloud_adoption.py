"""Owner-started adoption and Cloud-principal conversation; no real provider.

Actual private SQLite/leases/questions and bounded loopback transport are used.
Provider/owner authentication remains synthetic, never a Cloud execution claim.
"""
import base64
import hashlib
import json
from http.server import HTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request

from assistant_mesh.cloud_adoption import CloudAdoptions
from assistant_mesh.cloud_http import make_http_handler
from assistant_mesh.cloud_jobs import CloudJobs, CommandResult
from assistant_mesh.cloud_membership import CloudMembership, DISPLAY_ID, node_id
from assistant_mesh.conversation import ConversationService
from assistant_mesh.conversation_authority import ConversationAuthority, ROUTE
from assistant_mesh.conversation_runtime import ConversationBridge
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_http import LocalIngressBackend
from assistant_mesh.store import Conflict, Store


TOKEN = 'SYNTHETIC_ADOPTED_NODE_' + 'x' * 40
OWNER = 'synthetic-owner'
BINDING = {'mesh_task_id': 'owner-enrollment-work', 'mesh_epoch': 1, 'request_id': 'original-owner-join'}


class CloudAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mesh-cloud-adopt.')
        self.addCleanup(self.tmp.cleanup)
        self.root, self.now = Path(self.tmp.name), 1000
        self.jobs = CloudAdoptions(self.root / 'adopted', OWNER, clock=lambda: self.now)
        self.job = self.jobs.adopt(BINDING, authorized=True)['job_id']
        self.membership = CloudMembership(self.jobs)
        self.document = {
            'format': 'mesh-cloud-membership/1', **BINDING, 'lease_id': 'owner-enrollment-lease',
            'expires_at': 2000, 'callback_url': 'https://mesh.example.test/cloud/node',
            'scopes': ['node:hello', 'task:read', 'artifact:write', 'task:report', 'tasks:submit'],
            'capabilities': [], 'enrollment': {
                'format': 'mesh-enrollment/1', 'mesh_id': 'synthetic-mesh', 'node_id': node_id(self.job),
                'owner_binding': OWNER, 'repo_pin': {'repository': 'https://example.test/mesh', 'commit': 'a' * 40},
                'runtime': {'kind': 'temporary', 'expires_at': 2000, 'state_ref': 'mesh:durable-state'},
                'join': {'peer_id': 'synthetic-peer', 'authority_id': 'synthetic-authority',
                    'route_ref': 'route:existing-https', 'credential_ref': 'secret:scoped-mesh-join',
                    'trust_ref': 'trust:bound-owner'},
                'continuation': {'project_id': 'mesh-project', 'session_ref': 'session:original', 'state_ref': 'mesh:original'}}}

    def prepare(self):
        return self.membership.prepare(self.job, self.document,
            token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(), authorized=True)

    def envelope(self, action, arguments=None, request_id=None):
        return {'format': 'mesh-cloud-request/1', 'job_id': self.job, 'task_id': BINDING['mesh_task_id'],
            'epoch': 1, 'lease_id': self.document['lease_id'], 'request_id': request_id or action,
            'action': action, 'arguments': arguments or {}}

    def call(self, action, arguments=None, request_id=None):
        return self.membership.handle(TOKEN, self.envelope(action, arguments, request_id))

    def observation(self):
        return {'provider_task_id': 'already_saved_official_task',
            'provider_url': 'https://chatgpt.com/saved/already_saved_official_task',
            'environment_id': 'original-published-binding',
            'observation': {'evidence_sha256': 'b' * 64, 'observed_at': self.now - 10}}

    def test_owner_started_join_never_touches_original_launch_unknown_or_cli(self):
        calls = []
        def lost(argv, **kwargs):
            calls.append(argv)
            return CommandResult(0, b'NO_PROVIDER_RECEIPT', b'')
        legacy = CloudJobs(self.root / 'legacy', OWNER, {'legacy-env': ['main']},
            executable='/synthetic/codex', codex_home='/synthetic/home', runner=lost, clock=lambda: self.now)
        original = legacy.intent(dict(BINDING, request_id='older-unknown', environment_id='legacy-env',
                                      branch='main', prompt='synthetic older work'))['job_id']
        before = legacy.submit(original, authorized=True)
        self.assertEqual('submission_unknown', before['submission_state'])
        self.prepare()
        hello = self.call('hello')
        self.assertTrue(hello['cloud_node_enrolled'])
        self.assertFalse(hello['provider_identity_verified'])
        self.assertEqual('owner_authorized_unattested_entrance', hello['provenance'])
        self.assertEqual(before, legacy.get(original))
        self.assertEqual(1, len(calls))
        self.assertFalse(hasattr(self.jobs, 'submit'))
        self.assertFalse(self.jobs.get(self.job)['submission_attempted'])
        self.assertNotEqual(self.jobs.database, legacy.database)
        with self.assertRaises(Conflict):
            self.jobs.adopt(dict(BINDING, mesh_epoch=2), authorized=True)

    def test_claimed_url_cannot_promote_provenance_but_independent_observation_can(self):
        self.prepare()
        self.call('hello')
        with self.assertRaises(PermissionError):
            self.jobs.record_provider_observation(self.job, self.observation(), authorized=True)
        self.jobs.verifier = lambda binding, receipt: 1
        with self.assertRaises(PermissionError):
            self.jobs.record_provider_observation(self.job, self.observation(), authorized=True)
        seen = []
        self.jobs.verifier = lambda binding, receipt: seen.append((binding, receipt)) or True
        observed = self.jobs.record_provider_observation(self.job, self.observation(), authorized=True)
        self.assertEqual(OWNER, seen[0][0]['owner_binding'])
        self.assertTrue(observed['provider_identity_verified'])
        self.assertFalse(observed['execution_verified'])
        self.assertFalse(observed['submission_attempted'])
        self.assertTrue(self.membership.get(self.job)['provider_identity_verified'])
        self.jobs = CloudAdoptions(self.root / 'adopted', OWNER, clock=lambda: self.now)
        self.assertTrue(self.jobs.get(self.job)['provider_identity_verified'])
        with self.assertRaises(PermissionError):
            self.call('record_provider_observation', self.observation())
        bad = dict(self.observation(), provider_task_id='client-new-thread-1')
        self.jobs.verifier = lambda *args: True
        with self.assertRaises(ValueError):
            self.jobs.record_provider_observation(self.job, bad, authorized=True)
        other = self.jobs.adopt(dict(BINDING, request_id='other-owner-join'), authorized=True)['job_id']
        with self.assertRaises(Conflict):
            self.jobs.record_provider_observation(other, self.observation(), authorized=True)

    def test_adoption_does_not_grant_other_owner_or_unvalidated_business_authority(self):
        with self.assertRaises(PermissionError):
            self.jobs.adopt(dict(BINDING, request_id='unapproved'))
        with self.assertRaises(Conflict):
            CloudAdoptions(self.root / 'adopted', 'different-owner')
        self.document['enrollment']['owner_binding'] = 'different-owner'
        with self.assertRaises(PermissionError):
            self.prepare()
        self.document['enrollment']['owner_binding'] = OWNER
        self.document['scopes'].append('capabilities:call')
        self.document['capabilities'] = ['synthetic-business']
        calls = []
        self.membership.gateway = lambda *args: calls.append(args)
        self.prepare()
        self.call('hello')
        with self.assertRaises(PermissionError):
            self.call('call', {'capability_id': 'synthetic-business', 'input': {}})
        self.assertEqual([], calls)
        self.membership.close(self.job, authorized=True)
        with self.assertRaises(PermissionError):
            self.call('status')

    def test_same_existing_wire_real_http_full_artifact_survives_expiry(self):
        self.prepare()
        server = HTTPServer(('127.0.0.1', 0), make_http_handler(self.membership,
            self.document['callback_url'], trusted_proxy_peers=('127.0.0.1',)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # isolated loopback only
        def post(action, args=None):
            request = urllib.request.Request('http://127.0.0.1:{}/cloud/node'.format(server.server_port),
                data=json.dumps(self.envelope(action, args)).encode(),
                headers={'Host': 'mesh.example.test', 'X-Forwarded-Proto': 'https',
                         'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN})
            with opener.open(request, timeout=5) as response:
                return json.load(response)
        try:
            self.assertFalse(post('hello')['provider_identity_verified'])
            raw = bytes(range(256)) * 600
            saved = post('artifact', {'name': 'full.bin', 'encoding': 'base64', 'content_b64': base64.b64encode(raw).decode(),
                'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'media_type': 'application/octet-stream'})
            post('report', {'status': 'completed', 'result': 'complete synthetic result\n', 'artifacts': [saved['sha256']]})
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
        self.now = 2100
        reopened = CloudMembership(CloudAdoptions(self.root / 'adopted', OWNER, clock=lambda: self.now))
        self.assertEqual(raw, reopened.read_artifact(self.job, 'full.bin'))
        self.assertEqual('complete synthetic result\n', reopened.saved_result(self.job)['result'])

    def test_cloud_principal_original_question_reply_and_unknown_receipt_share_existing_bridge(self):
        store = Store(self.root / 'native.sqlite', clock=lambda: self.now)
        ingress = Ingress(store, 'synthetic-authority', {'owner_id': OWNER})
        results = IngressResults(store, 'synthetic-authority', {'owner_id': OWNER})
        backend = LocalIngressBackend(ingress, results, source=DISPLAY_ID)
        directory = self.root / 'conversations'
        directory.mkdir(mode=0o700)
        service = ConversationService(directory, OWNER, backend, source=DISPLAY_ID, clock=lambda: self.now)
        self.membership = CloudMembership(self.jobs, ingress=ingress, conversation_service=service,
                                          lease_validator=lambda binding: True)
        self.prepare()
        self.call('hello')
        accepted = self.call('task_submit', {'input': 'synthetic owner work'}, 'entry-original')
        principal = {'owner_id': OWNER, 'source': DISPLAY_ID, 'subject': node_id(self.job),
                     'scopes': ['tasks:read', 'tasks:submit']}
        authority = ConversationAuthority(ingress, sources=(DISPLAY_ID,))
        peer = {'role': 'ingress', 'ingress': principal}
        class Client:
            deliveries = 0
            lose = False
            def request(client, path, body):
                self.assertEqual(ROUTE, path)
                if body['action'] == 'receipt' and client.lose:
                    raise OSError('synthetic receipt unavailable')
                result = authority.dispatch(peer, body)
                if body['action'] == 'deliver':
                    client.deliveries += 1
                    if client.lose:
                        raise OSError('synthetic ACK lost after actual commit')
                return result
        client = Client()
        bridge = ConversationBridge(service, principal, client)
        context = {'conversation_id': 'original-conversation', 'request_id': 'entry-original'}
        self.call('conversation_open', context)
        store.heartbeat('leader', ['leader'], 10)
        task = store.claim('leader')
        self.assertEqual(accepted['task_id'], task['id'])
        store.interaction(task['id'], 'leader', task['epoch'], 'original-question', 'question',
                          {'questions': [{'id': 'which', 'question': 'Which branch?'}]})
        page = self.call('conversation_receive', dict(context, after=0))
        question = next(message for message in page['messages'] if message['kind'] == 'question')
        reply = dict(context, message_id='original-answer', text='same-branch', reply_to=question['message_id'])
        answered = self.call('conversation_reply', reply)
        self.assertEqual('native_answer_recorded', answered['runtime']['state'])
        self.assertEqual({'answers': {'which': {'answers': ['same-branch']}}},
            store.interaction(task['id'], 'leader', task['epoch'], 'original-question')['answer'])
        client.lose = True
        steering = dict(context, message_id='lost-steering', text='continue same original work')
        self.call('conversation_reply', steering, 'lost-reply')
        client.lose = False
        receipt = self.call('conversation_receipt', dict(context, message_id='lost-steering'))
        self.assertEqual('queued_for_native', receipt['runtime']['state'])
        count = client.deliveries
        self.call('conversation_reply', steering, 'lost-reply')
        self.assertEqual(count, client.deliveries)
        self.call('conversation_reply', steering, 'same-message-another-envelope')
        self.assertEqual(count, client.deliveries)
        with self.assertRaises(Conflict):
            ConversationService(directory, OWNER, LocalIngressBackend(ingress, results))
        with self.assertRaises(PermissionError):
            service.request(dict(principal, source='chatgpt-chat'), 'receive', context)
        with self.assertRaises(ValueError):
            self.call('conversation_receive', dict(context, source='chatgpt-chat'))
        original_request = service.request
        def expires_during_receive(principal, action, payload):
            result = original_request(principal, action, payload)
            if action == 'receive':
                self.now = 2100
            return result
        service.request = expires_during_receive
        with self.assertRaises(PermissionError):
            self.call('conversation_receive', context)
