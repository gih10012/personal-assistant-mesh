"""Transport ONLY a selected native rollout. Codex owns context and compaction."""
import base64
import datetime
import gzip
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path


def request(client, task, action, payload):
    return client.request('/v1/session', {'task_id': task['id'], 'epoch': task['epoch'],
                                         'action': action, 'payload': payload})


def save(client, task, node, harness, state, rollout=None, tick=None, expected_fingerprint=None):
    """Stream the native artifact; optionally require an unchanged frozen file.

    Strict evidence uses dev/ino/uid/S_IMODE mode/size/lowercase sha256 plus
    optional mtime_ns/ctime_ns. Failure never commits or retries uploaded parts;
    it does not delete them, settle effects, or alter the caller's native state.
    """
    if expected_fingerprint is not None:
        return _save_fingerprinted(client, task, node, harness, state, rollout, tick,
                                   expected_fingerprint)
    parts = 0
    if rollout:
        # Streaming compression and bounded chunks avoid holding a long native
        # conversation in RAM or truncating it to an application-written summary.
        with tempfile.TemporaryFile() as packed:
            with gzip.GzipFile(fileobj=packed, mode='wb') as archive, Path(rollout).open('rb') as source:
                while True:
                    block = source.read(65536)
                    if not block:
                        break
                    archive.write(block)
                    if tick:
                        tick()
            packed.seek(0)
            while True:
                block = packed.read(65536)
                if not block:
                    break
                request(client, task, 'upload', {'harness': harness, 'part': parts,
                                                'data': base64.b64encode(block).decode()})
                parts += 1
                if tick:
                    tick()
    return request(client, task, 'commit', {'harness': harness, 'state': dict(state, codex_node=node), 'parts': parts})


def _expected_fingerprint(value):
    required = {'dev', 'ino', 'uid', 'mode', 'size', 'sha256'}
    optional = {'mtime_ns', 'ctime_ns'}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - optional
            or any(not isinstance(value[key], int) or isinstance(value[key], bool)
                   or value[key] < 0 for key in set(value) - {'sha256'})
            or value['mode'] > 0o7777
            or not isinstance(value['sha256'], str)
            or re.fullmatch('[0-9a-f]{64}', value['sha256']) is None):
        raise ValueError('native_session_fingerprint_invalid')
    return dict(value)  # A caller cannot mutate the expected evidence mid-save.


def _fingerprint_metadata(metadata):
    return {'dev': metadata.st_dev, 'ino': metadata.st_ino,
            'uid': metadata.st_uid, 'mode': stat.S_IMODE(metadata.st_mode),
            'size': metadata.st_size, 'mtime_ns': metadata.st_mtime_ns,
            'ctime_ns': metadata.st_ctime_ns}


def _open_fingerprint_source(path):
    """Walk an absolute trusted path without following any symlink.

    Directory identities exclude times: unrelated entries may change safely.
    This does not lock out an owner-UID writer or make a remote commit atomic
    with the final local check. It never chmods or rewrites the source file.
    """
    if (not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY')
            or os.open not in getattr(os, 'supports_dir_fd', ())):
        raise ValueError('native_session_fingerprint_unsupported')
    directories, identities, descriptor = [], [], None
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    try:
        directory = os.open(path.anchor, flags | os.O_DIRECTORY)
        directories.append(directory)
        for component in (None,) + path.parts[1:-1]:
            if component is not None:
                directory = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
                directories.append(directory)
            metadata = os.fstat(directory)
            if (not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid not in (0, os.getuid())
                    or (metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX)):
                raise ValueError('native_session_fingerprint_path_unsafe')
            identities.append((metadata.st_dev, metadata.st_ino, metadata.st_uid,
                               stat.S_IMODE(metadata.st_mode)))
        # NONBLOCK avoids hanging on a substituted FIFO before the fstat check.
        descriptor = os.open(path.name, flags | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError('native_session_fingerprint_path_unsafe')
        # Re-verification must read the actual descriptor, not cached bytes
        # retained by a BufferedReader across seeks and external in-place writes.
        source = os.fdopen(descriptor, 'rb', buffering=0)
        descriptor = None
        return source, tuple(identities)
    except OSError:
        raise ValueError('native_session_fingerprint_path_unsafe') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)


def _check_fingerprint_identity(path, source, expected, ancestors, initial):
    # Walk again as well as inspecting the original descriptor: a replaced
    # path must not pass merely because its old inode remains open. Keep the
    # ancestry from this exact walk, not a separate path stat.
    opened, current_ancestors = _open_fingerprint_source(path)
    with opened:
        reopened = _fingerprint_metadata(os.fstat(opened.fileno()))
    actual = _fingerprint_metadata(os.fstat(source.fileno()))
    fields = set(expected) - {'sha256'}
    if (current_ancestors != ancestors or actual != initial
            or reopened != actual or any(actual[key] != expected[key] for key in fields)):
        raise ValueError('native_session_fingerprint_changed')


def _check_fingerprint_bytes(size, digest, expected):
    if size != expected['size'] or digest.hexdigest() != expected['sha256']:
        raise ValueError('native_session_fingerprint_changed')


def _verify_fingerprint(path, source, expected, ancestors, initial, tick):
    if tick:
        tick()
    _check_fingerprint_identity(path, source, expected, ancestors, initial)
    source.seek(0)
    digest, size = hashlib.sha256(), 0
    while True:
        block = source.read(65536)
        if not block:
            break
        size += len(block)
        if size > expected['size']:
            raise ValueError('native_session_fingerprint_changed')
        digest.update(block)
        if tick:
            tick()
    _check_fingerprint_bytes(size, digest, expected)
    _check_fingerprint_identity(path, source, expected, ancestors, initial)
    source.seek(0)


def _save_fingerprinted(client, task, node, harness, state, rollout, tick, value):
    expected = _expected_fingerprint(value)
    if not rollout:
        raise ValueError('native_session_fingerprint_rollout_required')
    path = Path(rollout)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('native_session_fingerprint_path_unsafe')
    opened, ancestors = _open_fingerprint_source(path)
    with opened as source, tempfile.TemporaryFile() as packed:
        initial = _fingerprint_metadata(os.fstat(source.fileno()))
        # Always retain time baselines, even when optional times were omitted.
        _verify_fingerprint(path, source, expected, ancestors, initial, tick)
        size, digest = 0, hashlib.sha256()
        with gzip.GzipFile(fileobj=packed, mode='wb') as archive:
            while True:
                block = source.read(65536)
                if not block:
                    break
                size += len(block)
                if size > expected['size']:
                    raise ValueError('native_session_fingerprint_changed')
                digest.update(block)
                archive.write(block)
                if tick:
                    tick()
        _check_fingerprint_bytes(size, digest, expected)
        _verify_fingerprint(path, source, expected, ancestors, initial, tick)
        packed.seek(0)
        parts = 0
        while True:
            block = packed.read(65536)
            if not block:
                break
            request(client, task, 'upload', {'harness': harness, 'part': parts,
                                            'data': base64.b64encode(block).decode()})
            parts += 1
            if tick:
                tick()
        # Already-uploaded but uncommitted parts are not deleted or replayed.
        # This final complete read also detects same-size writes during upload.
        _verify_fingerprint(path, source, expected, ancestors, initial, tick)
        return request(client, task, 'commit', {'harness': harness,
                       'state': dict(state, codex_node=node), 'parts': parts})


def _selected_thread_id(task):
    """Use the fenced task's native identity, never the artifact's claimed ID."""
    native = (task.get('sessions') or {}).get('codex')
    if native is None:
        candidate = task.get('session')
        if candidate and candidate.get('harness') == 'codex':
            native = candidate
    if native is not None:
        if native.get('harness') != 'codex':
            raise ValueError('native_session_harness_mismatch')
        state = native.get('state') or {}
    else:
        state = task.get('checkpoint') or {}
        if state.get('harness', 'codex') != 'codex':
            raise ValueError('native_session_selected_thread_required')
    identity = state.get('thread_id')
    try:
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('native_session_selected_thread_required') from None
    return identity


def _private_directory(folder):
    folder.mkdir(mode=0o700, exist_ok=True)
    metadata = folder.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022):
        raise ValueError('native_session_directory_unsafe')


def _checked_restore_directory(folder):
    """Validate before mkdir/download, without changing existing permissions.

    Existing ancestors must be root/owner-controlled, not symlinks, and not
    group/world-writable unless sticky (for example root-owned /tmp). An owned,
    non-writable-to-others destination below that shared ancestor is required. Native
    Codex directories may already be 0755; their non-writability plus 0600
    rollout files is sufficient here. This is a trusted-path boundary, not an
    openat-based defense against malicious concurrent processes with our UID.
    """
    if not folder.is_absolute() or '..' in folder.parts:
        raise ValueError('native_session_restore_absolute_path_required')
    for directory in (folder,) + tuple(folder.parents):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            continue  # newly created components are checked again below
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError('native_session_restore_ancestor_symlink')
        if (not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in (0, os.getuid())
                or (metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX)):
            raise ValueError('native_session_restore_ancestor_unsafe')


def _canonical_destination(path, folder, identity):
    try:
        with path.open('rb') as source:
            metadata = json.loads(source.readline())
        payload = metadata.get('payload')
        if metadata.get('type') != 'session_meta' or not isinstance(payload, dict):
            raise ValueError()
        if payload.get('id') != identity:
            raise ValueError('native_session_thread_mismatch')
        timestamp = payload.get('timestamp')
        match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)',
                             timestamp if isinstance(timestamp, str) else '')
        if not match:
            raise ValueError()
        zone = '+0000' if match.group(2) == 'Z' else match.group(2).replace(':', '')
        instant = datetime.datetime.strptime(match.group(1) + zone, '%Y-%m-%dT%H:%M:%S%z')
        instant = instant.astimezone(datetime.timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        if str(exc) == 'native_session_thread_mismatch':
            raise
        raise ValueError('native_session_metadata_invalid') from None
    destination = folder
    for component in (instant.strftime('%Y'), instant.strftime('%m'), instant.strftime('%d')):
        destination = destination / component
        _private_directory(destination)
    return destination / ('rollout-' + instant.strftime('%Y-%m-%dT%H-%M-%S') + '-' + identity + '.jsonl')


def _digest(source):
    result = hashlib.sha256()
    while True:
        block = source.read(65536)
        if not block:
            return result.digest()
        result.update(block)


def _publish_selected(path, destination):
    # Hard-link publication is atomic and refuses any existing path. In
    # particular, never truncate a newer native continuation or follow symlinks.
    try:
        os.link(str(path), str(destination))
    except FileExistsError:
        metadata = destination.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077):
            raise ValueError('native_session_destination_unsafe')
        descriptor = os.open(str(destination), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as existing, path.open('rb') as incoming:
            opened = os.fstat(existing.fileno())
            if (opened.st_ino != metadata.st_ino or opened.st_dev != metadata.st_dev
                    or _digest(existing) != _digest(incoming)):
                raise ValueError('native_session_destination_conflict')
    return str(destination)


def restore(client, task, folder, tick=None, harness='codex'):
    folder = Path(folder)
    identity = _selected_thread_id(task) if harness == 'codex' else None
    if harness == 'codex' and folder.name != 'sessions':
        raise ValueError('native_session_sessions_directory_required')
    _checked_restore_directory(folder)
    folder.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _checked_restore_directory(folder)
    _private_directory(folder)
    with tempfile.TemporaryFile() as packed:
        part = 0
        while True:
            value = request(client, task, 'download', {'part': part, 'harness': harness})['data']
            if value is None:
                break
            packed.write(base64.b64decode(value, validate=True))
            part += 1
            if tick:
                tick()
        if not part:
            raise ValueError('native_session_artifact_empty')
        packed.seek(0)
        path = None
        try:
            with tempfile.NamedTemporaryFile(dir=str(folder), prefix='native-rollout-', suffix='.jsonl', delete=False) as output:
                path = Path(output.name)
                with gzip.GzipFile(fileobj=packed, mode='rb') as archive:
                    while True:
                        block = archive.read(65536)
                        if not block:
                            break
                        output.write(block)
                        if tick:
                            tick()
                output.flush()
                os.fsync(output.fileno())
            if harness == 'codex':
                destination = _canonical_destination(path, folder, identity)
                return _publish_selected(path, destination)
            result = str(path)
            path = None  # Pi's opaque native session retains the staged filename.
            return result
        finally:
            if path is not None:
                path.unlink()
