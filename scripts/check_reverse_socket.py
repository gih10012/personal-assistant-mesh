"""Prepare ONE owner-private SSH reverse socket, never a directory cleanup.

An active socket is never removed. Only a verified stale, owned, private Unix
socket in an owned0700 nonsymlink directory may be unlinked for reconnection.
"""
import argparse
import errno
import json
import os
import socket
import stat
from pathlib import Path


def prepare(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('socket_path_requires_absolute_nonsymlink_path')
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError('socket_parent_requires_owned_0700_directory')
    try:
        current = path.lstat()
    except FileNotFoundError:
        return {'ready': True, 'stale_socket_removed': False}
    if (not stat.S_ISSOCK(current.st_mode) or current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) != 0o600):
        raise ValueError('reverse_target_is_not_private_owned_socket')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        try:
            connection.connect(str(path))
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise ValueError('socket_liveness_uncertain')
        else:
            raise ValueError('reverse_socket_still_active')
    # Check the identity again after the liveness probe, before unlinking.
    checked = path.lstat()
    if (checked.st_dev, checked.st_ino) != (current.st_dev, current.st_ino):
        raise ValueError('socket_changed_during_probe')
    path.unlink()
    return {'ready': True, 'stale_socket_removed': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.path)))


if __name__ == '__main__':
    main()
