"""Owner-local review of one pinned auth-copy/query-close failed native turn.

Default is read-only. This is NOT a zero-effect retry, a generic guard-clear API,
or a native Shell policy. A private, independently reviewed case spec and its
explicit digest approval are required; unknown code/effects fail closed.
Python 3.6 compatible. Auth bytes, commands and native bodies are never printed.
"""
import argparse
import ast
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import quote


_module_spec = importlib.util.spec_from_file_location(
    '_effect_review_quota_paths', str(Path(__file__).with_name('reconcile_native_quota.py')))
quota = importlib.util.module_from_spec(_module_spec)
_module_spec.loader.exec_module(quota)

PROTOCOL = 'owner-native-effect-review/1'
CASE = 'exclusive-auth-copy-query-close/1'
NAMESPACE = 'native-known-effects-failed-review-v1'
ROLES = ('metadata_read', 'local_account_query', 'code_read', 'worker_config_read',
         'exclusive_auth_copy', 'authority_account_query')
MAX_FILE = 256 * 1024 * 1024
MAX_SPEC = 1024 * 1024
MAX_LINE = 8 * 1024 * 1024
DEPENDENCY_TABLES = {'managed_allocations', 'remote_delegations', 'route_decisions',
                     'task_continuations', 'interactions', 'mesh_executions'}
SUPPORTED_RECORDS = {
    'event_msg:task_started', 'turn_context:', 'response_item:message',
    'event_msg:item_completed', 'event_msg:token_count', 'response_item:reasoning',
    'response_item:custom_tool_call', 'token_usage_record:',
    'response_item:custom_tool_call_output', 'compacted:', 'world_state:',
    'event_msg:thread_settings_applied', 'event_msg:task_complete'}


class ReviewDenied(ValueError):
    """Fixed, non-content-bearing denial; never include paths or provider text."""


def require(value, code):
    if not value:
        raise ReviewDenied(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf8')


def digest(value):
    return sha(encoded(value))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def parse(raw):
    return json.loads(raw, object_pairs_hook=unique_object)


def exact(value, keys, code):
    require(isinstance(value, dict) and set(value) == set(keys), code)


def file_metadata(path, private=False, root_ok=False):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'absolute_private_path_required')
    for parent in reversed(path.parents):
        m = parent.lstat()
        require(stat.S_ISDIR(m.st_mode) and not stat.S_ISLNK(m.st_mode), 'unsafe_path_ancestor')
        require(m.st_uid in (0, os.getuid()), 'foreign_path_ancestor')
        require(not m.st_mode & 0o022 or m.st_mode & stat.S_ISVTX, 'writable_path_ancestor')
    m = path.lstat()
    require(stat.S_ISREG(m.st_mode) and not stat.S_ISLNK(m.st_mode), 'regular_file_required')
    require(m.st_uid == os.getuid() or root_ok and m.st_uid == 0, 'owned_file_required')
    require(not m.st_mode & 0o022, 'writable_file_denied')
    if private:
        require(stat.S_IMODE(m.st_mode) == 0o600, 'private_0600_required')
    return {'dev': m.st_dev, 'ino': m.st_ino, 'uid': m.st_uid,
            'mode': stat.S_IMODE(m.st_mode), 'size': m.st_size,
            'mtime_ns': m.st_mtime_ns, 'ctime_ns': m.st_ctime_ns}


def read_checked(path, private=False, root_ok=False, limit=MAX_FILE):
    before = file_metadata(path, private, root_ok)
    require(before['size'] <= limit, 'file_size_bound')
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        opened = os.fstat(source.fileno())
        require((opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode & 0o777,
                 opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) ==
                tuple(before[k] for k in ('dev', 'ino', 'uid', 'mode', 'size', 'mtime_ns', 'ctime_ns')),
                'file_generation_changed')
        raw = source.read(limit + 1)
        require(len(raw) <= limit, 'file_size_bound')
    require(file_metadata(path, private, root_ok) == before, 'file_generation_changed')
    return raw, dict(before, sha256=sha(raw))


def private_directory(path):
    path = quota.checked_path(str(path))
    m = path.lstat()
    require(m.st_uid == os.getuid() and stat.S_IMODE(m.st_mode) == 0o700,
            'owned_0700_directory_required')
    return path


def load_spec(path, allow_expired=False):
    raw, version = read_checked(path, private=True, limit=MAX_SPEC)
    value = parse(raw.decode('utf8'))
    exact(value, ('protocol', 'case', 'issued_at', 'expires_at', 'worker_config', 'server_config',
                  'review_root', 'expected', 'commands', 'polls', 'record_counts',
                  'authority_sources', 'local_observer', 'worker'), 'case_spec_shape')
    require(value['protocol'] == PROTOCOL and value['case'] == CASE, 'unsupported_review_case')
    start, end = value['issued_at'], value['expires_at']
    require(all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (start, end))
            and 0 < end - start <= 1800, 'review_time_window_invalid')
    require(allow_expired or start <= time.time() <= end, 'review_spec_expired_or_future')
    expected = value['expected']
    exact(expected, ('task_id', 'epoch', 'scope', 'thread_id', 'turn_id', 'checkpoint_sha256',
                     'task_sha256', 'session_sha256', 'session_state_sha256', 'rollout_path',
                     'rollout_file', 'selected_sha256', 'selected_size', 'auth_path', 'auth_file',
                     'auth_email_binding_sha256', 'auth_account_binding_sha256',
                     'dependency_tables', 'old_artifact'), 'expected_case_shape')
    require(isinstance(expected['task_id'], str) and re.fullmatch(r'[0-9a-f]{64}', expected['task_id']),
            'original_task_id_required')
    require(type(expected['epoch']) is int and expected['epoch'] >= 1, 'original_epoch_required')
    quota.identity(expected['thread_id'])
    quota.identity(expected['turn_id'])
    require(isinstance(value['commands'], list) and len(value['commands']) == len(ROLES),
            'six_reviewed_commands_required')
    for item, role in zip(value['commands'], ROLES):
        exact(item, ('role', 'call_id', 'input_sha256', 'command_sha256', 'stdout_sha256',
                     'stdout_size', 'chunk_ids'), 'reviewed_command_shape')
        require(item['role'] == role and isinstance(item['call_id'], str), 'reviewed_command_order')
        require(type(item['stdout_size']) is int and item['stdout_size'] >= 0,
                'reviewed_stdout_bound')
    require(isinstance(value['polls'], list) and len(value['polls']) == 2, 'two_closed_polls_required')
    for item in value['polls']:
        exact(item, ('call_id', 'input_sha256', 'session_id'), 'reviewed_poll_shape')
        require(type(item['session_id']) is int and item['session_id'] >= 0, 'reviewed_poll_handle')
    all_ids = [x['call_id'] for x in value['commands'] + value['polls']]
    require(len(set(all_ids)) == 8, 'duplicate_reviewed_call')
    require(isinstance(value['record_counts'], dict)
            and set(value['record_counts']) <= SUPPORTED_RECORDS
            and all(type(n) is int and n >= 1 for n in value['record_counts'].values()),
            'unsupported_reviewed_record_type')
    for key in ('checkpoint_sha256', 'task_sha256', 'session_sha256', 'session_state_sha256',
                'selected_sha256', 'auth_email_binding_sha256', 'auth_account_binding_sha256'):
        require(isinstance(expected[key], str) and re.fullmatch(r'[0-9a-f]{64}', expected[key]),
                'reviewed_digest_required')
    require(expected['auth_file'].get('mode') == 0o600 and expected['auth_file'].get('size', 0) > 0,
            'reviewed_auth_metadata_required')
    return value, version


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _name(node.value) + '.' + node.attr
    return 'dynamic'


def _literal(node):
    if isinstance(node, getattr(ast, 'Constant', ())):
        return node.value
    if isinstance(node, getattr(ast, 'Str', ())):
        return node.s
    if isinstance(node, getattr(ast, 'Num', ())):
        return node.n
    if isinstance(node, getattr(ast, 'NameConstant', ())):
        return node.value
    return None


def python_body(command):
    # The original SSH command represents quoted PY as shell quote splices.
    # Admit only whitespace/quotes/backslashes around that exact delimiter;
    # do not execute, interpolate or broadly unescape the recorded command.
    marker = re.search(r'<<([^\n]*)\n', command)
    require(marker is not None and re.fullmatch(r"[\s'\"\\]*PY[\s'\"\\]*", marker.group(1)),
            'reviewed_python_heredoc_required')
    end = re.search(r'\nPY([\s\x27\x22]*)$', command)
    require(end is not None and end.start() > marker.end(), 'unexpected_shell_tail')
    try:
        return ast.parse(command[marker.end():end.start()])
    except SyntaxError:
        raise ReviewDenied('reviewed_python_ast_invalid') from None


def inspect_copy_command(command, auth_path):
    """Inspect the actual exclusive create, not a model's effect summary."""
    tree = python_body(command)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    allowed = {'pathlib.Path', 'p.mkdir', 'os.chmod', 'subprocess.run', 'json.loads', 'RuntimeError',
               'x.get', 'str', 'os.open', 'os.fdopen', 'h.write', 'print'}
    require(all(_name(n.func) in allowed for n in calls), 'unreviewed_auth_copy_call')
    path_assign = [n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'p' for t in n.targets)]
    require(len(path_assign) == 1 and isinstance(path_assign[0].value, ast.Call)
            and _name(path_assign[0].value.func) == 'pathlib.Path'
            and len(path_assign[0].value.args) == 1
            and _literal(path_assign[0].value.args[0]) == str(Path(auth_path).parent),
            'auth_copy_target_mismatch')
    files = [n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == 'f' for t in n.targets)]
    require(len(files) == 1 and isinstance(files[0].value, ast.BinOp)
            and isinstance(files[0].value.op, ast.Div)
            and _name(files[0].value.left) == 'p'
            and _literal(files[0].value.right) == 'auth.json', 'auth_copy_filename_mismatch')
    opens = [n for n in calls if _name(n.func) == 'os.open']
    require(len(opens) == 1 and len(opens[0].args) == 3 and not opens[0].keywords,
            'exclusive_auth_open_required')
    op = opens[0]
    require(isinstance(op.args[0], ast.Call) and _name(op.args[0].func) == 'str'
            and len(op.args[0].args) == 1 and _name(op.args[0].args[0]) == 'f'
            and _literal(op.args[2]) == 0o600, 'auth_open_path_or_mode_changed')
    flags = op.args[1]
    flag_nodes = list(ast.walk(flags))
    require({n.attr for n in flag_nodes if isinstance(n, ast.Attribute)} ==
            {'O_WRONLY', 'O_CREAT', 'O_EXCL'}
            and all(isinstance(n, (ast.BinOp, ast.BitOr, ast.Attribute, ast.Name, ast.Load))
                    for n in flag_nodes), 'auth_open_not_exclusive')
    writers = [n for n in calls if _name(n.func) == 'h.write']
    require(len(writers) == 1 and len(writers[0].args) == 1 and not writers[0].keywords
            and _name(writers[0].args[0]) == 'r.stdout', 'auth_copy_not_complete_stdout')
    managers = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
    require(len(managers) == 1 and len(managers[0].items) == 1
            and isinstance(managers[0].items[0].context_expr, ast.Call)
            and _name(managers[0].items[0].context_expr.func) == 'os.fdopen'
            and _literal(managers[0].items[0].context_expr.args[1]) == 'wb'
            and any(n is writers[0] for n in ast.walk(managers[0])), 'auth_writer_not_closed')
    runs = [n for n in calls if _name(n.func) == 'subprocess.run']
    require(len(runs) == 1 and len(runs[0].args) == 1
            and isinstance(runs[0].args[0], ast.List), 'single_existing_auth_read_required')
    argv = [_literal(n) for n in runs[0].args[0].elts]
    require(all(isinstance(x, str) for x in argv) and argv[0] == 'ssh'
            and argv[-1].startswith('cat /') and argv[-1].endswith('/auth.json')
            and re.fullmatch(r'cat /[a-zA-Z0-9_./@-]+/auth\.json', argv[-1]),
            'auth_source_not_plain_read')
    keywords = {k.arg: k.value for k in runs[0].keywords}
    require(set(keywords) == {'stdout', 'stderr', 'timeout'}
            and _name(keywords['stdout']) == 'subprocess.PIPE'
            and _name(keywords['stderr']) == 'subprocess.PIPE'
            and _literal(keywords['timeout']) == 25, 'auth_read_process_contract_changed')
    chmods = [n for n in calls if _name(n.func) == 'os.chmod']
    mkdirs = [n for n in calls if _name(n.func) == 'p.mkdir']
    require(len(chmods) == 1 and len(chmods[0].args) == 2
            and _literal(chmods[0].args[1]) == 0o700 and len(mkdirs) == 1
            and {k.arg: _literal(k.value) for k in mkdirs[0].keywords} ==
            {'mode': 0o700, 'parents': True, 'exist_ok': True}, 'auth_directory_contract_changed')
    require(any(isinstance(n, ast.If) and _name(n.test) == 'r.returncode'
                and any(isinstance(x, ast.Raise) for x in ast.walk(n)) for n in tree.body),
            'auth_source_read_failure_not_fenced')


def inspect_query_command(command):
    tree = python_body(command)
    require(any(isinstance(n, ast.Call) and _name(n.func) == 'Codex' for n in ast.walk(tree)),
            'original_account_runtime_required')
    rpc = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute) and n.func.attr == 'rpc']
    require(len(rpc) == 1 and len(rpc[0].args) == 2
            and _literal(rpc[0].args[0]) == 'account/rateLimits/read'
            and isinstance(rpc[0].args[1], ast.Dict) and not rpc[0].args[1].keys,
            'query_rpc_not_readonly')
    require(not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ('start', 'steer', 'interrupt', 'send', 'write', 'write_bytes',
                                        'write_text', 'unlink', 'rename', 'replace', 'system', 'Popen')
                    for n in ast.walk(tree)), 'query_has_unreviewed_effect')
    require(any(isinstance(n, ast.Try) and any(isinstance(x, ast.Call)
                    and isinstance(x.func, ast.Attribute) and x.func.attr == 'close'
                    for b in n.finalbody for x in ast.walk(b)) for n in ast.walk(tree)),
            'query_runtime_not_finally_closed')
    require(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'account' for n in ast.walk(tree)), 'account_read_missing')
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            require(all(x.name in ('json', 'sys') for x in node.names), 'query_import_changed')
        elif isinstance(node, ast.ImportFrom):
            require(node.module in ('pathlib', 'assistant_mesh.codex'), 'query_import_changed')
        elif isinstance(node, ast.Call):
            name = _name(node.func)
            require(name in {'Codex', 'Path.home', 'dict', 'json.dumps', 'json.loads', 'json.load',
                             'print', 'str', 'sys.path.insert', 'type', 'open'}
                    or name.endswith(('.get', '.account', '.close', '.rpc', '.read_bytes', '.startswith')),
                    'query_unreviewed_function')
            if name == 'open':
                require(len(node.args) == 1 and not node.keywords, 'query_open_not_readonly')


def inspect_read_command(command, role):
    prefix = command.split('<<', 1)[0]
    require(not re.search(r'(?<!<)>|`|\$\(|<\(|\b(?:tee|rm|mv|cp|touch|mkdir|chmod|chown|'
                          r'truncate|systemctl|kill|curl|wget|git)\b', prefix),
            'read_command_has_unreviewed_shell_effect')
    verbs = re.findall(r'(?:^|[;\n])\s*([\w./-]+)', prefix)
    allowed = {'metadata_read': {'rg', 'cat', 'ssh'}, 'code_read': {'rg'},
               'worker_config_read': {'sed', 'rg', 'python3'}}[role]
    require(verbs and set(verbs) <= allowed, 'read_shell_program_changed')
    if role == 'code_read':
        require('<<' not in command and '\n' not in command, 'code_read_shape_changed')
        return
    tree = python_body(command)
    allowed_calls = {'Path.home', 'base64.urlsafe_b64decode', 'bool', 'json.dumps', 'json.loads',
                     'json.load', 'list', 'str', 'print', 'open', 'type'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            require(all(x.name in ('json', 'base64') for x in node.names), 'read_import_changed')
        elif isinstance(node, ast.ImportFrom):
            require(node.module == 'pathlib', 'read_import_changed')
        elif isinstance(node, ast.Call):
            name = _name(node.func)
            require(name in allowed_calls or name.endswith(
                ('.get', '.split', '.iterdir', '.exists', '.is_dir', '.lower', '.read_bytes', '.glob', '.items')),
                'read_unreviewed_function')
            if name == 'open':
                require(len(node.args) == 1 and not node.keywords, 'read_open_not_readonly')


def command_from_call(source):
    require(isinstance(source, str), 'native_call_input_invalid')
    require(re.findall(r'tools\.([\w]+)\s*\(', source) == ['exec_command'],
            'unreviewed_nested_native_tool')
    matches = list(re.finditer(r'cmd\s*:\s*("(?:[^"\\]|\\.)*")', source))
    require(len(matches) == 1, 'single_recorded_command_required')
    return parse(matches[0].group(1))


def inspect_rollout(spec, selected):
    expected = spec['expected']
    path = quota.canonical_rollout(expected['rollout_path'], selected)
    before = file_metadata(path, private=True)
    require(dict(before, sha256=expected['rollout_file'].get('sha256')) == expected['rollout_file'],
            'rollout_generation_changed')
    require(before['size'] <= MAX_FILE, 'rollout_size_bound')
    commands = {x['call_id']: dict(x, data=[], chunks=[], terminal=False) for x in spec['commands']}
    polls = {x['call_id']: x for x in spec['polls']}
    pending_poll = {}
    seen_calls, seen_outputs, events, counts = set(), set(), {}, {}
    full, segment = hashlib.sha256(), hashlib.sha256()
    segment_size = starts = completes = 0
    active = False
    with open(str(path), 'rb') as source:
        for number, line in enumerate(source, 1):
            require(len(line) <= MAX_LINE and line.endswith(b'\n'), 'incomplete_rollout_record')
            full.update(line)
            value = parse(line.decode('utf8'))
            require(isinstance(value, dict) and isinstance(value.get('type'), str), 'invalid_native_record')
            kind, payload = value['type'], value.get('payload')
            item = payload.get('type') if isinstance(payload, dict) else None
            if number == 1:
                require(kind == 'session_meta' and isinstance(payload, dict)
                        and payload.get('id') == expected['thread_id'],
                        'native_history_thread_mismatch')
                require(path in quota.metadata_paths(Path(selected['checkpoint']['codex_auth_home']),
                                                   payload, expected['thread_id']),
                        'native_history_path_mismatch')
            if kind == 'event_msg' and item == 'task_started':
                if payload.get('turn_id') == expected['turn_id']:
                    require(not starts, 'duplicate_selected_turn')
                    starts, active = 1, True
                elif starts:
                    raise ReviewDenied('selected_turn_not_latest')
            if not active:
                continue
            require(isinstance(payload, dict), 'invalid_selected_native_record')
            require(not completes, 'native_record_after_selected_completion')
            segment.update(line)
            segment_size += len(line)
            require(all(key not in payload or payload[key] == wanted for key, wanted in
                        (('thread_id', expected['thread_id']), ('turn_id', expected['turn_id']))),
                    'selected_native_identity_mismatch')
            label = kind + ':' + str(item or '')
            require(label in spec['record_counts'], 'unreviewed_selected_record')
            counts[label] = counts.get(label, 0) + 1
            if kind == 'response_item' and item == 'custom_tool_call':
                cid, native_source = payload.get('call_id'), payload.get('input')
                require(cid not in seen_calls and payload.get('name') == 'exec', 'unreviewed_native_call')
                seen_calls.add(cid)
                if cid in commands:
                    cmd = commands[cid]
                    require(sha(native_source.encode()) == cmd['input_sha256'], 'native_call_code_changed')
                    text = command_from_call(native_source)
                    require(sha(text.encode()) == cmd['command_sha256'], 'native_command_changed')
                    if cmd['role'] == 'exclusive_auth_copy':
                        inspect_copy_command(text, expected['auth_path'])
                    elif cmd['role'] in ('local_account_query', 'authority_account_query'):
                        inspect_query_command(text)
                    else:
                        inspect_read_command(text, cmd['role'])
                else:
                    require(cid in polls and isinstance(native_source, str), 'unreviewed_native_call')
                    poll = polls[cid]
                    require(sha(native_source.encode()) == poll['input_sha256']
                            and re.findall(r'tools\.([\w]+)\s*\(', native_source) == ['write_stdin'],
                            'unreviewed_poll_code')
                    handle = re.search(r'session_id\s*:\s*(\d+)', native_source)
                    chars = re.findall(r'chars\s*:\s*("(?:[^"\\]|\\.)*")', native_source)
                    require(handle is not None and int(handle.group(1)) == poll['session_id']
                            and all(parse(x) == '' for x in chars),
                            'poll_is_not_empty_observation')
                    require(poll['session_id'] in pending_poll, 'poll_handle_not_from_original_command')
            elif kind == 'response_item' and item == 'custom_tool_call_output':
                cid = payload.get('call_id')
                require(cid in seen_calls and cid not in seen_outputs, 'unbound_native_output')
                seen_outputs.add(cid)
                dest = cid if cid in commands else pending_poll[polls[cid]['session_id']]
                command = commands[dest]
                require(not command['terminal'], 'output_after_command_terminal')
                records = payload.get('output')
                require(isinstance(records, list) and len(records) == 2
                        and all(isinstance(r, dict) and r.get('type') == 'input_text'
                                and isinstance(r.get('text'), str) for r in records), 'native_output_shape_changed')
                receipt = parse(records[1]['text'])
                require(isinstance(receipt, dict) and set(receipt) <=
                        {'chunk_id', 'wall_time_seconds', 'output', 'session_id', 'exit_code',
                         'original_token_count'} and isinstance(receipt.get('output'), str),
                        'unknown_native_command_receipt')
                command['chunks'].append(receipt.get('chunk_id'))
                command['data'].append(receipt['output'])
                if 'session_id' in receipt:
                    handle = receipt['session_id']
                    require(type(handle) is int and handle not in pending_poll and 'exit_code' not in receipt,
                            'duplicate_or_terminal_live_handle')
                    pending_poll[handle] = dest
                else:
                    require(receipt.get('exit_code') == 0, 'command_not_terminal_success')
                    command['terminal'] = True
            elif kind == 'event_msg' and item == 'item_completed':
                native = payload.get('item')
                require(isinstance(native, dict) and native.get('type') in
                        ('UserMessage', 'Reasoning', 'AgentMessage', 'CommandExecution', 'ContextCompaction'),
                        'unknown_native_effect_item')
                if native['type'] == 'CommandExecution':
                    require(native.get('status') == 'completed' and native.get('exit_code') == 0
                            and not native.get('stderr'), 'native_command_not_closed')
                    argv = native.get('command')
                    require(isinstance(argv, list) and len(argv) == 3
                            and argv[1] == '-lc' and isinstance(argv[2], str), 'native_command_event_shape')
                    key = sha(argv[2].encode())
                    require(key not in events, 'duplicate_native_command_event')
                    events[key] = native
            elif kind == 'event_msg' and item == 'task_complete':
                require(payload.get('last_agent_message') is None
                        and isinstance(payload.get('error'), dict)
                        and payload['error'].get('codex_error_info') == 'server_overloaded',
                        'not_reviewed_failed_terminal')
                completes = 1
    require(file_metadata(path, private=True) == before, 'rollout_generation_changed')
    require(starts == completes == 1 and full.hexdigest() == expected['rollout_file']['sha256']
            and segment.hexdigest() == expected['selected_sha256']
            and segment_size == expected['selected_size'] and counts == spec['record_counts'],
            'complete_reviewed_rollout_mismatch')
    require(seen_calls == seen_outputs == set(commands) | set(polls) and len(events) == 6,
            'native_call_or_output_not_closed')
    proof = []
    for item in spec['commands']:
        command = commands[item['call_id']]
        out = ''.join(command['data']).encode('utf8')
        event = events.get(item['command_sha256'])
        require(command['terminal'] and command['chunks'] == item['chunk_ids']
                and len(out) == item['stdout_size'] and sha(out) == item['stdout_sha256']
                and event is not None and (event.get('stdout') or '').encode('utf8') == out,
                'whole_original_output_mismatch')
        if item['role'] == 'authority_account_query':
            result = parse(out.decode('utf8'))
            account = result.get('account') if isinstance(result, dict) else None
            require(isinstance(account, dict) and account.get('authenticated') is True
                    and account.get('type') == 'chatgpt' and account.get('plan') == 'plus'
                    and isinstance(result.get('limits'), dict), 'original_post_copy_account_unconfirmed')
        proof.append(dict(item, original_terminal=True, event_stdout_equals_chunks=True))
    return {'rollout_file': dict(before, sha256=full.hexdigest()), 'selected_sha256': segment.hexdigest(),
            'selected_size': segment_size, 'commands': proof, 'error_category': 'server_overloaded',
            'effects_zero': False, 'original_turn_replay_authorized': False}


# Fixed read-only observer, sent over an already owner-approved SSH route. No
# model, app-server, native RPC, shell dispatch, writes or auth contents in output.
# Paths and expected identity hashes are private reviewer configuration, never
# taken from the failed model's stdout or from a new model's tool arguments.
OBSERVER_CODE = r'''
import ast,base64,hashlib,json,os,stat,sys,time
def deny(): raise ValueError('observer_denied')
def pairs(items):
 d={}
 for k,v in items:
  if k in d: deny()
  d[k]=v
 return d
def parse(s): return json.loads(s,object_pairs_hook=pairs)
def meta(p,private=False):
 if not isinstance(p,str) or not p.startswith('/') or '..' in p.split('/'): deny()
 q='/'
 for part in p.split('/')[1:-1]:
  q=os.path.join(q,part);a=os.lstat(q)
  if not stat.S_ISDIR(a.st_mode) or a.st_uid not in (0,os.getuid()) or (a.st_mode&18 and not a.st_mode&stat.S_ISVTX):deny()
 a=os.lstat(p)
 if not stat.S_ISREG(a.st_mode) or a.st_uid!=os.getuid() or a.st_mode&18:deny()
 if private and stat.S_IMODE(a.st_mode)!=384:deny()
 return {'dev':a.st_dev,'ino':a.st_ino,'uid':a.st_uid,'mode':stat.S_IMODE(a.st_mode),'size':a.st_size,'mtime_ns':a.st_mtime_ns,'ctime_ns':a.st_ctime_ns}
def read(p,private=False,limit=16777216):
 before=meta(p,private)
 if before['size']>limit:deny()
 f=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(f,'rb') as h:
  a=os.fstat(h.fileno())
  if (a.st_dev,a.st_ino,a.st_uid,stat.S_IMODE(a.st_mode),a.st_size,a.st_mtime_ns,a.st_ctime_ns)!=tuple(before[k] for k in ('dev','ino','uid','mode','size','mtime_ns','ctime_ns')):deny()
  raw=h.read(limit+1)
 if len(raw)>limit or meta(p,private)!=before:deny()
 before['sha256']=hashlib.sha256(raw).hexdigest()
 return raw,before
def auth(p,email_hash,account_hash):
 parent=os.lstat(os.path.dirname(p))
 if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.getuid() or stat.S_IMODE(parent.st_mode)!=448:deny()
 raw,m=read(p,True,65536);o=parse(raw.decode());t=o.get('tokens',{});tok=t.get('id_token','').split('.')
 if len(tok)!=3 or not all(isinstance(t.get(k),str) and t[k] for k in ('id_token','access_token','refresh_token')):deny()
 c=parse(base64.urlsafe_b64decode(tok[1]+'='*(-len(tok[1])%4)).decode());email=c.get('email','').lower();account=str(t.get('account_id',''))
 return {'file':m,'email_binding':hashlib.sha256(email.encode()).hexdigest()==email_hash,'account_binding':hashlib.sha256(account.encode()).hexdigest()==account_hash}
def literal(n):
 if isinstance(n,getattr(ast,'Constant',())):return n.value
 if isinstance(n,getattr(ast,'Str',())):return n.s
 if isinstance(n,getattr(ast,'NameConstant',())):return n.value
def codex_contract(raw):
 t=ast.parse(raw.decode());classes=[n for n in t.body if isinstance(n,ast.ClassDef) and n.name=='Codex']
 if len(classes)!=1:deny()
 methods={n.name:n for n in classes[0].body if isinstance(n,ast.FunctionDef)}
 if not all(n in methods for n in ('__init__','account','close')):deny()
 calls=lambda name:[n for n in ast.walk(methods[name]) if isinstance(n,ast.Call)]
 rpc=[n for n in calls('account') if isinstance(n.func,ast.Attribute) and n.func.attr=='rpc']
 if len(rpc)!=1 or len(rpc[0].args)!=2 or literal(rpc[0].args[0])!='account/read':deny()
 obj=rpc[0].args[1]
 if not isinstance(obj,ast.Dict) or {literal(k):literal(v) for k,v in zip(obj.keys,obj.values)}!={'refreshToken':False}:deny()
 names={n.func.attr for n in calls('close') if isinstance(n.func,ast.Attribute)}
 if not {'killpg','wait','poll'}<=names:deny()
 signals={n.attr for n in ast.walk(methods['close']) if isinstance(n,ast.Attribute)}
 if not {'SIGTERM','SIGKILL'}<=signals:deny()
 initnames={n.func.attr for n in calls('__init__') if isinstance(n.func,ast.Attribute)}
 if 'Popen' not in initnames:deny()
 return {'account_read_refresh_token':False,'close_reaps_owned_runtime':True,'constructor_launches_native_appserver':True}
def processes(interval,worker_pid=None):
 hz=os.sysconf('SC_CLK_TCK');boot=time.time()-float(open('/proc/uptime').read().split()[0]);servers=[];worker=None
 for pid in os.listdir('/proc'):
  if not pid.isdigit():continue
  try:
   p='/proc/'+pid;uid=os.stat(p).st_uid
   if uid!=os.getuid():continue
   raw=open(p+'/cmdline','rb').read();args=raw.split(b'\0');s=open(p+'/stat').read();v=s[s.rfind(')')+2:].split();ticks=int(v[19]);start=boot+ticks/hz
   row={'pid':int(pid),'uid':uid,'pgid':int(v[2]),'start_ticks':ticks,'command_sha256':hashlib.sha256(raw).hexdigest()}
   if int(pid)==worker_pid:
    if v[0] not in ('R','S','D','I'):deny()
    worker=row
   if b'app-server' in args:
    row['in_original_interval']=interval[0]-1<=start<=interval[1]+1;servers.append(row)
  except FileNotFoundError:continue
  except PermissionError:deny()
 return {'servers':sorted(servers,key=lambda r:r['pid']),'worker':worker,'original_interval_survivors':sum(r['in_original_interval'] for r in servers)}
def run(request):
 if not isinstance(request,dict) or set(request)!={'nonce','interval','auth_path','email_hash','account_hash','sources','worker_pid'}:deny()
 sources={};contracts={}
 for p in request['sources']:
  raw,m=read(p);sources[p]=m
  if p.endswith('/assistant_mesh/codex.py'):contracts[p]=codex_contract(raw)
 return {'nonce':request['nonce'],'observed_at':time.time(),'uid':os.getuid(),'auth':auth(request['auth_path'],request['email_hash'],request['account_hash']),'sources':sources,'codex_contracts':contracts,'processes':processes(request['interval'],request['worker_pid'])}
try:
 request=parse(sys.stdin.read(65537))
 print(json.dumps(run(request),sort_keys=True,separators=(',',':')))
except (OSError,ValueError,TypeError,KeyError,IndexError):
 print(json.dumps({'error':'observer_denied'}));sys.exit(2)
'''


def observer_request(spec, nonce, interval, local):
    expected = spec['expected']
    node = spec['local_observer'] if local else None
    return {'nonce': nonce, 'interval': interval,
            'auth_path': node['auth_path'] if local else expected['auth_path'],
            'email_hash': expected['auth_email_binding_sha256'],
            'account_hash': expected['auth_account_binding_sha256'],
            'sources': list(node['sources'] if local else spec['authority_sources']),
            'worker_pid': None if local else spec['worker']['pid']}


def local_observation(spec, request):
    cfg = spec['local_observer']
    exact(cfg, ('ssh_executable', 'ssh_executable_file', 'ssh_config', 'ssh_config_file',
                'alias', 'uid', 'auth_path', 'auth_file', 'sources'), 'local_observer_shape')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', cfg['alias']), 'ssh_alias_invalid')
    _, executable = read_checked(cfg['ssh_executable'], root_ok=True, limit=32 * 1024 * 1024)
    _, config = read_checked(cfg['ssh_config'], private=True, limit=MAX_SPEC)
    require(executable == cfg['ssh_executable_file'] and config == cfg['ssh_config_file'],
            'observer_transport_generation_changed')
    command = [cfg['ssh_executable'], '-F', cfg['ssh_config']]
    for key in ('BatchMode=yes', 'ConnectTimeout=10', 'StrictHostKeyChecking=yes', 'UpdateHostKeys=no',
                'RequestTTY=no', 'ClearAllForwardings=yes', 'ControlMaster=no', 'ControlPath=none',
                'PermitLocalCommand=no', 'LocalCommand=none', 'RemoteCommand=none'):
        command += ['-o', key]
    command += [cfg['alias'], 'python3 -c ' + shlex.quote(OBSERVER_CODE)]
    result = subprocess.run(command, input=encoded(request), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=25)
    require(result.returncode == 0 and len(result.stdout) <= MAX_SPEC,
            'local_observer_not_terminal')
    try:
        observed = parse(result.stdout.decode('utf8'))
    except (ValueError, UnicodeError):
        raise ReviewDenied('local_observer_invalid') from None
    require(file_metadata(cfg['ssh_executable'], root_ok=True) ==
            {k: v for k, v in executable.items() if k != 'sha256'}
            and file_metadata(cfg['ssh_config'], private=True) ==
            {k: v for k, v in config.items() if k != 'sha256'},
            'observer_transport_generation_changed')
    return observed


def authority_observation(spec, request):
    # Evaluate only our fixed observer source, never model/config supplied code.
    result = subprocess.run(['/usr/bin/python3', '-c', OBSERVER_CODE], input=encoded(request),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    require(result.returncode == 0 and len(result.stdout) <= MAX_SPEC, 'authority_observer_not_terminal')
    return parse(result.stdout.decode('utf8'))


def native_interval(spec):
    import datetime
    start = end = None
    with open(spec['expected']['rollout_path'], 'rb') as source:
        for line in source:
            value = parse(line.decode('utf8'))
            p = value.get('payload', {})
            if value.get('type') != 'event_msg' or p.get('turn_id') != spec['expected']['turn_id']:
                continue
            if p.get('type') not in ('task_started', 'task_complete'):
                continue
            text = value.get('timestamp', '')
            require(isinstance(text, str) and re.fullmatch(
                    r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z', text),
                    'native_timestamp_required')
            fmt = '%Y-%m-%dT%H:%M:%S.%fZ' if '.' in text else '%Y-%m-%dT%H:%M:%SZ'
            when = datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.timezone.utc).timestamp()
            if p['type'] == 'task_started':
                require(start is None, 'duplicate_native_start_time')
                start = when
            else:
                require(end is None, 'duplicate_native_end_time')
                end = when
    require(start is not None and end is not None and 0 < end - start < 3600,
            'native_execution_interval_invalid')
    return [start, end]


def live_evidence(spec, interval):
    nonce = sha(os.urandom(32))
    authority = authority_observation(spec, observer_request(spec, nonce, interval, False))
    local = local_observation(spec, observer_request(spec, nonce, interval, True))
    for observed, uid, expected_file, expected_sources in (
            (authority, os.getuid(), spec['expected']['auth_file'], spec['authority_sources']),
            (local, spec['local_observer']['uid'], spec['local_observer']['auth_file'],
             spec['local_observer']['sources'])):
        exact(observed, ('nonce', 'observed_at', 'uid', 'auth', 'sources', 'codex_contracts', 'processes'),
              'observer_result_shape')
        require(observed['nonce'] == nonce and observed['uid'] == uid
                and 0 <= time.time() - observed['observed_at'] <= 30, 'observer_identity_or_time_invalid')
        require(observed['auth']['file'] == expected_file
                and observed['auth']['email_binding'] is True and observed['auth']['account_binding'] is True,
                'reviewed_auth_identity_or_generation_changed')
        require(observed['sources'] == expected_sources, 'reviewed_source_generation_changed')
        codex_paths = {p for p in expected_sources if p.endswith('/assistant_mesh/codex.py')}
        require(len(codex_paths) == 1 and set(observed['codex_contracts']) == codex_paths
                and all(v == {'account_read_refresh_token': False, 'close_reaps_owned_runtime': True,
                              'constructor_launches_native_appserver': True}
                        for v in observed['codex_contracts'].values()), 'reviewed_codex_close_contract_missing')
        require(observed['processes']['original_interval_survivors'] == 0,
                'original_query_runtime_still_present')
    require(not authority['processes']['servers'], 'authority_native_runtime_not_idle')
    require(authority['processes']['worker'] == spec['worker'], 'worker_generation_changed')
    return {'nonce': nonce, 'authority': authority, 'local': local,
            'proof_kind': 'original_call_output_binding_plus_live_owner_observers',
            'external_auth_validity_now_proven': False}


def old_artifact(db, selected, spec):
    expected = spec['expected']['old_artifact']
    exact(expected, ('id', 'parts', 'compressed_size', 'compressed_sha256',
                     'raw_size', 'raw_sha256'), 'old_artifact_pin_shape')
    require(selected['session']['artifact'] == expected['id'], 'old_artifact_binding_changed')
    data, parts = io.BytesIO(), 0
    for part, body in db.execute('SELECT part,body FROM session_chunks WHERE id=? ORDER BY part',
                                 (expected['id'],)):
        require(part == parts and isinstance(body, bytes) and len(body) <= 65536,
                'old_native_artifact_parts_changed')
        data.write(body)
        parts += 1
        require(data.tell() <= MAX_FILE, 'old_artifact_size_bound')
    require(parts == expected['parts'] and data.tell() == expected['compressed_size']
            and sha(data.getvalue()) == expected['compressed_sha256'], 'old_native_artifact_changed')
    data.seek(0)
    size, checksum = 0, hashlib.sha256()
    with gzip.GzipFile(fileobj=data, mode='rb') as previous, open(spec['expected']['rollout_path'], 'rb') as current:
        while True:
            block = previous.read(65536)
            if not block:
                break
            size += len(block)
            require(size <= MAX_FILE and current.read(len(block)) == block,
                    'current_rollout_not_full_old_history_prefix')
            checksum.update(block)
    require(size == expected['raw_size'] and checksum.hexdigest() == expected['raw_sha256'],
            'old_native_artifact_body_changed')
    return expected


def selected_state(db, auth, spec):
    exp = spec['expected']
    selected = quota.snapshot(db, exp['task_id'], exp['epoch'], auth)
    task, session, checkpoint = selected['task'], selected['session'], selected['checkpoint']
    require(task['scope'] == exp['scope'] and selected['thread'] == exp['thread_id']
            and selected['turn'] == exp['turn_id'] and task['parent_id'] is None,
            'original_review_identity_changed')
    require(digest(task) == exp['task_sha256'] and digest(session) == exp['session_sha256']
            and sha(task['checkpoint'].encode()) == exp['checkpoint_sha256']
            and sha(session['state'].encode()) == exp['session_state_sha256'],
            'original_task_or_session_generation_changed')
    require(checkpoint.get('approval_required') is not True and not checkpoint.get('goal')
            and not checkpoint.get('native_lifecycle'), 'unreviewed_goal_or_approval')
    require(not db.execute('SELECT 1 FROM tasks WHERE scope=? AND status=? LIMIT 1',
                           (task['scope'], 'running')).fetchone()
            and not db.execute('SELECT 1 FROM tasks WHERE node=? AND status=? LIMIT 1',
                               (auth['node'], 'running')).fetchone(), 'current_worker_not_idle')
    require(not db.execute('SELECT 1 FROM tasks WHERE parent_id=? LIMIT 1', (task['id'],)).fetchone(),
            'original_task_has_children')
    for other in db.execute('SELECT id,checkpoint FROM tasks WHERE scope=? AND id<>? AND status<>?',
                             (task['scope'], task['id'], 'completed')):
        require(not parse(other['checkpoint']).get('side_effect_started'), 'other_scope_effect_unsettled')
    tables = exp['dependency_tables']
    require(isinstance(tables, dict) and tables, 'reviewed_dependency_schema_required')
    existing = {r[0] for r in db.execute('SELECT name FROM sqlite_master WHERE type=?', ('table',))}
    require(set(tables) == DEPENDENCY_TABLES & existing
            and {'managed_allocations', 'remote_delegations'} <= set(tables),
            'missing_or_unreviewed_dependency_table')
    for name, columns in tables.items():
        actual = [r[1] for r in db.execute('PRAGMA table_info(' + name + ')')]
        require(actual == columns and columns, 'dependency_schema_changed')
        # Fixed known tables only. Include embedded task bindings in JSON as well
        # as plain task_id, without exposing any row or manufacturing settlement.
        for row in db.execute('SELECT * FROM ' + name):
            require(not any(isinstance(v, str) and task['id'] in v for v in row),
                    'original_task_has_execution_or_delegation_dependency')
    old_artifact(db, selected, spec)
    return selected


def inspect(spec_path):
    spec, spec_file = load_spec(spec_path)
    authority = quota.authority(spec['worker_config'], spec['server_config'])
    _, worker_file = read_checked(spec['worker_config'], private=True, limit=MAX_SPEC)
    _, server_file = read_checked(spec['server_config'], private=True, limit=MAX_SPEC)
    db_file = file_metadata(authority['database'], private=True)
    root = private_directory(spec['review_root'])
    case_id = digest([NAMESPACE, spec['expected']['task_id'], spec['expected']['epoch'],
                      spec['expected']['rollout_file']['sha256']])
    require(not (root / case_id).exists(), 'original_review_intent_requires_readback')
    with contextlib.closing(quota.readonly(authority['database'])) as db:
        selected = selected_state(db, authority, spec)
    native = inspect_rollout(spec, selected)
    interval = native_interval(spec)
    live = live_evidence(spec, interval)
    require(read_checked(spec_path, private=True, limit=MAX_SPEC)[1] == spec_file
            and read_checked(spec['worker_config'], private=True, limit=MAX_SPEC)[1] == worker_file
            and read_checked(spec['server_config'], private=True, limit=MAX_SPEC)[1] == server_file,
            'review_control_generation_changed')
    require({k: db_file[k] for k in ('dev', 'ino', 'uid', 'mode')} ==
            {k: file_metadata(authority['database'], private=True)[k] for k in ('dev', 'ino', 'uid', 'mode')},
            'database_identity_changed')
    return {'spec': spec, 'spec_file': spec_file, 'worker_file': worker_file, 'server_file': server_file,
            'database_file': db_file, 'authority': authority, 'selected': selected,
            'native': native, 'interval': interval, 'live': live, 'case_id': case_id}


def create_file(path):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(descriptor, 'wb')


def fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, body):
    with create_file(path) as target:
        target.write(encoded(body) + b'\n')
        target.flush()
        os.fsync(target.fileno())
    fsync_directory(path.parent)
    return read_checked(path, private=True)[1]


def repeat_controls(context, spec_path):
    spec = context['spec']
    require(load_spec(spec_path)[1] == context['spec_file']
            and read_checked(spec['worker_config'], private=True, limit=MAX_SPEC)[1] == context['worker_file']
            and read_checked(spec['server_config'], private=True, limit=MAX_SPEC)[1] == context['server_file'],
            'review_control_generation_changed')
    previous = context['database_file']
    current = file_metadata(context['authority']['database'], private=True)
    require(all(previous[k] == current[k] for k in ('dev', 'ino', 'uid', 'mode')), 'database_identity_changed')
    native = inspect_rollout(spec, context['selected'])
    require(native == context['native'] and native_interval(spec) == context['interval'],
            'reviewed_native_generation_changed')
    # Each call performs a new challenge/metadata observation, not reuse of a
    # signed-looking boolean or of the old worker PID alone.
    return live_evidence(spec, context['interval'])


def stage_before(context, spec_path):
    spec = context['spec']
    directory = private_directory(spec['review_root']) / context['case_id']
    directory.mkdir(mode=0o700)  # An existing partial/unknown intent is never replaced.
    fsync_directory(directory.parent)
    intent = {'protocol': PROTOCOL, 'case': CASE, 'id': context['case_id'],
              'state': 'prepared', 'spec_sha256': context['spec_file']['sha256'],
              'task_id': spec['expected']['task_id'], 'source_epoch': spec['expected']['epoch'],
              'rollout_sha256': context['native']['rollout_file']['sha256'],
              'effects_zero': False, 'original_input_requeue': False, 'created': time.time()}
    intent_file = write_json(directory / '00-intent.json', intent)
    with contextlib.closing(quota.readonly(context['authority']['database'])) as db:
        selected = selected_state(db, context['authority'], spec)
        require(selected == context['selected'], 'before_snapshot_generation_changed')
        before = {'protocol': PROTOCOL, 'task': selected['task'], 'session': selected['session'],
                  'checkpoint': selected['checkpoint'], 'old_artifact': spec['expected']['old_artifact'],
                  'native': context['native'], 'owner_spec_sha256': context['spec_file']['sha256'],
                  'live_reviewer_evidence': context['live'], 'intent': intent}
        before_file = write_json(directory / '01-before.json', before)
        old_path = directory / '02-original-artifact.gz'
        with create_file(old_path) as target:
            for row in db.execute('SELECT body FROM session_chunks WHERE id=? ORDER BY part',
                                  (selected['session']['artifact'],)):
                target.write(row[0])
            target.flush()
            os.fsync(target.fileno())
        old_file = read_checked(old_path, private=True)[1]
        require(old_file['sha256'] == spec['expected']['old_artifact']['compressed_sha256'],
                'before_artifact_copy_mismatch')
        archive_path = directory / '03-full-rollout.gz'
        raw_digest, raw_size = hashlib.sha256(), 0
        with create_file(archive_path) as target:
            with gzip.GzipFile(filename='', fileobj=target, mode='wb', mtime=0) as packed:
                with open(spec['expected']['rollout_path'], 'rb') as original:
                    for block in iter(lambda: original.read(65536), b''):
                        packed.write(block)
                        raw_digest.update(block)
                        raw_size += len(block)
            target.flush()
            os.fsync(target.fileno())
        require(raw_digest.hexdigest() == spec['expected']['rollout_file']['sha256']
                and raw_size == spec['expected']['rollout_file']['size'], 'full_history_copy_changed')
        archive_file = read_checked(archive_path, private=True)[1]
        # Verify all uncompressed bytes again, not merely the compressed manifest.
        check, size = hashlib.sha256(), 0
        with gzip.open(str(archive_path), 'rb') as source:
            for block in iter(lambda: source.read(65536), b''):
                check.update(block)
                size += len(block)
        require(check.hexdigest() == raw_digest.hexdigest() and size == raw_size,
                'full_history_archive_readback_failed')
        backup_path = directory / '04-consistent-ledger.sql.gz'
        require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'source_database_not_integral')
        # Python 3.6 lacks Connection.backup(). A read transaction plus streamed
        # SQL dump is a consistent, complete private backup; never restore it
        # over the live ledger. This retains all original chunks/unknown rows.
        sql_digest = hashlib.sha256()
        sql_size = 0
        with create_file(backup_path) as target:
            with gzip.GzipFile(filename='', fileobj=target, mode='wb', mtime=0) as packed:
                for statement in db.iterdump():
                    raw = (statement + '\n').encode('utf8')
                    packed.write(raw)
                    sql_digest.update(raw)
                    sql_size += len(raw)
            target.flush()
            os.fsync(target.fileno())
        backup_file = read_checked(backup_path, private=True)[1]
        check, size = hashlib.sha256(), 0
        with gzip.open(str(backup_path), 'rb') as source:
            for block in iter(lambda: source.read(65536), b''):
                check.update(block)
                size += len(block)
        require(check.hexdigest() == sql_digest.hexdigest() and size == sql_size,
                'consistent_backup_readback_failed')
    live = repeat_controls(context, spec_path)
    ready = {'protocol': PROTOCOL, 'id': context['case_id'], 'state': 'before_snapshot_complete',
             'intent_file': intent_file, 'before_file': before_file, 'old_artifact_file': old_file,
             'full_history_file': archive_file, 'backup_file': backup_file,
             'backup_uncompressed_sha256': sql_digest.hexdigest(), 'backup_uncompressed_size': sql_size,
             'source_quick_check': 'ok', 'live_reviewer_evidence': live, 'created': time.time()}
    ready_file = write_json(directory / '05-backup-complete.json', ready)
    return {'directory': directory, 'ready': ready, 'ready_file': ready_file,
            'archive_path': archive_path, 'backup_path': backup_path}


def review_table(db):
    existing = db.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?',
                          ('table', 'native_effect_reviews')).fetchone()
    require(existing is None, 'existing_review_namespace_requires_readback')
    db.execute('''CREATE TABLE native_effect_reviews(
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, source_epoch INTEGER NOT NULL,
        closed_epoch INTEGER NOT NULL, before_task TEXT NOT NULL, before_session TEXT NOT NULL,
        before_artifact TEXT NOT NULL, complete_artifact TEXT NOT NULL,
        proof TEXT NOT NULL, backup_path TEXT NOT NULL, backup_sha256 TEXT NOT NULL,
        receipt TEXT NOT NULL, created REAL NOT NULL, UNIQUE(task_id,source_epoch))''')
    db.execute('''CREATE TRIGGER native_effect_reviews_no_update BEFORE UPDATE ON native_effect_reviews
        BEGIN SELECT RAISE(ABORT,'effect review is append only'); END''')
    db.execute('''CREATE TRIGGER native_effect_reviews_no_delete BEFORE DELETE ON native_effect_reviews
        BEGIN SELECT RAISE(ABORT,'effect review is append only'); END''')


def apply_review(spec_path, approved_sha256, before_transaction=None):
    context = inspect(spec_path)
    require(approved_sha256 == context['spec_file']['sha256'], 'explicit_owner_spec_digest_approval_required')
    spec, auth, selected = context['spec'], context['authority'], context['selected']
    staged = stage_before(context, spec_path)
    if before_transaction:
        before_transaction()  # Test-only injection, never a CLI or model hook.
    live = repeat_controls(context, spec_path)
    intent = write_json(staged['directory'] / '06-cas-intent.json', {
        'protocol': PROTOCOL, 'id': context['case_id'], 'state': 'cas_intent',
        'before_manifest_sha256': staged['ready_file']['sha256'],
        'approved_spec_sha256': approved_sha256, 'created': time.time()})
    artifact = digest([NAMESPACE, 'full-history', context['case_id'],
                       spec['expected']['rollout_file']['sha256']])
    receipt = {'protocol': PROTOCOL, 'id': context['case_id'], 'task_id': selected['task']['id'],
               'source_epoch': selected['task']['epoch'], 'closed_epoch': selected['task']['epoch'] + 1,
               'status': 'failed', 'outcome': 'known_effects_failed_reviewed',
               'original_input_requeued': False, 'original_failure_retained': True,
               'effects_zero': False, 'original_task_thread_and_turn_preserved': True,
               'original_checkpoint_retained_in_private_review': True,
               'native_session_state_changed': False, 'old_chunks_changed': False,
               'complete_artifact': artifact, 'full_rollout_sha256': context['native']['rollout_file']['sha256'],
               'before_manifest_sha256': staged['ready_file']['sha256'], 'cas_intent_sha256': intent['sha256']}
    try:
        db = sqlite3.connect('file:' + quote(str(auth['database']), safe='/') + '?mode=rw', uri=True,
                             isolation_level=None, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            current = selected_state(db, auth, spec)
            require(current == selected, 'task_or_session_cas_changed')
            repeat_controls(context, spec_path)
            require(read_checked(staged['archive_path'], private=True)[1] ==
                    staged['ready']['full_history_file'], 'staged_history_generation_changed')
            require(read_checked(staged['backup_path'], private=True)[1] ==
                    staged['ready']['backup_file'], 'staged_backup_generation_changed')
            require(not db.execute('SELECT 1 FROM session_chunks WHERE id=? LIMIT 1', (artifact,)).fetchone(),
                    'complete_history_namespace_conflict')
            review_table(db)
            parts = 0
            with open(str(staged['archive_path']), 'rb') as history:
                for block in iter(lambda: history.read(65536), b''):
                    db.execute('INSERT INTO session_chunks(id,part,body) VALUES(?,?,?)',
                               (artifact, parts, block))
                    parts += 1
            require(parts > 0, 'complete_history_empty')
            checkpoint = dict(selected['checkpoint'])
            checkpoint['side_effect_started'] = False
            checkpoint['owner_effect_review'] = {'protocol': PROTOCOL, 'id': context['case_id'],
                'outcome': 'known_effects_failed_reviewed', 'original_guard_was_true': True,
                'effects_zero': False, 'original_input_requeue_authorized': False}
            before = selected['session']
            changed = db.execute('''UPDATE native_sessions SET artifact=?,updated=?
                WHERE scope=? AND harness=? AND node=? AND state=? AND artifact IS ? AND updated=?''',
                (artifact, time.time(), before['scope'], before['harness'], before['node'], before['state'],
                 before['artifact'], before['updated'])).rowcount
            require(changed == 1, 'native_session_artifact_cas_failed')
            proof = {'spec_sha256': approved_sha256, 'native': context['native'], 'live': live,
                     'before_manifest': staged['ready'], 'cas_intent_file': intent}
            db.execute('INSERT INTO native_effect_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (context['case_id'], selected['task']['id'], selected['task']['epoch'],
                 selected['task']['epoch'] + 1, json.dumps(selected['task']),
                 json.dumps(selected['session']), selected['session']['artifact'], artifact,
                 json.dumps(proof), str(staged['backup_path']), staged['ready']['backup_file']['sha256'],
                 json.dumps(receipt), time.time()))
            changed = db.execute('''UPDATE tasks SET status=?,epoch=epoch+1,node=NULL,deadline=NULL,checkpoint=?
                WHERE id=? AND status=? AND epoch=? AND node IS ? AND checkpoint=? AND scope=?''',
                ('failed', json.dumps(checkpoint), selected['task']['id'], 'needs_review',
                 selected['task']['epoch'], selected['task']['node'], selected['task']['checkpoint'],
                 selected['task']['scope'])).rowcount
            require(changed == 1, 'original_task_cas_failed')
            # Still the same original turn/files just before the only commit.
            repeat_controls(context, spec_path)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        write_json(staged['directory'] / '07-receipt.json', dict(receipt, state='committed'))
        return dict(receipt, applied=True, outcome_unknown=False)
    except (OSError, ValueError, sqlite3.Error):
        # Includes lost local commit/receipt ACK. The durable SAME case ID and
        # full before snapshots survive. Never repeat CAS or create a new case.
        return {'protocol': PROTOCOL, 'id': context['case_id'], 'applied': None,
                'outcome_unknown': True, 'code': 'same_review_intent_requires_readback',
                'original_input_requeue_authorized': False, 'retry_authorized': False}


def review_status(spec_path):
    spec, version = load_spec(spec_path, allow_expired=True)
    auth = quota.authority(spec['worker_config'], spec['server_config'])
    case_id = digest([NAMESPACE, spec['expected']['task_id'], spec['expected']['epoch'],
                      spec['expected']['rollout_file']['sha256']])
    directory = private_directory(spec['review_root']) / case_id
    with contextlib.closing(quota.readonly(auth['database'])) as db:
        row = None
        if db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                       ('table', 'native_effect_reviews')).fetchone():
            row = db.execute('SELECT task_id,source_epoch,receipt FROM native_effect_reviews WHERE id=?',
                             (case_id,)).fetchone()
        if row:
            require(row['task_id'] == spec['expected']['task_id'] and row['source_epoch'] == spec['expected']['epoch'],
                    'review_record_identity_conflict')
            return dict(parse(row['receipt']), applied=True, outcome_unknown=False,
                        read_only=True, retry_authorized=False)
    phases = []
    if directory.exists():
        private_directory(directory)
        for name in ('00-intent.json', '05-backup-complete.json', '06-cas-intent.json', '07-receipt.json'):
            path = directory / name
            if path.exists():
                raw, unused = read_checked(path, private=True, limit=MAX_SPEC)
                state = parse(raw.decode())
                require(state.get('id') == case_id and state.get('protocol') == PROTOCOL,
                        'review_intent_identity_conflict')
                phases.append(state.get('state'))
    return {'protocol': PROTOCOL, 'id': case_id, 'read_only': True,
            'ledger_review_present': False, 'durable_phases': phases,
            'outcome_unknown': 'cas_intent' in phases, 'retry_authorized': False,
            'original_input_requeue_authorized': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec-file', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--status', action='store_true', help='Read SAME original review after a lost ACK; never reapply.')
    parser.add_argument('--approve-spec-sha256', help='Reviewer-approved exact PRIVATE spec digest; apply only.')
    args = parser.parse_args(argv)
    try:
        if args.status:
            result = review_status(args.spec_file)
        elif args.apply:
            require(isinstance(args.approve_spec_sha256, str)
                    and re.fullmatch(r'[0-9a-f]{64}', args.approve_spec_sha256),
                    'explicit_owner_spec_digest_approval_required')
            result = apply_review(args.spec_file, args.approve_spec_sha256)
        else:
            require(args.approve_spec_sha256 is None, 'approval_is_apply_only')
            context = inspect(args.spec_file)
            result = {'protocol': PROTOCOL, 'id': context['case_id'], 'applied': False, 'eligible': True,
                      'error_category': 'server_overloaded', 'effects_zero': False,
                      'complete_original_call_output_binding': True,
                      'original_input_requeue_authorized': False,
                      'private_spec_sha256': context['spec_file']['sha256']}
    except (ReviewDenied, quota.AuditDenied) as error:
        print(json.dumps({'applied': False, 'eligible': False, 'code': str(error)}))
        return 2
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, sqlite3.Error, subprocess.TimeoutExpired):
        print(json.dumps({'applied': False, 'eligible': False, 'code': 'review_input_or_observation_invalid'}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 3 if result.get('outcome_unknown') else 0


if __name__ == '__main__':
    raise SystemExit(main())
