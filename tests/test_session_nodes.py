"""Interactive node/real ledger tests; no provider calls or production state."""
import asyncio
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.conversation import ConversationService
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_backend import LocalIngressBackend, RemoteIngressBackend
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.mcp_http import create_server
from assistant_mesh.mcp_http_auth import OwnerTokenVerifier, validate_policy
from assistant_mesh.session_nodes import LocalSessionNodes, RemoteSessionNodes, TOOLS, arguments, dispatch
from assistant_mesh.store import Conflict, Store
from tests import test_mcp_http as existing_http


def principal(subject='owner-main-chat', scopes=None):
    return {'owner_id': 'owner-fixture', 'source': 'chatgpt-chat', 'subject': subject,
            'scopes': ['tasks:read', 'tasks:submit'] if scopes is None else scopes}


def subjects():
    return {subject: {'capabilities': ['agent', 'web.search'],
                      'visible_nodes': {'authority-fixture': 'AMITA coordinator'}}
            for subject in ('owner-main-chat', 'owner-other-chat')}


class SessionNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-interactive-node.')
        self.now = [10000.0]
        self.store = Store(Path(self.temp.name) / 'original.sqlite', clock=lambda: self.now[0])
        self.ingress = Ingress(self.store, 'authority-fixture', {'owner_id': 'owner-fixture', 'required': ['agent']})
        self.results = IngressResults(self.store, 'authority-fixture', {'owner_id': 'owner-fixture'})
        self.backend = LocalIngressBackend(self.ingress, self.results)
        self.service = LocalSessionNodes(self.backend, subjects())

    def tearDown(self):
        self.temp.cleanup()

    def connect(self, subject='owner-main-chat', **changes):
        value = {'session_id': 'original-chat', 'control_id': 'join-one', 'capabilities': ['agent', 'web.search']}
        value.update(changes)
        return self.service.request(principal(subject), 'connect', value)

    def create(self, request='request-one', subject='owner-main-chat', grant=True, ingress=None):
        receipt = (ingress or self.ingress).submit({'role': 'ingress', 'ingress': principal(subject)},
            {'request_id': request, 'input': 'Owner requested bounded documentation work'})
        if grant:
            self.service.authorize_work(subject, request, 'operation-' + request,
                                        conversation_id='conversation-' + request, authorized=True)
        return receipt['task_id']

    def pull(self, pull_id='pull-one', subject='owner-main-chat'):
        return self.service.request(principal(subject), 'work_pull', {'session_id': 'original-chat', 'pull_id': pull_id})

    def report(self, work, report_id='report-one', status='completed', **changes):
        value = {key: work[key] for key in ('task_id', 'epoch', 'operation_id', 'request_id', 'conversation_id')}
        value.update(session_id='original-chat', report_id=report_id, status=status)
        value.update(changes)
        return self.service.request(principal(), 'work_report', value)

    def row(self, task):
        with self.store.transaction() as db:
            return dict(db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())

    def test_control_join_ack_loss_ttl_and_capability_updates_need_no_business_task(self):
        heartbeat = self.store.heartbeat
        with patch.object(self.store, 'heartbeat', side_effect=RuntimeError('lost control ACK')):
            with self.assertRaises(RuntimeError):
                self.connect()
        first = self.connect()['session']
        self.service.request(principal(), 'node_update', {'session_id': 'original-chat', 'control_id': 'update-two',
            'capabilities': ['agent', 'native.unlisted'], 'agent_state': 'waiting_model', 'model_state': 'unavailable'})
        self.now[0] += 61
        expired = self.service.request(principal(), 'discover', {'session_id': 'original-chat'})
        self.assertEqual('inactive', expired['session']['network']['state'])
        self.assertTrue(expired['session']['agent']['observation_stale'])
        restored = self.connect()['session']
        self.assertEqual(first['node_id'], restored['node_id'])
        self.assertEqual(['agent', 'native.unlisted'], restored['capabilities'])
        self.assertEqual(['agent'], restored['managed_capabilities'])
        self.assertEqual('online', restored['network']['state'])
        self.assertEqual('waiting_model', restored['agent']['state'])
        self.assertFalse(restored['native_tools_restricted'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM interactive_nodes').fetchone()[0])
        self.assertIsNotNone(heartbeat)

    def test_owner_subject_scope_and_closed_arguments_cannot_choose_node_or_authority(self):
        for extra in ({'owner_id': 'other'}, {'node_id': 'authority-fixture'}, {'role': 'operator'}, {'task_id': 'fake'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                arguments('connect', dict(session_id='chat', control_id='join', **extra))
        one = self.connect()['session']
        other = self.connect(subject='owner-other-chat')['session']
        self.assertNotEqual(one['node_id'], other['node_id'])
        for bad in (dict(principal(), owner_id='foreign'), dict(principal(), source='operator'), principal(scopes=['tasks:read'])):
            with self.assertRaises(PermissionError):
                self.service.request(bad, 'work_pull', {'session_id': 'original-chat', 'pull_id': 'original'})
        with self.assertRaises(PermissionError):
            self.service.authorize_work('owner-main-chat', 'absent', 'op', authorized=False)

    def test_real_claim_filters_other_tasks_and_retains_original_business_identities(self):
        self.store.create_task('Unrelated private task', ['agent'], task_id='foreign-task')
        self.create('ungranted', grant=False)
        self.create('foreign-request', subject='owner-other-chat')
        original = self.create()
        self.connect()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda unused: self.pull(), range(2)))
        work = replies[0]['work']
        self.assertEqual(original, work['task_id'])
        self.assertEqual(work, replies[1]['work'])
        self.assertEqual('operation-request-one', work['operation_id'])
        self.assertEqual('conversation-request-one', work['conversation_id'])
        self.assertEqual(1, self.row(original)['attempts'])
        self.assertEqual('pending', self.row('foreign-task')['status'])
        self.assertTrue(work['may_execute'])
        report = self.report(work, checkpoint={'file': 'owner-artifact.md'}, result='bounded work completed')
        self.assertTrue(report['receipt']['accepted'])
        row = self.row(original)
        self.assertEqual('completed', row['status'])
        self.assertEqual('bounded work completed', row['result'])
        self.assertEqual(work['operation_id'], json.loads(row['checkpoint'])['operation_id'])
        self.assertEqual(work['task_id'], self.ingress.status({'role': 'ingress', 'ingress': principal()},
                                                           {'request_id': 'request-one'})['task_id'])
        self.assertFalse(report['execution_verified'])

    def test_atomic_claim_and_report_receipts_rollback_with_the_original_store_write(self):
        task = self.create()
        self.connect()
        with patch.object(self.service, '_save_receipt', side_effect=RuntimeError('receipt persistence failed')):
            with self.assertRaises(RuntimeError):
                self.pull()
        self.assertEqual(('pending', 0), (self.row(task)['status'], self.row(task)['attempts']))
        work = self.pull()['work']
        with patch.object(self.service, '_save_receipt', side_effect=RuntimeError('receipt persistence failed')):
            with self.assertRaises(RuntimeError):
                self.report(work)
        self.assertEqual('running', self.row(task)['status'])
        self.report(work)
        self.assertEqual('completed', self.row(task)['status'])

    def test_explicit_owner_assignment_executes_existing_network_work_without_recreating_it(self):
        task = self.store.create_task('Already accepted portable peer work', ['agent'], task_id='original-network-task',
            context={'request_id': 'original-network-request', 'session_scope': 'peer:original-business'})
        self.service.authorize_work('owner-main-chat', 'original-network-request', 'original-network-operation',
            task_id=task, conversation_id='original-network-conversation', authorized=True)
        self.connect()
        work = self.pull()['work']
        self.assertEqual(task, work['task_id'])
        self.assertEqual('original-network-operation', work['operation_id'])
        self.report(work, result='existing network work completed')
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM ingress_requests').fetchone()[0])

    def test_report_retry_after_expiry_only_reads_receipt_and_changed_original_ids_fail(self):
        self.create()
        self.connect()
        work = self.pull()['work']
        for change in ({'operation_id': 'replacement'}, {'request_id': 'replacement'}, {'conversation_id': 'replacement'}, {'epoch': 999}):
            with self.subTest(change=change), self.assertRaises((PermissionError, Conflict)):
                self.report(work, **change)
        first = self.report(work, result='original result')
        self.now[0] += 100
        second = self.report(work, result='original result')
        self.assertEqual(first['receipt'], second['receipt'])
        self.assertTrue(second['original_receipt'])
        with self.assertRaises(Conflict):
            self.report(work, result='changed result')
        self.assertEqual('original result', self.row(work['task_id'])['result'])
        self.assertFalse(self.pull()['work']['may_execute'])
        with self.assertRaises(Conflict):
            self.pull('new-after-expiry')

    def test_unknown_effect_preserves_operation_and_independent_work_can_continue(self):
        first = self.create()
        self.connect()
        work = self.pull()['work']
        self.report(work, report_id='effect-intent', status='effect_started')
        self.assertFalse(self.pull()['work']['may_execute'])
        with self.assertRaises(Conflict):
            self.report(work, report_id='no-receipt', status='completed')
        self.report(work, report_id='uncertain', status='unknown', checkpoint={'receipt': 'awaiting original provider'})
        self.now[0] += 100
        self.connect()
        other_ingress = Ingress(self.store, 'authority-fixture', {'owner_id': 'owner-fixture',
                                                                'required': ['agent'], 'session_scope': 'independent:owner'})
        second = self.create('independent', ingress=other_ingress)
        next_work = self.pull('next-work')['work']
        self.assertEqual(second, next_work['task_id'])
        self.assertEqual('needs_review', self.row(first)['status'])
        self.assertEqual('operation-request-one', json.loads(self.row(first)['checkpoint'])['operation_id'])
        self.assertEqual(1, self.row(first)['attempts'])

    def test_original_effect_receipt_allows_report_without_attesting_provider_execution(self):
        self.create()
        self.connect()
        work = self.pull()['work']
        self.report(work, report_id='intent', status='effect_started')
        answer = self.report(work, effect_receipt={'operation_id': work['operation_id'], 'reference': 'original-provider-receipt'},
                             result='provider returned the original receipt')
        self.assertFalse(answer['execution_verified'])
        checkpoint = json.loads(self.row(work['task_id'])['checkpoint'])
        self.assertFalse(checkpoint['side_effect_started'])
        self.assertEqual(work['operation_id'], checkpoint['interactive_effect_receipt']['operation_id'])

    def test_expired_lease_renews_original_operation_and_old_epoch_cannot_report(self):
        task = self.create()
        self.connect()
        original = self.pull()['work']
        self.now[0] += self.service.work_lease_seconds + 1
        self.connect()
        renewed = self.pull('after-lease-expiry')['work']
        self.assertEqual((task, original['operation_id']), (renewed['task_id'], renewed['operation_id']))
        self.assertEqual(original['epoch'] + 1, renewed['epoch'])
        self.assertFalse(self.pull()['work']['may_execute'])
        with self.assertRaises(Conflict):
            self.report(original, report_id='stale-original-epoch')
        self.service.request(principal(), 'node_update', {'session_id': 'original-chat',
            'control_id': 'explicit-offline', 'online': False})
        self.assertFalse(self.pull('after-lease-expiry')['work']['may_execute'])
        self.assertEqual('running', self.row(task)['status'])
        self.assertEqual(2, self.row(task)['attempts'])

    def test_deep_thought_outlives_presence_then_reports_and_renews_same_execution_epoch(self):
        task = self.create()
        self.connect()
        original = self.pull()['work']
        self.assertEqual(900, original['lease_remaining_seconds'])
        node = self.row(task)['node']
        with self.store.transaction() as db:
            original_seen = db.execute('SELECT seen FROM nodes WHERE id=?', (node,)).fetchone()['seen']
        self.now[0] += 180
        observed = self.service.request(principal(), 'discover', {'session_id': 'original-chat'})['session']
        self.assertEqual('inactive', observed['network']['state'])
        self.assertEqual('ready', observed['agent']['state'])
        self.assertTrue(observed['agent']['observation_stale'])
        with self.assertRaises(Conflict):
            self.pull('no-new-work-with-stale-presence')
        self.assertTrue(self.pull()['work']['may_execute'])
        self.report(original, report_id='long-reasoning-progress', status='progress', checkpoint={'step': 'reasoning finished'})
        self.assertEqual((original['epoch'], self.now[0] + 900), (self.row(task)['epoch'], self.row(task)['deadline']))
        with self.store.transaction() as db:
            self.assertEqual(original_seen, db.execute('SELECT seen FROM nodes WHERE id=?', (node,)).fetchone()['seen'])
        self.now[0] += 180
        self.report(original, result='deep reasoning completed under original lease')
        self.assertEqual(('completed', 1), (self.row(task)['status'], self.row(task)['attempts']))
        self.assertEqual(original['operation_id'], json.loads(self.row(task)['checkpoint'])['operation_id'])

    def test_inactive_presence_can_report_effect_outcome_but_offline_or_unknown_cannot_start_an_effect(self):
        for invalid in (False, 59, 3601):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                LocalSessionNodes(self.backend, subjects(), work_lease_seconds=invalid)
        self.service = LocalSessionNodes(self.backend, subjects(), work_lease_seconds=240)
        task = self.create()
        self.connect()
        original = self.pull()['work']
        self.now[0] += 120
        self.report(original, report_id='after-thinking-effect-intent', status='effect_started')
        self.assertFalse(self.pull()['work']['may_execute'])
        self.service.request(principal(), 'node_update', {'session_id': 'original-chat',
            'control_id': 'offline-during-existing-work', 'online': False, 'capabilities': ['agent']})
        with self.assertRaises(Conflict):
            self.report(original, report_id='offline-new-effect', status='effect_started')
        self.report(original, report_id='effect-became-unknown', status='unknown')
        self.assertEqual('needs_review', self.row(task)['status'])
        with self.assertRaises(Conflict):
            self.report(original, report_id='no-unknown-lease-revival', status='progress')
        self.assertEqual(original['epoch'], self.row(task)['epoch'])
        ordinary = self.store.create_task('Ordinary worker lease remains ninety seconds', ['agent'],
            task_id='ordinary-lease', context={'session_scope': 'ordinary:independent'})
        self.store.heartbeat('ordinary-worker', ['agent'])
        claimed = self.store.claim('ordinary-worker', task_ids=[ordinary])
        self.assertEqual(self.now[0] + 90, claimed['deadline'])

    def test_discovery_separates_network_agent_model_and_task_states(self):
        self.store.heartbeat('authority-fixture', ['leader', 'agent'])
        self.store.elect()
        task = self.store.create_task('Coordinator waits for a model', ['leader'])
        claimed = self.store.claim('authority-fixture')
        self.store.update_task(task, 'authority-fixture', claimed['epoch'], status='waiting_backend')
        self.connect(agent_state='waiting_model', model_state='unavailable')
        data = self.service.request(principal(), 'discover', {})
        coordinator = next(node for node in data['nodes'] if node.get('display_name') == 'AMITA coordinator')
        self.assertEqual('online', coordinator['network']['state'])
        self.assertIn('coordinator', coordinator['roles'])
        self.assertEqual({'waiting_backend': 1}, coordinator['tasks']['states'])
        self.assertEqual('unknown', coordinator['model']['state'])
        self.assertIsNone(self.pull()['work'])
        self.assertNotIn('authority-fixture', json.dumps(data))

    def test_remote_uses_existing_scoped_ingress_and_lost_claim_ack_recovers_same_operation(self):
        self.create()
        self.connect()
        calls, service, lose = [], self.service, [True]
        class FixedIngressClient:
            def request(self, route, payload):
                calls.append((route, payload))
                response = service.request(principal(), route.rsplit('/', 1)[1], payload)
                if lose[0]:
                    lose[0] = False
                    raise RuntimeError('lost ACK with PRIVATE_SECRET')
                return response
        remote = RemoteSessionNodes(RemoteIngressBackend({'owner-main-chat': FixedIngressClient()}, 'owner-fixture'))
        verifier = OwnerTokenVerifier(existing_http.Delegate(), validate_policy(existing_http.policy()))
        payload = {'session_id': 'original-chat', 'pull_id': 'original-pull'}
        lost = dispatch(remote, verifier, existing_http.token(), 'work_pull', payload)
        self.assertEqual('unknown', lost['outcome'])
        recovered = dispatch(remote, verifier, existing_http.token(), 'work_pull', payload)
        self.assertTrue(recovered['ok'])
        self.assertEqual(1, self.row(recovered['data']['work']['task_id'])['attempts'])
        self.assertEqual(['/v1/ingress/mesh/work_pull'] * 2, [call[0] for call in calls])
        self.assertNotIn('PRIVATE_SECRET', json.dumps(lost))
        self.assertEqual(payload, calls[0][1])


@unittest.skipUnless(existing_http.SDK_AVAILABLE, 'isolated MCP SDK 2.3.0 is required')
class SessionNodeSDKTests(unittest.TestCase):
    http = existing_http.ActualSDKHTTPTests.http
    rpc = staticmethod(existing_http.ActualSDKHTTPTests.rpc)

    def setUp(self):
        import uvicorn
        from mcp.server.auth.provider import AccessToken
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-interactive-sdk.')
        self.store = Store(Path(self.temp.name) / 'original.sqlite')
        self.ingress = Ingress(self.store, 'authority-fixture', {'owner_id': 'owner-fixture', 'required': ['agent']})
        self.results = IngressResults(self.store, 'authority-fixture', {'owner_id': 'owner-fixture'})
        backend = LocalIngressBackend(self.ingress, self.results)
        self.sessions = LocalSessionNodes(backend, subjects())
        self.conversation = ConversationService(self.temp.name, 'owner-fixture', backend)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        self.resource = 'http://127.0.0.1:{}/mcp'.format(sock.getsockname()[1])
        valid = AccessToken(token='SYNTHETIC_VALID', client_id='chatgpt-fixture-client', scopes=['tasks:read', 'tasks:submit'],
            expires_at=int(time.time()) + 600, resource=self.resource, subject='subject-owner', claims={'iss': existing_http.ISSUER})
        tokens = {'SYNTHETIC_VALID': valid, 'SYNTHETIC_READ': valid.model_copy(update={'scopes': ['tasks:read']}),
                  'SYNTHETIC_OTHER': valid.model_copy(update={'subject': 'subject-other'})}
        class Verifier:
            async def verify_token(self, raw):
                return tokens.get(raw)
        _, app = create_server(existing_http.policy(self.resource), token_verifier=Verifier(), backend=backend,
            session_service=self.sessions, conversation_service=self.conversation, allow_loopback_for_tests=True)
        self.http_server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False,
                                                       lifespan='on', timeout_graceful_shutdown=3))
        self.thread = threading.Thread(target=lambda: asyncio.run(self.http_server.serve(sockets=[sock])), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.http_server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.http_server.started)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.http_server.should_exit = True
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.temp.cleanup()

    def tool(self, name, value, bearer='SYNTHETIC_VALID'):
        response = self.http(payload=self.rpc('tools/call', {'name': name, 'arguments': value}), bearer=bearer)
        self.assertEqual(200, response[0])
        return response[2]

    def test_actual_sdk_thirteen_tools_current_oauth_grant_and_original_work_roundtrip(self):
        listed = self.http(payload=self.rpc('tools/list'))[2]['result']['tools']
        self.assertEqual(set(TOOLS) | {'mesh_submit_task', 'mesh_task_status', 'mesh_approved_result',
            'mesh_conversation_open', 'mesh_conversation_receive', 'mesh_conversation_reply',
            'mesh_conversation_receipt', 'mesh_conversation_sync'}, {tool['name'] for tool in listed})
        for descriptor in listed:
            self.assertEqual(descriptor['securitySchemes'], descriptor['_meta']['securitySchemes'])
        self.assertFalse(self.tool('mesh_discover', {})['result']['isError'])
        connect = {'session_id': 'sdk-chat', 'control_id': 'sdk-join', 'capabilities': ['agent']}
        denied = self.tool('mesh_connect', connect, 'SYNTHETIC_READ')['result']
        self.assertEqual('insufficient_scope', denied['structuredContent']['error'])
        self.assertIn('mcp/www_authenticate', denied['_meta'])
        self.assertFalse(self.tool('mesh_connect', connect)['result']['isError'])
        injected = self.tool('mesh_connect', dict(connect, node_id='authority-fixture'))
        self.assertEqual(-32602, injected['error']['code'])
        submitted = self.tool('mesh_submit_task', {'request_id': 'sdk-original', 'input': 'Bounded actual SDK fixture work'})
        task_id = submitted['result']['structuredContent']['data']['task_id']
        self.tool('mesh_conversation_open', {'conversation_id': 'sdk-conversation', 'request_id': 'sdk-original'})
        self.sessions.authorize_work('owner-main-chat', 'sdk-original', 'sdk-operation',
                                     conversation_id='sdk-conversation', authorized=True)
        work = self.tool('mesh_work_pull', {'session_id': 'sdk-chat', 'pull_id': 'sdk-pull'})['result']['structuredContent']['data']['work']
        self.assertEqual(task_id, work['task_id'])
        report = {key: work[key] for key in ('task_id', 'epoch', 'operation_id', 'request_id', 'conversation_id')}
        report.update(session_id='sdk-chat', report_id='sdk-report', status='completed', result='SDK fixture completed')
        accepted = self.tool('mesh_work_report', report)['result']['structuredContent']
        self.assertTrue(accepted['ok'])
        self.assertFalse(accepted['data']['execution_verified'])
        self.assertFalse(self.tool('mesh_work_report', report)['result']['isError'])
        status = self.tool('mesh_task_status', {'request_id': 'sdk-original'})['result']['structuredContent']['data']
        self.assertEqual(('completed', task_id), (status['status'], status['task_id']))
        foreign = self.tool('mesh_task_status', {'request_id': 'sdk-original'}, 'SYNTHETIC_OTHER')['result']
        self.assertTrue(foreign['isError'])
