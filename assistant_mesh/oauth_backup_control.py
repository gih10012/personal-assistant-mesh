"""Opt-in Linux owner-UID backup control; no public route or target activation.

All paths and the OAuth document come from trusted factory/lifespan code. The
wire accepts fixed actions and original operation/instance IDs only. This local
control lock does NOT fence another OAuth writer or its restart path.
"""
import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import time

from .oauth_state_bundle import canonical, decode, export_bundle, safe_path, sha, _read, _sync_tree, _write


PROTOCOL = 'mesh-oauth-owner-backup/1'
MAX_REQUEST = 4096


def identity(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', value) is None:
        raise ValueError('oauth_backup_invalid_identity')
    return value


def owner_peer(connection):
    if not hasattr(socket, 'SO_PEERCRED'):
        raise PermissionError('oauth_backup_owner_peer_unavailable')
    pid, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid() or pid <= 0:
        raise PermissionError('oauth_backup_owner_uid_required')
    return pid


def private_directory(value):
    from .enrollment import _private_directory
    return _private_directory(safe_path(value), create=False)


def record(path, value):
    _write(path, (canonical(value) + '\n').encode())
    _sync_tree(path.parent)


def read_record(path):
    return decode(_read(path, 32768))


def failure_code(error):
    if isinstance(error, asyncio.TimeoutError):
        return 'oauth_backup_drain_timeout'
    message = str(error)
    return message if re.fullmatch(r'oauth_[a-z_]{1,120}', message) else 'oauth_backup_failed_closed'


class OwnerBackupControl:
    def __init__(self, boundary, document, config, *, allow_loopback_for_tests=False):
        if (not isinstance(config, dict) or set(config) != {'directory', 'drain_timeout'}
                or type(config['drain_timeout']) is not int or not 1 <= config['drain_timeout'] <= 300):
            raise ValueError('oauth_backup_private_configuration_required')
        self.root = private_directory(config['directory'])
        source = boundary.state_root
        keys = [Path(reference['source_reference']) for reference in boundary.key_references.values()]
        if (source == self.root or source in self.root.parents or self.root in source.parents
                or any(key == self.root or self.root in key.parents for key in keys)):
            raise ValueError('oauth_backup_control_directory_must_be_separate')
        self.boundary, self.document = boundary, json.loads(json.dumps(document))
        self.timeout, self.allow_loopback = config['drain_timeout'], allow_loopback_for_tests
        self.socket_path = self.root / 'control.sock'
        self.operations = self.root / 'operations'
        self.closed = self.root / 'closed.json'
        self.server, self.lock, self.socket_identity = None, None, None
        self.active = None
        self.leases, self.tasks = {}, set()
        self.restart_closed = False

    def _closed_on_disk(self):
        if self.closed.exists():
            return True
        for directory in self.operations.iterdir():
            identity(directory.name); private_directory(directory)
            intent = read_record(directory / 'intent.json')
            if intent.get('protocol') != PROTOCOL or intent.get('operation_id') != directory.name:
                raise ValueError('oauth_backup_operation_journal_invalid')
            # A crashed backup/resume cannot silently reopen the source. Only
            # a durably completed resume makes a prior backup non-blocking.
            if not (directory / 'resume-result.json').exists():
                return True
            resumed = read_record(directory / 'resume-result.json')
            if resumed.get('state') != 'source_resumed_backup_only':
                return True
        return False

    async def start(self):
        if self.server is not None or self.lock is not None:
            raise ValueError('oauth_backup_control_already_started')
        self.operations.mkdir(mode=0o700, exist_ok=True)
        private_directory(self.operations)
        descriptor = os.open(self.root / 'control.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.lock = descriptor
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('oauth_backup_private_control_lock_required')
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.restart_closed = self._closed_on_disk()
            if self.restart_closed:
                self.boundary.phase = 'sealed'
            if self.socket_path.exists() or self.socket_path.is_symlink():
                raise ValueError('oauth_backup_existing_socket_requires_owner_recovery')
            self.server = await asyncio.start_unix_server(self._connection, path=str(self.socket_path), limit=MAX_REQUEST + 1)
            self.socket_path.chmod(0o600)
            info = self.socket_path.lstat()
            self.socket_identity = (info.st_dev, info.st_ino)
            _sync_tree(self.root)
        except BaseException:
            self.boundary.phase = 'sealed'  # An enabled but broken maintenance boundary fails closed.
            await self.close()
            raise

    async def close(self):
        if self.server is not None:
            self.server.close(); await self.server.wait_closed(); self.server = None
        if self.tasks:
            await asyncio.shield(asyncio.gather(*self.tasks, return_exceptions=True))
        if self.socket_identity is not None:
            with contextlib.suppress(FileNotFoundError):
                info = self.socket_path.lstat()
                if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self.socket_identity:
                    self.socket_path.unlink()
            self.socket_identity = None
        if self.lock is not None:
            os.close(self.lock); self.lock = None

    async def _connection(self, reader, writer):
        try:
            owner_peer(writer.get_extra_info('socket'))
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line.endswith(b'\n') or len(line) > MAX_REQUEST:
                raise ValueError('oauth_backup_request_budget')
            reply = await self.dispatch(decode(line))
        except Exception as error:
            reply = {'protocol': PROTOCOL, 'error': failure_code(error), 'request_accepted': False}
        try:
            writer.write((canonical(reply) + '\n').encode())
            await asyncio.wait_for(writer.drain(), 5)
        except (OSError, asyncio.TimeoutError):
            pass  # The durable operation remains queryable after a lost ACK.
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def status(self, operation, instance):
        directory = self.operations / identity(operation)
        if not directory.exists():
            return {'protocol': PROTOCOL, 'operation_id': operation, 'instance_id': instance, 'state': 'not_found'}
        private_directory(directory)
        intent = read_record(directory / 'intent.json')
        if intent.get('instance_id') != instance:
            raise ValueError('oauth_backup_original_instance_changed')
        result = dict(intent)
        for name in ('result.json', 'resume-intent.json', 'resume-result.json'):
            if (directory / name).exists():
                result.update(read_record(directory / name))
        if result['state'] in ('backup_pending', 'resume_pending') and operation != self.active:
            result.update(state='interrupted_original_instance_closed', source_admission='closed')
        result['current_instance_id'] = self.boundary.identity
        result['current_source_phase'] = self.boundary.phase
        return result

    async def dispatch(self, request):
        if not isinstance(request, dict) or request.get('protocol') != PROTOCOL:
            raise ValueError('oauth_backup_protocol_required')
        if request == {'protocol': PROTOCOL, 'action': 'info'}:
            return {'protocol': PROTOCOL, 'instance_id': self.boundary.identity, 'source_phase': self.boundary.phase,
                    'restart_closed': self.restart_closed, 'purpose': 'backup_not_handoff', 'activation_authorized': False}
        if set(request) != {'protocol', 'action', 'operation_id', 'instance_id'}:
            raise ValueError('oauth_backup_exact_request_fields_required')
        action, operation, instance = request['action'], identity(request['operation_id']), identity(request['instance_id'])
        if action not in ('backup', 'status', 'resume'):
            raise ValueError('oauth_backup_action_not_authorized')
        directory = self.operations / operation
        if action == 'status':
            return self.status(operation, instance)
        if instance != self.boundary.identity or self.restart_closed:
            raise ValueError('oauth_backup_original_live_instance_required')
        if action == 'backup':
            if directory.exists():
                return self.status(operation, instance)  # Same operation never exports a second time.
            if self.active is not None or self.closed.exists() or self.boundary.phase != 'open':
                raise ValueError('oauth_backup_source_already_closed')
            directory.mkdir(mode=0o700)
            intent = {'protocol': PROTOCOL, 'operation_id': operation, 'instance_id': instance,
                      'state': 'backup_pending', 'created_at': time.time(), 'purpose': 'backup_not_handoff',
                      'activation_authorized': False, 'cross_process_writer_fenced': False}
            try:
                record(directory / 'intent.json', intent)
                record(self.closed, intent)
            except BaseException:
                self.boundary.phase = 'sealed'
                raise
            self.active = operation
            task = asyncio.create_task(self._backup(operation))
            self.tasks.add(task); task.add_done_callback(self.tasks.discard)
            return dict(intent, source_admission='closing')
        # Resume is explicit, only after a durably committed backup and on the
        # same actual instance/opaque lease. No caller-supplied proof is read.
        current = self.status(operation, instance)
        if current['state'] == 'source_resumed_backup_only':
            return current
        if (current['state'] != 'backup_committed_source_closed' or operation not in self.leases
                or self.active is not None or not self.closed.exists()):
            raise ValueError('oauth_backup_committed_original_lease_required')
        lease = self.leases[operation]
        capture = read_record(directory / 'result.json')
        # Verify the committed bundle's exact canonical self hash and original
        # inactive semantics; this cannot authorize activating any target.
        from .oauth_state_bundle import verify_bundle
        verify_bundle(directory / 'bundle' / 'oauth-state-bundle.json', self.document,
                      expected_sha256=capture['bundle_sha256'], allow_loopback_for_tests=self.allow_loopback)
        record(directory / 'resume-intent.json', {'state': 'resume_pending', 'instance_id': instance})
        try:
            await self.boundary.resume_backup(lease)
            self.closed.unlink(); _sync_tree(self.root)
            record(directory / 'resume-result.json', {'state': 'source_resumed_backup_only',
                'instance_id': instance, 'resumed_at': time.time(), 'source_admission': 'open',
                'handoff_authorized': False})
            self.leases.pop(operation, None)
        except BaseException:
            self.boundary.phase = 'sealed'
            if not self.closed.exists():
                record(self.closed, {'operation_id': operation, 'instance_id': instance, 'state': 'resume_failed_closed'})
            raise
        return self.status(operation, instance)

    async def _backup(self, operation):
        directory = self.operations / operation
        try:
            lease = await self.boundary.freeze(timeout=self.timeout)
            bundle = export_bundle(self.document, directory / 'bundle', lease=lease, allow_loopback_for_tests=self.allow_loopback)
            record(directory / 'result.json', {'state': 'backup_committed_source_closed',
                'bundle_sha256': bundle['sha256'], 'bundle_path': str(directory / 'bundle' / 'oauth-state-bundle.json'),
                'completed_at': time.time(), 'source_admission': 'closed', 'asgi_active': 0, 'sdk_active': 0})
            self.leases[operation] = lease
        except BaseException as error:
            # Preserve the original operation and any partial/exported bytes.
            # Do not retry, issue another lease, or reopen on any failure.
            self.boundary.phase = 'sealed'
            with contextlib.suppress(Exception):
                record(directory / 'result.json', {'state': 'backup_failed_source_closed', 'error': failure_code(error)})
        finally:
            self.active = None
