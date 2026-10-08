import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .channel import Channel, ILink
from .config import read_secret
from .store import Conflict, Store


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class API:
    def __init__(self, config):
        self.config = config
        self.store = Store(config['database'])
        if self.store.get('activate_after_ms') is None:
            self.store.set('activate_after_ms', int(time.time() * 1000))
        self.peers = [(read_secret(p['token_file']), p) for p in config['peers']]

    def principal(self, authorization):
        if not authorization.startswith('Bearer '):
            return None
        supplied = authorization[7:]
        return next((p for secret, p in self.peers if hmac.compare_digest(secret, supplied)), None)

    def dispatch(self, method, path, payload, peer):
        role, node = peer.get('role'), peer.get('node')
        if method == 'GET' and path == '/v1/status':
            result = self.store.status()
            result['channel_error'] = self.store.get('channel_error')
            return result
        if method == 'GET' and path.split('?', 1)[0] == '/v1/projects' and role in ('operator', 'viewer'):
            from urllib.parse import parse_qs, urlsplit
            from .observability import projects
            query = parse_qs(urlsplit(path).query)
            return projects(self.store, int(query.get('limit', ['50'])[0]), int(query.get('after', ['0'])[0]))
        if method == 'POST' and path == '/v1/heartbeat' and node:
            configured = peer.get('capabilities', [])
            offered = payload.get('capabilities', configured)
            if not isinstance(offered, list) or not set(offered) <= set(configured):
                raise ValueError('capability_not_authorized')
            return self.store.heartbeat(node, offered, peer.get('score', 0), payload.get('details'))
        if method == 'POST' and path == '/v1/claim' and node:
            return {'task': self.store.claim(node)}
        if method == 'POST' and path == '/v1/recovery/claim' and node and 'native.wechat.refresh' in peer.get('capabilities', []):
            return {'renewal': self.store.claim_renewal(node)}
        if method == 'POST' and path == '/v1/task/update' and node:
            return self.store.update_task(payload['id'], node, payload['epoch'], payload.get('checkpoint'), payload.get('result'), payload.get('status'))
        if method == 'POST' and path == '/v1/tasks' and role == 'operator':
            return {'id': self.store.create_task(payload['input'], payload.get('required'), payload.get('parent_id'), payload.get('id'), payload.get('context'))}
        if method == 'POST' and path == '/v1/task/control' and role == 'operator':
            return self.store.control_task(payload['id'], payload['command'])
        if method == 'POST' and path == '/v1/agent/action' and node:
            return self.store.agent_action(payload['task_id'], node, payload['epoch'], payload['call_id'], payload['action'], payload.get('arguments', {}))
        if method == 'POST' and path == '/v1/session' and node:
            return self.store.session_action(payload['task_id'], node, payload['epoch'], payload['action'], payload.get('payload', {}))
        if method == 'POST' and path == '/v1/interaction' and node:
            return self.store.interaction(payload['task_id'], node, payload['epoch'], payload['id'], payload.get('kind'), payload.get('params'))
        if method == 'POST' and path == '/v1/interaction/resolve' and role == 'operator':
            return self.store.resolve_interaction(payload['id'], payload['answer'])
        if method == 'POST' and path == '/v1/task/steer' and role == 'operator':
            return self.store.steer(payload['id'], payload['text'], payload['request_id'])
        if method == 'POST' and path == '/v1/steering' and node:
            return self.store.poll_steering(payload['task_id'], node, payload['epoch'], payload.get('id'), payload.get('state'))
        if method == 'POST' and path == '/v1/notify' and role == 'operator':
            return {'id': self.store.enqueue(payload['request_id'], payload['text'])}
        if method == 'POST' and path == '/v1/notify/media' and role == 'operator':
            return {'id': self.store.enqueue_media(payload['request_id'], payload['items'])}
        if method == 'POST' and path == '/v1/notify/status' and role == 'operator':
            return self.store.send_status(payload['request_id'])
        if method == 'POST' and path == '/v1/task/status' and role in ('operator', 'viewer'):
            return self.store.task_status(payload['id'])
        if method == 'POST' and path == '/v1/tasks/retry-waiting' and role == 'operator':
            return self.store.retry_waiting()
        if method == 'POST' and path == '/v1/inbox' and role == 'operator':
            return self.store.inbox(payload.get('after', 0), payload.get('limit', 20))
        if method == 'POST' and path == '/v1/budget/reserve' and role == 'operator':
            return self.store.budget_reserve(payload['id'], payload['amount'], self.config.get('budget', {}))
        raise PermissionError('route_not_authorized')


def serve(config, ready=None):
    api = API(config)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # no tokens, message bodies or personal identifiers in journal

        def send_json(self, status, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_api(self):
            if self.command == 'GET' and self.path == '/healthz':
                return self.send_json(200, {'ok': True, 'service': 'assistant-mesh'})
            peer = api.principal(self.headers.get('Authorization', ''))
            if not peer:
                return self.send_json(401, {'error': 'authentication_required'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 128 * 1024:
                    return self.send_json(413, {'error': 'body_too_large'})
                payload = json.loads(self.rfile.read(size)) if size else {}
                if not isinstance(payload, dict):
                    raise ValueError('invalid_body')
                return self.send_json(200, api.dispatch(self.command, self.path, payload, peer))
            except Conflict as exc:
                return self.send_json(409, {'error': str(exc)})
            except PermissionError:
                return self.send_json(403, {'error': 'route_not_authorized'})
            except (ValueError, KeyError, TypeError):
                return self.send_json(400, {'error': 'invalid_request'})
            except Exception:
                return self.send_json(500, {'error': 'internal_error'})

        do_GET = handle_api
        do_POST = handle_api

    # Private RPC only. Use SSH forwarding or a TLS/auth proxy for remote nodes.
    server = ThreadingHTTPServer(('127.0.0.1', config.get('port', 17680)), Handler)
    server.daemon_threads = True
    channel = None
    if config.get('ilink_account'):
        channel = Channel(api.store, ILink(config['ilink_account']))
        for target in (channel.run_poll, channel.run_send):
            threading.Thread(target=target, daemon=True).start()
    if ready:
        ready(server, api, channel)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if channel:
            channel.stop.set()
        server.server_close()
