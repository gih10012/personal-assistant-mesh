"""Optional native CLI for the unified, task-bound Mesh workspace.

Private owner configuration selects credentials and read-only transports. The
model supplies an immutable choice, never a source path/URL/executable. This is
not an HTTP installer or a replacement for the running Worker. Preserve the
journal across upgrades; unknown writes are never retried by this CLI.
"""
import argparse
import base64
import hashlib
import json
import math
import os
import re
from pathlib import Path

from .artifact_transport import MAX_ARTIFACT_BYTES, _decode
from .egress import _bounded_capture
from .provider_runtime import _owner_bytes
from .resources import _epoch, _name
from .workspace import MeshWorkspace, WorkspaceError


_PLACEHOLDER = '{request_base64url}'
_DISCOVERY_KEYS = {'kind', 'issuer', 'include_unavailable', 'limit',
                   'observation_max_age_seconds'}
_CALL_KEYS = {'issuer', 'decision_id', 'selection', 'candidates', 'rationale',
              'lease_seconds', 'project_id', 'agent_id'}


def _outside_worktree(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or any(
            (parent / '.git').exists() for parent in (path,) + tuple(path.parents)):
        raise WorkspaceError('workspace_private_path_required')
    return path


def _private_json(path, maximum=196608, expected_sha=None):
    raw = _owner_bytes(_outside_worktree(path), maximum, 'workspace_private_file_invalid')
    if expected_sha is not None and (
            not isinstance(expected_sha, str) or re.fullmatch(r'[0-9a-f]{64}', expected_sha) is None
            or hashlib.sha256(raw).hexdigest() != expected_sha):
        raise WorkspaceError('workspace_owner_file_changed')
    return _decode(raw, maximum)


class CommandArtifactReader:
    """Owner-reviewed read-only native/SSH command, not arbitrary model exec.

    Exactly one whole argv placeholder receives shell-safe base64url JSON of
    three original IDs. No raw model strings are interpolated into SSH's remote
    shell. Fixed commands must authenticate the source and read original data
    only; this adapter cannot prove that an arbitrary trusted command is benign.
    The native agent retains its ordinary Shell access independently.
    """

    def __init__(self, binding):
        if not isinstance(binding, dict) or set(binding) != {
                'manifest_argv', 'read_argv', 'timeout_seconds'}:
            raise WorkspaceError('workspace_reader_config_invalid')
        self.commands = {}
        for action in ('manifest', 'read'):
            command = binding[action + '_argv']
            if (not isinstance(command, list) or not 2 <= len(command) <= 64
                    or any(not isinstance(arg, str) or not arg or len(arg) > 4096
                           or '\0' in arg for arg in command)
                    or command.count(_PLACEHOLDER) != 1
                    or any('{' in arg or '}' in arg for arg in command if arg != _PLACEHOLDER)
                    or not Path(command[0]).is_absolute()
                    or not os.access(command[0], os.X_OK)):
                raise WorkspaceError('workspace_reader_config_invalid')
            self.commands[action] = tuple(command)
        timeout = binding['timeout_seconds']
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise WorkspaceError('workspace_reader_config_invalid')
        self.timeout = timeout

    def _capture(self, action, message_id, operation_id, capability_id):
        request = {key: _name(value, key) for key, value in (
            ('message_id', message_id), ('operation_id', operation_id),
            ('capability_id', capability_id))}
        raw = json.dumps(request, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf8')
        token = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
        command = [token if arg == _PLACEHOLDER else arg for arg in self.commands[action]]
        maximum = 16384 if action == 'manifest' else MAX_ARTIFACT_BYTES
        try:
            code, captured = _bounded_capture(command, maximum, self.timeout)
            if code != 0:
                raise ValueError()
        except Exception:
            raise WorkspaceError('workspace_original_reader_unavailable') from None
        return captured

    def manifest(self, message_id, operation_id, capability_id):
        return _decode(self._capture('manifest', message_id, operation_id, capability_id), 16384)

    def read(self, message_id, operation_id, capability_id):
        return self._capture('read', message_id, operation_id, capability_id)


def load_workspace(owner_file, task_id, epoch, client_factory=None):
    """All identities/transports are private runtime input, not tool arguments."""
    _name(task_id, 'task_id')
    _epoch(epoch)
    config = _private_json(owner_file, 65536)
    required = {'format', 'client_config', 'client_config_sha256', 'authority',
                'node', 'journal_path', 'artifact_dir', 'artifact_readers'}
    if (set(config) != required or config['format'] != 'mesh-workspace-owner/1'
            or not isinstance(config['artifact_readers'], dict)
            or len(config['artifact_readers']) > 32):
        raise WorkspaceError('workspace_owner_config_invalid')
    client_config = _private_json(config['client_config'], 65536, config['client_config_sha256'])
    if client_config.get('node_id') != config['node']:
        raise WorkspaceError('workspace_owner_node_mismatch')
    journal = _outside_worktree(config['journal_path'])
    artifacts = (_outside_worktree(config['artifact_dir'])
                 if config['artifact_dir'] is not None else None)
    readers = {_name(issuer, 'issuer'): CommandArtifactReader(binding)
               for issuer, binding in config['artifact_readers'].items()}
    if client_factory is None:
        from .worker import Client
        client_factory = Client
    return MeshWorkspace(client_factory(client_config), str(journal), config['authority'],
                         config['node'], task_id, epoch,
                         str(artifacts) if artifacts is not None else None, readers)


def execute(owner_file, task_id, epoch, action, operation_id=None, payload_file=None,
            client_factory=None):
    if action not in ('discover', 'call', 'status', 'artifact'):
        raise WorkspaceError('workspace_arguments_invalid')
    if action in ('status', 'artifact') and (not operation_id or payload_file is not None):
        raise WorkspaceError('workspace_arguments_invalid')
    if action == 'discover' and operation_id is not None:
        raise WorkspaceError('workspace_arguments_invalid')
    if action == 'call' and (not operation_id or payload_file is None):
        raise WorkspaceError('workspace_arguments_invalid')
    payload = _private_json(payload_file) if payload_file is not None else {}
    if set(payload) - (_CALL_KEYS if action == 'call' else _DISCOVERY_KEYS):
        raise WorkspaceError('workspace_payload_fields_invalid')
    workspace = load_workspace(owner_file, task_id, epoch, client_factory)
    if action == 'discover':
        return workspace.discover(**payload)
    if action == 'call':
        return workspace.call(operation_id=operation_id, **payload)
    return getattr(workspace, action)(operation_id)


def _error():
    return {'status': 'error', 'diagnostics': ['workspace_unavailable_or_invalid'],
            'automatic_invocation_replay': False, 'retry_with_new_id': False,
            'native_tools_intercepted': False, 'execution_verified': False}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps(_error()), flush=True)
        self.exit(2)


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('--config', required=True, help='private owner config, not auth inline')
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--epoch', type=int, required=True)
    parser.add_argument('--operation-id')
    parser.add_argument('--payload-file')
    parser.add_argument('action', choices=('discover', 'call', 'status', 'artifact'))
    args = parser.parse_args(argv)
    try:
        value = execute(args.config, args.task_id, args.epoch, args.action,
                        args.operation_id, args.payload_file)
    except Exception:
        print(json.dumps(_error()), flush=True)
        return 2
    print(json.dumps(value, sort_keys=True, ensure_ascii=False), flush=True)
    return 2 if (value.get('phase') in ('unresolved', 'original_epoch_read_only',
                                       'artifact_unavailable')
                 or value.get('operation_state') == 'unknown') else 0


if __name__ == '__main__':
    raise SystemExit(main())
