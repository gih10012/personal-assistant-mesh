"""Transactional admission journal for *managed* Mesh capability operations.

This module does not run a tool, create a child task, intercept native Shell or
guarantee that a provider has performed an effect.  A deployment must bind every
``actor`` to authenticated identity and connect provider admission to its actual
operation ledger.  ``start()['execute_once']`` is true only for a new journal
transition: a timeout is not permission to repeat an effect.

Capacity is a contract for Mesh allocations, not an OS resource isolation claim.
Independent verified observations are time/workload/scope specific.  Providers'
settlement reports remain explicitly provider-reported, not independently proved.
"""
import hashlib
import json

from .resources import _epoch, _finite, _json, _name, _safe_metadata, _ttl
from .store import Conflict


MAX_QUANTA = (1 << 63) - 1
_HELD = ('reserved', 'accepted', 'running', 'unknown')
_SETTLED = ('completed', 'stopped')


def _quanta(value, label='quantity'):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_QUANTA:
        raise ValueError('invalid_' + label)
    return value


def _metadata(value, label, maximum=32768):
    encoded = _json(value, label, maximum)
    safe, redacted = _safe_metadata(value)
    if redacted:
        # Redacting an authorization/measurement contract would change its meaning.
        raise ValueError('private_metadata_not_allowed')
    return _json(safe, label, maximum)


def _digest(value):
    return hashlib.sha256(_json(value, 'allocation_fingerprint', 65536).encode('utf8')).hexdigest()


class Allocations:
    """Use the Registry's Store/clock, without nested write transactions.

    Pools and bindings are owner contracts.  A model cannot manufacture capacity,
    merge two charges by naming a shared pool, or approve new expenditure.
    ``usage_key`` is owner-bound: repeated claims for the same physical use within
    one allocation take max(quantity); distinct uses add.  Pool capacity is never
    summed across the capabilities which advertise it.
    """
    def __init__(self, store, registry):
        if registry.store is not store:
            raise ValueError('allocation_registry_store_mismatch')
        self.store, self.registry = store, registry
        self.owner_principal = registry.owner_principal
        with self.store.transaction() as db:
            statements = (
                '''CREATE TABLE IF NOT EXISTS resource_pools(
                    id TEXT PRIMARY KEY, dimensions TEXT NOT NULL, epoch INTEGER NOT NULL,
                    deadline REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS capability_pool_bindings(
                    capability_id TEXT NOT NULL, capability_epoch INTEGER NOT NULL,
                    pool_id TEXT NOT NULL, pool_epoch INTEGER NOT NULL, dimension TEXT NOT NULL,
                    quantity INTEGER NOT NULL, unit TEXT NOT NULL, usage_key TEXT NOT NULL,
                    epoch INTEGER NOT NULL, new_spend_minor INTEGER NOT NULL,
                    PRIMARY KEY(capability_id,pool_id,dimension))''',
                '''CREATE TABLE IF NOT EXISTS managed_allocations(
                    operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, actor TEXT NOT NULL,
                    task_id TEXT NOT NULL, task_epoch INTEGER NOT NULL, task_node TEXT NOT NULL,
                    leader_epoch INTEGER, plan TEXT NOT NULL, resolved TEXT NOT NULL,
                    state TEXT NOT NULL, epoch INTEGER NOT NULL, deadline REAL NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS managed_allocation_demands(
                    operation_id TEXT NOT NULL, pool_id TEXT NOT NULL,
                    pool_epoch INTEGER NOT NULL, dimension TEXT NOT NULL,
                    quantity INTEGER NOT NULL, unit TEXT NOT NULL,
                    PRIMARY KEY(operation_id,pool_id,dimension))''',
                '''CREATE TABLE IF NOT EXISTS managed_dispatch(
                    operation_id TEXT NOT NULL, capability_id TEXT NOT NULL,
                    provider TEXT NOT NULL, state TEXT NOT NULL, epoch INTEGER NOT NULL,
                    receipt_id TEXT, settlement TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(operation_id,capability_id))''',
                '''CREATE UNIQUE INDEX IF NOT EXISTS managed_provider_receipts
                    ON managed_dispatch(provider,receipt_id)''',
                '''CREATE INDEX IF NOT EXISTS managed_pool_usage
                    ON managed_allocation_demands(pool_id,dimension)''',
                '''CREATE TABLE IF NOT EXISTS managed_allocation_audit(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
                    operation TEXT NOT NULL, operation_id TEXT, detail TEXT NOT NULL,
                    created REAL NOT NULL)''',
            )
            for statement in statements:
                db.execute(statement)

    def _owner(self, actor):
        if actor != self.owner_principal:
            raise PermissionError('owner_approval_required')

    def _audit(self, db, actor, operation, identity, detail):
        db.execute('INSERT INTO managed_allocation_audit(actor,operation,operation_id,detail,created) VALUES(?,?,?,?,?)',
                   (actor, operation, identity, _metadata(detail, 'allocation_audit'), self.store.clock()))

    @staticmethod
    def _pool(db, identity):
        row = db.execute('SELECT * FROM resource_pools WHERE id=?', (identity,)).fetchone()
        if not row:
            raise ValueError('resource_pool_not_found')
        return row

    @staticmethod
    def _pool_live(row, now):
        if row['revoked'] or row['deadline'] <= now:
            raise Conflict('resource_pool_not_live')

    @staticmethod
    def _dimensions(value):
        if not isinstance(value, dict) or not value or len(value) > 64:
            raise ValueError('invalid_pool_dimensions')
        result = {}
        for dimension, spec in value.items():
            _name(dimension, 'dimension')
            if not isinstance(spec, dict) or set(spec) != {'capacity', 'unit'}:
                raise ValueError('invalid_pool_dimension')
            result[dimension] = {'capacity': _quanta(spec['capacity'], 'capacity'),
                                 'unit': _name(spec['unit'], 'unit', 100)}
        return result

    @staticmethod
    def _held(db, pool_id, dimension):
        # Accepted/running/unknown reservations are held regardless of any lease
        # or pool epoch.  Only explicit settlement can prove them quiescent.
        rows = db.execute('''SELECT d.quantity FROM managed_allocation_demands d
            JOIN managed_allocations a ON a.operation_id=d.operation_id
            WHERE d.pool_id=? AND d.dimension=?
            AND a.state IN ('reserved','accepted','running','unknown')''', (pool_id, dimension))
        return sum(row['quantity'] for row in rows)

    def define_pool(self, owner, id, dimensions, lease_seconds=90, expected_epoch=None):
        self._owner(owner)
        identity = _name(id, 'pool_id')
        dimensions = self._dimensions(dimensions)
        encoded, seconds = _metadata(dimensions, 'pool_dimensions'), _ttl(lease_seconds)
        with self.store.transaction() as db:
            now = self.store.clock()
            self._expire_in_db(db, now)
            row = db.execute('SELECT * FROM resource_pools WHERE id=?', (identity,)).fetchone()
            if row:
                if row['revoked']:
                    raise Conflict('resource_pool_revoked')
                if row['dimensions'] == encoded and row['deadline'] > now:
                    if expected_epoch is not None and row['epoch'] != _epoch(expected_epoch):
                        raise Conflict('stale_pool_epoch')
                    return self._pool_view(db, row)
                if expected_epoch is None or row['epoch'] != _epoch(expected_epoch):
                    raise Conflict('stale_pool_epoch')
                old = json.loads(row['dimensions'])
                for dimension in old:
                    held = self._held(db, identity, dimension)
                    if held and (dimension not in dimensions or old[dimension]['unit'] != dimensions[dimension]['unit']
                                 or dimensions[dimension]['capacity'] < held):
                        raise Conflict('pool_capacity_still_held')
                db.execute('UPDATE resource_pools SET dimensions=?,epoch=epoch+1,deadline=?,updated=? WHERE id=?',
                           (encoded, now + seconds, now, identity))
            else:
                if expected_epoch is not None:
                    raise Conflict('resource_pool_not_registered')
                db.execute('INSERT INTO resource_pools VALUES(?,?,1,?,0,?,?)',
                           (identity, encoded, now + seconds, now, now))
            result = self._pool_view(db, self._pool(db, identity))
            self._audit(db, owner, 'define_pool', None, {'pool_id': identity, 'epoch': result['epoch']})
            return result

    def renew_pool(self, owner, id, epoch, lease_seconds=90):
        self._owner(owner)
        _name(id, 'pool_id')
        seconds = _ttl(lease_seconds)
        with self.store.transaction() as db:
            row, now = self._pool(db, id), self.store.clock()
            if row['epoch'] != _epoch(epoch):
                raise Conflict('stale_pool_epoch')
            self._pool_live(row, now)
            db.execute('UPDATE resource_pools SET deadline=?,updated=? WHERE id=?', (now + seconds, now, id))
            self._audit(db, owner, 'renew_pool', None, {'pool_id': id, 'epoch': epoch})
            return self._pool_view(db, self._pool(db, id))

    def revoke_pool(self, owner, id, epoch):
        self._owner(owner)
        _name(id, 'pool_id')
        with self.store.transaction() as db:
            row = self._pool(db, id)
            if row['epoch'] != _epoch(epoch):
                raise Conflict('stale_pool_epoch')
            if not row['revoked']:
                db.execute('UPDATE resource_pools SET revoked=1,updated=? WHERE id=?', (self.store.clock(), id))
                self._audit(db, owner, 'revoke_pool', None, {'pool_id': id, 'epoch': epoch})
            return self._pool_view(db, self._pool(db, id))

    def _pool_view(self, db, row):
        result = dict(row)
        result['dimensions'] = json.loads(row['dimensions'])
        result['revoked'] = bool(row['revoked'])
        result['available'] = not row['revoked'] and row['deadline'] > self.store.clock()
        for dimension, spec in result['dimensions'].items():
            spec['held'] = self._held(db, row['id'], dimension)
            spec['remaining'] = max(0, spec['capacity'] - spec['held'])
        result['capacity_verification'] = 'owner_contract'
        result['native_resource_isolation'] = False
        return result

    def pools(self, actor):
        _name(actor, 'actor')
        with self.store.transaction() as db:
            return {'pools': [self._pool_view(db, row) for row in db.execute('SELECT * FROM resource_pools ORDER BY id')],
                    'as_of': self.store.clock(), 'execution_authorized': False}

    def bind_pool(self, owner, capability_id, capability_epoch, pool_id, pool_epoch,
                  dimension, quantity, unit, usage_key=None, expected_epoch=None, new_spend_minor=0):
        self._owner(owner)
        for value, label in ((capability_id, 'capability_id'), (pool_id, 'pool_id'), (dimension, 'dimension')):
            _name(value, label)
        quantity, unit = _quanta(quantity), _name(unit, 'unit', 100)
        usage_key = _name(capability_id if usage_key is None else usage_key, 'usage_key')
        if isinstance(new_spend_minor, bool) or not isinstance(new_spend_minor, int) or new_spend_minor != 0:
            raise PermissionError('new_spend_not_authorized')
        with self.store.transaction() as db:
            now, cap, pool = self.store.clock(), self.registry._find(db, capability_id), self._pool(db, pool_id)
            if cap['epoch'] != _epoch(capability_epoch) or not self.registry._live(cap, now):
                raise Conflict('binding_capability_stale')
            if pool['epoch'] != _epoch(pool_epoch):
                raise Conflict('stale_pool_epoch')
            self._pool_live(pool, now)
            spec = json.loads(pool['dimensions']).get(dimension)
            if not spec or spec['unit'] != unit:
                raise Conflict('pool_dimension_unit_mismatch')
            if quantity > spec['capacity']:
                raise Conflict('binding_exceeds_pool_capacity')
            identity = (capability_id, pool_id, dimension)
            row = db.execute('SELECT * FROM capability_pool_bindings WHERE capability_id=? AND pool_id=? AND dimension=?', identity).fetchone()
            body = (capability_epoch, pool_epoch, quantity, unit, usage_key, new_spend_minor)
            if row:
                same = body == (row['capability_epoch'], row['pool_epoch'], row['quantity'], row['unit'], row['usage_key'], row['new_spend_minor'])
                if same:
                    if expected_epoch is not None and row['epoch'] != _epoch(expected_epoch):
                        raise Conflict('stale_binding_epoch')
                    return dict(row)
                if expected_epoch is None or row['epoch'] != _epoch(expected_epoch):
                    raise Conflict('stale_binding_epoch')
                db.execute('''UPDATE capability_pool_bindings SET capability_epoch=?,pool_epoch=?,quantity=?,unit=?,
                    usage_key=?,new_spend_minor=?,epoch=epoch+1 WHERE capability_id=? AND pool_id=? AND dimension=?''', body + identity)
            else:
                if expected_epoch is not None:
                    raise Conflict('capability_pool_binding_not_registered')
                db.execute('INSERT INTO capability_pool_bindings VALUES(?,?,?,?,?,?,?,?,1,?)',
                           (capability_id, capability_epoch, pool_id, pool_epoch, dimension, quantity, unit, usage_key, new_spend_minor))
            result = dict(db.execute('SELECT * FROM capability_pool_bindings WHERE capability_id=? AND pool_id=? AND dimension=?', identity).fetchone())
            self._audit(db, owner, 'bind_pool', None, {'capability_id': capability_id, 'pool_id': pool_id,
                                                    'dimension': dimension, 'epoch': result['epoch']})
            return result

    def bindings(self, actor, capability_id):
        _name(actor, 'actor')
        _name(capability_id, 'capability_id')
        with self.store.transaction() as db:
            return {'bindings': [dict(row) for row in db.execute('SELECT * FROM capability_pool_bindings WHERE capability_id=? ORDER BY pool_id,dimension', (capability_id,))],
                    'execution_authorized': False}

    @staticmethod
    def _plan(plan):
        if not isinstance(plan, dict) or set(plan) - {'selections', 'route', 'target'}:
            raise ValueError('invalid_allocation_plan')
        selections = plan.get('selections')
        if not isinstance(selections, list) or not 1 <= len(selections) <= 32:
            raise ValueError('invalid_allocation_selections')
        result, seen = [], set()
        for selection in selections:
            required = {'capability_id', 'epoch', 'action', 'scope', 'workload', 'observations'}
            if not isinstance(selection, dict) or required - set(selection) or set(selection) - required - {'grant_id'}:
                raise ValueError('invalid_allocation_selection')
            identity = _name(selection['capability_id'], 'capability_id')
            if identity in seen:
                raise ValueError('duplicate_allocation_selection')
            seen.add(identity)
            _epoch(selection['epoch'])
            _name(selection['action'], 'action')
            if selection['scope'] in (None, '', {}, []):
                raise ValueError('invalid_scope')
            if not isinstance(selection['workload'], dict) or not selection['workload']:
                raise ValueError('invalid_allocation_workload')
            if not isinstance(selection['observations'], list) or not 1 <= len(selection['observations']) <= 16:
                raise ValueError('independent_measurement_required')
            if 'grant_id' in selection:
                _name(selection['grant_id'], 'grant_id', 256)
            result.append(dict(selection))
        route = plan.get('route', [])
        if not isinstance(route, list) or len(route) > 32:
            raise ValueError('invalid_allocation_route')
        target = _name(plan.get('target'), 'target')
        if target not in seen:
            raise ValueError('allocation_target_not_selected')
        result.sort(key=lambda item: item['capability_id'])
        normalized = {'selections': result, 'route': route, 'target': target}
        # Detach nested model/caller objects before fingerprinting and admitting.
        return json.loads(_metadata(normalized, 'allocation_plan'))

    def _task(self, db, actor, task_id, task_epoch, now, leader_epoch=None):
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not actor.startswith('node:') or not actor[5:]:
            raise PermissionError('allocation_task_principal_mismatch')
        node = actor[5:]
        if task and task['node'] != node:
            raise PermissionError('allocation_task_principal_mismatch')
        if not self.store._task_is_live(db, task, node=node, epoch=task_epoch):
            raise Conflict('stale_task_lease')
        deadline, current = task['deadline'], None
        if 'leader' in json.loads(task['required']):
            leader = db.execute('SELECT * FROM leader').fetchone()
            if leader_epoch is not None and leader['epoch'] != leader_epoch:
                raise Conflict('stale_leader_epoch')
            deadline, current = min(deadline, leader['deadline']), task['leader_epoch']
        return task, deadline, current

    def _authorize(self, db, actor, cap, selection, now):
        if actor == cap['principal']:
            return cap['deadline']
        scope = _metadata(selection['scope'], 'scope', 8192)
        where = 'principal=? AND capability_id=? AND capability_epoch=? AND action=? AND scope=? AND revoked=0 AND deadline>?'
        values = [actor, cap['id'], cap['epoch'], selection['action'], scope, now]
        if 'grant_id' in selection:
            where += ' AND id=?'
            values.append(selection['grant_id'])
        grant = db.execute('SELECT * FROM capability_grants WHERE ' + where + ' ORDER BY deadline DESC,id LIMIT 1', values).fetchone()
        if not grant or grant['approved_by'] != self.owner_principal:
            raise PermissionError('exact_grant_required')
        return min(cap['deadline'], grant['deadline'])

    def _observation(self, db, requirement, cap, expected, now):
        fields = {'observation_id', 'metric', 'unit', 'max_age_seconds'}
        if (not isinstance(requirement, dict) or fields - set(requirement)
                or set(requirement) - fields - {'predicate'}):
            raise ValueError('invalid_measurement_requirement')
        for key in ('observation_id', 'metric', 'unit'):
            _name(requirement[key], key)
        max_age = _ttl(requirement['max_age_seconds'])
        row = db.execute('SELECT * FROM capability_metrics WHERE id=?', (requirement['observation_id'],)).fetchone()
        if (not row or row['capability_id'] != cap['id'] or row['capability_epoch'] != cap['epoch']
                or row['verification'] != 'verified' or row['actor'] not in self.registry.trusted_verifiers
                or row['actor'] == cap['principal'] or row['metric'] != requirement['metric']
                or row['unit'] != requirement['unit']):
            raise Conflict('independent_measurement_required')
        if row['sample_time'] > now or row['sample_time'] + max_age <= now:
            raise Conflict('measurement_not_fresh')
        evidence = json.loads(row['evidence'])
        if not isinstance(evidence, dict) or any(key not in evidence or _json(evidence[key], 'measurement') != _json(value, 'measurement')
                                                for key, value in expected.items()):
            raise Conflict('measurement_scope_workload_mismatch')
        if 'predicate' in requirement:
            predicate = requirement['predicate']
            if (not isinstance(predicate, dict) or set(predicate) != {'op', 'value'}
                    or predicate['op'] not in ('eq', 'lt', 'lte', 'gt', 'gte')):
                raise ValueError('invalid_measurement_predicate')
            value = json.loads(row['value'])
            if predicate['op'] == 'eq':
                matches = _json(value, 'metric_value') == _json(predicate['value'], 'metric_value')
            else:
                try:
                    left, right = _finite(value, 'metric_value'), _finite(predicate['value'], 'metric_threshold')
                except ValueError:
                    raise Conflict('measurement_predicate_not_satisfied')
                matches = {'lt': left < right, 'lte': left <= right, 'gt': left > right, 'gte': left >= right}[predicate['op']]
            if not matches:
                raise Conflict('measurement_predicate_not_satisfied')
        return row['sample_time'] + max_age

    def _resolve(self, db, actor, task_id, task_epoch, plan, now, previous=None):
        task, deadline, leader_epoch = self._task(db, actor, task_id, task_epoch, now,
                                                None if previous is None else previous['leader_epoch'])
        caps, bindings, uses, selections = {}, [], {}, {}
        for selection in plan['selections']:
            cap = self.registry._find(db, selection['capability_id'])
            if cap['epoch'] != selection['epoch'] or not self.registry._live(cap, now):
                raise Conflict('allocation_capability_stale')
            caps[cap['id']] = cap
            selections[cap['id']] = selection
            deadline = min(deadline, self._authorize(db, actor, cap, selection, now))
            expected = {'scope': selection['scope'], 'workload': selection['workload']}
            for observation in selection['observations']:
                deadline = min(deadline, self._observation(db, observation, cap, expected, now))
            bound = db.execute('SELECT * FROM capability_pool_bindings WHERE capability_id=? ORDER BY pool_id,dimension', (cap['id'],)).fetchall()
            if not bound:
                raise Conflict('capability_pool_binding_required')
            for row in bound:
                pool = self._pool(db, row['pool_id'])
                if row['capability_epoch'] != cap['epoch'] or row['pool_epoch'] != pool['epoch']:
                    raise Conflict('allocation_binding_stale')
                self._pool_live(pool, now)
                if row['new_spend_minor'] != 0:
                    raise PermissionError('new_spend_not_authorized')
                dimension = json.loads(pool['dimensions']).get(row['dimension'])
                if not dimension or dimension['unit'] != row['unit']:
                    raise Conflict('pool_dimension_unit_mismatch')
                bindings.append(dict(row))
                key = (row['pool_id'], row['dimension'], row['usage_key'])
                uses[key] = max(uses.get(key, 0), row['quantity'])
                deadline = min(deadline, pool['deadline'])
        last, seen_edges = None, set()
        for item in plan['route']:
            if not isinstance(item, dict) or set(item) != {'edge_id', 'source_epoch', 'target_epoch', 'observation'}:
                raise ValueError('invalid_route_hop')
            _name(item['edge_id'], 'edge_id')
            if item['edge_id'] in seen_edges:
                raise ValueError('duplicate_route_hop')
            seen_edges.add(item['edge_id'])
            edge = db.execute('SELECT * FROM capability_edges WHERE id=?', (item['edge_id'],)).fetchone()
            if (not edge or edge['deadline'] <= now or edge['source'] not in caps or edge['target'] not in caps
                    or edge['source_epoch'] != _epoch(item['source_epoch']) or edge['target_epoch'] != _epoch(item['target_epoch'])
                    or caps[edge['source']]['epoch'] != edge['source_epoch'] or caps[edge['target']]['epoch'] != edge['target_epoch']):
                raise Conflict('allocation_route_stale')
            if last is not None and edge['source'] != last:
                raise Conflict('allocation_route_not_contiguous')
            if edge['actor'] != caps[edge['source']]['principal']:
                raise Conflict('allocation_route_authority_mismatch')
            expected = {'edge_id': edge['id'], 'target': edge['target'], 'source_epoch': edge['source_epoch'],
                        'target_epoch': edge['target_epoch'], 'reachable': True,
                        'scope': selections[edge['source']]['scope'], 'workload': selections[edge['source']]['workload']}
            deadline = min(deadline, edge['deadline'], self._observation(db, item['observation'], caps[edge['source']], expected, now))
            last = edge['target']
        if plan['route'] and last != plan['target']:
            raise Conflict('allocation_route_target_mismatch')
        demands = {}
        for (pool_id, dimension, usage_key), quantity in uses.items():
            key = (pool_id, dimension)
            demands[key] = demands.get(key, 0) + quantity
            _quanta(demands[key])
        resolved = {'task_node': task['node'], 'leader_epoch': leader_epoch,
                    'providers': {identity: cap['principal'] for identity, cap in caps.items()}, 'bindings': bindings,
                    'demands': [{'pool_id': key[0], 'dimension': key[1], 'quantity': quantity,
                                 'pool_epoch': self._pool(db, key[0])['epoch'],
                                 'unit': json.loads(self._pool(db, key[0])['dimensions'])[key[1]]['unit']}
                                for key, quantity in sorted(demands.items())]}
        if previous is not None and _json(resolved, 'resolved') != _json(previous, 'resolved'):
            raise Conflict('allocation_resolution_changed')
        return resolved, deadline

    def reserve(self, actor, operation_id, task_id, task_epoch, plan, lease_seconds=60):
        _name(actor, 'actor')
        _name(operation_id, 'operation_id')
        _name(task_id, 'task_id')
        _epoch(task_epoch)
        plan, seconds = self._plan(plan), _ttl(lease_seconds)
        fingerprint = _digest([actor, operation_id, task_id, task_epoch, plan, seconds])
        with self.store.transaction() as db:
            now = self.store.clock()
            old = db.execute('SELECT * FROM managed_allocations WHERE operation_id=?', (operation_id,)).fetchone()
            if old:
                if old['actor'] != actor:
                    raise PermissionError('allocation_principal_mismatch')
                if old['fingerprint'] != fingerprint:
                    raise Conflict('allocation_operation_content_conflict')
                # reserve is a managed task action even for an idempotent retry.
                # Historical/unknown outcomes remain readable through inspect.
                self._task(db, actor, task_id, task_epoch, now, old['leader_epoch'])
                return dict(self._view(db, old), reservation_created=False)
            self._expire_in_db(db, now)
            resolved, valid_until = self._resolve(db, actor, task_id, task_epoch, plan, now)
            for demand in resolved['demands']:
                pool = self._pool(db, demand['pool_id'])
                capacity = json.loads(pool['dimensions'])[demand['dimension']]['capacity']
                if self._held(db, demand['pool_id'], demand['dimension']) + demand['quantity'] > capacity:
                    raise Conflict('resource_pool_capacity_exhausted')
            deadline = min(now + seconds, valid_until)
            db.execute('INSERT INTO managed_allocations VALUES(?,?,?,?,?,?,?,?,?,\'reserved\',1,?,?,?)',
                       (operation_id, fingerprint, actor, task_id, task_epoch, resolved['task_node'], resolved['leader_epoch'],
                        _metadata(plan, 'allocation_plan'), _metadata(resolved, 'allocation_resolved'), deadline, now, now))
            for demand in resolved['demands']:
                db.execute('INSERT INTO managed_allocation_demands VALUES(?,?,?,?,?,?)',
                           (operation_id, demand['pool_id'], demand['pool_epoch'], demand['dimension'], demand['quantity'], demand['unit']))
            for identity, provider in resolved['providers'].items():
                db.execute('INSERT INTO managed_dispatch VALUES(?,?,?,\'pending\',1,NULL,NULL,?,?)', (operation_id, identity, provider, now, now))
            self._audit(db, actor, 'reserve', operation_id, {'task_id': task_id, 'task_epoch': task_epoch})
            return dict(self._view(db, self._allocation(db, operation_id)), reservation_created=True)

    @staticmethod
    def _allocation(db, identity):
        row = db.execute('SELECT * FROM managed_allocations WHERE operation_id=?', (identity,)).fetchone()
        if not row:
            raise ValueError('managed_allocation_not_found')
        return row

    @staticmethod
    def _dispatch(db, operation_id, capability_id, actor):
        row = db.execute('SELECT * FROM managed_dispatch WHERE operation_id=? AND capability_id=?', (operation_id, capability_id)).fetchone()
        if not row:
            raise ValueError('managed_dispatch_not_found')
        if row['provider'] != actor:
            raise PermissionError('managed_provider_principal_mismatch')
        return row

    def _admission(self, db, allocation, now):
        if allocation['state'] not in ('reserved', 'accepted', 'running') or allocation['deadline'] <= now:
            raise Conflict('allocation_not_admissible')
        self._resolve(db, allocation['actor'], allocation['task_id'], allocation['task_epoch'],
                      json.loads(allocation['plan']), now, json.loads(allocation['resolved']))

    @staticmethod
    def _dispatch_view(row):
        result = dict(row)
        result['settlement'] = json.loads(row['settlement']) if row['settlement'] else None
        result['execution_verification'] = 'provider_reported' if row['settlement'] else 'not_verified'
        result['execution_authorized'] = False
        return result

    def _view(self, db, row):
        result = dict(row)
        result.pop('fingerprint')
        result['plan'], result['resolved'] = json.loads(row['plan']), json.loads(row['resolved'])
        result['dispatch'] = [self._dispatch_view(item) for item in db.execute('SELECT * FROM managed_dispatch WHERE operation_id=? ORDER BY capability_id', (row['operation_id'],))]
        result['capacity_held'] = row['state'] in _HELD
        result['lease_expired'] = row['deadline'] <= self.store.clock()
        result['execution_verified'] = False
        result['execution_authorized'] = False
        result['native_tools_intercepted'] = False
        return result

    def inspect(self, actor, operation_id):
        _name(actor, 'actor')
        _name(operation_id, 'operation_id')
        with self.store.transaction() as db:
            row = self._allocation(db, operation_id)
            provider = db.execute('SELECT 1 FROM managed_dispatch WHERE operation_id=? AND provider=?', (operation_id, actor)).fetchone()
            if actor not in (self.owner_principal, row['actor']) and not provider:
                raise PermissionError('allocation_principal_mismatch')
            return self._view(db, row)

    def pending(self, actor, limit=100):
        _name(actor, 'actor')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('invalid_dispatch_limit')
        with self.store.transaction() as db:
            rows = db.execute('''SELECT d.* FROM managed_dispatch d JOIN managed_allocations a
                ON a.operation_id=d.operation_id WHERE d.provider=? AND d.state='pending'
                AND a.state IN ('reserved','accepted','running') AND a.deadline>?
                ORDER BY d.created,d.operation_id,d.capability_id LIMIT ?''', (actor, self.store.clock(), limit)).fetchall()
            result = []
            for row in rows:
                allocation = self._allocation(db, row['operation_id'])
                selection = next(item for item in json.loads(allocation['plan'])['selections'] if item['capability_id'] == row['capability_id'])
                result.append(dict(self._dispatch_view(row), selection=selection, task_id=allocation['task_id'],
                                   task_epoch=allocation['task_epoch'], deadline=allocation['deadline'], admission_required=True))
            return {'dispatch': result, 'execution_authorized': False}

    def accept(self, actor, operation_id, capability_id, epoch, receipt_id):
        for value, label in ((actor, 'actor'), (operation_id, 'operation_id'), (capability_id, 'capability_id'), (receipt_id, 'receipt_id')):
            _name(value, label)
        _epoch(epoch)
        with self.store.transaction() as db:
            dispatch = self._dispatch(db, operation_id, capability_id, actor)
            if dispatch['receipt_id'] is not None:
                if dispatch['receipt_id'] != receipt_id:
                    raise Conflict('provider_receipt_content_conflict')
                return dict(self._dispatch_view(dispatch), accepted_new=False)
            if dispatch['epoch'] != epoch or dispatch['state'] != 'pending':
                raise Conflict('stale_dispatch_epoch')
            allocation, now = self._allocation(db, operation_id), self.store.clock()
            self._admission(db, allocation, now)
            if db.execute('SELECT 1 FROM managed_dispatch WHERE provider=? AND receipt_id=?', (actor, receipt_id)).fetchone():
                raise Conflict('provider_receipt_already_bound')
            db.execute("UPDATE managed_dispatch SET state='accepted',epoch=epoch+1,receipt_id=?,updated=? WHERE operation_id=? AND capability_id=?", (receipt_id, now, operation_id, capability_id))
            if allocation['state'] == 'reserved':
                db.execute("UPDATE managed_allocations SET state='accepted',epoch=epoch+1,updated=? WHERE operation_id=?", (now, operation_id))
            self._audit(db, actor, 'accept', operation_id, {'capability_id': capability_id})
            return dict(self._dispatch_view(self._dispatch(db, operation_id, capability_id, actor)), accepted_new=True)

    def start(self, actor, operation_id, capability_id, epoch, receipt_id):
        for value, label in ((actor, 'actor'), (operation_id, 'operation_id'), (capability_id, 'capability_id'), (receipt_id, 'receipt_id')):
            _name(value, label)
        _epoch(epoch)
        with self.store.transaction() as db:
            dispatch = self._dispatch(db, operation_id, capability_id, actor)
            if dispatch['receipt_id'] != receipt_id:
                raise Conflict('provider_receipt_content_conflict')
            if dispatch['state'] != 'accepted':
                if dispatch['state'] in ('running', 'unknown') + _SETTLED:
                    return dict(self._dispatch_view(dispatch), execute_once=False)
                raise Conflict('dispatch_not_accepted')
            if dispatch['epoch'] != epoch:
                raise Conflict('stale_dispatch_epoch')
            allocation, now = self._allocation(db, operation_id), self.store.clock()
            self._admission(db, allocation, now)
            db.execute("UPDATE managed_dispatch SET state='running',epoch=epoch+1,updated=? WHERE operation_id=? AND capability_id=?", (now, operation_id, capability_id))
            db.execute("UPDATE managed_allocations SET state='running',epoch=epoch+1,updated=? WHERE operation_id=?", (now, operation_id))
            self._audit(db, actor, 'start', operation_id, {'capability_id': capability_id})
            return dict(self._dispatch_view(self._dispatch(db, operation_id, capability_id, actor)), execute_once=True)

    def unknown(self, actor, operation_id, capability_id, epoch):
        for value, label in ((actor, 'actor'), (operation_id, 'operation_id'), (capability_id, 'capability_id')):
            _name(value, label)
        _epoch(epoch)
        with self.store.transaction() as db:
            dispatch = self._dispatch(db, operation_id, capability_id, actor)
            if dispatch['state'] == 'unknown':
                return self._dispatch_view(dispatch)
            if dispatch['epoch'] != epoch or dispatch['state'] not in ('accepted', 'running'):
                raise Conflict('stale_dispatch_epoch')
            now = self.store.clock()
            db.execute("UPDATE managed_dispatch SET state='unknown',epoch=epoch+1,updated=? WHERE operation_id=? AND capability_id=?", (now, operation_id, capability_id))
            db.execute("UPDATE managed_allocations SET state='unknown',epoch=epoch+1,updated=? WHERE operation_id=?", (now, operation_id))
            self._audit(db, actor, 'unknown', operation_id, {'capability_id': capability_id})
            return self._dispatch_view(self._dispatch(db, operation_id, capability_id, actor))

    def settle(self, actor, operation_id, capability_id, epoch, receipt_id, outcome, evidence):
        for value, label in ((actor, 'actor'), (operation_id, 'operation_id'), (capability_id, 'capability_id'), (receipt_id, 'receipt_id')):
            _name(value, label)
        _epoch(epoch)
        if outcome not in _SETTLED:
            raise ValueError('invalid_provider_outcome')
        if not isinstance(evidence, dict) or evidence.get('resource_quiescent') is not True or not evidence.get('result_reference'):
            raise ValueError('provider_quiescence_evidence_required')
        settlement = _metadata({'outcome': outcome, 'evidence': evidence}, 'settlement', 8192)
        with self.store.transaction() as db:
            dispatch = self._dispatch(db, operation_id, capability_id, actor)
            if dispatch['receipt_id'] != receipt_id:
                raise Conflict('provider_receipt_content_conflict')
            if dispatch['settlement'] is not None:
                if dispatch['settlement'] != settlement:
                    raise Conflict('provider_settlement_content_conflict')
                return self._dispatch_view(dispatch)
            if dispatch['epoch'] != epoch or dispatch['state'] not in ('accepted', 'running', 'unknown'):
                raise Conflict('stale_dispatch_epoch')
            if dispatch['state'] == 'accepted' and outcome == 'completed':
                raise Conflict('dispatch_not_started')
            # Recording an outcome after revocation/lease loss is allowed.  It
            # cannot begin a new effect and is needed to release a genuine hold.
            now = self.store.clock()
            db.execute('UPDATE managed_dispatch SET state=?,epoch=epoch+1,settlement=?,updated=? WHERE operation_id=? AND capability_id=?', (outcome, settlement, now, operation_id, capability_id))
            unsettled = db.execute("SELECT 1 FROM managed_dispatch WHERE operation_id=? AND state NOT IN ('completed','stopped')", (operation_id,)).fetchone()
            if not unsettled:
                db.execute("UPDATE managed_allocations SET state='completed',epoch=epoch+1,updated=? WHERE operation_id=?", (now, operation_id))
            self._audit(db, actor, 'settle', operation_id, {'capability_id': capability_id, 'outcome': outcome,
                                                        'verification': 'provider_reported'})
            return self._dispatch_view(self._dispatch(db, operation_id, capability_id, actor))

    def decline(self, actor, operation_id, capability_id, epoch):
        """Stop a still-pending dispatch without falsely settling an accepted one.

        This permits multi-provider operations to reach a quiescent terminal state
        when a different hop is unknown or the original task has lost its lease.
        """
        for value, label in ((actor, 'actor'), (operation_id, 'operation_id'), (capability_id, 'capability_id')):
            _name(value, label)
        _epoch(epoch)
        with self.store.transaction() as db:
            dispatch = self._dispatch(db, operation_id, capability_id, actor)
            settlement = _metadata({'outcome': 'stopped', 'evidence': {'resource_quiescent': True,
                                    'result_reference': 'journal:never-accepted'}}, 'settlement')
            if dispatch['state'] == 'stopped' and dispatch['settlement'] == settlement:
                return self._dispatch_view(dispatch)
            if dispatch['epoch'] != epoch or dispatch['state'] != 'pending' or dispatch['receipt_id'] is not None:
                raise Conflict('accepted_dispatch_requires_settlement')
            now = self.store.clock()
            db.execute("UPDATE managed_dispatch SET state='stopped',epoch=epoch+1,settlement=?,updated=? WHERE operation_id=? AND capability_id=?",
                       (settlement, now, operation_id, capability_id))
            unsettled = db.execute("SELECT 1 FROM managed_dispatch WHERE operation_id=? AND state NOT IN ('completed','stopped')", (operation_id,)).fetchone()
            if not unsettled:
                db.execute("UPDATE managed_allocations SET state='completed',epoch=epoch+1,updated=? WHERE operation_id=?", (now, operation_id))
            self._audit(db, actor, 'decline_pending', operation_id, {'capability_id': capability_id})
            return self._dispatch_view(self._dispatch(db, operation_id, capability_id, actor))

    def cancel(self, actor, operation_id, epoch):
        _name(actor, 'actor')
        _name(operation_id, 'operation_id')
        _epoch(epoch)
        with self.store.transaction() as db:
            allocation = self._allocation(db, operation_id)
            if actor not in (allocation['actor'], self.owner_principal):
                raise PermissionError('allocation_principal_mismatch')
            if allocation['state'] == 'cancelled':
                return self._view(db, allocation)
            if allocation['epoch'] != epoch or allocation['state'] != 'reserved':
                raise Conflict('accepted_allocation_requires_settlement')
            now = self.store.clock()
            db.execute("UPDATE managed_allocations SET state='cancelled',epoch=epoch+1,updated=? WHERE operation_id=?", (now, operation_id))
            db.execute("UPDATE managed_dispatch SET state='cancelled',epoch=epoch+1,updated=? WHERE operation_id=? AND state='pending'", (now, operation_id))
            self._audit(db, actor, 'cancel', operation_id, {})
            return self._view(db, self._allocation(db, operation_id))

    def _expire_in_db(self, db, now):
        rows = db.execute("SELECT operation_id FROM managed_allocations WHERE state='reserved' AND deadline<=?", (now,)).fetchall()
        for row in rows:
            identity = row['operation_id']
            db.execute("UPDATE managed_allocations SET state='expired',epoch=epoch+1,updated=? WHERE operation_id=? AND state='reserved'", (now, identity))
            db.execute("UPDATE managed_dispatch SET state='expired',epoch=epoch+1,updated=? WHERE operation_id=? AND state='pending'", (now, identity))
            self._audit(db, self.owner_principal, 'expire_unaccepted', identity, {})
        return len(rows)

    def expire(self, owner):
        self._owner(owner)
        with self.store.transaction() as db:
            return {'expired_unaccepted': self._expire_in_db(db, self.store.clock()), 'native_tools_intercepted': False}
