"""Native owner-file-authorized, read-only capability artifact pipe.

Fixed producer installation/SSH routing is separate from these arguments. This
CLI never requests or retries HTTP, reserves capacity, changes task state or
creates an artifact grant. Producer ``read`` stdout is private binary content:
pipe it directly to the consumer, not to a terminal or agent transcript.
"""
import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _error(category):
    print(json.dumps({'status': 'error', 'diagnostics': [category],
        'native_features_restricted': False, 'http_request_replayed': False,
        'managed_artifact_grant_verified': False}), file=sys.stderr, flush=True)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        _error('artifact_cli_arguments_invalid')
        self.exit(2)


def main(argv=None):
    parser = _Parser(description=__doc__)
    modes = parser.add_subparsers(dest='mode')
    producer = modes.add_parser('producer')
    producer.add_argument('action', choices=('manifest', 'read'))
    producer.add_argument('--owner-config', required=True)
    producer.add_argument('--owner-config-sha256', required=True)
    producer.add_argument('--message-id', required=True)
    producer.add_argument('--operation-id', required=True)
    producer.add_argument('--capability-id', required=True)
    consumer = modes.add_parser('consumer')
    consumer.add_argument('--manifest-file', required=True)
    consumer.add_argument('--output-file', required=True)
    consumer.add_argument('--max-artifact-bytes', type=int, default=24 * 1024 * 1024)
    args = parser.parse_args(argv)
    if args.mode not in ('producer', 'consumer'):
        _error('artifact_cli_arguments_invalid')
        return 2
    try:
        from assistant_mesh.artifact_transport import ArtifactProducer, fetch_artifact
        if args.mode == 'producer':
            reader = ArtifactProducer(args.owner_config, args.owner_config_sha256)
            if args.action == 'manifest':
                value = reader.manifest(args.message_id, args.operation_id, args.capability_id)
                print(json.dumps(value, sort_keys=True), flush=True)
            else:
                if sys.stdout.isatty():
                    _error('artifact_private_pipe_required')
                    return 2
                reader.write_to(args.message_id, args.operation_id, args.capability_id,
                                sys.stdout.buffer)
                sys.stdout.buffer.flush()
        else:
            from assistant_mesh.provider_runtime import _owner_bytes, _unique_object
            raw = _owner_bytes(args.manifest_file, 65536, 'artifact_manifest_unavailable')
            manifest = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object)
            value = fetch_artifact(sys.stdin.buffer, manifest, args.output_file,
                                   max_artifact_bytes=args.max_artifact_bytes)
            print(json.dumps(value, sort_keys=True), flush=True)
        return 0
    except Exception:
        # Source errors have fixed codes, but local IO/SSH/parser exceptions can
        # contain private paths, bodies or credentials. Never print them here.
        _error('artifact_transfer_failed')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
