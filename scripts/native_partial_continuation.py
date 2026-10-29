"""Private maintenance transport; no model, task recreation or guessed release.

Owner reviews the exact JSON specification and original receipts separately.
Only accept mutates business state; upload stages complete immutable history.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path

from assistant_mesh.config import private_json
from assistant_mesh.native_partial_continuation import MAX_HISTORY, PREFIX, binding
from assistant_mesh.sessions import _fingerprint_metadata, _open_fingerprint_source
from assistant_mesh.worker import Client


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='private dedicated native_maintenance Client config')
    commands = parser.add_subparsers(dest='command')
    inspect = commands.add_parser('inspect'); inspect.add_argument('--task-id', required=True)
    upload = commands.add_parser('upload'); upload.add_argument('--review-file', required=True)
    upload.add_argument('--rollout', required=True)
    accept = commands.add_parser('accept'); accept.add_argument('--review-file', required=True)
    accept.add_argument('--approved-sha256', required=True, help='owner-reviewed exact specification file hash')
    receipt = commands.add_parser('receipt'); receipt.add_argument('--acceptance-id', required=True)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('command required')
    client = Client(private_json(args.config))
    if args.command == 'inspect':
        result = client.request(PREFIX + 'inspect', {'task_id': args.task_id})
    elif args.command == 'receipt':
        result = client.request(PREFIX + 'receipt', {'acceptance_id': args.acceptance_id})
    else:
        # No symlink-following of review files; never print their private bodies.
        source, _ = _open_fingerprint_source(Path(args.review_file))
        with source:
            before = _fingerprint_metadata(os.fstat(source.fileno()))
            raw = source.read(128 * 1024 + 1)
            if _fingerprint_metadata(os.fstat(source.fileno())) != before or len(raw) > 128 * 1024:
                raise ValueError('native_partial_review_file_changed_or_too_large')
        spec = json.loads(raw)
        if args.command == 'accept':
            if hashlib.sha256(raw).hexdigest() != args.approved_sha256:
                raise ValueError('native_partial_exact_review_approval_required')
            result = client.request(PREFIX + 'accept', spec)
        else:
            import base64
            declared = binding(spec['binding'])
            source, _ = _open_fingerprint_source(Path(args.rollout))
            with source:
                before = _fingerprint_metadata(os.fstat(source.fileno()))
                native = source.read(MAX_HISTORY + 1)
                if (_fingerprint_metadata(os.fstat(source.fileno())) != before or len(native) != declared['size']
                        or hashlib.sha256(native).hexdigest() != declared['sha256']):
                    raise ValueError('native_partial_original_history_changed')
            packed = gzip.compress(native, mtime=0)
            parts = (len(packed) + 65535) // 65536
            if parts != spec['parts']:
                raise ValueError('native_partial_reviewed_part_count_changed')
            for part in range(parts):
                client.request(PREFIX + 'history', {'binding': declared, 'part': part,
                    'data': base64.b64encode(packed[part * 65536:(part + 1) * 65536]).decode()})
            result = {'staged': True, 'parts': parts, 'task_changed': False,
                      'acceptance_id': declared['acceptance_id']}
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
