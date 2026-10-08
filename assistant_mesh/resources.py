"""Dynamic capability advertisements, evidence and exact remote-use grants.

This is a discovery/permission ledger, not the native agent's tool allowlist.
No method scans a network, opens a device, or invokes an advertised tool.
The caller MUST bind actor to an authenticated peer outside model input.
"""
import hashlib
import json
import math
import uuid
from urllib.parse import urlsplit, urlunsplit

from .store import Conflict


_PRIVATE_KEYS = frozenset(('token', 'secret', 'secrets', 'password', 'passwd', 'authorization',
                           'cookie', 'cookies', 'api_key', 'private_key', 'access_token',
                           'refresh_token', 'client_secret', 'credential', 'credentials',
                           'auth_token', 'bearer_token', 'session_token', 'session_cookie'))
_PRIVATE_CANONICAL = frozenset(key.replace('_', '') for key in _PRIVATE_KEYS)


def _name(value, label, maximum=200):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or 0xd800 <= ord(char) <= 0xdfff for char in value)):
        raise ValueError('invalid_' + label)
    return value


def _json(value, label, maximum=32768):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError('invalid_' + label)
    if len(encoded.encode('utf8')) > maximum:
        raise ValueError(label + '_too_large')
    return encoded


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('invalid_' + label)
    try:
        finite = math.isfinite(value)
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError('invalid_' + label)
    if not finite:
        raise ValueError('invalid_' + label)
    return result


def _ttl(value):
    seconds = _finite(value, 'lease_seconds')
    if seconds < 1 or seconds > 86400:
        raise ValueError('invalid_lease_seconds')
    return seconds


def _epoch(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('invalid_epoch')
    return value


def _safe_metadata(value):
    """Remove credential fields/URL userinfo/query from metadata before storage.

    Free-form text cannot be perfectly classified. Callers must never put
    credentials in descriptions, evidence, values or URL paths. Private auth
    bindings stay on the provider, not in this discovery database.
    """
    redacted = [False]

    def visit(item):
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError('invalid_metadata_key')
                normalized = key.lower().replace('-', '_')
                if (normalized.replace('_', '') in _PRIVATE_CANONICAL
                        or (normalized == 'auth' and not isinstance(child, dict))):
                    redacted[0] = True
                    continue
                result[key] = visit(child)
            return result
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, str) and '://' in item:
            try:
                url = urlsplit(item)
                if url.netloc and (url.username is not None or url.password is not None or url.query or url.fragment):
                    host = url.hostname or ''
                    if ':' in host:
                        host = '[' + host + ']'
                    if url.port is not None:
                        host += ':' + str(url.port)
                    redacted[0] = True
                    return urlunsplit((url.scheme, host, url.path, '', ''))
            except ValueError:
                # Malformed URLs with apparent userinfo are not useful metadata.
                if '@' in item or '?' in item:
                    redacted[0] = True
                    return '[private endpoint redacted]'
        return item

    return visit(value), redacted[0]


class Registry:
    """Use Store's authority clock, BEGIN IMMEDIATE and SQLite database.

    owner_principal is deployment configuration, never a model-supplied field.
    trusted_verifiers is an explicit deployment binding of independent checkers.
    A provider may publish arbitrary kinds and declared metrics, but cannot
    upgrade its own statements to verified evidence or approve a remote grant.
    """
    def __init__(self, store, owner_principal='operator', trusted_verifiers=()):
        self.store = store
        self.owner_principal = _name(owner_principal, 'owner_principal')
        if isinstance(trusted_verifiers, str):
            raise ValueError('invalid_trusted_verifiers')
        self.trusted_verifiers = frozenset(_name(v, 'verifier') for v in trusted_verifiers)
        with self.store.transaction() as db:
            # Do not use executescript: it implicitly commits BEGIN IMMEDIATE.
            statements = (
                '''CREATE TABLE IF NOT EXISTS capabilities(
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, kind TEXT NOT NULL,
                    description TEXT NOT NULL, spec TEXT NOT NULL, redacted INTEGER NOT NULL,
                    epoch INTEGER NOT NULL, health TEXT NOT NULL, deadline REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS capability_metrics(
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, capability_id TEXT NOT NULL,
                    capability_epoch INTEGER NOT NULL, metric TEXT NOT NULL, value TEXT NOT NULL,
                    unit TEXT NOT NULL, source TEXT NOT NULL, actor TEXT NOT NULL,
                    sample_time REAL NOT NULL, received REAL NOT NULL,
                    evidence TEXT NOT NULL, verification TEXT NOT NULL, redacted INTEGER NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS capability_edges(
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
                    source_epoch INTEGER NOT NULL, target_epoch INTEGER NOT NULL,
                    relation TEXT NOT NULL, spec TEXT NOT NULL, redacted INTEGER NOT NULL,
                    deadline REAL NOT NULL, created REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS capability_grant_requests(
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, principal TEXT NOT NULL,
                    capability_id TEXT NOT NULL, capability_epoch INTEGER NOT NULL,
                    action TEXT NOT NULL, scope TEXT NOT NULL, reason TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS capability_grants(
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, principal TEXT NOT NULL,
                    capability_id TEXT NOT NULL, capability_epoch INTEGER NOT NULL,
                    action TEXT NOT NULL, scope TEXT NOT NULL, approved_by TEXT NOT NULL,
                    deadline REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS capability_audit(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
                    operation TEXT NOT NULL, capability_id TEXT, detail TEXT NOT NULL, created REAL NOT NULL)''',
                'CREATE INDEX IF NOT EXISTS capability_metric_lookup ON capability_metrics(capability_id,sample_time)',
                'CREATE INDEX IF NOT EXISTS capability_grant_lookup ON capability_grants(principal,capability_id)',
            )
            for statement in statements:
                db.execute(statement)

    def _audit(self, db, actor, operation, identity, detail):
        safe, _ = _safe_metadata(detail)
        db.execute('INSERT INTO capability_audit(actor,operation,capability_id,detail,created) VALUES(?,?,?,?,?)',
                   (actor, operation, identity, _json(safe, 'audit'), self.store.clock()))

    @staticmethod
    def _find(db, identity):
        row = db.execute('SELECT * FROM capabilities WHERE id=?', (identity,)).fetchone()
        if not row:
            raise ValueError('capability_not_found')
        return row

    def _live(self, row, now=None):
        now = self.store.clock() if now is None else now
        return not row['revoked'] and row['deadline'] > now and row['health'] not in ('unavailable', 'failed')

    def _view(self, row):
        result = dict(row)
        result['spec'] = json.loads(result['spec'])
        result['secret_fields_redacted'] = bool(result.pop('redacted'))
        result['revoked'] = bool(result['revoked'])
        result['available'] = self._live(row)
        result['lease_expired'] = row['deadline'] <= self.store.clock()
        result['verification'] = 'declared'  # advertisements never prove execution
        result['health_verification'] = 'declared'
        return result

    def _provider(self, row, actor, epoch):
        if row['principal'] != actor:
            raise PermissionError('capability_principal_mismatch')
        if row['epoch'] != _epoch(epoch):
            raise Conflict('stale_capability_epoch')
        if row['revoked'] or row['deadline'] <= self.store.clock():
            raise Conflict('capability_lease_not_live')

    def advertise(self, actor, id, kind, spec=None, description='', lease_seconds=90,
                  principal=None, expected_epoch=None):
        actor, identity, kind = _name(actor, 'actor'), _name(id, 'capability_id'), _name(kind, 'kind')
        principal = actor if principal is None else _name(principal, 'principal')
        if principal != actor:
            raise PermissionError('capability_principal_mismatch')
        if not isinstance(description, str) or len(description.encode('utf8')) > 8000:
            raise ValueError('invalid_description')
        spec = {} if spec is None else spec
        if not isinstance(spec, dict):
            raise ValueError('invalid_capability_spec')
        _json(spec, 'capability_spec')
        safe, redacted = _safe_metadata(spec)
        encoded, seconds = _json(safe, 'capability_spec'), _ttl(lease_seconds)
        with self.store.transaction() as db:
            now = self.store.clock()
            row = db.execute('SELECT * FROM capabilities WHERE id=?', (identity,)).fetchone()
            if row:
                if row['principal'] != actor:
                    raise PermissionError('capability_principal_mismatch')
                if row['revoked']:
                    raise Conflict('capability_revoked')
                same = row['kind'] == kind and row['description'] == description and row['spec'] == encoded
                # An identical advertisement is a retry, not a lease renewal.
                if same and row['deadline'] > now:
                    if expected_epoch is not None and row['epoch'] != _epoch(expected_epoch):
                        raise Conflict('stale_capability_epoch')
                    return self._view(row)
                if expected_epoch is None or row['epoch'] != _epoch(expected_epoch):
                    raise Conflict('stale_capability_epoch')
                db.execute('UPDATE capabilities SET kind=?,description=?,spec=?,redacted=?,epoch=epoch+1,health=?,deadline=?,updated=? WHERE id=?',
                           (kind, description, encoded, int(redacted), 'unknown', now + seconds, now, identity))
            else:
                if expected_epoch is not None:
                    raise Conflict('capability_not_registered')
                db.execute('INSERT INTO capabilities VALUES(?,?,?,?,?,?,1,?,?,0,?,?)',
                           (identity, actor, kind, description, encoded, int(redacted), 'unknown', now + seconds, now, now))
            result = self._view(self._find(db, identity))
            self._audit(db, actor, 'advertise', identity, {'epoch': result['epoch'], 'kind': kind})
            return result

    def discover(self, kind=None, principal=None, include_unavailable=False, limit=100):
        if kind is not None:
            _name(kind, 'kind')
        if principal is not None:
            _name(principal, 'principal')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('invalid_limit')
        with self.store.transaction() as db:
            where, values = [], []
            for key, value in (('kind', kind), ('principal', principal)):
                if value is not None:
                    where.append(key + '=?')
                    values.append(value)
            if not include_unavailable:
                where.extend(('revoked=0', 'deadline>?', "health NOT IN ('unavailable','failed')"))
                values.append(self.store.clock())
            query = 'SELECT * FROM capabilities' + (' WHERE ' + ' AND '.join(where) if where else '')
            rows = db.execute(query + ' ORDER BY updated DESC,id LIMIT ?', values + [limit]).fetchall()
            return {'capabilities': [self._view(row) for row in rows], 'as_of': self.store.clock()}

    def describe(self, id, include_unavailable=True):
        _name(id, 'capability_id')
        with self.store.transaction() as db:
            row = self._find(db, id)
            if not include_unavailable and not self._live(row):
                raise Conflict('capability_not_available')
            result = self._view(row)
            metrics = db.execute('SELECT * FROM capability_metrics WHERE capability_id=? ORDER BY sample_time DESC,received DESC,id LIMIT 100', (id,)).fetchall()
            result['metrics'] = [self._metric_view(metric, row['epoch']) for metric in metrics]
            return result

    @staticmethod
    def _metric_view(row, epoch):
        result = dict(row)
        result.pop('fingerprint')
        result['value'], result['evidence'] = json.loads(result['value']), json.loads(result['evidence'])
        result['secret_fields_redacted'] = bool(result.pop('redacted'))
        result['current_epoch'] = result['capability_epoch'] == epoch
        return result

    def renew(self, actor, id, epoch, lease_seconds=90, health=None):
        _name(actor, 'actor')
        _name(id, 'capability_id')
        seconds = _ttl(lease_seconds)
        if health is not None:
            _name(health, 'health', 100)
        with self.store.transaction() as db:
            row = self._find(db, id)
            self._provider(row, actor, epoch)
            db.execute('UPDATE capabilities SET deadline=?,health=?,updated=? WHERE id=?',
                       (self.store.clock() + seconds, row['health'] if health is None else health, self.store.clock(), id))
            self._audit(db, actor, 'renew', id, {'epoch': epoch, 'health': health})
            return self._view(self._find(db, id))

    def observe(self, actor, id, metric, value, unit, source, sample_time=None,
                evidence=None, verification='declared', epoch=None, observation_id=None):
        actor, identity = _name(actor, 'actor'), _name(id, 'capability_id')
        metric, unit, source = _name(metric, 'metric'), _name(unit, 'unit', 100), _name(source, 'source', 2000)
        if verification not in ('declared', 'verified'):
            raise ValueError('invalid_verification')
        if verification == 'verified' and (actor not in self.trusted_verifiers or not evidence):
            raise PermissionError('independent_verification_required')
        _json(value, 'metric_value', 8192)
        _json(evidence, 'metric_evidence', 8192)
        safe, redacted = _safe_metadata({'value': value, 'source': source, 'evidence': evidence})
        sampled = self.store.clock() if sample_time is None else _finite(sample_time, 'sample_time')
        if sampled < 0 or sampled > self.store.clock() + 60:
            raise ValueError('invalid_sample_time')
        with self.store.transaction() as db:
            row = self._find(db, identity)
            if row['epoch'] != _epoch(epoch):
                raise Conflict('stale_capability_epoch')
            if row['revoked'] or row['deadline'] <= self.store.clock():
                raise Conflict('capability_lease_not_live')
            if actor != row['principal'] and actor not in self.trusted_verifiers:
                raise PermissionError('observation_principal_mismatch')
            if verification == 'verified' and actor == row['principal']:
                raise PermissionError('self_verification_not_allowed')
            body = [actor, identity, epoch, metric, safe['value'], unit, safe['source'], sampled, safe['evidence'], verification]
            fingerprint = hashlib.sha256(_json(body, 'observation').encode('utf8')).hexdigest()
            observation_id = fingerprint if observation_id is None else _name(observation_id, 'observation_id')
            previous = db.execute('SELECT * FROM capability_metrics WHERE id=?', (observation_id,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Conflict('observation_id_content_conflict')
                return self._metric_view(previous, row['epoch'])
            db.execute('INSERT INTO capability_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (observation_id, fingerprint, identity, epoch, metric, _json(safe['value'], 'metric_value'), unit,
                        safe['source'], actor, sampled, self.store.clock(), _json(safe['evidence'], 'metric_evidence'),
                        verification, int(redacted)))
            self._audit(db, actor, 'observe', identity, {'observation_id': observation_id, 'metric': metric, 'verification': verification})
            result = db.execute('SELECT * FROM capability_metrics WHERE id=?', (observation_id,)).fetchone()
            return self._metric_view(result, row['epoch'])

    def link(self, actor, id, source, target, relation='depends_on', spec=None,
             lease_seconds=90, epoch=None):
        for value, label in ((actor, 'actor'), (id, 'edge_id'), (source, 'source'), (target, 'target'), (relation, 'relation')):
            _name(value, label)
        spec = {} if spec is None else spec
        if not isinstance(spec, dict):
            raise ValueError('invalid_edge_spec')
        _json(spec, 'edge_spec')
        safe, redacted = _safe_metadata(spec)
        encoded, seconds = _json(safe, 'edge_spec'), _ttl(lease_seconds)
        with self.store.transaction() as db:
            src, dst = self._find(db, source), self._find(db, target)
            self._provider(src, actor, epoch)
            if not self._live(src) or not self._live(dst):
                raise Conflict('edge_endpoint_not_available')
            previous = db.execute('SELECT * FROM capability_edges WHERE id=?', (id,)).fetchone()
            if previous:
                if previous['actor'] != actor:
                    raise PermissionError('edge_principal_mismatch')
                same = (previous['source'] == source and previous['target'] == target and previous['relation'] == relation
                        and previous['spec'] == encoded and previous['source_epoch'] == src['epoch']
                        and previous['target_epoch'] == dst['epoch'])
                if not same:
                    raise Conflict('edge_id_content_conflict')
                if previous['deadline'] > self.store.clock():
                    return self._edge_view(previous, True)
                db.execute('UPDATE capability_edges SET deadline=? WHERE id=?',
                           (min(self.store.clock() + seconds, src['deadline'], dst['deadline']), id))
            else:
                db.execute('INSERT INTO capability_edges VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (id, actor, source, target, src['epoch'], dst['epoch'], relation, encoded, int(redacted),
                            min(self.store.clock() + seconds, src['deadline'], dst['deadline']), self.store.clock()))
            self._audit(db, actor, 'link', source, {'edge_id': id, 'target': target, 'relation': relation})
            return self._edge_view(db.execute('SELECT * FROM capability_edges WHERE id=?', (id,)).fetchone(), True)

    @staticmethod
    def _edge_view(row, available):
        result = dict(row)
        result['spec'] = json.loads(result['spec'])
        result['secret_fields_redacted'] = bool(result.pop('redacted'))
        result['available'] = available
        result['verification'] = 'declared'
        result['invocation_authorized'] = False  # graph topology is not permission
        return result

    def graph(self, include_unavailable=False, limit=200):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('invalid_limit')
        with self.store.transaction() as db:
            now = self.store.clock()
            # Filter live vertices before LIMIT so tombstones cannot starve discovery.
            where = '' if include_unavailable else " WHERE revoked=0 AND deadline>? AND health NOT IN ('unavailable','failed')"
            values = [limit] if include_unavailable else [now, limit]
            rows = db.execute('SELECT * FROM capabilities' + where + ' ORDER BY id LIMIT ?', values).fetchall()
            vertices = {row['id']: row for row in rows}
            edges = []
            for edge in db.execute('SELECT * FROM capability_edges ORDER BY id'):
                src, dst = vertices.get(edge['source']), vertices.get(edge['target'])
                if src is None or dst is None:
                    continue
                available = (edge['deadline'] > now and self._live(src, now) and self._live(dst, now)
                             and src['epoch'] == edge['source_epoch'] and dst['epoch'] == edge['target_epoch'])
                if available or include_unavailable:
                    edges.append(self._edge_view(edge, available))
                if len(edges) >= limit:
                    break
            return {'capabilities': [self._view(row) for row in rows], 'edges': edges,
                    'as_of': now, 'invocation_authorized': False}

    def revoke(self, actor, id, epoch, reason=''):
        actor, identity = _name(actor, 'actor'), _name(id, 'capability_id')
        if not isinstance(reason, str) or len(reason.encode('utf8')) > 2000:
            raise ValueError('invalid_reason')
        with self.store.transaction() as db:
            row = self._find(db, identity)
            if actor not in (row['principal'], self.owner_principal):
                raise PermissionError('capability_principal_mismatch')
            if row['epoch'] != _epoch(epoch):
                raise Conflict('stale_capability_epoch')
            if not row['revoked']:
                db.execute('UPDATE capabilities SET revoked=1,updated=? WHERE id=?', (self.store.clock(), identity))
                self._audit(db, actor, 'revoke', identity, {'epoch': epoch, 'reason': reason})
            return self._view(self._find(db, identity))

    @staticmethod
    def _scope(scope):
        if scope is None or scope == '' or scope == {} or scope == []:
            raise ValueError('invalid_scope')
        return _json(scope, 'scope', 8192)

    def request_grant(self, actor, id, action, scope, request_id=None, reason=''):
        actor, identity, action = _name(actor, 'actor'), _name(id, 'capability_id'), _name(action, 'action')
        encoded_scope = self._scope(scope)
        if not isinstance(reason, str) or len(reason.encode('utf8')) > 2000:
            raise ValueError('invalid_reason')
        request_id = uuid.uuid4().hex if request_id is None else _name(request_id, 'request_id')
        with self.store.transaction() as db:
            row = self._find(db, identity)
            if not self._live(row):
                raise Conflict('capability_not_available')
            fingerprint = hashlib.sha256(_json([actor, identity, row['epoch'], action, scope, reason], 'grant_request').encode('utf8')).hexdigest()
            previous = db.execute('SELECT * FROM capability_grant_requests WHERE id=?', (request_id,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Conflict('grant_request_id_content_conflict')
                return self._request_view(previous)
            db.execute('INSERT INTO capability_grant_requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (request_id, fingerprint, actor, identity, row['epoch'], action, encoded_scope, reason, 'pending', self.store.clock()))
            self._audit(db, actor, 'request_grant', identity, {'request_id': request_id, 'action': action, 'scope': scope})
            return self._request_view(db.execute('SELECT * FROM capability_grant_requests WHERE id=?', (request_id,)).fetchone())

    @staticmethod
    def _request_view(row):
        result = dict(row)
        result.pop('fingerprint')
        result['scope'] = json.loads(result['scope'])
        return result

    @staticmethod
    def _grant_view(row, now):
        result = dict(row)
        result['scope'] = json.loads(result['scope'])
        result['revoked'] = bool(result['revoked'])
        result['available'] = not row['revoked'] and row['deadline'] > now
        return result

    def grant(self, owner, request_id, expires_seconds=300, grant_id=None):
        if owner != self.owner_principal:
            raise PermissionError('owner_approval_required')
        _name(request_id, 'request_id')
        seconds = _ttl(expires_seconds)
        grant_id = _name(('grant-' + request_id) if grant_id is None else grant_id, 'grant_id', 256)
        with self.store.transaction() as db:
            request = db.execute('SELECT * FROM capability_grant_requests WHERE id=?', (request_id,)).fetchone()
            if not request:
                raise ValueError('grant_request_not_found')
            row = self._find(db, request['capability_id'])
            if not self._live(row) or row['epoch'] != request['capability_epoch']:
                raise Conflict('grant_request_capability_stale')
            previous = db.execute('SELECT * FROM capability_grants WHERE request_id=? OR id=?', (request_id, grant_id)).fetchone()
            if previous:
                if previous['request_id'] != request_id or previous['id'] != grant_id:
                    raise Conflict('grant_id_content_conflict')
                # Retrying approval does not extend or reactivate the grant.
                return self._grant_view(previous, self.store.clock())
            now = self.store.clock()
            db.execute('INSERT INTO capability_grants VALUES(?,?,?,?,?,?,?,?,?,0,?)',
                       (grant_id, request_id, request['principal'], row['id'], row['epoch'], request['action'],
                        request['scope'], owner, now + seconds, now))
            db.execute("UPDATE capability_grant_requests SET status='approved' WHERE id=?", (request_id,))
            self._audit(db, owner, 'grant', row['id'], {'grant_id': grant_id, 'request_id': request_id, 'principal': request['principal'], 'deadline': now + seconds})
            return self._grant_view(db.execute('SELECT * FROM capability_grants WHERE id=?', (grant_id,)).fetchone(), now)

    def authorize(self, actor, id, action, scope, grant_id=None, request_id=None):
        actor, identity, action = _name(actor, 'actor'), _name(id, 'capability_id'), _name(action, 'action')
        encoded_scope = self._scope(scope)
        if grant_id is not None:
            _name(grant_id, 'grant_id', 256)
        if request_id is not None:
            _name(request_id, 'request_id')
        with self.store.transaction() as db:
            row = self._find(db, identity)
            allowed, reason, chosen = False, 'capability_not_available', None
            if self._live(row):
                if actor == row['principal']:
                    allowed, reason = True, 'same_principal'
                else:
                    where, values = 'principal=? AND capability_id=? AND capability_epoch=? AND action=? AND scope=? AND revoked=0 AND deadline>?', [actor, identity, row['epoch'], action, encoded_scope, self.store.clock()]
                    if grant_id is not None:
                        where += ' AND id=?'
                        values.append(grant_id)
                    chosen = db.execute('SELECT * FROM capability_grants WHERE ' + where + ' ORDER BY deadline DESC,id LIMIT 1', values).fetchone()
                    allowed, reason = (True, 'exact_grant') if chosen else (False, 'exact_grant_required')
            result = {'allowed': allowed, 'reason': reason, 'principal': actor, 'capability_id': identity,
                      'epoch': row['epoch'], 'action': action, 'scope': scope,
                      'grant_id': chosen['id'] if chosen else None}
            # A reused authorization request is ALWAYS rechecked after revoke,
            # expiry or epoch changes. This is not an execution replay permit.
            self._audit(db, actor, 'authorize', identity, dict(result, request_id=request_id))
            return result

    def revoke_grant(self, owner, grant_id, reason=''):
        if owner != self.owner_principal:
            raise PermissionError('owner_approval_required')
        _name(grant_id, 'grant_id', 256)
        if not isinstance(reason, str) or len(reason.encode('utf8')) > 2000:
            raise ValueError('invalid_reason')
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM capability_grants WHERE id=?', (grant_id,)).fetchone()
            if not row:
                raise ValueError('grant_not_found')
            if not row['revoked']:
                db.execute('UPDATE capability_grants SET revoked=1 WHERE id=?', (grant_id,))
                db.execute("UPDATE capability_grant_requests SET status='revoked' WHERE id=?", (row['request_id'],))
                self._audit(db, owner, 'revoke_grant', row['capability_id'], {'grant_id': grant_id, 'reason': reason})
            return self._grant_view(db.execute('SELECT * FROM capability_grants WHERE id=?', (grant_id,)).fetchone(), self.store.clock())

    def audit(self, after=0, limit=100, principal=None):
        if (isinstance(after, bool) or not isinstance(after, int) or after < 0
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000):
            raise ValueError('invalid_audit_window')
        if principal is not None:
            _name(principal, 'principal')
        with self.store.transaction() as db:
            where, values = 'sequence>?', [after]
            if principal is not None:
                where += ' AND actor=?'
                values.append(principal)
            rows = db.execute('SELECT * FROM capability_audit WHERE ' + where + ' ORDER BY sequence LIMIT ?', values + [limit]).fetchall()
            events = []
            for row in rows:
                event = dict(row)
                event['detail'] = json.loads(event['detail'])
                events.append(event)
            return {'events': events, 'next': events[-1]['sequence'] if events else after}
