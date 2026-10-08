"""Real loopback RPC tests; no external hosts, credentials, models, or spend."""
import hashlib
import contextlib
import io
import json
import socket
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.codex import CodexError
from assistant_mesh.networking import PROTOCOL, native_scope
from assistant_mesh.node import LOCAL_RUNTIME, Node, run
from assistant_mesh.remote import Remote
from assistant_mesh.server import serve
from assistant_mesh.store import Store
from assistant_mesh.worker import Client, Worker


class UnavailableBackend:
    """Deliberately unavailable, not a fake model/inference success."""
    def __init__(self, *args, **kwargs):
        raise CodexError('no_local_backend_available')


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.now = 1000
        self.nodes = []
        self.external_servers = []

    def tearDown(self):
        for node in reversed(self.nodes):
            node.stop()
        for server, thread in self.external_servers:
            if thread.is_alive():
                server.shutdown()
            thread.join(timeout=3)
            server.server_close()
        self.tmp.cleanup()

    def private(self, name, value):
        path = self.directory / name
        path.write_text(json.dumps(value), encoding='utf8')
        path.chmod(0o600)
        return str(path)

    def secret(self, name):
        path = self.directory / (name + '.token')
        if not path.exists():
            path.write_text(hashlib.sha256(('test-only:' + name).encode()).hexdigest())
            path.chmod(0o600)
        return str(path)

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def config(self, name, port=None, central=False, start_worker=False, other=None):
        port = self.free_port() if port is None else port
        capabilities = ['agent', 'mesh.node:' + name] + (['leader'] if central else [])
        server = {'node_id': name, 'database': str(self.directory / (name + '.db')), 'port': port,
                  'peers': [
                      {'role': 'operator', 'token_file': self.secret(name + '-operator')},
                      {'role': 'worker', 'node': name, 'capabilities': capabilities,
                       'token_file': self.secret(name + '-worker')}],
                  'network_peers': {other: {'send_allowed': True}} if other else {}}
        if other:
            server['peers'].append({'role': 'agent_peer', 'node': other,
                                   'capabilities': ['a2a.delegate', 'a2a.report'],
                                   'token_file': self.secret(name + '-from-' + other)})
        worker = {'node_id': name, 'control_url': 'http://127.0.0.1:' + str(port),
                  'token_file': self.secret(name + '-worker'), 'capabilities': capabilities,
                  'codex': {'workspace': str(self.directory), 'sandbox': 'read-only'}}
        return {'node_id': name, 'local_server_config': self.private(name + '-server.json', server),
                'local_worker_config': self.private(name + '-worker.json', worker),
                'start_server': not central, 'start_worker': start_worker,
                'auto_maintenance': True, 'peers': []}

    def node(self, config, **kwargs):
        path = self.private(config['node_id'] + '-node.json', config)
        node = Node(config, config_path=path, clock=lambda: self.now, **kwargs)
        self.nodes.append(node)
        return node

    def rewrite(self, path, **changes):
        value = json.loads(Path(path).read_text())
        value.update(changes)
        Path(path).write_text(json.dumps(value))

    def start_external(self, config):
        value = json.loads(Path(config['local_server_config']).read_text())
        ready, current = threading.Event(), []
        def installed(server, api, channel):
            api.store.clock = lambda: self.now
            current.append(server)
            ready.set()
        thread = threading.Thread(target=serve, args=(value, installed), daemon=True)
        thread.start()
        self.assertTrue(ready.wait(3))
        self.external_servers.append((current[0], thread))
        return current[0], thread

    def pair(self, client_factory=Client, maintenance=True, alice_grants=None, bob_grants=None):
        alice_config = self.config('alice', other='bob')
        bob_config = self.config('bob', central=True, other='alice')
        alice_worker = json.loads(Path(alice_config['local_worker_config']).read_text())
        bob_worker = json.loads(Path(bob_config['local_worker_config']).read_text())
        for config, other, worker in ((alice_config, 'bob', bob_worker), (bob_config, 'alice', alice_worker)):
            grants = alice_grants if config['node_id'] == 'alice' else bob_grants
            if grants is not None:
                value = json.loads(Path(config['local_server_config']).read_text())
                value['peers'][-1]['capabilities'] = grants
                Path(config['local_server_config']).write_text(json.dumps(value))
            config['auto_maintenance'] = maintenance
            client = {'control_url': worker['control_url'],
                      'token_file': self.secret(other + '-from-' + config['node_id'])}
            config['peers'] = [{'node': other, 'authority': other,
                                'client_config': self.private(config['node_id'] + '-to-' + other + '.json', client),
                                'report_results': True}]
        alice, bob = self.node(alice_config, client_factory=client_factory), self.node(bob_config)
        self.start_external(bob_config)
        alice.start()
        bob.start()
        # Actual credential-bound central-worker heartbeat/claim elects its
        # preconfigured central Leader, not the isolated node supervisor.
        central = Client(bob_worker)
        central.request('/v1/heartbeat', {'capabilities': bob_worker['capabilities']})
        central.request('/v1/claim', {})
        return alice, bob

    def message(self, to='bob', identity='m1', text='test task'):
        return {'protocol': PROTOCOL, 'id': identity, 'to': to, 'input': text,
                'project': 'test-project', 'agent': 'persistent-specialist'}

    def incidents(self, node, peer):
        with node.store.transaction() as db:
            row = db.execute('SELECT * FROM node_incidents WHERE peer=?', (peer,)).fetchone()
            return dict(row) if row is not None else None

    def checkpoint(self, node, task_id):
        with node.store.transaction() as db:
            return json.loads(db.execute('SELECT checkpoint FROM tasks WHERE id=?', (task_id,)).fetchone()[0])

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(.01)
        self.fail('condition did not become true')

    def test_private_config_and_token_required(self):
        config = self.config('alice')
        Path(config['local_worker_config']).chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private_config_requires'):
            self.node(config)
        Path(config['local_worker_config']).chmod(0o600)
        worker = json.loads(Path(config['local_worker_config']).read_text())
        Path(worker['token_file']).chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'secret_requires'):
            self.node(config)

    def test_offline_maintenance_keeps_native_paths_available(self):
        node = self.node(self.config('alice'))
        node._incident('bob', 'transport_unavailable')
        with node.store.transaction() as db:
            text = db.execute('SELECT input FROM tasks').fetchone()[0]
        self.assertIn('不拦截任何原生 Shell、网络或其他功能', text)
        self.assertIn('不同联网路径重新连接已授权 peer', text)
        self.assertIn('不要求原生工具或新路线先注册', text)
        self.assertIn('擅自信任新 peer', text)

    def test_node_worker_routing_and_identity_are_config_bound(self):
        for mutation in ('worker-route', 'server-route', 'server-node', 'worker-token', 'duplicate-role-token'):
            config = self.config('alice-' + mutation)
            worker = json.loads(Path(config['local_worker_config']).read_text())
            server = json.loads(Path(config['local_server_config']).read_text())
            if mutation == 'worker-route':
                worker['capabilities'] = ['agent']
            elif mutation == 'server-route':
                server['peers'][1]['capabilities'] = ['agent']
            elif mutation == 'server-node':
                server['peers'][1]['node'] = 'impostor'
            elif mutation == 'worker-token':
                worker['token_file'] = self.secret('wrong-worker')
            else:
                server['peers'][0]['token_file'] = worker['token_file']
            Path(config['local_worker_config']).write_text(json.dumps(worker))
            Path(config['local_server_config']).write_text(json.dumps(server))
            with self.assertRaises(ValueError):
                self.node(config)

    def test_embedded_authority_does_not_own_global_leader_or_channel(self):
        config = self.config('alice')
        worker = json.loads(Path(config['local_worker_config']).read_text())
        self.rewrite(config['local_worker_config'], capabilities=worker['capabilities'] + ['leader'])
        with self.assertRaisesRegex(ValueError, 'must_not_be_global_leader'):
            self.node(config)
        self.rewrite(config['local_worker_config'], capabilities=worker['capabilities'])
        self.rewrite(config['local_server_config'], ilink_account='/private/channel.json')
        with self.assertRaisesRegex(ValueError, 'second_channel_receiver'):
            self.node(config)

    def test_companion_reuses_existing_central_roles_without_launching_them(self):
        node = self.node(self.config('bob', central=True))
        node.start()
        status = node.step()
        self.assertFalse(status['local_server_owned'])
        self.assertFalse(status['local_worker_alive'])
        self.assertFalse(status['global_takeover_allowed'])

    def test_companion_open_does_not_recover_other_services_inflight_submissions(self):
        for name, central in (('bob', True), ('alice', False)):
            config = self.config(name, central=central)
            server = json.loads(Path(config['local_server_config']).read_text())
            store = Store(server['database'], clock=lambda: self.now)
            store.enqueue('test-send', 'private fixture')
            with store.transaction() as db:
                db.execute("UPDATE outbox SET status='submitting' WHERE id='test-send'")
                db.execute("INSERT INTO steering VALUES('test-steer','test-task',1,'fixture','submitting')")
            node = self.node(config)
            with node.store.transaction() as db:
                expected = 'submitting' if central else 'unknown'
                self.assertEqual(expected, db.execute('SELECT status FROM outbox').fetchone()[0])
                self.assertEqual(expected, db.execute('SELECT state FROM steering').fetchone()[0])

    def test_remote_plaintext_and_identity_mismatch_local_config_rejected(self):
        config = self.config('alice')
        config['peers'] = [{'node': 'bob', 'control_url': 'http://192.0.2.1',
                            'token_file': self.secret('remote')}]
        with self.assertRaisesRegex(ValueError, 'control_requires_tls'):
            self.node(config)
        config['peers'] = []
        self.rewrite(config['local_worker_config'], node_id='wrong')
        with self.assertRaisesRegex(ValueError, 'local_node_identity_mismatch'):
            self.node(config)

    def test_real_ephemeral_loopback_server_and_private_worker_config(self):
        node = self.node(self.config('alice', port=0))
        node.start()
        self.assertGreater(node.server.server_address[1], 0)
        response = Client(node.worker_config).request('/v1/mesh/hello')
        self.assertEqual('alice', response['node'])
        self.assertTrue(response['a2a_idempotent_receive'])
        path = Path(node.runtime_config_path)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(node.worker_config, json.loads(path.read_text()))
        node.stop()
        self.assertFalse(path.exists())

    def test_single_owned_supervisor_lock_releases_after_stop(self):
        config = self.config('bob', central=True)
        one, two = self.node(config), self.node(config)
        one.start()
        with self.assertRaisesRegex(ValueError, 'already_running'):
            two.start()
        one.stop()
        two.start()
        self.assertIsNotNone(two.lock_file)

    def test_real_a2a_disconnect_local_work_and_reconnect_same_scope(self):
        alice, bob = self.pair()
        self.assertEqual('connected', alice.step()['mode'])
        central_server, central_thread = self.external_servers[-1]
        central_server.shutdown()
        central_thread.join(timeout=3)
        central_server.server_close()
        self.now += 20
        disconnected = alice.step()
        self.assertEqual('autonomous', disconnected['mode'])
        self.assertTrue(disconnected['local_work_allowed'])
        self.assertFalse(disconnected['global_takeover_allowed'])
        first_incident = self.incidents(alice, 'bob')
        self.assertEqual(1, first_incident['episode'])
        alice.step()  # no probe/maintenance retry storm inside backoff
        self.assertEqual(first_incident, self.incidents(alice, 'bob'))
        operator_config = dict(alice.worker_config, token_file=self.secret('alice-operator'))
        local = Client(operator_config).request('/v1/mesh/local', {'message': self.message('alice', 'offline-local')})
        alice.store.heartbeat('other-worker', ['agent'])
        self.assertIsNone(alice.store.claim('other-worker'))
        alice.store.heartbeat('alice', ['agent', 'mesh.node:alice'])
        task = alice.store.claim('alice')  # first, node maintenance task
        self.assertEqual(first_incident['task_id'], task['id'])
        self.assertEqual('local:alice', task['context']['authority'])
        alice.store.update_task(task['id'], 'alice', task['epoch'], status='completed', result='checked locally')
        offline_task = alice.store.claim('alice')
        self.assertEqual(local['task_id'], offline_task['id'])
        self.assertEqual(native_scope('alice', 'test-project', 'persistent-specialist', 'operator'), offline_task['scope'])
        self.assertIsNone(alice.store.status()['leader']['node'])
        # A different project can run without sharing the global Leader thread;
        # finishing the local task is a ledger fixture, NOT an inference claim.
        alice.store.update_task(offline_task['id'], 'alice', offline_task['epoch'], status='completed', result='local fixture')
        self.start_external(bob.config)
        self.now += 10
        self.assertEqual('connected', alice.step()['mode'])
        self.assertEqual(0, self.incidents(alice, 'bob')['active'])
        again = Client(operator_config).request('/v1/mesh/local', {'message': self.message('alice', 'offline-local-next')})
        self.assertEqual(offline_task['scope'], alice.store.task_status(again['task_id'])['scope'])
        self.assertNotEqual('leader:owner', offline_task['scope'])

    def test_real_uncertain_a2a_receipt_retries_same_message_once_receiver_proven(self):
        sends, lost = [], [False]
        class LossAfterAcceptance(Client):
            def request(client, path, body=None):
                response = super(LossAfterAcceptance, client).request(path, body)
                if path == '/v1/mesh/send':
                    sends.append(body['message'])
                    if not lost[0]:
                        lost[0] = True
                        raise urllib.error.URLError('test receipt loss after real server acceptance')
                return response
        alice, bob = self.pair(client_factory=LossAfterAcceptance, maintenance=False)
        alice.network.enqueue('bob', self.message())
        alice.step()
        self.assertEqual(1, len(sends))
        self.assertEqual(1, bob.store.status()['tasks']['pending'])
        self.assertFalse(alice.deliver('bob'))  # receiver contract must be refreshed
        self.now += 10
        alice.step()
        self.assertEqual([self.message(), self.message()], sends)
        self.assertEqual(1, bob.store.status()['tasks']['pending'])
        with alice.store.transaction() as db:
            delivery = db.execute('SELECT * FROM mesh_deliveries').fetchone()
            self.assertEqual('accepted', delivery['state'])
            self.assertEqual(2, delivery['attempt'])
        # Destination ownership cannot be reassigned by a more capable worker.
        bob.store.heartbeat('elsewhere', ['agent', 'leader', 'gpu'])
        self.assertIsNone(bob.store.claim('elsewhere'))
        self.assertIsNotNone(bob.store.claim('bob'))

    def test_real_auth_rejection_rotated_credentials_recover_deduplicated_episode(self):
        alice, bob = self.pair()
        client_path = alice.config['peers'][0]['client_config']
        client = json.loads(Path(client_path).read_text())
        # Node refreshes the referenced token file, not mutable client JSON.
        token_path = Path(client['token_file'])
        correct = token_path.read_text()
        token_path.write_text(hashlib.sha256(b'wrong-test-only').hexdigest())
        self.assertFalse(alice.probe('bob'))
        self.assertEqual('authentication_rejected', self.incidents(alice, 'bob')['code'])
        self.assertFalse(alice.network.probe_due('bob'))
        before = self.incidents(alice, 'bob')
        alice.step()
        self.assertEqual(before, self.incidents(alice, 'bob'))
        token_path.write_text(correct)
        self.now += 10
        self.assertTrue(alice.probe('bob'))
        alice.step()  # a complete healthy round, not hello alone, resolves it
        self.assertEqual(0, self.incidents(alice, 'bob')['active'])
        token_path.write_text(hashlib.sha256(b'wrong-again-test-only').hexdigest())
        self.assertFalse(alice.probe('bob'))
        self.assertEqual(2, self.incidents(alice, 'bob')['episode'])

    def test_peer_identity_authority_protocol_and_idempotence_all_required(self):
        for change, code in (({'node': 'impostor'}, 'peer_identity_mismatch'),
                             ({'authority': 'impostor'}, 'peer_authority_mismatch'),
                             ({'protocol': 'unknown/1'}, 'peer_protocol_mismatch'),
                             ({'a2a_idempotent_receive': False}, 'receiver_idempotence_not_confirmed')):
            alice, bob = self.pair(maintenance=False)
            old = alice.client_factory
            class MutatedHello(Client):
                def request(client, path, body=None):
                    response = super(MutatedHello, client).request(path, body)
                    return dict(response, **change) if path == '/v1/mesh/hello' else response
            alice.client_factory = MutatedHello
            alice.network.enqueue('bob', self.message())
            alice.step()
            self.assertEqual(code, self.incidents(alice, 'bob')['code'])
            self.assertEqual({}, bob.store.status()['tasks'])
            alice.client_factory = old
            alice.stop()
            # Stop this pair's external server before the next private pair.
            server, thread = self.external_servers[-1]
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            # Each scenario needs a new private ledger, not old tasks/leases.
            self.nodes.remove(alice)
            self.nodes.remove(bob)
            bob.stop()
            self.external_servers.pop()
            for path in self.directory.glob('*.db*'):
                path.unlink()

    def test_revoked_send_grant_blocks_previously_queued_delivery(self):
        alice, bob = self.pair(maintenance=False)
        alice.network.enqueue('bob', self.message())
        alice.server_config['network_peers']['bob']['send_allowed'] = False
        alice.step()
        self.assertEqual({}, bob.store.status()['tasks'])
        with alice.store.transaction() as db:
            self.assertEqual('denied', db.execute('SELECT state FROM mesh_deliveries').fetchone()[0])

    def test_definite_403_is_denied_without_retry_or_maintenance_storm(self):
        alice, bob = self.pair(bob_grants=['a2a.report'])
        alice.network.enqueue('bob', self.message())
        alice.step()
        with alice.store.transaction() as db:
            delivery = dict(db.execute('SELECT * FROM mesh_deliveries').fetchone())
        self.assertEqual('denied', delivery['state'])
        self.assertTrue(json.loads(delivery['response'])['delivery_not_accepted'])
        self.assertEqual({}, bob.store.status()['tasks'])
        self.assertEqual(1, self.incidents(alice, 'bob')['episode'])
        self.now += 10
        alice.step()
        self.assertEqual(1, self.incidents(alice, 'bob')['episode'])
        self.assertEqual(0, self.incidents(alice, 'bob')['active'])
        self.assertIsNone(alice.network.claim_delivery('bob'))
        self.assertEqual(1, alice.store.status()['tasks']['pending'])

    def test_hello_success_does_not_reset_operation_backoff_or_failure_episode(self):
        sends = []
        class BadReceipt(Client):
            def request(client, path, body=None):
                response = super(BadReceipt, client).request(path, body)
                if path == '/v1/mesh/send':
                    sends.append(body['message']['id'])
                    return dict(response, fingerprint='test-invalid-receipt')
                return response
        alice, bob = self.pair(client_factory=BadReceipt)
        alice.network.enqueue('bob', self.message())
        alice.step()
        self.now += 3
        alice.step()
        self.assertEqual(['m1', 'm1'], sends)
        with alice.store.transaction() as db:
            row = dict(db.execute('SELECT * FROM node_peer_backoff WHERE peer=?', ('bob',)).fetchone())
        self.assertEqual(2, row['failures'])
        self.assertGreater(row['retry_at'], self.now + 4)
        self.assertEqual(1, self.incidents(alice, 'bob')['episode'])
        self.assertEqual(1, alice.store.status()['tasks']['pending'])
        self.now += 3
        alice.step()
        self.assertEqual(['m1', 'm1'], sends)
        self.assertEqual(1, bob.store.status()['tasks']['pending'])

    def test_refusal_after_lost_receipt_keeps_unknown_and_never_claims_non_delivery(self):
        lost = [False]
        class LossThenRefusal(Client):
            def request(client, path, body=None):
                if path == '/v1/mesh/send' and lost[0]:
                    error = urllib.error.HTTPError('http://127.0.0.1', 403, 'test refusal', {}, io.BytesIO())
                    error.close()
                    raise error
                response = super(LossThenRefusal, client).request(path, body)
                if path == '/v1/mesh/send':
                    lost[0] = True
                    raise urllib.error.URLError('test accepted receipt lost')
                return response
        alice, bob = self.pair(client_factory=LossThenRefusal)
        alice.network.enqueue('bob', self.message())
        alice.step()
        self.now += 10
        alice.step()
        with alice.store.transaction() as db:
            row = dict(db.execute('SELECT * FROM mesh_deliveries').fetchone())
        self.assertEqual('unknown', row['state'])
        self.assertFalse(json.loads(row['response'])['delivery_not_accepted'])
        self.assertEqual(1, bob.store.status()['tasks']['pending'])
        self.assertEqual(1, self.incidents(alice, 'bob')['episode'])
        alice.server_config['network_peers']['bob']['send_allowed'] = False
        alice.step()
        with alice.store.transaction() as db:
            blocked_attempt = db.execute('SELECT attempt FROM mesh_deliveries').fetchone()[0]
        alice.step()
        with alice.store.transaction() as db:
            self.assertEqual(blocked_attempt, db.execute('SELECT attempt FROM mesh_deliveries').fetchone()[0])

    def delegated_parent(self, alice):
        receipt = alice.network.receive('operator', self.message('alice', 'parent-fixture'))
        alice.store.heartbeat('alice', ['agent', 'mesh.node:alice'])
        parent = alice.store.claim('alice')
        self.assertEqual(receipt['task_id'], parent['id'])
        remote = Remote(alice.store, alice.network)
        child = remote.delegate(parent['id'], 'alice', parent['epoch'], 'child-call', 'bob',
                                {'input': 'remote fixture task', 'project': 'test-project', 'agent': 'specialist'})
        alice.store.update_task(parent['id'], 'alice', parent['epoch'], status='waiting_children')
        alice.config['reconcile_remote_children'] = True
        return parent, child

    def test_real_authenticated_remote_completion_settles_child_and_wakes_parent(self):
        alice, bob = self.pair(maintenance=False)
        parent, child = self.delegated_parent(alice)
        alice.step()
        self.assertEqual('waiting_remote', alice.store.task_status(child['id'])['status'])
        destination = bob.store.claim('bob')
        bob.store.update_task(destination['id'], 'bob', destination['epoch'], status='completed', result='remote fixture evidence')
        alice.step()
        completed = alice.store.task_status(child['id'])
        self.assertEqual('completed', completed['status'])
        self.assertEqual('remote fixture evidence', completed['result'])
        self.assertEqual('pending', alice.store.task_status(parent['id'])['status'])
        checkpoint = self.checkpoint(alice, child['id'])
        self.assertTrue(checkpoint['remote_result_verified'])
        self.assertEqual(destination['id'], checkpoint['remote_task_id'])

    def test_definite_refusal_and_restart_gap_recover_child_needs_review_not_fake_completion(self):
        alice, bob = self.pair(maintenance=False, bob_grants=['a2a.report'])
        parent, child = self.delegated_parent(alice)
        # Simulate death after durable refusal but before callback settlement.
        alice.config['reconcile_remote_children'] = False
        alice.step()
        self.assertEqual('waiting_remote', alice.store.task_status(child['id'])['status'])
        alice.config['reconcile_remote_children'] = True
        alice.step()
        reviewed = alice.store.task_status(child['id'])
        self.assertEqual('needs_review', reviewed['status'])
        self.assertEqual('pending', alice.store.task_status(parent['id'])['status'])
        self.assertFalse(self.checkpoint(alice, child['id']).get('remote_result_verified', False))
        self.assertEqual({}, bob.store.status()['tasks'])

    def test_unknown_refusal_does_not_settle_remote_child(self):
        alice, bob = self.pair(maintenance=False)
        parent, child = self.delegated_parent(alice)
        delivery = alice.network.claim_delivery('bob')
        bob.network.receive('alice', delivery['message'])
        alice.network.finish_delivery(delivery)  # acceptance receipt lost
        retry = alice.network.claim_delivery('bob')
        self.assertEqual('unknown', alice.network.reject_delivery(retry)['state'])
        alice._reconcile_denials('bob')
        self.assertEqual('waiting_remote', alice.store.task_status(child['id'])['status'])
        self.assertEqual('waiting_children', alice.store.task_status(parent['id'])['status'])

    def test_mismatched_authenticated_read_cannot_settle_remote_child(self):
        alice, bob = self.pair(maintenance=False)
        parent, child = self.delegated_parent(alice)
        class WrongTask(Client):
            def request(client, path, body=None):
                response = super(WrongTask, client).request(path, body)
                return dict(response, task_id='impostor') if path.startswith('/v1/mesh/task?') else response
        alice.client_factory = WrongTask
        alice.step()
        self.assertEqual('waiting_remote', alice.store.task_status(child['id'])['status'])
        self.assertEqual('waiting_children', alice.store.task_status(parent['id'])['status'])
        self.assertEqual('task_receipt_identity_mismatch', self.incidents(alice, 'bob')['code'])

    def test_poll_and_report_use_actual_ownerbound_task_and_do_not_broadcast_local_work(self):
        alice, bob = self.pair(maintenance=False)
        alice.network.enqueue('bob', self.message())
        alice.step()
        task = bob.store.claim('bob')
        bob.store.update_task(task['id'], 'bob', task['epoch'], status='completed', result='verified fixture')
        alice.step()
        with alice.store.transaction() as db:
            remote = dict(db.execute('SELECT * FROM node_remote_tasks').fetchone())
        self.assertEqual('completed', remote['status'])
        self.assertEqual(1, remote['terminal'])
        self.assertEqual('verified fixture', json.loads(remote['result']))
        bob.network.receive('operator', self.message('bob', 'private-autonomous'))
        local = bob.store.claim('bob')
        bob.store.update_task(local['id'], 'bob', local['epoch'], status='completed', result='do not broadcast')
        bob.step()
        with alice.store.transaction() as db:
            reports = [json.loads(row['body']) for row in db.execute('SELECT body FROM mesh_reports')]
        self.assertFalse(any(report.get('result') == 'do not broadcast' for report in reports))
        self.assertTrue(any(report.get('result') == 'verified fixture' for report in reports))
        with bob.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM node_report_receipts WHERE accepted=1').fetchone()[0])

    def test_real_local_worker_without_backend_waits_not_fake_offline_inference(self):
        config = self.config('alice', port=0, start_worker=True)
        def worker_factory(worker_config, config_path=None):
            return Worker(worker_config, config_path=config_path, backend=UnavailableBackend)
        node = self.node(config, worker_factory=worker_factory)
        node.start()
        operator = Client(dict(node.worker_config, token_file=self.secret('alice-operator')))
        receipt = operator.request('/v1/mesh/local', {'message': self.message('alice', 'no-model')})
        self.wait(lambda: node.store.task_status(receipt['task_id'])['status'] == 'waiting_backend')
        task = node.store.task_status(receipt['task_id'])
        self.assertIn('no_local_backend_available', task['result'])
        self.assertNotIn('completed', task['status'])
        self.assertIsNone(node.store.status()['leader']['node'])
        self.assertEqual('not_verified_by_transport', node.step()['model_availability'])

    def canonical_runtime(self, config):
        package = self.directory / 'diagnostic-package'
        package.mkdir(mode=0o700)
        for name in ('bin', 'codex-resources', 'codex-path'):
            (package / name).mkdir(mode=0o700)
        executable, helper = package / 'bin/codex', package / 'bin/codex-code-mode-host'
        for path in (executable, helper):
            path.write_bytes(b'fixture metadata only, must not execute')
            path.chmod(0o755)
        (package / 'codex-package.json').write_text(json.dumps({
            'layoutVersion': 1, 'variant': 'codex', 'version': '0.159.2',
            'target': 'x86_64-unknown-linux-musl', 'entrypoint': 'bin/codex',
            'resourcesDir': 'codex-resources', 'pathDir': 'codex-path'}))
        (package / 'codex-package.json').chmod(0o644)
        worker = json.loads(Path(config['local_worker_config']).read_text())
        worker['codex']['executable'] = str(executable)
        self.rewrite(config['local_worker_config'], codex=worker['codex'])
        return package, helper

    def test_external_companion_observes_missing_helper_and_unique_maintenance_episode(self):
        config = self.config('alice', central=True, start_worker=False)
        package, helper = self.canonical_runtime(config)
        helper.unlink()
        node = self.node(config)
        with patch('subprocess.Popen', side_effect=AssertionError('no native/model execution')):
            node._observe_runtime(force=True)
            node._observe_runtime(force=True)
        incident = self.incidents(node, LOCAL_RUNTIME)
        self.assertEqual(1, incident['episode'])
        self.assertEqual(1, incident['active'])
        self.assertEqual(1, node.store.status()['tasks']['pending'])
        self.assertEqual('missing', node.status()['runtime_observation']['runtimes'][0]['layout'])
        self.assertTrue(node.status()['runtime_attention_required'])
        self.assertFalse(node.status()['runtime_execution_verified'])
        self.assertEqual('external_or_not_started', node.status()['worker_management'])
        with node.store.transaction() as db:
            task = db.execute('SELECT input,context,required FROM tasks WHERE id=?', (incident['task_id'],)).fetchone()
        self.assertIn('坏 Shell', task['input'])
        self.assertIn('真实执行验收', task['input'])
        self.assertNotEqual('leader:owner', json.loads(task['context'])['session_scope'])
        self.assertEqual(['agent', 'mesh.node:alice'], json.loads(task['required']))

    def test_layout_repair_or_owned_heartbeat_is_not_execution_incident_recovery(self):
        config = self.config('alice', central=True)
        package, helper = self.canonical_runtime(config)
        helper.unlink()
        node = self.node(config)
        node._observe_runtime(force=True)
        helper.write_bytes(b'restored fixture, not an execution proof')
        helper.chmod(0o755)
        node.store.heartbeat('alice', ['agent', 'mesh.node:alice', 'leader'])
        node._observe_runtime(force=True)
        self.assertEqual('complete', node.status()['runtime_observation']['runtimes'][0]['layout'])
        self.assertTrue(node.status()['runtime_attention_required'])
        self.assertEqual(1, self.incidents(node, LOCAL_RUNTIME)['active'])
        self.assertFalse(node.status()['runtime_execution_verified'])

    def test_unknown_layout_never_creates_runtime_incident_or_blocks_worker(self):
        node = self.node(self.config('alice', central=True))
        node._observe_runtime(force=True)
        self.assertEqual('unknown', node.status()['runtime_observation']['runtimes'][0]['layout'])
        self.assertIsNone(self.incidents(node, LOCAL_RUNTIME))
        self.assertTrue(node.status()['local_work_allowed'])

    def test_disabled_maintenance_still_records_runtime_attention_without_model_task(self):
        config = self.config('alice', central=True)
        config['auto_maintenance'] = False
        package, helper = self.canonical_runtime(config)
        helper.unlink()
        node = self.node(config)
        node._observe_runtime(force=True)
        self.assertTrue(node.status()['runtime_attention_required'])
        self.assertIsNone(self.incidents(node, LOCAL_RUNTIME)['task_id'])
        self.assertEqual(0, node.store.status()['tasks'].get('pending', 0))

    def test_config_binding_drift_and_private_errors_are_unknown_not_applied(self):
        config = self.config('alice', central=True)
        node = self.node(config)
        self.rewrite(config['local_worker_config'], node_id='other-node',
                     secret_payload='NEVER_OUTPUT_PRIVATE_CONFIG')
        node._observe_runtime(force=True)
        report = node.status()['runtime_observation']
        self.assertEqual('runtime_observation_unavailable', report['error'])
        self.assertNotIn('NEVER_OUTPUT', json.dumps(report))
        self.assertEqual('alice', node.worker_config['node_id'])
        self.assertIsNone(self.incidents(node, LOCAL_RUNTIME))

    def test_optional_observer_failure_and_interval_do_not_change_native_worker(self):
        node = self.node(self.config('alice', central=True))
        with patch('assistant_mesh.node.diagnose_runtime', side_effect=RuntimeError('PRIVATE_PROVIDER_BODY')) as observer:
            node._observe_runtime(force=True)
            node._observe_runtime()
        self.assertEqual(1, observer.call_count)
        self.assertNotIn('PRIVATE_PROVIDER_BODY', json.dumps(node.status()))
        self.assertFalse(node.status()['runtime_attention_required'])
        self.assertTrue(node.status()['local_work_allowed'])

    def test_optional_observation_write_failure_is_redacted_and_does_not_stop_worker_start(self):
        config = self.config('alice', central=True, start_worker=True)
        node = self.node(config, worker_factory=UnavailableBackend)
        original_set = node.store.set
        def fail_observation(key, value):
            if key == 'node_runtime_observation':
                raise OSError('PRIVATE_DATABASE_PATH_AND_BODY')
            return original_set(key, value)
        with patch.object(node.store, 'set', side_effect=fail_observation):
            node.start()
            node.step()
        self.assertEqual(1, node.store.get('node_worker_failure')['failures'])
        status = node.status()
        self.assertFalse(status['runtime_observation_persisted'])
        self.assertEqual('runtime_observation_record_failed', status['runtime_observation']['persistence_error'])
        self.assertNotIn('PRIVATE_DATABASE', json.dumps(status))
        self.assertTrue(status['local_work_allowed'])

    def test_optional_runtime_incident_write_failure_keeps_in_memory_attention(self):
        config = self.config('alice', central=True)
        package, helper = self.canonical_runtime(config)
        helper.unlink()
        node = self.node(config)
        with patch.object(node, '_incident', side_effect=OSError('PRIVATE_INCIDENT_ERROR')):
            node._observe_runtime(force=True)
        status = node.status()
        self.assertTrue(status['runtime_attention_required'])
        self.assertFalse(status['runtime_observation_persisted'])
        self.assertFalse(status['runtime_execution_verified'])
        self.assertNotIn('PRIVATE_INCIDENT', json.dumps(status))

    def test_valid_peer_local_runtime_cannot_clear_internal_runtime_incident(self):
        config = self.config('alice', central=True)
        config['peers'] = [{'node': 'local-runtime', 'client_config': config['local_worker_config']}]
        package, helper = self.canonical_runtime(config)
        helper.unlink()
        node = self.node(config)
        node._observe_runtime(force=True)
        node._incident('local-runtime', 'connection_unavailable')
        node._connected('local-runtime')
        self.assertEqual(0, self.incidents(node, 'local-runtime')['active'])
        self.assertEqual(1, self.incidents(node, LOCAL_RUNTIME)['active'])
        self.assertTrue(node.status()['runtime_attention_required'])

    def test_optional_incident_select_failure_does_not_stop_healthy_native_child(self):
        class HealthyWorker:
            def __init__(worker, *args, **kwargs):
                worker.stop = threading.Event()
            def run(worker):
                worker.stop.wait(5)
        node = self.node(self.config('alice', central=True, start_worker=True), worker_factory=HealthyWorker)
        node.start()
        transaction = node.store.transaction
        class AdvisoryFailure:
            def __init__(proxy, db):
                proxy.db = db
            def execute(proxy, statement, *args):
                if statement.startswith('SELECT active FROM node_incidents'):
                    raise OSError('PRIVATE_INCIDENT_READ_BODY')
                return proxy.db.execute(statement, *args)
        @contextlib.contextmanager
        def failed_optional_read():
            with transaction() as db:
                yield AdvisoryFailure(db)
        with patch.object(node.store, 'transaction', side_effect=failed_optional_read):
            snapshot = node.step()
        self.assertTrue(node.worker_thread.is_alive())
        self.assertFalse(node.worker.stop.is_set())
        self.assertEqual('unknown', snapshot['runtime_incident_status'])
        self.assertTrue(snapshot['runtime_attention_required'])
        self.assertNotIn('PRIVATE_INCIDENT_READ', json.dumps(snapshot))

    def test_worker_constructor_failure_and_unexpected_exit_have_durable_backoff(self):
        calls = []
        def failed(*args, **kwargs):
            calls.append(True)
            raise ValueError('credential-value-must-not-be-logged')
        config = self.config('alice', port=0, start_worker=True)
        node = self.node(config, worker_factory=failed)
        node.start()
        node.step()
        self.assertEqual(1, len(calls))
        self.assertEqual(1, node.store.get('node_worker_failure')['failures'])
        self.assertEqual(1, node.store.status()['tasks']['pending'])
        self.now += 3
        node.step()
        self.assertEqual(2, len(calls))
        self.assertEqual(2, node.store.get('node_worker_failure')['failures'])
        self.assertEqual(1, node.store.status()['tasks']['pending'])
        with node.store.transaction() as db:
            self.assertNotIn('credential-value-must-not-be-logged', str([dict(row) for row in db.execute('SELECT * FROM node_events')]))
        class ExitedWorker:
            def __init__(worker, *args, **kwargs):
                worker.stop = threading.Event()
            def run(worker):
                return
        node.worker_factory = ExitedWorker
        self.now += 5
        node.step()
        self.wait(lambda: node.store.get('node_worker_failure')['failures'] == 3)
        node.step()
        self.assertEqual(3, node.store.get('node_worker_failure')['failures'])

    def test_server_runtime_failure_is_not_reported_as_live(self):
        node = self.node(self.config('alice', port=0))
        node.start()
        node.server.shutdown()
        node.server_thread.join(timeout=3)
        with self.assertRaisesRegex(ValueError, 'local_node_server_stopped'):
            node.step()
        self.assertFalse(node.status()['local_server_alive'])

    def test_worker_runtime_failure_clears_only_after_stable_owned_heartbeat(self):
        class HeartbeatWorker:
            def __init__(worker, config, config_path=None):
                worker.config, worker.stop = config, threading.Event()
            def run(worker):
                Client(worker.config).request('/v1/heartbeat', {'capabilities': worker.config['capabilities']})
                worker.stop.wait(5)
        node = self.node(self.config('alice', port=0, start_worker=True), worker_factory=HeartbeatWorker)
        node.store.set('node_worker_failure', {'failures': 2, 'retry_at': 0})
        node._incident('local-worker', 'local_worker_runtime_failed')
        node.start()
        self.wait(lambda: bool(node.store.status()['nodes']))
        node.step()
        self.assertEqual(2, node.store.get('node_worker_failure')['failures'])
        node.worker_started_at -= 31  # injected elapsed time, not inference
        node.step()
        self.assertEqual(0, node.store.get('node_worker_failure')['failures'])
        self.assertEqual(0, self.incidents(node, 'local-worker')['active'])

    def test_cli_runner_rechecks_private_config_before_start(self):
        config = self.config('bob', central=True)
        path = self.private('entry.json', config)
        with self.assertRaisesRegex(ValueError, 'changed_before_start'):
            run(dict(config, node_id='impostor'), path)
        with patch('assistant_mesh.node.Node.run_forever', return_value='stopped') as runner:
            self.assertEqual('stopped', run(config, path))
            runner.assert_called_once()


if __name__ == '__main__':
    unittest.main()
