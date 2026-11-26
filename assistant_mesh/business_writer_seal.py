"""Owner-operated durable retirement of one declared SQLite business writer.

This local tool grants no new writer, fences no native/external execution, and
does not open a Store constructor (which can write during recovery).
"""
import argparse
import json
from pathlib import Path
import re
import sqlite3

from .business_journal import (DEPENDENCIES, Journal, PREFIX, canonical, digest,
                               policy, quote, schema)


REQUEST = 'amita-business-writer-seal-request/1'
RECEIPT = 'amita-business-writer-seal/1'
STATE = PREFIX + 'writer_seal'
TRIGGERS = '_business_writer_seal_'
MAX_INTEGER = 2 ** 53 - 1


def _need(value, reason):
    if not value:
        raise ValueError('business_writer_seal_' + reason)


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is not None


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_INTEGER


def _request(value):
    _need(isinstance(value, dict) and set(value) == {
        'format', 'operation_id', 'source_holder', 'authority', 'incarnation',
        'generation', 'manifest_sha256', 'meta_keys'}, 'request_invalid')
    _need(value['format'] == REQUEST and all(_identifier(value[key]) for key in
          ('operation_id', 'source_holder', 'authority', 'incarnation'))
          and _integer(value['generation'], 1)
          and isinstance(value['manifest_sha256'], str)
          and re.fullmatch(r'[0-9a-f]{64}', value['manifest_sha256']) is not None
          and isinstance(value['meta_keys'], list)
          and all(isinstance(key, str) for key in value['meta_keys'])
          and value['meta_keys'] == sorted(set(value['meta_keys'])), 'request_invalid')
    return json.loads(canonical(value))


def _connect(path, readonly):
    # mode=rw refuses a typo/new file. mode=ro and query_only prevent inspection
    # from migrating, recovering tasks, changing WAL mode, or creating a seal.
    db = sqlite3.connect(Path(path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                         uri=True, timeout=10, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        if readonly:
            db.execute('PRAGMA query_only=ON')
        else:
            db.execute('PRAGMA synchronous=FULL')
            _need(db.execute('PRAGMA journal_mode').fetchone()[0] in
                  ('wal', 'delete', 'truncate', 'persist'), 'durable_journal_required')
    except BaseException:
        db.close()
        raise
    return db


def _exists(db, name, kind='table'):
    return db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', (kind, name)).fetchone() is not None


def _check_sql(db, statements):
    for trigger, statement in statements:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone()
        _need(row is not None and row[0] == statement, 'trigger_mismatch:' + trigger)


def _coverage(db):
    _need(_exists(db, PREFIX + 'state'), 'journal_not_installed')
    _need(db.execute('SELECT COUNT(*) FROM ' + PREFIX + 'state').fetchone()[0] == 1,
          'journal_state_invalid')
    row = db.execute('SELECT policy FROM ' + PREFIX + 'state WHERE singleton=1').fetchone()
    _need(row is not None, 'journal_state_invalid')
    config = policy(json.loads(row[0]))
    _need(canonical(config) == row[0], 'policy_invalid')
    journal = Journal(None, config)
    state = journal._state(db)
    actual = {table: schema(db, table) for table in config['tables']}
    manifest = dict(tables=actual, meta_keys=config['meta_keys'], excluded=config['excluded'],
                    external_dependencies=list(DEPENDENCIES))
    manifest['sha256'] = digest(manifest)
    _need(json.loads(state['manifest']) == manifest, 'manifest_mismatch')
    description = journal._description(db)
    _need(not description['coverage_issues'], 'coverage_incomplete:' + ','.join(description['coverage_issues']))
    _need(_identifier(state['authority']) and _identifier(state['incarnation'])
          and _integer(state['generation'], 1) and _integer(state['start'])
          and _integer(description['head']) and state['start'] <= description['head'],
          'journal_identity_invalid')
    captures = []
    for table, info in actual.items():
        captures.extend(journal._trigger_statements(db, table, info, state['generation']))
    _check_sql(db, captures)
    # With recursive_triggers=OFF, REPLACE can silently delete a different
    # selected key through another UNIQUE constraint. Standard meta has only
    # BINARY key uniqueness; fail closed for a schema requiring broader guards.
    if config['meta_keys']:
        info = actual['meta']
        _need([info['columns'][i]['name'] for i in info['primary_key']] == ['key'], 'meta_key_schema_invalid')
        _need(not any(column['name'].lower() in ('rowid', '_rowid_', 'oid') for column in info['columns']),
              'meta_rowid_shadow_unsupported')
        for index in db.execute('PRAGMA index_list("meta")').fetchall():
            if index[2]:
                keys = [row for row in db.execute('PRAGMA index_xinfo(' + quote(index[1]) + ')') if row[5]]
                _need(len(keys) == 1 and keys[0][2] == 'key' and keys[0][4] == 'BINARY',
                      'meta_replace_coverage_required')
    return journal, state, manifest, description, captures


def _fences(db, journal, manifest):
    statements = []
    meta_rowid = False
    if manifest['meta_keys']:
        try:
            db.execute('SELECT _rowid_ FROM meta LIMIT 0')
            meta_rowid = True
        except sqlite3.OperationalError as error:
            # WITHOUT ROWID meta has only the validated primary-key conflict.
            _need(str(error) == 'no such column: _rowid_', 'meta_rowid_schema_invalid')
    # Freeze the journal's generation and data, while permitting read/export
    # and replica acknowledgements. Retention cannot prune the sealed history.
    tables = sorted(set(manifest['tables']) | {STATE, PREFIX + 'state', PREFIX + 'events', PREFIX + 'cells'})
    for table in tables:
        for action in ('INSERT', 'UPDATE', 'DELETE'):
            condition = '1'
            if table == 'meta':
                aliases = ('OLD', 'NEW') if action == 'UPDATE' else ('OLD',) if action == 'DELETE' else ('NEW',)
                condition = ' OR '.join(journal._filter(table, alias, db, manifest['meta_keys']) for alias in aliases)
                if meta_rowid and action in ('INSERT', 'UPDATE'):
                    # REPLACE can delete a covered row through its implicit
                    # rowid even when the new key is unselected and recursive
                    # triggers are off. Check that conflict before replacement.
                    condition += (' OR EXISTS(SELECT 1 FROM meta AS protected WHERE protected._rowid_=NEW._rowid_ AND '
                                  + journal._filter(table, 'protected', db, manifest['meta_keys']) + ')')
            trigger = TRIGGERS + table + '_' + action.lower()
            statements.append((trigger, 'CREATE TRIGGER ' + quote(trigger) + ' BEFORE ' + action
                + ' ON ' + quote(table) + ' WHEN ' + condition
                + " BEGIN SELECT RAISE(ABORT,'business_writer_sealed'); END"))
    return statements


def _receipt(request, state, manifest, head, captures, fences):
    receipt = dict(format=RECEIPT, operation_id=request['operation_id'], source_holder=request['source_holder'],
        request_sha256=digest(request), authority=state['authority'], incarnation=state['incarnation'],
        generation=state['generation'], manifest_sha256=manifest['sha256'], sealed_journal_head=head,
        covered_tables=sorted(manifest['tables']), meta_keys=manifest['meta_keys'], excluded=manifest['excluded'],
        capture_triggers_sha256=digest(captures), seal_triggers_sha256=digest(fences),
        boundary='sqlite_dml_declared_tables_and_selected_meta_keys',
        external_effects_fenced=False, execution_authorized=False, ha_qualified=False)
    receipt['sha256'] = digest(receipt)
    return receipt


def _read_seal(db, coverage):
    journal, state, manifest, description, captures = coverage
    rows = db.execute('SELECT singleton,request,receipt FROM ' + quote(STATE)).fetchall()
    _need(len(rows) == 1 and rows[0][0] == 1, 'record_invalid')
    request = _request(json.loads(rows[0][1]))
    _need(rows[0][1] == canonical(request), 'record_invalid')
    _matches_request(request, state, manifest)
    fences = _fences(db, journal, manifest)
    _check_sql(db, fences)
    actual = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
              if row[0].startswith(TRIGGERS)}
    _need(actual == {name for name, _ in fences}, 'fence_set_mismatch')
    expected = _receipt(request, state, manifest, description['head'], captures, fences)
    _need(rows[0][2] == canonical(expected), 'receipt_mismatch')
    return request, expected


def _matches_request(request, state, manifest):
    _need(all(request[key] == state[key] for key in ('authority', 'incarnation', 'generation'))
          and request['manifest_sha256'] == manifest['sha256']
          and request['meta_keys'] == manifest['meta_keys'], 'source_changed')


def seal_writer(path, request):
    """Seal once under the SQLite writer lock; retry only the original request.

    There is deliberately no unseal, activation, HTTP route or authority grant.
    The caller must already control the source writer's local database.
    """
    request = _request(request)
    db = _connect(path, readonly=False)
    try:
        # Existing writers finish first. No other writer can enter between the
        # coverage check, persistent fences, final head, receipt and commit.
        db.execute('BEGIN IMMEDIATE')
        coverage = _coverage(db)
        journal, state, manifest, description, captures = coverage
        if _exists(db, STATE):
            original, receipt = _read_seal(db, coverage)
            _need(request == original, 'original_operation_conflict')
        else:
            _matches_request(request, state, manifest)
            _need(not any(row[0].startswith(TRIGGERS) for row in
                          db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")), 'incomplete_seal')
            db.execute('CREATE TABLE ' + quote(STATE) + '(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
                       'request TEXT NOT NULL,receipt TEXT NOT NULL)')
            fences = _fences(db, journal, manifest)
            record_guards = {TRIGGERS + STATE + '_' + action for action in ('insert', 'update', 'delete')}
            for trigger, statement in fences:
                if trigger not in record_guards:
                    db.execute(statement)
            head = journal._head(db)
            receipt = _receipt(request, state, manifest, head, captures, fences)
            db.execute('INSERT INTO ' + quote(STATE) + ' VALUES(1,?,?)', (canonical(request), canonical(receipt)))
            for trigger, statement in fences:
                if trigger in record_guards:
                    db.execute(statement)
            _read_seal(db, _coverage(db))
        db.commit()
        return receipt
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def inspect_writer(path):
    """Read one consistent view without constructing/recovering the writer."""
    db = _connect(path, readonly=True)
    try:
        db.execute('BEGIN')
        result = dict(format='amita-business-writer-seal-status/1', sealed=_exists(db, STATE),
            valid=False, journal=None, receipt=None, source_seal_sha256=None, coverage_issues=[],
            external_effects_fenced=False, execution_authorized=False, ha_qualified=False)
        try:
            coverage = _coverage(db)
            _, state, manifest, description, _ = coverage
            result['journal'] = dict(authority=state['authority'], incarnation=state['incarnation'],
                generation=state['generation'], manifest_sha256=manifest['sha256'], head=description['head'],
                covered_tables=sorted(manifest['tables']), meta_keys=manifest['meta_keys'])
            if result['sealed']:
                _, receipt = _read_seal(db, coverage)
                result.update(valid=True, receipt=receipt, source_seal_sha256=receipt['sha256'])
        except (ValueError, TypeError, KeyError, sqlite3.Error) as error:
            result['coverage_issues'].append(str(error))
        return result
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('inspect', 'seal'):
        command = sub.add_parser(action)
        command.add_argument('--database', required=True)
        if action == 'seal':
            command.add_argument('--request', required=True, help='Original owner request JSON file')
    args = parser.parse_args(argv)
    try:
        if args.action == 'inspect':
            value = inspect_writer(args.database)
        else:
            raw = Path(args.request).read_bytes()
            _need(len(raw) <= 65536, 'request_too_large')
            value = seal_writer(args.database, json.loads(raw))
        print(canonical(value))
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        print(canonical(dict(error=str(error))))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
