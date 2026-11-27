"""Independent SQLite witnesses for the typed rows at an exact journal position.

The caller supplies a position from its trusted authority chain. These reads
cover the declared business ledger. Provider attestations, writer fencing and
external dependency verification belong to the owner transfer protocol.
"""
import contextlib
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .business_journal import FORMAT, canonical, cell, digest, quote, row_key
from .business_replica import Replica


class CheckpointMismatch(ValueError):
    pass


@dataclass(frozen=True)
class CheckpointPosition:
    authority: str
    incarnation: str
    generation: int
    manifest_sha256: str
    head: int

    def __post_init__(self):
        if (not isinstance(self.authority, str) or not self.authority
                or not isinstance(self.incarnation, str) or not self.incarnation
                or type(self.generation) is not int or self.generation < 1
                or type(self.head) is not int or self.head < 0
                or not isinstance(self.manifest_sha256, str)
                or not re.fullmatch(r'[0-9a-f]{64}', self.manifest_sha256)):
            raise CheckpointMismatch('business_checkpoint_invalid_position')


@dataclass(frozen=True)
class CheckpointWitness:
    kind: str
    position: CheckpointPosition
    typed_rows_sha256: str
    table_counts: tuple[tuple[str, int], ...]


@contextlib.contextmanager
def _read_snapshot(path):
    uri = Path(path).absolute().as_uri() + '?mode=ro'
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db


def _checked_manifest(state, expected):
    manifest = dict(state['manifest'])
    manifest_sha256 = manifest.pop('sha256', None)
    if (manifest_sha256 != expected.manifest_sha256 or digest(manifest) != manifest_sha256
            or state['authority'] != expected.authority or state['incarnation'] != expected.incarnation
            or state['generation'] != expected.generation or state['head'] != expected.head):
        raise CheckpointMismatch('business_checkpoint_position_conflict')
    if state['coverage_issues'] != []:
        raise CheckpointMismatch('business_checkpoint_incomplete_coverage')
    return manifest


def _typed_witness(kind, position, manifest, table_rows):
    sha256 = hashlib.sha256(b'amita-business-typed-rows/1\x00')
    counts = []
    for table, info in sorted(manifest['tables'].items()):
        # SQLite's native key order can depend on affinity and collation.
        # Sort fixed-size fingerprints of canonical typed keys and full rows.
        rows = sorted(bytes.fromhex(digest([key, values]))
                      for key, values in table_rows(table, info))
        counts.append((table, len(rows)))
        sha256.update(canonical(counts[-1]).encode('ascii') + b'\x00')
        for row in rows:
            sha256.update(row)
    return CheckpointWitness(kind, position, sha256.hexdigest(), tuple(counts))


def _sqlite_rows(db, table, info, condition='1'):
    columns = ','.join(quote(column['name']) for column in info['columns'])
    for row in db.execute('SELECT ' + columns + ' FROM ' + quote(table) + ' WHERE ' + condition):
        values = [cell(value) for value in row]
        yield row_key(info, values), values


def source_checkpoint(journal, expected):
    """Measure the source on a pinned read transaction while this Journal is idle."""
    with journal.lock:
        journal._cleanup()
        if journal.snapshots:
            raise CheckpointMismatch('business_checkpoint_source_snapshot_open')
        with _read_snapshot(journal.store.path) as db:
            state = journal._description(db)
            manifest = _checked_manifest(state, expected)
            if (not 0 <= state['generation_start'] <= expected.head
                    or not 0 <= state['retained_after'] <= expected.head):
                raise CheckpointMismatch('business_checkpoint_source_generation_conflict')
            return _typed_witness('source', expected, manifest, lambda table, info: _sqlite_rows(
                db, table, info, journal._filter(table, quote(table), db, manifest['meta_keys'])))


def _replica_rows(db, position, table, info):
    columns = [column['name'] for column in info['columns']]
    for row in db.execute('SELECT row_key,payload,sequence FROM replica_rows '
                          'WHERE authority=? AND incarnation=? AND table_name=? AND tombstone=0',
                          (position.authority, position.incarnation, table)):
        try:
            payload = json.loads(row['payload'])
            values = payload['row']
            payload_columns = payload['columns']
            key = row_key(info, values)
        except (KeyError, TypeError, ValueError):
            raise CheckpointMismatch('business_checkpoint_replica_row_conflict') from None
        if (key != row['row_key'] or columns != payload_columns or type(row['sequence']) is not int
                or not 0 <= row['sequence'] <= position.head):
            raise CheckpointMismatch('business_checkpoint_replica_row_conflict')
        yield key, values


@contextlib.contextmanager
def _replica_snapshot(path, expected):
    with _read_snapshot(path) as db:
        if [row[0] for row in db.execute('SELECT format FROM replica_identity')] != [FORMAT]:
            raise CheckpointMismatch('business_checkpoint_replica_format_conflict')
        source = Replica._source(db, expected.authority, expected.incarnation)
        if source is None:
            raise CheckpointMismatch('business_checkpoint_replica_baseline_required')
        state = Replica._state_value(source)
        manifest = _checked_manifest(state, expected)
        if state['cursor'] != expected.head:
            raise CheckpointMismatch('business_checkpoint_replica_not_caught_up')
        if db.execute('SELECT 1 FROM replica_sources WHERE authority=? AND incarnation<>? LIMIT 1',
                      (expected.authority, expected.incarnation)).fetchone():
            raise CheckpointMismatch('business_checkpoint_replica_incarnation_conflict')
        for table in ('replica_snapshots', 'replica_staging'):
            if db.execute('SELECT 1 FROM ' + table + ' WHERE authority=? LIMIT 1',
                          (expected.authority,)).fetchone():
                raise CheckpointMismatch('business_checkpoint_replica_snapshot_open')
        for row in db.execute('SELECT DISTINCT table_name FROM replica_rows '
                              'WHERE authority=? AND incarnation=? AND tombstone=0',
                              (expected.authority, expected.incarnation)):
            if row[0] not in manifest['tables']:
                raise CheckpointMismatch('business_checkpoint_replica_uncovered_table')
        yield db, manifest


def replica_checkpoint(replica, expected):
    """Measure the passive replica's published rows and progress in one read."""
    with _replica_snapshot(replica.path, expected) as (db, manifest):
        return _typed_witness('replica', expected, manifest,
            lambda table, info: _replica_rows(db, expected, table, info))


def compare_checkpoints(source, replica):
    """Return the matching row evidence fields for the owner command."""
    if source.kind != 'source' or replica.kind != 'replica' or source.position != replica.position:
        raise CheckpointMismatch('business_checkpoint_witness_position_conflict')
    if source.typed_rows_sha256 != replica.typed_rows_sha256 or source.table_counts != replica.table_counts:
        raise CheckpointMismatch('business_checkpoint_typed_rows_conflict')
    position = source.position
    return dict(authority=position.authority, incarnation=position.incarnation,
                generation=position.generation, manifest_sha256=position.manifest_sha256,
                sealed_journal_head=position.head, typed_rows_sha256=source.typed_rows_sha256)
