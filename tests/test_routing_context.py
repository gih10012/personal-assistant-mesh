"""Local evidence only: fixtures never probe networks or execute capabilities."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.allocations import Allocations
from assistant_mesh.federation_projection import Projection
from assistant_mesh.federation_source import FederationSource
from assistant_mesh.networking import Network
from assistant_mesh.remote import Remote
from assistant_mesh.resources import Registry
from assistant_mesh.routing_context import RoutingContext
from assistant_mesh.store import Conflict, Store


class RoutingContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = Store(Path(self.temp.name) / 'local.db', clock=lambda: self.now)
        self.registry = Registry(self.store, trusted_verifiers=['probe:independent'])
        self.allocations = Allocations(self.store, self.registry)
        self.projection = Projection(self.store)
        self.context = RoutingContext(self.store, 'authority-local', self.registry, self.allocations, self.projection)
        self.store.heartbeat('worker', ['agent', 'leader'])
        self.store.create_task('evidence fixture', task_id='parent', required=['leader'])
        self.task = self.store.claim('worker')

    def tearDown(self):
        self.temp.cleanup()

    def read(self, **kwargs):
        return self.context.read('parent', 'worker', self.task['epoch'], **kwargs)

    def rows(self, data, section):
        return data['sections'][section]['records']

    def cap(self, identity='shared-name', **kwargs):
        return self.registry.advertise('node:worker', identity, 'future.open.kind', **kwargs)

    def observe(self, **kwargs):
        values = {'evidence': {'scope': {'project': 'p'}, 'workload': {'bytes': 64}},
                  'epoch': 1, 'observation_id': 'measurement', 'verification': 'verified'}
        values.update(kwargs)
        return self.registry.observe('probe:independent', 'shared-name', 'latency.future', 3.2, 'ms', 'fixture', **values)

    def counts(self):
        with self.store.transaction() as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {name: db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0] for name in tables}

    def test_atomic_read_no_writes_no_score_no_execution_and_hash(self):
        self.cap()
        self.observe()
        before = self.counts()
        with mock.patch.object(self.store, 'transaction', wraps=self.store.transaction) as transaction:
            data = self.read()
        self.assertEqual(1, transaction.call_count)
        self.assertEqual(before, self.counts())
        self.assertTrue(data['database_atomic_snapshot'])
        for key in ('model_ranking_applied', 'network_probed', 'grant_checked', 'managed_invocation_authorized',
                    'native_tools_intercepted', 'global_consensus_verified', 'remote_metrics_included', 'remote_pools_included'):
            self.assertIs(data[key], False)
        digest = data.pop('evidence_sha256')
        self.assertEqual(digest, hashlib.sha256(json.dumps(data, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest())

    def test_units_scope_workload_provenance_and_no_default_freshness(self):
        self.cap()
        self.observe()
        metric = self.rows(self.read(), 'local_observations')[0]
        self.assertEqual(('ms', 3.2, 'probe:independent', 'verified'),
                         (metric['unit'], metric['value'], metric['actor'], metric['verification']))
        self.assertEqual({'project': 'p'}, metric['evidence']['scope'])
        self.assertEqual({'bytes': 64}, metric['evidence']['workload'])
        self.assertIsNone(metric['fresh_for_requested_age'])
        self.assertFalse(metric['scope_workload_checked'])
        metric = self.rows(self.read(observation_max_age_seconds=10), 'local_observations')[0]
        self.assertTrue(metric['fresh_for_requested_age'])
        self.assertEqual(1010, metric['freshness_deadline'])

    def test_exact_observation_age_boundary_is_stale(self):
        self.cap()
        self.observe(sample_time=990)
        metric = self.rows(self.read(observation_max_age_seconds=10), 'local_observations')[0]
        self.assertFalse(metric['fresh_for_requested_age'])

    def test_future_and_old_epoch_observations_cannot_be_fresh(self):
        self.cap()
        self.observe()
        with self.store.transaction() as db:
            db.execute('UPDATE capability_metrics SET sample_time=1001')
        metric = self.rows(self.read(observation_max_age_seconds=10), 'local_observations')[0]
        self.assertTrue(metric['sample_time_in_future'])
        self.assertFalse(metric['fresh_for_requested_age'])
        with self.store.transaction() as db:
            db.execute('UPDATE capability_metrics SET sample_time=1000,capability_epoch=99')
        self.assertFalse(self.rows(self.read(observation_max_age_seconds=10), 'local_observations')[0]['fresh_for_requested_age'])

    def test_local_and_remote_same_name_are_not_merged_or_capacity_imported(self):
        self.cap()
        remote_store = Store(Path(self.temp.name) / 'peer.db', clock=lambda: self.now)
        remote_registry = Registry(remote_store)
        remote_registry.advertise('node:foreign', 'shared-name', 'future.open.kind')
        self.projection.apply('authority-peer', FederationSource(remote_registry, 'authority-peer').export())
        data = self.read()
        local, remote = self.rows(data, 'local_capabilities')[0], self.rows(data, 'federated_capabilities')[0]
        self.assertEqual(local['id'], remote['id'])
        self.assertEqual('authority-peer', remote['issuer'])
        self.assertEqual('declared_estimate', remote['freshness_verification'])
        self.assertFalse(remote['managed_invocation_authorized'])
        self.assertEqual([], self.rows(data, 'local_pools'))

    def test_remote_contact_expired_outer_truth_keeps_inner_historical_declaration(self):
        remote_store = Store(Path(self.temp.name) / 'peer.db', clock=lambda: self.now)
        remote_registry = Registry(remote_store)
        remote_registry.advertise('node:foreign', 'foreign', 'future.open.kind')
        self.projection.apply('authority-peer', FederationSource(remote_registry, 'authority-peer').export())
        with self.store.transaction() as db:
            db.execute('UPDATE federation_projection_sources SET contact_deadline=1001')
        self.now += 2
        row = self.rows(self.read(), 'federated_capabilities')[0]
        self.assertFalse(row['available'])
        self.assertTrue(row['capability']['available'])
        self.assertEqual([], self.rows(self.read(include_unavailable=False), 'federated_capabilities'))

    def test_expired_and_revoked_pools_are_readable_false_not_abort(self):
        self.allocations.define_pool('operator', 'expired', {'concurrency': {'capacity': 2, 'unit': 'slot'}}, lease_seconds=1)
        self.allocations.define_pool('operator', 'revoked', {'concurrency': {'capacity': 2, 'unit': 'slot'}})
        self.allocations.revoke_pool('operator', 'revoked', 1)
        self.now += 2
        self.assertEqual([False, False], [row['available'] for row in self.rows(self.read(), 'local_pools')])

    def test_capacity_is_owner_contract_and_not_multiplied_by_capability_binding(self):
        for identity in ('shared-name', 'other'):
            self.cap(identity)
        self.allocations.define_pool('operator', 'one-pool', {'concurrency': {'capacity': 2, 'unit': 'slot'}})
        for identity in ('shared-name', 'other'):
            self.allocations.bind_pool('operator', identity, 1, 'one-pool', 1, 'concurrency', 1, 'slot', usage_key='one-physical-device')
        data = self.read()
        self.assertEqual(2, len(self.rows(data, 'local_pool_bindings')))
        self.assertEqual(1, len(self.rows(data, 'local_pools')))
        self.assertEqual(2, self.rows(data, 'local_pools')[0]['dimensions']['concurrency']['remaining'])
        self.assertEqual('owner_contract', self.rows(data, 'local_pools')[0]['capacity_verification'])

    def test_public_columns_do_not_export_future_private_schema_fields(self):
        self.cap()
        self.observe()
        self.allocations.define_pool('operator', 'pool', {'concurrency': {'capacity': 2, 'unit': 'slot'}})
        self.allocations.bind_pool('operator', 'shared-name', 1, 'pool', 1, 'concurrency', 1, 'slot')
        with self.store.transaction() as db:
            for table in ('capabilities', 'capability_metrics', 'capability_edges', 'resource_pools', 'capability_pool_bindings'):
                db.execute('ALTER TABLE ' + table + ' ADD COLUMN private_transport_hint TEXT DEFAULT "must-not-export"')
        encoded = json.dumps(self.read())
        self.assertNotIn('must-not-export', encoded)
        self.assertNotIn('private_transport_hint', encoded)

    def test_partial_sections_describe_selected_caps_not_global_completeness(self):
        self.cap('a')
        self.cap('b')
        data = self.read(limit=1)
        self.assertTrue(data['sections']['local_capabilities']['truncated'])
        self.assertEqual('selected_local_capability_ids_before_byte_truncation', data['sections']['local_observations']['selection_scope'])
        self.assertFalse(data['global_consensus_verified'])

    def test_disabled_projection_and_absent_allocations_are_explicit(self):
        context = RoutingContext(self.store, 'authority-local', self.registry)
        data = context.read('parent', 'worker', self.task['epoch'])
        self.assertEqual('disabled', data['sections']['federated_capabilities']['status'])
        self.assertEqual('not_initialized', data['sections']['local_pools']['status'])

    def test_worker_and_leader_fences_and_invalid_arguments(self):
        for kwargs in ({'limit': True}, {'limit': 101}, {'include_unavailable': 1},
                       {'observation_max_age_seconds': 0}, {'observation_max_age_seconds': float('nan')}, {'issuer': ''}):
            with self.assertRaises(ValueError):
                self.read(**kwargs)
        with self.assertRaises(ValueError):
            self.context.read('parent', 'worker', True)
        with self.assertRaises(Conflict):
            self.context.read('parent', 'other-worker', self.task['epoch'])
        with self.store.transaction() as db:
            db.execute('UPDATE leader SET epoch=epoch+1')
        with self.assertRaises(Conflict):
            self.read()

    def test_wire_budget_includes_flags_and_digest_and_marks_partial(self):
        for identity in ('a', 'b', 'c'):
            self.cap(identity, spec={'description': '中' * 1000})
        with mock.patch('assistant_mesh.routing_context.MAX_CONTEXT_BYTES', 6500):
            data = self.read()
        self.assertLessEqual(len(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()), 6500)
        self.assertTrue(data['byte_budget_truncated'])
        self.assertTrue(data['sections']['local_capabilities']['truncated'])
        self.assertFalse(data['sections']['local_capabilities']['complete_for_filter'])

    def test_truncation_keeps_honest_sampled_capability_scope_for_retained_observation(self):
        self.cap(spec={'large': 'x' * 8000})
        self.observe()
        with mock.patch('assistant_mesh.routing_context.MAX_CONTEXT_BYTES', 5000):
            data = self.read()
        self.assertEqual([], self.rows(data, 'local_capabilities'))
        self.assertEqual(1, len(self.rows(data, 'local_observations')))
        self.assertEqual(['shared-name'], data['selected_local_capability_ids'])
        self.assertEqual('selected_local_capability_ids_before_byte_truncation',
                         data['sections']['local_observations']['selection_scope'])

    def test_read_uses_one_sampled_as_of_for_capability_and_pool_freshness(self):
        self.cap(lease_seconds=1)
        self.allocations.define_pool('operator', 'pool', {'x': {'capacity': 2, 'unit': 'slot'}}, lease_seconds=1)
        with mock.patch.object(self.store, 'clock', side_effect=[1000, 1000] + [1002] * 20):
            data = self.read()
        self.assertEqual(1000, data['as_of'])
        self.assertTrue(self.rows(data, 'local_capabilities')[0]['available'])
        self.assertTrue(self.rows(data, 'local_pools')[0]['available'])

    def test_execution_candidates_keep_original_identity_no_automatic_binding(self):
        remote = Remote(self.store, Network(self.store, 'authority-local'))
        for index in range(2):
            remote.delegate('parent', 'worker', self.task['epoch'], 'remote-' + str(index), 'peer', {'input': 'fixture'})
        data = self.read(limit=1)
        section = data['sections']['remote_execution_candidates']
        self.assertTrue(section['truncated'])
        self.assertEqual('worker', section['records'][0]['caller_node'])
        self.assertEqual(section['records'][0]['child_id'], section['records'][0]['reference_id'])
        self.assertFalse(section['records'][0]['decision_binding_verified'])
        self.assertFalse(section['records'][0]['execution_verified'])
        self.assertEqual([], self.rows(data, 'managed_execution_candidates'))


if __name__ == '__main__':
    unittest.main()
