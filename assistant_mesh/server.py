import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlsplit

from .allocations import Allocations
from .channel import Channel, ILink
from .config import read_secret
from .resources import Registry
from .networking import Network
from .store import Conflict, Store


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class API:
    def __init__(self, config):
        self.config = config
        self.store = Store(config['database'])
        resource_config = config.get('resources', {})
        self.resources = Registry(self.store, owner_principal='operator',
                                  trusted_verifiers=resource_config.get('trusted_verifiers', ()))
        self.allocations = Allocations(self.store, self.resources)
        self.network = Network(self.store, config['node_id']) if config.get('node_id') else None
        if self.network:
            from .remote import Remote
            self.remote = Remote(self.store, self.network)
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
        worker = role == 'worker' and bool(node)
        parsed = urlsplit(path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        can_discover = role in ('operator', 'viewer') or bool(node)
        mesh_member = role in ('operator', 'viewer') or (role in ('worker', 'agent_peer') and bool(node))
        if self.network and parsed.path.startswith('/v1/mesh/'):
            return self.mesh_dispatch(method, parsed.path, query, payload, peer, mesh_member)
        if method == 'GET' and path == '/v1/status':
            result = self.store.status()
            result['channel_error'] = self.store.get('channel_error')
            return result
        if method == 'GET' and parsed.path == '/v1/projects' and role in ('operator', 'viewer'):
            from .observability import projects
            return projects(self.store, int(query.get('limit', ['50'])[0]), int(query.get('after', ['0'])[0]))
        if method == 'GET' and parsed.path == '/v1/resources' and can_discover:
            return self.resources.discover(query.get('kind', [None])[0], query.get('principal', [None])[0],
                                           self._query_flag(query, 'include_unavailable'),
                                           int(query.get('limit', ['100'])[0]))
        if method == 'GET' and parsed.path == '/v1/resource/graph' and can_discover:
            return self.resources.graph(self._query_flag(query, 'include_unavailable'),
                                        int(query.get('limit', ['200'])[0]))
        if method == 'GET' and parsed.path == '/v1/resource' and can_discover:
            if len(query.get('id', [])) != 1:
                raise ValueError('resource_id_required')
            include = self._query_flag(query, 'include_unavailable') if 'include_unavailable' in query else True
            return self.resources.describe(query['id'][0], include)
        if method == 'GET' and parsed.path == '/v1/capability-events' and can_discover:
            return self.capability_events(query)
        if method == 'POST' and path == '/v1/resource/action':
            return self.resource_action(payload, peer)
        if method == 'POST' and path == '/v1/allocation/action':
            return self.allocation_action(payload, peer)
        if method == 'POST' and path == '/v1/heartbeat' and worker:
            configured = peer.get('capabilities', [])
            offered = payload.get('capabilities', configured)
            if not isinstance(offered, list) or not set(offered) <= set(configured):
                raise ValueError('capability_not_authorized')
            return self.store.heartbeat(node, offered, peer.get('score', 0), payload.get('details'))
        if method == 'POST' and path == '/v1/claim' and worker:
            return {'task': self.store.claim(node)}
        if method == 'POST' and path == '/v1/recovery/claim' and worker and 'native.wechat.refresh' in peer.get('capabilities', []):
            return {'renewal': self.store.claim_renewal(node)}
        if method == 'POST' and path == '/v1/task/update' and worker:
            return self.store.update_task(payload['id'], node, payload['epoch'], payload.get('checkpoint'), payload.get('result'), payload.get('status'))
        if method == 'POST' and path == '/v1/tasks' and role == 'operator':
            return {'id': self.store.create_task(payload['input'], payload.get('required'), payload.get('parent_id'), payload.get('id'), payload.get('context'))}
        if method == 'POST' and path == '/v1/task/control' and role == 'operator':
            return self.store.control_task(payload['id'], payload['command'])
        if method == 'POST' and path == '/v1/agent/action' and worker:
            return self.store.agent_action(payload['task_id'], node, payload['epoch'], payload['call_id'], payload['action'], payload.get('arguments', {}))
        if method == 'POST' and path == '/v1/session' and worker:
            return self.store.session_action(payload['task_id'], node, payload['epoch'], payload['action'], payload.get('payload', {}))
        if method == 'POST' and path == '/v1/interaction' and worker:
            return self.store.interaction(payload['task_id'], node, payload['epoch'], payload['id'], payload.get('kind'), payload.get('params'))
        if method == 'POST' and path == '/v1/interaction/resolve' and role == 'operator':
            return self.store.resolve_interaction(payload['id'], payload['answer'])
        if method == 'POST' and path == '/v1/task/steer' and role == 'operator':
            return self.store.steer(payload['id'], payload['text'], payload['request_id'])
        if method == 'POST' and path == '/v1/steering' and worker:
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

    def mesh_dispatch(self, method, path, query, payload, peer, member):
        role, node = peer.get('role'), peer.get('node')
        grants = peer.get('capabilities', [])
        if method == 'GET' and member and path == '/v1/mesh/hello':
            return self.network.hello()
        if method == 'GET' and member and path == '/v1/mesh/links':
            return self.network.links()
        if method == 'GET' and role in ('worker', 'agent_peer') and node and path == '/v1/mesh/task':
            if len(query.get('id', [])) != 1:
                raise ValueError('message_id_required')
            return self.network.task_for_sender(node, query['id'][0])
        if method == 'POST' and role in ('worker', 'agent_peer') and node and path == '/v1/mesh/send' and 'a2a.delegate' in grants:
            if set(payload) != {'message'}:
                raise ValueError('invalid_agent_message_request')
            return self.network.receive(node, payload['message'])
        if method == 'POST' and role in ('worker', 'agent_peer') and node and path == '/v1/mesh/report' and 'a2a.report' in grants:
            if set(payload) != {'id', 'body'}:
                raise ValueError('invalid_agent_report_request')
            return self.network.report(node, payload['id'], payload['body'])
        if method == 'POST' and role == 'worker' and node and path == '/v1/mesh/delegate' and 'a2a.send' in grants:
            if set(payload) != {'task_id', 'epoch', 'call_id', 'peer', 'arguments'}:
                raise ValueError('invalid_remote_delegate_request')
            known = self.config.get('network_peers', {})
            if payload['peer'] not in known or not known[payload['peer']].get('send_allowed'):
                raise PermissionError('peer_delegation_not_authorized')
            return self.remote.delegate(payload['task_id'], node, payload['epoch'], payload['call_id'], payload['peer'], payload['arguments'])
        if method == 'POST' and role == 'operator' and path == '/v1/mesh/local':
            if set(payload) != {'message'}:
                raise ValueError('invalid_agent_message_request')
            return self.network.receive('operator', payload['message'])
        if method == 'POST' and path == '/v1/mesh/queue' and role == 'operator':
            if set(payload) != {'peer', 'message'}:
                raise ValueError('invalid_agent_message_request')
            known = self.config.get('network_peers', {})
            if payload['peer'] not in known or not known[payload['peer']].get('send_allowed'):
                raise PermissionError('peer_delegation_not_authorized')
            return self.network.enqueue(payload['peer'], payload['message'])
        raise PermissionError('route_not_authorized')

    @staticmethod
    def _query_flag(query, key):
        value = query.get(key, ['0'])[0]
        if value not in ('0', '1', 'false', 'true'):
            raise ValueError('invalid_query_flag')
        return value in ('1', 'true')

    def capability_events(self, query):
        limit = int(query.get('limit', ['100'])[0])
        if not 1 <= limit <= 1000:
            raise ValueError('invalid_limit')
        principal = query.get('principal', [None])[0]
        # Recent by default. Explicit after=0 asks for oldest-first backfill.
        with self.store.transaction() as db:
            where, values = (' WHERE actor=?', [principal]) if principal is not None else ('', [])
            maximum = db.execute('SELECT COALESCE(MAX(sequence),0) FROM capability_audit' + where, values).fetchone()[0]
            latest = db.execute('SELECT sequence FROM capability_audit' + where + ' ORDER BY sequence DESC LIMIT ?',
                                values + [limit]).fetchall()
        recent = 'after' not in query
        after = (latest[-1]['sequence'] - 1 if latest else 0) if recent else int(query['after'][0])
        result = self.resources.audit(after, limit, principal)
        result['page'] = {'mode': 'recent' if recent else 'after', 'after': after,
                          'limit': limit, 'next_cursor': result['next'],
                          'has_more': result['next'] < maximum}
        return result

    def resource_action(self, payload, peer):
        """A finite HTTP API, never the native model's tool/command allowlist.

        Identity and verifier authority come from authentication/deployment, not
        prompts, metadata, or JSON arguments. Viewer credentials remain read-only.
        """
        if not isinstance(payload, dict):
            raise ValueError('invalid_resource_request')
        if any(key in payload for key in ('actor', 'principal', 'owner', 'node', 'role', 'trusted_verifiers')):
            raise PermissionError('resource_identity_is_peer_bound')
        if set(payload) - {'action', 'arguments'}:
            raise ValueError('invalid_resource_request')
        role, node = peer.get('role'), peer.get('node')
        if role == 'operator':
            actor = 'operator'
        elif role == 'worker' and node:
            actor = 'node:' + node
        else:
            raise PermissionError('route_not_authorized')
        action, arguments = payload.get('action'), payload.get('arguments', {})
        if not isinstance(action, str) or not isinstance(arguments, dict):
            raise ValueError('invalid_resource_request')
        if any(key in arguments for key in ('actor', 'owner', 'role', 'node', 'trusted_verifiers')):
            raise PermissionError('resource_identity_is_peer_bound')
        reads = {'discover', 'describe', 'graph', 'audit'}
        provider = {'advertise', 'renew', 'observe', 'link', 'revoke', 'request_grant', 'authorize'}
        approvals = {'grant', 'revoke_grant'}
        if action not in reads | provider | approvals:
            raise ValueError('resource_action_not_implemented')
        if action in approvals and role != 'operator':
            raise PermissionError('owner_approval_required')
        arguments = dict(arguments)
        # Read principal is a search filter, not impersonation. A write cannot
        # select a provider or grantee other than its authenticated actor.
        if 'principal' in arguments and action not in reads:
            if arguments.pop('principal') != actor:
                raise PermissionError('resource_identity_is_peer_bound')
        method = getattr(self.resources, action)
        if action in reads:
            return method(**arguments)
        return method(actor, **arguments)

    def allocation_action(self, payload, peer):
        """Authenticated managed-capacity journal, not a native-tool gate.

        Reserving or accepting a dispatch neither starts a provider process nor
        proves execution. Providers must connect the journal to their own actual
        effect ledger. Identity is fixed by the authenticated peer, including
        when recording unknown outcomes or settlement after a task lease loss.
        """
        identity_fields = {'actor', 'principal', 'owner', 'node', 'role',
                           'provider', 'trusted_verifiers'}
        if not isinstance(payload, dict):
            raise ValueError('invalid_allocation_request')
        if identity_fields & set(payload):
            raise PermissionError('allocation_identity_is_peer_bound')
        if set(payload) - {'action', 'arguments'}:
            raise ValueError('invalid_allocation_request')
        role, node = peer.get('role'), peer.get('node')
        if role == 'operator':
            actor = 'operator'
        elif role == 'worker' and node:
            actor = 'node:' + node
        else:
            # As with resource/action, viewers use dedicated metadata routes;
            # an optional node field does not turn a viewer into a provider.
            raise PermissionError('route_not_authorized')
        action, arguments = payload.get('action'), payload.get('arguments', {})
        if not isinstance(action, str) or not isinstance(arguments, dict):
            raise ValueError('invalid_allocation_request')
        if identity_fields & set(arguments):
            raise PermissionError('allocation_identity_is_peer_bound')
        reads = {'pools', 'bindings', 'inspect', 'pending'}
        approvals = {'define_pool', 'renew_pool', 'revoke_pool', 'bind_pool', 'expire'}
        managed = {'reserve', 'accept', 'start', 'unknown', 'settle', 'decline', 'cancel'}
        if action not in reads | approvals | managed:
            raise ValueError('allocation_action_not_implemented')
        if action in approvals and role != 'operator':
            raise PermissionError('owner_approval_required')
        if action == 'reserve' and role != 'worker':
            # A task reservation must name the node holding its live task lease,
            # never an operator-supplied node or an unbound caller assertion.
            raise PermissionError('allocation_task_principal_mismatch')
        return getattr(self.allocations, action)(actor, **dict(arguments))


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
