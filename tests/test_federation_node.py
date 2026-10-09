"""Optional catalogue thread does not replace native/A2A supervisor work."""
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.test_node as node_fixture
from assistant_mesh.resources import Registry
from assistant_mesh.worker import Client


class FederationNodeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = node_fixture.NodeTests(methodName='runTest')
        self.fixture.setUp()
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        self.fixture.tearDown()

    def pair(self, client_factory=Client):
        first = self.fixture.config('alice', other='bob')
        second = self.fixture.config('bob', central=True, other='alice')
        worker = json.loads(Path(second['local_worker_config']).read_text())
        for config in (first, second):
            server = json.loads(Path(config['local_server_config']).read_text())
            server['federation'] = {'export_enabled': True, 'projection_enabled': True}
            server['peers'][-1]['capabilities'].append('capability.catalog')
            Path(config['local_server_config']).write_text(json.dumps(server))
        first['peers'] = [{'node': 'bob', 'authority': 'bob', 'sync_capabilities': True,
            'client_config': self.fixture.private('a-to-b.json', {'control_url': worker['control_url'],
                'token_file': self.fixture.secret('bob-from-alice')})}]
        first['federation_sync'] = {'interval_seconds': 1}
        alice = self.fixture.node(first, client_factory=client_factory)
        self.fixture.start_external(second)
        return alice, second

    def test_default_has_no_catalogue_runtime(self):
        with patch('assistant_mesh.federation_sync.FederationSync', side_effect=AssertionError('DO_NOT_INITIALIZE')):
            node = self.fixture.node(self.fixture.config('alice'))
            node.start()
        self.assertIsNone(node.federation_thread)
        self.assertEqual('disabled', node.status()['federated_capabilities']['status'])

    def test_catalogue_background_thread_reads_real_peer_and_retains_local_tasks(self):
        node, second = self.pair()
        registry = Registry(node_fixture.Store(json.loads(Path(second['local_server_config']).read_text())['database'],
                                                clock=lambda: self.fixture.now))
        registry.advertise('node:bob', 'remote-egress', 'network.egress', lease_seconds=300)
        task = node.store.create_task('Local offline work', task_id='local-work', required=['agent'])
        node.start()
        self.fixture.wait(lambda: bool(node.federation_syncs['bob'].projection.discover()['capabilities']))
        self.assertEqual('pending', node.store.task_status(task)['status'])
        self.assertFalse(node.status()['federated_capabilities']['managed_invocation_authorized'])
        self.assertTrue(node.federation_thread.is_alive())
        node.stop()
        self.assertFalse(node.federation_thread.is_alive())

    def test_slow_catalogue_does_not_block_normal_node_step_or_create_global_task(self):
        entered, release = threading.Event(), self.release
        def factory(configuration):
            real = Client(configuration)
            class SlowCatalogue:
                def request(inner, path, payload=None):
                    if 'capability-export' in path:
                        entered.set()
                        release.wait(5)
                    return real.request(path, payload)
            return SlowCatalogue()
        node, _ = self.pair(factory)
        node.start()
        self.assertTrue(entered.wait(3))
        started = time.monotonic()
        result = node.step()
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(result['local_work_allowed'])
        self.assertFalse(result['global_takeover_allowed'])
        with node.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
        release.set()

    def test_catalogue_authorization_failure_is_not_a2a_or_native_failure(self):
        node, second = self.pair()
        # The running API will read this revoked finite catalogue grant.
        server = json.loads(Path(second['local_server_config']).read_text())
        server['peers'][-1]['capabilities'].remove('capability.catalog')
        for running, thread in self.fixture.external_servers:
            running.shutdown()
            thread.join(timeout=3)
            running.server_close()
        self.fixture.external_servers.clear()
        Path(second['local_server_config']).write_text(json.dumps(server))
        self.fixture.start_external(second)
        node.start()
        self.fixture.wait(lambda: node.federation_observation.get('peers', {}).get('bob', {}).get('state') == 'degraded')
        result = node.step()
        self.assertTrue(result['a2a_reachable'])
        self.assertFalse(result['global_takeover_allowed'])
        self.assertEqual(['catalog_authorization_rejected'], node.federation_observation['peers']['bob']['diagnostics'])
        with node.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
