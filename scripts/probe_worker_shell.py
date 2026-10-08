"""Owner-authorized Shell acceptance through an existing persistent Worker.

Default: observe the SAME private state/task, without submitting or starting a
model. --submit explicitly uses /v1/tasks with one immutable ID/body. This probe
never creates Codex, changes a Worker, retries under a new ID, or provisions an
API. Run on the selected Worker's host so its private challenge is accessible.
--server-config optionally supplies read-only, selected-task DB evidence when
the operator API does not expose its checkpoint. No raw prompt, config, native
history, final reply, or credential is printed. Requested read-only work is not
a runtime sandbox. Root must independently verify the actual systemd process.
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import sqlite3
import stat
import sys
import tempfile
import time
import urllib.error
import uuid
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from assistant_mesh.config import private_json
from assistant_mesh.worker import Client


FORMAT = 'mesh-worker-shell-probe/1'
MAX_LINE = 8 * 1024 * 1024
STATUSES = ('pending', 'running', 'completed', 'failed', 'needs_review', 'paused',
            'waiting_auth', 'waiting_backend', 'waiting_children', 'waiting_remote', 'continuing')
TERMINAL = ('completed', 'failed', 'needs_review')


class ProbeError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ProbeError(code)


def checked_path(value, file=False, private=False, missing=False):
    path = Path(value).expanduser()
    require(path.is_absolute() and '..' not in path.parts, 'probe_absolute_path_required')
    for ancestor in reversed((path,) + tuple(path.parents)):
        try:
            metadata = ancestor.lstat()
        except FileNotFoundError:
            require(missing and ancestor == path, 'probe_path_missing')
            continue
        require(not stat.S_ISLNK(metadata.st_mode), 'probe_symlink_path_rejected')
        require(metadata.st_uid in (0, os.getuid()), 'probe_foreign_path_rejected')
        if ancestor != path or not file:
            require(stat.S_ISDIR(metadata.st_mode), 'probe_directory_required')
            require(not metadata.st_mode & 0o022 or metadata.st_mode & stat.S_ISVTX,
                    'probe_writable_ancestor_rejected')
    if file and path.exists():
        metadata = path.lstat()
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid(), 'probe_owned_file_required')
        if private:
            require(stat.S_IMODE(metadata.st_mode) == 0o600, 'probe_private_0600_file_required')
    return path


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf8')).hexdigest()


def roster(worker):
    codex = worker.get('codex')
    require(isinstance(codex, dict), 'probe_codex_config_required')
    candidates = worker.get('codex_accounts')
    if candidates is not None:
        require(isinstance(candidates, list) and candidates and all(isinstance(row, dict) for row in candidates),
                'probe_auth_roster_required')
        values = [row.get('auth_home', codex.get('auth_home')) for row in candidates]
    else:
        values = [codex.get('auth_home')] if codex.get('auth_home') else []
        if not codex.get('strict_auth_home', False):
            values += [str(Path.home() / '.codex-official'), str(Path.home() / '.codex')]
    require(values and all(isinstance(value, str) and Path(value).expanduser().is_absolute() for value in values),
            'probe_auth_roster_required')
    result = []
    for value in values:
        home = str(Path(value).expanduser())
        if home not in result:
            result.append(home)
    return result


def configuration_identity(operator, worker):
    node = worker.get('node_id')
    require(isinstance(node, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', node), 'probe_worker_node_required')
    required = ['agent', 'codex.native', 'mesh.node:' + node]
    require(set(required) <= set(worker.get('capabilities', [])), 'probe_worker_capabilities_missing')
    require(isinstance(worker['codex'].get('workspace'), str), 'probe_worker_workspace_required')
    # Paths/endpoint identities only: no credential files are read here.
    return _digest({'node': node, 'homes': roster(worker), 'workspace': worker['codex']['workspace'],
        'executable': worker['codex'].get('executable', 'codex'),
        'control_url': operator.get('control_url'), 'unix_socket': operator.get('unix_socket'),
        'operator_token_path': operator.get('token_file')})


def envelope(state):
    run, node = state['run_id'], state['node']
    scope = 'probe:worker-shell:' + node + ':' + run
    command = 'sha256sum -- ' + shlex.quote(state['challenge'])
    prompt = ('这是本人明确授权的常驻 Worker 原生 Shell 验收。使用原生终端执行这一条命令：' + command +
        '。文件内容和预期哈希没有提供给你。只读取这一份挑战文件；不要读其它文件、联网、写文件、'
        '安装、付费、拨号或创建/委派其它任务，不调用 Mesh 工具。最终只回复该命令实际输出里的 SHA256，'
        '不要猜测或自行生成校验值。这是本任务的工作范围，不是对原生能力的权限限制。')
    return {'id': _digest([FORMAT, node, run]), 'input': prompt,
        'required': ['agent', 'codex.native', 'mesh.node:' + node],
        'context': {'project_id': 'worker-shell-probe:' + run, 'agent_id': 'native-shell-probe:' + run,
                    'session_scope': scope, 'memory_scope': scope, 'source': 'owner-worker-shell-probe'}}


def load_state(path, operator, worker, create=False):
    selected = checked_path(path, file=True, private=True, missing=True)
    require(not any((parent / '.git').exists() for parent in selected.parents),
            'probe_state_must_be_outside_git_worktrees')
    require(stat.S_IMODE(selected.parent.stat().st_mode) == 0o700
            and selected.parent.stat().st_uid == os.getuid(), 'probe_state_private_parent_required')
    identity = configuration_identity(operator, worker)
    if not selected.exists():
        require(create, 'probe_state_missing_use_explicit_submit')
        directory = Path(tempfile.mkdtemp(prefix='mesh-worker-shell-probe.'))
        directory.chmod(0o700)
        challenge, body = directory / 'challenge.bin', secrets.token_bytes(64)
        descriptor = os.open(str(challenge), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        value = {'format': FORMAT, 'run_id': secrets.token_hex(16), 'node': worker['node_id'],
                 'configuration_identity': identity, 'challenge': str(challenge),
                 'challenge_sha256': hashlib.sha256(body).hexdigest()}
        value['task'] = envelope(value)
        with tempfile.TemporaryDirectory(prefix='.worker-probe-state-', dir=str(selected.parent)) as staging:
            draft = Path(staging) / 'state.json'
            descriptor = os.open(str(draft), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf8') as output:
                json.dump(value, output, sort_keys=True, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(str(draft), str(selected), follow_symlinks=False)
            except FileExistsError:
                # Adopt the winning immutable identity; do not submit a second
                # task. The losing private challenge remains diagnostic-only.
                pass
    checked_path(selected, file=True, private=True)
    require(selected.stat().st_size <= 16384, 'probe_state_oversized')
    value = private_json(selected)
    require(isinstance(value, dict) and set(value) == {'format', 'run_id', 'node', 'configuration_identity',
            'challenge', 'challenge_sha256', 'task'}, 'probe_state_invalid')
    require(value['format'] == FORMAT and value['node'] == worker['node_id']
            and value['configuration_identity'] == identity, 'probe_state_configuration_changed')
    require(isinstance(value['run_id'], str) and re.fullmatch(r'[0-9a-f]{32}', value['run_id']), 'probe_state_invalid')
    require(value['task'] == envelope(value), 'probe_immutable_task_changed')
    challenge = checked_path(value['challenge'], file=True, private=True)
    require(challenge.name == 'challenge.bin' and challenge.parent.name.startswith('mesh-worker-shell-probe.')
            and stat.S_IMODE(challenge.parent.stat().st_mode) == 0o700, 'probe_challenge_invalid')
    require(challenge.stat().st_size == 64 and hashlib.sha256(challenge.read_bytes()).hexdigest()
            == value['challenge_sha256'], 'probe_challenge_changed')
    require(value['challenge_sha256'] not in value['task']['input'], 'probe_expected_hash_disclosed')
    return value


def _uuid(value):
    try:
        require(isinstance(value, str) and str(uuid.UUID(value)) == value, 'probe_native_uuid_required')
    except (ValueError, AttributeError):
        raise ProbeError('probe_native_uuid_required') from None
    return value


def read_checkpoint(server, state, observed):
    database = checked_path(server['database'], file=True, private=True)
    require(any(peer.get('role') == 'worker' and peer.get('node') == state['node']
                for peer in server.get('peers', [])), 'probe_server_worker_binding_missing')
    with contextlib.closing(sqlite3.connect('file:' + quote(str(database), safe='/') + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT id,input,required,context,scope,status,node,epoch,result,checkpoint '
                         'FROM tasks WHERE id=?', (state['task']['id'],)).fetchone()
    require(row is not None, 'probe_selected_task_not_in_database')
    require(row['input'] == state['task']['input'] and json.loads(row['required']) == state['task']['required']
            and json.loads(row['context']) == state['task']['context'], 'probe_database_task_body_mismatch')
    require(all(row[key] == observed.get(key) for key in ('id', 'scope', 'status', 'node', 'epoch', 'result')),
            'probe_api_database_identity_mismatch')
    checkpoint = json.loads(row['checkpoint'])
    require(isinstance(checkpoint, dict), 'probe_checkpoint_invalid')
    return checkpoint


def _canonical_paths(home, metadata, thread):
    require(metadata.get('id') == thread, 'probe_rollout_thread_mismatch')
    timestamp = metadata.get('timestamp')
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)',
                         timestamp if isinstance(timestamp, str) else '')
    require(match is not None, 'probe_rollout_timestamp_invalid')
    zone = '+0000' if match.group(2) == 'Z' else match.group(2).replace(':', '')
    instant = datetime.datetime.strptime(match.group(1) + zone, '%Y-%m-%dT%H:%M:%S%z')
    # Native writer uses host-local time; selected-rollout restores use UTC.
    return {home / 'sessions' / moment.strftime('%Y/%m/%d') / (
        'rollout-' + moment.strftime('%Y-%m-%dT%H-%M-%S') + '-' + thread + '.jsonl')
        for moment in (instant.astimezone(), instant.astimezone(datetime.timezone.utc))}


def locate_rollout(worker, checkpoint, thread):
    homes = roster(worker)
    selected_home = checkpoint.get('codex_auth_home')
    if selected_home is not None:
        require(selected_home in homes, 'probe_auth_home_not_in_roster')
        homes = [selected_home]
    candidates = []
    for value in homes:
        home = Path(value)
        if not home.exists():
            continue
        checked_path(home)
        if checkpoint.get('native_rollout_path'):
            path = Path(checkpoint['native_rollout_path'])
            require(path.is_absolute(), 'probe_rollout_absolute_path_required')
            try:
                relative = path.relative_to(home / 'sessions')
            except ValueError:
                continue
            require(len(relative.parts) == 4 and path.name.endswith('-' + thread + '.jsonl'),
                    'probe_canonical_rollout_required')
            candidates.append((home, path))
        elif (home / 'sessions').exists():
            checked_path(home / 'sessions')
            candidates += [(home, path) for path in (home / 'sessions').glob(
                '????/??/??/rollout-*-' + thread + '.jsonl')]
        require(len(candidates) <= 1, 'probe_selected_rollout_ambiguous')
    require(len(candidates) == 1, 'probe_selected_rollout_missing')
    home, path = candidates[0]
    return home, checked_path(path, file=True, private=True)


def _command_evidence(arguments, command):
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except ValueError:
            decoded = None
    else:
        decoded = arguments
    if isinstance(decoded, dict):
        return decoded.get('cmd', decoded.get('command')) == command
    if not isinstance(arguments, str):
        return False
    # Code-mode is a native JS tool; require an actual exec_command call with
    # the exact requested literal, not an echoed command or another file.
    literal = r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'
    pattern = r'\btools\.exec_command\(\s*\{\s*(?:cmd|"cmd"|\'cmd\')\s*:\s*' + literal
    for match in re.finditer(pattern, arguments):
        encoded = match.group(1)
        try:
            value = json.loads(encoded) if encoded.startswith('"') else encoded[1:-1]
        except ValueError:
            continue
        if value == command:
            return True
    return False


def _output_evidence(value, state):
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        decoded = None
    if isinstance(decoded, dict) and 'exit_code' in decoded and decoded['exit_code'] != 0:
        return False
    # sha256sum prints both the digest and exact challenge filename; a final
    # model assertion containing the digest alone is not execution evidence.
    return re.search(re.escape(state['challenge_sha256']) + r'\s+' + re.escape(state['challenge']),
                     json.dumps(value, ensure_ascii=False)) is not None


def native_evidence(worker, state, observed, checkpoint=None):
    native = observed.get('native')
    require(isinstance(native, dict) and native.get('harness') == 'codex', 'probe_native_codex_required')
    thread, turn = _uuid(native.get('thread_id')), _uuid(native.get('turn_id'))
    checkpoint = checkpoint or {}
    if checkpoint:
        require(checkpoint.get('harness') == 'codex' and checkpoint.get('codex_node') == state['node']
                and checkpoint.get('thread_id') == thread and checkpoint.get('turn_id') == turn,
                'probe_checkpoint_native_identity_mismatch')
    home, path = locate_rollout(worker, checkpoint, thread)
    before = path.stat()
    version = lambda metadata: (metadata.st_dev, metadata.st_ino, metadata.st_size,
                                metadata.st_mtime_ns, metadata.st_ctime_ns)
    calls, outputs, starts, completed, active, metadata = {}, {}, 0, False, False, None
    command = 'sha256sum -- ' + shlex.quote(state['challenge'])
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        require(version(os.fstat(source.fileno())) == version(before), 'probe_rollout_changed')
        number = 0
        while True:
            line = source.readline(MAX_LINE + 1)
            if not line:
                break
            require(len(line) <= MAX_LINE and line.endswith(b'\n'), 'probe_rollout_incomplete_or_oversized')
            record = json.loads(line.decode('utf8'))
            require(isinstance(record, dict), 'probe_rollout_record_invalid')
            kind, payload = record.get('type'), record.get('payload')
            number += 1
            if number == 1:
                require(kind == 'session_meta' and isinstance(payload, dict), 'probe_session_metadata_required')
                require(path in _canonical_paths(home, payload, thread), 'probe_canonical_rollout_required')
                require(payload.get('cwd') == worker['codex']['workspace'], 'probe_native_workspace_mismatch')
                metadata = payload
                continue
            event = payload.get('type') if isinstance(payload, dict) else None
            if kind == 'event_msg' and event == 'task_started':
                if payload.get('turn_id') == turn:
                    require(starts == 0, 'probe_duplicate_selected_turn')
                    starts, active = 1, True
                elif starts:
                    raise ProbeError('probe_selected_turn_not_latest')
                continue
            if not active:
                continue
            if isinstance(payload, dict):
                require(payload.get('turn_id', turn) == turn and payload.get('thread_id', thread) == thread,
                        'probe_selected_turn_identity_mismatch')
            if kind == 'event_msg' and event == 'task_complete':
                require(not completed and payload.get('turn_id') == turn and not payload.get('error'),
                        'probe_native_completion_invalid')
                require((payload.get('last_agent_message') or '').strip() == state['challenge_sha256'],
                        'probe_native_final_reply_mismatch')
                completed = True
            elif kind == 'response_item' and isinstance(payload, dict):
                require(not completed, 'probe_response_after_native_completion')
                call_id = payload.get('call_id')
                if payload.get('type') in ('function_call', 'custom_tool_call') and isinstance(call_id, str):
                    name = str(payload.get('name', '')).lower()
                    arguments = payload.get('arguments', payload.get('input', ''))
                    if ('mesh' not in name and re.search(r'(^|[._/])(exec|exec_command|shell|shell_command|terminal)([._/]|$)', name)
                            and _command_evidence(arguments, command)):
                        require(call_id not in calls, 'probe_duplicate_native_call_id')
                        calls[call_id] = True
                elif payload.get('type') in ('function_call_output', 'custom_tool_call_output') and call_id in calls:
                    require(call_id not in outputs, 'probe_duplicate_native_call_output')
                    outputs[call_id] = _output_evidence(payload.get('output', ''), state)
        require(version(os.fstat(source.fileno())) == version(before), 'probe_rollout_changed')
    require(version(path.stat()) == version(before), 'probe_rollout_changed')
    require(starts == 1 and completed and metadata is not None, 'probe_selected_turn_incomplete')
    version_text = metadata.get('cli_version')
    provider = metadata.get('model_provider')
    return {'thread_id': thread, 'turn_id': turn, 'harness': 'codex', 'same_node_verified': True,
        'auth_roster_verified': True, 'canonical_rollout_verified': True,
        'checkpoint_auth_binding_verified': bool(checkpoint),
        'authorized_profile_index': roster(worker).index(str(home)),
        'native_cli_version': version_text if isinstance(version_text, str) and re.fullmatch(r'[0-9A-Za-z.+_-]{1,64}', version_text) else None,
        'native_model_provider': provider if isinstance(provider, str) and re.fullmatch(r'[0-9A-Za-z._-]{1,64}', provider) else None,
        'configured_executable_sha256': hashlib.sha256(str(worker['codex'].get('executable', 'codex')).encode('utf8')).hexdigest(),
        'configured_runtime_identity_only': True,
        'runtime_process_executable_verified': False, 'systemd_unit_verified': False,
        'shell_challenge_calls': len(calls), 'challenge_seen_in_native_output': any(outputs.values())}


class Probe:
    def __init__(self, client, worker, server=None):
        self.client, self.worker, self.server = client, worker, server

    def submit(self, state):
        response = self.client.request('/v1/tasks', state['task'])
        require(isinstance(response, dict) and response.get('id') == state['task']['id'], 'probe_submit_identity_mismatch')
        return {'task_id': response['id'], 'same_immutable_task_submitted': True,
                'completion_not_assumed': True}

    def read(self, state):
        response = self.client.request('/v1/task/status', {'id': state['task']['id']})
        require(isinstance(response, dict) and response.get('id') == state['task']['id']
                and response.get('scope') == state['task']['context']['session_scope']
                and response.get('status') in STATUSES, 'probe_task_identity_mismatch')
        require(response.get('node') in (None, state['node']), 'probe_task_executed_on_wrong_node')
        require(isinstance(response.get('epoch'), int) and not isinstance(response['epoch'], bool)
                and response['epoch'] >= 0, 'probe_task_epoch_invalid')
        return response

    def poll(self, state, timeout=30, interval=1, clock=time.monotonic, sleep=time.sleep):
        expires = clock() + timeout
        while True:
            observed = self.read(state)
            report = {'task_id': observed['id'], 'status': observed['status'], 'worker_node': observed.get('node'),
                'epoch': observed.get('epoch'), 'session_scope': observed['scope'],
                'execution_verified': False, 'systemd_unit_verified': False,
                'requested_read_only': True, 'runtime_read_only_enforced': False,
                'native_tools_intercepted': False, 'task_not_restarted_by_probe': True}
            if observed['status'] in TERMINAL:
                report['terminal_observed'] = True
                if observed['status'] != 'completed':
                    return report, 3
                require(observed.get('node') == state['node'], 'probe_completed_worker_identity_missing')
                require(isinstance(observed.get('result'), str), 'probe_final_reply_missing')
                report['reply_matches'] = observed['result'].strip() == state['challenge_sha256']
                checkpoint = (read_checkpoint(self.server, state, observed) if self.server else observed.get('checkpoint'))
                report['native'] = native_evidence(self.worker, state, observed, checkpoint)
                report['execution_verified'] = report['reply_matches'] and report['native']['challenge_seen_in_native_output']
                return report, 0 if report['execution_verified'] else 3
            remaining = expires - clock()
            if remaining <= 0:
                report.update(observation_timeout=True, retry_with_same_state=True, terminal_observed=False)
                return report, 2
            sleep(min(interval, remaining))


def error_code(error):
    if isinstance(error, ProbeError):
        return str(error)
    if isinstance(error, urllib.error.HTTPError):
        if error.fp is not None:
            error.close()
        return {401: 'probe_authentication_rejected', 403: 'probe_route_not_authorized',
                400: 'probe_task_observation_or_input_unavailable', 409: 'probe_task_identity_conflict'}.get(error.code, 'probe_http_failed')
    if isinstance(error, urllib.error.URLError):
        return 'probe_transport_failed_outcome_unknown'
    if isinstance(error, sqlite3.Error):
        return 'probe_selected_checkpoint_database_unavailable'
    return 'probe_private_configuration_or_evidence_invalid'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operator-config', required=True)
    parser.add_argument('--worker-config', required=True)
    parser.add_argument('--server-config', help='optional private local authority config, read-only selected-task DB evidence')
    parser.add_argument('--state', required=True, help='immutable 0600 state in an existing 0700 directory outside git')
    parser.add_argument('--submit', action='store_true', help='explicitly submit same immutable ID/body to existing Worker')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--interval', type=float, default=1)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 300 or not 0.1 <= args.interval <= 10:
        parser.error('timeout must be 0..300 seconds; interval must be 0.1..10 seconds')
    output = {'probe': FORMAT, 'new_paid_api_provisioned': False, 'explicit_submission': args.submit,
              'no_new_task_id_on_retry': True, 'global_leader_session_used': False}
    try:
        operator = private_json(checked_path(args.operator_config, file=True, private=True))
        worker = private_json(checked_path(args.worker_config, file=True, private=True))
        server = private_json(checked_path(args.server_config, file=True, private=True)) if args.server_config else None
        state = load_state(args.state, operator, worker, create=args.submit)
        output['task_id'] = state['task']['id']
        probe = Probe(Client(operator), worker, server)
        if args.submit:
            output['submission'] = probe.submit(state)
        output['observation'], code = probe.poll(state, args.timeout, args.interval)
    except Exception as error:
        output.update(error=error_code(error), retry_with_same_state=True, execution_verified=False,
                      task_not_restarted_by_probe=True)
        code = 1
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
