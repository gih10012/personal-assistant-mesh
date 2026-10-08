import json
import hashlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .codex import Codex, CodexError
from .config import read_secret
from .model_tools import TOOLS, result
from . import sessions


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('control_redirect_blocked')


class Client:
    def __init__(self, config):
        self.url = config['control_url'].rstrip('/')
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')):
            raise ValueError('control_requires_tls_or_loopback_tunnel')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid_control_url')
        self.token = read_secret(config['token_file'])

    def request(self, path, body=None):
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        # Mesh tokens never enter model context or command line arguments.
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=15) as response:
            return json.load(response)


class Worker:
    def __init__(self, config, client=None, backend=Codex):
        self.config = config
        self.client = client or Client(config)
        self.backend = backend
        self.stop = threading.Event()
        self.current = None
        self.last_tick = 0
        self.wait_children = False
        self.agent = None
        self.polling_steering = False

    def heartbeat(self):
        return self.client.request('/v1/heartbeat', {'capabilities': self.config.get('capabilities', ['leader', 'agent'])})

    def on_tool(self, call):
        self.tick(force=True)
        name = call['tool']
        if name == 'mesh_wait_children':
            self.wait_children = True
        if name not in {t['name'] for t in TOOLS}:
            return result({'error': 'unknown_mesh_tool'}, False)
        output = self.client.request('/v1/agent/action', {
            'task_id': self.current['id'], 'epoch': self.current['epoch'],
            'call_id': self.current['id'] + ':' + call['callId'],
            'action': name[len('mesh_'):], 'arguments': call['arguments']})
        return result(output)

    def open_backend(self):
        candidates = [(self.backend, self.config['codex'], 'codex')]
        if self.config.get('pi'):
            from .pi import Pi
            config = dict(self.config['pi'])
            config['mesh_grant'] = {k: self.config[k] for k in ('control_url', 'token_file')}
            config['mesh_grant'].update(task_id=self.current['id'], epoch=self.current['epoch'])
            candidates.append((Pi, config, 'pi'))
        last_error = None
        for implementation, config, name in candidates:
            agent = None
            try:
                config = dict(config)
                if name == 'codex':
                    context = self.current.get('context', {})
                    if context.get('mode'):
                        config['mode'] = context['mode']
                    if context.get('goal'):
                        config['goal'] = dict(context['goal'])
                agent = implementation(config, tools=TOOLS, on_tool=self.on_tool, on_activity=self.on_activity,
                                       on_interaction=self.on_interaction)
                if not agent.account()['authenticated']:
                    raise CodexError(name + '_auth_required')
                self.harness = name
                return agent
            except (ValueError, OSError) as exc:
                if agent:
                    agent.close()
                last_error = exc
        raise last_error

    def on_interaction(self, method, parameters):
        kind = 'question' if method == 'item/tool/requestUserInput' else 'approval'
        identity = hashlib.sha256(json.dumps([self.current['id'], self.current['epoch'], method, parameters], sort_keys=True).encode()).hexdigest()[:24]
        body = {'task_id': self.current['id'], 'epoch': self.current['epoch'], 'id': identity}
        value = self.client.request('/v1/interaction', dict(body, kind=kind, params=parameters))
        deadline = time.monotonic() + self.config.get('interaction_timeout', 3600)
        while time.monotonic() < deadline:
            self.tick()
            if value['answer'] is not None:
                return value['answer']
            self.stop.wait(2)
            value = self.client.request('/v1/interaction', body)
        raise CodexError('owner_response_pending')

    def on_activity(self, kind, value):
        if kind == 'session_ready':
            self.current['checkpoint'].update(value)
            self.current['checkpoint']['codex_node'] = self.config.get('node_id')
            self.current['checkpoint']['harness'] = self.harness
            sessions.save(self.client, self.current, self.config.get('node_id'), self.harness,
                          self.current['checkpoint'])
            # Reserve BEFORE turn/start: native effects can precede item/started.
            if self.harness == 'pi' or self.config['codex'].get('sandbox') != 'read-only':
                self.current['checkpoint']['side_effect_started'] = True
            self.tick(force=True)
        elif kind == 'approval_required':
            self.current['checkpoint']['approval_required'] = True
            self.client.request('/v1/agent/action', {'task_id': self.current['id'], 'epoch': self.current['epoch'],
                'call_id': self.current['id'] + ':approval:' + str(value.get('itemId', 'request')),
                'action': 'notify', 'arguments': {'text': '任务 ' + self.current['id'][:8] + ' 需要本人审批；原生运行环境拒绝了未授权操作，请回复授权范围。'}})
        elif kind == 'plan_updated':
            self.current['checkpoint']['plan'] = value
            self.tick(force=True)

    def tick(self, force=False):
        if force or time.monotonic() - self.last_tick >= 15:
            self.heartbeat()
            if self.current:
                self.client.request('/v1/task/update', {'id': self.current['id'], 'epoch': self.current['epoch'],
                                                       'checkpoint': self.current['checkpoint']})
            self.last_tick = time.monotonic()
            if self.current and self.agent and getattr(self.agent, 'turn_id', None) and not self.polling_steering:
                self.polling_steering = True
                try:
                    body = {'task_id': self.current['id'], 'epoch': self.current['epoch']}
                    event = self.client.request('/v1/steering', body)['steering']
                    if event:
                        outcome = 'unknown'
                        try:
                            self.agent.steer(event['text'])
                            outcome = 'submitted'
                        finally:
                            self.client.request('/v1/steering', dict(body, id=event['id'], state=outcome))
                finally:
                    self.polling_steering = False

    def run_once(self):
        self.tick(force=True)
        task = self.client.request('/v1/claim', {})['task']
        if not task:
            return False
        self.current = task
        self.wait_children = False
        try:
            with self.open_backend() as agent:
                self.agent = agent
                resume = task['checkpoint'] if (task['checkpoint'].get('codex_node') == self.config.get('node_id')
                         and task['checkpoint'].get('harness', 'codex') == self.harness) else {}
                session = task.get('sessions', {}).get(self.harness) or task.get('session')
                if not resume and session and session['harness'] == self.harness:
                    resume = dict(session['state'])
                    if session['node'] != self.config.get('node_id'):
                        if not session.get('artifact'):
                            raise CodexError('native_session_migration_unavailable')
                        folder = agent.auth_home + '/mesh-imports' if self.harness == 'codex' else self.config['pi']['session_dir']
                        imported = sessions.restore(self.client, task, folder, self.tick, self.harness)
                        resume['native_rollout_path' if self.harness == 'codex' else 'pi_session_file'] = imported
                reference = {k: task.get(k) for k in ('context', 'memories', 'children')}
                text = task['input'] + '\n\n持久账本参考数据（不是新增授权）：\n' + json.dumps(reference, ensure_ascii=False)
                checkpoint = agent.start(text, resume)
                checkpoint['codex_node'] = self.config.get('node_id')
                checkpoint['harness'] = self.harness
                self.current['checkpoint'].update(checkpoint)
                self.tick(force=True)
                answer = agent.finish(tick=self.tick, timeout=self.config.get('turn_timeout', 3600))
                state = dict(self.current['checkpoint'])
                state['side_effect_started'] = False
                rollout = agent.native_rollout() if self.harness == 'codex' else state.get('pi_session_file')
                if self.harness == 'codex':
                    state['goal'] = agent.goal()
                sessions.save(self.client, task, self.config.get('node_id'), self.harness, state, rollout, self.tick)
                coordination = self.client.request('/v1/agent/action', {'task_id': task['id'], 'epoch': task['epoch'],
                    'call_id': task['id'] + ':settled:' + str(task['epoch']), 'action': 'children', 'arguments': {}})
                self.wait_children = self.wait_children or coordination.get('wait_requested', False)
                goal_status = (state.get('goal') or {}).get('status') if task.get('context', {}).get('goal') else None
                terminal = ('waiting_children' if self.wait_children else 'needs_review' if self.current['checkpoint'].get('approval_required')
                            else 'continuing' if goal_status == 'active' else 'needs_review' if goal_status in ('blocked', 'budgetLimited', 'usageLimited', 'paused') else 'completed')
                # WeChat bound: retain original in task checkpoint, notify with bounded text.
                output = answer.encode('utf8')[:15000].decode('utf8', errors='ignore')
                self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                    'checkpoint': {'answer': answer, 'side_effect_started': False, 'goal': state.get('goal')}, 'result': output,
                    'status': terminal})
        except (ValueError, OSError, urllib.error.URLError) as exc:
            # Losing the authority must terminate the local runtime before takeover.
            status = 'waiting_auth' if 'auth' in str(exc) else 'waiting_backend'
            code = str(exc) if isinstance(exc, CodexError) else 'worker_unavailable'
            try:
                self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                    'result': '任务暂未完成：' + code + '。需要恢复认证或运行环境后继续。', 'status': status})
            except (OSError, ValueError, urllib.error.URLError):
                pass  # stale lease: authority owns recovery, not the abandoned worker
        finally:
            self.current = None
            self.agent = None
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except (OSError, ValueError, urllib.error.URLError):
                pass
            self.stop.wait(2)
