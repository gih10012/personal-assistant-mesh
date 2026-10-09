"""Read-only owner-reference observation of arbitrary installed Mesh tools.

  python -m scripts.observe_installed_tools --contract PRIVATE/contract.json \
      --reference PRIVATE/owner-reference.json --config PRIVATE/client.json

The private contract is an owner-retained collection envelope, not executable
source or authority. It declares the entire closed journal's operation set,
exact installed descriptors, task/Leader epochs, and fetched JSON artifact paths
and raw SHA-256 values. The independently retained reference supplies canonical
JSON artifact SHA-256 values and the independent oracle source SHA, never oracle
code. JSON canonicalization uses sorted keys, compact separators, UTF-8 Unicode,
and no nonfinite values or duplicate object keys.

Observation never constructs a provider/runtime, loads a tool/oracle, creates or
checkpoints SQLite, submits a task, starts a model/service, or replays an effect.
Only --report explicitly publishes a new immutable private report. Authenticated
hello, allocation inspect, and resource reads are the only remote operations.
Native provenance is owner-recorded metadata, not model/host attestation. A newer
registry epoch cannot independently reverify an older advertisement; that is an
explicit superseded verdict, not a binding match. Same-UID native OS access is
not sandboxed. Performance, physical host, and quiescence verification stay false.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from assistant_mesh.resources import _json as resource_json
from assistant_mesh.worker import Client
from scripts.probe_provider_execution import checked, publish
from scripts.probe_worker_shell import ProbeError


CONTRACT = 'installed-tool-observation/1'
REFERENCE = 'installed-tool-owner-reference/1'
REPORT = 'installed-tool-observation-report/1'
MAX_OPERATIONS = 256
MAX_ARTIFACT = 16 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_COLUMNS = {'operation_id', 'capability_id', 'receipt_id', 'fingerprint', 'context',
            'state', 'authority_epoch', 'invocation_nonce', 'result', 'error',
            'created', 'updated'}
_LOCAL_STATES = {'prepared', 'accept_intent', 'accepted', 'start_intent',
                 'invocation_intent', 'active', 'result_ready', 'settlement_intent',
                 'settlement_unknown', 'settled', 'unknown'}
_REMOTE_STATES = {'reserved', 'pending', 'accepted', 'running', 'unknown',
                  'completed', 'stopped', 'cancelled', 'expired'}


class ObservationError(ValueError):
    """Fixed error categories only; no source, paths, bodies or credentials."""


def require(condition, category):
    if not condition:
        raise ObservationError(category)


def _fingerprint(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode,
            metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def private_bytes(value, maximum):
    """Use existing private/Git checks and also validate the opened descriptor."""
    try:
        path = checked(value, file=True)
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as source:
            before = os.fstat(source.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                    and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1
                    and before.st_size <= maximum
                    and _fingerprint(before) == _fingerprint(path.lstat()),
                    'observation_private_file_invalid')
            data = source.read(maximum + 1)
            checked(path, file=True)
            require(len(data) <= maximum and _fingerprint(before) == _fingerprint(os.fstat(source.fileno()))
                    and _fingerprint(before) == _fingerprint(path.lstat()),
                    'observation_private_file_changed')
            return data
    except ObservationError:
        raise
    except Exception:
        raise ObservationError('observation_private_file_unavailable') from None


def _unique(items):
    result = {}
    for key, value in items:
        require(key not in result, 'observation_duplicate_json_key')
        result[key] = value
    return result


def _nonfinite(value):
    raise ObservationError('observation_nonfinite_json')


def strict_json(data):
    try:
        value = json.loads(data.decode('utf8'), object_pairs_hook=_unique, parse_constant=_nonfinite)
        canonical_json(value)  # reject overflowed numbers and Unicode surrogates too
        return value
    except ObservationError:
        raise
    except Exception:
        raise ObservationError('observation_json_invalid') from None


def canonical_json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False).encode('utf8')
    except Exception:
        raise ObservationError('observation_json_invalid') from None


def canonical_sha256(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _digest(value):
    return hashlib.sha256(resource_json(value, 'observation_digest', 65536).encode('utf8')).hexdigest()


def _name(value, maximum=200):
    require(isinstance(value, str) and 0 < len(value) <= maximum
            and all(ord(char) >= 32 and not 0xd800 <= ord(char) <= 0xdfff for char in value),
            'observation_identifier_invalid')
    return value


def _hash(value):
    require(isinstance(value, str) and _HASH.fullmatch(value) is not None,
            'observation_sha256_invalid')
    return value


def _epoch(value, nullable=False):
    if nullable and value is None:
        return None
    require(type(value) is int and value > 0, 'observation_epoch_invalid')
    return value


def _object(value, keys, category):
    require(isinstance(value, dict) and set(value) == set(keys), category)


def _descriptor(value):
    _object(value, {'handle', 'version', 'sha256', 'function', 'action',
                    'new_spend_minor', 'module_path_sha256'}, 'observation_descriptor_invalid')
    _name(value['handle'], 128)
    _name(value['action'])
    require(isinstance(value['version'], str)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,31}', value['version']) is not None
            and isinstance(value['function'], str)
            and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', value['function']) is not None
            and type(value['new_spend_minor']) is int and value['new_spend_minor'] == 0,
            'observation_descriptor_invalid')
    _hash(value['sha256'])
    _hash(value['module_path_sha256'])
    return value['handle'] + '@' + value['version'] + '#binding-sha256:' + _digest(value)


def load_contract(value):
    body = strict_json(private_bytes(value, 4 * 1024 * 1024))
    required = {'schema', 'format', 'run_id', 'authority_id', 'provider', 'journal', 'operations'}
    require(isinstance(body, dict) and required <= set(body)
            and not set(body) - required - {'native_provenance'}
            and type(body['schema']) is int and body['schema'] == 1 and body['format'] == CONTRACT,
            'observation_contract_invalid')
    _name(body['run_id'], 128)
    _name(body['authority_id'], 256)
    require(_name(body['provider'], 256).startswith('node:') and len(body['provider']) > 5,
            'observation_provider_invalid')
    checked(body['journal'], file=True)
    require(isinstance(body['operations'], list) and 0 < len(body['operations']) <= MAX_OPERATIONS,
            'observation_operations_invalid')
    identities = set()
    keys = {'operation_id', 'capability_id', 'capability_epoch', 'task_id', 'task_epoch',
            'leader_epoch', 'requester', 'scope', 'workload', 'adapter_handle',
            'managed_adapter', 'artifact'}
    for operation in body['operations']:
        _object(operation, keys, 'observation_operation_invalid')
        for field in ('operation_id', 'capability_id', 'task_id'):
            _name(operation[field])
        require(_name(operation['requester'], 256).startswith('node:')
                and len(operation['requester']) > 5, 'observation_requester_invalid')
        for field in ('capability_epoch', 'task_epoch'):
            _epoch(operation[field])
        _epoch(operation['leader_epoch'], nullable=True)
        require(operation['scope'] not in (None, '', {}, []) and isinstance(operation['workload'], dict)
                and bool(operation['workload']),
                'observation_scope_invalid')
        resource_json(operation['scope'], 'observation_scope')
        resource_json(operation['workload'], 'observation_workload')
        require(operation['adapter_handle'] == _descriptor(operation['managed_adapter']),
                'observation_binding_handle_mismatch')
        _object(operation['artifact'], {'path', 'sha256'}, 'observation_artifact_invalid')
        _hash(operation['artifact']['sha256'])
        checked(operation['artifact']['path'], file=True)
        identity = (operation['operation_id'], operation['capability_id'])
        require(identity not in identities, 'observation_duplicate_operation')
        identities.add(identity)
    if 'native_provenance' in body:
        rows = body['native_provenance']
        require(isinstance(rows, list) and 0 < len(rows) <= MAX_OPERATIONS,
                'observation_native_provenance_invalid')
        for row in rows:
            _object(row, {'task_id', 'thread_id', 'turn_id', 'source_sha256'},
                    'observation_native_provenance_invalid')
            for field in ('task_id', 'thread_id', 'turn_id'):
                _name(row[field], 256)
            _hash(row['source_sha256'])
            require(any(op['managed_adapter']['sha256'] == row['source_sha256'] for op in body['operations']),
                    'observation_native_source_unbound')
    return body


def load_reference(value, contract):
    body = strict_json(private_bytes(value, 1024 * 1024))
    _object(body, {'schema', 'format', 'run_id', 'oracle_source_sha256', 'operations'},
            'observation_reference_invalid')
    require(type(body['schema']) is int and body['schema'] == 1
            and body['format'] == REFERENCE and body['run_id'] == contract['run_id']
            and isinstance(body['operations'], list), 'observation_reference_invalid')
    _hash(body['oracle_source_sha256'])
    expected = {}
    for row in body['operations']:
        _object(row, {'operation_id', 'capability_id', 'expected_canonical_artifact_sha256'},
                'observation_reference_operation_invalid')
        identity = (_name(row['operation_id']), _name(row['capability_id']))
        require(identity not in expected, 'observation_duplicate_reference')
        expected[identity] = _hash(row['expected_canonical_artifact_sha256'])
    require(set(expected) == {(row['operation_id'], row['capability_id']) for row in contract['operations']},
            'observation_reference_operation_set_mismatch')
    return body, expected


def expected_context(contract, operation):
    identity = [contract['authority_id'], contract['provider'], operation['operation_id'],
                operation['capability_id']]
    return {'operation_id': operation['operation_id'], 'capability_id': operation['capability_id'],
            'provider': contract['provider'], 'receipt_id': 'provider-' + _digest(identity),
            'adapter_handle': operation['adapter_handle'], 'capability_epoch': operation['capability_epoch'],
            'action': operation['managed_adapter']['action'], 'scope': operation['scope'],
            'workload': operation['workload'], 'task_id': operation['task_id'],
            'task_epoch': operation['task_epoch']}


def closed_journal(contract):
    require(sqlite3.sqlite_version_info >= (3, 22, 0), 'observation_sqlite_immutable_reader_required')
    path = checked(contract['journal'], file=True)
    def closed_hash():
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                Path(str(path) + suffix).lstat()
            except FileNotFoundError:
                pass
            else:
                raise ObservationError('observation_closed_journal_required')
        return hashlib.sha256(private_bytes(path, 64 * 1024 * 1024)).hexdigest()
    descriptors = []
    try:
        for operation in contract['operations']:
            lock = Path(str(path) + '.lock-' + _digest([operation['operation_id'], operation['capability_id']]))
            try:
                lock.lstat()
            except FileNotFoundError:
                continue
            checked(lock, file=True)
            descriptor = os.open(str(lock), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            descriptors.append(descriptor)
            require(_fingerprint(os.fstat(descriptor)) == _fingerprint(lock.lstat()),
                    'observation_lock_changed')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ObservationError('observation_execution_active') from None
        before = closed_hash()
        with contextlib.closing(sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro&immutable=1', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            require(tables == {'provider_metadata', 'provider_executions'}, 'observation_journal_schema_invalid')
            require({row[1] for row in db.execute('PRAGMA table_info(provider_metadata)')} == {'key', 'value'}
                    and {row[1] for row in db.execute('PRAGMA table_info(provider_executions)')} == _COLUMNS,
                    'observation_journal_schema_invalid')
            require(dict(db.execute('SELECT key,value FROM provider_metadata')) == {
                'schema': '1', 'provider': contract['provider'], 'authority': contract['authority_id']},
                'observation_journal_namespace_mismatch')
            rows = [dict(row) for row in db.execute('SELECT * FROM provider_executions')]
        require(before == closed_hash(), 'observation_journal_changed')
    except ObservationError:
        raise
    except Exception:
        raise ObservationError('observation_journal_invalid') from None
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    expected = {(row['operation_id'], row['capability_id']): row for row in contract['operations']}
    require(len(rows) == len(expected)
            and {(row['operation_id'], row['capability_id']) for row in rows} == set(expected),
            'observation_journal_operation_set_mismatch')
    result = {}
    for row in rows:
        key = (row['operation_id'], row['capability_id'])
        context = expected_context(contract, expected[key])
        observed_context = strict_json(row['context'].encode('utf8'))
        require(resource_json(observed_context, 'observation_context', 65536) ==
                resource_json(context, 'observation_context', 65536)
                and row['receipt_id'] == context['receipt_id'] and row['fingerprint'] == _digest(observed_context),
                'observation_journal_context_mismatch')
        require(row['state'] in _LOCAL_STATES and (row['authority_epoch'] is None or
                type(row['authority_epoch']) is int and row['authority_epoch'] > 0)
                and (row['invocation_nonce'] is None or isinstance(row['invocation_nonce'], str)
                     and re.fullmatch(r'[0-9a-f]{32}', row['invocation_nonce']) is not None)
                and (row['error'] is None or isinstance(row['error'], str))
                and all(type(row[name]) in (int, float) and math.isfinite(row[name]) for name in ('created', 'updated')),
                'observation_journal_row_invalid')
        row['decoded_result'] = strict_json(row['result'].encode('utf8')) if row['result'] is not None else None
        actual = row['decoded_result']
        if actual is not None:
            _object(actual, {'outcome', 'resource_quiescent', 'result_reference', 'evidence'},
                    'observation_journal_result_invalid')
            require(actual['outcome'] in ('completed', 'stopped') and actual['resource_quiescent'] is True
                    and isinstance(actual['evidence'], dict) and isinstance(actual['result_reference'], str)
                    and row['invocation_nonce'] is not None, 'observation_journal_result_invalid')
        if row['state'] == 'settled':
            require(actual is not None and row['error'] is None and row['authority_epoch'] is not None,
                    'observation_journal_result_invalid')
        result[key] = row
    return result, before


def _remote(contract, operation, value):
    require(isinstance(value, dict) and value.get('operation_id') == operation['operation_id']
            and value.get('task_id') == operation['task_id']
            and type(value.get('task_epoch')) is int and value['task_epoch'] == operation['task_epoch']
            and value.get('actor') == operation['requester'] and value.get('task_node') == operation['requester'][5:]
            and value.get('leader_epoch') == operation['leader_epoch']
            and (value.get('leader_epoch') is None or type(value['leader_epoch']) is int)
            and value.get('state') in _REMOTE_STATES and type(value.get('capacity_held')) is bool
            and isinstance(value.get('plan'), dict) and isinstance(value['plan'].get('selections'), list)
            and isinstance(value.get('dispatch'), list), 'observation_authority_contract_mismatch')
    selected = [row for row in value['plan']['selections'] if isinstance(row, dict)
                and row.get('capability_id') == operation['capability_id']]
    dispatched = [row for row in value['dispatch'] if isinstance(row, dict)
                  and row.get('capability_id') == operation['capability_id']]
    require(len(selected) == 1 and len(dispatched) == 1, 'observation_authority_operation_mismatch')
    wanted = {'epoch': operation['capability_epoch'], 'action': operation['managed_adapter']['action'],
              'scope': operation['scope'], 'workload': operation['workload']}
    require(all(resource_json(selected[0].get(key), 'observation_selection') ==
                resource_json(item, 'observation_selection') for key, item in wanted.items()),
            'observation_authority_selection_mismatch')
    dispatch = dispatched[0]
    require(dispatch.get('operation_id') == operation['operation_id']
            and dispatch.get('provider') == contract['provider'] and dispatch.get('state') in _REMOTE_STATES
            and type(dispatch.get('epoch')) is int and dispatch['epoch'] > 0,
            'observation_authority_dispatch_mismatch')
    receipt = expected_context(contract, operation)['receipt_id']
    require(dispatch.get('receipt_id') == receipt or dispatch.get('receipt_id') is None
            and dispatch['state'] in ('pending', 'cancelled', 'expired'),
            'observation_authority_receipt_mismatch')
    return dispatch


def observe(contract_path, reference_path, client):
    contract = load_contract(contract_path)
    reference, retained = load_reference(reference_path, contract)
    rows, journal_hash = closed_journal(contract)
    hello = client.request('/v1/mesh/hello')
    require(isinstance(hello, dict) and hello.get('protocol') == 'mesh-a2a/1'
            and hello.get('node') == contract['authority_id']
            and hello.get('authority') == contract['authority_id'], 'observation_authority_identity_mismatch')
    allocations, resources, reports = {}, {}, []
    for operation in contract['operations']:
        key = (operation['operation_id'], operation['capability_id'])
        if operation['operation_id'] not in allocations:
            allocations[operation['operation_id']] = client.request('/v1/allocation/action', {
                'action': 'inspect', 'arguments': {'operation_id': operation['operation_id']}})
        value = allocations[operation['operation_id']]
        dispatch = _remote(contract, operation, value)
        if operation['capability_id'] not in resources:
            resources[operation['capability_id']] = client.request('/v1/resource?id=' +
                quote(operation['capability_id'], safe='') + '&include_unavailable=1')
        resource = resources[operation['capability_id']]
        require(isinstance(resource, dict) and resource.get('id') == operation['capability_id']
                and resource.get('principal') == contract['provider'] and type(resource.get('epoch')) is int
                and resource['epoch'] >= operation['capability_epoch'], 'observation_registry_identity_mismatch')
        current = resource['epoch'] == operation['capability_epoch']
        if current:
            require(isinstance(resource.get('spec'), dict) and
                    resource_json(resource['spec'].get('managed_adapter'), 'observation_binding') ==
                    resource_json(operation['managed_adapter'], 'observation_binding'),
                    'observation_registry_binding_mismatch')
        artifact = private_bytes(operation['artifact']['path'], MAX_ARTIFACT)
        raw_hash = hashlib.sha256(artifact).hexdigest()
        require(raw_hash == operation['artifact']['sha256'], 'observation_artifact_raw_sha_mismatch')
        canonical_hash = canonical_sha256(strict_json(artifact))
        owner_match = canonical_hash == retained[key]
        row = rows[key]
        actual = row['decoded_result']
        bound = actual is not None and actual['result_reference'] == 'sha256:' + raw_hash
        if actual is not None:
            require(bound, 'observation_result_artifact_binding_mismatch')
        settlement = None
        if actual is not None:
            evidence = dict(actual['evidence'], resource_quiescent=True, result_reference=actual['result_reference'],
                            adapter_handle=operation['adapter_handle'], invocation_nonce=row['invocation_nonce'])
            settlement = {'outcome': actual['outcome'], 'evidence': evidence}
        settled = (actual is not None and actual['outcome'] == 'completed'
                   and dispatch['state'] == 'completed' and value['state'] == 'completed'
                   and resource_json(dispatch.get('settlement'), 'observation_settlement') ==
                       resource_json(settlement, 'observation_settlement'))
        if row['state'] == 'settled':
            require(row['authority_epoch'] == dispatch['epoch'], 'observation_settlement_epoch_mismatch')
        reports.append({'operation_id': operation['operation_id'], 'capability_id': operation['capability_id'],
            'capability_epoch': operation['capability_epoch'], 'task_epoch': operation['task_epoch'],
            'leader_epoch_at_reservation': operation['leader_epoch'], 'source_sha256': operation['managed_adapter']['sha256'],
            'local_state': row['state'], 'authority_state': value['state'], 'dispatch_state': dispatch['state'],
            'raw_artifact_sha256': raw_hash, 'canonical_artifact_sha256': canonical_hash,
            'actual_artifact_bytes_match_owner_reference': owner_match,
            'recorded_result_binds_actual_artifact': bool(bound),
            'registry_binding_verified': current, 'registry_binding_superseded': not current,
            'historical_advertisement_independently_verified': False,
            'authority_settlement_confirmed': bool(settled),
            'authority_capacity_release_confirmed': bool(settled and value['capacity_held'] is False),
            'local_settlement_recorded': row['state'] == 'settled',
            'execution_verified_by_core': False, 'independent_performance_verified': False,
            'independent_physical_host_verified': False, 'resource_quiescence_independently_verified': False})
    verified = all(row['actual_artifact_bytes_match_owner_reference'] and row['recorded_result_binds_actual_artifact']
                   and row['authority_settlement_confirmed'] and row['authority_capacity_release_confirmed']
                   and row['local_settlement_recorded'] for row in reports)
    return {'format': REPORT, 'run_id': contract['run_id'], 'authority_id': contract['authority_id'],
        'provider': contract['provider'], 'authority_identity_verified': True,
        'canonical_closed_journal_sha256': journal_hash, 'operation_count': len(reports), 'operations': reports,
        'all_declared_results_and_settlements_verified': bool(verified),
        'oracle_source_sha256_recorded': reference['oracle_source_sha256'],
        'oracle_code_executed': False, 'native_provenance_recorded': contract.get('native_provenance', []),
        'native_model_provenance_attested': False, 'independent_performance_verified': False,
        'independent_physical_host_verified': False, 'resource_quiescence_independently_verified': False,
        'automatic_replay': False, 'retry_with_new_id': False, 'native_tools_intercepted': False,
        'models_or_services_started_by_observer': False, 'journal_or_runtime_created': False}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ObservationError('observation_cli_arguments_invalid')


def main(argv=None):
    output = {'format': REPORT, 'automatic_replay': False, 'retry_with_new_id': False,
              'native_tools_intercepted': False, 'completion_not_assumed': True}
    try:
        parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('--contract', required=True)
        parser.add_argument('--reference', required=True)
        parser.add_argument('--config', required=True)
        parser.add_argument('--report')
        args = parser.parse_args(argv)
        config = strict_json(private_bytes(args.config, 65536))
        client = Client(config)
        output = observe(args.contract, args.reference, client)
        if args.report:
            publish(args.report, output)
        code = 0 if output['all_declared_results_and_settlements_verified'] else 2
    except ObservationError as error:
        output['error'] = str(error)
        code = 1
    except ProbeError:
        output['error'] = 'observation_private_path_rejected'
        code = 1
    except Exception:
        output['error'] = 'observation_failed'
        code = 1
    print(json.dumps(output, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
