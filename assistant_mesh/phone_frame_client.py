"""Read the latest authorized phone frame as the original task worker."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from .config import private_json
from .ingress import _identifier
from .phone_screen_share import PhoneScreenShare
from .phone_screen_share_reader import ROUTE
from .worker import Client


def read_frame(client, lookup, directory):
    root = Path(directory)
    if (not root.is_absolute() or root != root.resolve()
            or any((parent / '.git').exists() for parent in (root,) + tuple(root.parents))):
        raise ValueError('phone_frame_private_directory_required')
    entry = root.stat()
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid() or stat.S_IMODE(entry.st_mode) != 0o700:
        raise ValueError('phone_frame_private_directory_required')

    receipt = client.request(ROUTE, lookup)
    if (not isinstance(receipt, dict) or receipt.get('format') != 'amita-phone-screen-share-frame/1'
            or any(receipt.get(key) != value for key, value in lookup.items())):
        raise ValueError('phone_frame_original_binding_required')
    frame, raw = PhoneScreenShare._frame(receipt.get('frame'))
    # Publish only the verified bytes. mkstemp refuses collisions and sets 0600.
    fd, filename = tempfile.mkstemp(prefix='amita-phone-', suffix='.jpg', dir=str(root))
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
    except BaseException:
        os.unlink(filename)
        raise
    return dict(frame, path=filename)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Original worker configuration')
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--task-epoch', required=True, type=int)
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('--capability-id', required=True)
    parser.add_argument('--directory', required=True, help='Existing 0700 directory outside the repository')
    args = parser.parse_args(argv)
    if args.task_epoch <= 0:
        parser.error('task epoch must be positive')
    lookup = {'task_id': args.task_id, 'task_epoch': args.task_epoch,
              'operation_id': args.operation_id, 'capability_id': args.capability_id}
    try:
        for key in ('task_id', 'operation_id', 'capability_id'):
            _identifier(lookup[key], 'phone_frame_' + key)
        print(json.dumps(read_frame(Client(private_json(args.config)), lookup, args.directory)))
    except (ValueError, OSError):
        print('No confirmed frame for the original task and operation.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
