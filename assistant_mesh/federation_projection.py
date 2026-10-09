"""Issuer-isolated, query-only projections of foreign capability announcements.

The deployment caller must bind ``expected_issuer`` to its enrolled authority
and authenticated transport, never to a model-supplied identity. This module
does not authenticate transport, contact a peer, import a local capability,
grant a permission, reserve capacity, or execute anything. Remote facts and
relative lease estimates are evidence for delegation, not execution authority.
Native agent tools, networking and local work are unaffected.
"""
import hashlib
import json

from .resources import _finite, _json, _name, _safe_metadata
from .store import Conflict


FORMAT = 'mesh-capability-export/1'
MAX_RECORDS = 1000
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 65536
_MAX_SEQUENCE = 2 ** 63 - 1
_FLAGS = {'managed_invocation_authorized': False,
          'local_registry_imported': False,
          'global_consensus_verified': False}
_PAGE_FIELDS = frozenset(('format', 'issuer', 'after', 'source_sequence',
                         'next_cursor', 'complete', 'exported_at', 'records'))
_CAPABILITY_FIELDS = frozenset(('id', 'principal', 'kind', 'description', 'spec',
                               'epoch', 'health', 'deadline', 'revoked', 'created',
                               'updated', 'secret_fields_redacted', 'available',
                               'lease_expired', 'verification', 'health_verification'))


def _sequence(value, positive=False):
    if (isinstance(value, bool) or not isinstance(value, int)
            or not (1 if positive else 0) <= value <= _MAX_SEQUENCE):
        raise ValueError('invalid_projection_sequence')
    return value


def _time(value):
    value = _finite(value, 'projection_time')
    if value < 0:
        raise ValueError('invalid_projection_time')
    return value


def _seconds(value):
    value = _finite(value, 'projection_freshness')
    if not 1 <= value <= 86400:
        raise ValueError('invalid_projection_freshness')
    return value


def _digest(encoded):
    return hashlib.sha256(encoded.encode('utf8')).hexdigest()


def _stable(capability):
    # These two fields depend on the export's sampling time, not a new source
    # mutation. All other announcement facts participate in conflict checks.
    return {key: value for key, value in capability.items()
            if key not in ('available', 'lease_expired')}


def _page(expected_issuer, page):
    """Validate and detach the exact wire object before opening a transaction."""
    _name(expected_issuer, 'projection_issuer')
    if not isinstance(page, dict) or set(page) != _PAGE_FIELDS:
        raise ValueError('invalid_projection_page')
    encoded = _json(page, 'projection_page', MAX_PAGE_BYTES)
    # json.dumps coerces non-string dictionary keys. Reject those on the
    # original object rather than silently changing the announced contract.
    _safe_metadata(page)
    page = json.loads(encoded)
    if page['format'] != FORMAT:
        raise ValueError('invalid_projection_format')
    _name(page['issuer'], 'projection_issuer')
    if page['issuer'] != expected_issuer:
        raise PermissionError('projection_issuer_mismatch')
    after = _sequence(page['after'])
    head = _sequence(page['source_sequence'])
    next_cursor = _sequence(page['next_cursor'])
    exported_at = _time(page['exported_at'])
    if (not isinstance(page['complete'], bool) or not after <= next_cursor <= head
            or (page['complete'] and next_cursor != head)):
        raise ValueError('invalid_projection_cursor')
    records = page['records']
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError('invalid_projection_records')
    normalized, previous, identities = [], after, set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {'id', 'revision', 'capability'}:
            raise ValueError('invalid_projection_record')
        _json(record, 'projection_record', MAX_RECORD_BYTES)
        identity = _name(record['id'], 'projection_capability_id')
        revision = _sequence(record['revision'], positive=True)
        if identity in identities or not previous < revision <= next_cursor:
            raise ValueError('invalid_projection_record_order')
        identities.add(identity)
        previous = revision
        cap = record['capability']
        if not isinstance(cap, dict) or set(cap) != _CAPABILITY_FIELDS:
            raise ValueError('invalid_projection_capability')
        if cap['id'] != identity:
            raise ValueError('projection_capability_id_mismatch')
        for key in ('id', 'principal', 'kind'):
            _name(cap[key], 'projection_' + key)
        _name(cap['health'], 'projection_health', 100)
        _sequence(cap['epoch'], positive=True)
        try:
            description_size = len(cap['description'].encode('utf8')) if isinstance(cap['description'], str) else 8001
        except UnicodeError:
            raise ValueError('invalid_projection_description')
        if description_size > 8000:
            raise ValueError('invalid_projection_description')
        if not isinstance(cap['spec'], dict):
            raise ValueError('invalid_projection_spec')
        _json(cap['spec'], 'projection_spec')
        safe, redacted = _safe_metadata(cap['spec'])
        if redacted or safe != cap['spec']:
            raise ValueError('unsafe_projection_metadata')
        for key in ('deadline', 'created', 'updated'):
            _time(cap[key])
        for key in ('revoked', 'secret_fields_redacted', 'available', 'lease_expired'):
            if not isinstance(cap[key], bool):
                raise ValueError('invalid_projection_capability_flag')
        if cap['verification'] != 'declared' or cap['health_verification'] != 'declared':
            raise ValueError('invalid_projection_verification')
        expired = cap['deadline'] <= exported_at
        available = not cap['revoked'] and not expired and cap['health'] not in ('unavailable', 'failed')
        if cap['lease_expired'] != expired or cap['available'] != available:
            raise ValueError('invalid_projection_source_freshness')
        stable = _stable(cap)
        body = _json(stable, 'projection_capability', MAX_RECORD_BYTES)
        normalized.append({'id': identity, 'revision': revision, 'capability': cap,
                           'fingerprint': _digest(body),
                           'remaining': min(86400, max(0, cap['deadline'] - exported_at))})
    if not page['complete'] and (not normalized or previous != next_cursor or next_cursor >= head):
        raise ValueError('invalid_projection_cursor')
    # Export time and its two derived fields can change on a repeated query;
    # the sequence, identities, ordering and every stable fact cannot.
    receipt = {key: value for key, value in page.items() if key not in ('exported_at', 'records')}
    receipt['records'] = [{'id': r['id'], 'revision': r['revision'],
                           'capability': _stable(r['capability'])} for r in normalized]
    return page, normalized, _digest(_json(receipt, 'projection_page', MAX_PAGE_BYTES))


class Projection:
    """Durable discovery cache, separate from the local Registry and grants."""

    def __init__(self, store, freshness_seconds=30):
        self.store, self.freshness_seconds = store, _seconds(freshness_seconds)
        with store.transaction() as db:
            statements = (
                '''CREATE TABLE IF NOT EXISTS federation_projection_sources(
                    issuer TEXT PRIMARY KEY, cursor INTEGER NOT NULL,
                    source_sequence INTEGER NOT NULL, received REAL NOT NULL,
                    connected INTEGER NOT NULL, contact_deadline REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS federation_projection_capabilities(
                    issuer TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, capability TEXT NOT NULL,
                    received REAL NOT NULL, local_deadline REAL NOT NULL,
                    PRIMARY KEY(issuer,id))''',
                '''CREATE TABLE IF NOT EXISTS federation_projection_revisions(
                    issuer TEXT NOT NULL, revision INTEGER NOT NULL,
                    id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    PRIMARY KEY(issuer,revision))''',
                '''CREATE TABLE IF NOT EXISTS federation_projection_pages(
                    issuer TEXT NOT NULL, after_cursor INTEGER NOT NULL,
                    next_cursor INTEGER NOT NULL, source_sequence INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    PRIMARY KEY(issuer,after_cursor,next_cursor,source_sequence))''',
            )
            for statement in statements:
                db.execute(statement)

    def apply(self, expected_issuer, page):
        """Atomically store an authenticated caller's page and its cursor.

        Sparse revisions are valid: this is latest-per-id state, not an event
        replay. A missing page never deletes a capability. A lost-reply retry
        keeps the original record age and cannot extend an estimated lease.
        """
        page, records, fingerprint = _page(expected_issuer, page)
        with self.store.transaction() as db:
            now = _time(self.store.clock())
            source = db.execute('SELECT * FROM federation_projection_sources WHERE issuer=?',
                                (expected_issuer,)).fetchone()
            cursor = source['cursor'] if source else 0
            known_head = source['source_sequence'] if source else 0
            if page['source_sequence'] < known_head or page['source_sequence'] < cursor:
                raise Conflict('projection_source_rollback')
            # Check historical identities even for a stale/retried page; the
            # authority may not reuse a revision for another fact or id.
            for record in records:
                seen = db.execute('''SELECT id,fingerprint FROM federation_projection_revisions
                    WHERE issuer=? AND revision=?''', (expected_issuer, record['revision'])).fetchone()
                if seen and (seen['id'] != record['id'] or seen['fingerprint'] != record['fingerprint']):
                    raise Conflict('projection_revision_content_conflict')
            receipt = db.execute('''SELECT fingerprint FROM federation_projection_pages
                WHERE issuer=? AND after_cursor=? AND next_cursor=? AND source_sequence=?''',
                (expected_issuer, page['after'], page['next_cursor'], page['source_sequence'])).fetchone()
            if receipt and receipt['fingerprint'] != fingerprint:
                raise Conflict('projection_page_content_conflict')
            if not receipt and page['after'] != cursor:
                raise Conflict('projection_cursor_mismatch')
            applied = 0
            for record in records:
                previous = db.execute('''SELECT * FROM federation_projection_capabilities
                    WHERE issuer=? AND id=?''', (expected_issuer, record['id'])).fetchone()
                if previous and previous['revision'] > record['revision']:
                    continue  # never overwrite a newer fact/tombstone with a retry
                deadline = now + record['remaining']
                if previous and previous['revision'] == record['revision']:
                    if previous['fingerprint'] != record['fingerprint']:
                        raise Conflict('projection_revision_content_conflict')
                    db.execute('''UPDATE federation_projection_capabilities
                        SET local_deadline=MIN(local_deadline,?) WHERE issuer=? AND id=?''',
                        (deadline, expected_issuer, record['id']))
                    continue
                if receipt:
                    raise Conflict('projection_receipt_state_conflict')
                cap = record['capability']
                if previous:
                    old = json.loads(previous['capability'])
                    if old['revoked'] and not cap['revoked']:
                        raise Conflict('projection_tombstone_conflict')
                    if old['principal'] != cap['principal']:
                        raise Conflict('projection_principal_conflict')
                    if old['epoch'] > cap['epoch']:
                        raise Conflict('projection_epoch_rollback')
                db.execute('INSERT INTO federation_projection_revisions VALUES(?,?,?,?)',
                           (expected_issuer, record['revision'], record['id'], record['fingerprint']))
                db.execute('INSERT OR REPLACE INTO federation_projection_capabilities VALUES(?,?,?,?,?,?,?)',
                           (expected_issuer, record['id'], record['revision'], record['fingerprint'],
                            _json(cap, 'projection_capability', MAX_RECORD_BYTES), now, deadline))
                applied += 1
            if not receipt:
                db.execute('INSERT INTO federation_projection_pages VALUES(?,?,?,?,?)',
                           (expected_issuer, page['after'], page['next_cursor'], page['source_sequence'], fingerprint))
                db.execute('INSERT OR REPLACE INTO federation_projection_sources VALUES(?,?,?,?,?,?)',
                           (expected_issuer, page['next_cursor'], page['source_sequence'], now, 1,
                            now + self.freshness_seconds))
            source = db.execute('SELECT * FROM federation_projection_sources WHERE issuer=?',
                                (expected_issuer,)).fetchone()
            return dict(_FLAGS, issuer=expected_issuer, cursor=source['cursor'],
                        source_sequence=source['source_sequence'], applied=applied,
                        duplicate=bool(receipt), freshness_verification='declared_estimate',
                        as_of=now)

    def observe_connection(self, expected_issuer, connected, lease_seconds=30):
        """Record the caller's transport observation; does not perform a probe.

        A fresh successful probe may restore connection freshness, but cannot
        renew any capability. Captured/replayed pages alone cannot do that.
        """
        _name(expected_issuer, 'projection_issuer')
        if not isinstance(connected, bool):
            raise ValueError('invalid_projection_connection')
        seconds = _seconds(lease_seconds)
        with self.store.transaction() as db:
            now = _time(self.store.clock())
            if not db.execute('SELECT 1 FROM federation_projection_sources WHERE issuer=?',
                              (expected_issuer,)).fetchone():
                raise ValueError('projection_source_not_found')
            db.execute('''UPDATE federation_projection_sources SET connected=?,contact_deadline=?
                WHERE issuer=?''', (int(connected), now + seconds if connected else 0, expected_issuer))
        return self.status(expected_issuer)

    def status(self, expected_issuer):
        _name(expected_issuer, 'projection_issuer')
        with self.store.transaction() as db:
            now = _time(self.store.clock())
            source = db.execute('SELECT * FROM federation_projection_sources WHERE issuer=?',
                                (expected_issuer,)).fetchone()
            if source is None:
                return dict(_FLAGS, issuer=expected_issuer, cursor=0, source_sequence=0,
                            connection_state='unknown', received_at=None,
                            freshness_verification='declared_estimate', as_of=now)
            return dict(_FLAGS, issuer=expected_issuer, cursor=source['cursor'],
                        source_sequence=source['source_sequence'], received_at=source['received'],
                        connection_state='recently_observed' if source['connected'] and source['contact_deadline'] > now
                        else 'disconnected' if not source['connected'] else 'stale',
                        contact_deadline_estimate=source['contact_deadline'],
                        freshness_verification='declared_estimate', as_of=now)

    def discover(self, issuer=None, kind=None, include_unavailable=False, limit=100):
        if issuer is not None:
            _name(issuer, 'projection_issuer')
        if kind is not None:
            _name(kind, 'projection_kind')
        if not isinstance(include_unavailable, bool):
            raise ValueError('invalid_projection_include_unavailable')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECORDS:
            raise ValueError('invalid_projection_limit')
        with self.store.transaction() as db:
            now = _time(self.store.clock())
            return self._discover_view(db, now, issuer, kind, include_unavailable, limit)

    @staticmethod
    def _discover_view(db, now, issuer, kind, include_unavailable, limit):
        """Pure view for an existing authority transaction and validated filters.

        Does not initialize/import projection, refresh contact, or open another
        transaction. Used by task-bound routing evidence without copying the
        projection's availability semantics.
        """
        query = '''SELECT p.*,s.connected,s.contact_deadline FROM federation_projection_capabilities p
            JOIN federation_projection_sources s ON s.issuer=p.issuer'''
        values = []
        if issuer is not None:
            query += ' WHERE p.issuer=?'
            values.append(issuer)
        candidates = []
        # Filter before LIMIT so stale announcements cannot starve live rows.
        # No JSON SQLite extension is required on older hosts.
        for row in db.execute(query + ' ORDER BY p.issuer,p.id', values):
            cap = json.loads(row['capability'])
            if kind is not None and cap['kind'] != kind:
                continue
            connected = bool(row['connected'] and row['contact_deadline'] > now)
            expired = row['local_deadline'] <= now
            available = connected and not expired and not cap['revoked'] and cap['health'] not in ('unavailable', 'failed')
            if not include_unavailable and not available:
                continue
            reason = ('revoked' if cap['revoked'] else 'source_disconnected' if not row['connected']
                      else 'source_stale' if not connected else 'lease_expired_estimate' if expired
                      else 'source_health_unavailable' if cap['health'] in ('unavailable', 'failed')
                      else 'delegation_candidate_only')
            candidates.append(dict(_FLAGS, issuer=row['issuer'], id=row['id'], revision=row['revision'],
                                   capability=cap, available=available,
                                   remote_delegation_candidate=available, unavailable_reason=reason,
                                   received_at=row['received'], local_deadline_estimate=row['local_deadline'],
                                   remaining_seconds_estimate=max(0, row['local_deadline'] - now),
                                   freshness_verification='declared_estimate',
                                   transport_delay_accounted=False, source_clock_verified=False))
            if len(candidates) >= limit:
                break
        return dict(_FLAGS, capabilities=candidates, as_of=now,
                    freshness_verification='declared_estimate',
                    transport_delay_accounted=False, source_clock_verified=False)
