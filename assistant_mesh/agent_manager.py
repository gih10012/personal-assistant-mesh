"""Owner-local native agent lifecycle: no new Mesh node, service or model key.

Only this optional manager's process groups are stopped. External Leader/Claw
profiles are next-explicit-turn inputs, never a claim of current-turn mutation.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time


FORMAT = 'mesh-agent-profiles/v1'
ACTIVE = frozenset(('launch_intent', 'running', 'stopping'))
MAX_PROMPT = 1024 * 1024


class AgentError(ValueError):
    pass


def _name(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is None:
        raise AgentError('agent_identifier_invalid')
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf8')


def _packet(connection):
    raw = b''
    while b'\n' not in raw and len(raw) <= 4096:
        data = connection.recv(4096 - len(raw) + 1)
        if not data:
            break
        raw += data
    if len(raw) > 4096 or not raw.endswith(b'\n'):
        raise AgentError('agent_control_packet_invalid')
    return json.loads(raw.decode('utf8'))


def _private_dir(value, create=False):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or os.path.realpath(str(path)) != str(path):
        raise AgentError('agent_private_directory_required')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    entry = path.lstat()
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o077:
        raise AgentError('agent_private_directory_required')
    return path


def _private_bytes(path, maximum=32 * 1024 * 1024):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as handle:
        entry = os.fstat(handle.fileno())
        if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o077:
            raise AgentError('agent_private_file_required')
        data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise AgentError('agent_private_file_too_large')
        return data


def _write(path, value):
    raw = _canonical(value) if not isinstance(value, bytes) else value
    descriptor, temporary = tempfile.mkstemp(prefix='.agent-write-', dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_profile(path, agent_id):
    """Readonly loader for reasoning.make_codex's per-agent next-turn binding."""
    _name(agent_id)
    try:
        value = json.loads(_private_bytes(path).decode('utf8'))
    except FileNotFoundError:
        return None
    if value.get('format') != FORMAT or not isinstance(value.get('agents'), dict):
        raise AgentError('agent_profiles_format_invalid')
    agent = value['agents'].get(agent_id)
    if agent is None:
        return None
    return {key: agent[key] for key in ('revision', 'model', 'reasoning_effort')}


def _platform():
    if not (sys.platform.startswith('linux') and hasattr(socket, 'SO_PEERCRED')
            and hasattr(os, 'waitid') and hasattr(os, 'WNOWAIT')):
        raise AgentError('agent_supervisor_platform_unsupported')


class AgentManager:
    def __init__(self, config_path, validator=None):
        _platform()
        self.config_path = str(Path(config_path).absolute())
        raw = _private_bytes(self.config_path, 65536)
        self.config_sha256 = hashlib.sha256(raw).hexdigest()
        self.config = json.loads(raw.decode('utf8'))
        allowed = {'format', 'state_dir', 'runtime_dir', 'runtime', 'external_agents'}
        if (set(self.config) != allowed or self.config['format'] != 'mesh-agent-manager/v1'
                or not isinstance(self.config['runtime'], dict)
                or not isinstance(self.config['external_agents'], dict)):
            raise AgentError('agent_manager_config_invalid')
        for identity, binding in self.config['external_agents'].items():
            _name(identity)
            if not isinstance(binding, dict) or set(binding) - {'runtime', 'model', 'reasoning_effort'}:
                raise AgentError('agent_external_binding_invalid')
        self.root = _private_dir(self.config['state_dir'], create=True)
        self.runtime_dir = _private_dir(self.config['runtime_dir'], create=True)
        self.profiles_path = self.root / 'profiles.json'
        self.validator = validator or self._native_validate

    @contextlib.contextmanager
    def _locked(self):
        import fcntl
        descriptor = os.open(str(self.root / 'lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'r+b') as lock:
            entry = os.fstat(lock.fileno())
            if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o077:
                raise AgentError('agent_private_lock_required')
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    value = json.loads(_private_bytes(self.profiles_path).decode('utf8'))
                except FileNotFoundError:
                    value = {'format': FORMAT, 'agents': {}, 'requests': {}}
                if value.get('format') != FORMAT:
                    raise AgentError('agent_profiles_format_invalid')
                yield value
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _save(self, value):
        _write(self.profiles_path, value)

    def _runtime(self, identity):
        runtime = dict(self.config['runtime'])
        runtime.update(self.config['external_agents'].get(identity, {}).get('runtime', {}))
        if (set(runtime) - {'executable', 'auth_home', 'network_env_file'}
                or not {'executable', 'auth_home'} <= set(runtime)):
            raise AgentError('agent_native_runtime_invalid')
        executable = Path(runtime['executable'])
        if not executable.is_absolute() or executable.is_symlink() or not os.access(str(executable), os.X_OK):
            raise AgentError('agent_pinned_native_binary_required')
        # This first thin adapter is the genuine Linux native Codex executable,
        # not an unreviewed shell wrapper with login/history/paid-route effects.
        with executable.open('rb') as handle:
            if handle.read(4) != b'\x7fELF':
                raise AgentError('agent_pinned_native_binary_required')
        return runtime

    def _native_validate(self, identity, model, effort):
        from .codex import Codex
        from .reasoning import read_model_catalog, validate_selection
        config = dict(self._runtime(identity), strict_auth_home=True,
                      native_memories=False, native_transport='stdio')
        with Codex(config) as observer:
            return validate_selection(read_model_catalog(observer.rpc), model, effort)

    def _known(self, value, identity):
        agent = value['agents'].get(identity)
        if agent is None and identity in self.config['external_agents']:
            binding = self.config['external_agents'][identity]
            return {'kind': 'external', 'revision': 0,
                    'model': binding.get('model'),
                    'reasoning_effort': binding.get('reasoning_effort')}
        if agent is None:
            raise AgentError('agent_not_found')
        return agent

    def configure(self, request_id, agent_id, model=None, effort=None):
        _name(request_id), _name(agent_id)
        fingerprint = hashlib.sha256(_canonical({'agent_id': agent_id, 'operation': 'configure',
            'model': model, 'reasoning_effort': effort})).hexdigest()
        with self._locked() as value:
            agent = self._known(value, agent_id)
            revision = agent['revision']
            selection = {'model': model if model is not None else agent.get('model'),
                         'reasoning_effort': effort if effort is not None else agent.get('reasoning_effort')}
            previous = value['requests'].get(request_id)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise AgentError('agent_request_id_conflict')
                return dict(previous, replayed=True)
        receipt = self.validator(agent_id, selection['model'], selection['reasoning_effort'])
        with self._locked() as value:
            previous = value['requests'].get(request_id)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise AgentError('agent_request_id_conflict')
                return dict(previous, replayed=True)
            agent = dict(self._known(value, agent_id))
            if agent['revision'] != revision:
                raise AgentError('agent_profile_changed_retry_new_configuration_intent')
            agent.update(selection, revision=revision + 1)
            value['agents'][agent_id] = agent
            result = {'operation': 'configure', 'agent_id': agent_id,
                      'request_id': request_id, 'fingerprint': fingerprint,
                      'profile_revision': agent['revision'], 'selection': selection,
                      'validation': receipt, 'status': 'configured',
                      'effective': 'next_explicit_turn', 'effective_verified': False,
                      'configured_at': time.time(), 'replayed': False}
            value['requests'][request_id] = result
            self._save(value)
            return result

    def spawn(self, request_id, agent_id, cwd, prompt, model, effort):
        return self._launch('spawn', request_id, agent_id, cwd, prompt, model, effort)

    def resume(self, request_id, agent_id, prompt):
        return self._launch('resume', request_id, agent_id, None, prompt, None, None)

    def _launch(self, operation, request_id, agent_id, cwd, prompt, model, effort):
        _name(request_id), _name(agent_id)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf8')) > MAX_PROMPT:
            raise AgentError('agent_prompt_invalid')
        if cwd is not None:
            path = Path(cwd)
            if not path.is_absolute() or not path.is_dir():
                raise AgentError('agent_cwd_invalid')
            cwd = str(path.resolve())
        fingerprint = hashlib.sha256(_canonical({'operation': operation, 'agent_id': agent_id,
            'cwd': cwd, 'prompt': prompt, 'model': model, 'reasoning_effort': effort})).hexdigest()
        with self._locked() as value:
            previous = value['requests'].get(request_id)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise AgentError('agent_request_id_conflict')
                return self._status(value, agent_id, replayed=True, request_id=request_id)
            if agent_id in self.config['external_agents']:
                raise AgentError('agent_externally_managed')
            if operation == 'spawn':
                if agent_id in value['agents']:
                    raise AgentError('agent_already_exists')
                agent = {'kind': 'managed', 'revision': 1, 'cwd': cwd,
                         'model': model, 'reasoning_effort': effort, 'session_id': None}
            else:
                agent = dict(self._known(value, agent_id))
                old = value['requests'][agent['current_request_id']]
                if old['status'] != 'completed' or not agent.get('session_id'):
                    raise AgentError('agent_prior_turn_not_safely_completed')
                cwd, model, effort = agent['cwd'], agent['model'], agent['reasoning_effort']
            revision = agent['revision']
        receipt = self.validator(agent_id, model, effort)
        runtime = self._runtime(agent_id)
        request_root = self.root / hashlib.sha256(request_id.encode('utf8')).hexdigest()
        request_root.mkdir(mode=0o700, exist_ok=True)
        _private_dir(str(request_root))
        control = self.runtime_dir / (hashlib.sha256(request_id.encode('utf8')).hexdigest()[:32] + '.sock')
        if len(os.fsencode(str(control))) >= 104:
            raise AgentError('agent_control_socket_path_too_long')
        with self._locked() as value:
            previous = value['requests'].get(request_id)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise AgentError('agent_request_id_conflict')
                return self._status(value, agent_id, replayed=True, request_id=request_id)
            if operation == 'spawn' and agent_id in value['agents']:
                raise AgentError('agent_already_exists')
            if operation == 'resume' and self._known(value, agent_id)['revision'] != revision:
                raise AgentError('agent_profile_changed_before_launch')
            # Persist the original intent before launching anything. Crash or
            # observation loss leaves it unknown: same ID only queries it.
            _write(request_root / 'prompt.txt', prompt.encode('utf8'))
            record = {'operation': operation, 'agent_id': agent_id, 'request_id': request_id,
                      'fingerprint': fingerprint, 'profile_revision': revision,
                      'selection': {'model': model, 'reasoning_effort': effort},
                      'validation': receipt, 'runtime': runtime, 'cwd': cwd,
                      'session_id': agent.get('session_id'), 'status': 'launch_intent',
                      'outcome_unknown': True, 'effective_verified': False,
                      'control_socket': str(control), 'control_token': os.urandom(32).hex(),
                      'request_root': str(request_root), 'created_at': time.time(),
                      'manager_config_sha256': self.config_sha256}
            value['requests'][request_id] = record
            agent['current_request_id'] = request_id
            value['agents'][agent_id] = agent
            self._save(value)
            with (request_root / 'supervisor.log').open('ab') as log:
                os.chmod(str(request_root / 'supervisor.log'), 0o600)
                process = subprocess.Popen([sys.executable, '-m', 'assistant_mesh.agent_manager',
                    '--config', self.config_path, '_supervise', '--request-id', request_id],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    cwd=os.getcwd(), start_new_session=True, close_fds=True)
            record['supervisor_pid'] = process.pid
            self._save(value)
            return self._status(value, agent_id)

    def _status(self, value, agent_id, replayed=False, request_id=None):
        agent = self._known(value, agent_id)
        result = {'agent_id': agent_id, 'kind': agent['kind'],
                  'profile': {key: agent.get(key) for key in ('revision', 'model', 'reasoning_effort')},
                  'profile_effective': 'next_explicit_turn', 'effective_verified': False,
                  'session_id': agent.get('session_id'), 'replayed': replayed}
        current = request_id or agent.get('current_request_id')
        if not current:
            result['status'] = 'externally_managed'
            return result
        record = value['requests'][current]
        result.update({key: record.get(key) for key in ('request_id', 'session_id', 'status', 'selection',
            'profile_revision', 'supervisor_pid', 'native_pid', 'returncode', 'turn_completed',
            'events_eof', 'outcome_unknown', 'owned_native_reaped', 'owned_group_children_reaped',
            'detached_or_remote_effects_settled', 'created_at', 'finished_at')})
        return result

    def status(self, agent_id=None, request_id=None):
        with self._locked() as value:
            if request_id is not None:
                record = value['requests'].get(_name(request_id))
                if record is None:
                    raise AgentError('agent_request_not_found')
                if agent_id is not None and agent_id != record['agent_id']:
                    raise AgentError('agent_request_binding_mismatch')
                agent_id = record['agent_id']
            if agent_id is None:
                return {'agents': [self._status(value, identity) for identity in sorted(
                    set(value['agents']) | set(self.config['external_agents']))]}
            result = self._status(value, _name(agent_id), request_id=request_id)
            if result['status'] in ACTIVE:
                record = dict(value['requests'][result['request_id']])
            else:
                return result
        try:
            observation = self._control(record, 'status')
            result['supervisor_reachable'] = observation.get('supervisor_reachable') is True
        except (OSError, AgentError, ValueError):
            result['supervisor_reachable'] = False
            result['outcome_unknown'] = True
        return result

    def _control(self, record, action):
        path = Path(record['control_socket'])
        entry = path.lstat()
        if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o077:
            raise AgentError('agent_owned_supervisor_unverified')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(path))
            pid, uid, unused = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid() or pid != record.get('supervisor_pid'):
                raise AgentError('agent_owned_supervisor_unverified')
            connection.sendall(_canonical({'action': action, 'token': record['control_token']}) + b'\n')
            return _packet(connection)

    def stop(self, agent_id):
        with self._locked() as value:
            agent = self._known(value, _name(agent_id))
            if agent['kind'] != 'managed':
                return {'agent_id': agent_id, 'status': 'externally_managed', 'stopped': False}
            record = dict(value['requests'][agent['current_request_id']])
            if record['status'] not in ACTIVE:
                return self._status(value, agent_id)
        # Never signal a numeric PID from disk. Only the live, nonce-authenticated
        # original supervisor is allowed to stop its exact owned process group.
        try:
            result = self._control(record, 'stop')
        except (OSError, ValueError):
            raise AgentError('agent_stop_outcome_unknown_query_original_request') from None
        return dict(result, agent_id=agent_id, request_id=record['request_id'])

    def _update(self, request_id, **fields):
        with self._locked() as value:
            record = value['requests'][request_id]
            record.update(fields)
            identity = fields.get('session_id')
            agent = value['agents'][record['agent_id']]
            if identity and agent.get('current_request_id') == request_id:
                agent['session_id'] = identity
            self._save(value)


def _child_env(runtime):
    from .config import discover_codex_auth, private_json
    home = discover_codex_auth(runtime['auth_home'], strict=True)
    env = os.environ.copy()
    # Select only this child profile; do not copy auth or change the host shell.
    env['CODEX_HOME'] = home
    env['CODEX_HOME_OVERRIDE'] = home
    env.pop('OPENAI_API_KEY', None)
    env.pop('CODEX_API_KEY', None)
    if runtime.get('network_env_file'):
        network = private_json(runtime['network_env_file'])
        allowed = {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                   'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}
        if set(network) - allowed or not all(isinstance(x, str) for x in network.values()):
            raise AgentError('agent_network_environment_invalid')
        env.update(network)
    return env


def _command(record):
    command = [record['runtime']['executable'], 'exec']
    if record['operation'] == 'resume':
        command.append('resume')
    command += ['--json', '--skip-git-repo-check', '--model', record['selection']['model'],
                '-c', 'model_provider="openai"', '-c', 'approval_policy="never"',
                '-c', 'model_reasoning_effort=' + json.dumps(record['selection']['reasoning_effort']),
                '--dangerously-bypass-approvals-and-sandbox',
                '-o', str(Path(record['request_root']) / 'last-message.txt')]
    if record['operation'] == 'spawn':
        command += ['--cd', record['cwd']]
    else:
        command.append(record['session_id'])
    return command + ['-']


def supervise(manager, request_id):
    """Short-lived owned runner, not a network daemon or another Mesh member."""
    with manager._locked() as value:
        record = dict(value['requests'][_name(request_id)])
        if (record['status'] != 'launch_intent' or record.get('supervisor_pid') != os.getpid()
                or record['manager_config_sha256'] != manager.config_sha256):
            raise AgentError('agent_supervisor_intent_mismatch')
    root = Path(record['request_root'])
    env, command = _child_env(record['runtime']), _command(record)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    process = None
    stopped = False
    completed = False
    eof = False
    session_id = record.get('session_id')
    error = None
    path = record['control_socket']
    socket_identity = None
    try:
        # Adopt only to reap original-group orphaned tool children. Detached
        # agents in other groups are not signalled or consumed by this cleanup.
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise AgentError('agent_owned_child_reaping_unsupported')
        listener.bind(path)  # Never unlink an incumbent socket to take ownership.
        os.chmod(path, 0o600)
        socket_entry = os.lstat(path)
        socket_identity = (socket_entry.st_dev, socket_entry.st_ino)
        listener.listen(4)
        listener.settimeout(.2)
        with (root / 'events.jsonl').open('wb') as output, (root / 'stderr.log').open('wb') as stderr:
            os.chmod(str(root / 'events.jsonl'), 0o600)
            os.chmod(str(root / 'stderr.log'), 0o600)
            with (root / 'prompt.txt').open('rb') as prompt:
                process = subprocess.Popen(command, cwd=record['cwd'], env=env,
                    stdin=prompt, stdout=subprocess.PIPE, stderr=stderr,
                    start_new_session=True, close_fds=True)
            manager._update(request_id, status='running', native_pid=process.pid,
                            cli_selection_sent=True, effective_verified=False)
            buffer = b''
            oversized_line = False
            stop_deadline = None
            dead_at = None
            while True:
                ready, unused, unused = select.select([listener] + ([] if eof else [process.stdout]), [], [], .1)
                if listener in ready:
                    connection, unused = listener.accept()
                    with connection:
                        try:
                            connection.settimeout(1)
                            pid, uid, unused = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                            message = _packet(connection)
                            valid = (uid == os.getuid() and isinstance(message, dict)
                                     and message.get('token') == record['control_token'])
                            action = message.get('action') if valid else None
                            if action == 'stop' and not stopped:
                                stopped = True
                                stop_deadline = time.monotonic() + 5
                                try:
                                    os.killpg(process.pid, signal.SIGTERM)
                                except ProcessLookupError:
                                    pass
                                manager._update(request_id, status='stopping', outcome_unknown=True)
                            connection.sendall(_canonical({'supervisor_reachable': valid,
                                'stop_accepted': action == 'stop', 'status': 'stopping' if stopped else 'running'}) + b'\n')
                        except (OSError, ValueError):
                            pass  # A malformed status client must not abort native work.
                if not eof and process.stdout in ready:
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        eof = True
                    else:
                        output.write(data)
                        output.flush()
                        buffer += data
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            if oversized_line or len(line) > 4 * MAX_PROMPT:
                                oversized_line = False
                                continue
                            try:
                                event = json.loads(line.decode('utf8'))
                            except (ValueError, UnicodeError):
                                continue
                            if event.get('type') == 'thread.started' and event.get('thread_id'):
                                observed = event['thread_id']
                                if session_id is not None and observed != session_id:
                                    raise AgentError('agent_original_session_mismatch')
                                session_id = observed
                                manager._update(request_id, session_id=session_id)
                            if event.get('type') == 'turn.completed':
                                completed = True
                        if len(buffer) > 4 * MAX_PROMPT:
                            # Keep all original bytes on disk; a very large tool
                            # output is not grounds to abort native capabilities.
                            buffer = b''
                            oversized_line = True
                # WNOWAIT leaves the exact child PID reserved until all signals
                # to its owned group finish; PID/PGID reuse cannot target a new
                # unrelated process in the completion/stop race.
                exited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None and dead_at is None:
                    dead_at = time.monotonic()
                if stop_deadline is not None and time.monotonic() >= stop_deadline:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stop_deadline = None
                if dead_at is not None and (eof or time.monotonic() - dead_at > .3):
                    break
            output.flush()
            os.fsync(output.fileno())
    except Exception as exc:
        error = type(exc).__name__
    finally:
        if process is not None:
            # Kill only this child's original process group before reaping its
            # reserved PID. Detached/remote effects are not declared settled.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = process.wait()
            process.stdout.close()
            reaped = False
            until = time.monotonic() + 2
            while time.monotonic() < until:
                try:
                    child, unused = os.waitpid(-process.pid, os.WNOHANG)
                    if child == 0:
                        time.sleep(.02)
                except ChildProcessError:
                    reaped = True
                    break
        else:
            code = None
            reaped = True
        listener.close()
        try:
            entry = os.lstat(path)
            if (stat.S_ISSOCK(entry.st_mode) and entry.st_uid == os.getuid()
                    and socket_identity == (entry.st_dev, entry.st_ino)):
                os.unlink(path)
        except FileNotFoundError:
            pass
        known = code == 0 and completed and session_id is not None and error is None and not stopped and eof and reaped
        manager._update(request_id, status='completed' if known else ('stopped' if stopped else 'unknown'),
            returncode=code, turn_completed=completed, events_eof=eof, outcome_unknown=not known,
            session_id=session_id, finished_at=time.time(), supervisor_error=error,
            owned_native_reaped=process is not None, owned_group_children_reaped=reaped,
            detached_or_remote_effects_settled=False, effective_verified=False)
    return 0 if known else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    commands = parser.add_subparsers(dest='action')
    for action in ('spawn', 'resume', 'configure', 'status', 'stop', '_supervise'):
        item = commands.add_parser(action)
        if action != 'status' and action != '_supervise':
            item.add_argument('--agent', required=True)
        elif action == 'status':
            item.add_argument('--agent')
            item.add_argument('--request-id')
        if action in ('spawn', 'resume', 'configure', '_supervise'):
            item.add_argument('--request-id', required=True)
        if action in ('spawn', 'configure'):
            item.add_argument('--model', required=action == 'spawn')
            item.add_argument('--effort', required=action == 'spawn')
        if action == 'spawn':
            item.add_argument('--cwd', required=True)
        if action in ('spawn', 'resume'):
            item.add_argument('--prompt-file', required=True, help='Private UTF-8 prompt file, or - for stdin')
    args = parser.parse_args(argv)
    try:
        manager = AgentManager(args.config)
        if args.action == '_supervise':
            return supervise(manager, args.request_id)
        if args.action in ('spawn', 'resume'):
            raw = sys.stdin.buffer.read(MAX_PROMPT + 1) if args.prompt_file == '-' else _private_bytes(args.prompt_file, MAX_PROMPT)
            prompt = raw.decode('utf8')
        if args.action == 'spawn':
            result = manager.spawn(args.request_id, args.agent, args.cwd, prompt, args.model, args.effort)
        elif args.action == 'resume':
            result = manager.resume(args.request_id, args.agent, prompt)
        elif args.action == 'configure':
            result = manager.configure(args.request_id, args.agent, args.model, args.effort)
        elif args.action == 'stop':
            result = manager.stop(args.agent)
        elif args.action == 'status':
            result = manager.status(args.agent, args.request_id)
        else:
            parser.error('choose spawn, resume, configure, status or stop')
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, AgentError) else type(exc).__name__,
                          'automatic_retry': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
