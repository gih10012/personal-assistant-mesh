"""One owner handoff fixture, real SQLite files/connections, no live source."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from assistant_mesh import business_writer_seal as writer_seal
from assistant_mesh.business_journal import CORE, Journal, PREFIX, canonical, digest, quote
from assistant_mesh.store import Store


class BusinessWriterSealTests(unittest.TestCase):
    def test_locked_durable_original_seal_and_fail_closed_inspection(self):
        with tempfile.TemporaryDirectory(prefix='business-writer-seal.') as temp:
            root = Path(temp)
            path = root / 'source.db'
            store = Store(path, recover_inflight=False)
            unknown = {'operation_id': 'original-native-op', 'thread_id': 'original-thread',
                       'side_effect_started': True, 'outcome': 'unknown'}
            store.create_task('Original business input', task_id='original')
            with store.transaction() as db:
                db.execute("UPDATE tasks SET status='needs_review',checkpoint=? WHERE id='original'", (canonical(unknown),))
                db.execute('INSERT INTO task_continuations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           ('continuation', 'fingerprint', 'original', 1, '{}', 'input', '{}', 'result', '{}', '{}', 1))
                db.execute('INSERT INTO interactions VALUES(?,?,?,?,?,?,?)', ('question', 'original', 1, 'question', '{}', None, 1))
                db.execute('INSERT INTO steering VALUES(?,?,?,?,?)', ('steering', 'original', 1, 'owner steering', 'unknown'))
                db.execute('INSERT INTO agent_actions VALUES(?,?,?)', ('action', 'fingerprint', '{"outcome":"unknown"}'))
                for table in ('sessions', 'native_sessions'):
                    db.execute('INSERT INTO ' + table + ' VALUES(?,?,?,?,?,?)',
                               ('scope', 'source', 'codex', canonical(unknown), 'artifact', 1))
                db.execute('INSERT INTO session_chunks VALUES(?,?,?)', ('artifact', 0, b'original\x00history'))
                db.execute('INSERT INTO memories VALUES(?,?,?,?)', ('memory', 'scope', 'Original memory', 1))
                db.execute('CREATE TABLE business_dependencies(id TEXT PRIMARY KEY,receipt BLOB UNIQUE)')
                db.execute('INSERT INTO business_dependencies VALUES(?,?)', ('dependency', b'original-receipt'))
                db.execute('INSERT INTO meta VALUES(?,?)', ('business_goal', '{"original":"goal"}'))
                db.execute('INSERT INTO meta VALUES(?,?)', ('local_operational', '{}'))
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            config = dict(authority='source-authority', tables=['business_dependencies'], meta_keys=['business_goal'],
                          excluded={table: 'Fixture local operational state' for table in tables - set(CORE) - {'business_dependencies', 'meta'}})
            journal = Journal(store, config)
            journal.install()
            initial = writer_seal.inspect_writer(path)
            self.assertFalse(initial['sealed'])
            self.assertEqual([], initial['coverage_issues'])
            request = dict(format=writer_seal.REQUEST, operation_id='original-transfer-op', source_holder='source-holder',
                **{key: initial['journal'][key] for key in ('authority', 'incarnation', 'generation', 'manifest_sha256', 'meta_keys')})

            def copy_database(name):
                destination = root / (name + '.db')
                with sqlite3.connect(path) as source, sqlite3.connect(destination) as target:
                    source.backup(target)
                return destination

            # A status based on trigger names alone is insufficient. Refuse a
            # missing or replaced capture, schema coverage gaps and unsafe meta
            # replacement semantics without creating any seal artifacts.
            mutations = [
                ('missing-capture', 'DROP TRIGGER ' + quote(PREFIX + 'tasks_update'), 'coverage_incomplete'),
                ('replaced-capture', 'DROP TRIGGER ' + quote(PREFIX + 'tasks_update'), 'trigger_mismatch'),
                ('uncovered-table', 'CREATE TABLE uncovered(id TEXT PRIMARY KEY)', 'coverage_incomplete'),
                ('meta-unique', 'CREATE UNIQUE INDEX meta_value_unique ON meta(value)', 'meta_replace_coverage_required'),
                ('manifest', 'UPDATE ' + PREFIX + "state SET manifest='{}'", 'manifest_mismatch'),
            ]
            for label, mutation, error in mutations:
                damaged = copy_database(label)
                with sqlite3.connect(damaged) as db:
                    db.execute(mutation)
                    if label == 'replaced-capture':
                        db.execute('CREATE TRIGGER ' + quote(PREFIX + 'tasks_update') + ' AFTER UPDATE ON tasks BEGIN SELECT 1; END')
                with self.assertRaisesRegex(ValueError, error):
                    writer_seal.seal_writer(damaged, request)
                status = writer_seal.inspect_writer(damaged)
                self.assertFalse(status['valid'])
                self.assertFalse(status['sealed'])
                self.assertTrue(status['coverage_issues'])

            # An already admitted writer can finish. The seal's BEGIN IMMEDIATE
            # waits for it, and binds the resulting same-generation final head.
            incumbent = sqlite3.connect(path, isolation_level=None)
            self.addCleanup(incumbent.close)
            incumbent.execute('PRAGMA recursive_triggers=ON')
            incumbent.execute('BEGIN IMMEDIATE')
            incumbent.execute("UPDATE tasks SET result='last admitted result' WHERE id='original'")
            admitted_head = incumbent.execute('SELECT MAX(sequence) FROM ' + PREFIX + 'events').fetchone()[0]
            attempting, finished = threading.Event(), threading.Event()
            results, errors = [], []
            connect = writer_seal._connect

            def traced_connect(*args, **kwargs):
                db = connect(*args, **kwargs)
                db.set_trace_callback(lambda sql: attempting.set() if sql == 'BEGIN IMMEDIATE' else None)
                return db

            def owner_seal():
                try:
                    results.append(writer_seal.seal_writer(path, request))
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()

            with patch.object(writer_seal, '_connect', traced_connect):
                owner = threading.Thread(target=owner_seal, daemon=True)
                owner.start()
                self.assertTrue(attempting.wait(3))
                self.assertFalse(finished.is_set())
                incumbent.commit()
                owner.join(12)
            self.assertFalse(owner.is_alive())
            self.assertEqual([], errors)
            receipt = results[0]
            self.assertEqual(admitted_head, receipt['sealed_journal_head'])
            self.assertGreater(admitted_head, initial['journal']['head'])
            self.assertEqual(request['generation'], receipt['generation'])
            self.assertEqual(receipt['sha256'], digest({key: value for key, value in receipt.items() if key != 'sha256'}))

            # mode=ro inspection and retry perform no source data commit, and
            # keep exactly the original immutable operation and receipt.
            version = incumbent.execute('PRAGMA data_version').fetchone()[0]
            inspected = writer_seal.inspect_writer(path)
            self.assertTrue(inspected['valid'])
            self.assertEqual(receipt, inspected['receipt'])
            self.assertEqual(receipt['sha256'], inspected['source_seal_sha256'])
            self.assertEqual(receipt, writer_seal.seal_writer(path, request))
            self.assertEqual(version, incumbent.execute('PRAGMA data_version').fetchone()[0])
            for change in ({'operation_id': 'replacement-op'}, {'source_holder': 'replacement-holder'}):
                with self.assertRaisesRegex(ValueError, 'original_operation_conflict'):
                    writer_seal.seal_writer(path, dict(request, **change))
            readonly = writer_seal._connect(path, readonly=True)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                    readonly.execute("INSERT INTO meta VALUES('inspection-must-not-write','{}')")
            finally:
                readonly.close()

            # Both incumbent and restarted unhooked connections are fenced,
            # including INSERT OR REPLACE with recursive_triggers disabled.
            incumbent.execute('PRAGMA recursive_triggers=OFF')
            protected = receipt['covered_tables'] + [PREFIX + 'state', PREFIX + 'events', PREFIX + 'cells', writer_seal.STATE]
            for table in protected:
                key = incumbent.execute('PRAGMA table_info(' + quote(table) + ')').fetchone()[1]
                statements = [
                    'INSERT OR REPLACE INTO ' + quote(table) + ' SELECT * FROM ' + quote(table) + ' LIMIT 1',
                    'UPDATE ' + quote(table) + ' SET ' + quote(key) + '=' + quote(key),
                    'DELETE FROM ' + quote(table),
                ]
                for statement in statements:
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'business_writer_sealed'):
                        incumbent.execute(statement)
            for statement in (
                "UPDATE meta SET key='local-other' WHERE key='business_goal'",
                "UPDATE meta SET key='business_goal' WHERE key='local_operational'",
                "INSERT OR REPLACE INTO meta(rowid,key,value) SELECT rowid,'unselected-replacement','{}' FROM meta WHERE key='business_goal'",
                "UPDATE OR REPLACE meta SET rowid=(SELECT rowid FROM meta WHERE key='business_goal') WHERE key='local_operational'",
                "INSERT OR REPLACE INTO business_dependencies VALUES('replacement',X'6f726967696e616c2d72656365697074')",
            ):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'business_writer_sealed'):
                    incumbent.execute(statement)
            incumbent.execute("UPDATE meta SET value='{}' WHERE key='local_operational'")
            incumbent.execute("INSERT INTO inbox VALUES('local-inbox','{}',1)")
            restarted = sqlite3.connect(path)
            try:
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'business_writer_sealed'):
                    restarted.execute("UPDATE tasks SET result='late old process' WHERE id='original'")
                restarted.rollback()
                row = restarted.execute("SELECT checkpoint,status,result FROM tasks WHERE id='original'").fetchone()
                self.assertEqual((unknown, 'needs_review', 'last admitted result'), (json.loads(row[0]), row[1], row[2]))
                self.assertEqual(canonical(receipt), restarted.execute('SELECT receipt FROM ' + writer_seal.STATE).fetchone()[0])
            finally:
                restarted.close()
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'business_writer_sealed'):
                Store(path, recover_inflight=True)
            self.assertEqual(receipt, writer_seal.inspect_writer(path)['receipt'])
            self.assertEqual(admitted_head, journal.status()['head'])

            # A trusted SQL administrator can remove triggers. That is an
            # explicit boundary, and the next inspection/retry fails closed.
            tampered = copy_database('admin-tamper')
            with sqlite3.connect(tampered) as db:
                db.execute('DROP TRIGGER ' + quote(writer_seal.TRIGGERS + 'tasks_update'))
            invalid = writer_seal.inspect_writer(tampered)
            self.assertFalse(invalid['valid'])
            self.assertIsNone(invalid['source_seal_sha256'])
            with self.assertRaisesRegex(ValueError, 'trigger_mismatch'):
                writer_seal.seal_writer(tampered, request)
            self.assertFalse(inspected['ha_qualified'])
            self.assertFalse(inspected['external_effects_fenced'])


if __name__ == '__main__':
    unittest.main()
