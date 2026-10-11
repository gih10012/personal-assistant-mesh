"""Private owner IPC for an already-owned legacy communication loop group.

This is a Linux deployment adapter, not a Mesh permission or migration API.
It exposes no start/reset, module loading, ledger writes or handoff grant.
"""
import argparse
import json
import math
import os
import socket
import stat
import struct
import sys
import threading
import time
import uuid
from pathlib import Path

from .channel import ChannelLoopLifecycle
from .config import private_json

PROTOCOL = 'mesh-legacy-channel-owner-v1'
_MAX_FRAME = 4096
_PEER = struct.Struct('=iII')
_SERVE_LOCK = threading.Lock()


class OwnerControlError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _supported():
    if not sys.platform.startswith('linux') or not hasattr(socket, 'SO_PEERCRED'):
        raise OwnerControlError('owner_peer_credentials_unsupported')


def _credentials(connection):
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEER.size)
        pid, uid, _ = _PEER.unpack(raw)
    except (OSError, struct.error):
        raise OwnerControlError('owner_peer_credentials_unavailable') from None
    if pid <= 0:
        raise OwnerControlError('owner_peer_credentials_invalid')
    return pid, uid


def _private_parent(path):
    path = Path(path)
    if (not path.is_absolute() or path.name in ('', '.', '..')
            or '..' in path.parts or len(os.fsencode(str(path))) > 107):
        raise OwnerControlError('owner_socket_path_invalid')
    try:
        for ancestor in (path.parent,) + tuple(path.parent.parents):
            if stat.S_ISLNK(os.lstat(str(ancestor)).st_mode):
                raise OwnerControlError('owner_socket_parent_invalid')
        parent = os.lstat(str(path.parent))
    except OSError:
        raise OwnerControlError('owner_socket_parent_invalid') from None
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise OwnerControlError('owner_socket_parent_invalid')
    return path


def _stop_timeout(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= 60 or not math.isfinite(value)):
        raise OwnerControlError('owner_stop_timeout_invalid')
    return value


def _frame(connection, deadline=None):
    deadline = time.monotonic() + 2 if deadline is None else deadline
    raw = bytearray()
    while len(raw) <= _MAX_FRAME:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout()
        connection.settimeout(remaining)
        part = connection.recv(min(1024, _MAX_FRAME + 1 - len(raw)))
        if not part:
            raise OwnerControlError('owner_request_invalid')
        raw.extend(part)
        if b'\n' in raw:
            body, remainder = bytes(raw).split(b'\n', 1)
            if remainder or len(raw) > _MAX_FRAME:
                raise OwnerControlError('owner_request_invalid')
            try:
                def invalid_constant(value):
                    raise ValueError('non_finite_json')
                value = json.loads(body.decode('utf8'), parse_constant=invalid_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise OwnerControlError('owner_request_invalid') from None
            if not isinstance(value, dict):
                raise OwnerControlError('owner_request_invalid')
            return value
    raise OwnerControlError('owner_request_invalid')


def _write(connection, value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf8') + b'\n'
    if len(raw) > _MAX_FRAME:
        raise OwnerControlError('owner_response_too_large')
    connection.sendall(raw)


class LegacyChannelOwnerControl:
    """Retain the exact ready-hook group; close() closes IPC, not its carrier.

    Construction reserves a NEW socket in an existing owner-only directory.
    attach() never constructs, starts or replaces a Channel/loop group.
    """
    def __init__(self, socket_path):
        _supported()
        self.path = _private_parent(socket_path)
        self.owner_pid, self.owner_uid = os.getpid(), os.geteuid()
        self.instance_id = uuid.uuid4().hex
        self._group = self._channel = self._listener = self._thread = None
        self._socket_identity = None
        self._closed = threading.Event()
        self._slots = threading.BoundedSemaphore(8)
        self._close_lock = threading.Lock()
        self._parent_fd = os.open(str(self.path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent = os.fstat(self._parent_fd)
        if (parent.st_uid != self.owner_uid or stat.S_IMODE(parent.st_mode) != 0o700):
            os.close(self._parent_fd)
            self._parent_fd = None
            raise OwnerControlError('owner_socket_parent_invalid')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # No automatic stale-socket unlink, replacement or incumbent probe.
            listener.bind(str(self.path))
            own = os.stat(self.path.name, dir_fd=self._parent_fd, follow_symlinks=False)
            self._socket_identity = (own.st_dev, own.st_ino)
            os.chmod(str(self.path), 0o600)
            listener.listen(8)
            listener.settimeout(0.2)
            self._listener = listener
        except OSError:
            listener.close()
            self.close()
            raise OwnerControlError('owner_socket_unavailable') from None

    def attach(self, server, api, channel):
        """serve.ready callback. HTTP/Leader objects are deliberately not saved."""
        if self._group is not None or self._closed.is_set() or os.getpid() != self.owner_pid:
            raise OwnerControlError('owner_control_already_attached_or_closed')
        group = getattr(channel, 'loop_lifecycle', None)
        if (not isinstance(group, ChannelLoopLifecycle) or group.channel is not channel
                or getattr(channel, '_supervisor', None) is not None
                or getattr(channel, 'carrier_fence', None) is not None
                or getattr(api, 'channel_authority', None) is not None):
            raise OwnerControlError('owner_legacy_group_unavailable')
        observation = group.snapshot()
        if not observation['started'] or not observation['actual_owned_thread_handles']:
            raise OwnerControlError('owner_legacy_group_unavailable')
        self._channel, self._group = channel, group
        self._thread = threading.Thread(target=self._accept, name='mesh-channel-owner-ipc', daemon=True)
        self._thread.start()

    def _snapshot(self):
        return {'protocol': PROTOCOL, 'instance_id': self.instance_id,
                'owner_pid': self.owner_pid, 'owner_uid': self.owner_uid,
                'lifecycle': self._group.snapshot()}

    def _execute(self, request):
        action = request.get('action')
        if request.get('protocol') != PROTOCOL or action not in ('status', 'stop'):
            raise OwnerControlError('owner_request_invalid')
        keys = {'protocol', 'action'}
        if action == 'stop':
            keys |= {'instance_id', 'timeout'}
        elif 'instance_id' in request:
            keys.add('instance_id')
        if set(request) != keys:
            raise OwnerControlError('owner_request_invalid')
        if ('instance_id' in request and request['instance_id'] != self.instance_id):
            raise OwnerControlError('owner_instance_mismatch')
        if action == 'stop':
            timeout = _stop_timeout(request['timeout'])
            # Exact retained group only; a timeout does not replace it or revoke
            # its admitted callbacks. No use of server/API/Store here.
            self._group.stop(timeout)
        return {'ok': True, **self._snapshot()}

    def _handle(self, connection):
        try:
            with connection:
                connection.settimeout(2)
                try:
                    _, uid = _credentials(connection)
                    if uid != self.owner_uid:
                        raise OwnerControlError('owner_peer_not_authorized')
                    response = self._execute(_frame(connection))
                except OwnerControlError as error:
                    response = {'ok': False, 'protocol': PROTOCOL, 'error': error.code}
                except (OSError, ValueError, TypeError, KeyError):
                    response = {'ok': False, 'protocol': PROTOCOL, 'error': 'owner_request_invalid'}
                except Exception:
                    # Never publish account, exception body, native history or
                    # paths from an operation/snapshot failure.
                    response = {'ok': False, 'protocol': PROTOCOL, 'error': 'owner_operation_failed'}
                try:
                    _write(connection, response)
                except (OSError, OwnerControlError):
                    pass
        finally:
            self._slots.release()

    def _accept(self):
        while not self._closed.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(False):
                connection.close()
                continue
            thread = threading.Thread(target=self._handle, args=(connection,), daemon=True)
            try:
                thread.start()
            except Exception:
                connection.close()
                self._slots.release()

    def close(self):
        """Close this private IPC only; never stop a carrier or HTTP server."""
        with self._close_lock:
            self._closed.set()
            if self._listener is not None:
                self._listener.close()
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(2)
            if self._parent_fd is not None:
                try:
                    current = os.stat(self.path.name, dir_fd=self._parent_fd, follow_symlinks=False)
                    if (stat.S_ISSOCK(current.st_mode) and current.st_uid == self.owner_uid
                            and (current.st_dev, current.st_ino) == self._socket_identity):
                        os.unlink(self.path.name, dir_fd=self._parent_fd)
                except OSError:
                    pass
                os.close(self._parent_fd)
                self._parent_fd = None


def request_owner(socket_path, action='status', instance_id=None, timeout=5, expected_pid=None):
    """One bounded request, no retry; verify actual socket peer before sending."""
    _supported()
    path = _private_parent(socket_path)
    if action not in ('status', 'stop'):
        raise OwnerControlError('owner_request_invalid')
    if action == 'stop':
        _stop_timeout(timeout)
        if not isinstance(instance_id, str) or len(instance_id) != 32:
            raise OwnerControlError('owner_instance_required')
    if (expected_pid is not None and (type(expected_pid) is not int or expected_pid <= 0)):
        raise OwnerControlError('owner_peer_pid_invalid')
    try:
        original = os.lstat(str(path))
        if (not stat.S_ISSOCK(original.st_mode) or original.st_uid != os.geteuid()
                or stat.S_IMODE(original.st_mode) != 0o600):
            raise OwnerControlError('owner_socket_invalid')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            deadline = time.monotonic() + 2 + (timeout if action == 'stop' else 0)
            connection.settimeout(2)
            connection.connect(str(path))
            peer_pid, peer_uid = _credentials(connection)
            current = os.lstat(str(path))
            if (peer_uid != os.geteuid() or (expected_pid is not None and peer_pid != expected_pid)
                    or (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino)):
                raise OwnerControlError('owner_socket_peer_mismatch')
            request = {'protocol': PROTOCOL, 'action': action}
            if instance_id is not None:
                request['instance_id'] = instance_id
            if action == 'stop':
                request['timeout'] = timeout
            _write(connection, request)
            response = _frame(connection, deadline=deadline)
            if response.get('protocol') != PROTOCOL:
                raise OwnerControlError('owner_response_invalid')
            if response.get('ok') is not True:
                # Only fixed contract codes, never an arbitrary server message.
                raise OwnerControlError('owner_request_rejected')
            if (response.get('owner_pid') != peer_pid or response.get('owner_uid') != peer_uid
                    or (instance_id is not None and response.get('instance_id') != instance_id)
                    or not isinstance(response.get('lifecycle'), dict)):
                raise OwnerControlError('owner_response_invalid')
            return response
    except OSError:
        raise OwnerControlError('owner_request_unconfirmed') from None


def serve_with_owner_control(config, socket_path):
    """Owner-only service entry; preserve incumbent Store business semantics.

    Use with a separately frozen receiver release retaining the ready-hook's
    actual legacy handles. This does NOT select/install a source release.
    """
    policy = config.get('notification_policy') if isinstance(config, dict) else None
    if (not isinstance(config, dict) or 'channel_carrier' in config
            or not config.get('ilink_account')
            or (policy is not None and (not isinstance(policy, dict)
                                        or policy.get('mode', 'channel') != 'channel'))):
        raise OwnerControlError('owner_legacy_config_required')
    if not _SERVE_LOCK.acquire(False):
        raise OwnerControlError('owner_service_already_running')
    control = None
    from . import server
    original_store = server.Store

    def passive_store(path, **kwargs):
        kwargs['recover_inflight'] = False
        return original_store(path, **kwargs)

    def ready(http_server, api, channel):
        # Construction has finished. Restore before HTTP handling; don't patch
        # Worker, native goal, history, transactions or settlement methods.
        if server.Store is not passive_store:
            raise OwnerControlError('owner_store_factory_changed')
        server.Store = original_store
        control.attach(http_server, api, channel)

    try:
        control = LegacyChannelOwnerControl(socket_path)
        server.Store = passive_store
        return server.serve(config, ready=ready)
    finally:
        if server.Store is passive_store:
            server.Store = original_store
        if control is not None:
            control.close()
        _SERVE_LOCK.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Private legacy carrier status/stop; no handoff authority')
    parser.add_argument('--socket', required=True)
    sub = parser.add_subparsers(dest='action')
    service = sub.add_parser('serve')
    service.add_argument('--config', required=True)
    status = sub.add_parser('status')
    status.add_argument('--expected-pid', type=int)
    stop = sub.add_parser('stop')
    stop.add_argument('--expected-pid', type=int, required=True)
    stop.add_argument('--instance-id', required=True)
    stop.add_argument('--timeout', type=float, default=5)
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error('an action is required')
    try:
        if args.action == 'serve':
            serve_with_owner_control(private_json(args.config), args.socket)
        else:
            result = request_owner(args.socket, args.action,
                instance_id=getattr(args, 'instance_id', None),
                timeout=getattr(args, 'timeout', 5), expected_pid=args.expected_pid)
            print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    except Exception:
        print(json.dumps({'ok': False, 'error': 'owner_control_unconfirmed'}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
