"""Isolated, owner-run Leader continuity acceptance; never a service controller.

Official resume semantics: https://learn.chatgpt.com/docs/app-server#start-or-resume-a-thread
The host must independently launch/stop the two proof Workers and wait for real
authority leases. This helper never starts Codex, changes deadlines, copies auth,
replays a failed turn, or touches the production Leader/iLink receiver.

Minimal workflow (ROOT already exists, owned 0700, outside git under /tmp):
  python -m scripts.probe_leader_continuity prepare --root ROOT --port 17682
  # Copy the small bundle, preserving its exact absolute /tmp ROOT, to cloud.
  # On EACH host, use that host's existing authorized Worker configuration:
  python -m scripts.probe_leader_continuity configure-worker --root ROOT \
    --phase cloud --source-worker-config /PRIVATE/current-worker.json
  # Operator launches cloud authority without ilink_account and cloud Worker.
  python -m scripts.probe_leader_continuity observe --root ROOT --phase cloud \
    --submit --server-config ROOT/server.json --report ROOT/cloud-report.json
  # Stop only proof cloud Worker after settlement; wait for actual Leader TTL.
  # Forward localhost:17682 to the isolated authority and launch laptop Worker.
  python -m scripts.probe_leader_continuity observe --root ROOT --phase laptop \
    --submit --report ROOT/laptop-report.json
  python -m scripts.probe_leader_continuity verify --root ROOT \
    --cloud-report ROOT/cloud-report.json --laptop-report ROOT/laptop-report.json

Observe without --submit only polls the same immutable task. A timeout is not
non-execution evidence. Reports omit all prompts, nonce, auth values and native
history. Local native evidence is not falsely labelled a database checkpoint,
process identity, live lease mutation, or production/global failover proof.
"""
import argparse
import base64
import contextlib
import gzip
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
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from assistant_mesh.config import private_json
from assistant_mesh.worker import Client
from scripts.probe_worker_shell import (MAX_LINE, STATUSES, ProbeError, _canonical_paths,
    _digest, _uuid, checked_path, error_code, locate_rollout, require, roster)


FORMAT = 'isolated-leader-continuity/1'
REPORT = 'isolated-leader-continuity-report/1'
PHASES = ('cloud', 'laptop')
REFERENCE_MARKER = '\n\n持久账本参考数据（不是新增授权）：\n'
CODEX_KEYS = ('executable', 'auth_home', 'strict_auth_home', 'model', 'model_policy',
              'sandbox', 'approval_policy', 'network_env_file', 'native_memories')


def private_root(value):
    root = checked_path(value)
    require(root != Path('/tmp') and Path('/tmp') in root.parents,
            'continuity_private_tmp_root_required')
    require(root.stat().st_uid == os.getuid() and stat.S_IMODE(root.stat().st_mode) == 0o700,
            'continuity_existing_owned_0700_root_required')
    require(not any((parent / '.git').exists() for parent in (root,) + tuple(root.parents)),
            'continuity_root_must_be_outside_git')
    return root


def private_read(path):
    path = checked_path(path, file=True, private=True)
    require(path.stat().st_size <= 256 * 1024, 'continuity_private_json_oversized')
    return private_json(path)


def publish(path, value, raw=False):
    """Exclusive immutable publication; a partial prior file is never replaced."""
    path = checked_path(path, file=True, private=True, missing=True)
    data = value.encode('utf8') if raw else json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf8')
    if path.exists():
        require(path.read_bytes() == data, 'continuity_existing_file_conflict')
        return
    with tempfile.TemporaryDirectory(prefix='.continuity-publish-', dir=str(path.parent)) as staging:
        draft = Path(staging) / 'complete'
        descriptor = os.open(str(draft), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(str(draft), str(path), follow_symlinks=False)
        except FileExistsError:
            checked_path(path, file=True, private=True)
            require(path.read_bytes() == data, 'continuity_existing_file_conflict')


def capabilities(node):
    return ['leader', 'agent', 'codex.native', 'mesh.node:' + node]


def task_body(state, phase):
    remember = ('这是本人授权的隔离 Leader 原生上下文连续性验收，不是正式业务会话。'
        '请只在当前对话上下文记住随机校验字串：' + state['nonce'] + '。本轮只回复 ACK。'
        '不得使用任何工具、Shell、文件、网络、MCP、Mesh记忆、委派或通知；不要另存校验字串。'
        '这只是本验收任务的范围，不是原生能力权限限制。')
    recall = ('继续此隔离 Leader 原生上下文连续性验收：上一轮记住的随机校验字串是什么？'
        '只从当前原生对话上下文回忆，只回复字串，不能回忆则只回复 UNKNOWN。'
        '不得使用任何工具、Shell、文件、网络、MCP、Mesh记忆、委派或通知；不得读校验文件。'
        '这只是本验收任务的范围，不是原生能力权限限制。')
    return {'id': _digest([FORMAT, state['run_id'], phase]),
        'input': remember if phase == 'cloud' else recall,
        'required': capabilities(state['nodes'][phase]),
        'context': {'project_id': 'leader-continuity:' + state['run_id'],
            'agent_id': 'continuity:' + state['run_id'], 'session_scope': state['scope'],
            'memory_scope': 'probe:' + state['scope'], 'source': 'owner-isolated-leader-continuity'}}


def server_body(state):
    root = Path(state['root'])
    return {'node_id': state['authority'], 'database': str(root / 'ledger.sqlite'),
        'port': state['port'], 'peers': [
            {'role': 'operator', 'token_file': str(root / 'operator.token')}] + [
            {'role': 'worker', 'node': state['nodes'][phase],
             'token_file': str(root / (phase + '.token')),
             'capabilities': capabilities(state['nodes'][phase])} for phase in PHASES]}


def load_state(root):
    root = private_root(root)
    state = private_read(root / 'state.json')
    require(isinstance(state, dict) and set(state) == {
        'format', 'root', 'run_id', 'authority', 'nodes', 'port', 'nonce', 'scope', 'tasks'},
        'continuity_state_invalid')
    run = state.get('run_id')
    require(state['format'] == FORMAT and state['root'] == str(root)
        and isinstance(run, str) and re.fullmatch(r'[0-9a-f]{32}', run), 'continuity_state_identity_changed')
    require(state['nodes'] == {phase: 'continuity-' + phase + '-' + run for phase in PHASES}
        and state['authority'] == 'continuity-authority-' + run
        and state['scope'] == 'leader:continuity:' + run, 'continuity_isolation_changed')
    require(isinstance(state['port'], int) and not isinstance(state['port'], bool)
        and 1024 <= state['port'] <= 65535, 'continuity_port_invalid')
    require(isinstance(state['nonce'], str) and re.fullmatch(r'LEADER_CONTINUITY_[0-9a-f]{32}', state['nonce']),
        'continuity_nonce_invalid')
    require(state['tasks'] == {phase: task_body(state, phase) for phase in PHASES},
        'continuity_immutable_task_changed')
    require(state['nonce'] not in json.dumps(state['tasks']['laptop'], ensure_ascii=False),
        'continuity_recall_nonce_disclosed')
    return state


def prepare(root, port=17682):
    root = private_root(root)
    if (root / 'state.json').exists():
        state = load_state(root)
        require(state['port'] == port, 'continuity_port_changed')
        require(private_read(root / 'server.json') == server_body(state), 'continuity_server_changed')
        return state
    require(not list(root.iterdir()), 'continuity_prepare_requires_empty_root')
    require(isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535,
            'continuity_port_invalid')
    run = secrets.token_hex(16)
    state = {'format': FORMAT, 'root': str(root), 'run_id': run,
        'authority': 'continuity-authority-' + run,
        'nodes': {phase: 'continuity-' + phase + '-' + run for phase in PHASES},
        'port': port, 'nonce': 'LEADER_CONTINUITY_' + secrets.token_hex(16),
        'scope': 'leader:continuity:' + run}
    state['tasks'] = {phase: task_body(state, phase) for phase in PHASES}
    for name in ('server', 'operator', 'cloud', 'laptop'):
        # server.token is reserved for isolated authority identification, not
        # an inbound grant; only explicitly listed peers authenticate API calls.
        publish(root / (name + '.token'), secrets.token_hex(32), raw=True)
    publish(root / 'server.json', server_body(state))
    for name in ('operator', 'cloud', 'laptop'):
        publish(root / (name + '-client.json'), {'control_url': 'http://127.0.0.1:' + str(port),
                                               'token_file': str(root / (name + '.token'))})
    publish(root / 'state.json', state)
    return load_state(root)


def configure_worker(state, phase, source_path):
    require(phase in PHASES, 'continuity_phase_invalid')
    root = private_root(state['root'])
    source = private_read(source_path)
    require(isinstance(source, dict) and isinstance(source.get('codex'), dict),
            'continuity_source_codex_required')
    codex = {key: source['codex'][key] for key in CODEX_KEYS if key in source['codex']}
    homes = roster(source)
    for value in homes:
        home = checked_path(value)
        require(home.stat().st_uid == os.getuid(), 'continuity_auth_home_not_owned')
        # Metadata only: neither auth values nor config/session directories are
        # copied or opened. Actual authentication/model access is not asserted.
        checked_path(home / 'auth.json', file=True, private=True)
    if source.get('codex_accounts'):
        accounts = [{key: row[key] for key in CODEX_KEYS if key in row}
                    for row in source['codex_accounts']]
        for row in accounts:
            home = str(Path(row.get('auth_home', source['codex'].get('auth_home'))).expanduser())
            require(home in homes, 'continuity_account_roster_changed')
            row.update(auth_home=home, strict_auth_home=True, native_memories=False)
    else:
        accounts = [{'auth_home': home, 'strict_auth_home': True, 'native_memories': False} for home in homes]
    workspace = root / 'workspace'
    if not workspace.exists():
        workspace.mkdir(mode=0o700)
    checked_path(workspace)
    require(workspace.stat().st_uid == os.getuid() and stat.S_IMODE(workspace.stat().st_mode) == 0o700,
            'continuity_workspace_requires_0700')
    codex.update(workspace=str(workspace), auth_home=homes[0], strict_auth_home=True, native_memories=False)
    worker = {'node_id': state['nodes'][phase], 'capabilities': capabilities(state['nodes'][phase]),
        'control_url': 'http://127.0.0.1:' + str(state['port']),
        'token_file': str(root / (phase + '.token')), 'codex': codex, 'codex_accounts': accounts}
    publish(root / (phase + '-worker.json'), worker)
    return worker


def validate_worker(state, phase, worker):
    require(phase in PHASES and worker.get('node_id') == state['nodes'][phase]
        and worker.get('capabilities') == capabilities(state['nodes'][phase]), 'continuity_worker_binding_changed')
    require(worker.get('control_url') == 'http://127.0.0.1:' + str(state['port'])
        and worker.get('token_file') == str(Path(state['root']) / (phase + '.token')),
        'continuity_worker_authority_changed')
    require(worker.get('codex', {}).get('workspace') == str(Path(state['root']) / 'workspace')
        and worker['codex'].get('native_memories') is False
        and all(row.get('native_memories') is False for row in worker.get('codex_accounts', [])),
        'continuity_worker_context_isolation_changed')
    roster(worker)


def checkpoint_evidence(server, state, phase, observed, leader, native_sha256):
    require(server == server_body(state), 'continuity_isolated_server_changed')
    database = checked_path(server['database'], file=True, private=True)
    with contextlib.closing(sqlite3.connect('file:' + quote(str(database), safe='/') + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT id,input,required,context,scope,status,node,epoch,leader_epoch,result,checkpoint '
                         'FROM tasks WHERE id=?', (state['tasks'][phase]['id'],)).fetchone()
        require(row is not None, 'continuity_selected_database_task_missing')
        require(all(row[key] == observed.get(key) for key in ('id', 'scope', 'status', 'node', 'epoch', 'leader_epoch', 'result')),
                'continuity_api_database_task_mismatch')
        body = state['tasks'][phase]
        require(row['input'] == body['input'] and json.loads(row['required']) == body['required']
            and json.loads(row['context']) == body['context'], 'continuity_database_task_body_changed')
        current = dict(db.execute('SELECT * FROM leader').fetchone())
        require(current == leader, 'continuity_api_database_leader_changed')
        checkpoint = json.loads(row['checkpoint'])
        require(isinstance(checkpoint, dict) and checkpoint.get('side_effect_started') is False,
                'continuity_task_not_settled')
        artifact = hashlib.sha256(json.dumps([observed['id'], observed['epoch'], 'codex'],
                                             separators=(',', ':')).encode()).hexdigest()
        with tempfile.TemporaryFile() as packed:
            count = 0
            for part in db.execute('SELECT part,body FROM session_chunks WHERE id=? ORDER BY part', (artifact,)):
                require(part['part'] == count, 'continuity_session_artifact_incomplete')
                packed.write(part['body'])
                count += 1
            require(count > 0, 'continuity_complete_artifact_missing')
            packed.seek(0)
            digest = hashlib.sha256()
            with gzip.GzipFile(fileobj=packed, mode='rb') as archive:
                for block in iter(lambda: archive.read(65536), b''):
                    digest.update(block)
            require(digest.hexdigest() == native_sha256, 'continuity_complete_artifact_mismatch')
    return checkpoint


def _message_text(payload):
    content = payload.get('content')
    require(isinstance(content, list), 'continuity_native_message_invalid')
    require(all(isinstance(part, dict) and part.get('type') in ('input_text', 'output_text', 'text')
                and isinstance(part.get('text'), str) for part in content), 'continuity_native_message_invalid')
    return '\n'.join(part['text'] for part in content)


def _input_evidence(text, state, phase):
    require(isinstance(text, str) and text.startswith(state['tasks'][phase]['input'] + REFERENCE_MARKER),
            'continuity_native_input_changed')
    raw = text[len(state['tasks'][phase]['input'] + REFERENCE_MARKER):]
    reference = json.loads(raw)
    require(isinstance(reference, dict) and set(reference) == {'context', 'memories', 'children', 'resource_access'}
        and reference['context'] == state['tasks'][phase]['context']
        and reference['memories'] == [] and reference['children'] == [], 'continuity_reference_not_context_only')
    require(state['nonce'] not in raw, 'continuity_reference_nonce_disclosed')
    if phase == 'laptop':
        require(state['nonce'] not in text, 'continuity_recall_nonce_disclosed')


def _environment_evidence(text, worker, state):
    """Recognize native metadata, never a second task or instruction envelope.

    This validates the observed history; it does not change native permissions.
    Unknown future shapes fail closed instead of granting a broad user-message
    exemption. DTDs, processing instructions, mixed text and unknown fields are
    not part of the currently supported native metadata shape.
    """
    code = 'continuity_native_environment_invalid'
    require(isinstance(text, str) and len(text) <= 65536
        and '<!' not in text and '<?' not in text and state['nonce'] not in text, code)
    try:
        envelope = ET.fromstring(text.strip())
    except ET.ParseError:
        raise ProbeError(code) from None
    require(envelope.tag == 'environment_context' and not envelope.attrib, code)
    require(state['nonce'] not in ''.join(envelope.itertext())
        and all(state['nonce'] not in value for element in envelope.iter() for value in element.attrib.values()),
        'continuity_native_environment_nonce_disclosed')

    def children(element, allowed):
        require(not (element.text or '').strip()
            and all(not (child.tail or '').strip() for child in element), code)
        fields = [child.tag for child in element]
        require(len(fields) == len(set(fields)) and set(fields) <= set(allowed), code)
        return {child.tag: child for child in element}

    def leaf(element):
        require(not element.attrib and not list(element), code)
        return (element.text or '').strip()

    def absolute_path(element):
        value = leaf(element)
        require(value and Path(value).is_absolute() and '..' not in Path(value).parts
            and not any(ord(character) < 32 for character in value), code)
        return value

    fields = children(envelope, ('cwd', 'shell', 'current_date', 'timezone', 'filesystem', 'subagents'))
    require('cwd' in fields and absolute_path(fields['cwd']) == worker['codex']['workspace'],
            'continuity_native_environment_workspace_changed')
    if 'shell' in fields:
        require(re.fullmatch(r'(?:/[A-Za-z0-9_.+-]+)+|[A-Za-z0-9_.+-]{1,64}', leaf(fields['shell'])), code)
    if 'current_date' in fields:
        require(re.fullmatch(r'\d{4}-\d\d-\d\d', leaf(fields['current_date'])), code)
    if 'timezone' in fields:
        require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*|[+-]\d\d:\d\d',
                             leaf(fields['timezone'])), code)
    if 'filesystem' in fields:
        filesystem = fields['filesystem']
        require(not filesystem.attrib, code)
        parts = children(filesystem, ('workspace_roots', 'permission_profile'))
        require(set(parts) == {'workspace_roots', 'permission_profile'}, code)
        roots = parts['workspace_roots']
        require(not roots.attrib and not (roots.text or '').strip()
            and all(root.tag == 'root' and not (root.tail or '').strip() for root in roots), code)
        for root in roots:
            absolute_path(root)
        profile = parts['permission_profile']
        require(set(profile.attrib) == {'type'} and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', profile.attrib['type']), code)
        permissions = children(profile, ('file_system',))
        require(set(permissions) == {'file_system'}, code)
        file_system = permissions['file_system']
        require(set(file_system.attrib) == {'type'}
            and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', file_system.attrib['type'])
            and not list(file_system) and not (file_system.text or '').strip(), code)
    if 'subagents' in fields:
        agents = fields['subagents']
        require(not agents.attrib and not (agents.text or '').strip(), code)
        for agent in agents:
            require(agent.tag == 'agent' and set(agent.attrib) == {'name'}
                and re.fullmatch(r'[A-Za-z0-9_./:-]{1,256}', agent.attrib['name'])
                and not list(agent) and not (agent.text or '').strip() and not (agent.tail or '').strip(), code)


def native_evidence(worker, state, phase, observed, checkpoint=None):
    native = observed.get('native')
    require(isinstance(native, dict) and native.get('harness') == 'codex', 'continuity_native_codex_required')
    thread, turn = _uuid(native.get('thread_id')), _uuid(native.get('turn_id'))
    checkpoint = checkpoint or {}
    if checkpoint:
        require(checkpoint.get('thread_id') == thread and checkpoint.get('turn_id') == turn
            and checkpoint.get('codex_node') == state['nodes'][phase]
            and checkpoint.get('harness') == 'codex', 'continuity_checkpoint_native_mismatch')
    home, path = locate_rollout(worker, checkpoint, thread)
    before = path.stat()
    version = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    digest, active, started, complete, input_seen, reply_seen, records = hashlib.sha256(), False, 0, False, False, False, 0
    environment_seen = False

    def user_message(text):
        nonlocal input_seen, environment_seen
        if isinstance(text, str) and text.lstrip().startswith('<environment_context'):
            require(not input_seen and not environment_seen, 'continuity_native_environment_order_invalid')
            _environment_evidence(text, worker, state)
            environment_seen = True
        else:
            _input_evidence(text, state, phase)
            input_seen = True

    expected = 'ACK' if phase == 'cloud' else state['nonce']
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        require(version(os.fstat(source.fileno())) == version(before), 'continuity_rollout_changed')
        while True:
            line = source.readline(MAX_LINE + 1)
            if not line:
                break
            require(len(line) <= MAX_LINE and line.endswith(b'\n'), 'continuity_rollout_incomplete_or_oversized')
            digest.update(line)
            value = json.loads(line.decode('utf8'))
            require(isinstance(value, dict), 'continuity_native_record_invalid')
            kind, payload = value.get('type'), value.get('payload')
            records += 1
            if records == 1:
                require(kind == 'session_meta' and isinstance(payload, dict)
                    and path in _canonical_paths(home, payload, thread), 'continuity_canonical_rollout_required')
                require(payload.get('cwd') == worker['codex']['workspace'], 'continuity_native_workspace_changed')
                continue
            event = payload.get('type') if isinstance(payload, dict) else None
            if kind == 'event_msg' and event == 'task_started':
                if payload.get('turn_id') == turn:
                    require(started == 0, 'continuity_duplicate_selected_turn')
                    started, active = 1, True
                elif started:
                    raise ProbeError('continuity_selected_turn_not_latest')
                continue
            if not active:
                continue  # Older native history remains opaque, not rewritten.
            require(isinstance(payload, dict) and payload.get('turn_id', turn) == turn
                and payload.get('thread_id', thread) == thread, 'continuity_native_turn_identity_changed')
            if kind == 'response_item':
                require(not complete, 'continuity_response_after_completion')
                item = payload.get('type')
                require(item in ('message', 'reasoning'), 'continuity_tool_or_unknown_item_observed')
                if item == 'message':
                    role = payload.get('role')
                    require(role in ('user', 'assistant', 'developer', 'system'), 'continuity_native_message_invalid')
                    if role == 'user':
                        user_message(_message_text(payload))
                    elif role in ('developer', 'system'):
                        require(state['nonce'] not in _message_text(payload),
                                'continuity_native_instruction_nonce_disclosed')
                    elif role == 'assistant' and _message_text(payload).strip() == expected:
                        reply_seen = True
            elif kind == 'event_msg':
                if event == 'task_complete':
                    require(not complete and payload.get('turn_id') == turn and not payload.get('error')
                        and (payload.get('last_agent_message') or '').strip() == expected,
                        'continuity_native_completion_mismatch')
                    complete = True
                elif event == 'user_message':
                    require(not complete, 'continuity_response_after_completion')
                    user_message(payload.get('message'))
                elif event == 'agent_message':
                    require(not complete, 'continuity_response_after_completion')
                    reply_seen = reply_seen or (payload.get('message', payload.get('text', '')).strip() == expected)
                elif event in ('item_started', 'item_completed'):
                    item = payload.get('item')
                    require(not complete and isinstance(item, dict) and item.get('type') in (
                        'UserMessage', 'AgentMessage', 'Reasoning', 'userMessage', 'agentMessage', 'reasoning'),
                        'continuity_tool_or_unknown_event_observed')
                else:
                    require(event in ('token_count', 'agent_reasoning', 'agent_reasoning_raw_content'),
                            'continuity_tool_or_unknown_event_observed')
            else:
                require(not complete and kind in ('turn_context', 'world_state', 'token_usage_record'),
                        'continuity_tool_or_unknown_record_observed')
        require(version(os.fstat(source.fileno())) == version(before), 'continuity_rollout_changed')
    require(version(path.stat()) == version(before), 'continuity_rollout_changed')
    require(started == 1 and complete and input_seen and reply_seen, 'continuity_native_evidence_incomplete')
    return {'thread_id': thread, 'turn_id': turn, 'raw_history_sha256': digest.hexdigest(),
        'raw_history_bytes': before.st_size, 'native_tool_calls': 0, 'native_turn_completed': True,
        'native_input_and_reference_verified': True, 'native_reply_matches': True,
        'native_environment_envelope_verified': environment_seen,
        'selected_turn_instruction_nonce_absence_verified': True,
        'canonical_rollout_verified': True, 'authorized_profile_index': roster(worker).index(str(home)),
        'selected_auth_home_sha256': hashlib.sha256(str(home).encode()).hexdigest(),
        'checkpoint_auth_binding_verified': bool(checkpoint),
        'runtime_process_executable_verified': False, 'systemd_unit_verified': False}


class Probe:
    def __init__(self, client, state, phase, worker, server=None):
        validate_worker(state, phase, worker)
        self.client, self.state, self.phase, self.worker, self.server = client, state, phase, worker, server

    def read(self, phase=None):
        phase = phase or self.phase
        response = self.client.request('/v1/task/status', {'id': self.state['tasks'][phase]['id']})
        require(isinstance(response, dict) and response.get('id') == self.state['tasks'][phase]['id']
            and response.get('scope') == self.state['scope'] and response.get('status') in STATUSES
            and response.get('node') in (None, self.state['nodes'][phase]), 'continuity_api_task_identity_mismatch')
        require(isinstance(response.get('epoch'), int) and not isinstance(response['epoch'], bool)
            and response['epoch'] >= 0, 'continuity_api_task_epoch_invalid')
        if response['status'] == 'completed':
            require(response['epoch'] > 0 and response.get('node') == self.state['nodes'][phase]
                and isinstance(response.get('leader_epoch'), int) and not isinstance(response['leader_epoch'], bool)
                and response['leader_epoch'] > 0, 'continuity_api_leader_term_missing')
        return response

    def submit(self):
        if self.phase == 'laptop':
            previous = self.read('cloud')
            require(previous['status'] == 'completed' and (previous.get('result') or '').strip() == 'ACK',
                    'continuity_cloud_phase_not_settled')
        response = self.client.request('/v1/tasks', self.state['tasks'][self.phase])
        require(isinstance(response, dict) and response.get('id') == self.state['tasks'][self.phase]['id'],
                'continuity_submit_identity_mismatch')
        return {'same_immutable_task_submitted': True, 'completion_not_assumed': True}

    def observe(self, timeout=30, interval=1, clock=time.monotonic, sleep=time.sleep):
        expires = clock() + timeout
        while True:
            observed = self.read()
            report = {'format': REPORT, 'run_id': self.state['run_id'], 'state_sha256': _digest(self.state),
                'phase': self.phase, 'task_id': observed['id'], 'scope': observed['scope'],
                'node': observed.get('node'), 'task_epoch': observed['epoch'],
                'leader_epoch': observed.get('leader_epoch'), 'status': observed['status'],
                'continuity_verified': False, 'production_leader_scope_used': False,
                'checkpoint_verified': False, 'complete_artifact_verified': False,
                'task_not_restarted_by_probe': True, 'native_tools_intercepted': False}
            if observed['status'] in ('completed', 'failed', 'needs_review'):
                if observed['status'] != 'completed':
                    return report, 3
                expected = 'ACK' if self.phase == 'cloud' else self.state['nonce']
                require(isinstance(observed.get('result'), str) and observed['result'].strip() == expected,
                        'continuity_api_final_reply_mismatch')
                leader = self.client.request('/v1/status').get('leader')
                require(isinstance(leader, dict) and isinstance(leader.get('epoch'), int)
                    and not isinstance(leader['epoch'], bool) and leader['epoch'] >= observed['leader_epoch'],
                    'continuity_authority_leader_term_invalid')
                checkpoint = None
                native = native_evidence(self.worker, self.state, self.phase, observed)
                if self.server:
                    checkpoint = checkpoint_evidence(self.server, self.state, self.phase, observed, leader,
                                                     native['raw_history_sha256'])
                    native = native_evidence(self.worker, self.state, self.phase, observed, checkpoint)
                    report.update(checkpoint_verified=True, complete_artifact_verified=True)
                if self.phase == 'laptop':
                    previous = self.read('cloud')
                    prior_native = previous.get('native')
                    require(previous['status'] == 'completed' and isinstance(previous.get('result'), str)
                        and previous['result'].strip() == 'ACK'
                        and isinstance(prior_native, dict) and prior_native.get('harness') == 'codex'
                        and prior_native.get('thread_id') == native['thread_id']
                        and _uuid(prior_native.get('turn_id')) != native['turn_id']
                        and previous['leader_epoch'] < observed['leader_epoch'],
                        'continuity_native_thread_or_term_not_continued')
                report.update(continuity_verified=True, api_reply_matches=True, native=native,
                    host_identity_sha256=hashlib.sha256(os.uname().nodename.encode()).hexdigest(),
                    host_identity_source='local_os_hostname', physical_host_identity_independently_verified=False)
                return report, 0
            remaining = expires - clock()
            if remaining <= 0:
                report.update(observation_timeout=True, retry_with_same_state=True)
                return report, 2
            sleep(min(interval, remaining))


def verify_reports(state, cloud, laptop):
    for phase, report in (('cloud', cloud), ('laptop', laptop)):
        require(isinstance(report, dict) and report.get('format') == REPORT
            and report.get('run_id') == state['run_id'] and report.get('state_sha256') == _digest(state)
            and report.get('phase') == phase and report.get('task_id') == state['tasks'][phase]['id']
            and report.get('scope') == state['scope'] and report.get('node') == state['nodes'][phase]
            and report.get('status') == 'completed' and report.get('continuity_verified') is True,
            'continuity_phase_report_invalid')
        native = report.get('native', {})
        _uuid(native.get('thread_id'))
        _uuid(native.get('turn_id'))
        require(isinstance(native.get('native_tool_calls'), int)
            and not isinstance(native['native_tool_calls'], bool) and native['native_tool_calls'] == 0
            and native.get('canonical_rollout_verified') is True and native.get('native_turn_completed') is True
            and native.get('native_input_and_reference_verified') is True
            and native.get('native_reply_matches') is True, 'continuity_phase_native_report_invalid')
        require(isinstance(report.get('task_epoch'), int) and not isinstance(report['task_epoch'], bool)
            and report['task_epoch'] > 0, 'continuity_phase_task_epoch_invalid')
        require(isinstance(report.get('leader_epoch'), int) and not isinstance(report['leader_epoch'], bool)
            and report['leader_epoch'] > 0, 'continuity_phase_leader_term_invalid')
        require(isinstance(report.get('host_identity_sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', report['host_identity_sha256']), 'continuity_phase_host_identity_invalid')
    require(cloud['native']['thread_id'] == laptop['native']['thread_id']
        and cloud['native']['turn_id'] != laptop['native']['turn_id'], 'continuity_report_thread_not_continued')
    require(laptop['leader_epoch'] > cloud['leader_epoch'], 'continuity_report_leader_term_not_increased')
    require(cloud['host_identity_sha256'] != laptop['host_identity_sha256'], 'continuity_report_same_observed_host')
    return {'same_native_thread': True, 'distinct_native_turns': True, 'different_observed_hostnames': True,
        'leader_term_increased': True, 'native_tool_calls': 0,
        'checkpoint_and_complete_artifact_verified_both_phases': all(
            report.get('checkpoint_verified') is True and report.get('complete_artifact_verified') is True
            for report in (cloud, laptop)), 'production_leader_scope_used': False,
        'physical_host_identity_independently_verified': False, 'authority_host_failover_verified': False,
        'unknown_effect_turn_replay_verified': False, 'evidence_source': 'owner_private_phase_reports'}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProbeError('continuity_cli_arguments_invalid')


def main(argv=None):
    output = {'probe': FORMAT, 'model_started_by_helper': False, 'auth_values_read_or_copied': False,
              'new_paid_api_provisioned': False, 'production_services_changed': False}
    try:
        parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('command')
        parser.add_argument('--root', required=True)
        parser.add_argument('--port', type=int, default=17682)
        parser.add_argument('--phase')
        parser.add_argument('--source-worker-config')
        parser.add_argument('--server-config')
        parser.add_argument('--report')
        parser.add_argument('--cloud-report')
        parser.add_argument('--laptop-report')
        parser.add_argument('--submit', action='store_true')
        parser.add_argument('--timeout', type=float, default=30)
        parser.add_argument('--interval', type=float, default=1)
        args = parser.parse_args(argv)
        require(args.command in ('prepare', 'configure-worker', 'observe', 'verify'), 'continuity_command_invalid')
        require(not args.submit or args.command == 'observe', 'continuity_explicit_submit_only_for_observe')
        require(math.isfinite(args.timeout) and 0 < args.timeout <= 300
            and math.isfinite(args.interval) and 0.1 <= args.interval <= 10, 'continuity_poll_bounds_invalid')
        if args.command == 'prepare':
            state = prepare(args.root, args.port)
            output.update(prepared=True, run_id=state['run_id'], phase_nodes=state['nodes'])
            code = 0
        else:
            state = load_state(args.root)
            if args.command == 'configure-worker':
                worker = configure_worker(state, args.phase, args.source_worker_config)
                output.update(configured=True, phase=args.phase, node=worker['node_id'],
                    authentication_verified=False, native_permissions_copied=True,
                    native_memories_requested=False, process_started=False)
                code = 0
            elif args.command == 'verify':
                output['verification'] = verify_reports(state, private_read(args.cloud_report), private_read(args.laptop_report))
                code = 0
            else:
                require(args.phase in PHASES, 'continuity_phase_invalid')
                root = Path(state['root'])
                operator = private_read(root / 'operator-client.json')
                require(operator == {'control_url': 'http://127.0.0.1:' + str(state['port']),
                    'token_file': str(root / 'operator.token')}, 'continuity_operator_binding_changed')
                worker = private_read(root / (args.phase + '-worker.json'))
                server = private_read(args.server_config) if args.server_config else None
                probe = Probe(Client(operator), state, args.phase, worker, server)
                if args.submit:
                    output['submission'] = probe.submit()
                report, code = probe.observe(args.timeout, args.interval)
                output['observation'] = report
                if args.report and code == 0:
                    report_path = Path(args.report)
                    require(report_path.parent == root, 'continuity_report_must_be_in_private_root')
                    publish(report_path, report)
    except Exception as error:
        output.update(error=error_code(error), retry_with_same_state=True, completion_not_assumed=True)
        code = 1
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
