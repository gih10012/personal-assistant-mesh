"""Read-only, private channel checkpoint; NOT an import or carrier lease.

A single SQLite read transaction preserves channel rows and complete task rows.
Other authority tables are fingerprinted, not copied or declared migratable.
The format always rejects migration_ready=True. No Store constructor, credential
file, iLink request, task claim, native history rewrite or send is performed.
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
from pathlib import Path


PROTOCOL = 'mesh-channel-checkpoint/1'
_SHA = re.compile(r'^[0-9a-f]{64}$')
_COLUMNS = {
    'meta': ('key', 'value'),
    'inbox': ('id', 'body', 'created'),
    'tasks': ('id', 'parent_id', 'input', 'required', 'status', 'node', 'epoch',
              'deadline', 'checkpoint', 'result', 'created', 'attempts', 'context',
              'scope', 'paused_status', 'paused_deadline', 'leader_epoch'),
    'outbox': ('id', 'fingerprint', 'body', 'status', 'client_id', 'detail',
               'created', 'context_version', 'media_items', 'retry_count', 'delivery_route'),
    'notification_relays': ('request_id', 'source_node', 'peer', 'authority',
                            'fingerprint', 'state', 'attempt', 'post_attempted',
                            'lease_until', 'receipt', 'error', 'created', 'updated'),
    'mesh_notify_receipts': ('source_node', 'request_id', 'fingerprint', 'outbox_id', 'created'),
}
_LAYOUT = {
    'meta': ('TEXT TEXT', {'key'}, {}, ('key',)),
    'inbox': ('TEXT TEXT REAL', {'id'}, {}, ('id',)),
    'tasks': ('TEXT TEXT TEXT TEXT TEXT TEXT INTEGER REAL TEXT TEXT REAL INTEGER TEXT TEXT TEXT REAL INTEGER',
              {'id', 'parent_id', 'node', 'deadline', 'result', 'paused_status', 'paused_deadline', 'leader_epoch'},
              {'epoch': '0', 'checkpoint': "'{}'", 'attempts': '0', 'context': "'{}'", 'scope': "'leader:owner'"}, ('id',)),
    'outbox': ('TEXT TEXT TEXT TEXT TEXT TEXT REAL TEXT TEXT INTEGER TEXT',
               {'id', 'context_version', 'media_items'},
               {'detail': "'{}'", 'retry_count': '0', 'delivery_route': "'channel'"}, ('id',)),
    'notification_relays': ('TEXT TEXT TEXT TEXT TEXT TEXT INTEGER INTEGER REAL TEXT TEXT REAL REAL',
                            {'request_id', 'receipt', 'error'},
                            {'state': "'pending'", 'attempt': '0', 'post_attempted': '0', 'lease_until': '0'}, ('request_id',)),
    'mesh_notify_receipts': ('TEXT TEXT TEXT TEXT REAL', set(), {}, ('source_node', 'request_id')),
}
_META = frozenset(('cursor', 'owner_context', 'last_poll', 'activate_after_ms',
                   'channel_error', 'owner_notification_channel_binding',
                   'owner_notification_receiver_incarnation'))
_SAFETY = {'native_features_restricted': False, 'carrier_lease_verified': False,
           'source_quiescence_verified': False, 'native_history_complete': False,
           'task_execution_dependencies_complete': False, 'import_or_send_authorized': False}
_OUTBOX_STATES = frozenset(('pending', 'submitting', 'accepted', 'rejected',
                           'unknown', 'waiting_auth'))
_TASK_STATES = frozenset(('pending', 'running', 'waiting_backend', 'waiting_auth',
                         'waiting_children', 'waiting_remote', 'continuing',
                         'paused', 'unknown', 'completed', 'failed', 'needs_review'))


class CheckpointError(ValueError):
    """Fixed diagnostics only; never include private rows or SQLite messages."""


def _require(value, code):
    if not value:
        raise CheckpointError(code)


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CheckpointError('checkpoint_encoding_invalid') from None


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _name(value):
    _require(isinstance(value, str) and 0 < len(value) <= 200 and
             not any(ord(c) < 32 for c in value), 'checkpoint_identity_invalid')
    return value


def _sha(value):
    _require(isinstance(value, str) and _SHA.fullmatch(value) is not None,
             'checkpoint_digest_invalid')
    return value


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _json(value, kind):
    try:
        result = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        raise CheckpointError('checkpoint_row_json_invalid') from None
    _require(isinstance(result, kind), 'checkpoint_row_json_invalid')
    _canonical(result)
    return result


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


class _Budget:
    def __init__(self, max_rows, max_bytes):
        _require(type(max_rows) is int and 0 < max_rows <= 1000000 and
                 type(max_bytes) is int and 0 < max_bytes <= 1024 ** 3,
                 'checkpoint_limits_invalid')
        self.max_rows, self.max_bytes = max_rows, max_bytes
        self.rows, self.bytes = 0, 0

    def add(self, size):
        self.rows += 1
        self.bytes += size
        _require(self.rows <= self.max_rows and self.bytes <= self.max_bytes,
                 'checkpoint_limit_exceeded')


def _private_parent(path):
    _require(path.is_absolute() and '..' not in path.parts,
             'checkpoint_private_path_required')
    try:
        _require(not any(p.is_symlink() for p in (path,) + tuple(path.parents)),
                 'checkpoint_private_path_required')
        parent = path.parent.lstat()
        _require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid()
                 and stat.S_IMODE(parent.st_mode) == 0o700,
                 'checkpoint_private_path_required')
        for ancestor in path.parents:
            metadata = ancestor.lstat()
            _require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid in (0, os.getuid())
                     and (not metadata.st_mode & 0o022 or metadata.st_mode & stat.S_ISVTX),
                     'checkpoint_private_path_required')
    except OSError:
        raise CheckpointError('checkpoint_private_path_required') from None


def _private_file(path):
    path = Path(path)
    _private_parent(path)
    try:
        metadata = path.lstat()
        _require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
                 and metadata.st_nlink == 1 and stat.S_IMODE(metadata.st_mode) == 0o600,
                 'checkpoint_private_path_required')
    except OSError:
        raise CheckpointError('checkpoint_private_path_required') from None
    return path, (metadata.st_dev, metadata.st_ino)


def _schema(db, table):
    rows = [list(row) for row in db.execute('PRAGMA table_info(' + _quoted(table) + ')')]
    _require(rows == _expected_schema(table),
             'checkpoint_schema_unsupported')
    return rows


def _expected_schema(table):
    types, nullable, defaults, primary = _LAYOUT[table]
    return [[index, name, kind, int(name not in nullable), defaults.get(name),
             primary.index(name) + 1 if name in primary else 0]
            for index, (name, kind) in enumerate(zip(_COLUMNS[table], types.split()))]


def _rows(db, table, budget):
    columns = _COLUMNS[table]
    output = []
    primary = _LAYOUT[table][3]
    for raw in db.execute('SELECT * FROM ' + _quoted(table) + ' ORDER BY ' +
                          ','.join(_quoted(c) for c in primary)):
        row = dict(zip(columns, raw))
        if table == 'meta' and row['key'] not in _META:
            continue
        budget.add(len(_canonical(row)))
        output.append(row)
    return output


def _dependency(db, table, sql, budget):
    """A source-authority anchor, not export of arbitrary table/filesystem data."""
    info = [list(row) for row in db.execute('PRAGMA table_info(' + _quoted(table) + ')')]
    names = [row[1] for row in info]
    order = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]] or names
    digest = hashlib.sha256()
    count = 0
    for raw in db.execute('SELECT * FROM ' + _quoted(table) + ' ORDER BY ' +
                          ','.join(_quoted(c) for c in order)):
        value, size = [], 0
        for item in raw:
            if isinstance(item, bytes):
                size += len(item)
                item = {'sqlite_blob_sha256': hashlib.sha256(item).hexdigest(), 'bytes': len(item)}
            value.append(item)
        encoded = _canonical(value)
        budget.add(len(encoded) + size)
        digest.update(encoded + b'\n')
        count += 1
    return {'table': table, 'schema_sha256': _digest([sql, info]),
            'rows': count, 'content_sha256': digest.hexdigest(),
            'retained_at_source_authority': True}


def _mapping(tables, authority):
    return [{'task_id': row['id'], 'ledger_authority': authority,
             'scope': row['scope'], 'epoch': row['epoch'], 'task_row_sha256': _digest(row)}
            for row in tables['tasks']]


def _inbox_bindings(tables, meta):
    tasks = {row['id']: row for row in tables['tasks']}
    output = []
    for row in tables['inbox']:
        msg = _json(row['body'], dict)
        _require(_number(msg.get('create_time_ms', 0)), 'checkpoint_inbox_time_invalid')
        raw_id = msg.get('message_id')
        # Preserve the actual Store.ingest identity algorithm, including ASCII
        # JSON escaping for historical Unicode message IDs.
        try:
            identity = hashlib.sha256(json.dumps([msg.get('to_user_id'), raw_id,
                msg if raw_id is None else None], sort_keys=True).encode()).hexdigest()
        except (ValueError, TypeError, UnicodeError):
            raise CheckpointError('checkpoint_inbox_identity_invalid') from None
        _require(identity == row['id'], 'checkpoint_inbox_identity_invalid')
        items = msg.get('item_list', [])
        _require(isinstance(items, list), 'checkpoint_inbox_items_invalid')
        text = '\n'.join(str(item.get('text_item', {}).get('text', '')) for item in items
                         if isinstance(item, dict) and item.get('type') == 1).strip()
        stale = msg.get('create_time_ms', 0) < meta.get('activate_after_ms', 0)
        control = (text in ('/status', '状态', '助理状态') or
                   text.startswith(('ClawBot 自动刷新', '/answer ', '/approve ', '/deny ',
                                    '/steer ', '/pause ', '/resume ')))
        invalid_goal = text.startswith('/goal ') and (not text[6:].strip() or len(text[6:].strip()) > 4000)
        expected_task = bool(text) and not stale and not control and not invalid_goal
        _require(not expected_task or row['id'] in tasks, 'checkpoint_owner_task_missing')
        output.append({'message_id': row['id'], 'task_id': row['id'] if row['id'] in tasks else None,
                       'binding': 'canonical_task' if row['id'] in tasks else 'archived_control_or_media'})
    return output


def _validate_payload(payload):
    required = {'protocol', 'purpose', 'checkpoint_id', 'authority', 'account_binding',
                'route_id', 'captured_at', 'migration_ready', 'safety', 'schemas',
                'tables', 'task_bindings', 'inbox_task_bindings', 'source_dependencies'}
    _require(isinstance(payload, dict) and set(payload) == required and
             payload['protocol'] == PROTOCOL and payload['purpose'] == 'private_audit_checkpoint'
             and payload['migration_ready'] is False and isinstance(payload['safety'], dict)
             and set(payload['safety']) == set(_SAFETY)
             and all(payload['safety'][key] is False for key in _SAFETY),
             'checkpoint_format_invalid')
    authority = _name(payload['authority'])
    _name(payload['checkpoint_id'])
    _sha(payload['account_binding'])
    _sha(payload['route_id'])
    _require(_number(payload['captured_at']) and payload['captured_at'] >= 0,
             'checkpoint_time_invalid')
    tables, schemas = payload['tables'], payload['schemas']
    _require(isinstance(tables, dict) and isinstance(schemas, dict) and
             set(tables) == set(schemas) and set(_COLUMNS) - {'mesh_notify_receipts'} <= set(tables)
             and not set(tables) - set(_COLUMNS), 'checkpoint_tables_invalid')
    for table, rows in tables.items():
        schema = schemas[table]
        _require(schema == _expected_schema(table), 'checkpoint_schema_unsupported')
        _require(isinstance(rows, list) and all(isinstance(row, dict) and set(row) == set(_COLUMNS[table])
                                              for row in rows), 'checkpoint_rows_invalid')
        _require(all(all(value is None or type(value) in (str, int) or _number(value)
                         for value in row.values()) for row in rows), 'checkpoint_rows_invalid')
        keys = [tuple(row[r[1]] for r in sorted(schema, key=lambda r: r[5]) if r[5]) for row in rows]
        _require(all(keys) and len(set(keys)) == len(keys), 'checkpoint_duplicate_row')
        for key in keys:
            for value in key:
                _name(value)
    meta = {row['key']: _json(row['value'], (dict, list, str, int, float, bool, type(None)))
            for row in tables['meta']}
    _require(set(meta) <= _META and isinstance(meta.get('cursor'), str), 'checkpoint_cursor_invalid')
    _require(meta.get('owner_notification_channel_binding') == payload['account_binding'],
             'checkpoint_account_binding_mismatch')
    incarnation = _sha(meta.get('owner_notification_receiver_incarnation'))
    _require(_digest(['mesh-notify/1', payload['account_binding'], incarnation]) == payload['route_id'],
             'checkpoint_route_binding_mismatch')
    context = meta.get('owner_context')
    if context is not None:
        _require(isinstance(context, dict) and isinstance(context.get('token'), str)
                 and bool(context['token']) and isinstance(context.get('version'), str)
                 and _number(context.get('created')), 'checkpoint_context_invalid')
    if 'activate_after_ms' in meta:
        _require(_number(meta['activate_after_ms']), 'checkpoint_time_invalid')
    tasks = {row['id']: row for row in tables['tasks']}
    for task in tasks.values():
        _name(task['id'])
        _name(task['scope'])
        _require(isinstance(task['input'], str) and task['status'] in _TASK_STATES
                 and type(task['epoch']) is int and task['epoch'] >= 0
                 and type(task['attempts']) is int and task['attempts'] >= 0,
                 'checkpoint_task_invalid')
        _json(task['checkpoint'], dict)
        _json(task['context'], dict)
        _require(all(isinstance(c, str) for c in _json(task['required'], list)), 'checkpoint_task_invalid')
        _require(task['parent_id'] is None or task['parent_id'] in tasks,
                 'checkpoint_task_parent_missing')
    # Every parent edge is included; cycles cannot masquerade as a task tree.
    visited = set()
    for task_id in tasks:
        chain, cursor = set(), task_id
        while cursor is not None and cursor not in visited:
            _require(cursor not in chain, 'checkpoint_task_parent_cycle')
            chain.add(cursor)
            cursor = tasks[cursor]['parent_id']
        visited.update(chain)
    _require(payload['task_bindings'] == _mapping(tables, authority), 'checkpoint_task_binding_mismatch')
    _require(payload['inbox_task_bindings'] == _inbox_bindings(tables, meta),
             'checkpoint_inbox_task_binding_mismatch')
    outbox = {row['id']: row for row in tables['outbox']}
    for row in outbox.values():
        _name(row['id'])
        _name(row['client_id'])
        _require(isinstance(row['body'], str) and _sha(row['fingerprint']) == hashlib.sha256(row['body'].encode()).hexdigest()
                 and row['status'] in _OUTBOX_STATES and row['delivery_route'] in ('channel', 'relay', 'private')
                 and type(row['retry_count']) is int and row['retry_count'] >= 0, 'checkpoint_outbox_invalid')
        _json(row['detail'], dict)
        if row['media_items'] is not None:
            _json(row['media_items'], list)
    for row in tables['notification_relays']:
        original = outbox.get(row['request_id'])
        _require(original is not None and original['delivery_route'] == 'relay'
                 and original['media_items'] is None and original['fingerprint'] == row['fingerprint']
                 and row['state'] in _OUTBOX_STATES | {'queued'}
                 and type(row['post_attempted']) is int and row['post_attempted'] in (0, 1)
                 and type(row['attempt']) is int and row['attempt'] >= 0,
                 'checkpoint_relay_binding_invalid')
        for key in ('source_node', 'peer', 'authority'):
            _name(row[key])
        if row['receipt'] is not None:
            receipt = _json(row['receipt'], dict)
            expected_id = 'mesh-notify-' + _digest(['mesh-notify/1', row['source_node'], row['request_id']])
            _require(receipt.get('protocol') == 'mesh-notify/1' and receipt.get('authority') == row['authority']
                     and receipt.get('source_node') == row['source_node'] and receipt.get('request_id') == row['request_id']
                     and receipt.get('fingerprint') == row['fingerprint'] and receipt.get('outbox_id') == expected_id
                     and receipt.get('delivery_verified') is False, 'checkpoint_relay_receipt_invalid')
            _sha(receipt.get('route_id'))
    for row in tables.get('mesh_notify_receipts', []):
        original = outbox.get(row['outbox_id'])
        expected = 'mesh-notify-' + _digest(['mesh-notify/1', row['source_node'], row['request_id']])
        _require(original is not None and row['outbox_id'] == expected
                 and original['delivery_route'] == 'channel' and original['media_items'] is None
                 and original['fingerprint'] == row['fingerprint'], 'checkpoint_notify_receipt_invalid')
    dependencies = payload['source_dependencies']
    _require(isinstance(dependencies, list), 'checkpoint_dependencies_invalid')
    names = set()
    for dependency in dependencies:
        _require(isinstance(dependency, dict) and set(dependency) ==
                 {'table', 'schema_sha256', 'rows', 'content_sha256', 'retained_at_source_authority'}
                 and dependency['retained_at_source_authority'] is True
                 and isinstance(dependency['table'], str) and dependency['table'] not in names
                 and dependency['table'] not in tables and type(dependency['rows']) is int
                 and dependency['rows'] >= 0, 'checkpoint_dependencies_invalid')
        names.add(dependency['table'])
        _sha(dependency['schema_sha256'])
        _sha(dependency['content_sha256'])


def export_checkpoint(database, authority, account_binding, checkpoint_id,
                      max_rows=100000, max_bytes=64 * 1024 * 1024, clock=time.time):
    """Capture one consistent read view. authority is a trusted caller binding.

    This does not assert the live carrier/worker stopped. A later writer may
    advance the source immediately; the snapshot is not a takeover permission.
    Limits cover exported rows plus source dependency bytes (including blobs).
    """
    _name(authority)
    _sha(account_binding)
    _name(checkpoint_id)
    budget = _Budget(max_rows, max_bytes)
    path, identity = _private_file(database)
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None, timeout=10)
        _require(_private_file(path)[1] == identity, 'checkpoint_source_replaced')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        # This first read pins the schema and every subsequent table to the
        # same SQLite snapshot, even if an independent WAL writer advances.
        catalog = [(r[0], r[1]) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        names = {name for name, _ in catalog}
        _require(set(_COLUMNS) - {'mesh_notify_receipts'} <= names, 'checkpoint_schema_unsupported')
        selected = sorted(names & set(_COLUMNS))
        schemas = {table: _schema(db, table) for table in selected}
        tables = {table: _rows(db, table, budget) for table in selected}
        dependencies = [_dependency(db, table, sql, budget) for table, sql in catalog if table not in tables]
        meta = {r['key']: _json(r['value'], (dict, list, str, int, float, bool, type(None))) for r in tables['meta']}
        incarnation = _sha(meta.get('owner_notification_receiver_incarnation'))
        payload = {'protocol': PROTOCOL, 'purpose': 'private_audit_checkpoint',
                   'checkpoint_id': checkpoint_id, 'authority': authority,
                   'account_binding': account_binding,
                   'route_id': _digest(['mesh-notify/1', account_binding, incarnation]),
                   'captured_at': clock(), 'migration_ready': False, 'safety': dict(_SAFETY),
                   'schemas': schemas, 'tables': tables, 'task_bindings': _mapping(tables, authority),
                   'inbox_task_bindings': _inbox_bindings(tables, meta), 'source_dependencies': dependencies}
        _validate_payload(payload)
        encoded = _canonical(payload)
        _require(len(encoded) <= max_bytes, 'checkpoint_limit_exceeded')
        _require(_private_file(path)[1] == identity, 'checkpoint_source_replaced')
        return {'payload': payload, 'sha256': hashlib.sha256(encoded).hexdigest()}
    except sqlite3.Error:
        raise CheckpointError('checkpoint_source_read_failed') from None
    finally:
        if db is not None:
            db.close()  # read-only rollback; never commit source mutations


def validate_checkpoint(checkpoint, authority, account_binding, expected_sha256):
    """Validate against a separately retained digest/binding, never its own claim.

    SHA is integrity evidence, not a signature or proof of carrier exclusivity.
    Keep expected_sha256 outside the artifact through an authenticated handoff.
    """
    _require(isinstance(checkpoint, dict) and set(checkpoint) == {'payload', 'sha256'}, 'checkpoint_format_invalid')
    _name(authority)
    _sha(account_binding)
    _sha(expected_sha256)
    _sha(checkpoint['sha256'])
    _require(_digest(checkpoint['payload']) == checkpoint['sha256'] == expected_sha256,
             'checkpoint_integrity_mismatch')
    _validate_payload(checkpoint['payload'])
    payload = checkpoint['payload']
    _require(payload['authority'] == authority, 'checkpoint_authority_mismatch')
    _require(payload['account_binding'] == account_binding, 'checkpoint_account_binding_mismatch')
    return {'valid': True, 'sha256': expected_sha256, 'migration_ready': False,
            'import_or_send_authorized': False, 'native_features_restricted': False}


def write_checkpoint(checkpoint, destination, authority, account_binding, expected_sha256,
                     max_bytes=64 * 1024 * 1024):
    """Publish a new 0600 artifact in an existing 0700 directory; never replace.

    This does not read/copy credential files or open a destination authority DB.
    Atomic link publication fails if any original destination already exists.
    """
    validate_checkpoint(checkpoint, authority, account_binding, expected_sha256)
    encoded = _canonical(checkpoint)
    _require(type(max_bytes) is int and 0 < len(encoded) <= max_bytes, 'checkpoint_limit_exceeded')
    path = Path(destination)
    _private_parent(path)
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.mesh-checkpoint-', dir=str(path.parent))
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, str(path))
            directory = os.open(str(path.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)
    except FileExistsError:
        raise CheckpointError('checkpoint_destination_exists') from None
    except OSError:
        raise CheckpointError('checkpoint_publish_failed') from None
    return {'written': True, 'sha256': expected_sha256, 'migration_ready': False}
