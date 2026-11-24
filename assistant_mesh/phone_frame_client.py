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
from .phone_screen_share import MAX_FRAME_AGE_SECONDS, PhoneScreenShare, _digest, _finite
from .phone_screen_share_reader import LOOKUP_FIELDS, ROUTE
from .worker import Client


STREAM_FIELDS = {'stream_id', 'binding_digest'}
VISUAL_FIELDS = {'revision', 'sha256', 'width', 'height'}
EXPECTATION_FIELDS = LOOKUP_FIELDS | STREAM_FIELDS | VISUAL_FIELDS


class FrameStaleError(ValueError):
    pass


def validate_expectation(value):
    if (not isinstance(value, dict) or set(value) != EXPECTATION_FIELDS
            or any(type(value[key]) is not int or not 0 < value[key] < 2**53
                   for key in ('task_epoch', 'revision'))
            or any(type(value[key]) is not int or not 0 < value[key] <= 640 for key in ('width', 'height'))
            or any(not _digest(value[key]) for key in ('sha256', 'binding_digest'))):
        raise ValueError('phone_frame_expectation_required')
    for key in ('task_id', 'operation_id', 'capability_id', 'stream_id'):
        _identifier(value[key], 'phone_frame_' + key)


def fetch_frame(client, lookup):
    receipt = client.request(ROUTE, lookup)
    if (not isinstance(receipt, dict) or receipt.get('format') != 'amita-phone-screen-share-frame/1'
            or any(receipt.get(key) != value for key, value in lookup.items())):
        raise ValueError('phone_frame_original_binding_required')
    frame, raw = PhoneScreenShare._frame(receipt.get('frame'))
    expectation = {key: receipt.get(key) for key in LOOKUP_FIELDS | STREAM_FIELDS}
    expectation.update({key: frame[key] for key in VISUAL_FIELDS})
    validate_expectation(expectation)
    return frame, raw, expectation, receipt.get('source_fresh_for_seconds')


def verify_frame(client, expected, clock):
    """Return the local deadline for a fresh match on the exact original stream."""
    validate_expectation(expected)
    started = clock()
    actual, fresh_for = fetch_frame(client, {key: expected[key] for key in LOOKUP_FIELDS})[2:]
    if not _finite(fresh_for) or not 0 <= fresh_for <= MAX_FRAME_AGE_SECONDS:
        raise ValueError('phone_frame_source_freshness_unavailable')
    # Charging the whole round trip avoids comparing different hosts' clocks.
    deadline = started + fresh_for
    if (clock() >= deadline or actual['revision'] < expected['revision']
            or any(actual[key] != expected[key] for key in (STREAM_FIELDS | VISUAL_FIELDS) - {'revision'})):
        raise FrameStaleError('paired_phone_frame_stale')
    return deadline


def read_frame(client, lookup, directory):
    root = Path(directory)
    if (not root.is_absolute() or root != root.resolve()
            or any((parent / '.git').exists() for parent in (root,) + tuple(root.parents))):
        raise ValueError('phone_frame_private_directory_required')
    entry = root.stat()
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid() or stat.S_IMODE(entry.st_mode) != 0o700:
        raise ValueError('phone_frame_private_directory_required')

    frame, raw, expectation, _ = fetch_frame(client, lookup)
    # Publish only the verified bytes. mkstemp refuses collisions and sets 0600.
    fd, filename = tempfile.mkstemp(prefix='amita-phone-', suffix='.jpg', dir=str(root))
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
    except BaseException:
        os.unlink(filename)
        raise
    return dict(frame, path=filename, frame_expectation=expectation)


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
