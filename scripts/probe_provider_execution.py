"""Owner-run isolated managed-provider acceptance, not a service controller.

Workflow (ROOT is an existing owned 0700 /var/tmp or /tmp directory):
  python -m scripts.probe_provider_execution prepare --root ROOT --port 17683
  # Preserve this immutable bundle at the SAME absolute root on both hosts.
  # Owner starts its isolated server.json, without any production/iLink config.
  python -m scripts.probe_provider_execution submit --root ROOT
  # Owner heartbeats/claims the one parent task with requester-client.json.
  python -m scripts.probe_provider_execution reserve --root ROOT \
    --task-epoch ACTUAL --leader-epoch ACTUAL
  # On the provider host, explicitly invoke the fixed read-only SHA adapter:
  python -m scripts.probe_provider_execution execute --root ROOT
  # Copy the closed provider.sqlite to the requester/owner's private root.
  python -m scripts.probe_provider_execution observe --root ROOT

No command starts models, services, SSH or a task claim. Observe is the default
and never constructs ManagedProvider, checkpoints SQLite, writes a journal,
creates state, or replays an operation. --report explicitly publishes a private
report; otherwise only stdout is produced. Reconcile is an explicit same-ID
outcome report, never callback reexecution. Preserve journals after any timeout,
lost response, restart or unknown result; do not prepare a replacement run.

The independent observation is an actual 64-byte workload measurement, NOT
latency, throughput, scheduling quality, or independently measured performance.
Matching a random file's independently retained owner SHA establishes result
correctness, not physical-host identity or general provider trust. The adapter
does not receive the owner reference SHA. Native Shell/MCP/network are untouched.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from assistant_mesh.provider import Adapter, ManagedProvider
from assistant_mesh.resources import _json as resource_json
from assistant_mesh.worker import Client
from scripts.probe_worker_shell import ProbeError, require


FORMAT = 'isolated-provider-execution/1'
REPORT = 'isolated-provider-execution-report/1'
HANDLE = 'owner.probe.file-sha256:v1'
ACTION = 'probe.file.sha256'
SIZE = 64
ROLES = ('operator', 'requester', 'provider', 'verifier')
LEASE = 3600


def checked(value, file=False, missing=False):
    """No symlink/hardlink/Git ancestry; metadata checks, not credential reads."""
    path = Path(value)
    require(path.is_absolute() and '..' not in path.parts, 'provider_probe_absolute_path_required')
    for ancestor in reversed((path,) + tuple(path.parents)):
        try:
            metadata = ancestor.lstat()
        except FileNotFoundError:
            require(missing and ancestor == path, 'provider_probe_path_missing')
            continue
        require(not stat.S_ISLNK(metadata.st_mode), 'provider_probe_symlink_rejected')
        require(metadata.st_uid in (0, os.getuid()), 'provider_probe_foreign_ancestor')
        if ancestor != path or not file:
            require(stat.S_ISDIR(metadata.st_mode), 'provider_probe_directory_required')
            require(not metadata.st_mode & 0o022 or metadata.st_mode & stat.S_ISVTX,
                    'provider_probe_writable_ancestor')
            # lstat also catches a broken .git symlink without modifying it.
            try:
                (ancestor / '.git').lstat()
            except FileNotFoundError:
                pass
            else:
                raise ProbeError('provider_probe_private_path_inside_git')
    if file and path.exists():
        metadata = path.lstat()
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_nlink == 1,
                'provider_probe_owned_regular_0600_single_link_required')
        parent = path.parent.lstat()
        require(parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) == 0o700,
                'provider_probe_owned_0700_parent_required')
    return path


def private_root(value):
    root = checked(value)
    require(any(base in root.parents for base in (Path('/tmp'), Path('/var/tmp'))),
            'provider_probe_private_temporary_root_required')
    metadata = root.lstat()
    require(metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700,
            'provider_probe_existing_owned_0700_root_required')
    return root


def read_json(value):
    path = checked(value, file=True)
    require(path.stat().st_size <= 256 * 1024, 'provider_probe_json_too_large')
    with path.open(encoding='utf8') as source:
        return json.load(source)


def publish(value, body):
    """Exclusive fsynced publication. Never overwrite partial or prior state."""
    path = checked(value, file=True, missing=True)
    require(path.parent.stat().st_uid == os.getuid()
            and stat.S_IMODE(path.parent.stat().st_mode) == 0o700,
            'provider_probe_owned_0700_parent_required')
    data = body if isinstance(body, bytes) else json.dumps(body, sort_keys=True, allow_nan=False).encode('utf8')
    if path.exists():
        require(path.read_bytes() == data, 'provider_probe_existing_file_conflict')
        sync_directory(path.parent)
        return
    with tempfile.TemporaryDirectory(prefix='.provider-publish-', dir=str(path.parent)) as staging:
        draft = Path(staging) / 'complete'
        descriptor = os.open(str(draft), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(str(draft), str(path), follow_symlinks=False)
        except FileExistsError:
            checked(path, file=True)
            require(path.read_bytes() == data, 'provider_probe_existing_file_conflict')
    checked(path, file=True)
    sync_directory(path.parent)


def sync_directory(parent):
    descriptor = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def digest(value):
    # Share the actual authority/provider canonical encoding (including Unicode
    # escaping defaults), instead of copying assumptions about JSON settings.
    return hashlib.sha256(resource_json(value, 'provider_probe_digest', 65536).encode('utf8')).hexdigest()


def capabilities(state):
    return ['leader', 'agent', 'mesh.node:' + state['nodes']['requester']]


def task_body(state):
    return {'id': state['ids']['task'],
            'input': 'Owner-controlled isolated managed-provider read-only file SHA acceptance. '
                     'No model execution is requested; the owner orchestrates this parent task.',
            'required': capabilities(state),
            'context': {'project_id': 'provider-proof:' + state['run_id'],
                        'session_scope': 'leader:provider-proof:' + state['run_id'],
                        'source': 'owner-isolated-provider-execution'}}


def scope(state):
    return {'fixture_path': str(Path(state['root']) / 'input.bin'), 'run_id': state['run_id']}


def plan(state):
    return {'target': state['ids']['capability'], 'route': [], 'selections': [{
        'capability_id': state['ids']['capability'], 'epoch': 1, 'action': ACTION,
        'scope': scope(state), 'workload': {'input_bytes': SIZE},
        'grant_id': state['ids']['grant'], 'observations': [{
            'observation_id': state['ids']['observation'], 'metric': 'input_bytes', 'unit': 'byte',
            'max_age_seconds': LEASE, 'predicate': {'op': 'eq', 'value': SIZE}}]}]}


def client_body(state, role):
    return {'control_url': 'http://127.0.0.1:' + str(state['port']),
            'token_file': str(Path(state['root']) / (role + '.token'))}


def server_body(state):
    root = Path(state['root'])
    return {'node_id': state['authority'], 'database': str(root / 'ledger.sqlite'),
            'port': state['port'], 'resources': {'trusted_verifiers': ['node:' + state['nodes']['verifier']]},
            'peers': [{'role': 'operator', 'token_file': str(root / 'operator.token')}] + [
                {'role': 'worker', 'node': state['nodes'][role],
                 'token_file': str(root / (role + '.token')),
                 'capabilities': capabilities(state) if role == 'requester' else []}
                for role in ROLES if role != 'operator']}


def load_state(value):
    root = private_root(value)
    state = read_json(root / 'state.json')
    require(isinstance(state, dict) and set(state) == {
        'format', 'root', 'run_id', 'authority', 'nodes', 'ids', 'port', 'sample_time'},
        'provider_probe_state_invalid')
    run = state.get('run_id')
    require(state['format'] == FORMAT and state['root'] == str(root)
            and isinstance(run, str) and re.fullmatch('[0-9a-f]{32}', run),
            'provider_probe_state_identity_changed')
    require(state['authority'] == 'provider-proof-authority-' + run
            and state['nodes'] == {role: 'provider-proof-' + role + '-' + run
                                   for role in ROLES if role != 'operator'}
            and state['ids'] == {name: digest([FORMAT, run, name]) for name in (
                'task', 'operation', 'capability', 'pool', 'request', 'grant', 'observation')},
            'provider_probe_bindings_changed')
    require(isinstance(state['port'], int) and not isinstance(state['port'], bool)
            and 1024 <= state['port'] <= 65535, 'provider_probe_port_invalid')
    require(isinstance(state['sample_time'], (int, float)) and not isinstance(state['sample_time'], bool)
            and math.isfinite(state['sample_time']) and state['sample_time'] >= 0,
            'provider_probe_sample_time_invalid')
    require(read_json(root / 'server.json') == server_body(state), 'provider_probe_server_binding_changed')
    return state


def prepare(value, port=17683):
    root = private_root(value)
    if (root / 'state.json').exists():
        state = load_state(root)
        require(state['port'] == port, 'provider_probe_port_changed')
        # Only explicit prepare reads the retained reference to validate retry.
        require(reference(state)['sha256'] == hash_input(state), 'provider_probe_input_reference_changed')
        for role in ROLES:
            require(read_json(root / (role + '-client.json')) == client_body(state, role),
                    'provider_probe_client_binding_changed')
            checked(root / (role + '.token'), file=True)  # metadata, no secret reads
        return state
    require(not list(root.iterdir()), 'provider_probe_prepare_requires_empty_root')
    require(isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535,
            'provider_probe_port_invalid')
    run = secrets.token_hex(16)
    state = {'format': FORMAT, 'root': str(root), 'run_id': run, 'port': port,
             'authority': 'provider-proof-authority-' + run,
             'nodes': {role: 'provider-proof-' + role + '-' + run for role in ROLES if role != 'operator'},
             'ids': {name: digest([FORMAT, run, name]) for name in (
                 'task', 'operation', 'capability', 'pool', 'request', 'grant', 'observation')}}
    data = os.urandom(SIZE)
    publish(root / 'input.bin', data)
    # Record the measurement time after the actual published file was read,
    # not before input creation or a later guessed provider performance test.
    retained = input_data(state)
    state['sample_time'] = time.time()
    publish(root / 'owner-reference.json', {'format': FORMAT, 'run_id': run, 'input_bytes': len(retained),
                                         'sha256': hashlib.sha256(retained).hexdigest()})
    for role in ROLES:
        publish(root / (role + '.token'), secrets.token_hex(32).encode('ascii'))
        publish(root / (role + '-client.json'), client_body(state, role))
    publish(root / 'server.json', server_body(state))
    publish(root / 'state.json', state)  # publish completion marker LAST
    return load_state(root)


def input_data(state):
    path = checked(Path(state['root']) / 'input.bin', file=True)
    require(path.stat().st_size == SIZE, 'provider_probe_input_size_changed')
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        metadata = os.fstat(source.fileno())
        named = path.lstat()
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o600
                and metadata.st_ino == named.st_ino and metadata.st_dev == named.st_dev
                and metadata.st_nlink == 1 and metadata.st_size == SIZE,
                'provider_probe_input_identity_changed')
        data = source.read(SIZE + 1)
    require(len(data) == SIZE, 'provider_probe_input_size_changed')
    return data


def hash_input(state):
    return hashlib.sha256(input_data(state)).hexdigest()


def reference(state):
    value = read_json(Path(state['root']) / 'owner-reference.json')
    require(isinstance(value, dict) and set(value) == {'format', 'run_id', 'input_bytes', 'sha256'}
            and value['format'] == FORMAT and value['run_id'] == state['run_id']
            and type(value['input_bytes']) is int and value['input_bytes'] == SIZE
            and isinstance(value['sha256'], str) and re.fullmatch('[0-9a-f]{64}', value['sha256']),
            'provider_probe_owner_reference_invalid')
    return value


def client_for(state, role):
    require(role in ROLES, 'provider_probe_role_invalid')
    cfg = read_json(Path(state['root']) / (role + '-client.json'))
    require(cfg == client_body(state, role), 'provider_probe_client_binding_changed')
    checked(cfg['token_file'], file=True)
    # Client alone reads the fresh isolated Mesh API token. No native account,
    # auth.json, browser login or existing provider credentials are opened.
    return Client(cfg)


def rpc(client, domain, operation, **arguments):
    return client.request('/v1/' + domain + '/action', {'action': operation, 'arguments': arguments})


def submit(state, clients):
    """Explicit setup only. Partial/unknown setup retains every original ID."""
    measured = len(input_data(state))
    owner, requester, provider, verifier = (clients[role] for role in ROLES)
    ids = state['ids']
    cap = rpc(provider, 'resource', 'advertise', id=ids['capability'], kind='owner.installed.read-only-tool',
              spec={'adapter_handle': HANDLE, 'action': ACTION, 'input_bytes': measured},
              description='Isolated owner SHA callback; no performance claim.', lease_seconds=LEASE)
    require(cap.get('id') == ids['capability'] and cap.get('epoch') == 1
            and cap.get('principal') == 'node:' + state['nodes']['provider'],
            'provider_probe_advertisement_invalid')
    observation = rpc(verifier, 'resource', 'observe', id=ids['capability'], metric='input_bytes',
        value=measured, unit='byte', source='owner retained private random input measured by independent bound checker',
        sample_time=state['sample_time'], epoch=1, observation_id=ids['observation'], verification='verified',
        evidence={'scope': scope(state), 'workload': {'input_bytes': measured},
                  'measurement_method': 'bounded read of owned regular input; not performance'})
    require(observation.get('id') == ids['observation'] and observation.get('verification') == 'verified'
            and observation.get('actor') == 'node:' + state['nodes']['verifier'],
            'provider_probe_observation_invalid')
    rpc(requester, 'resource', 'request_grant', id=ids['capability'], action=ACTION, scope=scope(state),
        request_id=ids['request'], reason='Owner isolated read-only SHA acceptance')
    rpc(owner, 'resource', 'grant', request_id=ids['request'], grant_id=ids['grant'], expires_seconds=LEASE)
    rpc(owner, 'allocation', 'define_pool', id=ids['pool'],
        dimensions={'requests': {'capacity': 1, 'unit': 'request'}}, lease_seconds=LEASE)
    rpc(owner, 'allocation', 'bind_pool', capability_id=ids['capability'], capability_epoch=1,
        pool_id=ids['pool'], pool_epoch=1, dimension='requests', quantity=1, unit='request',
        usage_key='single-owned-sha-use-' + state['run_id'], new_spend_minor=0)
    task = owner.request('/v1/tasks', task_body(state))
    require(task == {'id': ids['task']}, 'provider_probe_task_submission_invalid')
    return {'task_id': ids['task'], 'operation_id': ids['operation'], 'setup_submitted': True,
            'parent_task_claimed_by_helper': False, 'measured_input_bytes': measured,
            'independent_performance_verified': False, 'automatic_retry': False}


def reserve(state, owner, requester, task_epoch, leader_epoch):
    require(type(task_epoch) is int and task_epoch > 0 and type(leader_epoch) is int and leader_epoch > 0,
            'provider_probe_actual_epochs_required')
    task = owner.request('/v1/task/status', {'id': state['ids']['task']})
    status = owner.request('/v1/status')
    leader = status.get('leader', {})
    require(task.get('id') == state['ids']['task'] and task.get('status') == 'running'
            and task.get('node') == state['nodes']['requester'] and type(task.get('epoch')) is int
            and task['epoch'] == task_epoch and type(task.get('leader_epoch')) is int
            and task['leader_epoch'] == leader_epoch
            and task.get('scope') == task_body(state)['context']['session_scope']
            and leader.get('node') == state['nodes']['requester'] and type(leader.get('epoch')) is int
            and leader['epoch'] == leader_epoch and isinstance(status.get('time'), (int, float))
            and leader.get('deadline', 0) > status['time'], 'provider_probe_claim_or_leader_not_current')
    result = rpc(requester, 'allocation', 'reserve', operation_id=state['ids']['operation'],
                 task_id=state['ids']['task'], task_epoch=task_epoch, plan=plan(state), lease_seconds=300)
    validate_remote(state, result)
    require(result.get('task_epoch') == task_epoch and result.get('leader_epoch') == leader_epoch,
            'provider_probe_reserved_epochs_changed')
    return result


def validate_remote(state, value):
    require(isinstance(value, dict) and value.get('operation_id') == state['ids']['operation']
            and value.get('task_id') == state['ids']['task']
            and value.get('actor') == 'node:' + state['nodes']['requester']
            and value.get('task_node') == state['nodes']['requester'] and value.get('plan') == plan(state)
            and type(value.get('task_epoch')) is int and value['task_epoch'] > 0
            and type(value.get('leader_epoch')) is int and value['leader_epoch'] > 0,
            'provider_probe_remote_identity_or_plan_changed')
    dispatch = value.get('dispatch')
    require(isinstance(dispatch, list) and len(dispatch) == 1
            and dispatch[0].get('operation_id') == state['ids']['operation']
            and dispatch[0].get('capability_id') == state['ids']['capability']
            and dispatch[0].get('provider') == 'node:' + state['nodes']['provider'],
            'provider_probe_remote_dispatch_changed')
    return dispatch[0]


def hash_adapter(state):
    """Fixed owner code; closure contains NO expected digest or executable args."""
    def invoke(context):
        require(context.get('operation_id') == state['ids']['operation']
                and context.get('capability_id') == state['ids']['capability']
                and context.get('adapter_handle') == HANDLE and context.get('capability_epoch') == 1
                and context.get('action') == ACTION and context.get('scope') == scope(state)
                and context.get('workload') == {'input_bytes': SIZE}, 'provider_probe_callback_contract_changed')
        # The bounded regular-file read is closed before the result is returned.
        # No processes/children/network are launched; no elapsed-time metric is
        # fabricated from this callback. Never read owner-reference.json here.
        actual = hash_input(state)
        return {'outcome': 'completed', 'resource_quiescent': True,
                'result_reference': 'sha256:' + actual,
                'evidence': {'sha256': actual, 'input_bytes': SIZE,
                             'file_read_closed': True, 'child_processes_started': 0}}
    return Adapter(HANDLE, 1, ACTION, invoke)


def execute(state, provider_client):
    root = private_root(state['root'])
    remote = rpc(provider_client, 'allocation', 'inspect', operation_id=state['ids']['operation'])
    dispatch = validate_remote(state, remote)
    journal = root / 'provider.sqlite'
    if dispatch['state'] != 'pending':
        require(journal.exists(), 'provider_probe_admitted_operation_journal_missing_no_replay')
    bridge = ManagedProvider(provider_client, str(journal), 'node:' + state['nodes']['provider'],
                             {state['ids']['capability']: hash_adapter(state)}, state['authority'])
    if dispatch['state'] != 'pending':
        return bridge.inspect(state['ids']['operation'], state['ids']['capability'])
    pending = rpc(provider_client, 'allocation', 'pending', limit=1000)
    require(isinstance(pending, dict) and isinstance(pending.get('dispatch'), list),
            'provider_probe_pending_invalid')
    matches = [row for row in pending['dispatch'] if row.get('operation_id') == state['ids']['operation']
               and row.get('capability_id') == state['ids']['capability']]
    require(len(matches) == 1, 'provider_probe_original_pending_missing_no_replay')
    return bridge.run(matches[0])


def reconcile(state, provider_client):
    journal = checked(Path(state['root']) / 'provider.sqlite', file=True)
    # Empty bindings: this path cannot enter an adapter even accidentally.
    bridge = ManagedProvider(provider_client, str(journal), 'node:' + state['nodes']['provider'],
                             {}, state['authority'])
    return bridge.reconcile(state['ids']['operation'], state['ids']['capability'])


def canonical_journal(state, value):
    """Read a closed single-operation journal; never checkpoint/recreate it.

    The owner must retain/copy the database after callback/bridge closure. A WAL
    or rollback journal means this is not a demonstrably closed snapshot. URI
    immutable mode avoids even read-only SQLite's possible SHM sidecar creation.
    Hash/sidecar checks before and after reject concurrent snapshot changes.
    SQLite >= 3.22 is required for this verifier; an older provider host can
    execute and retain its journal, then the owner verifies its closed copy on a
    current reader host. Never silently fall back to a SHM-creating reader.
    """
    require(sqlite3.sqlite_version_info >= (3, 22, 0), 'provider_probe_sqlite_immutable_reader_required')
    path = checked(value, file=True)
    def closed_hash():
        checked(path, file=True)
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                Path(str(path) + suffix).lstat()
            except FileNotFoundError:
                pass
            else:
                raise ProbeError('provider_probe_closed_journal_snapshot_required')
        require(path.stat().st_size <= 8 * 1024 * 1024, 'provider_probe_journal_too_large')
        return hashlib.sha256(path.read_bytes()).hexdigest()
    lock = Path(str(path) + '.lock-' + digest([state['ids']['operation'], state['ids']['capability']]))
    descriptor = None
    try:
        try:
            lock.lstat()
        except FileNotFoundError:
            pass  # An owner-copied closed snapshot need not include the lock.
        else:
            checked(lock, file=True)
            descriptor = os.open(str(lock), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProbeError('provider_probe_execution_instance_active')
        before = closed_hash()
        with contextlib.closing(sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro&immutable=1', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            metadata = dict(db.execute('SELECT key,value FROM provider_metadata'))
            require(metadata == {'schema': '1', 'provider': 'node:' + state['nodes']['provider'],
                                 'authority': state['authority']}, 'provider_probe_journal_namespace_changed')
            rows = db.execute('SELECT * FROM provider_executions').fetchall()
            require(len(rows) == 1, 'provider_probe_single_operation_journal_required')
            row = dict(rows[0])
        require(before == closed_hash(), 'provider_probe_journal_changed_during_observe')
    finally:
        if descriptor is not None:
            os.close(descriptor)
    require(row['operation_id'] == state['ids']['operation'] and row['capability_id'] == state['ids']['capability'],
            'provider_probe_journal_operation_changed')
    context = json.loads(row['context'])
    expected = {'operation_id': state['ids']['operation'], 'capability_id': state['ids']['capability'],
        'provider': 'node:' + state['nodes']['provider'], 'receipt_id': 'provider-' + digest([
            state['authority'], 'node:' + state['nodes']['provider'], state['ids']['operation'], state['ids']['capability']]),
        'adapter_handle': HANDLE, 'capability_epoch': 1, 'action': ACTION,
        'scope': scope(state), 'workload': {'input_bytes': SIZE}, 'task_id': state['ids']['task'],
        'task_epoch': context.get('task_epoch')}
    require(type(context.get('task_epoch')) is int and context['task_epoch'] > 0 and context == expected
            and row['receipt_id'] == expected['receipt_id'] and row['fingerprint'] == digest(context),
            'provider_probe_journal_context_changed')
    return row, before


def observe(state, owner, journal=None):
    """Read-only independent result matching; no ManagedProvider construction."""
    remote = rpc(owner, 'allocation', 'inspect', operation_id=state['ids']['operation'])
    dispatch = validate_remote(state, remote)
    root = private_root(state['root'])
    row, journal_hash = canonical_journal(state, journal or root / 'provider.sqlite')
    retained = reference(state)
    require(hash_input(state) == retained['sha256'], 'provider_probe_owner_input_reference_changed')
    context = json.loads(row['context'])
    require(context['task_epoch'] == remote['task_epoch'], 'provider_probe_journal_task_epoch_changed')
    result = json.loads(row['result']) if row['result'] else None
    expected = 'sha256:' + retained['sha256']
    evidence = {'sha256': retained['sha256'], 'input_bytes': SIZE,
                'file_read_closed': True, 'child_processes_started': 0}
    local_result = (isinstance(result, dict) and result == {'outcome': 'completed',
                    'resource_quiescent': True, 'result_reference': expected, 'evidence': evidence})
    settlement = {'outcome': 'completed', 'evidence': dict(evidence, resource_quiescent=True,
        result_reference=expected, adapter_handle=HANDLE, invocation_nonce=row['invocation_nonce'])}
    correct = (local_result and row['state'] == 'settled' and row['error'] is None
        and isinstance(row['invocation_nonce'], str) and re.fullmatch('[0-9a-f]{32}', row['invocation_nonce']) is not None
        and dispatch.get('receipt_id') == row['receipt_id'] and dispatch.get('state') == 'completed'
        and dispatch.get('epoch') == row['authority_epoch'] and dispatch.get('settlement') == settlement
        and remote.get('state') == 'completed' and remote.get('capacity_held') is False)
    return {'format': REPORT, 'run_id': state['run_id'], 'authority_id': state['authority'],
        'task_id': state['ids']['task'], 'operation_id': state['ids']['operation'],
        'capability_id': state['ids']['capability'], 'task_epoch': remote['task_epoch'],
        'leader_epoch_at_reservation': remote['leader_epoch'], 'provider': 'node:' + state['nodes']['provider'],
        'local_state': row['state'], 'authority_state': remote['state'],
        'canonical_closed_journal_sha256': journal_hash,
        'actual_result_matches_independently_retained_owner_reference': bool(correct),
        'result_verification': 'owner_reference_match' if correct else 'not_verified',
        'execution_verified_by_core': False, 'independent_performance_verified': False,
        'independent_physical_host_verified': False, 'resource_quiescence_independently_verified': False,
        'authority_settlement_and_capacity_release_confirmed': bool(correct),
        'automatic_replay': False, 'retry_with_new_id': False, 'native_tools_intercepted': False,
        'models_or_services_started_by_helper': False}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProbeError('provider_probe_cli_arguments_invalid')


def main(argv=None):
    output = {'probe': FORMAT, 'automatic_retry': False, 'retry_with_new_id': False,
              'native_tools_intercepted': False, 'production_services_changed': False,
              'model_started_by_helper': False, 'native_auth_values_read_or_copied': False,
              'new_paid_api_provisioned': False}
    try:
        parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('command', nargs='?', default='observe',
                            choices=('prepare', 'submit', 'reserve', 'execute', 'observe', 'reconcile'))
        parser.add_argument('--root', required=True)
        parser.add_argument('--port', type=int, default=17683)
        parser.add_argument('--task-epoch', type=int)
        parser.add_argument('--leader-epoch', type=int)
        parser.add_argument('--journal')
        parser.add_argument('--report')
        args = parser.parse_args(argv)
        require(args.journal is None or args.command == 'observe', 'provider_probe_journal_only_for_observe')
        require(args.report is None or args.command == 'observe', 'provider_probe_report_only_for_observe')
        require((args.task_epoch is None and args.leader_epoch is None) or args.command == 'reserve',
                'provider_probe_epochs_only_for_reserve')
        if args.command == 'prepare':
            state = prepare(args.root, args.port)
            output.update(prepared=True, run_id=state['run_id'], ids=state['ids'], phase_nodes=state['nodes'])
        else:
            state = load_state(args.root)
            if args.command == 'submit':
                output['submission'] = submit(state, {role: client_for(state, role) for role in ROLES})
            elif args.command == 'reserve':
                result = reserve(state, client_for(state, 'operator'), client_for(state, 'requester'),
                                 args.task_epoch, args.leader_epoch)
                output['reservation'] = {'operation_id': result['operation_id'], 'task_epoch': result['task_epoch'],
                    'leader_epoch': result['leader_epoch'], 'state': result['state'],
                    'reservation_created': result['reservation_created'], 'execution_verified': False}
            elif args.command == 'execute':
                output['execution'] = execute(state, client_for(state, 'provider'))
            elif args.command == 'reconcile':
                output['reconciliation'] = reconcile(state, client_for(state, 'provider'))
            else:
                output['observation'] = observe(state, client_for(state, 'operator'), args.journal)
                if args.report:
                    report = Path(args.report)
                    require(report.parent == Path(state['root']), 'provider_probe_report_in_private_root_required')
                    publish(report, output['observation'])
        code = 0 if args.command != 'observe' or output['observation'][
            'actual_result_matches_independently_retained_owner_reference'] else 2
    except Exception as error:
        # Do not echo HTTP bodies, paths, exception strings or credentials.
        output.update(error=str(error) if isinstance(error, ProbeError) else 'provider_probe_' + type(error).__name__,
                      completion_not_assumed=True, preserve_original_state_and_journal=True)
        code = 1
    print(json.dumps(output, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
