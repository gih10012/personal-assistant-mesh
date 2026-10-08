"""Pi native RPC adapter. No borrowed OAuth tokens or silent paid-provider switch.

Configured fallback only: use an already authorized subscription or a local
endpoint. Never replay a started Codex turn through a different harness.
"""
import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time

from .codex import CodexError


class Pi:
    def __init__(self, config, tools=None, on_tool=None, on_activity=None, on_interaction=None):
        if config.get('cost_policy') not in ('existing_subscription', 'local'):
            raise CodexError('pi_cost_authorization_required')
        if not config.get('model'):
            raise CodexError('pi_model_configuration_required')
        self.config, self.on_activity = config, on_activity
        self.serial, self.events, self.deferred = 0, queue.Queue(), []
        env = os.environ.copy()
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
        checkpoint = checkpoint or {}
        if checkpoint.get('pi_session_file'):
            switched = self.rpc('switch_session', {'sessionPath': checkpoint['pi_session_file']})
            if switched.get('cancelled'):
                raise CodexError('pi_resume_cancelled')
        state = self.rpc('get_state')
        reference = {'pi_session_file': state.get('sessionFile'), 'thread_id': state.get('sessionId')}
        if self.on_activity:
            self.on_activity('session_ready', reference)
        reply = self.rpc('prompt', {'message': text})
        if reply.get('disposition') != 'started':
            raise CodexError('pi_prompt_not_started')
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
                    raise CodexError('pi_turn_failed')
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
