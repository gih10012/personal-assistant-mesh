import copy
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.allocations import Allocations
from assistant_mesh.federation_projection import FORMAT, Projection
from assistant_mesh.resources import Registry
from assistant_mesh.store import Conflict, Store


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Deliberately different wall clocks: projected leases are relative.
        self.now, self.remote_now = 8000.0, 1000.0
        self.store = Store(Path(self.temp.name) / 'local.db', clock=lambda: self.now)
        self.remote_store = Store(Path(self.temp.name) / 'remote.db', clock=lambda: self.remote_now)
        self.registry = Registry(self.remote_store)
        self.projection = Projection(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def capability(self, identity='cap-a', **kwargs):
        return self.registry.advertise('node:provider', identity, 'open.future.kind',
                                       spec={'actions': ['arbitrary.tool'], 'units': 'tokens/s'},
                                       **kwargs)

    def page(self, records=(), after=0, head=None, next_cursor=None, complete=True,
             issuer='authority:remote', exported_at=None):
        records = [{'id': cap['id'], 'revision': rev, 'capability': copy.deepcopy(cap)}
                   for rev, cap in records]
        head = max([r['revision'] for r in records] + [after]) if head is None else head
        return {'format': FORMAT, 'issuer': issuer, 'after': after, 'source_sequence': head,
                'next_cursor': head if next_cursor is None else next_cursor,
                'complete': complete, 'exported_at': self.remote_now if exported_at is None else exported_at,
                'records': records}

    def apply(self, page):
        return self.projection.apply(page['issuer'], page)

    def projected(self, issuer=None):
        return self.projection.discover(issuer=issuer, include_unavailable=True)['capabilities']

    def assert_query_only(self, value):
        for key in ('managed_invocation_authorized', 'local_registry_imported', 'global_consensus_verified'):
            self.assertIs(value[key], False)

    def test_actual_sqlite_detached_open_kind_relative_clock_and_false_authority(self):
        cap = self.capability()
        page = self.page([(1, cap)])
        applied = self.apply(page)
        self.assertEqual(1, applied['cursor'])
        self.assertEqual(1, applied['applied'])
        self.assert_query_only(applied)
        page['records'][0]['capability']['kind'] = 'mutated.caller.object'
        result = self.projection.discover()
        self.assert_query_only(result)
        row = result['capabilities'][0]
        self.assertEqual('open.future.kind', row['capability']['kind'])
        self.assertEqual(8090, row['local_deadline_estimate'])
        self.assertTrue(row['available'])
        self.assert_query_only(row)
        self.assertEqual('declared_estimate', row['freshness_verification'])
        self.assertFalse(row['source_clock_verified'])
        self.assertFalse(row['transport_delay_accounted'])

    def test_same_name_and_revision_are_isolated_by_issuer(self):
        cap = self.capability()
        self.apply(self.page([(1, cap)], issuer='issuer:a'))
        other = copy.deepcopy(cap)
        other['principal'] = 'other:provider'
        other['description'] = 'Other owner, not the same resource.'
        self.apply(self.page([(1, other)], issuer='issuer:b'))
        rows = self.projected()
        self.assertEqual(['issuer:a', 'issuer:b'], [r['issuer'] for r in rows])
        self.assertEqual('node:provider', self.projected('issuer:a')[0]['capability']['principal'])
        self.assertEqual('other:provider', self.projected('issuer:b')[0]['capability']['principal'])
        self.assertEqual(1, len(self.projection.discover(issuer='issuer:b')['capabilities']))

    def test_issuer_is_fixed_by_caller_not_capability_principal(self):
        cap = self.capability()
        page = self.page([(1, cap)])
        with self.assertRaisesRegex(PermissionError, 'projection_issuer_mismatch'):
            self.projection.apply('authority:enrolled', page)
        self.assertEqual([], self.projected())
        self.assertEqual(0, self.projection.status('authority:enrolled')['cursor'])

    def test_renewal_and_revoke_same_epoch_use_distinct_revisions(self):
        first = self.capability()
        self.apply(self.page([(2, first)], head=2))
        self.remote_now += 5
        self.now += 5
        renewed = self.registry.renew('node:provider', first['id'], 1, lease_seconds=150, health='healthy')
        self.apply(self.page([(8, renewed)], after=2, head=9))
        row = self.projected()[0]
        self.assertEqual(8, row['revision'])
        self.assertEqual(1, row['capability']['epoch'])
        self.assertEqual(8155, row['local_deadline_estimate'])
        revoked = self.registry.revoke('operator', first['id'], 1)
        self.apply(self.page([(12, revoked)], after=9, head=14))
        self.assertEqual([], self.projection.discover()['capabilities'])
        self.assertTrue(self.projected()[0]['capability']['revoked'])
        self.assertEqual(1, self.projected()[0]['capability']['epoch'])
        self.assertEqual(14, self.projection.status('authority:remote')['cursor'])

    def test_sparse_paginated_latest_state_and_absence_never_deletes(self):
        first, second = self.capability('a'), self.capability('b')
        self.apply(self.page([(4, first)], head=20, next_cursor=4, complete=False))
        self.assertEqual(4, self.projection.status('authority:remote')['cursor'])
        with self.assertRaisesRegex(Conflict, 'projection_cursor_mismatch'):
            self.apply(self.page([], after=10, head=20))
        self.apply(self.page([(17, second)], after=4, head=20))
        self.assertEqual(['a', 'b'], [r['id'] for r in self.projected()])
        self.apply(self.page([], after=20, head=33))
        self.assertEqual(['a', 'b'], [r['id'] for r in self.projected()])
        self.assertEqual(33, self.projection.status('authority:remote')['cursor'])

    def test_source_head_rollback_rejected_even_above_consumed_cursor(self):
        cap = self.capability()
        self.apply(self.page([(2, cap)], head=100, next_cursor=2, complete=False))
        with self.assertRaisesRegex(Conflict, 'projection_source_rollback'):
            self.apply(self.page([], after=2, head=99))
        self.assertEqual(2, self.projection.status('authority:remote')['cursor'])
        self.assertEqual(100, self.projection.status('authority:remote')['source_sequence'])

    def test_lost_reply_cold_restart_is_idempotent_without_lease_or_age_refresh(self):
        page = self.page([(1, self.capability())])
        self.apply(page)
        self.now += 20
        self.projection = Projection(Store(self.store.path, clock=lambda: self.now, recover_inflight=False))
        duplicate = self.apply(page)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(0, duplicate['applied'])
        row = self.projected()[0]
        self.assertEqual(8000, row['received_at'])
        self.assertEqual(8090, row['local_deadline_estimate'])
        self.assertEqual(8000, self.projection.status('authority:remote')['received_at'])
        self.now = 8031
        self.assertEqual([], self.projection.discover()['capabilities'])

    def test_derived_freshness_changes_do_not_conflict_or_extend_same_revision(self):
        cap = self.capability()
        self.apply(self.page([(1, cap)]))
        self.remote_now = 1100
        self.now = 8010
        expired = self.registry.describe(cap['id'])
        expired.pop('metrics')
        duplicate = self.apply(self.page([(1, expired)]))
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(8010, self.projected()[0]['local_deadline_estimate'])
        self.assertEqual([], self.projection.discover()['capabilities'])

    def test_same_revision_different_fact_and_id_conflict(self):
        page = self.page([(1, self.capability())])
        self.apply(page)
        for key, value in (('description', 'changed'), ('deadline', 1200), ('epoch', 2),
                           ('principal', 'spoofed:provider'), ('spec', {'new': True})):
            changed = copy.deepcopy(page)
            changed['records'][0]['capability'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(Conflict, 'projection_revision_content_conflict'):
                self.apply(changed)
        changed = copy.deepcopy(page)
        changed['records'][0]['id'] = changed['records'][0]['capability']['id'] = 'other-cap'
        with self.assertRaisesRegex(Conflict, 'projection_revision_content_conflict'):
            self.apply(changed)
        self.assertEqual('cap-a', self.projected()[0]['id'])

    def test_old_revision_never_overwrites_new_announcement_or_tombstone(self):
        first = self.capability()
        original = self.page([(1, first)])
        self.apply(original)
        self.remote_now += 1
        updated = self.registry.advertise('node:provider', first['id'], 'new.kind', expected_epoch=1)
        self.apply(self.page([(2, updated)], after=1))
        with self.assertRaisesRegex(Conflict, 'projection_source_rollback'):
            self.apply(original)
        self.assertEqual('new.kind', self.projected()[0]['capability']['kind'])
        revoked = self.registry.revoke('operator', first['id'], 2)
        self.apply(self.page([(3, revoked)], after=2))
        with self.assertRaises(Conflict):
            self.apply(original)
        self.assertTrue(self.projected()[0]['capability']['revoked'])

    def test_higher_revision_cannot_resurrect_tombstone_or_change_principal_or_lower_epoch(self):
        cap = self.capability()
        self.apply(self.page([(1, cap)]))
        for key, value, category in (('principal', 'other:provider', 'projection_principal_conflict'),
                                     ('epoch', 0, 'invalid_projection_sequence')):
            changed = copy.deepcopy(cap)
            changed[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, category):
                self.apply(self.page([(2, changed)], after=1))
        changed = copy.deepcopy(cap)
        changed['epoch'] = 2
        self.apply(self.page([(2, changed)], after=1))
        with self.assertRaisesRegex(Conflict, 'projection_epoch_rollback'):
            self.apply(self.page([(3, cap)], after=2))
        revoked = copy.deepcopy(changed)
        revoked['revoked'], revoked['available'] = True, False
        self.apply(self.page([(3, revoked)], after=2))
        with self.assertRaisesRegex(Conflict, 'projection_tombstone_conflict'):
            self.apply(self.page([(4, changed)], after=3))
        self.assertEqual(3, self.projection.status('authority:remote')['cursor'])

    def test_connection_observations_expiry_and_no_capability_renewal(self):
        self.apply(self.page([(1, self.capability())]))
        self.projection.observe_connection('authority:remote', False)
        self.assertEqual([], self.projection.discover()['capabilities'])
        self.assertEqual('source_disconnected', self.projected()[0]['unavailable_reason'])
        self.projection.observe_connection('authority:remote', True, lease_seconds=200)
        self.assertEqual(1, len(self.projection.discover()['capabilities']))
        self.now = 8090
        self.assertEqual([], self.projection.discover()['capabilities'])
        self.assertEqual('lease_expired_estimate', self.projected()[0]['unavailable_reason'])
        self.assertEqual(8090, self.projected()[0]['local_deadline_estimate'])

    def test_duplicate_does_not_undo_explicit_disconnect(self):
        page = self.page([(1, self.capability())])
        self.apply(page)
        self.projection.observe_connection('authority:remote', False)
        self.apply(page)
        self.assertEqual('disconnected', self.projection.status('authority:remote')['connection_state'])
        self.assertEqual([], self.projection.discover()['capabilities'])

    def test_expired_source_and_failed_health_remain_unavailable_candidates(self):
        first = self.capability('a')
        failed = self.registry.renew('node:provider', 'a', 1, health='failed')
        self.apply(self.page([(1, failed)]))
        self.assertEqual([], self.projection.discover()['capabilities'])
        self.assertEqual('source_health_unavailable', self.projected()[0]['unavailable_reason'])
        self.remote_now = 1100
        expired = self.registry.describe(first['id'])
        expired.pop('metrics')
        self.apply(self.page([(2, expired)], after=1))
        self.assertEqual(8000, self.projected()[0]['local_deadline_estimate'])

    def test_transaction_failure_rolls_back_all_records_receipts_and_cursor(self):
        a, b = self.capability('a'), self.capability('b')
        with self.store.transaction() as db:
            db.execute('''CREATE TRIGGER fail_second_projection BEFORE INSERT ON federation_projection_capabilities
                WHEN NEW.id='b' BEGIN SELECT RAISE(ABORT,'injected failure'); END''')
        page = self.page([(2, a), (5, b)], head=9)
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(page)
        self.assertEqual([], self.projected())
        self.assertEqual(0, self.projection.status('authority:remote')['cursor'])
        with self.store.transaction() as db:
            for table in ('federation_projection_pages', 'federation_projection_revisions', 'federation_projection_sources'):
                self.assertEqual(0, db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0])
            db.execute('DROP TRIGGER fail_second_projection')
        self.assertEqual(2, self.apply(page)['applied'])

    def test_invalid_later_record_cannot_partially_commit_earlier_record(self):
        page = self.page([(1, self.capability('a')), (2, self.capability('b'))])
        page['records'][1]['capability']['spec']['password'] = 'must-not-store'
        with self.assertRaisesRegex(ValueError, 'unsafe_projection_metadata'):
            self.apply(page)
        self.assertEqual([], self.projected())
        self.assertEqual(0, self.projection.status('authority:remote')['cursor'])

    def test_later_conflict_rolls_back_an_existing_sources_first_updated_record(self):
        a, b = self.capability('a'), self.capability('b')
        self.apply(self.page([(1, a), (2, b)]))
        updated_a, updated_b = copy.deepcopy(a), copy.deepcopy(b)
        updated_a['epoch'] = 2
        updated_a['spec'] = {'version': 2}
        updated_b['principal'] = 'unauthorized:replacement'
        with self.assertRaisesRegex(Conflict, 'projection_principal_conflict'):
            self.apply(self.page([(3, updated_a), (4, updated_b)], after=2))
        rows = self.projected()
        self.assertEqual([1, 2], [r['revision'] for r in rows])
        self.assertEqual(1, rows[0]['capability']['epoch'])
        self.assertEqual(2, self.projection.status('authority:remote')['cursor'])
        with self.store.transaction() as db:
            self.assertEqual(2, db.execute('SELECT COUNT(*) FROM federation_projection_revisions').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM federation_projection_pages').fetchone()[0])

    def test_actual_federation_source_pages_round_trip_renew_and_revoke(self):
        from assistant_mesh.federation_source import FederationSource

        source = FederationSource(self.registry, 'authority:remote')
        initial = source.export()
        self.apply(initial)
        cap = self.capability('a')
        self.capability('b')
        self.registry.observe('node:provider', 'a', 'throughput', 10, 'tokens/s', 'self', epoch=1)
        first = source.export(after=0, limit=1)
        self.assertEqual(8, len(first))
        self.assertEqual(16, len(first['records'][0]['capability']))
        self.assertFalse(first['complete'])
        self.apply(first)
        last = source.export(after=first['next_cursor'], limit=1)
        self.apply(last)
        self.assertEqual(last['source_sequence'], self.projection.status('authority:remote')['cursor'])
        self.assertEqual(['a', 'b'], [r['id'] for r in self.projected()])
        self.remote_now += 3
        self.now += 3
        renewed = self.registry.renew('node:provider', 'a', cap['epoch'], health='healthy', lease_seconds=150)
        after = self.projection.status('authority:remote')['cursor']
        renewal_page = source.export(after=after)
        self.apply(renewal_page)
        self.assertEqual(1, renewed['epoch'])
        self.assertEqual(8153, self.projected()[0]['local_deadline_estimate'])
        self.registry.revoke('operator', 'a', 1)
        after = self.projection.status('authority:remote')['cursor']
        self.apply(source.export(after=after))
        self.assertTrue(self.projected()[0]['capability']['revoked'])
        self.assertEqual(['b'], [r['id'] for r in self.projection.discover()['capabilities']])
        # No changed announcements: keep both the missing live row and tombstone.
        after = self.projection.status('authority:remote')['cursor']
        self.apply(source.export(after=after))
        self.assertEqual(['a', 'b'], [r['id'] for r in self.projected()])

    def test_concurrent_duplicate_from_distinct_connections_commits_once(self):
        page = self.page([(3, self.capability())], head=5)
        results, errors = [], []
        barrier = threading.Barrier(6)

        def worker():
            try:
                store = Store(self.store.path, clock=lambda: self.now, recover_inflight=False)
                projection = Projection(store)
                barrier.wait(timeout=5)
                results.append(projection.apply('authority:remote', page))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual([], errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(6, len(results))
        self.assertEqual(1, sum(r['applied'] for r in results))
        self.assertEqual(5, sum(r['duplicate'] for r in results))
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM federation_projection_pages').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM federation_projection_revisions').fetchone()[0])

    def test_no_local_registry_grant_pool_task_or_native_effect_mutation(self):
        local_registry = Registry(self.store)
        Allocations(self.store, local_registry)
        local_registry.advertise('local:provider', 'local-cap', 'local.kind')
        self.store.create_task('Already local task', task_id='local-task')
        tables = ('capabilities', 'capability_grants', 'capability_grant_requests', 'capability_metrics',
                  'capability_audit', 'resource_pools', 'managed_allocations', 'managed_dispatch', 'tasks', 'nodes')

        def snapshot():
            with self.store.transaction() as db:
                return {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)] for table in tables}

        before = snapshot()
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no execution')):
            self.apply(self.page([(1, self.capability())]))
            self.projection.discover()
            self.projection.observe_connection('authority:remote', False)
        self.assertEqual(before, snapshot())
        self.assertEqual(['local-cap'], [r['id'] for r in local_registry.discover()['capabilities']])

    def test_strict_page_schema_types_cursor_order_bounds_and_metadata(self):
        valid = self.page([(1, self.capability())])
        invalid = []
        for key, value in (('format', 'other/1'), ('after', True), ('source_sequence', -1),
                           ('next_cursor', 2), ('complete', 1), ('exported_at', float('nan')),
                           ('records', {})):
            page = copy.deepcopy(valid)
            page[key] = value
            invalid.append(page)
        page = copy.deepcopy(valid)
        page['unexpected'] = 'not an extension point'
        invalid.append(page)
        page = copy.deepcopy(valid)
        page['records'] *= 1001
        invalid.append(page)
        page = copy.deepcopy(valid)
        page['records'][0]['capability']['description'] = 'x' * 8001
        invalid.append(page)
        page = copy.deepcopy(valid)
        page['records'][0]['capability']['spec'] = {'endpoint': 'https://name:secret@host/path?token=x'}
        invalid.append(page)
        page = copy.deepcopy(valid)
        page['records'][0]['capability']['secret_fields_redacted'] = 1
        invalid.append(page)
        page = copy.deepcopy(valid)
        page['records'][0]['capability']['available'] = False
        invalid.append(page)
        page = copy.deepcopy(valid)
        page['records'][0]['capability']['verification'] = 'verified'
        invalid.append(page)
        page = copy.deepcopy(valid)
        page['records'][0]['capability']['spec'] = {1: 'not a JSON key'}
        invalid.append(page)
        page = self.page([], head=3, next_cursor=0, complete=False)
        invalid.append(page)
        page = self.page([(1, self.capability('z'))], head=3, next_cursor=2, complete=False)
        invalid.append(page)
        for page in invalid:
            with self.subTest(page_key=next(iter(page))), self.assertRaises(ValueError):
                self.apply(page)
        self.assertEqual([], self.projected())

    def test_record_and_total_page_size_bounds_precede_writes(self):
        valid = self.page([(1, self.capability())])
        with mock.patch('assistant_mesh.federation_projection.MAX_RECORD_BYTES', 100):
            with self.assertRaisesRegex(ValueError, 'projection_record_too_large'):
                self.apply(valid)
        with mock.patch('assistant_mesh.federation_projection.MAX_PAGE_BYTES', 100):
            with self.assertRaisesRegex(ValueError, 'projection_page_too_large'):
                self.apply(valid)
        self.assertEqual(0, self.projection.status('authority:remote')['cursor'])

    def test_discovery_filters_live_candidates_before_limit_and_validates_inputs(self):
        a, b = self.capability('a'), self.capability('b')
        a['revoked'], a['available'] = True, False
        self.apply(self.page([(1, a), (2, b)]))
        self.assertEqual(['b'], [r['id'] for r in self.projection.discover(limit=1)['capabilities']])
        self.assertEqual([], self.projection.discover(kind='no.such.kind')['capabilities'])
        for options in ({'limit': True}, {'limit': 0}, {'limit': 1001}, {'include_unavailable': 1}, {'issuer': ''}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.projection.discover(**options)
        with self.assertRaisesRegex(ValueError, 'projection_source_not_found'):
            self.projection.observe_connection('unknown:issuer', True)
        for args in (('authority:remote', 1, 30), ('authority:remote', True, 0),
                     ('authority:remote', True, float('inf'))):
            with self.assertRaises(ValueError):
                self.projection.observe_connection(*args)

    def test_cold_empty_source_status_does_not_invent_identity_or_global_authority(self):
        status = self.projection.status('unknown:authority')
        self.assertEqual('unknown', status['connection_state'])
        self.assertEqual(0, status['cursor'])
        self.assert_query_only(status)
        self.assertEqual([], self.projected())


if __name__ == '__main__':
    unittest.main()
