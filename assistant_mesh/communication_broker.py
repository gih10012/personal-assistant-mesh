"""Standalone communication authority/state/witness, not a Leader workspace.

No dependency on VPS task Store for channel admission, cursor or notifications.
The logical original authority label is retained; physical host is explicit.
Default disabled transport and missing owner verifiers do not grant/start IO.
"""
import argparse
import fcntl
import hmac
import json
import os
import stat
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit

from .channel import Channel, ILink
from .channel_authority import ChannelAuthority, validate_config, validate_peer
from .channel_carrier import CarrierFence
from .communication_store import CommunicationStore
from .config import private_json, read_secret
from .legacy_fenced_receiver import FencedChannelOwnerControl, OwnedFencedSupervisor
from .networking import digest, identifier
from .remote_notifications import PROTOCOL as NOTIFY_PROTOCOL, RemoteNotifications
from .store import Conflict

OWNER_PROTOCOL = 'mesh-communication-broker-owner/1'


def _owner_lock(database):
    from .communication_store import _private_path
    path = _private_path(database)
    fd = os.open(str(path) + '.communication-owner.lock',
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        item = os.fstat(fd)
        if (not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid()
                or stat.S_IMODE(item.st_mode) != 0o600 or item.st_nlink != 1):
            raise ValueError('communication_owner_lock_unsafe')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


class Broker:
    def __init__(self, config, handoff_verifier=None, operation_verifier=None):
        if (not isinstance(config, dict) or config.get('protocol') != OWNER_PROTOCOL
                or set(config) - {'protocol', 'database', 'account_file', 'node_id',
                                  'channel_carrier', 'peers', 'port'}):
            raise ValueError('communication_broker_config_invalid')
        identifier(config['node_id'])
        carrier = validate_config(config['channel_carrier'])
        self.peers = [(read_secret(peer['token_file']), peer) for peer in config['peers']]
        for index, (secret, peer) in enumerate(self.peers):
            if peer.get('role') not in ('channel_carrier', 'notifier', 'worker', 'agent_peer', 'executor', 'viewer'):
                raise ValueError('communication_peer_role_invalid')
            if peer.get('role') == 'executor':
                identifier(peer['execution_authority'])
                if 'owner.notify' in peer.get('capabilities', []):
                    identifier(peer['node'])
            if peer.get('role') in ('notifier', 'worker', 'agent_peer'):
                identifier(peer['node'])
                if 'owner.notify' not in peer.get('capabilities', []):
                    raise ValueError('communication_notifier_capability_required')
            if any(hmac.compare_digest(secret, previous) for previous, _ in self.peers[:index]):
                raise ValueError('communication_credential_alias_forbidden')
        self.config, self.store = config, CommunicationStore(config['database'])
        if not self.store.status()['prepared_complete']:
            raise ValueError('communication_candidate_incomplete')
        identity = self.store.identity
        if carrier['owner_ref'] != identity['owner_ref']:
            raise ValueError('communication_original_owner_changed')
        self.ilink = ILink(config['account_file'])  # validates file; no external IO
        binding = digest([NOTIFY_PROTOCOL, self.ilink.base,
            self.ilink.account['ilink_user_id'], self.ilink.account['ilink_bot_id']])
        if binding != identity['account_binding']:
            raise ValueError('communication_original_account_changed')
        for _, peer in self.peers:
            if peer.get('role') == 'channel_carrier':
                validate_peer(peer, identity['owner_ref'], identity['origin_authority'])
                if peer['channel']['account_binding'] != binding:
                    raise ValueError('communication_original_account_changed')
        if any(hook is not None and not callable(hook) for hook in (handoff_verifier, operation_verifier)):
            raise ValueError('communication_owner_verifier_invalid')
        def verified_handoff(before, holder, proof_ref):
            self.store.require_active()
            return handoff_verifier(before, holder, proof_ref)
        self.channel_authority = ChannelAuthority(self.store, carrier,
            identity['origin_authority'], self.ilink, binding,
            handoff_verifier=verified_handoff if handoff_verifier is not None else None,
            operation_verifier=operation_verifier)
        self.notifications = RemoteNotifications(self.store, identity['origin_authority'], route_id=binding)

    def principal(self, header):
        if not isinstance(header, str) or not header.startswith('Bearer '):
            return None
        return next((peer for secret, peer in self.peers if hmac.compare_digest(secret, header[7:])), None)

    def dispatch(self, path, body, peer):
        parsed = urlsplit(path)
        if parsed.query or parsed.fragment or not isinstance(body, dict):
            raise ValueError('communication_request_invalid')
        role, path = peer['role'], parsed.path
        if role == 'channel_carrier':
            if not path.startswith('/channel-carrier/'):
                raise PermissionError('communication_carrier_route_forbidden')
            action = path[len('/channel-carrier/'):]
            if action != 'inspect' and action != 'state/get':
                self.store.require_active()
            return self.channel_authority.dispatch(peer['node'], action, body)
        if path.startswith('/channel-carrier/'):
            raise PermissionError('communication_carrier_role_required')
        if path == '/communication/status' and role in ('viewer', 'executor') and not body:
            return dict(self.store.status(), physical_host=self.config['node_id'],
                        authority_kind='independent_communication_not_execution_leader')
        can_notify = (role in ('notifier', 'worker', 'agent_peer')
            or (role == 'executor' and 'owner.notify' in peer.get('capabilities', [])))
        if can_notify and path in ('/v1/mesh/notify', '/v1/mesh/notify/status'):
            if path == '/v1/mesh/notify':
                return self.notifications.receive(peer['node'], body)
            if path == '/v1/mesh/notify/status':
                return self.notifications.status(peer['node'], body)
        if role != 'executor':
            raise PermissionError('communication_route_forbidden')
        authority = peer['execution_authority']
        if path == '/communication/task_reference' and set(body) == {'task_id', 'task_row_sha256'}:
            return self.store.register_reference(authority, **body)
        if path == '/communication/control_reference' and set(body) == {'task_id', 'task_row_sha256', 'control_id'}:
            return self.store.register_reference(authority, **body)
        if path == '/communication/pending' and set(body) == {'limit'}:
            return {'events': self.store.pending(body['limit'])}
        if path == '/communication/prepare' and set(body) == {'message_id', 'authority'}:
            if body['authority'] != authority:
                raise PermissionError('communication_executor_binding_changed')
            return self.store.prepare_delivery(body['message_id'], authority)
        if path == '/communication/event' and set(body) == {'message_id'}:
            return self.store.event(body['message_id'], authority)
        if path == '/communication/receipt' and set(body) == {'message_id', 'receipt'}:
            self.store.record_delivery(body['message_id'], authority, body['receipt'])
            return {'confirmed': True, 'execution_verified': False}
        if path == '/communication/unknown' and set(body) == {'message_id'}:
            self.store.mark_delivery_unknown(body['message_id'], authority)
            return {'unknown': True, 'replay_authorized': False}
        raise PermissionError('communication_route_forbidden')


class _HTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def serve(config, ready=None, handoff_verifier=None, operation_verifier=None, before_channel=None,
          owner_socket=None):
    """Root-owned service entry. No peer route activates, freezes or grants."""
    if any(hook is not None and not callable(hook) for hook in (ready, before_channel)):
        raise ValueError('communication_owner_hook_invalid')
    lock_fd = _owner_lock(config['database'])
    try:
        return _serve_locked(config, ready, handoff_verifier, operation_verifier,
                             before_channel, owner_socket)
    finally:
        os.close(lock_fd)


def _serve_locked(config, ready, handoff_verifier, operation_verifier, before_channel, owner_socket):
    api = Broker(config, handoff_verifier, operation_verifier)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *arguments):
            pass
        def reply(self, status, body):
            data = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if self.path == '/healthz':
                self.reply(200, {'ok': True, 'kind': 'communication_broker'})
            else:
                self.reply(403, {'error': 'route_forbidden'})
        def do_POST(self):
            peer = api.principal(self.headers.get('Authorization', ''))
            if peer is None:
                return self.reply(401, {'error': 'authentication_required'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                large = peer['role'] == 'channel_carrier' and self.path in (
                    '/channel-carrier/state/record_result', '/channel-carrier/state/commit_poll')
                if not 0 <= size <= (8 * 1024 * 1024 + 65536 if large else 128 * 1024):
                    return self.reply(413, {'error': 'body_too_large'})
                body = json.loads(self.rfile.read(size)) if size else {}
                self.reply(200, api.dispatch(self.path, body, peer))
            except PermissionError:
                self.reply(403, {'error': 'route_forbidden'})
            except Conflict:
                self.reply(409, {'error': 'original_identity_conflict'})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {'error': 'invalid_request'})
            except Exception:
                self.reply(500, {'error': 'operation_unconfirmed'})
    http = _HTTPServer(('127.0.0.1', config.get('port', 17684)), Handler)
    channel = group = control = None
    try:
        if owner_socket is not None:
            control = FencedChannelOwnerControl(owner_socket)
        if before_channel is not None:
            before_channel(api)
        carrier = api.channel_authority.config
        if carrier['transport_mode'] == 'fenced':
            api.store.require_active()
            term = api.channel_authority.witness.inspect(api.channel_authority.binding)
            if term['holder'] != config['node_id'] or term['frozen']:
                raise ValueError('communication_local_term_not_granted')
            channel = Channel(api.channel_authority.state, api.ilink,
                CarrierFence(api.channel_authority.bound_client(config['node_id']), term))
            group = OwnedFencedSupervisor(channel, **carrier.get('supervisor', {}))
            group.start()
        if control is not None:
            control.attach(http, api, channel)
        if ready is not None:
            ready(http, api, channel)
        http.serve_forever(poll_interval=0.2)
    finally:
        if group is not None:
            group.stop(5)
        if control is not None:
            control.close()
        http.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent communication broker, no automatic takeover')
    parser.add_argument('--config', required=True)
    parser.add_argument('--owner-socket')
    args = parser.parse_args(argv)
    try:
        serve(private_json(args.config), owner_socket=args.owner_socket)
    except Exception:
        print(json.dumps({'ok': False, 'error': 'communication_broker_unconfirmed'}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
