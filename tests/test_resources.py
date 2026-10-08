import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.resources import Registry
from assistant_mesh.store import Conflict, Store


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = Store(Path(self.temp.name) / 'mesh.db', clock=lambda: self.now)
        self.registry = Registry(self.store, trusted_verifiers=['probe:independent', 'node:a'])

    def tearDown(self):
        self.temp.cleanup()

    def advertise(self, id='cap-a', actor='node:a', kind='compute.future.quantum', **kwargs):
        return self.registry.advertise(actor, id, kind, **kwargs)

    def remote_grant(self, action='sensor.read', scope=None, actor='node:b', request_id='request'):
        scope = {'device': 'thermometer', 'field': 'temperature'} if scope is None else scope
        self.registry.request_grant(actor, 'cap-a', action, scope, request_id=request_id)
        return self.registry.grant('operator', request_id)

    def test_open_capability_kind_and_description_survive_restart(self):
        first = self.advertise(spec={'workload': ['model-created-tool', 'inference'], 'throughput': {'value': 41, 'unit': 'tokens/s'}},
                               description='Device role is not fixed.')
        restored = Registry(Store(self.store.path, clock=lambda: self.now))
        self.assertEqual(first, restored.discover()['capabilities'][0])
        self.assertEqual('compute.future.quantum', restored.describe('cap-a')['kind'])
        self.assertEqual('declared', first['verification'])
        self.assertEqual([], restored.describe('cap-a')['metrics'])

    def test_registration_is_not_a_remote_grant_or_health_proof(self):
        row = self.advertise()
        self.assertEqual('unknown', row['health'])
        self.assertEqual('declared', row['health_verification'])
        result = self.registry.authorize('node:b', 'cap-a', 'execute', '/project')
        self.assertFalse(result['allowed'])
        self.assertEqual('exact_grant_required', result['reason'])
        self.assertTrue(self.registry.authorize('node:a', 'cap-a', 'own.native.tool', '/project')['allowed'])

    def test_principal_cannot_be_spoofed_even_by_provider(self):
        with self.assertRaises(PermissionError):
            self.registry.advertise('node:b', 'cap-a', 'new.tool', principal='node:a')
        self.advertise()
        with self.assertRaises(PermissionError):
            self.advertise(actor='node:b')
        with self.assertRaises(PermissionError):
            self.registry.renew('node:b', 'cap-a', 1)

    def test_identical_registration_retry_does_not_extend_lease(self):
        first = self.advertise()
        self.now += 10
        self.assertEqual(first['deadline'], self.advertise()['deadline'])
        self.assertEqual(1, self.advertise()['epoch'])
        self.assertEqual(1, len(self.registry.audit()['events']))
        with self.assertRaises(Conflict):
            self.advertise(expected_epoch=2)

    def test_metadata_update_and_expired_readvertisement_are_fenced(self):
        self.advertise(spec={'model': 'frontier-a'})
        with self.assertRaises(Conflict):
            self.advertise(spec={'model': 'frontier-b'})
        updated = self.advertise(spec={'model': 'frontier-b'}, expected_epoch=1)
        self.assertEqual(2, updated['epoch'])
        with self.assertRaises(Conflict):
            self.advertise(spec={'model': 'frontier-old'}, expected_epoch=1)
        self.now += 91
        self.assertEqual([], self.registry.discover()['capabilities'])
        with self.assertRaises(Conflict):
            self.registry.renew('node:a', 'cap-a', 2)
        with self.assertRaises(Conflict):
            self.advertise(spec={'model': 'frontier-b'})
        self.assertEqual(3, self.advertise(spec={'model': 'frontier-b'}, expected_epoch=2)['epoch'])

    def test_live_renewal_reports_health_without_verifying_it(self):
        self.advertise()
        self.now += 5
        renewed = self.registry.renew('node:a', 'cap-a', 1, health='degraded', lease_seconds=150)
        self.assertEqual(1155, renewed['deadline'])
        self.assertEqual('declared', renewed['health_verification'])
        self.registry.renew('node:a', 'cap-a', 1, health='failed')
        self.assertEqual([], self.registry.discover()['capabilities'])
        self.assertFalse(self.registry.authorize('node:a', 'cap-a', 'native.use', '/project')['allowed'])
        self.registry.renew('node:a', 'cap-a', 1, health='healthy')
        self.assertEqual(1, len(self.registry.discover()['capabilities']))

    def test_revocation_tombstone_cannot_be_renewed_or_readvertised(self):
        self.advertise()
        self.registry.revoke('operator', 'cap-a', 1, reason='owner changed authority')
        self.registry.revoke('operator', 'cap-a', 1)
        self.assertEqual([], self.registry.discover()['capabilities'])
        self.assertTrue(self.registry.describe('cap-a')['revoked'])
        with self.assertRaises(Conflict):
            self.registry.renew('node:a', 'cap-a', 1)
        with self.assertRaises(Conflict):
            self.advertise(expected_epoch=1)
        with self.assertRaises(PermissionError):
            self.registry.revoke('node:b', 'cap-a', 1)
        self.assertEqual(1, sum(event['operation'] == 'revoke' for event in self.registry.audit()['events']))

    def test_observed_performance_is_time_workload_and_source_specific(self):
        self.advertise()
        observation = self.registry.observe('node:a', 'cap-a', 'tokens.generated.per_second', 12.75,
                                           'tokens/s', 'provider benchmark', sample_time=995,
                                           evidence={'method': 'warm-run', 'workload': '7B q4', 'samples': 3},
                                           epoch=1, observation_id='metric-1')
        self.assertEqual('declared', observation['verification'])
        self.assertEqual(995, observation['sample_time'])
        self.assertEqual(1000, observation['received'])
        self.assertEqual('7B q4', observation['evidence']['workload'])
        self.assertEqual(12.75, self.registry.describe('cap-a')['metrics'][0]['value'])
        restored = Registry(Store(self.store.path, clock=lambda: self.now))
        self.assertEqual(observation, restored.describe('cap-a')['metrics'][0])

    def test_only_independent_trusted_verifier_can_mark_verified(self):
        self.advertise()
        common = {'metric': 'latency', 'value': 20, 'unit': 'ms', 'source': 'ping', 'epoch': 1,
                  'verification': 'verified', 'evidence': {'probe': 'bounded-check', 'result': 'sha256:test'}}
        with self.assertRaises(PermissionError):
            self.registry.observe('node:a', 'cap-a', **common)  # trusted provider still cannot self-certify
        with self.assertRaises(PermissionError):
            self.registry.observe('node:untrusted', 'cap-a', **common)
        with self.assertRaises(PermissionError):
            self.registry.observe('probe:independent', 'cap-a', **dict(common, evidence=None))
        checked = self.registry.observe('probe:independent', 'cap-a', **common)
        self.assertEqual('verified', checked['verification'])
        self.assertEqual('declared', self.registry.describe('cap-a')['verification'])

    def test_observation_stable_id_rejects_changed_content_and_old_epoch(self):
        self.advertise()
        args = dict(metric='load', value=0.5, unit='ratio', source='proc', epoch=1, observation_id='load1', sample_time=1000)
        first = self.registry.observe('node:a', 'cap-a', **args)
        self.assertEqual(first, self.registry.observe('node:a', 'cap-a', **args))
        with self.assertRaises(Conflict):
            self.registry.observe('node:a', 'cap-a', **dict(args, value=0.9))
        self.advertise(spec={'runtime': 'updated'}, expected_epoch=1)
        with self.assertRaises(Conflict):
            self.registry.observe('node:a', 'cap-a', **args)
        self.assertFalse(self.registry.describe('cap-a')['metrics'][0]['current_epoch'])

    def test_private_fields_and_url_credentials_do_not_enter_discovery(self):
        self.advertise(spec={'endpoint': 'https://user:password@host.test/run?token=secret#part',
                             'token': 'private-value', 'auth': {'scopes': ['file.read'], 'refresh_token': 'private-refresh'},
                             'routes': [{'api-key': 'private-api', 'privateKey': 'private-key', 'kind': 'https'}],
                             'accessToken': 'private-access', 'credentials': {'value': 'private-binding'}})
        observed = self.registry.observe('node:a', 'cap-a', 'route', {'cookie': 'private-cookie', 'hops': 2},
                                         'count', 'https://host.test/probe?password=private-password',
                                         evidence={'client_secret': 'private-secret', 'method': 'bounded'}, epoch=1)
        serialized = json.dumps(self.registry.describe('cap-a'))
        for value in ('private-value', 'private-refresh', 'private-api', 'private-key', 'private-access',
                      'private-binding', 'private-cookie', 'private-password', 'private-secret', 'user:password'):
            self.assertNotIn(value, serialized)
        spec = self.registry.describe('cap-a')['spec']
        self.assertEqual('https://host.test/run', spec['endpoint'])
        self.assertEqual(['file.read'], spec['auth']['scopes'])
        self.assertTrue(self.registry.describe('cap-a')['secret_fields_redacted'])
        self.assertTrue(observed['secret_fields_redacted'])

    def test_resource_graph_models_routes_not_implicit_permissions(self):
        self.advertise('laptop', kind='terminal.unrestricted-native')
        self.advertise('relay', actor='node:cloud', kind='network.public_endpoint')
        self.advertise('gateway', actor='node:phone', kind='radio.bluetooth_gateway')
        self.advertise('sensor', actor='node:phone', kind='sensor.temperature')
        self.registry.link('node:a', 'edge1', 'laptop', 'relay', relation='reachable_via', epoch=1)
        self.registry.link('node:cloud', 'edge2', 'relay', 'gateway', relation='reverse_connected', epoch=1)
        self.registry.link('node:phone', 'edge3', 'gateway', 'sensor', relation='nearby', epoch=1)
        graph = self.registry.graph()
        self.assertEqual(4, len(graph['capabilities']))
        self.assertEqual(3, len(graph['edges']))
        self.assertFalse(graph['invocation_authorized'])
        self.assertTrue(all(not edge['invocation_authorized'] for edge in graph['edges']))
        self.assertFalse(self.registry.authorize('node:a', 'sensor', 'sensor.read', 'temperature')['allowed'])
        with self.assertRaises(PermissionError):
            self.registry.link('node:a', 'fake-edge', 'gateway', 'sensor', epoch=1)

    def test_edges_expire_when_endpoint_expires_or_changes_epoch(self):
        self.advertise('source', lease_seconds=300)
        self.advertise('target', actor='node:b', lease_seconds=20)
        first = self.registry.link('node:a', 'path', 'source', 'target', lease_seconds=100, epoch=1)
        self.assertEqual(1020, first['deadline'])
        self.now += 5
        self.assertEqual(first, self.registry.link('node:a', 'path', 'source', 'target', lease_seconds=100, epoch=1))
        self.registry.advertise('node:b', 'target', 'new.capability', expected_epoch=1)
        self.assertEqual([], self.registry.graph()['edges'])
        self.assertFalse(self.registry.graph(include_unavailable=True)['edges'][0]['available'])
        with self.assertRaises(Conflict):
            self.registry.link('node:a', 'path', 'source', 'target', epoch=1)
        fresh = self.registry.link('node:a', 'new-path', 'source', 'target', epoch=1)
        self.assertTrue(fresh['available'])
        self.now += 100
        self.assertEqual([], self.registry.graph()['edges'])

    def test_remote_exact_action_scope_principal_are_owner_approved(self):
        self.advertise()
        scope = {'device': 'thermometer', 'field': 'temperature'}
        self.registry.request_grant('node:b', 'cap-a', 'sensor.read', scope, request_id='request')
        with self.assertRaises(PermissionError):
            self.registry.grant('node:b', 'request')
        with self.assertRaises(PermissionError):
            self.registry.grant('node:a', 'request')
        grant = self.registry.grant('operator', 'request')
        exact = self.registry.authorize('node:b', 'cap-a', 'sensor.read', {'field': 'temperature', 'device': 'thermometer'})
        self.assertTrue(exact['allowed'])
        self.assertEqual(grant['id'], exact['grant_id'])
        self.assertFalse(self.registry.authorize('node:other', 'cap-a', 'sensor.read', scope)['allowed'])
        self.assertFalse(self.registry.authorize('node:b', 'cap-a', 'sensor.write', scope)['allowed'])
        self.assertFalse(self.registry.authorize('node:b', 'cap-a', 'sensor.read', {'device': 'thermometer'})['allowed'])
        self.assertFalse(self.registry.authorize('node:b', 'cap-a', 'sensor.read', '/temperature/*')['allowed'])
        self.assertFalse(self.registry.authorize('node:b', 'cap-a', 'sensor.read', scope, grant_id='unrelated')['allowed'])

    def test_wildcard_strings_are_literal_not_scope_expansion(self):
        self.advertise()
        self.remote_grant(action='file.read', scope='/project/*')
        self.assertTrue(self.registry.authorize('node:b', 'cap-a', 'file.read', '/project/*')['allowed'])
        self.assertFalse(self.registry.authorize('node:b', 'cap-a', 'file.read', '/project/secret')['allowed'])

    def test_grant_expiry_revocation_and_epoch_invalidate_cached_authorization(self):
        self.advertise(lease_seconds=600)
        scope = '/same/scope'
        self.registry.request_grant('node:b', 'cap-a', 'read', scope, request_id='request')
        first = self.registry.grant('operator', 'request', expires_seconds=30)
        self.now += 5
        self.assertEqual(first['deadline'], self.registry.grant('operator', 'request', expires_seconds=500)['deadline'])
        self.assertTrue(self.registry.authorize('node:b', 'cap-a', 'read', scope, request_id='invoke1')['allowed'])
        self.now += 26
        self.assertFalse(self.registry.authorize('node:b', 'cap-a', 'read', scope, request_id='invoke1')['allowed'])
        self.remote_grant('read', scope, request_id='another')
        self.assertTrue(self.registry.authorize('node:b', 'cap-a', 'read', scope, request_id='invoke2')['allowed'])
        self.registry.revoke_grant('operator', 'grant-another')
        self.assertFalse(self.registry.authorize('node:b', 'cap-a', 'read', scope, request_id='invoke2')['allowed'])
        self.assertFalse(self.registry.grant('operator', 'another')['available'])
        self.remote_grant('read', scope, request_id='third')
        self.advertise(spec={'new': 'resource shape'}, expected_epoch=1)
        self.assertFalse(self.registry.authorize('node:b', 'cap-a', 'read', scope)['allowed'])

    def test_stable_requests_reject_immutable_payload_conflicts(self):
        self.advertise()
        first = self.registry.request_grant('node:b', 'cap-a', 'read', '/path', request_id='same')
        self.assertEqual(first, self.registry.request_grant('node:b', 'cap-a', 'read', '/path', request_id='same'))
        with self.assertRaises(Conflict):
            self.registry.request_grant('node:b', 'cap-a', 'write', '/path', request_id='same')
        with self.assertRaises(Conflict):
            self.registry.request_grant('node:c', 'cap-a', 'read', '/path', request_id='same')
        self.registry.grant('operator', 'same', grant_id='stable-grant')
        self.registry.request_grant('node:b', 'cap-a', 'write', '/path', request_id='different')
        with self.assertRaises(Conflict):
            self.registry.grant('operator', 'different', grant_id='stable-grant')

    def test_stale_request_cannot_be_approved_after_capability_update(self):
        self.advertise()
        self.registry.request_grant('node:b', 'cap-a', 'read', '/path', request_id='request')
        self.advertise(spec={'changed': True}, expected_epoch=1)
        with self.assertRaises(Conflict):
            self.registry.grant('operator', 'request')
        with self.assertRaises(Conflict):
            self.registry.request_grant('node:b', 'cap-a', 'read', '/path', request_id='request')

    def test_audit_is_ordered_private_filtered_and_persistent(self):
        self.advertise()
        self.registry.authorize('node:b', 'cap-a', 'read', '/path')
        self.remote_grant('read', '/path')
        before = self.registry.audit(limit=2)
        after = self.registry.audit(after=before['next'])
        self.assertEqual([1, 2], [event['sequence'] for event in before['events']])
        self.assertTrue(all(event['sequence'] > before['next'] for event in after['events']))
        only_actor = self.registry.audit(principal='node:b')
        self.assertTrue(all(event['actor'] == 'node:b' for event in only_actor['events']))
        restored = Registry(Store(self.store.path, clock=lambda: self.now))
        self.assertEqual(self.registry.audit(), restored.audit())

    def test_mutation_and_audit_are_one_transaction_not_partial_state(self):
        with mock.patch.object(self.registry, '_audit', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                self.advertise()
        self.assertEqual([], self.registry.discover()['capabilities'])
        self.advertise()
        with mock.patch.object(self.registry, '_audit', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                self.registry.renew('node:a', 'cap-a', 1, health='failed')
        self.assertEqual('unknown', self.registry.describe('cap-a')['health'])
        self.registry.request_grant('node:b', 'cap-a', 'read', '/path', request_id='request')
        with mock.patch.object(self.registry, '_audit', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                self.registry.grant('operator', 'request')
        self.assertFalse(self.registry.authorize('node:b', 'cap-a', 'read', '/path')['allowed'])
        self.assertEqual('pending', self.registry.request_grant('node:b', 'cap-a', 'read', '/path', request_id='request')['status'])

    def test_two_concurrent_updates_cannot_both_pass_same_epoch(self):
        self.advertise()
        barrier, results, lock = threading.Barrier(2), [], threading.Lock()

        def update(value):
            registry = Registry(Store(self.store.path, clock=lambda: self.now))
            barrier.wait()
            try:
                result = registry.advertise('node:a', 'cap-a', 'compute.future.quantum', spec={'v': value}, expected_epoch=1)
            except Conflict:
                result = 'fenced'
            with lock:
                results.append(result)

        threads = [threading.Thread(target=update, args=(value,)) for value in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(1, sum(result == 'fenced' for result in results))
        self.assertEqual(2, self.registry.describe('cap-a')['epoch'])

    def test_input_limits_reject_nonfinite_time_metrics_and_invalid_ids(self):
        for ttl in (0, -1, True, float('inf'), 86401, 10 ** 1000):
            with self.assertRaises(ValueError):
                self.advertise(lease_seconds=ttl)
        with self.assertRaises(ValueError):
            self.advertise(id='bad\nidentifier')
        with self.assertRaises(ValueError):
            self.advertise(id='bad\ud800identifier')
        with self.assertRaises(ValueError):
            Registry(self.store, trusted_verifiers='node:a')
        with self.assertRaises(ValueError):
            self.advertise(spec={'x': float('nan')})
        self.advertise()
        with self.assertRaises(ValueError):
            self.registry.observe('node:a', 'cap-a', 'load', float('inf'), 'ratio', 'probe', epoch=1)
        with self.assertRaises(ValueError):
            self.registry.observe('node:a', 'cap-a', 'load', 1, 'ratio', 'probe', epoch=1, sample_time=2000)
        with self.assertRaises(ValueError):
            self.registry.renew('node:a', 'cap-a', True)
        with self.assertRaises(ValueError):
            self.registry.request_grant('node:b', 'cap-a', 'read', {})


if __name__ == '__main__':
    unittest.main()
