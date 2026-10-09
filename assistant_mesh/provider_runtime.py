"""Optional persistent runtime for privately owner-installed Mesh adapters.

This registry is additional managed Mesh functionality, NOT a native Shell,
network, filesystem or MCP allowlist. The constructor accepts an owner config
file and an already credential-bound client, never executable model payloads.
Every module's exact bytes and the entire manifest are validated before journal
initialization. Python compilation does not execute the module; module top-level
code/imports and its entry function execute only inside an admitted invocation.

Owner-installed Python is trusted, not sandboxed. Dependencies are not a closed
or hash-pinned supply chain, costs/CPU limits are an owner callback contract, and
same-UID agents already have native OS access. Callbacks must bound themselves
and genuinely prove their own quiescence. Registry checks and local execution
are not a distributed atomic transaction. Updating config requires a new
runtime; changing module version/SHA requires a matching actual capability epoch
advance in the authority registry. Existing unknown invocations never replay.
Only locally recorded genuine results receive automatic SAME-ID reconciliation.
"""
import ast
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
from pathlib import Path

from .provider import Adapter, AdapterContractMismatch, ManagedProvider, _checked_path
from .resources import _epoch, _json, _name


class ProviderRuntimeError(ValueError):
    """Fixed categories only: no paths, source, credentials or exception text."""


class _AuthorityFault(Exception):
    def __init__(self, category, recoverable):
        self.category = category
        self.recoverable = recoverable
        Exception.__init__(self, category)


class _ObservedClient:
    def __init__(self, client):
        if not callable(getattr(client, 'request', None)):
            raise ProviderRuntimeError('provider_client_required')
        self.client = client
        self.faults = []

    def request(self, path, body=None):
        try:
            return self.client.request(path, body)
        except urllib.error.HTTPError as error:
            try:
                if error.fp is not None:
                    error.close()
            except Exception:
                pass  # cleanup failure never makes this rejected RPC healthy
            if error.code in (429, 500, 502, 503, 504):
                fault = _AuthorityFault('authority_unavailable', True)
            else:
                fault = _AuthorityFault('authority_rejected', False)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            fault = _AuthorityFault('authority_unavailable', True)
        except Exception:
            fault = _AuthorityFault('authority_response_invalid', False)
        self.faults.append(fault)
        raise fault from None


def _fingerprint(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode,
            metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _owner_bytes(path, maximum, category):
    try:
        path = Path(_checked_path(path))
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as source:
            before = os.fstat(source.fileno())
            if _fingerprint(before) != _fingerprint(path.lstat()):
                raise ValueError('owner_file_changed')
            data = source.read(maximum + 1)
            _checked_path(path)
            if (len(data) > maximum or _fingerprint(before) != _fingerprint(os.fstat(source.fileno()))
                    or _fingerprint(before) != _fingerprint(path.lstat())):
                raise ValueError('owner_file_changed_or_too_large')
            return data
    except Exception:
        raise ProviderRuntimeError(category) from None


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate_owner_key')
        result[key] = value
    return result


def _positive_integer(value, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError('invalid_owner_integer')
    return value


def _manifest(path):
    data = _owner_bytes(path, 65536, 'owner_config_unavailable')
    try:
        value = json.loads(data.decode('utf8'), object_pairs_hook=_unique_object)
        required = {'schema', 'provider', 'authority_id', 'journal', 'adapters'}
        if (not isinstance(value, dict) or not required <= set(value)
                or set(value) - required - {'poll_interval_seconds', 'dispatch_limit', 'reconcile_limit'}
                or type(value['schema']) is not int or value['schema'] != 1
                or not isinstance(value['adapters'], list) or len(value['adapters']) > 256):
            raise ValueError('invalid_owner_manifest')
        value['provider'] = _name(value['provider'], 'provider', 256)
        value['authority_id'] = _name(value['authority_id'], 'authority', 256)
        journal = Path(value['journal'])
        if not journal.is_absolute() or '..' in journal.parts:
            raise ValueError('invalid_owner_journal')
        value['journal'] = str(journal)
        interval = value.get('poll_interval_seconds', 2)
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or not 0 < interval <= 60):
            raise ValueError('invalid_owner_poll_interval')
        value['poll_interval_seconds'] = interval
        value['dispatch_limit'] = _positive_integer(value.get('dispatch_limit', 100), 1000)
        value['reconcile_limit'] = _positive_integer(value.get('reconcile_limit', 100), 1000)
        identities = set()
        for entry in value['adapters']:
            keys = {'capability_id', 'handle', 'version', 'module', 'sha256',
                    'function', 'capability_epoch', 'action', 'new_spend_minor'}
            if not isinstance(entry, dict) or set(entry) != keys:
                raise ValueError('invalid_owner_adapter')
            entry['capability_id'] = _name(entry['capability_id'], 'capability', 200)
            if entry['capability_id'] in identities:
                raise ValueError('duplicate_owner_adapter')
            identities.add(entry['capability_id'])
            entry['handle'] = _name(entry['handle'], 'handle', 128)
            entry['module'] = str(Path(entry['module']))
            if not isinstance(entry['version'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,31}', entry['version']):
                raise ValueError('invalid_owner_version')
            if not isinstance(entry['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
                raise ValueError('invalid_owner_sha')
            if not isinstance(entry['function'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', entry['function']):
                raise ValueError('invalid_owner_function')
            entry['capability_epoch'] = _epoch(entry['capability_epoch'])
            entry['action'] = _name(entry['action'], 'action')
            if type(entry['new_spend_minor']) is not int or entry['new_spend_minor'] != 0:
                raise ValueError('new_spend_not_authorized')
        return value
    except Exception:
        raise ProviderRuntimeError('owner_config_invalid') from None


def installed_binding(entry):
    """Validate a manifest entry's private bytes without executing/importing it.

    Callers first validate the complete manifest with ``_manifest``. This pure
    installation check is shared by the optional local lifecycle controller;
    it neither initializes a journal nor contacts an authority.
    """
    source = _owner_bytes(entry['module'], 1024 * 1024, 'owner_module_unavailable')
    if hashlib.sha256(source).hexdigest() != entry['sha256']:
        raise ProviderRuntimeError('owner_module_integrity_failed')
    try:
        tree = ast.parse(source.decode('utf8'), filename=entry['module'])
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == entry['function']]
        if len(functions) != 1:
            raise ValueError('owner_entry_function_required')
        compiled = compile(tree, entry['module'], 'exec')
    except Exception:
        raise ProviderRuntimeError('owner_module_compile_failed') from None
    descriptor = {key: entry[key] for key in ('handle', 'version', 'sha256', 'function', 'action', 'new_spend_minor')}
    descriptor['module_path_sha256'] = hashlib.sha256(entry['module'].encode('utf8')).hexdigest()
    provenance = hashlib.sha256(_json(descriptor, 'owner_binding').encode('utf8')).hexdigest()
    handle = entry['handle'] + '@' + entry['version'] + '#binding-sha256:' + provenance
    return compiled, descriptor, handle


class ProviderRuntime:
    """Programmatic poll/serve API; no service, listener or tool auto-install."""
    def __init__(self, client, owner_config_path):
        self.config = _manifest(owner_config_path)
        self.client = _ObservedClient(client)
        self.binding_faults = []
        self._poll_lock = threading.Lock()
        bindings, descriptions = {}, []
        for entry in self.config['adapters']:
            compiled, descriptor, handle = installed_binding(entry)
            bindings[entry['capability_id']] = Adapter(handle, entry['capability_epoch'], entry['action'],
                self._callback(dict(entry), descriptor, compiled))
            descriptions.append({'capability_id': entry['capability_id'], 'capability_epoch': entry['capability_epoch'],
                                 'adapter_handle': handle, 'managed_adapter': descriptor})
        # Nothing above executes any source, even if later entries are invalid.
        # The journal's existing schema/provider/authority is validated here,
        # before any module can execute at a subsequent admitted invocation.
        try:
            self.bridge = ManagedProvider(self.client, self.config['journal'], self.config['provider'],
                                          bindings, self.config['authority_id'])
        except Exception:
            raise ProviderRuntimeError('provider_journal_invalid') from None
        self._descriptions = descriptions

    def describe_bindings(self):
        """Metadata for explicit owner advertisement; never auto-advertises."""
        return json.loads(json.dumps(self._descriptions))

    def _callback(self, entry, descriptor, compiled):
        def invoke(context):
            identity = urllib.parse.quote(entry['capability_id'], safe='')
            value = self.client.request('/v1/resource?id=' + identity + '&include_unavailable=1')
            if (not isinstance(value, dict) or value.get('id') != entry['capability_id']
                    or value.get('principal') != self.config['provider'] or value.get('available') is not True
                    or type(value.get('epoch')) is not int or value['epoch'] != entry['capability_epoch']
                    or not isinstance(value.get('spec'), dict)
                    or _json(value['spec'].get('managed_adapter'), 'owner_binding') != _json(descriptor, 'owner_binding')):
                self.binding_faults.append('owner_advertisement_mismatch')
                raise ProviderRuntimeError('owner_advertisement_mismatch')
            namespace = {'__name__': '_mesh_owner_' + entry['sha256'], '__file__': entry['module'], '__package__': None}
            # Owner imports/top-level effects are execution, not installation
            # checks; any failure remains a durable unknown, never replayed.
            exec(compiled, namespace)
            function = namespace.get(entry['function'])
            if not callable(function):
                self.binding_faults.append('owner_entry_invalid')
                raise ProviderRuntimeError('owner_entry_invalid')
            return function(dict(context, owner_binding=dict(descriptor)))
        return invoke

    def _local(self):
        try:
            with self.bridge._db() as db:
                summary = dict(db.execute('''SELECT
                    COUNT(CASE WHEN state != 'settled' THEN 1 END) AS unsettled,
                    COUNT(CASE WHEN state != 'settled' AND result IS NULL THEN 1 END) AS unresolved,
                    COUNT(CASE WHEN error = 'adapter_outcome_unknown' THEN 1 END) AS adapter_unknown,
                    COUNT(CASE WHEN state != 'settled' AND result IS NOT NULL THEN 1 END) AS recorded_pending
                    FROM provider_executions''').fetchone())
                recorded = [dict(row) for row in db.execute('''SELECT operation_id,capability_id
                    FROM provider_executions WHERE result IS NOT NULL
                    AND state IN ('result_ready','settlement_intent','settlement_unknown')
                    ORDER BY updated,operation_id,capability_id LIMIT ?''', (self.config['reconcile_limit'],))]
        except Exception:
            raise ProviderRuntimeError('provider_journal_failed') from None
        return summary, recorded

    def _pending(self, stop):
        if stop is not None and stop.is_set():
            return []
        limit = self.config['dispatch_limit']
        value = self.bridge._rpc('pending', {'limit': limit})
        if not isinstance(value, dict) or not isinstance(value.get('dispatch'), list) or len(value['dispatch']) > limit:
            raise ValueError('provider_pending_response_invalid')
        dispatch = []
        for item in value['dispatch']:
            if stop is not None and stop.is_set():
                break
            try:
                dispatch.append(self.bridge.run(item))
            except AdapterContractMismatch:
                # Only a valid unaccepted selection for a different installed
                # epoch/action is isolated. Never hide malformed identities,
                # invalid epochs or journal/content conflicts. Keep the old ID
                # and authority hold for explicit owner decline or expiry.
                dispatch.append({'operation_id': item['operation_id'], 'capability_id': item['capability_id'],
                                 'state': 'not_executed', 'error': 'owner_adapter_contract_mismatch',
                                 'local_invocation_started': False, 'local_invocation_intent_recorded': False,
                                 'automatic_replay': False, 'retry_with_new_id': False,
                                 'execution_verified': False, 'native_tools_intercepted': False})
        return dispatch

    def poll_once(self, stop=None):
        """Pending read plus at-most-once bridge; never re-invokes old rows."""
        if not self._poll_lock.acquire(False):
            raise ProviderRuntimeError('provider_poll_already_active')
        try:
            self.client.faults = []
            self.binding_faults = []
            _, recorded = self._local()
            dispatch, reconciled = [], []
            try:
                dispatch = self._pending(stop)
            except _AuthorityFault:
                pass  # only an RPC-boundary fault, not a journal/callback error
            except Exception:
                raise ProviderRuntimeError('provider_dispatch_failed') from None
            # Snapshot candidates before run_pending: no immediate retry of a
            # newly lost settlement response. Re-report only already durable
            # genuine results under the original IDs, never invoke adapters.
            for row in recorded:
                if stop is not None and stop.is_set():
                    break
                try:
                    reconciled.append(self.bridge.reconcile(row['operation_id'], row['capability_id']))
                except Exception:
                    raise ProviderRuntimeError('provider_reconciliation_failed') from None
            summary, _ = self._local()
            categories = {fault.category for fault in self.client.faults} | set(self.binding_faults)
            if summary['unresolved']:
                categories.add('local_outcome_unresolved')
            if summary['adapter_unknown']:
                categories.add('adapter_outcome_unknown')
            if any(item.get('error') == 'owner_adapter_missing' for item in dispatch):
                categories.add('owner_adapter_missing')
            if any(item.get('error') == 'owner_adapter_contract_mismatch' for item in dispatch):
                categories.add('owner_adapter_contract_mismatch')
            if summary['recorded_pending']:
                categories.add('recorded_settlement_pending')
            return {'status': 'degraded' if categories else 'ok', 'diagnostics': sorted(categories),
                    'recoverable': not self.binding_faults and all(fault.recoverable for fault in self.client.faults),
                    'dispatch': dispatch, 'reconciled': reconciled,
                    'local_unsettled': summary['unsettled'],
                    'automatic_invocation_replay': False, 'retry_with_new_id': False,
                    'execution_verified': False, 'native_tools_intercepted': False}
        finally:
            self._poll_lock.release()

    def serve(self, stop=None, on_report=None, max_cycles=None):
        """Best-effort polling, interruptible wait; fatal local faults propagate."""
        if max_cycles is not None:
            try:
                _positive_integer(max_cycles, 1000000)
            except ValueError:
                raise ProviderRuntimeError('invalid_provider_cycle_limit') from None
        stop = threading.Event() if stop is None else stop
        cycles, report = 0, None
        while not stop.is_set():
            report = self.poll_once(stop=stop)
            cycles += 1
            if on_report is not None:
                on_report(report)
            if not report['recoverable'] or (max_cycles is not None and cycles >= max_cycles):
                break
            stop.wait(self.config['poll_interval_seconds'])
        return {'cycles': cycles, 'last_report': report, 'automatic_invocation_replay': False,
                'native_tools_intercepted': False}
