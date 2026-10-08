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


def save(client, task, node, harness, state, rollout=None, tick=None):
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
