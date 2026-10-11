"""Thin, task-bound composition of existing managed capability RPCs.

An owner-installed entrypoint supplies the client, private journal and task
binding; model input supplies only an immutable choice. This does not invoke a
provider, invent observations, approve grants, read remote files, intercept
native tools or retry an ambiguous write. Preserve the journal across restarts.
"""
import functools
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from .allocations import Allocations
from .resources import _epoch, _name, _ttl
from .routing import _decision


class CapabilityCallError(ValueError):
    """Fixed diagnostics only: never expose credentials or private paths."""


def _guard(function):
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CapabilityCallError:
            raise
        except Exception:
            raise CapabilityCallError('capability_call_failed') from None
    return guarded


def _json(value):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, allow_nan=False)
    if len(encoded.encode('utf8')) > 192 * 1024:
        raise CapabilityCallError('capability_call_request_too_large')
    return encoded


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def _journal_path(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise CapabilityCallError('capability_call_private_journal_required')
    for parent in (path.parent,) + tuple(path.parent.parents):
        metadata = parent.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in (0, os.getuid())
                or (metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX)):
            raise CapabilityCallError('capability_call_private_journal_required')
    metadata = path.parent.lstat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise CapabilityCallError('capability_call_private_journal_required')
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
        raise CapabilityCallError('capability_call_private_journal_required')
    return path


class TaskCapabilityCalls:
    """Install once per authenticated worker; inject its CURRENT task binding.

    ``client.request(path, body=None)`` must be owner-authenticated, bounded and
    must not automatically retry writes. Neither credentials nor these constructor
    arguments belong in the model-call schema. Journal identity is authority/node;
    each original call also retains its immutable task ID and creation epoch.
    """

    @_guard
    def __init__(self, client, journal_path, authority_id, node, task_id, task_epoch):
        if not callable(getattr(client, 'request', None)):
            raise CapabilityCallError('capability_call_client_required')
        self.client = client
        self.authority = _name(authority_id, 'authority')
        self.node = _name(node, 'node')
        self.task_id = _name(task_id, 'task_id')
        self.task_epoch = _epoch(task_epoch)
        self.path = _journal_path(journal_path)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS call_identity(authority TEXT, node TEXT)')
            rows = db.execute('SELECT authority,node FROM call_identity').fetchall()
            if not rows:
                db.execute('INSERT INTO call_identity VALUES(?,?)', (self.authority, self.node))
            elif len(rows) != 1 or tuple(rows[0]) != (self.authority, self.node):
                raise CapabilityCallError('capability_call_journal_identity_mismatch')
            db.execute('''CREATE TABLE IF NOT EXISTS capability_calls(
                operation_id TEXT PRIMARY KEY, decision_id TEXT UNIQUE NOT NULL,
                task_id TEXT NOT NULL, task_epoch INTEGER NOT NULL,
                request TEXT NOT NULL, fingerprint TEXT NOT NULL,
                propose TEXT NOT NULL, reserve TEXT NOT NULL, link TEXT NOT NULL)''')
            db.commit()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            yield db
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _row(self, operation_id):
        _name(operation_id, 'operation_id')
        with self._db() as db:
            row = db.execute('SELECT * FROM capability_calls WHERE operation_id=?',
                             (operation_id,)).fetchone()
        if not row:
            raise CapabilityCallError('capability_call_not_found')
        if row['task_id'] != self.task_id:
            raise CapabilityCallError('capability_call_task_identity_mismatch')
        return dict(row)

    def _remember(self, request):
        encoded, fingerprint = _json(request), _hash(request)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM capability_calls WHERE operation_id=?',
                             (request['operation_id'],)).fetchone()
            if row:
                if (row['task_id'] != self.task_id or row['request'] != encoded
                        or row['fingerprint'] != fingerprint):
                    raise CapabilityCallError('capability_call_identity_content_conflict')
            else:
                try:
                    db.execute('INSERT INTO capability_calls VALUES(?,?,?,?,?,?,?,?,?)',
                        (request['operation_id'], request['decision']['decision_id'], self.task_id,
                         self.task_epoch, encoded, fingerprint, 'ready', 'ready', 'ready'))
                except sqlite3.IntegrityError:
                    raise CapabilityCallError('capability_call_decision_identity_conflict') from None
            db.commit()
        return self._row(request['operation_id'])

    def _transition(self, operation_id, stage, before, after):
        # Internal stage constants, never model-supplied SQL identifiers. CAS
        # gives concurrent callers only one durable attempt at each write.
        if stage not in ('propose', 'reserve', 'link'):
            raise CapabilityCallError('capability_call_stage_invalid')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE capability_calls SET ' + stage + '=? WHERE operation_id=? AND '
                                 + stage + '=?', (after, operation_id, before)).rowcount
            db.commit()
        return bool(changed)

    def _routing(self, action, arguments):
        return self.client.request('/v1/routing/action', {'task_id': self.task_id,
            'epoch': self.task_epoch, 'action': action, 'arguments': arguments})

    def _allocation(self, action, arguments):
        return self.client.request('/v1/allocation/action', {'action': action, 'arguments': arguments})

    def _hello(self):
        value = self.client.request('/v1/mesh/hello')
        if (not isinstance(value, dict) or value.get('protocol') != 'mesh-a2a/1'
                or value.get('authority') != self.authority or value.get('node') != self.authority):
            raise CapabilityCallError('capability_call_authority_mismatch')

    def _context(self):
        value = self._routing('context', {})
        task = value.get('task') if isinstance(value, dict) else None
        if (not isinstance(value, dict) or value.get('format') != 'mesh-routing-context/1'
                or value.get('authority') != self.authority or not isinstance(task, dict)
                or task.get('id') != self.task_id or task.get('node') != self.node
                or type(task.get('epoch')) is not int or task['epoch'] != self.task_epoch):
            raise CapabilityCallError('capability_call_current_task_binding_invalid')
        return value

    @staticmethod
    def _request(operation_id, decision_id, selection, candidates, rationale, lease_seconds):
        try:
            _name(operation_id, 'operation_id')
            _name(decision_id, 'decision_id')
            # Reuse only pure protocol validators, never registry mutations.
            plan = Allocations._plan({'selections': [selection],
                                      'target': selection['capability_id'], 'route': []})
            selected = plan['selections'][0]
            for observation in selected['observations']:
                fields = {'observation_id', 'metric', 'unit', 'max_age_seconds'}
                if (not isinstance(observation, dict) or not fields <= set(observation)
                        or set(observation) - fields - {'predicate'}):
                    raise ValueError('invalid_observation_reference')
                for key in ('observation_id', 'metric', 'unit'):
                    _name(observation[key], key)
                _ttl(observation['max_age_seconds'])
            decision, unused = _decision({'decision_id': decision_id,
                'work': {'action': selected['action'], 'scope': selected['scope'],
                         'workload': selected['workload']},
                'candidates': candidates,
                'selection': {'kind': 'managed_allocation', 'operation_id': operation_id, 'plan': plan},
                'rationale': rationale,
                'evidence_refs': [dict(source='local_observation', capability_id=selected['capability_id'],
                    capability_epoch=selected['epoch'], **{key: observation[key]
                        for key in ('observation_id', 'metric', 'unit')})
                    for observation in selected['observations']]})
            return {'operation_id': operation_id, 'decision': decision,
                    'plan': plan, 'lease_seconds': _ttl(lease_seconds)}
        except Exception:
            raise CapabilityCallError('invalid_capability_call_request') from None

    def _route_view(self, value, row, require_link=False):
        request = json.loads(row['request'])
        decision, encoded = _decision(request['decision'])
        if (not isinstance(value, dict) or value.get('authority') != self.authority
                or value.get('task_id') != row['task_id'] or value.get('original_node') != self.node
                or type(value.get('original_epoch')) is not int or value['original_epoch'] != row['task_epoch']
                or value.get('decision_id') != row['decision_id'] or value.get('decision') != decision
                or value.get('content_sha256') != hashlib.sha256(encoded.encode('utf8')).hexdigest()):
            raise CapabilityCallError('capability_call_routing_receipt_invalid')
        link = value.get('linked_execution')
        if link is not None and (not isinstance(link, dict) or link.get('kind') != 'managed_allocation'
                                 or link.get('reference_id') != row['operation_id']):
            raise CapabilityCallError('capability_call_original_link_conflict')
        if require_link and link is None:
            raise CapabilityCallError('capability_call_link_ack_unknown')
        return value

    def _allocation_view(self, value, row):
        plan = json.loads(row['request'])['plan']
        if (not isinstance(value, dict) or value.get('operation_id') != row['operation_id']
                or value.get('actor') != 'node:' + self.node or value.get('task_node') != self.node
                or value.get('task_id') != row['task_id'] or type(value.get('task_epoch')) is not int
                or value['task_epoch'] != row['task_epoch']
                or value.get('plan') != plan
                or value.get('state') not in ('reserved', 'accepted', 'running', 'unknown', 'completed', 'cancelled', 'expired')
                or not isinstance(value.get('dispatch'), list)):
            raise CapabilityCallError('capability_call_allocation_receipt_invalid')
        providers = value.get('resolved', {}).get('providers') if isinstance(value.get('resolved'), dict) else None
        dispatch = value['dispatch']
        if (not isinstance(providers, dict) or set(providers) != {plan['target']}
                or len(dispatch) != 1 or not isinstance(dispatch[0], dict)
                or dispatch[0].get('operation_id') != row['operation_id']
                or dispatch[0].get('capability_id') != plan['target']
                or dispatch[0].get('provider') != providers[plan['target']]
                or not isinstance(providers[plan['target']], str)
                or not providers[plan['target']].startswith('node:')):
            raise CapabilityCallError('capability_call_dispatch_receipt_invalid')
        return value

    def _report(self, row, phase, allocation=None, routing=None, diagnostic=None):
        request = json.loads(row['request'])
        report = {'format': 'mesh-capability-call/1', 'authority': self.authority,
            'operation_id': row['operation_id'], 'decision_id': row['decision_id'],
            'task_id': row['task_id'], 'original_task_epoch': row['task_epoch'],
            'current_task_epoch': self.task_epoch, 'phase': phase,
            'request_sha256': row['fingerprint'], 'plan_sha256': _hash(request['plan']),
            'stages': {stage: row[stage] for stage in ('propose', 'reserve', 'link')},
            'operation_state': allocation.get('state') if allocation else None,
            'allocation': allocation, 'routing': routing,
            'diagnostics': [diagnostic] if diagnostic else [],
            'automatic_invocation_replay': False, 'retry_with_new_id': False,
            'native_tools_intercepted': False, 'execution_verified': False,
            'target_content_verified': False, 'authentication_verified': False,
            'inference_verified': False}
        return report

    @_guard
    def call(self, operation_id, decision_id, selection, candidates, rationale, lease_seconds=60):
        """Compose proposal/reservation/link once; the provider runs separately.

        Repeating this exact request reconciles original IDs. An attempted stage
        is NEVER resubmitted, even if lookup fails or the record is absent. A
        confirmed predecessor can unlock only an as-yet unattempted next stage.
        """
        request = self._request(operation_id, decision_id, selection, candidates, rationale, lease_seconds)
        # Check the actual largest write before any durable intent. Client uses
        # default JSON separators/ASCII escapes, not our compact journal encoding.
        payloads = [
            {'task_id': self.task_id, 'epoch': self.task_epoch, 'action': 'propose',
             'arguments': {'decision': request['decision']}},
            {'action': 'reserve', 'arguments': {'operation_id': operation_id,
             'task_id': self.task_id, 'task_epoch': self.task_epoch,
             'plan': request['plan'], 'lease_seconds': request['lease_seconds']}},
            {'task_id': self.task_id, 'epoch': self.task_epoch, 'action': 'link',
             'arguments': {'decision_id': decision_id, 'kind': 'managed_allocation',
                           'reference_id': operation_id}}]
        if any(len(json.dumps(payload, allow_nan=False).encode('utf8')) > 128 * 1024
               for payload in payloads):
            raise CapabilityCallError('capability_call_rpc_body_too_large')
        row = self._remember(request)
        try:
            self._hello()
            self._context()
        except Exception:
            return self._report(row, 'unresolved', diagnostic='authority_or_task_binding_unavailable')
        routing, allocation = None, None
        for stage in ('propose', 'reserve', 'link'):
            row = self._row(operation_id)
            try:
                if row[stage] == 'ready':
                    if row['task_epoch'] != self.task_epoch:
                        return self._report(row, 'original_epoch_read_only', allocation, routing,
                                            'original_task_epoch_changed_no_write')
                    if not self._transition(operation_id, stage, 'ready', 'attempted'):
                        row = self._row(operation_id)
                    else:
                        row = self._row(operation_id)
                        # Check advertised authority immediately before a write.
                        # A failure after durable intent stays ambiguous, never retried.
                        self._hello()
                        if stage == 'propose':
                            routing = self._route_view(self._routing('propose', {'decision': request['decision']}), row)
                        elif stage == 'reserve':
                            allocation = self._allocation_view(self._allocation('reserve', {
                                'operation_id': operation_id, 'task_id': self.task_id,
                                'task_epoch': row['task_epoch'], 'plan': request['plan'],
                                'lease_seconds': request['lease_seconds']}), row)
                        else:
                            routing = self._route_view(self._routing('link', {'decision_id': decision_id,
                                'kind': 'managed_allocation', 'reference_id': operation_id}), row, True)
                        self._transition(operation_id, stage, 'attempted', 'confirmed')
                        continue
                # Includes durable intent from a crashed or concurrent caller.
                if stage == 'reserve':
                    allocation = self._allocation_view(self._allocation('inspect', {'operation_id': operation_id}), row)
                else:
                    routing = self._route_view(self._routing('inspect', {'decision_id': decision_id}), row, stage == 'link')
                self._transition(operation_id, stage, 'attempted', 'confirmed')
            except Exception:
                return self._report(self._row(operation_id), 'unresolved', allocation, routing,
                                    stage + '_ack_or_readback_unknown')
        # Reserve ACK is not the latest provider outcome; status reads original ID.
        return self.status(operation_id)

    @_guard
    def status(self, operation_id):
        """Read original receipts only; allowed after task lease/epoch loss.

        Allocation inspect remains principal-bound without a live task lease.
        Routing inspect is task-fenced; its failure never stops the independent
        original allocation read or causes mutation/replay.
        """
        row = self._row(operation_id)
        try:
            self._hello()
        except Exception:
            return self._report(row, 'unresolved', diagnostic='authority_unavailable_or_changed')
        routing, allocation, diagnostic = None, None, None
        try:
            routing = self._route_view(self._routing('inspect', {'decision_id': row['decision_id']}), row,
                                       row['link'] == 'confirmed')
        except Exception:
            diagnostic = 'original_routing_readback_unavailable'
        try:
            allocation = self._allocation_view(self._allocation('inspect', {'operation_id': operation_id}), row)
        except Exception:
            return self._report(row, 'unresolved', routing=routing,
                                diagnostic='original_allocation_readback_unknown')
        return self._report(row, 'observed', allocation, routing, diagnostic)

    @_guard
    def result(self, operation_id):
        """Return provider-reported settlement references, not verified bodies.

        Artifact access is a separate owner-installed capability. This method
        never follows a private path, fetches a URL, runs a callback or settles.
        """
        report = self.status(operation_id)
        allocation = report['allocation']
        report['results'] = []
        if allocation:
            for dispatch in allocation['dispatch']:
                if isinstance(dispatch, dict) and dispatch.get('settlement') is not None:
                    report['results'].append({'capability_id': dispatch.get('capability_id'),
                        'receipt_id': dispatch.get('receipt_id'), 'state': dispatch.get('state'),
                        'settlement': dispatch['settlement'],
                        'settlement_sha256': _hash(dispatch['settlement']),
                        'verification': 'provider_reported'})
        return report
