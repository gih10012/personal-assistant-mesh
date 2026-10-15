"""Read-only communication extraction and NEW prepared candidate installation.

This is NOT the old audit checkpoint and does not move executable tasks/native
history. Activation requires trusted owner code and actual old-writer closure;
JSON booleans, elapsed TTL or missing PIDs cannot authorize it.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from .channel_carrier import CarrierWitness
from .channel_state import ChannelState
from .communication_store import (
    CORE_SCHEMA, META_KEYS, PROTOCOL, CommunicationStore, _private_path, message_identity,
)
from .channel_checkpoint import _COLUMNS, _schema
from .networking import canonical, digest, identifier

TRANSFER = 'mesh-communication-transfer/2'
FENCE_TABLES = ('channel_carrier_witness', 'channel_carrier_accounts',
                'channel_carrier_candidates', 'channel_carrier_intents', 'channel_canonical_intents')


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def export_transfer(database, origin_authority, owner_ref, account_binding,
                    transfer_id, max_bytes=64 * 1024 * 1024):
    """One mode=ro snapshot; no Store/recovery, credential or external IO."""
    database = _private_path(database)
    for value in (origin_authority, owner_ref, transfer_id):
        identifier(value)
    _require(type(max_bytes) is int and 0 < max_bytes <= 1024 ** 3, 'communication_budget_invalid')
    uri = database.as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables, schemas = {}, {}
        for table in ('meta', 'inbox', 'outbox', 'mesh_notify_receipts'):
            if table == 'mesh_notify_receipts' and table not in names:
                continue
            schemas[table] = _schema(db, table)
            rows = [dict(row) for row in db.execute('SELECT * FROM ' + table)]
            if table == 'meta':
                rows = [row for row in rows if row['key'] in META_KEYS]
            if table == 'outbox':
                rows = [row for row in rows if row['delivery_route'] == 'channel']
            tables[table] = sorted(rows, key=canonical)
        meta = {row['key']: json.loads(row['value']) for row in tables['meta']}
        _require(meta.get('owner_notification_channel_binding') == account_binding
                 and isinstance(meta.get('cursor'), str), 'communication_original_binding_missing')
        incarnation = meta.get('owner_notification_receiver_incarnation')
        _require(isinstance(incarnation, str) and len(incarnation) == 64,
                 'communication_original_route_missing')
        references = []
        independent_source = 'communication_identity' in names
        if independent_source:
            _require('communication_inbox_order' in names,
                     'communication_original_archive_order_missing')
            inbox_order = [dict(row) for row in db.execute(
                'SELECT message_id,sequence FROM communication_inbox_order ORDER BY sequence')]
        else:
            inbox_order = [{'message_id': row['id'], 'sequence': row['rowid']} for row in db.execute(
                'SELECT rowid,id FROM inbox ORDER BY rowid')]
        if independent_source:
            _require(db.execute('SELECT prepared FROM communication_identity').fetchone()[0] == 1,
                     'communication_source_candidate_incomplete')
            original_identity = json.loads(db.execute('SELECT identity FROM communication_identity').fetchone()[0])
            _require(original_identity['origin_authority'] == origin_authority
                and original_identity['owner_ref'] == owner_ref
                and original_identity['account_binding'] == account_binding,
                'communication_original_identity_changed')
            references = [json.loads(row[0]) for row in db.execute(
                'SELECT reference FROM communication_task_refs ORDER BY authority,task_id')]
        else:
            for row in db.execute('SELECT * FROM tasks ORDER BY id'):
                row = dict(row)
                references.append({'task_id': row['id'], 'authority': origin_authority,
                    'parent_id': row['parent_id'], 'scope': row['scope'], 'epoch': row['epoch'],
                    'state_at_export': row['status'], 'task_row_sha256': digest(row),
                    'runnable_copy': False})
        controls = []
        if independent_source:
            controls = [{'authority': row[0], 'control_id': row[1], 'task_id': row[2],
                         'original_reference': json.loads(row[3])} for row in db.execute(
                'SELECT authority,control_id,task_id,reference FROM communication_control_refs ORDER BY authority,control_id')]
        elif 'interactions' in names:
            for row in db.execute('SELECT * FROM interactions ORDER BY id'):
                row = dict(row)
                controls.append({'authority': origin_authority, 'control_id': row['id'],
                    'task_id': row['task_id'], 'epoch': row['epoch'], 'row_sha256': digest(row)})
        fences = {}
        present = set(FENCE_TABLES) & names
        _require(not present or present == set(FENCE_TABLES), 'communication_partial_fence_schema')
        if present:
            for table in FENCE_TABLES:
                schemas[table] = [list(row) for row in db.execute('PRAGMA table_info(' + table + ')')]
                fences[table] = sorted([dict(row) for row in db.execute('SELECT * FROM ' + table)], key=canonical)
            accounts = fences['channel_carrier_accounts']
            _require(len(accounts) == 1 and accounts[0]['account_binding'] == account_binding
                and accounts[0]['canonical_authority'] == origin_authority
                and accounts[0]['owner_ref'] == owner_ref and accounts[0]['frozen'] == 1,
                'communication_source_witness_not_exclusively_frozen')
        relays = []
        if independent_source:
            relays = [json.loads(row[0]) for row in db.execute(
                'SELECT provenance FROM communication_legacy_relays ORDER BY request_id')]
        elif 'notification_relays' in names:
            # Opaque provenance only, never a runnable relay or resend queue.
            relays = [dict(row) for row in db.execute('SELECT * FROM notification_relays ORDER BY request_id')]
        payload = {'protocol': TRANSFER, 'purpose': 'communication_state_split',
            'transfer_id': transfer_id, 'captured_at': time.time(),
            'identity': {'protocol': PROTOCOL, 'origin_authority': origin_authority,
                'owner_ref': owner_ref, 'account_binding': account_binding,
                'receiver_incarnation': incarnation,
                'communication_id': digest([PROTOCOL, origin_authority, account_binding, incarnation])},
            'tables': tables, 'schemas': schemas, 'fences': fences, 'inbox_order': inbox_order,
            'task_references': references, 'control_references': controls,
            'events': ([dict(row) for row in db.execute('SELECT * FROM communication_events ORDER BY message_id')]
                       if independent_source else []),
            'legacy_relay_provenance': relays,
            'activation_authorized': False, 'runnable_tasks_or_native_history_copied': False}
        _require(len(canonical(payload).encode('utf8')) <= max_bytes, 'communication_transfer_too_large')
        return {'payload': payload, 'sha256': digest(payload)}
    except sqlite3.Error:
        raise ValueError('communication_source_read_failed') from None
    finally:
        db.close()


def validate_transfer(transfer, expected_sha256):
    _require(isinstance(transfer, dict) and set(transfer) == {'payload', 'sha256'},
             'communication_transfer_invalid')
    payload = transfer['payload']
    keys = {'protocol', 'purpose', 'transfer_id', 'captured_at', 'identity', 'tables', 'schemas',
            'fences', 'task_references', 'control_references', 'legacy_relay_provenance', 'events',
            'activation_authorized', 'runnable_tasks_or_native_history_copied', 'inbox_order'}
    _require(isinstance(payload, dict) and set(payload) == keys and payload.get('protocol') == TRANSFER
        and payload.get('purpose') == 'communication_state_split'
        and payload.get('activation_authorized') is False
        and payload.get('runnable_tasks_or_native_history_copied') is False
        and transfer['sha256'] == expected_sha256 == digest(payload),
        'communication_external_pin_mismatch')
    identity = payload['identity']
    _require(set(identity) == {'protocol', 'origin_authority', 'owner_ref', 'account_binding',
        'receiver_incarnation', 'communication_id'} and identity.get('protocol') == PROTOCOL
        and identity['communication_id'] == digest([
        PROTOCOL, identity['origin_authority'], identity['account_binding'], identity['receiver_incarnation']]),
        'communication_identity_invalid')
    rows = payload['tables']
    _require(set(rows) in ({'meta', 'inbox', 'outbox'}, {'meta', 'inbox', 'outbox', 'mesh_notify_receipts'}),
             'communication_tables_invalid')
    for table, values in rows.items():
        _require(isinstance(values, list) and all(set(row) == set(_COLUMNS[table]) for row in values),
                 'communication_rows_invalid')
    _require(not payload['fences'] or set(payload['fences']) == set(FENCE_TABLES),
             'communication_fence_tables_invalid')
    for table, values in payload['fences'].items():
        columns = {item[1] for item in payload['schemas'][table]}
        _require(isinstance(values, list) and all(set(row) == columns for row in values),
                 'communication_fence_rows_invalid')
    if payload['fences']:
        accounts = payload['fences']['channel_carrier_accounts']
        _require(len(payload['fences']['channel_carrier_witness']) == 1
            and len(accounts) == 1 and accounts[0]['frozen'] == 1
            and accounts[0]['account_binding'] == identity['account_binding']
            and accounts[0]['canonical_authority'] == identity['origin_authority']
            and accounts[0]['owner_ref'] == identity['owner_ref'],
            'communication_source_witness_not_exclusively_frozen')
    for row in rows['inbox']:
        _require(message_identity(json.loads(row['body'])) == row['id'],
                 'communication_original_message_identity_changed')
    for row in rows['outbox']:
        import hashlib
        _require(hashlib.sha256(row['body'].encode('utf8')).hexdigest() == row['fingerprint']
            and row['delivery_route'] == 'channel', 'communication_original_send_changed')
    meta = {row['key']: json.loads(row['value']) for row in rows['meta']}
    _require(set(meta) <= META_KEYS and isinstance(meta.get('cursor'), str)
        and meta.get('owner_notification_channel_binding') == identity['account_binding']
        and meta.get('owner_notification_receiver_incarnation') == identity['receiver_incarnation'],
        'communication_original_meta_changed')
    inbox = {row['id']: json.loads(row['body']) for row in rows['inbox']}
    order = payload['inbox_order']
    _require(isinstance(order, list) and len(order) == len(inbox)
        and all(isinstance(item, dict) and set(item) == {'message_id', 'sequence'}
                and isinstance(item['message_id'], str) and item['message_id'] in inbox
                and type(item['sequence']) is int and 0 < item['sequence'] <= 2 ** 63 - 1
                for item in order), 'communication_original_archive_order_invalid')
    _require(len({item['message_id'] for item in order}) == len(inbox)
        and len({item['sequence'] for item in order}) == len(order),
        'communication_original_archive_order_invalid')
    event_keys = {'message_id', 'body_sha256', 'kind', 'required_authority', 'target_authority',
                  'state', 'post_attempted', 'receipt', 'created', 'updated'}
    for event in payload['events']:
        _require(set(event) == event_keys and event['message_id'] in inbox
            and event['body_sha256'] == digest(inbox[event['message_id']])
            and event['state'] in ('queued', 'dispatching', 'unknown', 'confirmed')
            and event['post_attempted'] in (0, 1), 'communication_original_event_changed')
    _require(all(ref.get('runnable_copy') is False for ref in payload['task_references']),
             'communication_runnable_copy_forbidden')
    return payload


def write_transfer(transfer, path):
    path = _private_path(path, existing=False)
    validate_transfer(transfer, transfer['sha256'])
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf8') as stream:
        stream.write(canonical(transfer))
        stream.flush()
        os.fsync(stream.fileno())


def install_prepared(transfer, database, expected_sha256):
    """NEW independent DB only. It stays inactive/frozen, even after import."""
    payload = validate_transfer(transfer, expected_sha256)
    path = _private_path(database, existing=False)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    db = sqlite3.connect(str(path), isolation_level=None)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        for statement in CORE_SCHEMA:
            db.execute(statement)
        db.execute('INSERT INTO communication_identity VALUES(1,?,?,NULL,0)',
                   (canonical(payload['identity']), expected_sha256))
        for table, rows in payload['tables'].items():
            for row in rows:
                columns = _COLUMNS[table]
                db.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' +
                           ','.join('?' for _ in columns) + ')', tuple(row[key] for key in columns))
        for item in payload['inbox_order']:
            db.execute('INSERT INTO communication_inbox_order VALUES(?,?)',
                       (item['message_id'], item['sequence']))
        for reference in payload['task_references']:
            db.execute('INSERT INTO communication_task_refs VALUES(?,?,?)',
                       (reference['authority'], reference['task_id'], canonical(reference)))
        for reference in payload['control_references']:
            db.execute('INSERT INTO communication_control_refs VALUES(?,?,?,?)',
                (reference['authority'], reference['control_id'], reference['task_id'], canonical(reference)))
        for event in payload['events']:
            columns = tuple(event)
            db.execute('INSERT INTO communication_events(' + ','.join(columns) + ') VALUES(' +
                ','.join('?' for _ in columns) + ')', tuple(event[key] for key in columns))
        db.execute('CREATE TABLE communication_legacy_relays(request_id TEXT PRIMARY KEY,provenance TEXT NOT NULL)')
        for row in payload['legacy_relay_provenance']:
            db.execute('INSERT INTO communication_legacy_relays VALUES(?,?)', (row['request_id'], canonical(row)))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    store = CommunicationStore(path)
    if payload['fences']:
        witness_id = payload['fences']['channel_carrier_witness'][0]['witness_id']
        witness = CarrierWitness(store, witness_id)  # schema only, no grant/transport
        ChannelState(store, witness)
        with store.transaction() as db:
            for table, rows in payload['fences'].items():
                _require([list(row) for row in db.execute('PRAGMA table_info(' + table + ')')]
                    == payload['schemas'][table], 'communication_fence_schema_changed')
                db.execute('DELETE FROM ' + table)  # only NEW inactive candidate, never source
                for row in rows:
                    columns = tuple(info[1] for info in db.execute('PRAGMA table_info(' + table + ')'))
                    db.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' +
                        ','.join('?' for _ in columns) + ')', tuple(row[key] for key in columns))
    with store.transaction() as db:
        db.execute('UPDATE communication_identity SET prepared=1 WHERE singleton=1')
    return store


def verify_prepared(store, transfer, expected_sha256):
    """Compare ACTUAL candidate data to the externally pinned source artifact."""
    payload = validate_transfer(transfer, expected_sha256)
    _require(store.import_sha256 == expected_sha256 and store.identity == payload['identity'],
             'communication_candidate_identity_changed')
    with store.transaction() as db:
        marker = db.execute('SELECT identity,import_sha256,prepared,activation '
                            'FROM communication_identity WHERE singleton=1').fetchone()
        _require(marker is not None and json.loads(marker[0]) == payload['identity']
                 and marker[1] == expected_sha256, 'communication_candidate_identity_changed')
        _require(marker[2] == 1 and marker[3] is None, 'communication_candidate_not_complete_or_already_active')
        for table, rows in payload['tables'].items():
            actual = sorted([dict(row) for row in db.execute('SELECT * FROM ' + table)], key=canonical)
            _require(actual == sorted(rows, key=canonical), 'communication_candidate_rows_changed')
        actual = sorted([dict(row) for row in db.execute('SELECT * FROM communication_events')], key=canonical)
        _require(actual == sorted(payload['events'], key=canonical), 'communication_candidate_events_changed')
        actual = [dict(row) for row in db.execute('SELECT * FROM communication_inbox_order ORDER BY sequence')]
        _require(actual == sorted(payload['inbox_order'], key=lambda row: row['sequence']),
                 'communication_candidate_archive_order_changed')
        for table, keys, field, references in (
                ('communication_task_refs', ('authority', 'task_id'), 'reference', payload['task_references']),
                ('communication_control_refs', ('authority', 'control_id', 'task_id'), 'reference', payload['control_references']),
                ('communication_legacy_relays', ('request_id',), 'provenance', payload['legacy_relay_provenance'])):
            actual = sorted([dict(row) for row in db.execute('SELECT * FROM ' + table)], key=canonical)
            expected = [dict({key: reference[key] for key in keys}, **{field: canonical(reference)})
                        for reference in references]
            _require(actual == sorted(expected, key=canonical), 'communication_candidate_references_changed')
        for table, rows in payload['fences'].items():
            actual = [dict(row) for row in db.execute('SELECT * FROM ' + table)]
            expected = [dict(row) for row in rows]
            if table == 'channel_carrier_witness':
                _require(actual[0]['last_clock'] >= expected[0]['last_clock'], 'communication_clock_regressed')
                # Inspection changes only this monotonic watermark, not the
                # original term, holder, unknown or durable incarnation.
                for row in actual + expected:
                    row.pop('last_clock')
            _require(sorted(actual, key=canonical) == sorted(expected, key=canonical),
                     'communication_candidate_fence_changed')
    return {'candidate_bytes_verified': True, 'quiescence_verified': False, 'grant_authorized': False}


def activate_prepared(store, proof_ref, owner_verifier=None, transfer=None):
    """Trusted code verifier only, no RPC/CLI activation or posted booleans."""
    identifier(proof_ref)
    _require(callable(owner_verifier), 'communication_owner_verifier_missing')
    _require(transfer is not None, 'communication_external_artifact_required')
    verify_prepared(store, transfer, store.import_sha256)
    before = store.status()
    proof = owner_verifier(dict(store.identity), store.import_sha256, proof_ref)
    fields = ('source_channel_writers_stopped', 'source_canonical_writes_frozen',
              'external_operations_quiescent', 'original_identity_preserved')
    _require(isinstance(proof, dict) and set(proof) == {
        'protocol', 'communication_id', 'import_sha256', 'proof_ref'} | set(fields)
        and proof['protocol'] == 'mesh-communication-cutover-proof/1'
        and proof['communication_id'] == store.identity['communication_id']
        and proof['import_sha256'] == store.import_sha256 and proof['proof_ref'] == proof_ref
        and all(proof[key] is True for key in fields), 'communication_cutover_proof_invalid')
    with store.transaction() as db:
        row = db.execute('SELECT activation FROM communication_identity').fetchone()
        if row[0] is not None:
            _require(json.loads(row[0]) == proof, 'communication_activation_already_bound')
        else:
            db.execute('UPDATE communication_identity SET activation=? WHERE singleton=1', (canonical(proof),))
    return dict(before, activated=True, carrier_granted=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Private communication split candidate; no activate/grant/start')
    commands = parser.add_subparsers(dest='action')
    export = commands.add_parser('export')
    for name in ('database', 'authority', 'owner-ref', 'account-binding', 'transfer-id', 'output'):
        export.add_argument('--' + name, required=True)
    install = commands.add_parser('prepare')
    for name in ('transfer', 'expected-sha256', 'database'):
        install.add_argument('--' + name, required=True)
    verify = commands.add_parser('verify')
    for name in ('transfer', 'expected-sha256', 'database'):
        verify.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'export':
            transfer = export_transfer(args.database, args.authority, args.owner_ref,
                                       args.account_binding, args.transfer_id)
            write_transfer(transfer, args.output)
            print(json.dumps({'sha256': transfer['sha256'], 'activated': False, 'source_read_only': True}))
        elif args.action == 'prepare':
            transfer = json.loads(_private_path(args.transfer).read_text())
            store = install_prepared(transfer, args.database, args.expected_sha256)
            print(json.dumps(store.status()))
        elif args.action == 'verify':
            transfer = json.loads(_private_path(args.transfer).read_text())
            print(json.dumps(verify_prepared(CommunicationStore(args.database), transfer, args.expected_sha256)))
        else:
            parser.error('an action is required')
    except Exception:
        print(json.dumps({'ok': False, 'error': 'communication_transfer_unconfirmed'}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
