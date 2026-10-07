import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .codex import Codex, CodexError
from .config import read_secret


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
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=15) as response:
            return json.load(response)


class Worker:
    def __init__(self, config, client=None, backend=Codex):
        self.config = config
        self.client = client or Client(config)
        self.backend = backend
        self.stop = threading.Event()
        self.current = None
        self.last_tick = 0

    def heartbeat(self):
        return self.client.request('/v1/heartbeat', {'capabilities': self.config.get('capabilities', ['leader', 'codex.readonly'])})

    def tick(self, force=False):
        if force or time.monotonic() - self.last_tick >= 15:
            self.heartbeat()
            if self.current:
                self.client.request('/v1/task/update', {'id': self.current['id'], 'epoch': self.current['epoch'],
                                                       'checkpoint': self.current['checkpoint']})
            self.last_tick = time.monotonic()

    def run_once(self):
        self.tick(force=True)
        task = self.client.request('/v1/claim', {})['task']
        if not task:
            return False
        self.current = task
        try:
            with self.backend(self.config['codex']) as agent:
                if not agent.account()['authenticated']:
                    raise CodexError('codex_auth_required')
                resume = task['checkpoint'] if task['checkpoint'].get('codex_node') == self.config.get('node_id') else {}
                checkpoint = agent.start(task['input'], resume)
                checkpoint['codex_node'] = self.config.get('node_id')
                self.current['checkpoint'].update(checkpoint)
                self.tick(force=True)
                answer = agent.finish(tick=self.tick, timeout=self.config.get('turn_timeout', 300))
                # WeChat bound: retain original in task checkpoint, notify with bounded text.
                output = answer.encode('utf8')[:15000].decode('utf8', errors='ignore')
                self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                                                       'checkpoint': {'answer': answer}, 'result': output, 'status': 'completed'})
        except (ValueError, OSError, urllib.error.URLError) as exc:
            # Losing the authority must terminate the local runtime before takeover.
            status = 'waiting_auth' if 'auth' in str(exc) else 'failed'
            code = str(exc) if isinstance(exc, CodexError) else 'worker_unavailable'
            try:
                self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                    'result': '任务暂未完成：' + code + '。需要恢复认证或运行环境后继续。', 'status': status})
            except (OSError, ValueError, urllib.error.URLError):
                pass  # stale lease: authority owns recovery, not the abandoned worker
        finally:
            self.current = None
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except (OSError, ValueError, urllib.error.URLError):
                pass
            self.stop.wait(2)
