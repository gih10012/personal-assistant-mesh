"""Local verified artifact split/join for independently selected transports.

No network, credentials, SSH execution, installation or native-tool policy. The
caller explicitly chooses the transport and supplies the verified whole size
and SHA256. Existing outputs are never overwritten; private parts are bounded
and joined only after their complete whole-file identity is verified.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from scripts.fetch_artifact import (ArtifactFetchError, CHUNK_SIZE, _ResumeParts,
                                   _already_present, _output_parent)


def identity(size, sha256, workers):
    if (not isinstance(size, int) or isinstance(size, bool) or size <= 0
            or not isinstance(sha256, str) or re.fullmatch(r'[0-9a-f]{64}', sha256) is None
            or not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 32):
        raise ArtifactFetchError('artifact_explicit_identity_required')
    count = min(workers, size)
    quotient, remainder = divmod(size, count)
    return [quotient + (1 if index < remainder else 0) for index in range(count)]


def split(source, directory, size, sha256, workers=8):
    lengths = identity(size, sha256, workers)
    source, source_parent = _output_parent(source)
    parent = None
    try:
        directory, parent = _output_parent(directory)
        if directory.exists() or directory.is_symlink():
            raise ArtifactFetchError('artifact_parts_destination_exists')
        descriptor = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=source_parent)
        with os.fdopen(descriptor, 'rb') as original:
            before = os.fstat(original.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size != size):
                raise ArtifactFetchError('artifact_private_source_required')
            with tempfile.TemporaryDirectory(prefix='.artifact-parts-', dir=str(directory.parent)) as temporary:
                draft = Path(temporary) / 'parts'
                draft.mkdir(mode=0o700)
                digest = hashlib.sha256()
                for index, length in enumerate(lengths):
                    fd = os.open(str(draft / 'part-{:03d}'.format(index)), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as part:
                        remaining = length
                        while remaining:
                            body = original.read(min(CHUNK_SIZE, remaining))
                            if not body:
                                raise ArtifactFetchError('artifact_source_changed')
                            part.write(body)
                            digest.update(body)
                            remaining -= len(body)
                        part.flush()
                        os.fsync(part.fileno())
                after = os.fstat(original.fileno())
                if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        or original.read(1) or digest.hexdigest() != sha256):
                    raise ArtifactFetchError('artifact_sha256_mismatch')
                # Exclusive directory reservation avoids rename-overwrite.
                os.mkdir(directory.name, 0o700, dir_fd=parent)
                for index in range(len(lengths)):
                    name = 'part-{:03d}'.format(index)
                    os.link(str(draft / name), str(directory / name), follow_symlinks=False)
                os.fsync(parent)
        return {'ok': True, 'size': size, 'sha256': sha256, 'parts': len(lengths),
                'sha256_verified': True, 'network_accessed': False}
    finally:
        os.close(source_parent)
        if parent is not None:
            os.close(parent)


def join(directory, output, size, sha256, workers=8):
    lengths = identity(size, sha256, workers)
    output, parent = _output_parent(output)
    parts = None
    try:
        parts = _ResumeParts(directory, lengths)
        if any(parts.metadata[name][5] != length for name, length in zip(parts.names, lengths)):
            raise ArtifactFetchError('artifact_parts_incomplete')
        if _already_present(parent, output.name, size, sha256):
            return {'ok': True, 'size': size, 'sha256': sha256,
                    'sha256_verified': True, 'already_present': True, 'network_accessed': False}
        with tempfile.TemporaryDirectory(prefix='.artifact-join-', dir=str(output.parent)) as temporary:
            complete = Path(temporary) / 'complete'
            descriptor = os.open(str(complete), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as assembled:
                for name in parts.names:
                    parts.hashes[name] = parts._read(name, time.monotonic() + 600, assembled)
                assembled.flush()
                os.fsync(assembled.fileno())
            digest = hashlib.sha256()
            with complete.open('rb') as assembled:
                for body in iter(lambda: assembled.read(CHUNK_SIZE), b''):
                    digest.update(body)
            if complete.stat().st_size != size or digest.hexdigest() != sha256:
                raise ArtifactFetchError('artifact_sha256_mismatch')
            parts.verify(time.monotonic() + 600)
            try:
                os.link(str(complete), output.name, dst_dir_fd=parent, follow_symlinks=False)
                existing = False
                os.fsync(parent)
            except FileExistsError:
                existing = _already_present(parent, output.name, size, sha256)
                if not existing:
                    raise ArtifactFetchError('artifact_output_changed')
        return {'ok': True, 'size': size, 'sha256': sha256, 'parts': len(lengths),
                'sha256_verified': True, 'already_present': existing, 'network_accessed': False}
    finally:
        if parts is not None:
            parts.close()
        os.close(parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('split', 'join'))
    parser.add_argument('--source')
    parser.add_argument('--output')
    parser.add_argument('--parts-directory', required=True)
    parser.add_argument('--size', type=int, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--workers', type=int, default=8)
    args = parser.parse_args()
    try:
        if args.command == 'split':
            result = split(args.source, args.parts_directory, args.size, args.sha256, args.workers)
        else:
            result = join(args.parts_directory, args.output, args.size, args.sha256, args.workers)
    except ArtifactFetchError as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 1
    except Exception:
        print(json.dumps({'ok': False, 'error': 'artifact_parts_operation_failed'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
