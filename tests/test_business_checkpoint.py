import json
from pathlib import Path
import tempfile
import unittest

from assistant_mesh.business_checkpoint import (
    CheckpointMismatch, CheckpointPosition, compare_checkpoints, replica_checkpoint, source_checkpoint,
)
from assistant_mesh.business_journal import CORE, Journal, canonical, cell
from assistant_mesh.business_replica import Replica
from assistant_mesh.store import Store


class BusinessCheckpointTests(unittest.TestCase):
    def test_published_typed_rows_at_one_position(self):
        with tempfile.TemporaryDirectory(prefix='business-checkpoint.') as directory:
            root = Path(directory)
            store = Store(root / 'source ledger.db', recover_inflight=False)
            blob = bytes(range(256)) * 4096
            with store.transaction() as db:
                db.execute('CREATE TABLE typed_partitions(part TEXT NOT NULL COLLATE NOCASE, '
                           'identity BLOB NOT NULL, body BLOB, PRIMARY KEY(part,identity))')
                db.executemany('INSERT INTO typed_partitions VALUES(?,?,?)', (
                    ('X', b'1', blob), ('x', '1', 'body'), ('X', 1.25, 2.5), ('x', 1, None),
                    ('deleted', 1, b'tombstone'),
                ))
                db.executemany('INSERT INTO meta VALUES(?,?)', (
                    ('business_goal', 'included'), ('owner_context', 'excluded'),
                ))
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            covered = set(CORE) | {'meta', 'typed_partitions'}
            journal = Journal(store, dict(authority='source', tables=['typed_partitions'],
                meta_keys=['business_goal'], excluded={table: 'fixture exclusion' for table in tables - covered
                                                      if not table.startswith('sqlite_')}))
            journal.install()
            try:
                self._compare(journal, store, Replica(root / 'replica with #.db'), blob)
            finally:
                journal.close()

    def _compare(self, journal, store, replica, blob):
        owner = ('agent_peer', 'disposable-replica')
        authority = journal.status()['authority']
        start = journal.snapshot_begin(owner)
        self._finish_snapshot(journal, replica, owner, start, 0)

        with store.transaction() as db:
            db.execute("DELETE FROM typed_partitions WHERE part='deleted'")
            db.execute("UPDATE meta SET value='still excluded' WHERE key='owner_context'")
        current = journal.status()
        expected = CheckpointPosition(authority, current['incarnation'], current['generation'],
                                      current['manifest']['sha256'], current['head'])
        with self.assertRaisesRegex(CheckpointMismatch, 'position_conflict'):
            replica_checkpoint(replica, expected)
        replica.apply(journal.export(start['baseline_cursor'], expected.incarnation, expected.generation), authority)
        source = source_checkpoint(journal, expected)
        passive = replica_checkpoint(replica, expected)
        evidence = compare_checkpoints(source, passive)
        self.assertEqual(dict(authority=authority, incarnation=expected.incarnation, generation=expected.generation,
            manifest_sha256=expected.manifest_sha256, sealed_journal_head=expected.head,
            typed_rows_sha256=source.typed_rows_sha256), evidence)
        self.assertEqual(4, dict(passive.table_counts)['typed_partitions'])
        self.assertEqual(1, dict(passive.table_counts)['meta'])
        self.assertTrue(any(row['tombstone'] for row in replica.query(authority, expected.incarnation,
                                                                     'typed_partitions', tombstones=True)['rows']))

        fresh = journal.snapshot_begin(owner)
        page = journal.snapshot_page(owner, fresh['snapshot_id'], 0, limit=1)
        replica.apply(page, authority)
        with self.assertRaisesRegex(CheckpointMismatch, 'source_snapshot_open'):
            source_checkpoint(journal, expected)
        with self.assertRaisesRegex(CheckpointMismatch, 'replica_snapshot_open'):
            replica_checkpoint(replica, expected)
        self._finish_snapshot(journal, replica, owner, fresh, page['cursor'])
        self.assertEqual(evidence, compare_checkpoints(source, replica_checkpoint(replica, expected)))

        # Compare disk bytes independently of the unchanged journal and manifest position.
        key = canonical([cell('X'), cell(b'1')])
        with replica.transaction() as db:
            stored = db.execute('SELECT payload FROM replica_rows WHERE row_key=?', (key,)).fetchone()[0]
            altered = json.loads(stored)
            altered['row'][2] = cell(blob[:-1] + b'\x00')
            db.execute('UPDATE replica_rows SET payload=? WHERE row_key=?', (canonical(altered), key))
        with self.assertRaisesRegex(CheckpointMismatch, 'typed_rows_conflict'):
            compare_checkpoints(source, replica_checkpoint(replica, expected))

        with store.transaction() as db:
            db.execute('CREATE TABLE undeclared_business(id TEXT PRIMARY KEY)')
        with self.assertRaisesRegex(CheckpointMismatch, 'incomplete_coverage'):
            source_checkpoint(journal, expected)

    @staticmethod
    def _finish_snapshot(journal, replica, owner, start, after):
        while True:
            page = journal.snapshot_page(owner, start['snapshot_id'], after, limit=2)
            replica.apply(page, start['authority'])
            if page['done']:
                journal.snapshot_end(owner, start['snapshot_id'])
                return
            after = page['cursor']
