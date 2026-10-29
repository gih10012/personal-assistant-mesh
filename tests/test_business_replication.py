"""Four bounded SQLite/dispatch fixtures, no live authorities or model calls."""
import copy
import json
import sqlite3
from pathlib import Path
import tempfile
import types
import unittest

from assistant_mesh.business_journal import CORE, Journal, PREFIX, canonical, digest, seal, hook_store_type
from assistant_mesh.business_replica import Replica, ReplicaConflict
from assistant_mesh.business_replica_api import attach
from assistant_mesh.store import Store


class BusinessReplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='business-replication.')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'authority.db', recover_inflight=False)
        self.store.create_task('Complete original input', task_id='original', context={
            'project_id': 'synthetic', 'depends_on': ['prior-business-id'], 'nested': {'full': [1, 'two']}})
        self.store.create_task('Child input', task_id='child', parent_id='original')
        self.unknown = {'thread_id': 'original-thread', 'turn_id': 'original-turn', 'operation_id': 'original-op',
            'side_effect_started': True, 'runtime_failure': {'native_start_attempted': True, 'outcome': 'unknown'}}
        self.blob = bytes(range(256)) * 4096
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET checkpoint=?,result=?,status='needs_review' WHERE id='original'",
                       (canonical(self.unknown), 'Full original result'))
            db.execute('INSERT INTO interactions VALUES(?,?,?,?,?,?,?)',
                       ('question', 'original', 7, 'question', '{"choices":["yes","no"]}', 'owner answer', 10))
            db.execute('INSERT INTO steering VALUES(?,?,?,?,?)', ('steer', 'original', 7, 'Original steering', 'unknown'))
            db.execute('INSERT INTO agent_actions VALUES(?,?,?)', ('action', 'original-fingerprint', '{"receipt":"full"}'))
            db.execute('INSERT INTO task_continuations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('continuation', 'fingerprint', 'original', 6, '{"request":"original"}', 'old input', '{}', 'old result', '{}', '{"ok":true}', 12))
            for table in ('sessions', 'native_sessions'):
                db.execute('INSERT INTO ' + table + ' VALUES(?,?,?,?,?,?)',
                           ('original-scope', 'original-node', 'codex', canonical(self.unknown), 'artifact', 11))
            db.execute('INSERT INTO session_chunks VALUES(?,?,?)', ('artifact', 0, self.blob))
            db.execute('INSERT INTO memories VALUES(?,?,?,?)', ('memory', 'project:synthetic', 'Full business memory', 11))
            db.execute('CREATE TABLE business_dependencies(parent TEXT NOT NULL,child TEXT NOT NULL,receipt BLOB,revoked INTEGER,PRIMARY KEY(parent,child))')
            db.execute('INSERT INTO business_dependencies VALUES(?,?,?,?)', ('original', 'child', b'\x00\xffreceipt', 1))
            db.execute('INSERT INTO meta VALUES(?,?)', ('business_goal', '{"goal":"full objective"}'))
            db.execute('INSERT INTO meta VALUES(?,?)', ('owner_context', '{"token":"DO_NOT_COPY"}'))
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        covered = set(CORE) | {'business_dependencies', 'meta'}
        self.config = dict(authority='source-authority', tables=['business_dependencies'], meta_keys=['business_goal'],
            excluded={name: 'Explicit fixture operational/channel exclusion' for name in names - covered})
        self.journal = Journal(self.store, self.config)
        self.journal.install()
        self.replica = Replica(self.root / 'passive-replica.db')
        self.owner = ('agent_peer', 'replica-node')

    def baseline(self, journal=None):
        journal = journal or self.journal
        start = journal.snapshot_begin(self.owner)
        cursor = 0
        pages = []
        while True:
            page = journal.snapshot_page(self.owner, start['snapshot_id'], cursor, limit=3)
            self.replica.apply(page, 'source-authority')
            pages.append(page)
            cursor = page['cursor']
            if page['done']:
                break
        journal.snapshot_end(self.owner, start['snapshot_id'])
        return start, pages

    def changes(self):
        status = self.journal.status()
        state = self.replica.state('source-authority', status['incarnation'])
        return self.journal.export(state['cursor'], status['incarnation'], status['generation'])

    def test_business_writes_and_changes_roll_back_together_and_replica_refuses_store(self):
        before = self.journal.status()['head']
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET status='completed',checkpoint='{}' WHERE id='original'")
                self.assertGreater(db.execute('SELECT COUNT(*) FROM ' + PREFIX + 'events').fetchone()[0], 0)
                raise RuntimeError('rollback')
        self.assertEqual(before, self.journal.status()['head'])

        # A raw unhooked connection cannot silently omit replacement deletes.
        db = sqlite3.connect(self.store.path)
        try:
            db.execute('PRAGMA recursive_triggers=OFF')
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'writer_hook_required'):
                db.execute("UPDATE tasks SET result='unlogged' WHERE id='original'")
            db.rollback()
        finally:
            db.close()
        with self.store.transaction() as db:
            row = db.execute("SELECT status,checkpoint FROM tasks WHERE id='original'").fetchone()
            self.assertEqual(('needs_review', self.unknown), (row[0], json.loads(row[1])))
        with self.assertRaisesRegex(ValueError, 'refuses_existing_authority'):
            Replica(self.store.path)
        self.assertEqual(before, self.journal.status()['head'])

    def test_persisted_capture_allows_original_constructor_writes_after_restart(self):
        class RestartedStore(Store):
            pass

        with self.store.transaction() as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)',
                       ('restart-session', 'node', 'codex', '{}', None, 20))
        before = self.journal.status()['head']
        hook_store_type(RestartedStore)
        method = RestartedStore.transaction
        hook_store_type(RestartedStore)
        self.assertIs(method, RestartedStore.transaction)
        restarted = RestartedStore(self.store.path, recover_inflight=False)
        with restarted.transaction() as db:
            self.assertEqual(1, db.execute('PRAGMA recursive_triggers').fetchone()[0])
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM native_sessions WHERE scope='restart-session'").fetchone())
            self.assertEqual(self.unknown, json.loads(db.execute(
                "SELECT checkpoint FROM tasks WHERE id='original'").fetchone()[0]))
            self.assertGreater(db.execute('SELECT MAX(sequence) FROM ' + PREFIX + 'events').fetchone()[0], before)

    def test_consistent_streamed_full_rows_blob_and_incremental_unknown_with_scoped_export(self):
        api = types.SimpleNamespace(store=self.store, dispatch=lambda *args: 'original-dispatch')
        journal = attach(api, self.config)
        reader = {'role': 'agent_peer', 'node': 'replica-node', 'capabilities': ['business.replica.read']}
        dedicated = dict(reader, role='replica_reader')
        self.assertFalse(api.dispatch('POST', '/v1/business-replica/status', {}, dedicated)['execution_authorized'])
        for route in ('/v1/tasks', '/v1/mesh/send', '/v1/phone/owner/plan'):
            with self.assertRaises(PermissionError):
                api.dispatch('POST', route, {}, dedicated)
        for peer in ({'role': 'viewer'}, {'role': 'worker', 'node': 'node', 'capabilities': ['agent']}):
            with self.assertRaises(PermissionError):
                api.dispatch('POST', '/v1/business-replica/status', {}, peer)
        with self.assertRaises(PermissionError):
            api.dispatch('POST', '/v1/business-replica/refresh', {}, reader)
        start = api.dispatch('POST', '/v1/business-replica/snapshot', {'action': 'begin'}, reader)
        with self.store.transaction() as db:
            # Snapshot remains at its original cursor while writes proceed.
            db.execute("UPDATE tasks SET status='paused' WHERE id='original'")
            db.execute('INSERT INTO memories VALUES(?,?,?,?)', ('after-snapshot', 'project:synthetic', 'New memory', 13))
        cursor = 0
        while True:
            page = api.dispatch('POST', '/v1/business-replica/snapshot',
                dict(action='page', snapshot_id=start['snapshot_id'], after=cursor, limit=2), reader)
            self.replica.apply(page, 'source-authority')
            cursor = page['cursor']
            if page['done']:
                break
        authority, incarnation = start['authority'], start['incarnation']
        rows = self.replica.query(authority, incarnation, 'tasks')['rows']
        original = next(row['value'] for row in rows if row['value']['id'] == 'original')
        self.assertEqual(('needs_review', 'Complete original input', 'Full original result'),
                         (original['status'], original['input'], original['result']))
        self.assertEqual(self.unknown, json.loads(original['checkpoint']))
        self.assertEqual('original', next(row['value']['parent_id'] for row in rows if row['value']['id'] == 'child'))
        self.assertEqual(['prior-business-id'], json.loads(original['context'])['depends_on'])
        self.assertEqual(self.blob, self.replica.query(authority, incarnation, 'session_chunks')['rows'][0]['value']['body'])
        self.assertEqual({'business_goal'}, {row['value']['key'] for row in self.replica.query(authority, incarnation, 'meta')['rows']})
        for table in CORE + ('business_dependencies',):
            self.assertGreater(len(self.replica.query(authority, incarnation, table)['rows']), 0)
        page = api.dispatch('POST', '/v1/business-replica/export',
            dict(after=start['baseline_cursor'], incarnation=incarnation, generation=start['generation']), reader)
        self.assertNotIn('session_chunks', [item['table'] for item in page['records']])
        self.replica.apply(page, authority)
        original = next(row['value'] for row in self.replica.query(authority, incarnation, 'tasks')['rows'] if row['value']['id'] == 'original')
        self.assertEqual(('paused', self.unknown), (original['status'], json.loads(original['checkpoint'])))
        self.assertFalse(self.replica.state(authority, incarnation)['execution_authorized'])
        self.assertEqual('original-dispatch', api.dispatch('POST', '/unchanged', {}, reader))
        journal.snapshot_end(self.owner, start['snapshot_id'])

    def test_replay_conflicts_gaps_deletion_tombstones_and_source_incarnation_do_not_overwrite_unknown(self):
        start, _ = self.baseline()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET result='new receipt' WHERE id='original'")
        page = self.changes()
        self.replica.apply(page, 'source-authority')
        self.replica.apply(page, 'source-authority')
        forged = copy.deepcopy(page)
        forged['records'][-1]['row'][2] = ['text', 'changed original input']
        forged.pop('sha256')
        seal(forged)
        with self.assertRaises(ReplicaConflict):
            self.replica.apply(forged, 'source-authority')
        with self.store.transaction() as db:
            db.execute('DELETE FROM business_dependencies')
        deleted = self.changes()
        self.replica.apply(deleted, 'source-authority')
        incarnation = start['incarnation']
        self.assertEqual([], self.replica.query('source-authority', incarnation, 'business_dependencies')['rows'])
        tombstone = self.replica.query('source-authority', incarnation, 'business_dependencies', tombstones=True)['rows'][0]
        self.assertTrue(tombstone['tombstone'])
        self.assertEqual(b'\x00\xffreceipt', tombstone['value']['receipt'])
        # Older authentic frame is idempotent after the newer delete.
        self.replica.apply(page, 'source-authority')
        changed_source = copy.deepcopy(deleted)
        changed_source['incarnation'] = 'different-incarnation'
        changed_source.pop('sha256'); seal(changed_source)
        with self.assertRaises(ReplicaConflict):
            self.replica.apply(changed_source, 'source-authority')
        gap = copy.deepcopy(deleted)
        gap.update(after=deleted['cursor'], cursor=deleted['cursor'] + 2, head=deleted['cursor'] + 2)
        gap['records'][0]['sequence'] = gap['cursor']
        gap.pop('sha256'); seal(gap)
        with self.assertRaises(ReplicaConflict):
            self.replica.apply(gap, 'source-authority')
        original = next(row['value'] for row in self.replica.query('source-authority', incarnation, 'tasks')['rows'] if row['value']['id'] == 'original')
        self.assertEqual(self.unknown, json.loads(original['checkpoint']))
        with self.replica.transaction() as db:
            self.assertGreaterEqual(db.execute('SELECT COUNT(*) FROM replica_conflicts').fetchone()[0], 2)
        with self.store.transaction() as db:
            db.execute('CREATE TABLE replace_receipts(id TEXT PRIMARY KEY,unique_receipt TEXT UNIQUE)')
            db.execute("INSERT INTO replace_receipts VALUES('old','same-receipt')")
        desired = copy.deepcopy(self.config)
        desired['tables'].append('replace_receipts')
        self.journal.refresh(desired)
        self.baseline()
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO replace_receipts VALUES('new','same-receipt')")
        self.replica.apply(self.changes(), 'source-authority')
        rows = self.replica.query('source-authority', incarnation, 'replace_receipts', tombstones=True)['rows']
        self.assertEqual({'old': True, 'new': False}, {row['value']['id']: row['tombstone'] for row in rows})

    def test_schema_drift_is_degraded_until_explicit_refresh_and_new_full_baseline(self):
        start, _ = self.baseline()
        with self.store.transaction() as db:
            db.execute('ALTER TABLE tasks ADD COLUMN new_business_field TEXT')
            db.execute("UPDATE tasks SET new_business_field='new complete value' WHERE id='original'")
            db.execute('CREATE TABLE new_business_extension(id TEXT PRIMARY KEY,body BLOB)')
            db.execute('INSERT INTO new_business_extension VALUES(?,?)', ('receipt', b'full-new-bytes'))
        status = self.journal.status()
        self.assertIn('schema_refresh_required:tasks', status['coverage_issues'])
        self.assertIn('uncovered_table:new_business_extension', status['coverage_issues'])
        desired = copy.deepcopy(self.config)
        desired['tables'].append('new_business_extension')
        refreshed = self.journal.refresh(desired)
        self.assertTrue(refreshed['bootstrap_required'])
        self.assertEqual([], self.journal.status()['coverage_issues'])
        self.baseline()
        original = next(row['value'] for row in self.replica.query('source-authority', start['incarnation'], 'tasks')['rows'] if row['value']['id'] == 'original')
        self.assertEqual('new complete value', original['new_business_field'])
        self.assertEqual(self.unknown, json.loads(original['checkpoint']))
        self.assertEqual(b'full-new-bytes', self.replica.query('source-authority', start['incarnation'], 'new_business_extension')['rows'][0]['value']['body'])


if __name__ == '__main__':
    unittest.main()
