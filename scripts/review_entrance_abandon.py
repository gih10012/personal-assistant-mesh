"""Owner-only abandonment of an audited internal-notification/silent unknown.

Not native completion, zero-effect retry, quota reconciliation or a worker
closed-failure receipt. All case bindings and bodies stay in an approved private
spec. Never starts/stops a service, invokes native tools or requeues old input.
The entrance service must remain owner-stopped through the committed readback.
"""
import argparse
import codecs
import contextlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import quote

from assistant_mesh.entrance import validate_policy

_loader = importlib.util.spec_from_file_location('_entrance_abandon_util',
    str(Path(__file__).with_name('review_native_effects.py')))
util = importlib.util.module_from_spec(_loader)
_loader.loader.exec_module(util)
require, digest, parse = util.require, util.digest, util.parse
PROTOCOL = 'owner-entrance-abandon/1'
CASE = 'internal-notification-two-exec-silent/1'
NAMESPACE = 'owner-entrance-abandoned-unknown-v1'
TABLE = 'native_entrance_abandon_reviews'
SYSTEMD_FIELDS = ('Id', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlPID',
    'Job', 'ControlGroup', 'InvocationID', 'KillMode', 'StateChangeTimestampMonotonic',
    'InactiveEnterTimestampMonotonic')


def load(path, expired=False):
    raw, version = util.read_checked(path, private=True, limit=util.MAX_SPEC)
    spec = parse(raw.decode('utf8'))
    util.exact(spec, ('protocol', 'case', 'issued_at', 'expires_at', 'worker_config',
        'server_config', 'review_root', 'expected', 'backup', 'closure'), 'abandon_spec_shape')
    require(spec['protocol'] == PROTOCOL and spec['case'] == CASE, 'abandon_case_required')
    start, end = spec['issued_at'], spec['expires_at']
    require(type(start) in (int, float) and type(end) in (int, float)
            and 0 < end - start <= 1800, 'abandon_spec_window')
    require(expired or start <= time.time() <= end, 'abandon_spec_expired')
    exp = spec['expected']
    util.exact(exp, ('task_id', 'epoch', 'scope', 'event_id', 'thread_id', 'turn_id',
        'worker_sha256', 'server_sha256', 'task_sha256', 'session_sha256',
        'checkpoint_sha256', 'session_state_sha256', 'event_sha256', 'action_row_sha256',
        'rollout_path', 'rollout_file', 'selected_sha256', 'selected_size', 'record_counts',
        'old_artifact', 'tools'), 'abandon_expected_shape')
    require(isinstance(exp['event_id'], str) and re.fullmatch('notice-[0-9a-f]{64}', exp['event_id'])
            and exp['task_id'] == 'entrance-' + exp['event_id'], 'abandon_original_notice_required')
    require(type(exp['epoch']) is int and exp['epoch'] >= 1, 'abandon_original_epoch_required')
    for name in ('thread_id', 'turn_id'):
        util.quota.identity(exp[name])
    require(isinstance(exp['tools'], list) and len(exp['tools']) == 2
            and len({tool['call_id'] for tool in exp['tools']}) == 2, 'abandon_two_execs_required')
    for tool in exp['tools']:
        util.exact(tool, ('call_id', 'input', 'input_sha256', 'output_canonical_sha256'),
                   'abandon_original_tool_shape')
        require(isinstance(tool['input'], str) and util.sha(tool['input'].encode()) == tool['input_sha256'],
                'abandon_original_input_changed')
    util.exact(spec['backup'], ('path', 'sha256', 'size', 'sql_sha256', 'sql_size'),
               'abandon_backup_shape')
    util.private_directory(spec['review_root'])
    return spec, version


def case_id(spec):
    exp = spec['expected']
    return digest([NAMESPACE, exp['task_id'], exp['epoch'], exp['task_sha256'],
                   exp['session_sha256'], exp['rollout_file']['sha256']])


def authority(spec):
    """Original entrance peer, NOT quota.authority's leader/agent assumptions."""
    worker_raw, worker_file = util.read_checked(spec['worker_config'], private=True, limit=util.MAX_SPEC)
    server_raw, server_file = util.read_checked(spec['server_config'], private=True, limit=util.MAX_SPEC)
    worker, server = parse(worker_raw.decode()), parse(server_raw.decode())
    exp = spec['expected']
    require(worker_file['sha256'] == exp['worker_sha256']
            and server_file['sha256'] == exp['server_sha256'], 'abandon_config_changed')
    policy = validate_policy(worker.get('entrance'))
    node, capability = worker.get('node_id'), policy['capability']
    require(isinstance(node, str) and node and node != server.get('node_id')
            and worker.get('capabilities') == [capability], 'abandon_entrance_worker_required')
    require(any(p.get('role') == 'worker' and p.get('node') == node
                and capability in p.get('capabilities', []) for p in server.get('peers', []))
            and any(p.get('role') == 'operator' for p in server.get('peers', [])),
            'abandon_original_entrance_peer_required')
    codex = worker.get('codex') or {}
    roster = worker.get('codex_accounts', [codex])
    require(isinstance(roster, list) and roster and all(isinstance(row, dict) for row in roster),
            'abandon_original_auth_roster_required')
    homes = {str(util.quota.checked_path(row.get('auth_home', codex.get('auth_home')))) for row in roster}
    database = util.quota.checked_path(server['database'], file=True, private=True)
    return {'node': node, 'homes': homes, 'database': database, 'policy': policy,
            'worker_file': worker_file, 'server_file': server_file}


def selected(db, auth, spec):
    exp = spec['expected']
    value = util.quota.snapshot(db, exp['task_id'], exp['epoch'], auth)
    task, session, cp = value['task'], value['session'], value['checkpoint']
    require(digest(task) == exp['task_sha256'] and digest(session) == exp['session_sha256']
            and util.sha(task['checkpoint'].encode()) == exp['checkpoint_sha256']
            and util.sha(session['state'].encode()) == exp['session_state_sha256'],
            'abandon_original_generation_changed')
    require(task['scope'] == exp['scope'] == auth['policy']['scope'] and task['parent_id'] is None
            and parse(task['required']) == [auth['policy']['capability']]
            and value['thread'] == exp['thread_id'] and value['turn'] == exp['turn_id'],
            'abandon_original_binding_changed')
    state, context = parse(session['state']), parse(task['context'])
    for item in (cp, state):
        require(not item.get('goal') and not item.get('native_lifecycle')
                and not item.get('approval_required'), 'abandon_unreviewed_lifecycle')
    require(not state.get('side_effect_started')
            and cp.get('runtime_failure', {}).get('category') == 'codex_disconnected'
            and cp['runtime_failure'].get('phase') == 'native_finish'
            and cp['runtime_failure'].get('outcome') == 'unknown'
            and cp['runtime_failure'].get('native_start_attempted') is True,
            'abandon_original_disconnected_unknown_required')
    require(context.get('entrance', {}).get('event_id') == exp['event_id'], 'abandon_event_context_changed')
    require(not db.execute("SELECT 1 FROM tasks WHERE (scope=? OR node=?) AND status='running' LIMIT 1",
                           (exp['scope'], auth['node'])).fetchone()
            and not db.execute('SELECT 1 FROM tasks WHERE parent_id=? LIMIT 1', (task['id'],)).fetchone(),
            'abandon_worker_or_child_not_idle')
    for row in db.execute("SELECT checkpoint FROM tasks WHERE scope=? AND id<>? AND status<>'completed'",
                          (task['scope'], task['id'])):
        require(not parse(row[0]).get('side_effect_started'), 'abandon_other_scope_guard')
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in util.DEPENDENCY_TABLES & tables:
        for row in db.execute('SELECT * FROM ' + name):
            require(not any(isinstance(cell, str) and task['id'] in cell for cell in row),
                    'abandon_task_has_dependency')
    event_row = db.execute('SELECT * FROM entrance_events WHERE event_id=?', (exp['event_id'],)).fetchone()
    require(event_row is not None, 'abandon_original_event_missing')
    event = dict(event_row)
    require(digest(event) == exp['event_sha256'] and event['task_id'] == task['id']
            and event['direction'] == 'internal' and event['kind'] == 'notification'
            and event['outbound_request_id'] is None and parse(event['decision']).get('kind') == 'silent',
            'abandon_original_silent_event_changed')
    actions = [dict(row) for row in db.execute('SELECT * FROM entrance_actions WHERE action_id>=? AND action_id<? ORDER BY action_id',
                                               (task['id'] + ':exec-', task['id'] + ':exec.'))]
    require(len(actions) == 3 and {row['action_id']: digest(row) for row in actions} == exp['action_row_sha256'],
            'abandon_original_actions_changed')
    require(not db.execute('SELECT 1 FROM entrance_replies WHERE event_id=? LIMIT 1', (exp['event_id'],)).fetchone()
            and not db.execute("SELECT 1 FROM entrance_events WHERE reply_to=? AND direction='outbound' LIMIT 1",
                               (exp['event_id'],)).fetchone()
            and not db.execute('SELECT 1 FROM entrance_closed_failures WHERE task_id=? LIMIT 1', (task['id'],)).fetchone(),
            'abandon_reply_or_native_closed_failure_present')
    util.old_artifact(db, value, {'expected': exp})
    return dict(value, event=event, actions=actions)


def native(spec, value):
    exp = spec['expected']
    path = util.quota.canonical_rollout(exp['rollout_path'], value)
    raw, version = util.read_checked(path, private=True)
    # Spec ns fields are decimal strings, never a JavaScript/float round-trip.
    expected = dict(exp['rollout_file'])
    for key in ('mtime_ns', 'ctime_ns'):
        require(isinstance(expected.get(key), str) and re.fullmatch('[0-9]+', expected[key]),
                'abandon_exact_ns_strings_required')
        expected[key] = int(expected[key])
    require(version == expected and exp['old_artifact']['raw_size'] < len(raw), 'abandon_full_history_changed')
    active, lines, calls, outputs, counts = False, [], [], [], {}
    for number, line in enumerate(raw.splitlines(True)):
        require(line.endswith(b'\n'), 'abandon_incomplete_native_record')
        record = parse(line.decode('utf8')); payload = record.get('payload') or {}
        if number == 0:
            require(record.get('type') == 'session_meta' and payload.get('id') == value['thread'],
                    'abandon_original_native_thread_changed')
        if record.get('type') == 'event_msg' and payload.get('type') == 'task_started':
            require(not active, 'abandon_later_native_turn')
            active = payload.get('turn_id') == value['turn']
        if not active:
            continue
        lines.append(line)
        require(payload.get('turn_id', value['turn']) == value['turn'], 'abandon_native_turn_changed')
        kind = record.get('type') + ':' + str(payload.get('type') or '')
        counts[kind] = counts.get(kind, 0) + 1
        require(not (record.get('type') == 'event_msg' and payload.get('type') == 'task_complete'),
                'abandon_unexpected_native_completion')
        if record.get('type') == 'response_item':
            require(payload.get('type') in ('message', 'reasoning', 'custom_tool_call', 'custom_tool_call_output'),
                    'abandon_unreviewed_native_call')
            if payload.get('type') == 'custom_tool_call':
                require(payload.get('name') == 'exec', 'abandon_two_execs_required')
                calls.append({'call_id': payload['call_id'], 'input': payload['input'],
                              'input_sha256': util.sha(payload['input'].encode())})
            if payload.get('type') == 'custom_tool_call_output':
                outputs.append((payload['call_id'], digest(payload['output'])))
    segment = b''.join(lines)
    require(active and len(segment) == exp['selected_size'] and util.sha(segment) == exp['selected_sha256']
            and counts == exp['record_counts'], 'abandon_selected_history_changed')
    expected_calls = [{key: tool[key] for key in ('call_id', 'input', 'input_sha256')} for tool in exp['tools']]
    expected_outputs = [(tool['call_id'], tool['output_canonical_sha256']) for tool in exp['tools']]
    require(calls == expected_calls and outputs == expected_outputs, 'abandon_original_execs_changed')
    return raw, version


def closure(spec):
    """This owner user-unit + named-systemd cgroup-v1 profile only, no SSH.

    A currently stopped unit and emptied original cgroup fence NEW execution.
    They do not reconstruct the disconnected runtime's historical reap/EOF.
    """
    pin = spec['closure']
    util.exact(pin, ('evidence_path', 'evidence_sha256'), 'abandon_closure_pin_shape')
    raw, evidence_file = util.read_checked(pin['evidence_path'], private=True, limit=util.MAX_SPEC)
    require(evidence_file['sha256'] == pin['evidence_sha256'], 'abandon_closure_evidence_changed')
    evidence = parse(raw.decode())
    util.exact(evidence, ('protocol', 'uid', 'unit', 'before', 'after', 'owner_stop_completed',
        'current_execution_boundary_only', 'native_terminal_proven', 'historical_reader_eof_proven'),
        'abandon_closure_evidence_shape')
    require(evidence['protocol'] == 'owner-stopped-entrance-user-service/1'
            and type(evidence['uid']) is int and evidence['uid'] == os.getuid()
            and evidence['owner_stop_completed'] is True and evidence['current_execution_boundary_only'] is True
            and evidence['native_terminal_proven'] is False and evidence['historical_reader_eof_proven'] is False,
            'abandon_explicit_current_owner_boundary_required')
    unit = evidence['unit']; before, after = evidence['before'], evidence['after']
    require(isinstance(unit, str) and re.fullmatch('[A-Za-z0-9_.@-]+\\.service', unit),
            'abandon_user_unit_required')
    for value in (before, after):
        util.exact(value, SYSTEMD_FIELDS, 'abandon_user_unit_fields')
        require(all(isinstance(v, str) for v in value.values()), 'abandon_systemd_decimal_strings_required')
    cgroup = '/user.slice/user-{0}.slice/user@{0}.service/{1}'.format(evidence['uid'], unit)
    require(before['Id'] == after['Id'] == unit and before['LoadState'] == after['LoadState'] == 'loaded'
            and before['ActiveState'] == 'active' and before['SubState'] == 'running'
            and before['MainPID'].isdigit() and int(before['MainPID']) > 0
            and before['ControlPID'] == '0' and before['Job'] == '' and before['ControlGroup'] == cgroup
            and re.fullmatch('[0-9a-f]{32}', before['InvocationID'])
            and before['KillMode'] == after['KillMode'] == 'control-group'
            and after['ActiveState'] == 'inactive' and after['SubState'] == 'dead'
            and after['MainPID'] == after['ControlPID'] == '0' and after['Job'] == ''
            and after['ControlGroup'] in ('', cgroup) and after['InvocationID'] in ('', before['InvocationID']),
            'abandon_owner_stopped_original_user_unit_required')
    stamps = [before['StateChangeTimestampMonotonic'], after['StateChangeTimestampMonotonic'],
              after['InactiveEnterTimestampMonotonic']]
    require(all(re.fullmatch('[0-9]+', stamp) for stamp in stamps)
            and 0 < int(stamps[0]) < int(stamps[2]) <= int(stamps[1]), 'abandon_original_stop_generation_required')
    executable = '/usr/bin/systemctl'
    _, executable_file = util.read_checked(executable, root_ok=True, limit=32 * 1024 * 1024)
    runtime = '/run/user/' + str(os.getuid())
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'XDG_RUNTIME_DIR': runtime,
           'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + runtime + '/bus'}
    command = [executable, '--user', '--no-pager', 'show', unit] + ['--property=' + key for key in SYSTEMD_FIELDS]
    observed = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=5, check=True).stdout
    require(len(observed) <= 65536, 'abandon_user_unit_output_bound')
    actual = dict(line.split('=', 1) for line in observed.decode().splitlines())
    require(actual == after, 'abandon_original_user_service_not_stopped')
    mount_root = Path('/sys/fs/cgroup/systemd')
    with open('/proc/self/mountinfo', 'r') as source:
        mounts = source.read(1024 * 1024 + 1)
    require(len(mounts) <= 1024 * 1024, 'abandon_mountinfo_bound')
    matches = []
    for line in mounts.splitlines():
        fields = line.split(); separator = fields.index('-')
        if fields[4] == str(mount_root):
            require(fields[separator + 1] == 'cgroup'
                    and 'name=systemd' in fields[separator + 3].split(','), 'abandon_named_systemd_cgroup_v1_required')
            matches.append(line)
    require(len(matches) == 1 and not mount_root.is_symlink(), 'abandon_named_systemd_cgroup_v1_required')
    group = mount_root / cgroup.lstrip('/')
    # No glob-selected arbitrary cgroups: only the original unit subtree.
    for parent in reversed((group,) + tuple(group.parents)):
        require(not parent.is_symlink(), 'abandon_cgroup_symlink_denied')
    try:
        group_metadata = group.lstat()
    except FileNotFoundError:
        group_metadata = None
    if group_metadata is not None:
        require(util.stat.S_ISDIR(group_metadata.st_mode), 'abandon_cgroup_directory_required')
        def walk_error(unused):
            raise util.ReviewDenied('abandon_cgroup_unreadable')
        for folder, directories, unused_files in os.walk(str(group), onerror=walk_error):
            require(not any((Path(folder) / name).is_symlink() for name in directories), 'abandon_cgroup_symlink_denied')
            with open(str(Path(folder) / 'cgroup.procs'), 'r') as source:
                require(source.read(65537).strip() == '', 'abandon_original_cgroup_not_empty')
    return {'evidence_file': evidence_file, 'systemctl_file': executable_file,
            'user_service': actual, 'cgroup_mount': matches[0], 'original_cgroup': cgroup,
            'original_cgroup_empty_or_removed': True, 'current_execution_boundary_only': True,
            'native_terminal_proven': False, 'historical_reader_eof_proven': False}


def verify_backup(spec, auth, before):
    """Audited deferred-review pattern: complete gzip/SQL, disposable DB only."""
    pin = spec['backup']; path = Path(pin['path'])
    version = util.digest_checked_backup(path)
    require(version['sha256'] == pin['sha256'] and version['size'] == pin['size'], 'abandon_backup_changed')
    root = util.private_directory(spec['review_root'])
    # This is a NEW empty verification DB, not a second live-ledger backup.
    # Keep a hard one-ledger page cap, a separate 32MiB work reserve and the
    # existing 1GiB free-space floor. A larger restore must fail, never expand.
    bound = util.file_metadata(auth['database'], private=True)['size']
    require(shutil.disk_usage(str(root)).free >= bound + 32 * 1024 * 1024 + 1024 ** 3,
            'abandon_backup_space_required')
    with contextlib.closing(util.quota.readonly(auth['database'])) as live:
        schema = [tuple(r) for r in live.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
    checksum, size = util.hashlib.sha256(), 0
    with tempfile.TemporaryDirectory(prefix='.entrance-abandon-backup.', dir=str(root)) as folder:
        target = Path(folder) / 'verification.sqlite'
        with util.create_file(target):
            pass
        with contextlib.closing(sqlite3.connect(str(target), isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            db.execute('PRAGMA max_page_count=' + str(max(1, bound // page_size)))
            db.set_authorizer(util.backup_authorizer)
            pending, committed, count = '', False, 0
            decoder = codecs.getincrementaldecoder('utf8')()
            with os.fdopen(os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW), 'rb') as compressed, \
                    gzip.GzipFile(fileobj=compressed, mode='rb') as stream:
                for block in iter(lambda: stream.readline(65536), b''):
                    checksum.update(block); size += len(block); pending += decoder.decode(block)
                    require(len(pending.encode('utf8')) <= util.MAX_FILE, 'abandon_backup_statement_bound')
                    if not sqlite3.complete_statement(pending):
                        continue
                    statement = pending.strip()
                    require(not committed and (count or statement == 'BEGIN TRANSACTION;'),
                            'abandon_backup_transaction_incomplete')
                    db.execute(pending); count += 1
                    require(shutil.disk_usage(str(root)).free >= 1024 ** 3, 'abandon_backup_space_required')
                    committed = statement == 'COMMIT;'; pending = ''
                pending += decoder.decode(b'', final=True)
            require(committed and not pending.strip() and not db.in_transaction
                    and size == pin['sql_size'] and checksum.hexdigest() == pin['sql_sha256'],
                    'abandon_backup_sql_incomplete_or_changed')
            # Only this trusted verifier runs SQL after restoration. Permit the
            # additional entrance tables, but never dump triggers/functions.
            def read_authorizer(action, first, second, database, trigger):
                allowed = action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ)
                allowed |= action == sqlite3.SQLITE_PRAGMA and first == 'quick_check' and second is None
                return sqlite3.SQLITE_OK if allowed and trigger is None and database in (None, 'main') else sqlite3.SQLITE_DENY
            db.set_authorizer(read_authorizer)
            require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                    and [tuple(r) for r in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')] == schema
                    and selected(db, auth, spec) == before, 'abandon_backup_original_state_changed')
    require(util.digest_checked_backup(path) == version, 'abandon_backup_generation_changed')
    return version


def inspect(path):
    spec, version = load(path); auth = authority(spec)
    live = closure(spec)
    with contextlib.closing(util.quota.readonly(auth['database'])) as db:
        before = selected(db, auth, spec)
    raw, rollout = native(spec, before)
    backup = verify_backup(spec, auth, before)
    require(closure(spec) == live, 'abandon_closure_generation_changed')
    return {'spec': spec, 'version': version, 'auth': auth, 'selected': before,
            'raw': raw, 'rollout': rollout, 'closure': live, 'backup': backup}


def receipt_for(spec):
    exp = spec['expected']; identifier = case_id(spec)
    return {'protocol': PROTOCOL, 'id': identifier, 'task_id': exp['task_id'],
        'source_epoch': exp['epoch'], 'closed_epoch': exp['epoch'] + 1, 'status': 'failed',
        'owner_decision': 'owner-abandoned-unknown', 'native_outcome': 'unknown',
        'native_terminal_proven': False, 'historical_reader_eof_proven': False,
        'effects_zero': False, 'external_effects_settled': False, 'original_input_requeued': False,
        'complete_artifact': digest([NAMESPACE, 'full-history', identifier]),
        'full_rollout_sha256': exp['rollout_file']['sha256'], 'native_session_state_changed': False,
        'old_chunks_changed': False, 'retry_authorized': False}


def closed_task(before, spec):
    checkpoint = parse(before['checkpoint'])
    checkpoint.update(side_effect_started=False, owner_abandon_review={
        'protocol': PROTOCOL, 'id': case_id(spec), 'owner_decision': 'owner-abandoned-unknown',
        'guard_release_basis': 'explicit-owner-abandon-after-current-writer-closure',
        'native_outcome': 'unknown', 'effects_zero': False, 'external_effects_settled': False,
        'native_terminal_proven': False, 'historical_reader_eof_proven': False,
        'original_input_requeue_authorized': False})
    return dict(before, status='failed', epoch=before['epoch'] + 1, node=None,
                deadline=None, checkpoint=json.dumps(checkpoint))


def complete_history(packed, spec):
    pin = spec['expected']['rollout_file']; size, checksum = 0, util.hashlib.sha256()
    with gzip.GzipFile(fileobj=io.BytesIO(packed), mode='rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            size += len(block); require(size <= pin['size'], 'abandon_history_bound'); checksum.update(block)
    require(size == pin['size'] and checksum.hexdigest() == pin['sha256'], 'abandon_full_artifact_changed')


def status(path, current_session=False):
    spec, version = load(path, expired=True); auth = authority(spec); identifier = case_id(spec)
    directory = Path(spec['review_root']) / identifier
    with contextlib.closing(util.quota.readonly(auth['database'])) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
        row = db.execute('SELECT * FROM ' + TABLE + ' WHERE id=?', (identifier,)).fetchone() if exists else None
        if row:
            proof, receipt = parse(row['proof']), parse(row['receipt']); before = proof['selected']
            require(proof['spec_sha256'] == version['sha256']
                    and digest(before['task']) == spec['expected']['task_sha256']
                    and digest(before['session']) == spec['expected']['session_sha256']
                    and receipt == receipt_for(spec)
                    and all(type(receipt[key]) is type(item) for key, item in receipt_for(spec).items()),
                    'abandon_review_identity_conflict')
            require(dict(db.execute('SELECT * FROM tasks WHERE id=?', (spec['expected']['task_id'],)).fetchone())
                    == closed_task(before['task'], spec), 'abandon_review_task_readback_conflict')
            util.old_artifact(db, before, {'expected': spec['expected']})
            packed = io.BytesIO()
            for number, (part, body) in enumerate(db.execute('SELECT part,body FROM session_chunks WHERE id=? ORDER BY part',
                                                            (receipt['complete_artifact'],))):
                require(part == number and len(body) <= 65536, 'abandon_artifact_parts_changed')
                packed.write(body); require(packed.tell() <= util.MAX_FILE, 'abandon_artifact_bound')
            complete_history(packed.getvalue(), spec)
            if current_session:
                session = db.execute('SELECT * FROM native_sessions WHERE scope=? AND harness=?',
                                     (before['session']['scope'], before['session']['harness'])).fetchone()
                require(dict(session) == proof['after_session'], 'abandon_session_readback_conflict')
            return dict(receipt, applied=True, outcome_unknown=False, read_only=True)
    attempted = directory.exists()
    return {'protocol': PROTOCOL, 'id': identifier, 'read_only': True,
            'applied': None if attempted else False, 'outcome_unknown': attempted, 'retry_authorized': False}


def apply(path, approved_sha256):
    spec, version = load(path)
    require(version['sha256'] == approved_sha256, 'abandon_explicit_spec_approval_required')
    directory = util.private_directory(spec['review_root']) / case_id(spec)
    require(not directory.exists(), 'abandon_existing_intent_use_status')
    context = inspect(path); before, auth = context['selected'], context['auth']
    require(context['version'] == version, 'abandon_approved_spec_changed')
    directory.mkdir(mode=0o700); util.fsync_directory(directory.parent)
    util.write_json(directory / '00-intent.json', {'protocol': PROTOCOL, 'id': case_id(spec),
        'approved_spec_sha256': approved_sha256, 'selected': before, 'backup': context['backup']})
    history = directory / '03-full-rollout.gz'
    with util.create_file(history) as target:
        with gzip.GzipFile(filename='', fileobj=target, mode='wb', mtime=0) as compressed:
            compressed.write(context['raw'])
        target.flush(); os.fsync(target.fileno())
    packed, packed_version = util.read_checked(history, private=True); complete_history(packed, spec)
    receipt = receipt_for(spec); artifact = receipt['complete_artifact']
    util.write_json(directory / '06-cas-intent.json', {'protocol': PROTOCOL, 'id': case_id(spec),
        'approved_spec_sha256': approved_sha256, 'closure': context['closure']})
    try:
        with contextlib.closing(sqlite3.connect('file:' + quote(str(auth['database']), safe='/') + '?mode=rw',
                                               uri=True, isolation_level=None, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            try:
                db.execute('BEGIN IMMEDIATE')
                require(load(path)[1] == version and authority(spec) == auth, 'abandon_controls_cas_changed')
                require(selected(db, auth, spec) == before and native(spec, before)[1] == context['rollout'],
                        'abandon_original_cas_changed')
                require(util.digest_checked_backup(spec['backup']['path']) == context['backup']
                        and util.read_checked(history, private=True)[1] == packed_version
                        and closure(spec) == context['closure'], 'abandon_evidence_cas_changed')
                db.execute('CREATE TABLE IF NOT EXISTS ' + TABLE + '''(
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL,source_epoch INTEGER NOT NULL,
                    proof TEXT NOT NULL,receipt TEXT NOT NULL,UNIQUE(task_id,source_epoch))''')
                for operation in ('UPDATE', 'DELETE'):
                    db.execute('CREATE TRIGGER IF NOT EXISTS entrance_abandon_no_' + operation.lower()
                        + ' BEFORE ' + operation + ' ON ' + TABLE
                        + " BEGIN SELECT RAISE(ABORT,'owner abandon review is append only'); END")
                require(not db.execute('SELECT 1 FROM session_chunks WHERE id=? LIMIT 1', (artifact,)).fetchone(),
                        'abandon_artifact_namespace_conflict')
                for part, offset in enumerate(range(0, len(packed), 65536)):
                    db.execute('INSERT INTO session_chunks VALUES(?,?,?)', (artifact, part, packed[offset:offset + 65536]))
                old = before['session']; updated = time.time()
                changed = db.execute('''UPDATE native_sessions SET artifact=?,updated=? WHERE scope=? AND harness=?
                    AND node=? AND state=? AND artifact IS ? AND updated=?''',
                    (artifact, updated, old['scope'], old['harness'], old['node'], old['state'], old['artifact'], old['updated'])).rowcount
                require(changed == 1, 'abandon_session_cas_failed')
                after = closed_task(before['task'], spec)
                changed = db.execute('''UPDATE tasks SET status='failed',epoch=?,node=NULL,deadline=NULL,checkpoint=?
                    WHERE id=? AND epoch=? AND status='needs_review' AND checkpoint=? AND scope=?''',
                    (after['epoch'], after['checkpoint'], spec['expected']['task_id'], spec['expected']['epoch'],
                     before['task']['checkpoint'], before['task']['scope'])).rowcount
                require(changed == 1, 'abandon_task_cas_failed')
                proof = {'spec_sha256': approved_sha256, 'selected': before, 'backup': context['backup'],
                    'closure': context['closure'], 'rollout': context['rollout'],
                    'after_session': dict(old, artifact=artifact, updated=updated)}
                db.execute('INSERT INTO ' + TABLE + ' VALUES(?,?,?,?,?)',
                    (case_id(spec), spec['expected']['task_id'], spec['expected']['epoch'], json.dumps(proof), json.dumps(receipt)))
                def commit_authorizer(action, first, second, unused_db, unused_trigger):
                    if action == getattr(sqlite3, 'SQLITE_TRANSACTION', 22) and first == 'COMMIT':
                        try:
                            require(load(path)[1] == version and authority(spec) == auth
                                    and native(spec, before)[1] == context['rollout']
                                    and closure(spec) == context['closure'], 'abandon_commit_boundary_changed')
                        except (OSError, ValueError, subprocess.SubprocessError):
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.set_authorizer(commit_authorizer); db.commit()
            except BaseException:
                db.rollback(); raise
        require(status(path, current_session=True)['applied'] is True
                and closure(spec) == context['closure'], 'abandon_same_case_readback_required')
        util.write_json(directory / '07-receipt.json', receipt)
        return dict(receipt, applied=True, outcome_unknown=False)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        return {'protocol': PROTOCOL, 'id': case_id(spec), 'applied': None, 'outcome_unknown': True,
                'retry_authorized': False, 'code': 'abandon_same_case_requires_status'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec-file', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true'); mode.add_argument('--status', action='store_true')
    parser.add_argument('--approve-spec-sha256')
    args = parser.parse_args(argv)
    try:
        if args.status:
            answer = status(args.spec_file)
        elif args.apply:
            answer = apply(args.spec_file, args.approve_spec_sha256)
        else:
            context = inspect(args.spec_file)
            answer = dict(receipt_for(context['spec']), eligible=True, applied=False, read_only=True,
                          backup_sha256=context['backup']['sha256'])
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        answer = {'protocol': PROTOCOL, 'eligible': False, 'applied': False,
                  'code': str(error) if isinstance(error, util.ReviewDenied) else 'abandon_review_unavailable'}
        print(json.dumps(answer, sort_keys=True)); return 2
    print(json.dumps(answer, sort_keys=True)); return 0


if __name__ == '__main__':
    raise SystemExit(main())
