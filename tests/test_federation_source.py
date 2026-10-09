import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.federation_source import FORMAT, FederationSource
from assistant_mesh.resources import Registry
from assistant_mesh.store import Conflict, Store


class FederationSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = Store(Path(self.temp.name) / 'mesh.db', clock=lambda: self.now)
        self.registry = Registry(self.store)
        self.source = FederationSource(self.registry, 'authority-local')

    def tearDown(self):
        self.temp.cleanup()

    def advertise(self, identity='cap-a', **kwargs):
        return self.registry.advertise('node:provider', identity, 'model-authored.any-kind', **kwargs)

    def test_empty_catalogue_and_exact_contract(self):
        page = self.source.export()
        self.assertEqual({'format', 'issuer', 'after', 'source_sequence', 'next_cursor',
                          'complete', 'exported_at', 'records'}, set(page))
        self.assertEqual(FORMAT, page['format'])
        self.assertEqual('authority-local', page['issuer'])
        self.assertEqual(0, page['source_sequence'])
        self.assertEqual(0, page['next_cursor'])
        self.assertTrue(page['complete'])
        self.assertEqual([], page['records'])

    def test_export_matches_current_registry_public_view(self):
        expected = self.advertise(spec={'runtime': 'mobile-native', 'throughput': {'unit': 'tokens/s'}})
        record = self.source.export()['records'][0]
        self.assertEqual({'id', 'revision', 'capability'}, set(record))
        self.assertEqual('cap-a', record['id'])
        self.assertEqual(expected, record['capability'])
        self.assertEqual(1, record['revision'])
        self.assertEqual('declared', record['capability']['verification'])

    def test_renew_same_epoch_gets_new_export_revision(self):
        self.advertise()
        self.now += 10
        renewed = self.registry.renew('node:provider', 'cap-a', 1, health='healthy')
        page = self.source.export(after=1)
        self.assertEqual(1, renewed['epoch'])
        self.assertEqual(2, page['records'][0]['revision'])
        self.assertEqual(renewed, page['records'][0]['capability'])
        self.assertEqual(2, page['next_cursor'])

    def test_revoke_same_epoch_is_explicit_tombstone(self):
        self.advertise()
        self.registry.revoke('operator', 'cap-a', 1)
        record = self.source.export(after=1)['records'][0]
        self.assertEqual(2, record['revision'])
        self.assertEqual(1, record['capability']['epoch'])
        self.assertTrue(record['capability']['revoked'])
        self.assertFalse(record['capability']['available'])
        self.assertEqual([], self.source.export(after=2)['records'])

    def test_latest_state_not_every_historical_mutation(self):
        self.advertise()
        self.registry.renew('node:provider', 'cap-a', 1)
        self.registry.revoke('operator', 'cap-a', 1)
        page = self.source.export()
        self.assertEqual(1, len(page['records']))
        self.assertEqual(3, page['records'][0]['revision'])
        self.assertTrue(page['records'][0]['capability']['revoked'])

    def test_sparse_revisions_and_complete_cursor_include_non_exported_events(self):
        self.advertise()
        self.registry.observe('node:provider', 'cap-a', 'throughput', 2, 'tokens/s',
                              'self-report', epoch=1)
        self.advertise('cap-b')
        self.registry.request_grant('remote', 'cap-a', 'run', {'project': 'p'})
        first = self.source.export(limit=1)
        self.assertEqual([1], [row['revision'] for row in first['records']])
        self.assertFalse(first['complete'])
        self.assertEqual(1, first['next_cursor'])
        self.assertEqual(4, first['source_sequence'])
        final = self.source.export(after=1, limit=1)
        self.assertEqual([3], [row['revision'] for row in final['records']])
        self.assertTrue(final['complete'])
        self.assertEqual(4, final['next_cursor'])
        self.assertNotIn('metrics', final['records'][0]['capability'])

    def test_pagination_bounded_order_and_no_absence_deletion_claim(self):
        for identity in ('cap-c', 'cap-a', 'cap-b'):
            self.advertise(identity)
        first = self.source.export(limit=2)
        self.assertEqual(['cap-c', 'cap-a'], [row['id'] for row in first['records']])
        self.assertFalse(first['complete'])
        self.assertEqual(2, first['next_cursor'])
        second = self.source.export(after=2, limit=2)
        self.assertEqual(['cap-b'], [row['id'] for row in second['records']])
        self.assertTrue(second['complete'])
        self.assertNotIn('deleted', first)

    def test_mutation_between_pages_reappears_after_previous_cursor(self):
        self.advertise('cap-a')
        self.advertise('cap-b')
        first = self.source.export(limit=1)
        self.registry.renew('node:provider', 'cap-a', 1, health='healthy')
        second = self.source.export(after=first['next_cursor'], limit=1)
        third = self.source.export(after=second['next_cursor'], limit=1)
        self.assertEqual('cap-b', second['records'][0]['id'])
        self.assertEqual('cap-a', third['records'][0]['id'])
        self.assertEqual(3, third['records'][0]['revision'])

    def test_export_snapshot_excludes_concurrent_writer_until_commit(self):
        self.advertise()
        record_entered, release_record, writer_started, writer_done = [threading.Event() for _ in range(4)]
        pages, errors = [], []
        original_record = self.source._record

        def hold_record(row, at):
            record_entered.set()
            if not release_record.wait(2):
                raise AssertionError('test record release timeout')
            return original_record(row, at)

        def export_page():
            try:
                pages.append(self.source.export())
            except BaseException as error:
                errors.append(error)

        def renew():
            writer_started.set()
            try:
                self.registry.renew('node:provider', 'cap-a', 1, health='healthy')
            except BaseException as error:
                errors.append(error)
            finally:
                writer_done.set()

        with mock.patch.object(self.source, '_record', side_effect=hold_record):
            exporter = threading.Thread(target=export_page)
            writer = threading.Thread(target=renew)
            exporter.start()
            try:
                self.assertTrue(record_entered.wait(2))
                writer.start()
                self.assertTrue(writer_started.wait(2))
                self.assertFalse(writer_done.wait(0.05))
            finally:
                release_record.set()
                exporter.join(3)
                if writer.ident is not None:
                    writer.join(3)
        self.assertFalse(exporter.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, pages[0]['source_sequence'])
        self.assertEqual('unknown', pages[0]['records'][0]['capability']['health'])
        self.assertEqual(2, self.source.export(after=1)['records'][0]['revision'])

    def test_reopen_preserves_source_revision_and_cursor(self):
        self.advertise()
        self.registry.renew('node:provider', 'cap-a', 1)
        restored = FederationSource(Registry(Store(self.store.path, clock=lambda: self.now,
                                                   recover_inflight=False)), 'authority-local')
        self.assertEqual(self.source.export(), restored.export())
        self.assertEqual([], restored.export(after=2)['records'])

    def test_metadata_credentials_and_url_secrets_are_removed(self):
        self.advertise(spec={'token': 'fixture-token', 'auth': {'clientSecret': 'fixture-secret'},
                             'endpoint': 'https://user:pass@example.invalid/api?q=private#fragment',
                             'nested': [{'Authorization': 'fixture-auth', 'safe': 'public'}]})
        page = self.source.export()
        encoded = json.dumps(page)
        for private in ('fixture-token', 'fixture-secret', 'fixture-auth', 'user:pass', 'q=private', '#fragment'):
            self.assertNotIn(private, encoded)
        spec = page['records'][0]['capability']['spec']
        self.assertEqual('https://example.invalid/api', spec['endpoint'])
        self.assertTrue(page['records'][0]['capability']['secret_fields_redacted'])

    def test_read_sanitizes_legacy_spec_without_changing_source(self):
        self.advertise()
        legacy = json.dumps({'password': 'fixture-private', 'safe': 3})
        with self.store.transaction() as db:
            db.execute('UPDATE capabilities SET spec=?,redacted=0 WHERE id=?', (legacy, 'cap-a'))
        page = self.source.export()
        self.assertEqual({'safe': 3}, page['records'][0]['capability']['spec'])
        self.assertTrue(page['records'][0]['capability']['secret_fields_redacted'])
        with self.store.transaction() as db:
            self.assertEqual(legacy, db.execute('SELECT spec FROM capabilities').fetchone()[0])

    def test_future_private_database_column_is_not_exported(self):
        self.advertise()
        with self.store.transaction() as db:
            db.execute('ALTER TABLE capabilities ADD COLUMN future_auth TEXT')
            db.execute('UPDATE capabilities SET future_auth=?', ('fixture-hidden',))
        self.assertNotIn('fixture-hidden', json.dumps(self.source.export()))
        self.assertNotIn('future_auth', self.source.export()['records'][0]['capability'])

    def test_export_leaves_all_core_tables_and_schema_unchanged(self):
        self.advertise()
        self.store.create_task('fixture task with no execution', task_id='task-fixture')
        self.registry.request_grant('remote', 'cap-a', 'run', {'project': 'p'}, request_id='request-fixture')
        self.registry.grant('operator', 'request-fixture')

        def dump():
            db = sqlite3.connect(self.store.path)
            try:
                return '\n'.join(db.iterdump())
            finally:
                db.close()

        before = dump()
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no processes')), \
                mock.patch('socket.socket', side_effect=AssertionError('no network')):
            source = FederationSource(self.registry, 'authority-local')
            for _ in range(3):
                page = source.export()
        self.assertEqual(before, dump())
        serialized = json.dumps(page)
        for forbidden in ('task-fixture', 'request-fixture', 'approved_by', 'grants', 'pools'):
            self.assertNotIn(forbidden, serialized)

    def test_export_clock_sampled_once_and_expired_state_remains_explicit(self):
        self.advertise()
        self.now += 100
        clock = mock.Mock(return_value=self.now)
        self.store.clock = clock
        page = self.source.export()
        self.assertEqual(1, clock.call_count)
        self.assertEqual(self.now, page['exported_at'])
        self.assertTrue(page['records'][0]['capability']['lease_expired'])
        self.assertFalse(page['records'][0]['capability']['available'])
        self.assertFalse(page['records'][0]['capability']['revoked'])

    def test_same_revision_only_dynamic_availability_changes_with_clock(self):
        self.advertise()
        first = self.source.export()['records'][0]
        self.now += 100
        second = self.source.export()['records'][0]
        self.assertEqual(first['revision'], second['revision'])
        for record in (first, second):
            record['capability'].pop('available')
            record['capability'].pop('lease_expired')
        self.assertEqual(first, second)

    def test_after_ahead_of_head_rejected_without_implicit_reset(self):
        self.advertise()
        with self.assertRaisesRegex(Conflict, 'cursor_ahead'):
            self.source.export(after=2)
        self.assertEqual(1, self.source.export(after=1)['next_cursor'])

    def test_invalid_cursor_limit_issuer_and_clock(self):
        for after in (True, -1, 1.0, '0', None, 2**63):
            with self.subTest(after=after), self.assertRaises(ValueError):
                self.source.export(after=after)
        for limit in (True, 0, -1, 1001, 1.0, '1', None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.source.export(limit=limit)
        for issuer in ('', None, 'a\n', '\ud800', 'x' * 201):
            with self.subTest(issuer=repr(issuer)), self.assertRaises(ValueError):
                FederationSource(self.registry, issuer)
        for at in (True, -1, float('nan'), float('inf'), '1000'):
            self.store.clock = lambda: at
            with self.subTest(clock=repr(at)), self.assertRaises(ValueError):
                self.source.export()
        with self.assertRaises(ValueError):
            FederationSource(self.store, 'authority-local')

    def test_missing_source_revision_fails_closed(self):
        self.advertise()
        with self.store.transaction() as db:
            db.execute('DELETE FROM capability_audit')
        with self.assertRaisesRegex(Conflict, 'revision_missing'):
            self.source.export()

    def test_byte_bounded_page_stops_at_last_emitted_revision(self):
        for identity in ('cap-a', 'cap-b', 'cap-c'):
            self.advertise(identity, description='x' * 100)
        single = self.source.export(limit=1)
        size = len(json.dumps(single, sort_keys=True, separators=(',', ':'),
                              ensure_ascii=True, allow_nan=False).encode('utf8'))
        with mock.patch('assistant_mesh.federation_source.MAX_PAGE_BYTES', size + 32):
            first = self.source.export(limit=100)
            self.assertEqual(1, len(first['records']))
            self.assertFalse(first['complete'])
            self.assertEqual(1, first['next_cursor'])
            self.assertLessEqual(len(json.dumps(first, sort_keys=True, separators=(',', ':')).encode()), size + 32)
            second = self.source.export(after=first['next_cursor'])
            self.assertEqual('cap-b', second['records'][0]['id'])

    def test_oversized_single_record_or_impossible_page_does_not_skip(self):
        self.advertise()
        with mock.patch('assistant_mesh.federation_source.MAX_RECORD_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'record_too_large'):
                self.source.export()
        with mock.patch('assistant_mesh.federation_source.MAX_PAGE_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'page_too_small'):
                self.source.export()
        self.assertEqual('cap-a', self.source.export()['records'][0]['id'])


if __name__ == '__main__':
    unittest.main()
