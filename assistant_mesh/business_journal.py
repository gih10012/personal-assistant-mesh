"""Transactional business change journal and bounded consistent bootstrap reads.

Only declared SQLite rows are exported. No login files, native rollout paths,
workspace files or execution authority are opened or transferred here.
"""
import base64
import contextlib
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid


FORMAT = 'amita-business-replication/1'
PREFIX = '_business_replication_'
CORE = ('tasks', 'task_continuations', 'interactions', 'steering', 'agent_actions',
        'sessions', 'native_sessions', 'session_chunks', 'memories')
DEPENDENCIES = ('workspace_files', 'external_native_history_files', 'oauth_state_and_writer',
                'channel_state_and_fenced_carrier')
MAX_BYTES = 6 * 1024 * 1024
# A single base64 fetch must fit the existing bounded control response too.
MAX_BLOB_BYTES = 4 * 1024 * 1024
ACK_PROTOCOL = 'amita-business-replica-ack/1'
ACK_FIELDS = {'authority', 'incarnation', 'generation', 'cursor', 'manifest_sha256'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf8')).hexdigest()


def name(value):
    if not isinstance(value, str) or not re.match(r'^[A-Za-z][A-Za-z0-9_]{0,127}$', value):
        raise ValueError('business_replication_identifier_invalid')
    return value


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def cell(value):
    if value is None:
        return ['null', None]
    if isinstance(value, bytes):
        return ['blob', base64.b64encode(value).decode('ascii')]
    if type(value) is int:
        return ['integer', str(value)]
    if type(value) is float:
        return ['real', value.hex()]
    if isinstance(value, str):
        return ['text', value]
    raise ValueError('business_replication_value_invalid')


def uncell(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('business_replication_cell_invalid')
    kind, data = value
    if kind == 'null' and data is None:
        return None
    if not isinstance(data, str):
        raise ValueError('business_replication_cell_invalid')
    if kind == 'blob':
        return base64.b64decode(data, validate=True)
    if kind == 'integer':
        return int(data)
    if kind == 'real':
        return float.fromhex(data)
    if kind == 'text':
        return data
    raise ValueError('business_replication_cell_invalid')


def row_key(schema, row):
    if not isinstance(row, list) or len(row) != len(schema['columns']):
        raise ValueError('business_replication_row_invalid')
    for value in row:
        if cell(uncell(value)) != value:
            raise ValueError('business_replication_cell_noncanonical')
    keys = [row[index] for index in schema['primary_key']]
    if not keys or any(value[0] == 'null' for value in keys):
        raise ValueError('business_replication_nonnull_primary_key_required')
    return canonical(keys)


def policy(value):
    if not isinstance(value, dict) or set(value) != {'authority', 'tables', 'meta_keys', 'excluded'}:
        raise ValueError('business_replication_policy_invalid')
    if not isinstance(value['authority'], str) or not value['authority'] or len(value['authority']) > 200:
        raise ValueError('business_replication_authority_invalid')
    if (not isinstance(value['tables'], list) or not isinstance(value['meta_keys'], list)
            or not isinstance(value['excluded'], dict) or len(value['tables']) > 200):
        raise ValueError('business_replication_policy_invalid')
    tables = sorted(set(value['tables']) | set(CORE) | ({'meta'} if value['meta_keys'] else set()))
    for table in tables:
        name(table)
    for table, reason in value['excluded'].items():
        name(table)
        if table in tables or not isinstance(reason, str) or not reason.strip():
            raise ValueError('business_replication_exclusion_invalid')
    for key in value['meta_keys']:
        if (not isinstance(key, str) or not key or len(key) > 200
                or key in ('owner_context', 'cursor')
                or any(part in key.lower() for part in ('token', 'secret', 'credential', 'auth', 'account'))):
            raise ValueError('business_replication_sensitive_meta_key')
    return dict(authority=value['authority'], tables=tables,
                meta_keys=sorted(set(value['meta_keys'])), excluded=value['excluded'])


def schema(db, table):
    rows = db.execute('PRAGMA table_xinfo(' + quote(table) + ')').fetchall()
    if not rows:
        rows = db.execute('PRAGMA table_info(' + quote(table) + ')').fetchall()
    if not rows:
        return None
    columns = [dict(name=row[1], type=row[2], notnull=bool(row[3]), default=row[4], pk=row[5],
                    hidden=row[6] if len(row) > 6 else 0) for row in rows]
    pk = sorted((row['pk'], index) for index, row in enumerate(columns) if row['pk'])
    if not pk:
        raise ValueError('business_replication_explicit_primary_key_required:' + table)
    return dict(table=table, columns=columns, primary_key=[index for _, index in pk])


def seal(value):
    value['sha256'] = digest(value)
    return value


def hook_store_type(store_type):
    """Install the connection hook before an incumbent constructor can write.

    Persisted triggers survive restart. A ready-only instance hook would be
    too late for a constructor's session-copy or migration transaction.
    Subclasses retain the same constructor and business methods.
    """
    if not isinstance(store_type, type) or not callable(getattr(store_type, 'transaction', None)):
        raise ValueError('business_replication_store_type_required')
    original = store_type.transaction
    if getattr(original, '_business_replication_connection_hook', False):
        return

    @contextlib.contextmanager
    def transaction(self):
        with original(self) as db:
            db.execute('PRAGMA recursive_triggers=ON')
            yield db

    transaction._business_replication_connection_hook = True
    store_type.transaction = transaction


def hook_store(store):
    """Retain the original transaction/constructor; enable complete REPLACE capture.

    Every process writing covered tables must install this tiny connection
    hook. The persisted trigger guard rejects unhooked covered writes instead
    of silently omitting SQLite's secondary-UNIQUE replacement deletes.
    """
    if (getattr(store, '_business_replication_transaction_hook', False)
            or getattr(getattr(store.transaction, '__func__', store.transaction),
                       '_business_replication_connection_hook', False)):
        return
    original = store.transaction

    @contextlib.contextmanager
    def transaction():
        with original() as db:
            db.execute('PRAGMA recursive_triggers=ON')
            yield db

    store.transaction = transaction
    store._business_replication_transaction_hook = True


class Journal:
    def __init__(self, store, config, snapshot_ttl=300, clock=time.monotonic,
                 timer_factory=threading.Timer):
        self.store, self.config = store, policy(config)
        if not 10 <= snapshot_ttl <= 600:
            raise ValueError('business_replication_snapshot_ttl_invalid')
        self.snapshot_ttl, self.clock = snapshot_ttl, clock
        self.timer_factory = timer_factory
        self.snapshots, self.lock = {}, threading.Lock()

    @staticmethod
    def _head(db):
        # AUTOINCREMENT's committed high water survives journal deletion.
        sequence = db.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (PREFIX + 'events',)).fetchone()
        maximum = db.execute('SELECT COALESCE(MAX(sequence),0) FROM ' + PREFIX + 'events').fetchone()[0]
        return max(maximum, sequence[0] if sequence else 0)

    @staticmethod
    def _retention(db):
        row = db.execute('SELECT required_peers,keep_events,batch,retained_after FROM '
                         + PREFIX + 'retention WHERE singleton=1').fetchone()
        return dict(required_peers=json.loads(row[0]), keep_events=row[1], batch=row[2], retained_after=row[3])

    def _state(self, db):
        row = db.execute('SELECT * FROM ' + PREFIX + 'state WHERE singleton=1').fetchone()
        return dict(zip(('singleton', 'authority', 'incarnation', 'generation', 'start', 'policy', 'manifest'), row))

    def install(self):
        hook_store(self.store)
        with self.store.transaction() as db:
            for statement in (
                'CREATE TABLE IF NOT EXISTS ' + PREFIX + 'state(singleton INTEGER PRIMARY KEY,authority TEXT NOT NULL,incarnation TEXT NOT NULL,generation INTEGER NOT NULL,start INTEGER NOT NULL,policy TEXT NOT NULL,manifest TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS ' + PREFIX + 'events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,generation INTEGER NOT NULL,table_name TEXT NOT NULL,operation TEXT NOT NULL,schema_id TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS ' + PREFIX + 'cells(sequence INTEGER NOT NULL,position INTEGER NOT NULL,value BLOB,PRIMARY KEY(sequence,position)) WITHOUT ROWID',
                'CREATE TABLE IF NOT EXISTS ' + PREFIX + 'retention(singleton INTEGER PRIMARY KEY,required_peers TEXT NOT NULL,keep_events INTEGER NOT NULL,batch INTEGER NOT NULL,retained_after INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS ' + PREFIX + 'acks(peer TEXT PRIMARY KEY,authority TEXT NOT NULL,incarnation TEXT NOT NULL,generation INTEGER NOT NULL,cursor INTEGER NOT NULL,manifest_sha256 TEXT NOT NULL,receipt_id TEXT NOT NULL)',
            ):
                db.execute(statement)
            db.execute('INSERT OR IGNORE INTO ' + PREFIX + "retention VALUES(1,'[]',1000,200,0)")
            if db.execute('SELECT 1 FROM ' + PREFIX + 'state').fetchone():
                if self._state(db)['authority'] != self.config['authority']:
                    raise ValueError('business_replication_authority_changed')
                return
            self._refresh(db, initial=True)

    def suspend(self):
        """Explicit rollback aid: stop capture, keep journal, require rebootstrap."""
        with self.store.transaction() as db:
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
                                  (PREFIX + '%',)).fetchall():
                db.execute('DROP TRIGGER ' + quote(row[0]))
        return dict(capture_suspended=True, bootstrap_required=True, journal_retained=True)

    def refresh(self, config=None):
        """Explicit schema/config generation boundary; requires a new bootstrap."""
        selected = self.config if config is None else policy(config)
        if selected['authority'] != self.config['authority']:
            raise ValueError('business_replication_authority_changed')
        previous = self.config
        self.config = selected
        try:
            with self.store.transaction() as db:
                return self._refresh(db, initial=False)
        except BaseException:
            self.config = previous
            raise

    def _refresh(self, db, initial):
        previous = None if initial else self._state(db)
        actual = {}
        for table in self.config['tables']:
            value = schema(db, table)
            if value is not None:
                actual[table] = value
        generation = 1 if initial else previous['generation'] + 1
        head = self._head(db)
        manifest = dict(tables=actual, meta_keys=self.config['meta_keys'], excluded=self.config['excluded'],
                        external_dependencies=list(DEPENDENCIES))
        manifest['sha256'] = digest(manifest)
        # No executescript: DDL, triggers and the generation boundary share the
        # caller's existing writer transaction and roll back together.
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
                              (PREFIX + '%',)).fetchall():
            db.execute('DROP TRIGGER ' + quote(row[0]))
        for table, info in actual.items():
            self._triggers(db, table, info, generation)
        db.execute('INSERT OR REPLACE INTO ' + PREFIX + 'state VALUES(1,?,?,?,?,?,?)',
            (self.config['authority'], uuid.uuid4().hex if initial else previous['incarnation'],
             generation, head, canonical(self.config), canonical(manifest)))
        return dict(generation=generation, bootstrap_required=True, manifest_sha256=manifest['sha256'])

    def _filter(self, table, alias, db, meta_keys=None):
        if table != 'meta':
            return '1'
        values = ','.join(db.execute('SELECT quote(?)', (key,)).fetchone()[0]
                          for key in (self.config['meta_keys'] if meta_keys is None else meta_keys))
        return alias + '."key" IN (' + values + ')'

    def _triggers(self, db, table, info, generation):
        sid = digest(info)
        def emit(operation, alias):
            condition = self._filter(table, alias, db)
            cells = ','.join('(last_insert_rowid(),{},{}.{} )'.format(index, alias, quote(col['name']))
                             for index, col in enumerate(info['columns']))
            # WITHOUT ROWID cells cannot change last_insert_rowid(). BLOB
            # affinity preserves each SQLite value's actual storage type.
            return ("INSERT INTO " + PREFIX + "events(generation,table_name,operation,schema_id) SELECT "
                + str(generation) + ",'" + table + "','" + operation + "','" + sid + "' WHERE " + condition + '; '
                + 'INSERT INTO ' + PREFIX + 'cells(sequence,position,value) SELECT * FROM (VALUES ' + cells + ') WHERE ' + condition + '; ')
        for action in ('INSERT', 'UPDATE', 'DELETE'):
            body = (emit('delete', 'OLD') if action in ('UPDATE', 'DELETE') else '')
            body += emit('upsert', 'NEW') if action in ('UPDATE', 'INSERT') else ''
            aliases = ('OLD', 'NEW') if action == 'UPDATE' else ('NEW',) if action == 'INSERT' else ('OLD',)
            condition = ' OR '.join(self._filter(table, alias, db) for alias in aliases)
            guard = ("SELECT RAISE(ABORT,'business_replication_writer_hook_required') WHERE (" + condition
                     + ') AND (SELECT recursive_triggers FROM pragma_recursive_triggers)<>1; ')
            db.execute('CREATE TRIGGER ' + quote(PREFIX + table + '_' + action.lower())
                       + ' AFTER ' + action + ' ON ' + quote(table) + ' BEGIN ' + guard + body + 'END')

    def _description(self, db):
        state = self._state(db)
        manifest = json.loads(state['manifest'])
        current = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                   if not row[0].startswith(PREFIX)}
        unknown = sorted(current - set(manifest['tables']) - set(manifest['excluded']) - {'meta'})
        issues = ['uncovered_table:' + table for table in unknown]
        issues += ['missing_table:' + table for table in self.config['tables'] if table not in manifest['tables']]
        if 'meta' in current and not manifest['meta_keys'] and 'meta' not in manifest['excluded']:
            issues.append('meta_business_keys_not_declared')
        if state['policy'] != canonical(self.config):
            issues.append('configured_coverage_refresh_required')
        for table, expected in manifest['tables'].items():
            try:
                actual = schema(db, table)
            except ValueError:
                actual = None
            if actual != expected:
                issues.append('schema_refresh_required:' + table)
            for action in ('insert', 'update', 'delete'):
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                    (PREFIX + table + '_' + action,)).fetchone():
                    issues.append('missing_trigger:' + table + ':' + action)
        head = self._head(db)
        retention = self._retention(db)
        return dict(format=FORMAT, authority=state['authority'], incarnation=state['incarnation'],
            generation=state['generation'], generation_start=state['start'], head=head, manifest=manifest,
            retained_after=retention['retained_after'], ack_protocol=ACK_PROTOCOL,
            coverage_issues=issues, ledger_coverage='declared_tables_current' if not issues else 'degraded',
            redundancy='degraded', execution_authorized=False)

    def status(self):
        with self.store.transaction() as db:
            return self._description(db)

    def configure_retention(self, required_peers, keep_events=1000, batch=200):
        if (not isinstance(required_peers, list) or len(required_peers) > 128
                or any(not isinstance(peer, str) or not 1 <= len(peer) <= 200
                       or any(ord(character) < 32 for character in peer) for peer in required_peers)
                or len(set(required_peers)) != len(required_peers)
                or type(keep_events) is not int or not 1 <= keep_events <= 2147483647
                or type(batch) is not int or not 1 <= batch <= 2000):
            raise ValueError('business_replication_retention_config_invalid')
        with self.store.transaction() as db:
            db.execute('UPDATE ' + PREFIX + 'retention SET required_peers=?,keep_events=?,batch=? WHERE singleton=1',
                       (canonical(sorted(required_peers)), keep_events, batch))
            return dict(self._retention(db), ack_protocol=ACK_PROTOCOL, coverage_generation_changed=False,
                        execution_authorized=False)

    def acknowledge(self, peer, payload):
        if (not isinstance(peer, str) or not peer or not isinstance(payload, dict)
                or set(payload) != ACK_FIELDS | {'receipt_id'}):
            raise ValueError('business_replication_ack_invalid')
        content = {key: payload[key] for key in ACK_FIELDS}
        if digest(content) != payload['receipt_id']:
            raise ValueError('business_replication_ack_receipt_invalid')
        with self.lock:
            self._cleanup()
            with self.store.transaction() as db:
                current = self._description(db)
                if (payload['authority'] != current['authority']
                        or payload['incarnation'] != current['incarnation']
                        or type(payload['generation']) is not int
                        or payload['generation'] != current['generation']
                        or payload['manifest_sha256'] != current['manifest']['sha256']):
                    raise ValueError('business_replication_ack_source_mismatch')
                cursor = payload['cursor']
                if (type(cursor) is not int or not current['generation_start'] <= cursor <= current['head']):
                    raise ValueError('business_replication_ack_cursor_invalid')
                if cursor < current['retained_after']:
                    raise ValueError('business_replication_bootstrap_required')
                known = db.execute('SELECT incarnation,generation,manifest_sha256,cursor,receipt_id FROM '
                                   + PREFIX + 'acks WHERE peer=?', (peer,)).fetchone()
                same_source = known and tuple(known[:3]) == (
                    current['incarnation'], current['generation'], current['manifest']['sha256'])
                if same_source and cursor < known[3]:
                    raise ValueError('business_replication_ack_would_regress')
                duplicate = bool(same_source and known[4] == payload['receipt_id'])
                db.execute('INSERT OR REPLACE INTO ' + PREFIX + 'acks VALUES(?,?,?,?,?,?,?)',
                    (peer, current['authority'], current['incarnation'], current['generation'], cursor,
                     current['manifest']['sha256'], payload['receipt_id']))
                result = self._prune(db, current)
                return dict(result, receipt_id=payload['receipt_id'], duplicate=duplicate,
                    assertion='authenticated_receiver_assertion', remote_disk_independently_verified=False,
                    execution_authorized=False)

    def _prune(self, db, current):
        config = self._retention(db)
        result = dict(pruned_events=0, retained_after=config['retained_after'], head=current['head'])
        if not config['required_peers']:
            return dict(result, retention='disabled')
        cursors = []
        for peer in config['required_peers']:
            row = db.execute('SELECT cursor FROM ' + PREFIX + 'acks WHERE peer=? AND authority=? '
                             'AND incarnation=? AND generation=? AND manifest_sha256=?',
                (peer, current['authority'], current['incarnation'], current['generation'],
                 current['manifest']['sha256'])).fetchone()
            if row is None:
                return dict(result, retention='required_ack_missing')
            cursors.append(row[0])
        boundary = db.execute('SELECT sequence FROM ' + PREFIX + 'events ORDER BY sequence DESC LIMIT 1 OFFSET ?',
                              (config['keep_events'] - 1,)).fetchone()
        if boundary is None:
            return dict(result, retention='minimum_events_retained')
        ceiling = min(min(cursors), boundary[0] - 1)
        if self.snapshots:
            ceiling = min(ceiling, min(value['description']['baseline_cursor'] for value in self.snapshots.values()))
        rows = db.execute('SELECT sequence FROM ' + PREFIX + 'events WHERE sequence<=? ORDER BY sequence LIMIT ?',
                          (ceiling, config['batch'])).fetchall()
        if not rows:
            return dict(result, retention='watermark_retained')
        floor = rows[-1][0]
        db.execute('DELETE FROM ' + PREFIX + 'cells WHERE sequence<=?', (floor,))
        db.execute('DELETE FROM ' + PREFIX + 'events WHERE sequence<=?', (floor,))
        db.execute('UPDATE ' + PREFIX + 'retention SET retained_after=? WHERE singleton=1', (floor,))
        return dict(pruned_events=len(rows), retained_after=floor, head=current['head'], retention='bounded_prune')

    @staticmethod
    def _bounds(limit, max_bytes):
        if type(limit) is not int or not 1 <= limit <= 500 or type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_BYTES:
            raise ValueError('business_replication_page_bounds_invalid')

    def export(self, after, incarnation, generation, limit=100, max_bytes=MAX_BYTES):
        self._bounds(limit, max_bytes)
        if type(after) is not int or after < 0:
            raise ValueError('business_replication_cursor_invalid')
        with self.store.transaction() as db:
            output = self._description(db)
            if incarnation != output['incarnation'] or generation != output['generation']:
                raise ValueError('business_replication_bootstrap_required')
            if after < output['retained_after']:
                raise ValueError('business_replication_bootstrap_required')
            if not output['generation_start'] <= after <= output['head']:
                raise ValueError('business_replication_cursor_out_of_range')
            output.update(kind='changes', after=after, cursor=after, records=[])
            size = len(canonical(output))
            manifest = output['manifest']
            for row in db.execute('SELECT * FROM ' + PREFIX + 'events WHERE sequence>? ORDER BY sequence LIMIT ?', (after, limit)):
                sequence, gen, table, operation, sid = row
                if sequence != output['cursor'] + 1:
                    raise ValueError('business_replication_bootstrap_required')
                if gen != generation:
                    raise ValueError('business_replication_bootstrap_required')
                info = manifest['tables'][table]
                if sid != digest(info):
                    raise ValueError('business_replication_schema_conflict')
                values = [cell(item[0]) for item in db.execute('SELECT value FROM ' + PREFIX + 'cells WHERE sequence=? ORDER BY position', (sequence,))]
                row_key(info, values)
                record = dict(sequence=sequence, table=table, operation=operation, row=values)
                length = len(canonical(record))
                if size + length > max_bytes:
                    if not output['records']:
                        raise ValueError('business_replication_row_exceeds_page_budget')
                    break
                output['records'].append(record)
                output['cursor'], size = sequence, size + length
            if not output['records'] and after < output['head']:
                raise ValueError('business_replication_bootstrap_required')
            return seal(output)

    def _cleanup(self):
        now = self.clock()
        for identity, value in list(self.snapshots.items()):
            if value['expires'] <= now:
                self._close_snapshot(identity)

    def _close_snapshot(self, identity):
        value = self.snapshots.pop(identity, None)
        if value is None:
            return
        timer = value.get('timer')
        if timer is not None:
            timer.cancel()
        try:
            if value['iterator'] is not None:
                value['iterator'].close()
        finally:
            try:
                if value['db'] is not None:
                    value['db'].close()
            finally:
                value.clear()  # release pending rows, last wire page and raw BLOB cache

    def _schedule_expiry(self, identity):
        value = self.snapshots[identity]
        timer = self.timer_factory(max(0, value['expires'] - self.clock()),
                                   lambda: self._expire_snapshot(identity))
        timer.daemon = True
        value['timer'] = timer
        timer.start()

    def _expire_snapshot(self, identity):
        with self.lock:
            value = self.snapshots.get(identity)
            if value is None:
                return
            if value['expires'] > self.clock():
                # An early timer wake retains the SAME absolute deadline.
                self._schedule_expiry(identity)
            else:
                self._close_snapshot(identity)

    def close(self):
        with self.lock:
            for identity in list(self.snapshots):
                self._close_snapshot(identity)

    def _snapshot_owned(self, owner, snapshot_id):
        value = self.snapshots.get(snapshot_id)
        if not value or value['owner'] != owner:
            raise ValueError('business_replication_snapshot_expired_or_unowned')
        # Another Journal instance may prune the same source. Never finish an
        # unreadable bridge silently, even when that instance cannot see our
        # in-memory reader lease. This read is outside the pinned snapshot.
        db = sqlite3.connect(Path(self.store.path).absolute().as_uri() + '?mode=ro', uri=True, timeout=10)
        try:
            floor = self._retention(db)['retained_after']
        finally:
            db.close()
        if floor > value['description']['baseline_cursor']:
            self._close_snapshot(snapshot_id)
            raise ValueError('business_replication_bootstrap_required')
        return value

    def snapshot_begin(self, owner, blob_mode='inline'):
        if blob_mode not in ('inline', 'references'):
            raise ValueError('business_replication_blob_mode_invalid')
        with self.lock:
            self._cleanup()
            if len(self.snapshots) >= 2:
                raise ValueError('business_replication_snapshot_capacity')
            db = sqlite3.connect('file:' + self.store.path + '?mode=ro', uri=True, timeout=10, check_same_thread=False)
            identity = None
            try:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                description = self._description(db)
                # Drift is disclosed, not a business-write outage. Explicit
                # refresh is required before a new whole-row baseline.
                if any(issue.startswith(('schema_refresh_required:', 'missing_trigger:')) for issue in description['coverage_issues']):
                    raise ValueError('business_replication_coverage_refresh_required')
                counts = {}
                for table in description['manifest']['tables']:
                    condition = self._filter(table, quote(table), db, description['manifest']['meta_keys'])
                    counts[table] = db.execute('SELECT COUNT(*) FROM ' + quote(table) + ' WHERE ' + condition).fetchone()[0]
                identity = uuid.uuid4().hex
                description.update(kind='snapshot', snapshot_id=identity, baseline_cursor=description['head'],
                                   counts=counts, total_rows=sum(counts.values()))
                if blob_mode == 'references':
                    description['blob_mode'] = blob_mode
                self.snapshots[identity] = dict(db=db, description=description, owner=owner,
                    expires=self.clock() + self.snapshot_ttl, after=0, table=0, offset=0,
                    iterator=None, pending=None, last=None, blobs={})
                self._schedule_expiry(identity)
                output = dict(description, after=0, cursor=0, rows=[], done=False)
                if blob_mode == 'references':
                    output['blob_sizes'] = {}
                return seal(output)
            except BaseException:
                if identity in self.snapshots:
                    self._close_snapshot(identity)
                else:
                    db.close()
                raise

    def snapshot_page(self, owner, snapshot_id, after, limit=100, max_bytes=MAX_BYTES):
        self._bounds(limit, max_bytes)
        with self.lock:
            self._cleanup()
            value = self._snapshot_owned(owner, snapshot_id)
            if value['last'] and value['last']['after'] == after:
                return value['last']
            if type(after) is not int or after != value['after'] or value['db'] is None:
                raise ValueError('business_replication_snapshot_cursor_invalid')
            output = dict(value['description'], after=after, cursor=after, rows=[], done=False)
            references = output.get('blob_mode') == 'references'
            blobs, blob_bytes = {}, 0
            if references:
                output['blob_sizes'] = {}
            tables = sorted(output['manifest']['tables'])
            size = len(canonical(output))
            while value['table'] < len(tables) and len(output['rows']) < limit:
                table = tables[value['table']]
                if value['offset'] == output['counts'][table]:
                    if value['iterator'] is not None:
                        value['iterator'].close()
                    value['iterator'], value['pending'] = None, None
                    value['table'], value['offset'] = value['table'] + 1, 0
                    continue
                info, db = output['manifest']['tables'][table], value['db']
                order = ','.join(quote(info['columns'][index]['name']) for index in info['primary_key'])
                selected = ','.join(quote(col['name']) for col in info['columns'])
                if value['iterator'] is None:
                    value['iterator'] = db.execute('SELECT ' + selected + ' FROM ' + quote(table) + ' WHERE '
                        + self._filter(table, quote(table), db, output['manifest']['meta_keys']) + ' ORDER BY ' + order)
                row = value['pending'] if value['pending'] is not None else value['iterator'].fetchone()
                value['pending'] = row
                if references and any(isinstance(item, bytes) and len(item) > MAX_BLOB_BYTES for item in row):
                    if output['rows']:
                        break
                    raise ValueError('business_replication_blob_exceeds_fetch_budget')
                values = [cell(item) for item in row]
                row_key(info, values)
                row_blobs = {}
                if references:
                    for index, item in enumerate(row):
                        if isinstance(item, bytes):
                            identity = hashlib.sha256(item).hexdigest()
                            values[index] = ['blob_ref', identity]
                            row_blobs[identity] = item
                record = dict(index=output['cursor'] + 1, table=table, row=values)
                length = len(canonical(record))
                new_blobs = {key: item for key, item in row_blobs.items() if key not in blobs}
                added_bytes = sum(map(len, new_blobs.values()))
                # Account for the per-page digest/size map on the wire as well.
                if references:
                    length += sum(len(canonical(key)) + len(str(len(item))) + 2
                                  for key, item in new_blobs.items())
                if size + length > max_bytes or blob_bytes + added_bytes > MAX_BYTES:
                    if not output['rows']:
                        raise ValueError('business_replication_row_exceeds_page_budget')
                    break
                output['rows'].append(record)
                blobs.update(new_blobs)
                blob_bytes += added_bytes
                if references:
                    output['blob_sizes'].update({key: len(item) for key, item in new_blobs.items()})
                output['cursor'], size = record['index'], size + length
                value['offset'] += 1
                value['pending'] = None
            value['after'] = output['cursor']
            output['done'] = output['cursor'] == output['total_rows']
            if output['done']:
                if value['iterator'] is not None:
                    value['iterator'].close()
                    value['iterator'] = None
                value['db'].close()
                value['db'] = None
            value['last'] = seal(output)
            # Only this exact admitted page may service authenticated fetches.
            value['blobs'] = blobs
            return value['last']

    def snapshot_blob(self, owner, snapshot_id, sha256):
        with self.lock:
            self._cleanup()
            if not isinstance(snapshot_id, str):
                raise ValueError('business_replication_snapshot_expired_or_unowned')
            value = self._snapshot_owned(owner, snapshot_id)
            if value['description'].get('blob_mode') != 'references':
                raise ValueError('business_replication_snapshot_expired_or_unowned')
            if not isinstance(sha256, str) or sha256 not in value['blobs']:
                raise ValueError('business_replication_blob_not_in_current_page')
            raw = value['blobs'][sha256]
            return dict(snapshot_id=snapshot_id, sha256=sha256, size=len(raw),
                        base64=base64.b64encode(raw).decode('ascii'))

    def snapshot_end(self, owner, snapshot_id):
        with self.lock:
            value = self.snapshots.get(snapshot_id)
            if value and value['owner'] == owner:
                self._close_snapshot(snapshot_id)
            return {'closed': True}
