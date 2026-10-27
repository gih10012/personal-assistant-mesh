"""Synthetic identities against the real Store/Registry/Allocation and HTTP.

No phone, production registration, paid model or device execution is claimed.
"""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from assistant_mesh.enrollment import result_envelope
from assistant_mesh.enrollment_relay import EnrollmentRelay, REQUEST_FORMAT
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_backend import LocalIngressBackend
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.phone_authority import PhoneAuthority, LiveTaskGate, RegistryEnrollment, issue_node
from assistant_mesh.phone_http import handler
from assistant_mesh.resources import Registry
from assistant_mesh.server import ThreadingHTTPServer
from assistant_mesh.session_nodes import LocalSessionNodes
from assistant_mesh.store import Store, Conflict
from tests import test_allocations as allocation_fixtures
from tests.test_enrollment_relay import fixture, owned, TOKEN


NODE_TOKEN = 'SYNTHETIC_PHONE_NARROW_' + 'b' * 48


class PhoneAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.original = allocation_fixtures.AllocationTests()
        self.original.setUp()
        f = self.original
        ingress = Ingress(f.store, 'authority-fixture', {'owner_id': 'owner-fixture'})
        results = IngressResults(f.store, 'authority-fixture', {'owner_id': 'owner-fixture'})
        sessions = LocalSessionNodes(LocalIngressBackend(ingress, results), {
            'existing-chat': {'capabilities': ['agent'], 'visible_nodes': {}}})
        self.config_path = Path(f.temp.name) / 'phone.json'
        self.config = {'format': 'mesh-phone-authority/1', 'enabled': True, 'nodes': [{
            'node_id': 'cloud', 'subject': 'synthetic-phone', 'session_id': 'phone-session',
            'token_sha256': hashlib.sha256(NODE_TOKEN.encode()).hexdigest(), 'enabled': True,
            'capabilities': ['target'], 'scope': ['phone:node', 'phone:updates'],
            'enrollment': {'synthetic_test_identity': True}}]}
        owned(self.config_path, self.config)
        self.api = SimpleNamespace(store=f.store, resources=f.registry, allocations=f.bridge,
                                   session_nodes=sessions)
        self.service = PhoneAuthority(self.api, self.config_path)
        self.connect()

    def tearDown(self):
        self.original.tearDown()

    def call(self, action, **arguments):
        arguments['session_id'] = 'phone-session'
        return self.service.node(NODE_TOKEN, {'action': action, 'arguments': arguments})

    def connect(self):
        return self.call('mesh_connect', control_id='connect', capabilities=['target'])

    def report(self, action, report_id=None, **arguments):
        arguments.setdefault('operation_id', 'op')
        arguments.setdefault('capability_id', 'target')
        return self.call('mesh_work_report', report_id=report_id or action,
                         provider={'action': action, 'arguments': arguments})['provider']

    def start(self):
        self.original.reserve()
        dispatch = self.call('mesh_work_pull', pull_id='pull')['dispatch'][0]
        self.assertEqual(self.original.task['id'], dispatch['task_id'])
        accepted = self.report('accept', epoch=dispatch['epoch'], receipt_id='receipt')
        return self.report('start', epoch=accepted['epoch'], receipt_id='receipt')

    def test_original_allocation_full_bytes_then_settlement_and_caller_read(self):
        self.assertEqual('cloud', self.connect()['session']['node_id'])
        started = self.start()
        self.assertTrue(started['execute_once'])
        retried = self.report('start', epoch=2, receipt_id='receipt')
        self.assertFalse(retried['execute_once'])
        raw = bytes(range(256)) * 200
        digest = hashlib.sha256(raw).hexdigest()
        evidence = {'resource_quiescent': True, 'result_reference': 'phone-result:' + digest,
                    'sha256': digest, 'bytes': len(raw)}
        with self.assertRaisesRegex(Conflict, 'full_result_required'):
            self.report('settle', epoch=started['epoch'], receipt_id='receipt', outcome='completed', evidence=evidence)
        stored = self.report('result', receipt_id='receipt', result={
            'bytes': len(raw), 'sha256': digest, 'base64': base64.b64encode(raw).decode()})
        self.assertEqual('phone-result:' + digest, stored['result_reference'])
        settled = self.report('settle', epoch=started['epoch'], receipt_id='receipt', outcome='completed', evidence=evidence)
        self.assertEqual('completed', settled['state'])
        result = self.service.read_result(self.original.actor, 'op', 'target')
        self.assertEqual(raw, base64.b64decode(result['base64']))
        self.assertEqual(1, self.original.row_count('tasks'))
        with self.assertRaises(PermissionError):
            self.service.read_result('node:cloud', 'op', 'target')

    def test_withdrawal_rejects_start_and_result_remains_unknown_without_new_operation(self):
        self.original.reserve()
        self.report('accept', epoch=1, receipt_id='receipt')
        self.call('mesh_node_update', control_id='foreground-ended', capabilities=[], online=False)
        with self.assertRaises(Conflict):
            self.report('start', epoch=2, receipt_id='receipt')
        unknown = self.report('unknown', epoch=2)
        self.assertEqual('unknown', unknown['state'])
        self.assertEqual('op', unknown['operation_id'])

    def test_node_scope_disable_and_original_control_identity(self):
        with self.assertRaises(PermissionError):
            self.service.node(TOKEN, {'action': 'mesh_discover', 'arguments': {'session_id': 'phone-session'}})
        with self.assertRaises(PermissionError):
            self.service.node(NODE_TOKEN, {'action': 'mesh_connect', 'arguments': {'session_id': 'another'}})
        self.config['nodes'][0]['enabled'] = False
        owned(self.config_path, self.config)
        with self.assertRaises(PermissionError):
            self.connect()

    def test_exact_loopback_http_no_management_fallthrough(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.service, 'https://mesh.example.test',
                                                            trusted_proxy_peers=('127.0.0.1',)))
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            for path, expected in (('/phone/node', 200), ('/v1/status', 404), ('/phone/node?operator=1', 404)):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
                raw = json.dumps({'action': 'mesh_connect', 'arguments': {
                    'session_id': 'phone-session', 'control_id': 'connect', 'capabilities': ['target']}})
                conn.request('POST', path, raw, headers={'Host': 'mesh.example.test',
                    'X-Forwarded-Proto': 'https', 'Authorization': 'Bearer ' + NODE_TOKEN,
                    'Content-Type': 'application/json'})
                response = conn.getresponse()
                self.assertEqual(expected, response.status); response.read(); conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


class EnrollmentAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='phone-authority-enrollment.')
        self.root = Path(self.temp.name); self.root.chmod(0o700)
        (self.bootstrap, unused, unused2, self.challenge,
         self.relay_config, self.relay_path, self.raw) = fixture(self.root)
        self.store = Store(self.root / 'original.db', recover_inflight=False)
        # Original task/request, never created by the phone enrollment handler.
        Ingress(self.store, 'synthetic-authority', {'owner_id': 'synthetic-owner'})
        self.store.heartbeat('actual-worker', ['leader', 'agent'])
        self.store.create_task('synthetic original owner request', task_id='original-mesh-task',
                               context={'request_id': 'original-owner-message'})
        self.task = self.store.claim('actual-worker')
        self.gate = LiveTaskGate(self.store)
        self.relay = EnrollmentRelay(self.bootstrap, self.relay_path, live_task_validator=self.gate)

    def tearDown(self):
        self.temp.cleanup()

    def envelope(self, action='challenge', arguments=None):
        return {'format': REQUEST_FORMAT, 'binding': self.relay_config['binding'],
                'action': action, 'arguments': {} if arguments is None else arguments}

    def test_admission_lock_blocks_cancel_until_original_journal_commit(self):
        entered, cancelled = threading.Event(), threading.Event()
        def cancel():
            entered.set()
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET status='paused' WHERE id=?", (self.task['id'],))
            cancelled.set()
        with self.gate.admission():
            self.assertTrue(self.gate(self.relay_config['binding']))
            worker = threading.Thread(target=cancel); worker.start(); entered.wait(1)
            self.assertFalse(cancelled.wait(.05))
            response = self.relay.handle(TOKEN, self.envelope())
            self.assertTrue(response['may_execute_original_challenge'])
        worker.join(2)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.gate(self.relay_config['binding']))
        with self.gate.admission():
            with self.assertRaises(PermissionError):
                self.relay.handle(TOKEN, self.envelope('result', result_envelope(self.challenge, self.raw * 3)))
        self.assertEqual('attempted', self.bootstrap.status(self.challenge['operation_id'])['state'])

    def test_independent_oracle_real_registry_query_then_narrow_credential(self):
        registry = Registry(self.store)
        hooks = RegistryEnrollment(registry, self.bootstrap)
        self.bootstrap.registrar, self.bootstrap.registration_observer = hooks.register, hooks.observe
        with self.gate.admission():
            self.relay.handle(TOKEN, self.envelope())
            self.relay.handle(TOKEN, self.envelope('result', result_envelope(self.challenge, self.raw * 3)))
        with self.assertRaises(PermissionError):
            issue_node(self.bootstrap, registry, subject='phone', session_id='original-phone',
                       capabilities=['synthetic-phone:bytes-v1'], resource_url='https://mesh.example.test/phone/node')
        self.bootstrap.verify(self.challenge['operation_id'])
        registered = self.bootstrap.register(self.challenge['operation_id'])
        self.assertEqual('registered', registered['registration_state'])
        self.bootstrap.reconcile(self.challenge['operation_id'])
        node, exported = issue_node(self.bootstrap, registry, subject='phone', session_id='original-phone',
            capabilities=['synthetic-phone:bytes-v1'], resource_url='https://mesh.example.test/phone/node')
        self.assertEqual('synthetic-phone', node['node_id'])
        self.assertEqual(hashlib.sha256(exported['credential']['token'].encode()).hexdigest(), node['token_sha256'])
        self.assertNotIn('token', exported['config'])
        self.assertEqual('mesh-enrollment-registration/1', exported['enrollment']['registration_receipt']['format'])


if __name__ == '__main__':
    unittest.main()
