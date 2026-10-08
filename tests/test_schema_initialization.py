"""Real SQLite constructor/migration concurrency; no services or models.

Temporary local databases exercise rollback and independent thread/process
connections, not deployed-host acceptance or native execution behavior.
"""
import contextlib
import json
import multiprocessing
import sqlite3
import tempfile
import threading
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.allocations import Allocations
from assistant_mesh.networking import Network
from assistant_mesh.resources import Registry
from assistant_mesh.store import Store


def initialize_bundle(path, index, rounds=2):
    for _ in range(rounds):
        store = Store(path, recover_inflight=False)
        # Different service initialization orders share one authority database.
        if index % 2:
            Network(store, 'fixture-node-' + str(index))
        registry = Registry(store)
        Allocations(store, registry)
        if not index % 2:
            Network(store, 'fixture-node-' + str(index))
        with store.transaction() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                       ('constructor:' + str(index), json.dumps('finished')))


def process_initialize(path, index, barrier, results):
    try:
        barrier.wait(timeout=15)
        initialize_bundle(path, index)
        results.put((index, None))
    except BaseException as error:
        results.put((index, traceback.format_exc()))


class SchemaInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-schema-fixture.')
        self.root = Path(self.temporary.name)
        self.path = self.root / 'ledger.sqlite'

    def tearDown(self):
        self.temporary.cleanup()

    @contextlib.contextmanager
    def connection(self):
        with contextlib.closing(sqlite3.connect(str(self.path))) as db:
            yield db

    def legacy(self):
        # Statements are explicit here as well: fixtures do not hide an
        # executescript transaction boundary from the code being tested.
        with self.connection() as db:
            statements = (
                '''CREATE TABLE tasks(id TEXT PRIMARY KEY,parent_id TEXT,input TEXT NOT NULL,
                    required TEXT NOT NULL,status TEXT NOT NULL,node TEXT,
                    epoch INTEGER NOT NULL DEFAULT 0,deadline REAL,
                    checkpoint TEXT NOT NULL DEFAULT '{}',result TEXT,
                    created REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0)''',
                '''CREATE TABLE outbox(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,
                    body TEXT NOT NULL,status TEXT NOT NULL,client_id TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',created REAL NOT NULL,context_version TEXT)''',
                '''CREATE TABLE sessions(scope TEXT PRIMARY KEY,node TEXT NOT NULL,harness TEXT NOT NULL,
                    state TEXT NOT NULL,artifact TEXT,updated REAL NOT NULL)''',
                '''CREATE TABLE steering(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,text TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending')''',
            )
            for statement in statements:
                db.execute(statement)
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('legacy-task', None, 'preserve original task', '["leader"]', 'running',
                 'legacy-node', 9, 2000.0, '{"side_effect_started":true,"thread_id":"legacy-thread"}',
                 'preserve original result', 1000.0, 3))
            db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?)',
                ('legacy-send', 'fingerprint', '{"text":"fixture"}', 'submitting',
                 'stable-client-id', '{"transport":"fixture"}', 1000.0, 'v1'))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)',
                ('leader:owner', 'legacy-node', 'codex', '{"thread_id":"legacy-thread"}', 'legacy-artifact', 1000.0))
            db.execute('INSERT INTO steering VALUES(?,?,?,?,?)',
                ('legacy-steer', 'legacy-task', 9, 'preserve original steer', 'submitting'))
            db.commit()

    def snapshot(self):
        with self.connection() as db:
            schema = list(db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master '
                                     "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
            data = {row[1]: list(db.execute('SELECT * FROM "' + row[1] + '" ORDER BY rowid'))
                    for row in schema if row[0] == 'table'}
            return schema, data

    @contextlib.contextmanager
    def fail_statement(self, target, message='fixture_schema_failure'):
        original_connect = sqlite3.connect
        connections = []

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.strip() == target:
                    raise sqlite3.OperationalError(message)
                return sqlite3.Connection.execute(self, sql, *args, **kwargs)

            def executescript(self, *args, **kwargs):
                raise AssertionError('constructor must not use executescript')

        def connect(*args, **kwargs):
            kwargs['factory'] = FailingConnection
            db = original_connect(*args, **kwargs)
            connections.append(db)
            return db

        with patch('assistant_mesh.store.sqlite3.connect', side_effect=connect):
            yield connections

    def assert_closed(self, connections):
        self.assertTrue(connections)
        for db in connections:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
                db.execute('SELECT 1')

    def assert_complete_schema(self, count):
        with self.connection() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'meta', 'tasks', 'outbox', 'leader', 'native_sessions', 'mesh_links',
                'mesh_messages', 'mesh_deliveries', 'mesh_executions', 'mesh_reports',
                'capabilities', 'capability_metrics', 'capability_grants', 'resource_pools',
                'managed_allocations', 'managed_dispatch'} <= tables)
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM leader').fetchone()[0])
            columns = [row[1] for row in db.execute('PRAGMA table_info(tasks)')]
            for column in ('context', 'scope', 'paused_status', 'paused_deadline', 'leader_epoch'):
                self.assertEqual(1, columns.count(column))
            self.assertEqual(count, db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'constructor:%'").fetchone()[0])
            self.assertEqual('ok', db.execute('PRAGMA integrity_check').fetchone()[0])

    def test_store_mid_migration_failure_rolls_back_all_ddl_and_legacy_data(self):
        self.legacy()
        before = self.snapshot()
        with self.fail_statement('ALTER TABLE tasks ADD COLUMN leader_epoch INTEGER') as connections:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture_schema_failure'):
                Store(self.path)
        self.assert_closed(connections)
        self.assertEqual(before, self.snapshot())

    def test_store_late_recovery_failure_rolls_back_schema_session_copy_and_outbox_recovery(self):
        self.legacy()
        before = self.snapshot()
        with self.fail_statement("UPDATE steering SET state='unknown' WHERE state='submitting'") as connections:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture_schema_failure'):
                Store(self.path)
        self.assert_closed(connections)
        self.assertEqual(before, self.snapshot())

    def test_failed_fresh_creation_has_no_partial_schema(self):
        with self.fail_statement('CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL)') as connections:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture_schema_failure'):
                Store(self.path)
        self.assert_closed(connections)
        self.assertEqual(([], {}), self.snapshot())

    def test_legacy_migration_preserves_unknown_leader_term_history_and_recovery_semantics(self):
        self.legacy()
        Store(self.path)
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            task = dict(db.execute('SELECT * FROM tasks').fetchone())
            self.assertIsNone(task['leader_epoch'])
            self.assertEqual(9, task['epoch'])
            self.assertEqual(3, task['attempts'])
            self.assertEqual('running', task['status'])
            self.assertEqual('preserve original task', task['input'])
            self.assertEqual('preserve original result', task['result'])
            self.assertTrue(json.loads(task['checkpoint'])['side_effect_started'])
            self.assertEqual('legacy-thread', json.loads(task['checkpoint'])['thread_id'])
            self.assertEqual('leader:owner', task['scope'])
            self.assertEqual({}, json.loads(task['context']))
            self.assertEqual('unknown', db.execute('SELECT status FROM outbox').fetchone()[0])
            self.assertEqual('unknown', db.execute('SELECT state FROM steering').fetchone()[0])
            self.assertEqual(list(db.execute('SELECT * FROM sessions')), list(db.execute('SELECT * FROM native_sessions')))
        migrated = self.snapshot()
        Store(self.path)
        self.assertEqual(migrated, self.snapshot())

    def test_companion_nonrecovery_constructor_does_not_reclassify_inflight_operations(self):
        self.legacy()
        Store(self.path, recover_inflight=False)
        with self.connection() as db:
            self.assertEqual('submitting', db.execute('SELECT status FROM outbox').fetchone()[0])
            self.assertEqual('submitting', db.execute('SELECT state FROM steering').fetchone()[0])

    def test_network_ddl_failure_rolls_back_its_entire_schema_without_touching_store(self):
        store = Store(self.path)
        before = self.snapshot()
        statement = '''CREATE TABLE IF NOT EXISTS mesh_reports(
                    sender TEXT NOT NULL, report_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, body TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(sender,report_id))'''
        with self.fail_statement(statement) as connections:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture_schema_failure'):
                Network(store, 'fixture-node')
        self.assert_closed(connections)
        self.assertEqual(before, self.snapshot())
        Network(store, 'fixture-node')
        with self.connection() as db:
            self.assertEqual(5, db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'mesh_%'").fetchone()[0])

    def test_transaction_setup_failures_close_actual_connections_before_yield(self):
        for statement in ('PRAGMA busy_timeout=0', 'PRAGMA busy_timeout=10000', 'PRAGMA journal_mode=WAL', 'BEGIN IMMEDIATE'):
            with self.subTest(statement=statement), self.fail_statement(statement) as connections:
                store = Store.__new__(Store)
                store.path = str(self.path)
                with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture_schema_failure'):
                    with store.transaction():
                        self.fail('failed setup must not yield a connection')
            self.assert_closed(connections)

    def test_transient_wal_setup_is_retried_but_transaction_body_runs_once(self):
        original_connect = sqlite3.connect
        attempts, connections, bodies = [], [], []
        class TransientConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == 'PRAGMA journal_mode=WAL':
                    attempts.append(sql)
                    if len(attempts) <= 2:
                        raise sqlite3.OperationalError('database is locked')
                return sqlite3.Connection.execute(self, sql, *args, **kwargs)
        def connect(*args, **kwargs):
            kwargs['factory'] = TransientConnection
            connection = original_connect(*args, **kwargs)
            connections.append(connection)
            return connection
        store = Store.__new__(Store)
        store.path = str(self.path)
        with patch('assistant_mesh.store.sqlite3.connect', side_effect=connect), \
                patch('assistant_mesh.store.time.sleep') as sleep:
            with store.transaction() as db:
                bodies.append('one-business-transaction')
                db.execute('CREATE TABLE business(id INTEGER PRIMARY KEY)')
                db.execute('INSERT INTO business VALUES(1)')
        self.assertEqual(3, len(attempts))
        self.assertEqual(2, sleep.call_count)
        self.assertEqual(['one-business-transaction'], bodies)
        self.assert_closed(connections)
        with self.connection() as db:
            self.assertEqual([(1,)], list(db.execute('SELECT * FROM business')))

    def test_wal_retry_deadline_fails_closed_and_closes_without_yielding(self):
        with self.fail_statement('PRAGMA journal_mode=WAL', 'database is locked') as connections, \
                patch('assistant_mesh.store.time.monotonic', side_effect=[0, 0, 11]), \
                patch('assistant_mesh.store.time.sleep') as sleep:
            store = Store.__new__(Store)
            store.path = str(self.path)
            with self.assertRaisesRegex(sqlite3.OperationalError, '^database is locked$'):
                with store.transaction():
                    self.fail('timed-out setup must not yield a transaction')
        sleep.assert_called_once_with(0.05)
        self.assert_closed(connections)
        self.assertEqual(([], {}), self.snapshot())

    def test_business_sql_lock_error_rolls_back_without_replaying_body(self):
        store = Store(self.path)
        before = self.snapshot()
        statement = "INSERT INTO meta VALUES('fixture-key','true')"
        bodies = []
        with self.fail_statement(statement, 'database is locked') as connections, \
                patch('assistant_mesh.store.time.sleep') as sleep:
            with self.assertRaisesRegex(sqlite3.OperationalError, '^database is locked$'):
                with store.transaction() as db:
                    bodies.append('one-business-transaction')
                    db.execute('CREATE TABLE partial_business(id INTEGER)')
                    db.execute(statement)
        self.assertEqual(['one-business-transaction'], bodies)
        sleep.assert_not_called()
        self.assert_closed(connections)
        self.assertEqual(before, self.snapshot())

    def thread_constructors(self, legacy=False):
        if legacy:
            self.legacy()
        count, failures, lock = 8, [], threading.Lock()
        barrier = threading.Barrier(count)
        def initialize(index):
            try:
                barrier.wait(timeout=15)
                initialize_bundle(str(self.path), index)
            except BaseException as error:
                with lock:
                    failures.append((index, type(error).__name__, str(error)))
        threads = [threading.Thread(target=initialize, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertTrue(all(not thread.is_alive() for thread in threads), 'constructor thread did not finish')
        self.assertEqual([], failures)
        self.assert_complete_schema(count)
        if legacy:
            with self.connection() as db:
                self.assertEqual((9, None), db.execute('SELECT epoch,leader_epoch FROM tasks').fetchone())

    def test_threads_concurrently_initialize_fresh_authority_network_registry_allocations(self):
        self.thread_constructors()

    def test_threads_concurrently_migrate_legacy_authority_network_registry_allocations(self):
        self.thread_constructors(legacy=True)

    def process_constructors(self, legacy=False):
        if legacy:
            self.legacy()
        count = 6
        context = multiprocessing.get_context('spawn')
        barrier, results = context.Barrier(count), context.Queue()
        processes = [context.Process(target=process_initialize,
            args=(str(self.path), index, barrier, results)) for index in range(count)]
        try:
            for process in processes:
                process.start()
            observations = [results.get(timeout=20) for _ in processes]
            self.assertEqual([], [observation for observation in observations if observation[1] is not None])
            for process in processes:
                process.join(10)
                self.assertEqual(0, process.exitcode)
            self.assert_complete_schema(count)
        finally:
            # Only the explicitly created local fixture child processes.
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()
            results.join_thread()

    def test_processes_concurrently_initialize_fresh_authority_network_registry_allocations(self):
        self.process_constructors()

    def test_processes_concurrently_migrate_legacy_authority_network_registry_allocations(self):
        self.process_constructors(legacy=True)


if __name__ == '__main__':
    unittest.main()
