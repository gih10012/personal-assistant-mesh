"""Pi native RPC adapter. No borrowed OAuth tokens or silent paid-provider switch.

Configured fallback only: use an already authorized subscription, a local
endpoint, or an explicitly scoped zero-cost API contract. Never replay a
started Codex turn through a different harness.
"""
import json
import math
import os
from pathlib import Path
import queue
import signal
import stat
import subprocess
import tempfile
import threading
import time

from .codex import CodexError
from .model_provider_policy import validate_provider_cost


def _identity(value):
    return (isinstance(value, str) and 0 < len(value) <= 512
            and not value.startswith('-') and not any(ord(c) < 32 for c in value))


def _private_path(value, directory=False):
    """Check owner-provided files without resolving or reading credentials."""
    try:
        path = Path(value).expanduser()
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError()
        entry = path.lstat()
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected(entry.st_mode) or entry.st_uid != os.getuid()
                or stat.S_IMODE(entry.st_mode) & 0o077
                or (not directory and entry.st_nlink != 1)):
            raise ValueError()
        for ancestor in path.parents:
            item = ancestor.lstat()
            if (not stat.S_ISDIR(item.st_mode) or item.st_uid not in (0, os.getuid())
                    or (item.st_mode & 0o022 and not item.st_mode & stat.S_ISVTX)):
                raise ValueError()
        return str(path)
    except (OSError, ValueError, TypeError):
        raise CodexError('pi_private_configuration_required') from None


def _model_metadata(model):
    """A catalog projection, never raw endpoint/headers/auth or live evidence."""
    if not isinstance(model, dict) or not _identity(model.get('id')) or not _identity(model.get('provider')):
        raise CodexError('pi_model_catalog_invalid')
    value = {'id': model['id'], 'provider': model['provider']}
    for key in ('name', 'api'):
        if _identity(model.get(key)):
            value[key] = model[key]
    if isinstance(model.get('reasoning'), bool):
        value['reasoning'] = model['reasoning']
    for key in ('contextWindow', 'maxTokens'):
        number = model.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            value[key] = number
    if isinstance(model.get('input'), list):
        value['input'] = [kind for kind in model['input'] if kind in ('text', 'image')]
    if isinstance(model.get('cost'), dict):
        value['declared_cost'] = {key: number for key, number in model['cost'].items()
            if key in ('input', 'output', 'cacheRead', 'cacheWrite')
            and isinstance(number, (int, float)) and not isinstance(number, bool)
            and (isinstance(number, int) or math.isfinite(number)) and number >= 0}
    return value


class Pi:
    def __init__(self, config, tools=None, on_tool=None, on_activity=None, on_interaction=None):
        if not _identity(config.get('model')) or (config.get('provider') is not None and not _identity(config['provider'])):
            raise CodexError('pi_model_configuration_required')
        self.config, self.on_activity = config, on_activity
        self._authorize_model(config.get('provider'), config['model'])
        self._bound_provider = config.get('provider')
        self._run_state = 'idle'
        self.serial, self.events, self.deferred = 0, queue.Queue(), []
        env = os.environ.copy()
        if config.get('agent_dir') is not None:
            agent_dir = _private_path(config['agent_dir'], directory=True)
            env['PI_CODING_AGENT_DIR'] = agent_dir
            for name in ('models.json', 'auth.json'):
                filename = Path(agent_dir) / name
                if filename.exists() or filename.is_symlink():
                    _private_path(str(filename))
            if config.get('models_file') is not None:
                models_file = _private_path(config['models_file'])
                if models_file != str(Path(agent_dir) / 'models.json'):
                    raise CodexError('pi_models_file_location_invalid')
        elif config.get('models_file') is not None or config.get('cost_policy') == 'free_api':
            raise CodexError('pi_agent_dir_required')
        self.grant_path = None
        command = [config.get('executable', 'pi'), '--mode', 'rpc', '--model', config['model'],
                   '--session-dir', config['session_dir']]
        if config.get('provider'):
            command += ['--provider', config['provider']]
        if config.get('instructions'):
            command += ['--append-system-prompt', config['instructions']]
        if tools:
            if not config.get('mesh_grant'):
                raise CodexError('pi_mesh_grant_required')
            os.makedirs(config['session_dir'], mode=0o700, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=config['session_dir'], prefix='mesh-grant-', suffix='.json', delete=False) as handle:
                self.grant_path = handle.name
                json.dump(config['mesh_grant'], handle)
            env['MESH_PI_GRANT_FILE'] = self.grant_path
            extension = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'extensions', 'pi-mesh.ts')
            command += ['--extension', extension]
        try:
            self.process = subprocess.Popen(command, cwd=config['workspace'], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                bufsize=1, start_new_session=True, env=env)
        except BaseException:
            # The temporary capability must not survive a failed process spawn.
            if self.grant_path:
                os.unlink(self.grant_path)
            raise
        threading.Thread(target=self._read, daemon=True).start()

    def _authorize_model(self, provider, model):
        try:
            validate_provider_cost(self.config, provider, model)
        except ValueError as exc:
            # The shared validator emits fixed codes, never credential values.
            raise CodexError(str(exc)) from None

    def _authorize_state(self, state):
        model = state.get('model')
        if isinstance(model, dict) and _identity(model.get('id')) and _identity(model.get('provider')):
            bound = getattr(self, '_bound_provider', self.config.get('provider'))
            if bound is not None and model['provider'] != bound:
                raise CodexError('pi_provider_selection_requires_config')
            self._authorize_model(model['provider'], model['id'])
            self._bound_provider = model['provider']
        else:
            raise CodexError('pi_model_configuration_required')

    def models(self):
        reply = self.rpc('get_available_models')
        rows = reply.get('models')
        if not isinstance(rows, list):
            raise CodexError('pi_model_catalog_invalid')
        return {'models': [_model_metadata(row) for row in rows],
                'source': 'pi_configured_catalog', 'live_auth_verified': False,
                'inference_verified': False}

    def select_model(self, provider, model_id):
        """Select a model within this Pi session; never submit/replay a prompt."""
        if not _identity(provider) or not _identity(model_id):
            raise CodexError('pi_model_configuration_required')
        if getattr(self, '_run_state', 'idle') not in ('idle', 'settled'):
            raise CodexError('pi_model_selection_while_active')
        bound = getattr(self, '_bound_provider', self.config.get('provider'))
        if bound is not None and provider != bound:
            raise CodexError('pi_provider_selection_requires_config')
        self._authorize_model(provider, model_id)
        state = self.rpc('get_state')
        self._authorize_state(state)
        if provider != self._bound_provider:
            raise CodexError('pi_provider_selection_requires_config')
        if state.get('isStreaming') or state.get('isCompacting') or state.get('pendingMessageCount'):
            raise CodexError('pi_model_selection_while_active')
        previous = getattr(self, '_run_state', 'idle')
        self._run_state = 'selection_unknown'
        model = self.rpc('set_model', {'provider': provider, 'modelId': model_id})
        if (not isinstance(model, dict) or model.get('provider') != provider or model.get('id') != model_id):
            raise CodexError('pi_model_selection_mismatch')
        self._authorize_model(provider, model_id)  # Contract may expire during RPC.
        self._run_state = previous
        return _model_metadata(model)

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            self.events.put({'eof': True})

    def event(self, timeout):
        try:
            value = self.events.get(timeout=timeout)
        except queue.Empty:
            raise CodexError('pi_timeout') from None
        if value.get('eof'):
            raise CodexError('pi_disconnected')
        if value.get('type') == 'extension_ui_request':
            # No fake human answer; unsupported interactions are cancelled.
            self.send({'type': 'extension_ui_response', 'id': value['id'], 'cancelled': True})
        return value

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def rpc(self, method, parameters=None, timeout=30):
        self.serial += 1
        identity = 'mesh-' + str(self.serial)
        value = dict(parameters or {}, type=method, id=identity)
        self.send(value)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = self.event(max(.01, deadline - time.monotonic()))
            if reply.get('type') == 'response' and reply.get('id') == identity:
                if not reply.get('success'):
                    raise CodexError('pi_rpc_failed_' + method)
                return reply.get('data') or {}
            self.deferred.append(reply)
        raise CodexError('pi_timeout')

    def account(self):
        # Model availability is NOT proof of credentials; actual turn verifies it.
        state = self.rpc('get_state')
        return {'authenticated': bool(state.get('model')), 'type': 'configured_pi', 'live_auth_verified': False}

    def start(self, text, checkpoint=None):
        if getattr(self, '_run_state', 'idle') not in ('idle', 'settled'):
            raise CodexError('pi_prompt_already_attempted')
        checkpoint = checkpoint or {}
        if checkpoint.get('pi_session_file'):
            switched = self.rpc('switch_session', {'sessionPath': checkpoint['pi_session_file']})
            if switched.get('cancelled'):
                raise CodexError('pi_resume_cancelled')
        state = self.rpc('get_state')
        self._authorize_state(state)  # Resume restores Pi's actual prior model.
        if state.get('isStreaming') or state.get('isCompacting') or state.get('pendingMessageCount'):
            raise CodexError('pi_prompt_already_attempted')
        reference = {'pi_session_file': state.get('sessionFile'), 'thread_id': state.get('sessionId')}
        if self.on_activity:
            self.on_activity('session_ready', reference)
        self._authorize_state(state)  # Recheck immediately before inference.
        self._run_state = 'prompt_unknown'
        reply = self.rpc('prompt', {'message': text})
        if reply.get('disposition') != 'started':
            raise CodexError('pi_prompt_not_started')
        self._run_state = 'running'
        # Pi identifies its active stream by session, not a Codex TurnId. This
        # sentinel is only the worker's liveness gate for native Pi steer.
        self.turn_id = 'pi-stream-' + str(self.serial)
        reference['turn_id'] = self.turn_id
        return reference

    def steer(self, text):
        return self.rpc('steer', {'message': text})

    def finish(self, tick=None, timeout=3600):
        deadline, messages, failed = time.monotonic() + timeout, [], False
        while time.monotonic() < deadline:
            if tick:
                tick()
            try:
                value = self.deferred.pop(0) if self.deferred else self.event(min(10, max(.01, deadline-time.monotonic())))
            except CodexError as exc:
                if str(exc) == 'pi_timeout':
                    continue
                raise
            if value.get('type') == 'message_end' and value.get('message', {}).get('role') == 'assistant':
                message = value['message']
                failed = message.get('stopReason') in ('error', 'aborted')
                messages.append(''.join(c.get('text', '') for c in message.get('content', []) if c.get('type') == 'text'))
            if value.get('type') == 'agent_settled':
                if failed:
                    self._run_state = 'prompt_unknown'
                    raise CodexError('pi_turn_failed')
                self._run_state = 'settled'
                return '\n\n'.join(m for m in messages if m) or 'Pi 已结束，没有生成文字结果。'
        raise CodexError('pi_turn_timeout')

    def close(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
        if self.grant_path:
            try:
                os.unlink(self.grant_path)
            except FileNotFoundError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
