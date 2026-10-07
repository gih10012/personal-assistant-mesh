"""Codex app-server JSON-RPC over stdio; auth stays under a discovered Codex home."""
import json
import os
import queue
import signal
import subprocess
import threading
import time

from .config import discover_codex_auth


class CodexError(ValueError):
    pass


class Codex:
    def __init__(self, config):
        self.config = config
        root = discover_codex_auth(config.get('auth_home'))
        env = os.environ.copy()
        # The configured executable wrapper consumes this task-specific override.
        # Do not change this Codex session's CODEX_HOME or global shell environment.
        env['CODEX_HOME_OVERRIDE'] = root
        command = [config.get('executable', 'codex'), '-c', 'model_provider="openai"', 'app-server', '--stdio']
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=env, universal_newlines=True,
                                        start_new_session=True, bufsize=1)
        self.events = queue.Queue()
        self.serial = 0
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.rpc('initialize', {'clientInfo': {'name': 'personal_assistant_mesh', 'version': '0.1.0'}})
            self.send({'method': 'initialized'})
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        self.events.put(value)
                except ValueError:
                    pass
        finally:
            self.events.put({'eof': True})

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def event(self, timeout=20):
        try:
            value = self.events.get(timeout=timeout)
        except queue.Empty:
            raise CodexError('codex_timeout') from None
        if value.get('eof'):
            raise CodexError('codex_disconnected')
        if 'id' in value and 'method' in value:
            # Approval requests are never silently accepted. The read-only worker
            # cannot turn a chat instruction into unbounded machine permissions.
            if '/requestApproval' in value['method']:
                self.send({'id': value['id'], 'result': {'decision': 'decline'}})
            else:
                self.send({'id': value['id'], 'error': {'code': -32601, 'message': 'Host interaction unavailable; ask owner in reply.'}})
        return value

    def rpc(self, method, params, timeout=30):
        self.serial += 1
        request_id = self.serial
        self.send({'id': request_id, 'method': method, 'params': params})
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            value = self.event(max(0.01, until - time.monotonic()))
            if value.get('id') == request_id and 'method' not in value:
                if 'error' in value:
                    # Don't expose arbitrary protocol errors: may contain private data.
                    raise CodexError('codex_rpc_failed_' + method.replace('/', '_'))
                return value.get('result', {})
        raise CodexError('codex_timeout')

    def account(self):
        value = self.rpc('account/read', {'refreshToken': False})
        account = value.get('account') or {}
        return {'authenticated': bool(account), 'type': account.get('type'), 'plan': account.get('planType')}

    def models(self):
        return self.rpc('model/list', {}).get('data', [])

    def start(self, text, checkpoint=None):
        checkpoint = checkpoint or {}
        root = self.config['workspace']
        parameters = {'cwd': root, 'approvalPolicy': 'never', 'sandbox': 'read-only',
                      'developerInstructions': self.config.get('instructions',
                        '你是本人的个人助理。先复用已授权的能力和认证；确实缺失时再向本人询问。'
                        '当前 worker 为只读模式：可分析、回答和规划，不能声称完成未执行的操作。'
                        '不得购买资源、发送给第三人或控制桌面。凭据不可写入回答。')}
        if self.config.get('model'):
            parameters['model'] = self.config['model']
        if checkpoint.get('thread_id'):
            parameters['threadId'] = checkpoint['thread_id']
            value = self.rpc('thread/resume', parameters)
        else:
            value = self.rpc('thread/start', parameters)
        self.thread_id = value['thread']['id']
        turn = {'threadId': self.thread_id, 'input': [{'type': 'text', 'text': text}],
                'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'readOnly'}}
        started = self.rpc('turn/start', turn, timeout=60)
        self.turn_id = started['turn']['id']
        return {'thread_id': self.thread_id, 'turn_id': self.turn_id}

    def finish(self, tick=None, timeout=300):
        deadline = time.monotonic() + timeout
        replies = []
        while time.monotonic() < deadline:
            if tick:
                tick()
            try:
                value = self.event(timeout=min(10, max(0.01, deadline - time.monotonic())))
            except CodexError as exc:
                if str(exc) == 'codex_timeout':
                    continue
                raise
            method, params = value.get('method'), value.get('params', {})
            if params.get('threadId') not in (None, self.thread_id):
                continue
            if method == 'item/completed':
                item = params.get('item', {})
                if item.get('type') == 'agentMessage':
                    replies.append(item.get('text', ''))
            if method == 'turn/completed' and params.get('turn', {}).get('id') == self.turn_id:
                if params['turn'].get('status') != 'completed':
                    raise CodexError('codex_turn_failed')
                return '\n\n'.join(replies) or '任务已结束，但没有生成文字回答。'
        self.interrupt()
        raise CodexError('codex_turn_timeout')

    def interrupt(self):
        if getattr(self, 'turn_id', None):
            try:
                self.rpc('turn/interrupt', {'threadId': self.thread_id, 'turnId': self.turn_id}, timeout=5)
            except (CodexError, OSError):
                pass

    def close(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
