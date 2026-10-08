import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.allocations import Allocations, MAX_QUANTA
from assistant_mesh.resources import Registry
from assistant_mesh.store import Conflict, Store


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = Store(Path(self.temp.name) / 'mesh.db', clock=lambda: self.now)
        self.registry = Registry(self.store, trusted_verifiers=['probe:independent', 'node:cloud'])
        self.bridge = Allocations(self.store, self.registry)
        self.actor = 'node:laptop'
        self.scope = {'fixture': 'owned-read-only-result'}
        self.workload = {'operation': 'hash', 'input_bytes': 64}
        self.store.heartbeat('laptop', ['leader', 'agent'])
        self.store.create_task('fixture parent', task_id='parent')
        self.task = self.store.claim('laptop')
        self.capability('target')
        self.bridge.define_pool('operator', 'shared', {'concurrent.future': {'capacity': 2, 'unit': 'request-quantum'}})
        self.binding('target')
        self.plan = {'target': 'target', 'selections': [self.selection('target')], 'route': []}

    def tearDown(self):
        self.temp.cleanup()

    def capability(self, identity, provider='node:cloud'):
        self.registry.advertise(provider, identity, 'model.created.unenumerated', spec={'contract': 'read-only fixture'})
        self.registry.observe('probe:independent', identity, 'latency.future', 3.2, 'ms', 'bounded fixture',
                              evidence={'scope': self.scope, 'workload': self.workload}, verification='verified',
                              epoch=1, observation_id='measurement-' + identity)
        if provider != self.actor:
            request = 'grant-request-' + identity
            self.registry.request_grant(self.actor, identity, 'future.read', self.scope, request_id=request)
            self.registry.grant('operator', request)

    def binding(self, identity, **kwargs):
        values = {'capability_epoch': 1, 'pool_id': 'shared', 'pool_epoch': 1,
                  'dimension': 'concurrent.future', 'quantity': 1, 'unit': 'request-quantum'}
        values.update(kwargs)
        return self.bridge.bind_pool('operator', identity, **values)

    def selection(self, identity):
        return {'capability_id': identity, 'epoch': 1, 'action': 'future.read',
                'scope': copy.deepcopy(self.scope), 'workload': copy.deepcopy(self.workload),
                'observations': [{'observation_id': 'measurement-' + identity, 'metric': 'latency.future',
                                  'unit': 'ms', 'max_age_seconds': 60}]}

    def reserve(self, operation='op', plan=None, **kwargs):
        return self.bridge.reserve(self.actor, operation, self.task['id'], self.task['epoch'],
                                   self.plan if plan is None else plan, lease_seconds=40, **kwargs)

    def accept(self, operation='op', identity='target', epoch=1, receipt=None):
        return self.bridge.accept('node:cloud', operation, identity, epoch, receipt or 'receipt-' + operation + '-' + identity)

    def start(self, operation='op', identity='target', epoch=2, receipt=None):
        return self.bridge.start('node:cloud', operation, identity, epoch, receipt or 'receipt-' + operation + '-' + identity)

    def settle(self, operation='op', identity='target', epoch=3, outcome='completed', **kwargs):
        evidence = {'resource_quiescent': True, 'result_reference': 'fixture-result-reference'}
        evidence.update(kwargs)
        return self.bridge.settle('node:cloud', operation, identity, epoch,
                                  'receipt-' + operation + '-' + identity, outcome, evidence)

    def row_count(self, table):
        with self.store.transaction() as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def remaining(self):
        return self.bridge.pools(self.actor)['pools'][0]['dimensions']['concurrent.future']['remaining']

    def test_reserve_persists_pending_journal_without_child_task_or_execution(self):
        row = self.reserve()
        self.assertTrue(row['reservation_created'])
        self.assertFalse(row['execution_verified'])
        self.assertFalse(row['native_tools_intercepted'])
        self.assertEqual('reserved', row['state'])
        self.assertEqual(1, self.row_count('tasks'))
        self.assertEqual(1, self.row_count('managed_dispatch'))
        self.assertEqual(1, self.remaining())
        pending = self.bridge.pending('node:cloud')['dispatch'][0]
        self.assertTrue(pending['admission_required'])
        self.assertFalse(pending['execution_authorized'])
        self.assertEqual('future.read', pending['selection']['action'])

    def test_restart_uses_same_store_and_preserves_journal(self):
        original = self.reserve()
        store = Store(self.store.path, clock=lambda: self.now)
        restored = Allocations(store, Registry(store, trusted_verifiers=['probe:independent']))
        self.assertEqual(original['operation_id'], restored.inspect(self.actor, 'op')['operation_id'])
        self.assertEqual('reserved', restored.inspect(self.actor, 'op')['state'])
        with self.assertRaises(ValueError):
            Allocations(store, self.registry)

    def test_pool_and_binding_are_owner_only(self):
        with self.assertRaises(PermissionError):
            self.bridge.define_pool(self.actor, 'spoof', {'x': {'capacity': 3, 'unit': 'u'}})
        with self.assertRaises(PermissionError):
            self.bridge.bind_pool('node:cloud', 'target', 1, 'shared', 1, 'concurrent.future', 1, 'request-quantum')
        with self.assertRaises(PermissionError):
            self.bridge.revoke_pool(self.actor, 'shared', 1)

    def test_integer_quanta_and_exact_units(self):
        for invalid in (True, 0, -1, 1.5, MAX_QUANTA + 1):
            with self.assertRaises(ValueError):
                self.bridge.define_pool('operator', 'bad', {'x': {'capacity': invalid, 'unit': 'u'}})
            with self.assertRaises(ValueError):
                self.binding('target', quantity=invalid, expected_epoch=1)
        with self.assertRaises(Conflict):
            self.binding('target', unit='different-unit', expected_epoch=1)
        with self.assertRaises(Conflict):
            self.binding('target', quantity=3, expected_epoch=1)

    def test_pool_definition_retry_never_renews_and_updates_are_cas(self):
        first = self.bridge.pools(self.actor)['pools'][0]
        self.now += 5
        same = self.bridge.define_pool('operator', 'shared', {'concurrent.future': {'capacity': 2, 'unit': 'request-quantum'}})
        self.assertEqual(first['deadline'], same['deadline'])
        with self.assertRaises(Conflict):
            self.bridge.define_pool('operator', 'shared', {'concurrent.future': {'capacity': 3, 'unit': 'request-quantum'}})
        changed = self.bridge.define_pool('operator', 'shared', {'concurrent.future': {'capacity': 3, 'unit': 'request-quantum'}}, expected_epoch=1)
        self.assertEqual(2, changed['epoch'])
        with self.assertRaises(Conflict):
            self.reserve()
        self.binding('target', pool_epoch=2, expected_epoch=1)
        self.assertTrue(self.reserve()['reservation_created'])
        with self.assertRaises(Conflict):
            self.bridge.renew_pool('operator', 'shared', 1)

    def test_zero_new_spend_is_owner_contract_not_provider_advertisement(self):
        for cost in (True, 1, -1, 0.0):
            with self.assertRaises(PermissionError):
                self.binding('target', new_spend_minor=cost)
        self.registry.advertise('node:cloud', 'unbound', 'new.tool', spec={'extra_cost_minor': 0})
        plan = copy.deepcopy(self.plan)
        plan['target'] = plan['selections'][0]['capability_id'] = 'unbound'
        # No owner binding, grant or independent metric: advertised zero is not admission.
        with self.assertRaises((Conflict, PermissionError)):
            self.reserve(plan=plan)

    def test_operation_retry_does_not_extend_lease_or_duplicate_demand(self):
        first = self.reserve()
        self.now += 5
        second = self.reserve()
        self.assertFalse(second['reservation_created'])
        self.assertEqual(first['deadline'], second['deadline'])
        self.assertEqual(first['epoch'], second['epoch'])
        self.assertEqual(1, self.row_count('managed_allocation_demands'))
        plan = copy.deepcopy(self.plan)
        plan['selections'][0]['workload']['input_bytes'] = 65
        with self.assertRaises(Conflict):
            self.reserve(plan=plan)

    def test_actor_is_bound_to_actual_parent_node(self):
        for actor in ('operator', 'laptop', 'node:cloud', 'node:'):
            with self.assertRaises((Conflict, PermissionError)):
                self.bridge.reserve(actor, 'op', 'parent', self.task['epoch'], self.plan)
        self.assertEqual(0, self.row_count('managed_allocations'))

    def test_invalid_task_epoch_and_lost_task_deny_reserve_but_preserve_inspection(self):
        for epoch in (True, 1.0, None, 0):
            with self.assertRaises(ValueError):
                self.bridge.reserve(self.actor, 'bad', 'parent', epoch, self.plan)
        self.reserve()
        self.now += 91
        with self.assertRaises(Conflict):
            self.reserve()
        self.assertEqual('reserved', self.bridge.inspect(self.actor, 'op')['state'])
        with self.assertRaises(PermissionError):
            self.bridge.inspect('node:stranger', 'op')

    def test_same_name_new_leader_term_fences_old_parent_and_start(self):
        self.reserve()
        self.accept()
        with self.store.transaction() as db:
            db.execute('UPDATE leader SET epoch=epoch+1')
        with self.assertRaises(Conflict):
            self.start()
        with self.assertRaises(Conflict):
            self.reserve('other')
        self.assertEqual('accepted', self.bridge.inspect(self.actor, 'op')['state'])

    def test_current_capability_epoch_and_binding_epoch_are_required(self):
        self.reserve()
        self.registry.advertise('node:cloud', 'target', 'new.implementation', expected_epoch=1)
        with self.assertRaises(Conflict):
            self.accept()
        with self.assertRaises(Conflict):
            self.reserve('other')

    def test_independent_metric_must_match_scope_workload_unit_and_current_epoch(self):
        for field, value in (('scope', {'fixture': 'other'}), ('workload', {'operation': 'other'})):
            plan = copy.deepcopy(self.plan)
            plan['selections'][0][field] = value
            with self.assertRaises((Conflict, PermissionError)):
                self.reserve(plan=plan)
        for key, value in (('unit', 'seconds'), ('metric', 'different')):
            plan = copy.deepcopy(self.plan)
            plan['selections'][0]['observations'][0][key] = value
            with self.assertRaises(Conflict):
                self.reserve(plan=plan)
        with self.store.transaction() as db:
            db.execute("UPDATE capability_metrics SET capability_epoch=2 WHERE id='measurement-target'")
        with self.assertRaises(Conflict):
            self.reserve()

    def test_declared_or_self_verified_metric_never_counts(self):
        with self.store.transaction() as db:
            db.execute("UPDATE capability_metrics SET verification='declared'")
        with self.assertRaises(Conflict):
            self.reserve()
        with self.store.transaction() as db:
            db.execute("UPDATE capability_metrics SET verification='verified',actor='node:cloud'")
        with self.assertRaises(Conflict):
            self.reserve()

    def test_sample_ttl_not_receipt_age_or_future_sample(self):
        self.now += 60
        with self.assertRaises(Conflict):
            self.reserve()
        self.now = 1000
        with self.store.transaction() as db:
            db.execute('UPDATE capability_metrics SET sample_time=1001,received=1000')
        with self.assertRaises(Conflict):
            self.reserve()
        with self.store.transaction() as db:
            db.execute('UPDATE capability_metrics SET sample_time=900,received=1000')
        with self.assertRaises(Conflict):
            self.reserve()

    def test_exact_action_scope_grant_rechecked_before_accept_and_start(self):
        self.reserve()
        self.registry.revoke_grant('operator', 'grant-grant-request-target')
        with self.assertRaises(PermissionError):
            self.accept()
        self.registry.request_grant(self.actor, 'target', 'future.read', self.scope, request_id='replacement')
        self.registry.grant('operator', 'replacement')
        self.accept()
        self.registry.revoke_grant('operator', 'grant-replacement')
        with self.assertRaises(PermissionError):
            self.start()

    def test_binding_change_is_fenced_before_provider_start(self):
        self.reserve()
        self.accept()
        self.binding('target', quantity=2, expected_epoch=1)
        with self.assertRaises(Conflict):
            self.start()

    def test_two_advertisements_do_not_double_shared_pool_capacity(self):
        self.bridge.define_pool('operator', 'shared', {'concurrent.future': {'capacity': 1, 'unit': 'request-quantum'}}, expected_epoch=1)
        self.binding('target', pool_epoch=2, expected_epoch=1)
        self.capability('other')
        self.binding('other', pool_epoch=2)
        self.reserve()
        other = {'target': 'other', 'selections': [self.selection('other')], 'route': []}
        with self.assertRaises(Conflict):
            self.reserve('other-op', other)
        self.assertEqual(0, self.remaining())

    def test_owner_usage_key_deduplicates_same_use_not_independent_uses(self):
        self.bridge.define_pool('operator', 'shared', {'concurrent.future': {'capacity': 1, 'unit': 'request-quantum'}}, expected_epoch=1)
        self.binding('target', pool_epoch=2, usage_key='one-physical-use', expected_epoch=1)
        self.capability('alias')
        self.binding('alias', pool_epoch=2, usage_key='one-physical-use')
        plan = {'target': 'target', 'selections': [self.selection('target'), self.selection('alias')], 'route': []}
        self.assertEqual(1, self.reserve(plan=plan)['resolved']['demands'][0]['quantity'])
        self.bridge.cancel(self.actor, 'op', 1)
        self.binding('alias', pool_epoch=2, usage_key='independent-use', expected_epoch=1)
        with self.assertRaises(Conflict):
            self.reserve('other-op', plan)

    def test_parallel_reservations_have_one_capacity_winner(self):
        self.bridge.define_pool('operator', 'shared', {'concurrent.future': {'capacity': 1, 'unit': 'request-quantum'}}, expected_epoch=1)
        self.binding('target', pool_epoch=2, expected_epoch=1)
        barrier, results = threading.Barrier(2), []
        def reserve(identity):
            barrier.wait()
            try:
                self.reserve(identity)
                results.append('reserved')
            except Conflict:
                results.append('conflict')
        workers = [threading.Thread(target=reserve, args=(str(index),)) for index in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(['conflict', 'reserved'], sorted(results))

    def test_provider_identity_receipt_and_dispatch_cas(self):
        self.reserve()
        with self.assertRaises(PermissionError):
            self.bridge.accept(self.actor, 'op', 'target', 1, 'receipt')
        with self.assertRaises(Conflict):
            self.accept(epoch=2)
        accepted = self.accept()
        self.assertTrue(accepted['accepted_new'])
        self.assertEqual(2, accepted['epoch'])
        self.assertFalse(self.accept()['accepted_new'])
        with self.assertRaises(Conflict):
            self.accept(receipt='different')
        self.reserve('other')
        with self.assertRaises(Conflict):
            self.accept('other', receipt='receipt-op-target')

    def test_start_permission_only_once_even_after_lost_ack(self):
        self.reserve()
        self.accept()
        first = self.start()
        self.assertTrue(first['execute_once'])
        self.assertEqual(3, first['epoch'])
        self.assertFalse(self.start()['execute_once'])
        self.assertEqual(3, self.start()['epoch'])
        self.bridge.unknown('node:cloud', 'op', 'target', 3)
        self.assertFalse(self.start()['execute_once'])

    def test_accepted_running_unknown_do_not_expire_or_allow_replay(self):
        self.reserve()
        self.accept()
        self.now += 41
        self.assertEqual(0, self.bridge.expire('operator')['expired_unaccepted'])
        self.assertEqual(1, self.remaining())
        with self.assertRaises(Conflict):
            self.start()
        self.now = 1000
        self.start()
        self.bridge.unknown('node:cloud', 'op', 'target', 3)
        self.now += 41
        self.assertEqual(0, self.bridge.expire('operator')['expired_unaccepted'])
        self.assertTrue(self.bridge.inspect(self.actor, 'op')['capacity_held'])
        store = Store(self.store.path, clock=lambda: self.now)
        restarted = Allocations(store, Registry(store, trusted_verifiers=['probe:independent']))
        self.assertEqual('unknown', restarted.inspect(self.actor, 'op')['state'])
        self.assertFalse(restarted.start('node:cloud', 'op', 'target', 3, 'receipt-op-target')['execute_once'])

    def test_only_unaccepted_reservations_expire_or_cancel(self):
        self.reserve()
        self.reserve('accepted')
        self.accept('accepted')
        self.now += 41
        self.assertEqual(1, self.bridge.expire('operator')['expired_unaccepted'])
        self.assertEqual('expired', self.bridge.inspect(self.actor, 'op')['state'])
        self.assertEqual(1, self.remaining())
        with self.assertRaises(Conflict):
            self.bridge.cancel(self.actor, 'accepted', 2)
        with self.assertRaises(PermissionError):
            self.bridge.expire(self.actor)

    def test_settlement_requires_provider_quiescence_and_cas_not_model_success(self):
        self.reserve()
        self.accept()
        with self.assertRaises(Conflict):
            self.settle(epoch=2)
        self.start()
        with self.assertRaises(ValueError):
            self.settle(resource_quiescent=False)
        with self.assertRaises(Conflict):
            self.settle(epoch=2)
        with self.assertRaises(PermissionError):
            self.bridge.settle(self.actor, 'op', 'target', 3, 'receipt-op-target', 'completed',
                               {'resource_quiescent': True, 'result_reference': 'result'})
        result = self.settle()
        self.assertEqual('provider_reported', result['execution_verification'])
        self.assertEqual('completed', self.bridge.inspect(self.actor, 'op')['state'])
        self.assertFalse(self.bridge.inspect(self.actor, 'op')['execution_verified'])
        self.assertEqual(2, self.remaining())
        self.assertEqual(result, self.settle())
        with self.assertRaises(Conflict):
            self.settle(result_reference='changed-result')

    def test_late_settlement_after_revocation_and_task_loss_is_allowed(self):
        self.reserve()
        self.accept()
        self.start()
        self.registry.revoke('operator', 'target', 1)
        self.bridge.revoke_pool('operator', 'shared', 1)
        self.now += 100
        self.settle()
        self.assertFalse(self.bridge.inspect(self.actor, 'op')['capacity_held'])
        self.assertEqual(2, self.remaining())
        with self.assertRaises(Conflict):
            self.bridge.define_pool('operator', 'shared', {'concurrent.future': {'capacity': 2, 'unit': 'request-quantum'}}, expected_epoch=1)

    def test_unknown_multihop_holds_until_all_providers_quiescent(self):
        self.capability('other')
        self.binding('other')
        plan = {'target': 'target', 'selections': [self.selection('target'), self.selection('other')], 'route': []}
        self.reserve(plan=plan)
        self.accept()
        self.start()
        self.bridge.unknown('node:cloud', 'op', 'target', 3)
        self.settle(epoch=4)
        self.assertTrue(self.bridge.inspect(self.actor, 'op')['capacity_held'])
        with self.assertRaises(Conflict):
            self.accept(identity='other')
        self.bridge.decline('node:cloud', 'op', 'other', 1)
        self.assertFalse(self.bridge.inspect(self.actor, 'op')['capacity_held'])
        self.assertEqual(2, self.remaining())

    def test_decline_cannot_fake_quiescence_for_accepted_dispatch(self):
        self.reserve()
        self.accept()
        with self.assertRaises(Conflict):
            self.bridge.decline('node:cloud', 'op', 'target', 2)
        with self.assertRaises(PermissionError):
            self.bridge.decline(self.actor, 'op', 'target', 2)

    def test_pool_epoch_update_cannot_escape_unknown_held_capacity(self):
        self.reserve()
        self.accept()
        self.start()
        self.bridge.unknown('node:cloud', 'op', 'target', 3)
        with self.assertRaises(Conflict):
            self.bridge.define_pool('operator', 'shared', {'concurrent.future': {'capacity': 2, 'unit': 'other-unit'}}, expected_epoch=1)
        changed = self.bridge.define_pool('operator', 'shared', {'concurrent.future': {'capacity': 3, 'unit': 'request-quantum'}}, expected_epoch=1)
        self.assertEqual(1, changed['dimensions']['concurrent.future']['held'])
        self.assertEqual(2, changed['dimensions']['concurrent.future']['remaining'])

    def route_plan(self):
        self.capability('relay', provider=self.actor)
        self.binding('relay')
        self.registry.link(self.actor, 'relay-edge', 'relay', 'target', relation='future.reachable', epoch=1)
        self.registry.observe('probe:independent', 'relay', 'route.round_trip', 5, 'ms', 'bounded route fixture',
                              evidence={'edge_id': 'relay-edge', 'target': 'target', 'source_epoch': 1, 'target_epoch': 1,
                                        'scope': self.scope, 'workload': self.workload, 'reachable': True},
                              verification='verified', epoch=1, observation_id='route-measurement')
        return {'target': 'target', 'selections': [self.selection('relay'), self.selection('target')],
                'route': [{'edge_id': 'relay-edge', 'source_epoch': 1, 'target_epoch': 1,
                           'observation': {'observation_id': 'route-measurement', 'metric': 'route.round_trip',
                                           'unit': 'ms', 'max_age_seconds': 60}}]}

    def test_route_requires_all_hops_authorized_current_and_independently_measured(self):
        plan = self.route_plan()
        row = self.reserve(plan=plan)
        self.assertEqual(2, len(row['dispatch']))
        self.bridge.cancel(self.actor, 'op', 1)
        missing = copy.deepcopy(plan)
        missing['selections'] = [self.selection('target')]
        with self.assertRaises(Conflict):
            self.reserve('missing-hop', missing)
        with self.store.transaction() as db:
            db.execute("UPDATE capability_metrics SET verification='declared' WHERE id='route-measurement'")
        with self.assertRaises(Conflict):
            self.reserve('unverified-hop', plan)

    def test_route_target_epoch_scope_and_topology_cannot_be_forged(self):
        plan = self.route_plan()
        for mutation in ('target', 'epoch', 'duplicate'):
            changed = copy.deepcopy(plan)
            if mutation == 'target':
                changed['target'] = 'relay'
            elif mutation == 'epoch':
                changed['route'][0]['target_epoch'] = 2
            else:
                changed['route'].append(copy.deepcopy(changed['route'][0]))
            with self.assertRaises((Conflict, ValueError)):
                self.reserve(mutation, changed)
        with self.store.transaction() as db:
            db.execute("UPDATE capability_edges SET actor='node:stranger'")
        with self.assertRaises(Conflict):
            self.reserve(plan=plan)

    def test_private_metadata_rejected_not_silently_changed(self):
        plan = copy.deepcopy(self.plan)
        plan['selections'][0]['workload']['token'] = 'fixture-secret-not-credential'
        with self.assertRaises(ValueError):
            self.reserve(plan=plan)
        self.assertEqual(0, self.row_count('managed_allocations'))

    def test_metric_predicates_are_typed_unit_exact_and_not_a_fixed_rank(self):
        for operator, threshold in (('eq', 3.2), ('lte', 3.2), ('lt', 4), ('gte', 3.2), ('gt', 3)):
            plan = copy.deepcopy(self.plan)
            plan['selections'][0]['observations'][0]['predicate'] = {'op': operator, 'value': threshold}
            self.reserve(operator, plan)
            self.bridge.cancel(self.actor, operator, 1)
        for operator, threshold in (('lt', 3.2), ('eq', 9), ('gt', True), ('gte', 'fast')):
            plan = copy.deepcopy(self.plan)
            plan['selections'][0]['observations'][0]['predicate'] = {'op': operator, 'value': threshold}
            with self.assertRaises(Conflict):
                self.reserve('bad-predicate', plan)

    def test_unreachable_route_or_other_workload_does_not_prove_current_path(self):
        plan = self.route_plan()
        with self.store.transaction() as db:
            db.execute("UPDATE capability_metrics SET evidence=? WHERE id='route-measurement'",
                       ('{"edge_id":"relay-edge","target":"target","source_epoch":1,"target_epoch":1,"reachable":false}',))
        with self.assertRaises(Conflict):
            self.reserve(plan=plan)

    def test_parent_task_status_and_fence_rechecked_for_accept_and_start(self):
        self.reserve()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='paused' WHERE id='parent'")
        with self.assertRaises(Conflict):
            self.accept()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='running' WHERE id='parent'")
        self.accept()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET epoch=epoch+1 WHERE id='parent'")
        with self.assertRaises(Conflict):
            self.start()

    def test_reserve_and_admission_never_call_transaction_opening_registry_methods(self):
        with mock.patch.object(self.registry, 'authorize', side_effect=AssertionError('nested transaction')):
            self.reserve()
            self.accept()
            self.start()

    def test_audit_failure_rolls_back_allocation_and_demands(self):
        with mock.patch.object(self.bridge, '_audit', side_effect=RuntimeError('fixture audit failure')):
            with self.assertRaises(RuntimeError):
                self.reserve()
        self.assertEqual(0, self.row_count('managed_allocations'))
        self.assertEqual(0, self.row_count('managed_allocation_demands'))
        self.assertEqual(0, self.row_count('managed_dispatch'))
        self.assertEqual(2, self.remaining())
        self.reserve()
        with mock.patch.object(self.bridge, '_audit', side_effect=RuntimeError('fixture audit failure')):
            with self.assertRaises(RuntimeError):
                self.accept()
        self.assertEqual('pending', self.bridge.inspect(self.actor, 'op')['dispatch'][0]['state'])
        self.assertEqual('reserved', self.bridge.inspect(self.actor, 'op')['state'])


if __name__ == '__main__':
    unittest.main()
