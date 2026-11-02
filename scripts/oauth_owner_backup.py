"""Local owner-UID OAuth backup control, never target activation.

The socket path comes from the SAME private deployment-owned control document;
wire requests cannot select an OAuth config, destination, key, path or command.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import stat

from assistant_mesh.config import private_json
from assistant_mesh.oauth_backup_control import PROTOCOL, identity, owner_peer, private_directory
from assistant_mesh.oauth_state_bundle import canonical, decode


def request(config, action, operation=None, instance=None):
    if not isinstance(config, dict) or set(config) != {'directory', 'drain_timeout'}:
        raise ValueError('oauth_backup_private_configuration_required')
    root = private_directory(config['directory'])
    path = root / 'control.sock'
    metadata = path.lstat()
    if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise ValueError('oauth_backup_private_socket_required')
    payload = {'protocol': PROTOCOL, 'action': action}
    if action != 'info':
        payload.update(operation_id=identity(operation), instance_id=identity(instance))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(path))
        owner_peer(connection)  # Check server identity as well as its path.
        connection.sendall((canonical(payload) + '\n').encode())
        data = bytearray()
        while b'\n' not in data and len(data) <= 32768:
            block = connection.recv(min(4096, 32769 - len(data)))
            if not block:
                break
            data.extend(block)
        if not data.endswith(b'\n') or len(data) > 32768:
            raise ValueError('oauth_backup_reply_invalid')
        return decode(bytes(data))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-config', required=True, help='existing private deployment-owned backup control JSON')
    parser.add_argument('action', choices=('info', 'backup', 'status', 'resume'))
    parser.add_argument('--operation-id')
    parser.add_argument('--instance-id')
    args = parser.parse_args(argv)
    if (args.action == 'info') != (args.operation_id is None and args.instance_id is None):
        parser.error('info has no IDs; every other action requires both original IDs')
    if args.action != 'info' and (args.operation_id is None or args.instance_id is None):
        parser.error('use the original operation and info instance IDs, including after a lost ACK')
    result = request(private_json(Path(args.control_config)), args.action, args.operation_id, args.instance_id)
    print(json.dumps(result, sort_keys=True))
    return 1 if 'error' in result else 0


if __name__ == '__main__':
    raise SystemExit(main())
