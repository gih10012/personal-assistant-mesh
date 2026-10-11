"""Explicit temporary native free execution, not a Mesh Leader or API relay.

The owner installs this callback through ManagedProvider/ProviderRuntime. Their
durable admission journal remains authoritative; a second immutable local intent
protects even an accidental direct invocation. Unknown execution is never replayed.
Only the named private request is exposed to the genuine stock client. Native
tools remain stock, and no host-wide shell/files/network policy is changed.

This Linux example uses bubblewrap; other nodes may adapt the same contract.
It does not impersonate a client, create credentials, switch to paid models, or
import the continuous Codex/Pi Leader's history. Model HTTP request counts and
downstream proxy routes remain unknown unless independently measured.
"""
import base64
import hashlib
import json
import math
import os
import re
import signal
import socket
import stat
import subprocess
import threading
import time
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .egress_adapter import _root, _sync_directory
from .provider_runtime import _owner_bytes, _fingerprint
from .resources import _name


FORMAT = 'mesh-native-free-execution/1'
PRIVACY = ('public-synthetic', 'owner-approved-third-party')


class FreeExecutionError(ValueError):
    """Fixed diagnostic only; no private prompt/path or native error text."""


def _require(value, category):
    if not value:
        raise FreeExecutionError(category)


def _sha(value):
    _require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value),
             'free_execution_sha_required')
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')


def _private_json(path, maximum, expected=None):
    try:
        raw = _owner_bytes(path, maximum, 'free_execution_private_file_unavailable')
        if expected is not None:
            _require(hashlib.sha256(raw).hexdigest() == _sha(expected), 'free_execution_private_hash_changed')
        def unique(items):
            result = {}
            for key, value in items:
                _require(key not in result, 'free_execution_duplicate_key')
                result[key] = value
            return result
        value = json.loads(raw.decode('utf8'), object_pairs_hook=unique)
        _require(isinstance(value, dict), 'free_execution_private_json_invalid')
        return value
    except FreeExecutionError:
        raise
    except Exception:
        raise FreeExecutionError('free_execution_private_file_unavailable') from None


def _integer(value, maximum):
    return type(value) is int and 1 <= value <= maximum


def _seconds(value, maximum):
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and
            math.isfinite(value) and 0 < value <= maximum)


def _executable(path, expected):
    try:
        target = Path(path)
        _require(target.is_absolute() and '..' not in target.parts, 'free_execution_executable_invalid')
        # Executables may be system-owned; reject symlinks and writable paths.
        for ancestor in (target.parent,) + tuple(target.parent.parents):
            item = ancestor.lstat()
            _require(stat.S_ISDIR(item.st_mode) and item.st_uid in (0, os.getuid()) and
                     not item.st_mode & 0o022, 'free_execution_executable_invalid')
        item = target.lstat()
        _require(stat.S_ISREG(item.st_mode) and item.st_uid in (0, os.getuid()) and
                 not item.st_mode & 0o022 and item.st_mode & 0o111,
                 'free_execution_executable_invalid')
        with target.open('rb') as source:
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        _require(digest.hexdigest() == _sha(expected), 'free_execution_executable_changed')
        return str(target)
    except FreeExecutionError:
        raise
    except Exception:
        raise FreeExecutionError('free_execution_executable_invalid') from None


def _configuration(path, expected, runtime=True):
    config = _private_json(path, 65536, expected)
    fields = {'schema', 'provider', 'capability_id', 'capability_epoch', 'action',
              'artifact_root', 'request_root', 'client', 'client_sha256',
              'bubblewrap', 'bubblewrap_sha256', 'catalog', 'catalog_sha256',
              'model', 'physical_model_reference', 'effort', 'expires_at',
              'max_seconds', 'max_input_bytes', 'max_output_bytes', 'steps',
              'output_tokens', 'allowed_privacy', 'proxy_url'}
    _require(set(config) == fields and type(config['schema']) is int and config['schema'] == 1,
             'free_execution_owner_config_invalid')
    for key in ('provider', 'capability_id', 'action'):
        try:
            _name(config[key], key, 200)
        except Exception:
            raise FreeExecutionError('free_execution_owner_config_invalid') from None
    _require(_integer(config['capability_epoch'], 2147483647) and
             _seconds(config['max_seconds'], 300) and
             _integer(config['max_input_bytes'], 1048576) and
             _integer(config['max_output_bytes'], 8388608) and
             _integer(config['steps'], 12) and _integer(config['output_tokens'], 16384) and
             _seconds(config['expires_at'], 1e12), 'free_execution_owner_limits_invalid')
    _require(isinstance(config['allowed_privacy'], list) and config['allowed_privacy'] and
             all(item in PRIVACY for item in config['allowed_privacy']), 'free_execution_privacy_invalid')
    _require(isinstance(config['model'], str) and re.fullmatch(r'opencode/[a-z0-9][a-z0-9.-]*-free', config['model']) and
             isinstance(config['physical_model_reference'], str) and len(config['physical_model_reference']) <= 200 and
             config['effort'] in ('default', 'low', 'medium', 'high'), 'free_execution_model_invalid')
    proxy = config['proxy_url']
    _require(isinstance(proxy, str), 'free_execution_proxy_invalid')
    if proxy:
        try:
            parsed = urlsplit(proxy)
            _require(parsed.scheme in ('http', 'socks5h') and parsed.hostname and parsed.port and
                     not parsed.username and not parsed.password and not parsed.query and
                     not parsed.fragment and parsed.path in ('', '/'), 'free_execution_proxy_invalid')
        except ValueError:
            raise FreeExecutionError('free_execution_proxy_invalid') from None
    config['artifact_root'] = _root(config['artifact_root'])
    if not runtime:
        # Recovery reads are independent of today's binary, public catalog or
        # availability. Updating/removing a client must not hide past results.
        return config
    config['request_root'] = _root(config['request_root'])
    config['client'] = _executable(config['client'], config['client_sha256'])
    config['bubblewrap'] = _executable(config['bubblewrap'], config['bubblewrap_sha256'])
    _require(Path(config['client']).parts[:2] == ('/', 'usr'), 'free_execution_linux_client_mount_required')
    catalog = _private_json(config['catalog'], 16 * 1024 * 1024, config['catalog_sha256'])
    try:
        model = catalog['opencode']['models'][config['model'].split('/', 1)[1]]
        cost = model['cost']
        _require(all(type(cost.get(key)) in (int, float) and cost[key] == 0
                     for key in ('input', 'output', 'cache_read', 'cache_write')),
                 'free_execution_nonzero_catalog_cost')
        _require(model.get('tool_call') is True, 'free_execution_tool_model_required')
        _require(config['effort'] == 'default' or config['effort'] in model.get('variants', {}),
                 'free_execution_effort_not_in_catalog')
    except (KeyError, TypeError):
        raise FreeExecutionError('free_execution_catalog_model_invalid') from None
    return config


def _relative(value):
    _require(isinstance(value, str) and 0 < len(value) <= 512 and '\\' not in value and
             all(ord(char) >= 32 for char in value), 'free_execution_relative_path_invalid')
    path = PurePosixPath(value)
    _require(not path.is_absolute() and path.as_posix() == value and
             all(part not in ('', '.', '..', '.git') for part in path.parts),
             'free_execution_relative_path_invalid')
    return value


def _request(context, config):
    _require(isinstance(context, dict), 'free_execution_admitted_context_required')
    for key in ('provider', 'capability_id', 'capability_epoch', 'action'):
        _require(context.get(key) == config[key] and type(context.get(key)) is type(config[key]),
                 'free_execution_admitted_identity_mismatch')
    for key in ('operation_id', 'task_id', 'receipt_id', 'adapter_handle'):
        try:
            _name(context.get(key), key, 256)
        except Exception:
            raise FreeExecutionError('free_execution_admitted_context_required') from None
    _require(_integer(context.get('task_epoch'), 2147483647) and
             isinstance(context.get('invocation_nonce'), str) and
             re.fullmatch(r'[0-9a-f]{32}', context['invocation_nonce']),
             'free_execution_admitted_context_required')
    scope, workload = context.get('scope'), context.get('workload')
    _require(isinstance(scope, dict) and set(scope) == {'request_sha256', 'privacy'} and
             scope['privacy'] in config['allowed_privacy'], 'free_execution_scope_invalid')
    digest = _sha(scope['request_sha256'])
    _require(isinstance(workload, dict) and set(workload) == {'kind', 'timeout_seconds', 'max_output_bytes'} and
             workload['kind'] == 'native-free-execution' and
             _seconds(workload['timeout_seconds'], config['max_seconds']) and
             _integer(workload['max_output_bytes'], config['max_output_bytes']),
             'free_execution_workload_invalid')
    _require(time.time() < config['expires_at'], 'free_execution_owner_contract_expired')
    request = _private_json(config['request_root'] / (digest + '.json'),
                            config['max_input_bytes'] * 2 + 65536, digest)
    _require(set(request) == {'schema', 'prompt', 'inputs_base64', 'artifacts'} and
             type(request['schema']) is int and request['schema'] == 1 and
             isinstance(request['prompt'], str) and 0 < len(request['prompt'].encode('utf8')) <= 32768 and
             isinstance(request['inputs_base64'], dict) and len(request['inputs_base64']) <= 64 and
             isinstance(request['artifacts'], list) and len(request['artifacts']) <= 64,
             'free_execution_private_request_invalid')
    inputs = {}
    try:
        for name, encoded in request['inputs_base64'].items():
            inputs[_relative(name)] = base64.b64decode(encoded, validate=True)
    except Exception:
        raise FreeExecutionError('free_execution_private_inputs_invalid') from None
    _require(sum(map(len, inputs.values())) <= config['max_input_bytes'], 'free_execution_input_limit')
    for name in request['artifacts']:
        _relative(name)
    _require(len(set(request['artifacts'])) == len(request['artifacts']), 'free_execution_artifact_duplicate')
    return request, inputs


def _write(path, data):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _artifact(work, name, maximum):
    path = work / _relative(name)
    try:
        for ancestor in (path.parent,) + tuple(path.parent.parents):
            if ancestor == work.parent:
                break
            _require(stat.S_ISDIR(ancestor.lstat().st_mode), 'free_execution_artifact_symlink')
        item = path.lstat()
        _require(stat.S_ISREG(item.st_mode) and item.st_nlink == 1 and item.st_uid == os.getuid() and
                 item.st_size <= maximum, 'free_execution_artifact_invalid')
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            _require(os.fstat(stream.fileno()).st_ino == item.st_ino, 'free_execution_artifact_changed')
            data = stream.read(maximum + 1)
        _require(len(data) <= maximum and _fingerprint(path.lstat()) == _fingerprint(item),
                 'free_execution_artifact_changed')
        return {'name': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                'base64': base64.b64encode(data).decode('ascii')}
    except FreeExecutionError:
        raise
    except Exception:
        raise FreeExecutionError('free_execution_artifact_unavailable') from None


def _messages(messages, model):
    _require(isinstance(messages, list) and messages, 'free_execution_native_messages_invalid')
    assistants, tools = [], []
    for message in messages:
        _require(isinstance(message, dict) and isinstance(message.get('info'), dict) and
                 isinstance(message.get('parts'), list), 'free_execution_native_messages_invalid')
        info = message['info']
        _require(isinstance(info.get('id'), str), 'free_execution_native_messages_invalid')
        if info.get('role') != 'assistant':
            continue
        _require(isinstance(info.get('providerID'), str) and isinstance(info.get('modelID'), str) and
                 info['providerID'] + '/' + info['modelID'] == model and
                 type(info.get('cost')) in (int, float) and info['cost'] == 0 and
                 isinstance(info.get('time'), dict) and info['time'].get('completed') and
                 not info.get('error'), 'free_execution_native_outcome_unproved')
        assistants.append(info)
        for part in message['parts']:
            if part.get('type') == 'tool':
                _require(part.get('state', {}).get('status') == 'completed',
                         'free_execution_native_tool_unsettled')
                tools.append(part.get('tool'))
    _require(assistants, 'free_execution_native_outcome_unproved')
    # A CLI exit 0 alone is not inference success or a settled tool result.
    return {'assistant_steps': len(assistants), 'completed_tools': tools,
            'reported_cost_minor': 0, 'messages_sha256': hashlib.sha256(_canonical(messages)).hexdigest()}


class _NativeStock:
    """Owned genuine client processes. No fabricated identity or model wire API."""
    def __init__(self, config, root, work, deadline):
        self.config, self.root, self.work, self.deadline = config, root, work, deadline
        home = os.environ.get('HOME')
        _require(home and Path(home).is_absolute() and home not in ('/', '/usr', '/tmp'),
                 'free_execution_original_home_invalid')
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'SHELL': '/bin/bash', 'HOME': home,
                    'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}
        if 'CODEX_HOME' in os.environ:
            self.env['CODEX_HOME'] = os.environ['CODEX_HOME']
        for key, directory in (('XDG_DATA_HOME', 'data'), ('XDG_CONFIG_HOME', 'config'),
                               ('XDG_CACHE_HOME', 'cache'), ('XDG_STATE_HOME', 'state'), ('TMPDIR', 'tmp')):
            (root / directory).mkdir(mode=0o700)
            self.env[key] = str(root / directory)
        self.env.update(OPENCODE_CONFIG_DIR=str(root / 'config'), OPENCODE_CONFIG=str(root / 'opencode.json'),
            OPENCODE_MODELS_PATH=str(root / 'catalog.json'), OPENCODE_DISABLE_MODELS_FETCH='true',
            OPENCODE_DISABLE_PROJECT_CONFIG='true', OPENCODE_DISABLE_AUTOUPDATE='true',
            OPENCODE_DISABLE_PRUNE='true', OPENCODE_DISABLE_TERMINAL_TITLE='true',
            OPENCODE_DISABLE_LSP_DOWNLOAD='true', OPENCODE_DISABLE_AUTOCOMPACT='true',
            OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER='true',
            OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=str(config['output_tokens']))
        if config['proxy_url']:
            self.env.update(HTTP_PROXY=config['proxy_url'], HTTPS_PROXY=config['proxy_url'])
        bwrap = [config['bubblewrap'], '--unshare-user', '--unshare-pid', '--unshare-ipc',
                 '--die-with-parent', '--new-session', '--ro-bind', '/usr', '/usr',
                 '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib', '/lib',
                 '--symlink', 'usr/lib', '/lib64', '--proc', '/proc', '--dev', '/dev',
                 '--tmpfs', '/tmp', '--dir', str(Path(home).parent), '--tmpfs', home,
                 '--dir', '/etc', '--ro-bind', '/etc/ssl', '/etc/ssl']
        for path in ('/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/passwd', '/etc/group', '/etc/ld.so.cache'):
            if Path(path).is_file():
                bwrap += ['--ro-bind', path, path]
        bwrap += ['--bind', str(root), str(root),
                  '--ro-bind', str(root / 'opencode.json'), str(root / 'opencode.json'),
                  '--ro-bind', str(root / 'catalog.json'), str(root / 'catalog.json'), '--chdir', str(work)]
        self.command = bwrap + [config['client'], '--pure']
        self.processes, self.sinks = [], []
        self.events, self.monitor_fault = [], False
        self.observer_stop, self.observer_ready = threading.Event(), threading.Event()
        self.event_stream, self.observer = None, None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        _require(remaining > 0, 'free_execution_timeout_unknown')
        return remaining

    def _spawn(self, arguments, label):
        def sink(name):
            descriptor = os.open(str(self.root / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            return os.fdopen(descriptor, 'wb')
        out, err = sink(label + '.stdout'), sink(label + '.stderr')
        self.sinks.extend((out, err))
        proc = subprocess.Popen(self.command + arguments, cwd=str(self.work), env=self.env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        self.processes.append(proc)
        return proc

    def run(self, arguments, label):
        proc = self._spawn(arguments, label)
        try:
            code = proc.wait(timeout=self.remaining())
        except subprocess.TimeoutExpired:
            raise FreeExecutionError('free_execution_timeout_unknown') from None
        _require(code == 0, 'free_execution_native_exit_unknown')
        output = self.root / (label + '.stdout')
        _require(output.stat().st_size <= 24 * 1024 * 1024, 'free_execution_native_output_limit')
        return output.read_bytes()

    def api(self, path, value=None):
        request = urllib.request.Request(self.base + path,
            data=None if value is None else _canonical(value), headers={'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=min(5, self.remaining())) as response:
            raw = response.read(24 * 1024 * 1024 + 1)
        _require(len(raw) <= 24 * 1024 * 1024, 'free_execution_native_output_limit')
        return json.loads(raw)

    def execute(self, request):
        # Probe only the actual namespace with zero model traffic, before serve.
        probe = ('import os,pathlib; p=pathlib.Path; h=p(os.environ["HOME"]); '
                 'assert not (h/".local/share/opencode/auth.json").exists(); '
                 'assert not (h/".config/opencode").exists(); '
                 'assert not (h/".codex/auth.json").exists(); '
                 'assert not p('+repr(str(self.root / 'data/opencode/auth.json'))+').exists(); '
                 'assert p.cwd()==p('+repr(str(self.work))+')')
        proc = subprocess.Popen(self.command[:-2] + ['/usr/bin/python3', '-c', probe],
            cwd=str(self.work), env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.processes.append(proc)
        _require(proc.wait(timeout=min(10, self.remaining())) == 0, 'free_execution_namespace_unproved')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        self.base = 'http://127.0.0.1:' + str(port)
        server = self._spawn(['serve', '--hostname', '127.0.0.1', '--port', str(port)], 'server')
        ready_deadline = min(self.deadline, time.monotonic() + 25)
        while True:
            _require(server.poll() is None, 'free_execution_server_exited')
            try:
                self.api('/global/health')
                break
            except Exception:
                _require(time.monotonic() < ready_deadline, 'free_execution_server_unavailable')
                time.sleep(.1)
        session = self.api('/session', {'title': 'Mesh managed external execution'})['id']
        _require(isinstance(session, str) and re.fullmatch(r'ses_[A-Za-z0-9]+', session),
                 'free_execution_native_session_invalid')
        _write(self.root / 'native-session.json', _canonical({'session_id': session}))
        def monitor():
            try:
                with self.opener.open(self.base + '/event', timeout=self.remaining()) as stream:
                    self.event_stream = stream
                    self.observer_ready.set()
                    for line in stream:
                        if self.observer_stop.is_set():
                            break
                        if not line.startswith(b'data: '):
                            continue
                        event = json.loads(line[6:])
                        self.events.append(event)
                        props = event.get('properties', {})
                        if event.get('type') == 'session.status' and props.get('sessionID') == session and props.get('status', {}).get('type') == 'retry':
                            self.api('/session/' + session + '/abort', {})
                            self.monitor_fault = True
                            break
            except Exception:
                if not self.observer_stop.is_set():
                    self.monitor_fault = True
        self.observer = threading.Thread(target=monitor, daemon=True)
        self.observer.start()
        _require(self.observer_ready.wait(min(5, self.remaining())), 'free_execution_observer_unavailable')
        args = ['run', '--attach', self.base, '--session', session, '--model', self.config['model'],
                '--agent', 'build', '--format', 'json']
        if self.config['effort'] != 'default':
            args += ['--variant', self.config['effort']]
        args += ['--', request['prompt']]
        self.run(args, 'turn')
        messages = self.api('/session/' + session + '/message')
        _write(self.root / 'native-messages.json', _canonical(messages))
        _require(not self.monitor_fault and not any(event.get('type') == 'session.error' for event in self.events),
                 'free_execution_native_observation_unknown')
        return session, messages

    def stop(self):
        self.observer_stop.set()
        for proc in reversed(self.processes):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)
        if self.event_stream is not None:
            self.event_stream.close()
        if self.observer is not None:
            self.observer.join(timeout=2)
        for stream in self.sinks:
            stream.close()
        return all(proc.poll() is not None for proc in self.processes) and (self.observer is None or not self.observer.is_alive())

    def export(self, session):
        raw = self.run(['export', session], 'export')
        return json.loads(raw)


def execute(context, owner_config_path, owner_config_sha256):
    """One bounded admitted callback; no registration, retry or paid fallback.

    The request is a private SHA-addressed immutable JSON, not host paths or
    executable owner configuration from model arguments. Public evidence carries
    hashes and availability/cost assumptions; prompts/tools/full native exports
    remain private. Exceptions intentionally leave ManagedProvider unknown.
    """
    config = _configuration(owner_config_path, owner_config_sha256)
    request, inputs = _request(context, config)
    identity = hashlib.sha256(_canonical([config['provider'], context['operation_id'], context['capability_id']])).hexdigest()
    root = config['artifact_root'] / ('free-' + identity)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        raise FreeExecutionError('free_execution_original_invocation_no_replay') from None
    _sync_directory(config['artifact_root'])
    _write(root / 'intent.json', _canonical({'context': context, 'model': config['model'],
        'started_at': time.time(), 'automatic_replay': False, 'wire_model_post_count': None}))
    work = root / 'work'
    work.mkdir(mode=0o700)
    # The real repository root makes native project discovery deterministic.
    (work / '.git').mkdir(mode=0o700)
    _write(work / '.git/HEAD', b'ref: refs/heads/main\n')
    (work / '.git/objects').mkdir(mode=0o700)
    (work / '.git/refs').mkdir(mode=0o700)
    for name, data in inputs.items():
        path = work / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write(path, data)
    catalog = _owner_bytes(config['catalog'], 16 * 1024 * 1024, 'free_execution_catalog_unavailable')
    _require(hashlib.sha256(catalog).hexdigest() == config['catalog_sha256'], 'free_execution_catalog_changed')
    _write(root / 'catalog.json', catalog)
    native_config = {'model': config['model'], 'small_model': config['model'], 'enabled_providers': ['opencode'],
        'default_agent': 'build', 'share': 'disabled', 'autoupdate': False, 'plugin': [], 'mcp': {}, 'instructions': [],
        'compaction': {'auto': False, 'prune': False},
        'provider': {'opencode': {'whitelist': [config['model'].split('/', 1)[1]], 'options': {'timeout': 60000}}},
        'agent': {'build': {'steps': config['steps']}, 'title': {'disable': True},
                  'summary': {'disable': True}, 'compaction': {'disable': True}}}
    _write(root / 'opencode.json', _canonical(native_config))
    began = time.monotonic()
    timeout = min(context['workload']['timeout_seconds'], config['expires_at'] - time.time())
    _require(timeout > 0, 'free_execution_owner_contract_expired')
    backend = _NativeStock(config, root, work, began + timeout)
    try:
        session, messages = backend.execute(request)
        observed = _messages(messages, config['model'])
    finally:
        quiescent = backend.stop()
        _write(root / 'events.json', _canonical(backend.events))
    _require(quiescent, 'free_execution_resource_quiescence_unproved')
    try:
        exported = backend.export(session)
    finally:
        export_quiescent = backend.stop()
    _require(export_quiescent and exported.get('info', {}).get('id') == session and
             type(exported.get('info', {}).get('cost')) in (int, float) and exported['info']['cost'] == 0 and
             exported.get('messages') == messages, 'free_execution_export_mismatch')
    _require(not (root / 'data/opencode/auth.json').exists(), 'free_execution_anonymous_auth_changed')
    _write(root / 'native-export.json', _canonical(exported))
    remaining = context['workload']['max_output_bytes']
    artifacts = []
    for name in request['artifacts']:
        artifact = _artifact(work, name, remaining)
        remaining -= artifact['bytes']
        artifacts.append(artifact)
    evidence = dict(observed, model=config['model'], physical_model_reference=config['physical_model_reference'],
        effort=config['effort'], exact_quality_variant_verified=False, cash_cost_minor=0,
        cash_cost_basis='zero-price-public-catalog-and-native-records-not-billing-audit',
        privacy=context['scope']['privacy'], availability_expires_at=config['expires_at'],
        artifact_count=len(artifacts), artifact_bytes=sum(item['bytes'] for item in artifacts),
        original_native_export_matches=True, owned_processes_reaped=True,
        wire_model_post_count=None, wire_model_post_count_instrumented=False,
        downstream_egress_verified=False, automatic_replay=False, native_tools_intercepted=False,
        leader_replaced=False, execution_verification='provider_reported',
        external_side_effect_closure_verified=False,
        duration_seconds=round(time.monotonic() - began, 3))
    result = {'format': FORMAT, 'context': context, 'session_id': session, 'observation': evidence,
              'native_messages': messages, 'native_export': exported, 'artifacts': artifacts}
    raw = _canonical(result)
    _require(len(raw) <= 24 * 1024 * 1024, 'free_execution_result_limit')
    _write(root / 'result.json', raw)
    _sync_directory(root)
    digest = hashlib.sha256(raw).hexdigest()
    return {'outcome': 'completed', 'resource_quiescent': True,
            'result_reference': 'mesh-native-free-execution:sha256:' + digest,
            'evidence': dict(evidence, artifact_id=root.name, artifact_sha256=digest)}


def read_result(owner_config_path, owner_config_sha256, artifact_id, artifact_sha256):
    """Pure private result read: never launches a client or retries a prompt."""
    config = _configuration(owner_config_path, owner_config_sha256, runtime=False)
    _require(isinstance(artifact_id, str) and re.fullmatch(r'free-[0-9a-f]{64}', artifact_id),
             'free_execution_artifact_identity_invalid')
    value = _private_json(config['artifact_root'] / artifact_id / 'result.json',
                          24 * 1024 * 1024, artifact_sha256)
    _require(value.get('format') == FORMAT, 'free_execution_artifact_invalid')
    try:
        _require(value['native_export']['messages'] == value['native_messages'] and
                 value['native_export']['info']['id'] == value['session_id'], 'free_execution_artifact_invalid')
        _require(hashlib.sha256(_canonical(value['native_messages'])).hexdigest() ==
                 value['observation']['messages_sha256'], 'free_execution_artifact_invalid')
        for artifact in value['artifacts']:
            _relative(artifact['name'])
            data = base64.b64decode(artifact['base64'], validate=True)
            _require(len(data) == artifact['bytes'] and hashlib.sha256(data).hexdigest() == artifact['sha256'],
                     'free_execution_artifact_invalid')
    except (KeyError, TypeError, ValueError):
        raise FreeExecutionError('free_execution_artifact_invalid') from None
    return value


def describe(owner_config_path, owner_config_sha256):
    """Owner metadata candidate, not auto-enrollment or live execution proof."""
    config = _configuration(owner_config_path, owner_config_sha256)
    return {'kind': 'native.free.execution', 'harness': 'genuine-opencode-stock-build',
            'leader_role': False, 'provider': config['provider'], 'capability_id': config['capability_id'],
            'capability_epoch': config['capability_epoch'], 'action': config['action'],
            'model': config['model'], 'physical_model_reference': config['physical_model_reference'],
            'effort': config['effort'], 'cash_cost_minor': 0, 'cash_cost_basis': 'public-catalog-owner-reference',
            'quota_guaranteed': False, 'quality_variant_verified': False,
            'allowed_payload_privacy': list(config['allowed_privacy']), 'availability_expires_at': config['expires_at'],
            'availability': 'unverified-candidate' if time.time() < config['expires_at'] else 'expired',
            'host_native_features_restricted': False, 'automatic_registration': False,
            'native_tools': 'stock-build-unmodified', 'platform_example': 'Linux-bubblewrap',
            'wire_model_post_count_instrumented': False, 'downstream_egress_verified': False}
