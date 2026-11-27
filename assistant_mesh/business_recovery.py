"""Prepare a private read-only recovery candidate from an exact business checkpoint."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from .business_checkpoint import (CheckpointMismatch, CheckpointPosition, _read_snapshot,
                                  _replica_rows, _replica_snapshot, _sqlite_rows, _typed_witness)
from .business_journal import canonical, digest, quote, schema, uncell


CHECKPOINT_FIELDS = {'authority', 'incarnation', 'generation', 'manifest_sha256',
                     'sealed_journal_head', 'typed_rows_sha256'}


def _checkpoint(value):
    if (not isinstance(value, dict) or set(value) != CHECKPOINT_FIELDS
            or not isinstance(value['typed_rows_sha256'], str)
            or re.fullmatch(r'[0-9a-f]{64}', value['typed_rows_sha256']) is None):
        raise CheckpointMismatch('business_recovery_checkpoint_invalid')
    return CheckpointPosition(value['authority'], value['incarnation'], value['generation'],
                              value['manifest_sha256'], value['sealed_journal_head'])


def _schema_statements(template, tables):
    # The matching application version supplies trusted schema, including
    # collations, constraints and explicit indexes absent from the row manifest.
    for table, info in tables.items():
        if schema(template, table) != info:
            raise CheckpointMismatch('business_recovery_template_schema_conflict:' + table)
    return [(row['name'], row['sql']) for row in template.execute(
        "SELECT name,tbl_name,sql FROM sqlite_master WHERE type IN ('table','index') "
        "AND sql IS NOT NULL ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,name")
        if row['tbl_name'] in tables]


def _prepare(path, replica_path, template_path, position, expected_hash):
    with _replica_snapshot(replica_path, position) as (replica, manifest), \
            _read_snapshot(template_path) as template, \
            contextlib.closing(sqlite3.connect(path)) as target:
        tables = manifest['tables']
        statements = _schema_statements(template, tables)
        target.row_factory = sqlite3.Row
        target.execute('PRAGMA trusted_schema=OFF')
        target.execute('PRAGMA synchronous=FULL')
        with target:
            target.execute('BEGIN IMMEDIATE')
            for _, sql in statements:
                target.execute(sql)
            for table, info in tables.items():
                columns = ','.join(quote(column['name']) for column in info['columns'])
                parameters = ','.join('?' for _ in info['columns'])
                target.executemany('INSERT INTO ' + quote(table) + '(' + columns + ') VALUES(' + parameters + ')',
                    ([uncell(value) for value in row] for _, row in _replica_rows(replica, position, table, info)))
            # Re-read SQLite's own values to catch corruption, affinity changes,
            # dropped selected keys or missing rows in the exact input snapshot.
            return _candidate_evidence(target, position, manifest, expected_hash, statements)


def _candidate_evidence(db, position, manifest, expected_hash, expected_schema):
    tables = manifest['tables']
    objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
    if ({row[1] for row in objects if row[0] == 'table'} != set(tables)
            or any(row[0] not in ('table', 'index') for row in objects)
            or _schema_statements(db, tables) != expected_schema):
        raise CheckpointMismatch('business_recovery_candidate_schema_conflict')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise CheckpointMismatch('business_recovery_foreign_key_conflict')
    witness = _typed_witness('recovery', position, manifest, lambda table, info: _sqlite_rows(db, table, info))
    if witness.typed_rows_sha256 != expected_hash:
        raise CheckpointMismatch('business_recovery_typed_rows_conflict')
    return dict(table_counts=dict(witness.table_counts), schema_sql_sha256=digest(expected_schema))


def prepare_recovery(replica_path, template_path, destination, sealed_checkpoint):
    """Copy verified typed rows into a fresh, inactive database, without replacing a file.

    The caller supplies evidence from the trusted sealed source. This prepares
    data only. A voter grant and the separate runtime dependency checks still
    govern activation. No excluded rows, journal incarnation, or runtime lease
    is created by this function.
    """
    position = _checkpoint(sealed_checkpoint)
    destination = Path(destination).absolute()
    _validate_destination(destination)
    with tempfile.TemporaryDirectory(prefix='.amita-recovery-', dir=destination.parent) as work:
        candidate = Path(work) / 'candidate.db'
        evidence = _prepare(candidate, replica_path, template_path, position, sealed_checkpoint['typed_rows_sha256'])
        _publish(candidate, destination, 0o400)
    return dict(format='amita-business-recovery-candidate/1', checkpoint=sealed_checkpoint,
                **evidence, execution_authorized=False, ha_qualified=False)


def _validate_destination(destination):
    directory = destination.parent.stat()
    if directory.st_uid != os.getuid() or directory.st_mode & 0o077 or destination.parent.is_symlink():
        raise ValueError('business_recovery_private_directory_required')
    if any(os.path.lexists(str(destination) + suffix) for suffix in ('', '-wal', '-shm', '-journal')):
        raise FileExistsError('business_recovery_destination_exists')


def _publish(candidate, destination, mode):
    candidate.chmod(mode)
    with candidate.open('rb') as handle:
        os.fsync(handle.fileno())
    # Atomic creation with no replacement, including a concurrent publisher.
    os.link(candidate, destination)
    fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def inspect_recovery(replica_path, template_path, candidate, sealed_checkpoint):
    """Recompute evidence from an existing candidate without opening a Store."""
    position = _checkpoint(sealed_checkpoint)
    candidate = Path(candidate)
    info = candidate.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o400 or info.st_uid != os.getuid()
            or any(os.path.lexists(str(candidate) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
        raise CheckpointMismatch('business_recovery_inactive_candidate_required')
    with _replica_snapshot(replica_path, position) as (_, manifest), \
            _read_snapshot(template_path) as template, _read_snapshot(candidate) as target:
        target.execute('PRAGMA trusted_schema=OFF')
        expected_schema = _schema_statements(template, manifest['tables'])
        evidence = _candidate_evidence(target, position, manifest, sealed_checkpoint['typed_rows_sha256'],
                                       expected_schema)
    sha256 = hashlib.sha256()
    with candidate.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            sha256.update(chunk)
        checked = os.fstat(handle.fileno())
    fingerprint = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(info, field) != getattr(checked, field) for field in fingerprint):
        raise CheckpointMismatch('business_recovery_candidate_changed')
    return dict(checkpoint=sealed_checkpoint, candidate_sha256=sha256.hexdigest(), **evidence)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replica', required=True)
    parser.add_argument('--schema-template', required=True)
    parser.add_argument('--checkpoint', required=True, help='Trusted sealed checkpoint JSON')
    parser.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    try:
        with open(args.checkpoint, 'rb') as handle:
            document = handle.read(65537)
        if len(document) > 65536:
            raise ValueError('business_recovery_checkpoint_too_large')
        checkpoint = json.loads(document)
        result = prepare_recovery(args.replica, args.schema_template, args.destination, checkpoint)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(canonical(dict(error=str(error))))
        return 2
    print(canonical(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
