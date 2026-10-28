"""Separate durable query-only business projection; never a runnable Store.

No replica row, status, epoch or merged metadata can claim work or authorize an
external effect. Incarnations and conflicting inputs remain separate evidence.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3

from .business_journal import FORMAT, canonical, digest, row_key, uncell


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

    def apply(self, page, authority):
        try:
            checked_page(page, authority)
            with self.transaction() as db:
                if page['kind'] == 'snapshot':
                    return self._snapshot(db, page)
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

    def _snapshot(self, db, page):
        authority, incarnation, identity = page['authority'], page['incarnation'], page['snapshot_id']
        if (type(page['done']) is not bool or type(page['baseline_cursor']) is not int
                or type(page['total_rows']) is not int or page['baseline_cursor'] != page['head']
                or set(page['counts']) != set(page['manifest']['tables'])
                or any(type(count) is not int or count < 0 for count in page['counts'].values())
                or sum(page['counts'].values()) != page['total_rows']):
            raise ReplicaConflict('business_replica_snapshot_description_invalid')
        if db.execute('SELECT 1 FROM replica_sources WHERE authority=? AND incarnation<>?', (authority, incarnation)).fetchone():
            raise ReplicaConflict('business_replica_incarnation_changed_requires_separate_replica')
        receipt = db.execute('SELECT fingerprint FROM replica_snapshot_receipts WHERE snapshot_id=? AND after_cursor=?',
                             (identity, page['after'])).fetchone()
        if receipt:
            if receipt[0] != page['sha256']:
                raise ReplicaConflict('business_replica_snapshot_replay_conflict')
            return {'applied': False, 'duplicate': True, 'cursor': page['cursor']}
        source = self._source(db, authority, incarnation)
        if source and (page['baseline_cursor'] < source['cursor'] or page['head'] < source['head']):
            raise ReplicaConflict('business_replica_snapshot_would_regress')
        description = {key: page[key] for key in ('authority', 'incarnation', 'snapshot_id', 'generation',
            'baseline_cursor', 'manifest', 'counts', 'total_rows')}
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
        for item in page['rows']:
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


def pull(client, replica, authority, pages=100, limit=100):
    """One bounded authenticated synchronization; no task/model health requests."""
    if type(pages) is not int or not 1 <= pages <= 10000:
        raise ValueError('business_replica_page_count_invalid')
    info = client.request('/v1/business-replica/status', {})
    if info['authority'] != authority:
        raise ReplicaConflict('business_replica_source_mismatch')
    state = replica.state(authority, info['incarnation'])
    if not state or state['generation'] != info['generation'] or state['manifest'] != info['manifest']:
        start = client.request('/v1/business-replica/snapshot', {'action': 'begin'})
        cursor = 0
        try:
            for _ in range(pages):
                page = client.request('/v1/business-replica/snapshot', dict(action='page', snapshot_id=start['snapshot_id'], after=cursor, limit=limit))
                replica.apply(page, authority)
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
    args = parser.parse_args()
    replica = Replica(args.database)
    if args.client:
        from .config import private_json
        from .worker import Client
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
