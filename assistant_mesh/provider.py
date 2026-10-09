"""One-shot bridge from managed Mesh admission to owner-installed callbacks.

This is not a Shell/MCP/OS allowlist. Native agent functionality is untouched.
Owner bindings are executable Python callables with fixed versioned handles,
capability epochs and actions; a plan cannot install or replace them. Callbacks
receive the admitted scope/workload, not executable source from a prompt.

The private local journal is part of the execution contract: preserve it across
restarts. An invocation intent or unknown outcome is never automatically replayed,
even if a lease expires or a process disappears. ``reconcile`` only reads the
same authority operation and reports already recorded actual adapter outcomes;
it never calls an adapter. Callbacks must implement their own bounded execution
and provide genuine quiescence/result references. Exception/timeout/PID absence
alone cannot settle a reservation. Reports are not independent verification.

Path checks protect an owner-controlled directory, not against malicious
concurrent processes with the same UID (which already have native OS access).
"""
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .resources import _epoch, _json, _name, _safe_metadata


class Adapter:
    """Owner-created binding. No imports, commands or handles are plan-resolved."""
    def __init__(self, handle, capability_epoch, action, invoke, new_spend_minor=0):
        self.handle = _name(handle, 'adapter_handle', 256)
        self.capability_epoch = _epoch(capability_epoch)
        self.action = _name(action, 'action')
        if not callable(invoke):
            raise ValueError('adapter_callable_required')
        if isinstance(new_spend_minor, bool) or not isinstance(new_spend_minor, int) or new_spend_minor != 0:
            raise PermissionError('new_spend_not_authorized')
        self.invoke = invoke


def _private_file(path):
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
        raise ValueError('provider_journal_requires_owned_regular_0600_file')


def _checked_path(path, create=False):
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', path.name)):
        raise ValueError('provider_journal_absolute_filename_required')
    for directory in (path.parent,) + tuple(path.parent.parents):
        metadata = directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in (0, os.getuid())
                or (metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX)):
            raise ValueError('provider_journal_ancestor_unsafe')
    parent = path.parent.lstat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError('provider_journal_requires_owned_0700_parent')
    if create:
        try:
            descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    _private_file(path)
    for suffix in ('-wal', '-shm', '-journal'):
        auxiliary = Path(str(path) + suffix)
        try:
            _private_file(auxiliary)
        except FileNotFoundError:
            pass
    return str(path)


class ManagedProvider:
    """Connect a credential-bound Client to a separate durable execution journal.

    ``provider`` and ``bindings`` are trusted host/owner configuration, never
    model arguments. The authority checks the Client credential at every RPC;
    returned provider/operation/capability/receipt identities are also checked.
    The same journal cannot silently change provider or authority namespace.
    """
    _COLUMNS = {'operation_id', 'capability_id', 'receipt_id', 'fingerprint',
                'context', 'state', 'authority_epoch', 'invocation_nonce',
                'result', 'error', 'created', 'updated'}

    def __init__(self, client, journal, provider, bindings, authority_id, clock=None):
        self.client = client
        self.provider = _name(provider, 'provider', 256)
        self.authority_id = _name(authority_id, 'authority_id', 256)
        if not isinstance(bindings, dict):
            raise ValueError('owner_adapter_bindings_required')
        self.bindings = {}
        for identity, adapter in bindings.items():
            _name(identity, 'capability_id')
            if not isinstance(adapter, Adapter):
                raise ValueError('owner_adapter_binding_required')
            # Snapshot owner authority; later mutation of the supplied object
            # cannot change the binding during an admitted invocation.
            self.bindings[identity] = Adapter(adapter.handle, adapter.capability_epoch,
                                             adapter.action, adapter.invoke)
        self.clock = clock or time.time
        self.path = _checked_path(journal, create=True)
        with self._db(initialize=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables and tables != {'provider_metadata', 'provider_executions'}:
                raise ValueError('provider_journal_schema_mismatch')
            if not tables:
                db.execute('CREATE TABLE provider_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.execute('''CREATE TABLE provider_executions(
                    operation_id TEXT NOT NULL, capability_id TEXT NOT NULL,
                    receipt_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                    context TEXT NOT NULL, state TEXT NOT NULL, authority_epoch INTEGER,
                    invocation_nonce TEXT, result TEXT, error TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(operation_id,capability_id))''')
                db.executemany('INSERT INTO provider_metadata VALUES(?,?)',
                               [('schema', '1'), ('provider', self.provider), ('authority', self.authority_id)])
            metadata = dict(db.execute('SELECT key,value FROM provider_metadata'))
            if {row[1] for row in db.execute('PRAGMA table_info(provider_metadata)')} != {'key', 'value'}:
                raise ValueError('provider_journal_schema_mismatch')
            if metadata != {'schema': '1', 'provider': self.provider, 'authority': self.authority_id}:
                raise ValueError('provider_journal_identity_or_schema_mismatch')
            if {row[1] for row in db.execute('PRAGMA table_info(provider_executions)')} != self._COLUMNS:
                raise ValueError('provider_journal_schema_mismatch')
        # Do not alter an unrelated database's journaling mode before proving its
        # identity/schema. New schema creation above is one atomic transaction.
        with self._db():
            pass

    @contextmanager
    def _db(self, initialize=False):
        _checked_path(self.path)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            if not initialize:
                # Namespace/schema validation above must happen before any WAL
                # conversion. Concurrent validated constructors can then race
                # on that conversion, where SQLite may skip its busy handler.
                # Own one five-second setup budget rather than stacking the
                # connection's five-second busy wait on every retry. Retry
                # ONLY this setup statement, never BEGIN or a caller's writes.
                db.execute('PRAGMA busy_timeout=0')
                expires = time.monotonic() + 5
                while True:
                    try:
                        db.execute('PRAGMA journal_mode=WAL')
                        break
                    except sqlite3.OperationalError as error:
                        remaining = expires - time.monotonic()
                        if str(error) not in ('database is locked', 'database is busy') or remaining <= 0:
                            raise
                        time.sleep(min(0.05, remaining))
                db.execute('PRAGMA busy_timeout=5000')
            db.execute('PRAGMA synchronous=FULL')
            _checked_path(self.path)
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def _rpc(self, action, arguments):
        return self.client.request('/v1/allocation/action', {'action': action, 'arguments': arguments})

    @contextmanager
    def _operation_lock(self, operation_id, capability_id):
        """Only serialize the same dispatch, not all capability execution."""
        _checked_path(self.path)
        digest = hashlib.sha256(_json([operation_id, capability_id], 'lock_identity').encode('utf8')).hexdigest()
        path = Path(self.path + '.lock-' + digest)
        try:
            _private_file(path)
        except FileNotFoundError:
            pass
        descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            _private_file(path)
            opened = os.fstat(descriptor)
            named = path.lstat()
            if opened.st_ino != named.st_ino or opened.st_dev != named.st_dev:
                raise ValueError('provider_lock_identity_changed')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @staticmethod
    def _row(db, operation_id, capability_id):
        row = db.execute('SELECT * FROM provider_executions WHERE operation_id=? AND capability_id=?',
                         (operation_id, capability_id)).fetchone()
        if not row:
            raise ValueError('provider_execution_not_found')
        return row

    def _transition(self, identity, states, **changes):
        if set(changes) - {'state', 'authority_epoch', 'invocation_nonce', 'result', 'error'}:
            raise ValueError('invalid_provider_transition')
        with self._db() as db:
            row = self._row(db, *identity)
            if row['state'] not in states:
                raise ValueError('provider_execution_transition_conflict')
            changes['updated'] = self.clock()
            keys = sorted(changes)
            db.execute('UPDATE provider_executions SET ' + ','.join(key + '=?' for key in keys)
                       + ' WHERE operation_id=? AND capability_id=?',
                       [changes[key] for key in keys] + list(identity))
            return dict(self._row(db, *identity))

    def _view(self, row):
        actual = json.loads(row['result']) if row['result'] else None
        return {'operation_id': row['operation_id'], 'capability_id': row['capability_id'],
                'provider': self.provider, 'receipt_id': row['receipt_id'], 'state': row['state'],
                'error': row['error'],
                'local_invocation_intent_recorded': row['invocation_nonce'] is not None,
                # A durable intent/active marker precedes the Python call. It
                # cannot prove that callback entry happened before a crash.
                'local_invocation_started': True if actual else (None if row['invocation_nonce'] else False),
                'local_adapter_result_recorded': actual is not None,
                'result_reference': actual['result_reference'] if actual else None,
                'authority_settlement_confirmed': row['state'] == 'settled',
                'execution_verified': False, 'execution_verification': 'provider_reported' if actual else 'not_verified',
                'resource_quiescence_independently_verified': False,
                'automatic_replay': False, 'retry_with_new_id': False, 'native_tools_intercepted': False}

    def inspect(self, operation_id, capability_id):
        _name(operation_id, 'operation_id')
        _name(capability_id, 'capability_id')
        with self._db() as db:
            return self._view(self._row(db, operation_id, capability_id))

    def _response(self, value, context, states, epochs, receipt=True):
        if (not isinstance(value, dict) or value.get('operation_id') != context['operation_id']
                or value.get('capability_id') != context['capability_id']
                or value.get('provider') != self.provider or value.get('state') not in states
                or isinstance(value.get('epoch'), bool) or not isinstance(value.get('epoch'), int)
                or value['epoch'] not in epochs
                or (receipt and value.get('receipt_id') != context['receipt_id'])):
            raise ValueError('provider_authority_response_invalid')
        return value

    def _authority_context(self, context, pending_epoch):
        """Do not trust a caller-supplied pending row as execution authority."""
        value = self._rpc('inspect', {'operation_id': context['operation_id']})
        if (not isinstance(value, dict) or value.get('operation_id') != context['operation_id']
                or value.get('task_id') != context['task_id']
                or isinstance(value.get('task_epoch'), bool)
                or not isinstance(value.get('task_epoch'), int)
                or value['task_epoch'] != context['task_epoch']
                or not isinstance(value.get('plan'), dict)
                or not isinstance(value['plan'].get('selections'), list)
                or not isinstance(value.get('dispatch'), list)):
            raise ValueError('provider_authority_contract_invalid')
        selections = [item for item in value['plan']['selections'] if isinstance(item, dict)
                      and item.get('capability_id') == context['capability_id']]
        dispatch = [item for item in value['dispatch'] if isinstance(item, dict)
                    and item.get('capability_id') == context['capability_id']]
        if len(selections) != 1 or len(dispatch) != 1:
            raise ValueError('provider_authority_contract_invalid')
        self._response(dispatch[0], context, ('pending',), (pending_epoch,), receipt=False)
        if dispatch[0].get('receipt_id') is not None:
            raise ValueError('provider_authority_contract_invalid')
        selected = selections[0]
        for key, local in (('epoch', context['capability_epoch']), ('action', context['action']),
                           ('scope', context['scope']), ('workload', context['workload'])):
            if _json(selected.get(key), 'authority_contract') != _json(local, 'authority_contract'):
                raise ValueError('provider_authority_contract_mismatch')

    def run(self, pending):
        """Admit and invoke one new dispatch. Existing local rows never replay."""
        if not isinstance(pending, dict):
            raise ValueError('invalid_pending_dispatch')
        operation = _name(pending.get('operation_id'), 'operation_id')
        capability = _name(pending.get('capability_id'), 'capability_id')
        with self._operation_lock(operation, capability) as acquired:
            if not acquired:
                try:
                    return dict(self.inspect(operation, capability), execution_instance_active=True)
                except ValueError as error:
                    if str(error) != 'provider_execution_not_found':
                        raise
                    return {'operation_id': operation, 'capability_id': capability,
                            'state': 'busy', 'execution_instance_active': True,
                            'local_invocation_started': False, 'local_invocation_intent_recorded': False,
                            'execution_verified': False,
                            'automatic_replay': False, 'retry_with_new_id': False,
                            'native_tools_intercepted': False}
            return self._run(pending)

    def _run(self, pending):
        operation = _name(pending.get('operation_id'), 'operation_id')
        capability = _name(pending.get('capability_id'), 'capability_id')
        identity = (operation, capability)
        epoch = _epoch(pending.get('epoch'))
        if pending.get('provider') != self.provider or pending.get('state') != 'pending' or pending.get('receipt_id') is not None:
            raise PermissionError('provider_pending_identity_mismatch')
        adapter = self.bindings.get(capability)
        if adapter is None:
            with self._db() as db:
                old = db.execute('SELECT * FROM provider_executions WHERE operation_id=? AND capability_id=?', identity).fetchone()
                if old:
                    # Removing an installed adapter is not evidence that a
                    # previously admitted/started operation never executed.
                    return self._view(old)
            return {'operation_id': operation, 'capability_id': capability, 'state': 'not_executed',
                    'error': 'owner_adapter_missing', 'local_invocation_started': False,
                    'local_invocation_intent_recorded': False,
                    'execution_verified': False, 'automatic_replay': False,
                    'retry_with_new_id': False, 'native_tools_intercepted': False}
        selection = pending.get('selection')
        if (not isinstance(selection, dict) or selection.get('capability_id') != capability
                or _epoch(selection.get('epoch')) != adapter.capability_epoch
                or selection.get('action') != adapter.action):
            raise PermissionError('owner_adapter_contract_mismatch')
        context = {'operation_id': operation, 'capability_id': capability, 'provider': self.provider,
                   'receipt_id': 'provider-' + hashlib.sha256(_json([self.authority_id, self.provider, operation, capability], 'receipt').encode('utf8')).hexdigest(),
                   'adapter_handle': adapter.handle, 'capability_epoch': adapter.capability_epoch,
                   'action': adapter.action, 'scope': selection.get('scope'), 'workload': selection.get('workload'),
                   'task_id': _name(pending.get('task_id'), 'task_id'), 'task_epoch': _epoch(pending.get('task_epoch'))}
        encoded = _json(context, 'provider_context', 65536)
        context = json.loads(encoded)
        fingerprint = hashlib.sha256(encoded.encode('utf8')).hexdigest()
        with self._db() as db:
            old = db.execute('SELECT * FROM provider_executions WHERE operation_id=? AND capability_id=?', identity).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise ValueError('provider_execution_content_conflict')
                return self._view(old)
            now = self.clock()
            db.execute("INSERT INTO provider_executions VALUES(?,?,?,?,?,'prepared',NULL,NULL,NULL,NULL,?,?)",
                       (operation, capability, context['receipt_id'], fingerprint, encoded, now, now))
        try:
            self._authority_context(context, epoch)
        except Exception:
            return self._view(self._transition(identity, ('prepared',), state='unknown',
                                               error='authority_contract_unknown'))
        row = self._transition(identity, ('prepared',), state='accept_intent')
        try:
            accepted = self._response(self._rpc('accept', dict(operation_id=operation, capability_id=capability,
                                         epoch=epoch, receipt_id=context['receipt_id'])), context, ('accepted',), (epoch + 1,))
            if not isinstance(accepted.get('accepted_new'), bool):
                raise ValueError('provider_authority_response_invalid')
            row = self._transition(identity, ('accept_intent',), state='accepted', authority_epoch=accepted['epoch'])
            row = self._transition(identity, ('accepted',), state='start_intent')
            started = self._response(self._rpc('start', dict(operation_id=operation, capability_id=capability,
                                         epoch=accepted['epoch'], receipt_id=context['receipt_id'])), context,
                                     ('running',), (accepted['epoch'] + 1,))
            if started.get('execute_once') is not True:
                raise ValueError('provider_start_not_new')
        except Exception:
            row = self._transition(identity, (row['state'],), state='unknown', error='authority_admission_unknown')
            return self._view(row)
        nonce = uuid.uuid4().hex
        row = self._transition(identity, ('start_intent',), state='invocation_intent',
                               authority_epoch=started['epoch'], invocation_nonce=nonce)
        # This intent is durable before any callback. No restart path calls it.
        row = self._transition(identity, ('invocation_intent',), state='active')
        try:
            result = self._adapter_result(adapter.invoke(dict(context, invocation_nonce=nonce)))
        except Exception:
            row = self._transition(identity, ('active',), state='unknown', error='adapter_outcome_unknown')
            return self._view(row)
        row = self._transition(identity, ('active',), state='result_ready', result=_json(result, 'adapter_result', 8192))
        return self._settle(row, started['epoch'])

    @staticmethod
    def _adapter_result(value):
        if (not isinstance(value, dict) or value.get('outcome') not in ('completed', 'stopped')
                or value.get('resource_quiescent') is not True
                or not isinstance(value.get('result_reference'), str) or not value['result_reference'].strip()
                or len(value['result_reference']) > 2048 or not isinstance(value.get('evidence', {}), dict)):
            raise ValueError('adapter_quiescent_result_required')
        result = {'outcome': value['outcome'], 'resource_quiescent': True,
                  'result_reference': value['result_reference'], 'evidence': value.get('evidence', {})}
        safe, redacted = _safe_metadata(result)
        if redacted:
            raise ValueError('private_adapter_result_not_allowed')
        return json.loads(_json(safe, 'adapter_result', 8192))

    def _settle(self, row, epoch):
        context, actual = json.loads(row['context']), json.loads(row['result'])
        identity = (row['operation_id'], row['capability_id'])
        evidence = dict(actual['evidence'])
        evidence.update(resource_quiescent=True, result_reference=actual['result_reference'],
                        adapter_handle=context['adapter_handle'], invocation_nonce=row['invocation_nonce'])
        row = self._transition(identity, ('result_ready', 'settlement_unknown'), state='settlement_intent')
        try:
            value = self._response(self._rpc('settle', dict(operation_id=row['operation_id'],
                                   capability_id=row['capability_id'], epoch=epoch, receipt_id=row['receipt_id'],
                                   outcome=actual['outcome'], evidence=evidence)), context,
                                   (actual['outcome'],), (epoch, epoch + 1))
            if value.get('settlement') != {'outcome': actual['outcome'], 'evidence': evidence}:
                raise ValueError('provider_settlement_response_invalid')
        except Exception:
            row = self._transition(identity, ('settlement_intent',), state='settlement_unknown',
                                   error='authority_settlement_unknown')
        else:
            row = self._transition(identity, ('settlement_intent',), state='settled', authority_epoch=value['epoch'], error=None)
        return self._view(row)

    def reconcile(self, operation_id, capability_id):
        """Explicit same-ID inspection/outcome reporting; never invokes adapters."""
        _name(operation_id, 'operation_id')
        _name(capability_id, 'capability_id')
        with self._operation_lock(operation_id, capability_id) as acquired:
            if not acquired:
                return dict(self.inspect(operation_id, capability_id),
                            reconciliation='execution_instance_active')
            return self._reconcile(operation_id, capability_id)

    def _reconcile(self, operation_id, capability_id):
        with self._db() as db:
            row = dict(self._row(db, _name(operation_id, 'operation_id'), _name(capability_id, 'capability_id')))
        context = json.loads(row['context'])
        if row['state'] == 'settled':
            return self._view(row)
        try:
            value = self._rpc('inspect', {'operation_id': operation_id})
            if not isinstance(value, dict) or value.get('operation_id') != operation_id or not isinstance(value.get('dispatch'), list):
                raise ValueError('provider_authority_response_invalid')
            matches = [item for item in value['dispatch'] if isinstance(item, dict) and item.get('capability_id') == capability_id]
            if len(matches) != 1:
                raise ValueError('provider_authority_response_invalid')
            remote = matches[0]
            epoch = _epoch(remote.get('epoch'))
            self._response(remote, context, ('accepted', 'running', 'unknown', 'completed', 'stopped'), (epoch,))
        except Exception:
            return dict(self._view(row), reconciliation='authority_inspection_unknown')
        if row['result']:
            if row['state'] == 'settlement_intent':
                row = self._transition((operation_id, capability_id), ('settlement_intent',), state='settlement_unknown')
            if row['state'] in ('result_ready', 'settlement_unknown'):
                return self._settle(row, epoch)
        elif remote['state'] in ('accepted', 'running'):
            try:
                unknown = self._response(self._rpc('unknown', dict(operation_id=operation_id,
                                         capability_id=capability_id, epoch=epoch)), context, ('unknown',), (epoch + 1,))
            except Exception:
                return dict(self._view(row), reconciliation='authority_unknown_report_uncertain')
            row = self._transition((operation_id, capability_id), (row['state'],), state='unknown',
                                   authority_epoch=unknown['epoch'], error='adapter_outcome_not_proved')
        return dict(self._view(row), reconciliation='inspected_no_replay')

    def run_pending(self, limit=100):
        """Process provider-owned pending rows; no automatic unknown replay."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('invalid_dispatch_limit')
        value = self._rpc('pending', {'limit': limit})
        if not isinstance(value, dict) or not isinstance(value.get('dispatch'), list) or len(value['dispatch']) > limit:
            raise ValueError('provider_pending_response_invalid')
        return {'dispatch': [self.run(item) for item in value['dispatch']],
                'execution_verified': False, 'native_tools_intercepted': False}
