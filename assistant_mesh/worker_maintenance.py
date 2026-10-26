"""Private, finite worker claim-admission leases on the EXISTING receiver.

Attach in its original ready chain. No Store/API construction, migration,
recovery, model, worker lifecycle or native/task mutation occurs here.
"""
import json
import threading
from urllib.parse import urlsplit

from .store import Conflict


ROUTE = '/v1/worker-maintenance'
FORMAT = 'mesh-worker-maintenance/1'
PREFIX = 'amita.worker-maintenance.'


def _id(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('worker_maintenance_identity_invalid')
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _get(db, kind, identity):
    row = db.execute('SELECT value FROM meta WHERE key=?', (PREFIX + kind + ':' + identity,)).fetchone()
    return json.loads(row[0]) if row else None


def _put(db, kind, identity, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (PREFIX + kind + ':' + identity, _json(value)))


def _workers(api):
    return frozenset(_id(peer['node']) for unused_secret, peer in api.peers
                     if peer.get('role') == 'worker' and peer.get('node'))


class WorkerMaintenance:
    def __init__(self, api):
        self.api, self.store, self.workers = api, api.store, _workers(api)
        self.locks = {node: threading.RLock() for node in self.workers}

    @staticmethod
    def _operation(db, operation, node):
        record = _get(db, 'operation', operation) if operation else None
        if record is not None and record['node'] != node:
            raise Conflict('worker_maintenance_operation_content_changed')
        return record

    @staticmethod
    def _view(record, now):
        if record is None:
            return {'operation_id': None, 'state': 'none', 'live': False, 'expires_at': None}
        state = ('released' if record.get('released_at') is not None
                 else 'live' if now < record['expires_at'] else 'expired')
        return {'operation_id': record['operation_id'], 'state': state,
                'live': state == 'live', 'expires_at': record['expires_at']}

    def _status(self, db, node, now, operation=None, control=None):
        current = _get(db, 'lease', node)
        head = self._operation(db, current, node)
        selected = self._operation(db, operation, node) if operation else head
        lease = self._view(head, now)
        response = {'format': FORMAT, 'node': node, 'lease': lease,
            'operation': self._view(selected, now), 'claim_blocked': lease['live'],
            'task_state_changed': False, 'unknown_settled': False}
        if control:
            saved = _get(db, 'control', control)
            if saved and (saved['request']['node'] != node or
                          (operation and saved['request']['operation_id'] != operation)):
                raise Conflict('worker_maintenance_control_content_changed')
            response['control_id'] = control
            response['receipt'] = saved['receipt'] if saved else None
        return response

    def claim_blocked(self, node):
        with self.store.transaction() as db:
            return self._status(db, node, self.store.clock())['claim_blocked']

    def dispatch(self, peer, payload):
        if not isinstance(peer, dict) or peer.get('role') != 'operator':
            raise PermissionError('worker_maintenance_operator_required')
        if not isinstance(payload, dict):
            raise ValueError('worker_maintenance_payload_invalid')
        action, node = payload.get('action'), payload.get('node')
        if not isinstance(node, str) or node not in self.workers:
            raise ValueError('worker_maintenance_existing_worker_required')
        if action == 'status':
            if set(payload) - {'action', 'node', 'operation_id', 'control_id'}:
                raise ValueError('worker_maintenance_payload_invalid')
            operation = _id(payload['operation_id']) if 'operation_id' in payload else None
            control = _id(payload['control_id']) if 'control_id' in payload else None
            with self.locks[node], self.store.transaction() as db:
                return self._status(db, node, self.store.clock(), operation, control)
        if action not in ('acquire', 'renew', 'release'):
            raise ValueError('worker_maintenance_action_invalid')
        required = {'action', 'node', 'operation_id', 'control_id'}
        allowed = required | ({'ttl'} if action in ('acquire', 'renew') else set())
        if not required <= set(payload) or set(payload) - allowed:
            raise ValueError('worker_maintenance_payload_invalid')
        operation, control = _id(payload['operation_id']), _id(payload['control_id'])
        request = {key: payload[key] for key in required}
        if action in ('acquire', 'renew'):
            ttl = payload.get('ttl', 120)
            if type(ttl) is not int or not 15 <= ttl <= 600:
                raise ValueError('worker_maintenance_ttl_invalid')
            request['ttl'] = ttl
        with self.locks[node], self.store.transaction() as db:
            now = self.store.clock()
            saved = _get(db, 'control', control)
            if saved:
                if saved['request'] != request:
                    raise Conflict('worker_maintenance_control_content_changed')
                result = self._status(db, node, now, operation, control)
                result['replayed'] = True
                return result
            record = self._operation(db, operation, node)
            current = _get(db, 'lease', node)
            head = self._operation(db, current, node)
            applied = False
            if action == 'acquire':
                if record:
                    if record['initial_ttl'] != request['ttl']:
                        raise Conflict('worker_maintenance_operation_content_changed')
                    # An old operation is a tombstone forever, even with a new
                    # control ID. Neither expiry nor release re-acquires it.
                else:
                    if self._view(head, now)['live']:
                        raise Conflict('worker_maintenance_worker_already_held')
                    record = {'operation_id': operation, 'node': node, 'initial_ttl': request['ttl'],
                        'acquired_at': now, 'expires_at': now + request['ttl'], 'released_at': None}
                    _put(db, 'lease', node, operation)
                    applied = True
            elif action == 'renew':
                if (record is None or current != operation or not self._view(record, now)['live']):
                    raise Conflict('worker_maintenance_live_original_operation_required')
                record['expires_at'] = now + request['ttl']
                applied = True
            else:
                if record is None:
                    raise Conflict('worker_maintenance_original_operation_required')
                if record.get('released_at') is None:
                    if current != operation:
                        raise Conflict('worker_maintenance_release_holder_changed')
                    record['released_at'] = now
                    applied = True
            _put(db, 'operation', operation, record)
            receipt = {'action': action, 'operation_id': operation, 'control_id': control,
                'applied': applied, 'recorded_at': now, 'expires_at': record['expires_at']}
            _put(db, 'control', control, {'request': request, 'receipt': receipt})
            result = self._status(db, node, now, operation, control)
            result['replayed'] = False
            return result


def attach(api):
    """Compose the existing authenticated ready/dispatch chain, in memory only.

    One API/receiver is the owner. Every affected original short claim dispatch
    and maintenance acquisition uses the same finite per-node RLock. No DB
    transaction spans the original dispatch; no lock spans a model, network or
    worker lifecycle. Other routes and nodes use their original path unchanged.
    """
    installed = getattr(api, '_worker_maintenance', None)
    if installed is not None:
        if installed.store is not api.store or installed.workers != _workers(api):
            raise Conflict('worker_maintenance_attachment_changed')
        return installed
    maintenance = WorkerMaintenance(api)
    previous = api.dispatch

    def dispatch(method, path, payload, peer):
        if urlsplit(path).path == ROUTE:
            if method != 'POST' or path != ROUTE:
                raise PermissionError('worker_maintenance_private_route_required')
            return maintenance.dispatch(peer, payload)
        node = peer.get('node') if isinstance(peer, dict) else None
        if (method == 'POST' and path == '/v1/claim' and isinstance(peer, dict)
                and peer.get('role') == 'worker' and node in maintenance.workers):
            with maintenance.locks[node]:
                if maintenance.claim_blocked(node):
                    return {'task': None}
                return previous(method, path, payload, peer)
        return previous(method, path, payload, peer)

    api.dispatch = dispatch
    api._worker_maintenance = maintenance
    return maintenance
