"""Separate durable query-only business projection; never a runnable Store.

No replica row, status, epoch or merged metadata can claim work or authorize an
external effect. Incarnations and conflicting inputs remain separate evidence.
"""
import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .business_journal import FORMAT, MAX_BYTES, MAX_BLOB_BYTES, canonical, cell, digest, row_key, uncell


class ReplicaConflict(ValueError):
    pass


def checked_page(page, authority):
    if not isinstance(page, dict) or page.get('format') != FORMAT or page.get('authority') != authority:
        raise ReplicaConflict('business_replica_source_mismatch')
    unsigned = dict(page)
    signature = unsigned.pop('sha256', None)
    if digest(unsigned) != signature:
        raise ReplicaConflict('business_replica_page_content_conflict')
    manifest = dict(page['manifest'])
    expected = manifest.pop('sha256')
    if digest(manifest) != expected or not page.get('incarnation'):
        raise ReplicaConflict('business_replica_manifest_conflict')
    if any(type(page.get(key)) is not int or page[key] < 0 for key in ('generation', 'head', 'after', 'cursor')):
        raise ReplicaConflict('business_replica_cursor_invalid')
    return page


def checked_blob(raw, sha256, size):
    if (not isinstance(raw, bytes) or len(raw) != size
            or hashlib.sha256(raw).hexdigest() != sha256):
        raise ReplicaConflict('business_replica_blob_content_conflict')
    return raw


def resolved_snapshot_rows(page, resolver):
    """Resolve only after checking the original wire hash; never reseal a page."""
    sizes = page.get('blob_sizes')
    if (not isinstance(sizes, dict) or not isinstance(page.get('rows'), list)
            or len(page['rows']) > 500
            or any(not isinstance(key, str) or len(key) != 64
                   or any(character not in '0123456789abcdef' for character in key)
                   or type(size) is not int or not 0 <= size <= MAX_BLOB_BYTES
                   for key, size in sizes.items())
            or sum(sizes.values()) > MAX_BYTES):
        raise ReplicaConflict('business_replica_blob_description_invalid')
    referenced = set()
    for item in page['rows']:
        info, row = page['manifest']['tables'][item['table']], item['row']
        if not isinstance(row, list) or len(row) != len(info['columns']):
            raise ReplicaConflict('business_replica_blob_row_invalid')
        for value in row:
            if not isinstance(value, list) or len(value) != 2:
                raise ReplicaConflict('business_replica_blob_row_invalid')
            if value[0] == 'blob_ref':
                if not isinstance(value[1], str) or value[1] not in sizes:
                    raise ReplicaConflict('business_replica_blob_description_invalid')
                referenced.add(value[1])
    if referenced != set(sizes):
        raise ReplicaConflict('business_replica_blob_description_invalid')
    if referenced and not callable(resolver):
        raise ReplicaConflict('business_replica_blob_dependency_missing')
    blobs = {key: cell(checked_blob(resolver(key, sizes[key]), key, sizes[key])) for key in referenced}
    return [dict(item, row=[blobs[value[1]] if value[0] == 'blob_ref' else value
                            for value in item['row']]) for item in page['rows']]


class Replica:
    def __init__(self, path):
        self.path = Path(path).absolute()
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('business_replica_private_regular_path_required')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Refuse a Store or any unrelated existing DB BEFORE setting PRAGMAs or
        # making schema changes. A replica is never restored over an authority.
        if self.path.exists():
            db = sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True)
            try:
                names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if names and ('replica_identity' not in names or any(not name.startswith('replica_') for name in names)):
                    raise ValueError('business_replica_refuses_existing_authority')
                if names and db.execute('SELECT format FROM replica_identity').fetchall() != [(FORMAT,)]:
                    raise ValueError('business_replica_format_changed')
            finally:
                db.close()
        with self.transaction() as db:
            for statement in (
                'CREATE TABLE IF NOT EXISTS replica_identity(format TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS replica_sources(authority TEXT,incarnation TEXT,generation INTEGER,cursor INTEGER,head INTEGER,manifest TEXT,coverage TEXT,PRIMARY KEY(authority,incarnation))',
                'CREATE TABLE IF NOT EXISTS replica_rows(authority TEXT,incarnation TEXT,table_name TEXT,row_key TEXT,payload TEXT,tombstone INTEGER NOT NULL,sequence INTEGER NOT NULL,PRIMARY KEY(authority,incarnation,table_name,row_key))',
                'CREATE TABLE IF NOT EXISTS replica_receipts(authority TEXT,incarnation TEXT,sequence INTEGER,fingerprint TEXT NOT NULL,PRIMARY KEY(authority,incarnation,sequence))',
                'CREATE TABLE IF NOT EXISTS replica_snapshots(authority TEXT,incarnation TEXT,snapshot_id TEXT,generation INTEGER,cursor INTEGER,baseline INTEGER,description TEXT,PRIMARY KEY(authority,incarnation))',
                'CREATE TABLE IF NOT EXISTS replica_staging(authority TEXT,incarnation TEXT,table_name TEXT,row_key TEXT,payload TEXT,PRIMARY KEY(authority,incarnation,table_name,row_key))',
                'CREATE TABLE IF NOT EXISTS replica_snapshot_receipts(snapshot_id TEXT,after_cursor INTEGER,fingerprint TEXT,PRIMARY KEY(snapshot_id,after_cursor))',
                'CREATE TABLE IF NOT EXISTS replica_conflicts(id INTEGER PRIMARY KEY,authority TEXT,reason TEXT,input TEXT NOT NULL)',
            ):
                db.execute(statement)
            if not db.execute('SELECT 1 FROM replica_identity').fetchone():
                db.execute('INSERT INTO replica_identity VALUES(?)', (FORMAT,))
        os.chmod(str(self.path), 0o600)

    @contextlib.contextmanager
    def transaction(self):
        db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _source(db, authority, incarnation):
        return db.execute('SELECT * FROM replica_sources WHERE authority=? AND incarnation=?', (authority, incarnation)).fetchone()

    def state(self, authority, incarnation):
        with self.transaction() as db:
            source = self._source(db, authority, incarnation)
            if not source:
                return None
            return self._state_value(source)

    @staticmethod
    def _state_value(source):
        value = dict(source)
        value['manifest'], value['coverage_issues'] = json.loads(value['manifest']), json.loads(value.pop('coverage'))
        value.update(caught_up_to_observed_head=value['cursor'] == value['head'],
                     execution_authorized=False, redundancy='degraded')
        return value

    def apply(self, page, authority, blob_resolver=None):
        try:
            checked_page(page, authority)
            rows = None
            if page['kind'] == 'snapshot' and page.get('blob_mode', 'inline') == 'references':
                # A lost apply response must replay without fetching dependencies
                # from an expired source page. Network/cache reads take place
                # outside the SQLite write transaction.
                with self.transaction() as db:
                    duplicate = self._snapshot_duplicate(db, page)
                if duplicate is not None:
                    return duplicate
                rows = resolved_snapshot_rows(page, blob_resolver)
            with self.transaction() as db:
                if page['kind'] == 'snapshot':
                    return self._snapshot(db, page, rows)
                if page['kind'] == 'changes':
                    return self._changes(db, page)
                raise ReplicaConflict('business_replica_page_kind_invalid')
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as error:
            # Conflict evidence persists in its own transaction AFTER the
            # attempted apply rolled back. Existing rows/unknowns stay intact.
            reason = str(error) if isinstance(error, ReplicaConflict) else 'business_replica_invalid_page'
            with self.transaction() as db:
                db.execute('INSERT INTO replica_conflicts(authority,reason,input) VALUES(?,?,?)',
                           (authority, reason, canonical(page)))
            raise ReplicaConflict(reason) from None

    @staticmethod
    def _snapshot_duplicate(db, page):
        receipt = db.execute('SELECT fingerprint FROM replica_snapshot_receipts WHERE snapshot_id=? AND after_cursor=?',
                             (page['snapshot_id'], page['after'])).fetchone()
        if receipt:
            if receipt[0] != page['sha256']:
                raise ReplicaConflict('business_replica_snapshot_replay_conflict')
            return {'applied': False, 'duplicate': True, 'cursor': page['cursor']}
        return None

    def _snapshot(self, db, page, resolved_rows=None):
        authority, incarnation, identity = page['authority'], page['incarnation'], page['snapshot_id']
        mode = page.get('blob_mode', 'inline')
        if (mode not in ('inline', 'references')
                or type(page['done']) is not bool or type(page['baseline_cursor']) is not int
                or type(page['total_rows']) is not int or page['baseline_cursor'] != page['head']
                or set(page['counts']) != set(page['manifest']['tables'])
                or any(type(count) is not int or count < 0 for count in page['counts'].values())
                or sum(page['counts'].values()) != page['total_rows']):
            raise ReplicaConflict('business_replica_snapshot_description_invalid')
        if db.execute('SELECT 1 FROM replica_sources WHERE authority=? AND incarnation<>?', (authority, incarnation)).fetchone():
            raise ReplicaConflict('business_replica_incarnation_changed_requires_separate_replica')
        duplicate = self._snapshot_duplicate(db, page)
        if duplicate is not None:
            return duplicate
        source = self._source(db, authority, incarnation)
        if source and (page['baseline_cursor'] < source['cursor'] or page['head'] < source['head']):
            raise ReplicaConflict('business_replica_snapshot_would_regress')
        description = {key: page[key] for key in ('authority', 'incarnation', 'snapshot_id', 'generation',
            'baseline_cursor', 'manifest', 'counts', 'total_rows')}
        description['blob_mode'] = mode
        staging = db.execute('SELECT * FROM replica_snapshots WHERE authority=? AND incarnation=?', (authority, incarnation)).fetchone()
        if not staging or staging['snapshot_id'] != identity:
            if page['after'] != 0:
                raise ReplicaConflict('business_replica_snapshot_prefix_missing')
            db.execute('DELETE FROM replica_staging WHERE authority=? AND incarnation=?', (authority, incarnation))
            db.execute('INSERT OR REPLACE INTO replica_snapshots VALUES(?,?,?,?,?,?,?)',
                (authority, incarnation, identity, page['generation'], 0, page['baseline_cursor'], canonical(description)))
            cursor = 0
        else:
            if staging['description'] != canonical(description):
                raise ReplicaConflict('business_replica_snapshot_description_changed')
            cursor = staging['cursor']
        if page['after'] != cursor:
            raise ReplicaConflict('business_replica_snapshot_cursor_gap')
        for item in (page['rows'] if resolved_rows is None else resolved_rows):
            if type(item['index']) is not int or item['index'] != cursor + 1:
                raise ReplicaConflict('business_replica_snapshot_row_gap')
            info = page['manifest']['tables'][item['table']]
            key = row_key(info, item['row'])
            db.execute('INSERT INTO replica_staging VALUES(?,?,?,?,?)',
                       (authority, incarnation, item['table'], key, self._payload(info, item['row'])))
            cursor += 1
        if page['cursor'] != cursor or cursor > page['total_rows']:
            raise ReplicaConflict('business_replica_snapshot_count_conflict')
        db.execute('UPDATE replica_snapshots SET cursor=? WHERE authority=? AND incarnation=?', (cursor, authority, incarnation))
        if page['done']:
            if cursor != page['total_rows']:
                raise ReplicaConflict('business_replica_snapshot_incomplete')
            for table, count in page['counts'].items():
                if db.execute('SELECT COUNT(*) FROM replica_staging WHERE authority=? AND incarnation=? AND table_name=?',
                              (authority, incarnation, table)).fetchone()[0] != count:
                    raise ReplicaConflict('business_replica_snapshot_table_count_conflict')
            # Absence in a complete newer baseline is a durable tombstone too.
            # Keep historical fields for queries; stale frames cannot resurrect
            # rows removed before this bootstrap's source cursor.
            db.execute('UPDATE replica_rows SET tombstone=1,sequence=? WHERE authority=? AND incarnation=?',
                       (page['baseline_cursor'], authority, incarnation))
            db.execute('INSERT OR REPLACE INTO replica_rows SELECT authority,incarnation,table_name,row_key,payload,0,? FROM replica_staging WHERE authority=? AND incarnation=?',
                       (page['baseline_cursor'], authority, incarnation))
            db.execute('INSERT OR REPLACE INTO replica_sources VALUES(?,?,?,?,?,?,?)',
                (authority, incarnation, page['generation'], page['baseline_cursor'], page['head'],
                 canonical(page['manifest']), canonical(page['coverage_issues'])))
            db.execute('DELETE FROM replica_staging WHERE authority=? AND incarnation=?', (authority, incarnation))
            db.execute('DELETE FROM replica_snapshots WHERE authority=? AND incarnation=?', (authority, incarnation))
        db.execute('INSERT INTO replica_snapshot_receipts VALUES(?,?,?)', (identity, page['after'], page['sha256']))
        return {'applied': True, 'baseline_published': page['done'], 'cursor': cursor}

    def _changes(self, db, page):
        authority, incarnation = page['authority'], page['incarnation']
        source = self._source(db, authority, incarnation)
        if (not source or source['generation'] != page['generation']
                or source['manifest'] != canonical(page['manifest'])):
            raise ReplicaConflict('business_replica_bootstrap_required')
        if (page['after'] > source['cursor']
                or (page['head'] < source['head'] and page['cursor'] > source['cursor'])):
            raise ReplicaConflict('business_replica_cursor_gap_or_regression')
        cursor, sequence = source['cursor'], page['after']
        for item in page['records']:
            sequence += 1
            if type(item['sequence']) is not int or item['sequence'] != sequence or item['operation'] not in ('upsert', 'delete'):
                raise ReplicaConflict('business_replica_event_gap')
            fingerprint = digest(item)
            if sequence <= cursor:
                known = db.execute('SELECT fingerprint FROM replica_receipts WHERE authority=? AND incarnation=? AND sequence=?',
                                   (authority, incarnation, sequence)).fetchone()
                if not known or known[0] != fingerprint:
                    raise ReplicaConflict('business_replica_event_replay_conflict')
                continue
            info = page['manifest']['tables'][item['table']]
            key = row_key(info, item['row'])
            db.execute('INSERT OR REPLACE INTO replica_rows VALUES(?,?,?,?,?,?,?)',
                (authority, incarnation, item['table'], key, self._payload(info, item['row']),
                 int(item['operation'] == 'delete'), sequence))
            db.execute('INSERT INTO replica_receipts VALUES(?,?,?,?)', (authority, incarnation, sequence, fingerprint))
            cursor = sequence
        if page['cursor'] != sequence or page['cursor'] > page['head']:
            raise ReplicaConflict('business_replica_page_cursor_conflict')
        head = max(source['head'], page['head'])
        coverage = canonical(page['coverage_issues']) if page['head'] >= source['head'] else source['coverage']
        db.execute('UPDATE replica_sources SET cursor=?,head=?,coverage=? WHERE authority=? AND incarnation=?',
                   (cursor, head, coverage, authority, incarnation))
        return {'applied': True, 'cursor': cursor, 'caught_up_to_observed_head': cursor == head}

    def query(self, authority, incarnation, table, limit=100, after_key='', tombstones=False):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('business_replica_query_limit_invalid')
        with self.transaction() as db:
            source = self._source(db, authority, incarnation)
            state = self._state_value(source) if source else None
            if not state or table not in state['manifest']['tables']:
                raise ValueError('business_replica_baseline_or_table_unavailable')
            rows = db.execute('SELECT row_key,payload,tombstone,sequence FROM replica_rows WHERE authority=? AND incarnation=? AND table_name=? AND row_key>?'
                + ('' if tombstones else ' AND tombstone=0') + ' ORDER BY row_key LIMIT ?',
                (authority, incarnation, table, after_key, limit)).fetchall()
        output = []
        for row in rows:
            payload = json.loads(row['payload'])
            output.append(dict(key=row['row_key'], tombstone=bool(row['tombstone']), sequence=row['sequence'],
                value=dict(zip(payload['columns'], [uncell(value) for value in payload['row']]))))
        return dict(state=state, rows=output)

    @staticmethod
    def _payload(info, row):
        return canonical(dict(columns=[col['name'] for col in info['columns']], row=row))


def snapshot_blob_resolver(client, cache, snapshot_id):
    def resolve(sha256, size):
        raw = cache.get(sha256, size)
        if raw is not None:
            return checked_blob(raw, sha256, size)
        result = client.request('/v1/business-replica/snapshot',
            dict(action='blob', snapshot_id=snapshot_id, sha256=sha256))
        if (not isinstance(result, dict) or set(result) != {'snapshot_id', 'sha256', 'size', 'base64'}
                or result['snapshot_id'] != snapshot_id or result['sha256'] != sha256
                or type(result['size']) is not int or result['size'] != size
                or not isinstance(result['base64'], str)
                or len(result['base64']) > ((MAX_BLOB_BYTES + 2) // 3) * 4):
            raise ReplicaConflict('business_replica_blob_response_invalid')
        try:
            raw = base64.b64decode(result['base64'], validate=True)
        except ValueError:
            raise ReplicaConflict('business_replica_blob_response_invalid') from None
        checked_blob(raw, sha256, size)
        cache.put(sha256, raw)
        return raw
    return resolve


def pull(client, replica, authority, pages=100, limit=100, blob_cache=None):
    """One bounded authenticated synchronization; no task/model health requests."""
    if type(pages) is not int or not 1 <= pages <= 10000:
        raise ValueError('business_replica_page_count_invalid')
    info = client.request('/v1/business-replica/status', {})
    if info['authority'] != authority:
        raise ReplicaConflict('business_replica_source_mismatch')
    state = replica.state(authority, info['incarnation'])
    if not state or state['generation'] != info['generation'] or state['manifest'] != info['manifest']:
        request = {'action': 'begin'}
        if blob_cache is not None:
            request['blob_mode'] = 'references'
        start = client.request('/v1/business-replica/snapshot', request)
        cursor = 0
        try:
            checked_page(start, authority)
            if blob_cache is not None and start.get('blob_mode') != 'references':
                raise ReplicaConflict('business_replica_blob_mode_not_accepted')
            for _ in range(pages):
                page = client.request('/v1/business-replica/snapshot', dict(action='page', snapshot_id=start['snapshot_id'], after=cursor, limit=limit))
                resolver = (snapshot_blob_resolver(client, blob_cache, start['snapshot_id'])
                            if blob_cache is not None else None)
                replica.apply(page, authority, blob_resolver=resolver)
                cursor = page['cursor']
                if page['done']:
                    break
            else:
                return {'status': 'bootstrap_incomplete', 'rows_staged': cursor, 'execution_authorized': False}
        finally:
            client.request('/v1/business-replica/snapshot', dict(action='end', snapshot_id=start['snapshot_id']))
        state = replica.state(authority, info['incarnation'])
    for _ in range(pages):
        page = client.request('/v1/business-replica/export', dict(after=state['cursor'], incarnation=info['incarnation'], generation=info['generation'], limit=limit))
        replica.apply(page, authority)
        state = replica.state(authority, info['incarnation'])
        if state['caught_up_to_observed_head']:
            return state
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--authority', required=True)
    parser.add_argument('--incarnation')
    parser.add_argument('--table')
    parser.add_argument('--client', help='private read-only replication client JSON; absent means local query')
    parser.add_argument('--pages', type=int, default=100)
    parser.add_argument('--seed-db', action='append', default=[],
                        help='owned read-only SQLite seed; indexes only session_chunks(id,part,body)')
    parser.add_argument('--blob-cache-dir', help='private local cache directory for fetched snapshot BLOBs')
    args = parser.parse_args()
    replica = Replica(args.database)
    if args.client:
        from .config import private_json
        from .worker import Client
        if args.seed_db or args.blob_cache_dir:
            from .business_blob_cache import BlobCache
            with BlobCache(args.seed_db, args.blob_cache_dir) as cache:
                result = pull(Client(private_json(args.client)), replica, args.authority,
                              args.pages, blob_cache=cache)
                result = dict(result, blob_cache=cache.status())
        else:
            result = pull(Client(private_json(args.client)), replica, args.authority, args.pages)
    elif args.table and args.incarnation:
        # CLI serializes BLOB values back to explicit typed cells for safe JSON.
        from .business_journal import cell
        result = replica.query(args.authority, args.incarnation, args.table)
        for row in result['rows']:
            row['value'] = {key: cell(value) for key, value in row['value'].items()}
    else:
        parser.error('--client or both --incarnation/--table required')
    print(canonical(result))


if __name__ == '__main__':
    main()
