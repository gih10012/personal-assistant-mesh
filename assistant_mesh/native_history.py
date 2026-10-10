"""Read-only native byte-history evidence, never a summary or replay command."""
import hashlib
import json
import os
from pathlib import Path

from . import sessions


CHUNK_SIZE = 65536
MAX_RECORD_BYTES = 16 * 1024 * 1024
IDENTITY_FIELDS = ('dev', 'ino', 'uid', 'mode')
PREFIX_FIELDS = set(IDENTITY_FIELDS) | {'size', 'sha256'}


def _path(value):
    try:
        path = Path(value)
    except (TypeError, ValueError):
        raise ValueError('native_history_path_unsafe') from None
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('native_history_path_unsafe')
    return path


def _thread(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('native_history_thread_invalid')
    return value


def _open(path):
    try:
        return sessions._open_fingerprint_source(path)
    except ValueError as exc:
        code = ('native_history_unsupported' if str(exc) == 'native_session_fingerprint_unsupported'
                else 'native_history_path_unsafe')
        raise ValueError(code) from None


def _metadata(source):
    try:
        return sessions._fingerprint_metadata(os.fstat(source.fileno()))
    except OSError:
        raise ValueError('native_history_unstable') from None


def _check_binding(path, source, ancestors, initial, append=False):
    opened, current_ancestors = _open(path)
    with opened:
        current = _metadata(opened)
    actual = _metadata(source)
    if (current_ancestors != ancestors or any(
            current[key] != initial[key] or actual[key] != initial[key]
            for key in IDENTITY_FIELDS)):
        raise ValueError('native_history_identity_changed')
    if append:
        # The fixed prefix can be read while the runtime appends more bytes.
        # Such appends legitimately change file size, mtime and ctime.
        if current['size'] < initial['size'] or actual['size'] < initial['size']:
            raise ValueError('native_history_prefix_changed')
        if any(metadata['size'] == initial['size'] and any(
                metadata[key] != initial[key] for key in ('mtime_ns', 'ctime_ns'))
                for metadata in (current, actual)):
            raise ValueError('native_history_prefix_changed')
    elif actual != initial or current != initial:
        raise ValueError('native_history_unstable')


def _read(source, length):
    try:
        return source.read(length)
    except OSError:
        raise ValueError('native_history_unstable') from None


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('native_history_jsonl_invalid')
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError('native_history_jsonl_invalid')


def _record(line):
    try:
        value = json.loads(line.decode('utf8'), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('native_history_jsonl_invalid') from None
    if (not isinstance(value, dict) or not isinstance(value.get('type'), str)
            or not value['type']):
        raise ValueError('native_history_jsonl_invalid')
    return value


def _session_metadata(value, thread_id):
    payload = value.get('payload')
    if (value.get('type') != 'session_meta' or not isinstance(payload, dict)
            or payload.get('id') != thread_id):
        raise ValueError('native_history_metadata_invalid')


def _prefix_read(source, size, tick, metadata_thread=None):
    source.seek(0)
    before = _metadata(source)
    digest, remaining, first = hashlib.sha256(), size, bytearray()
    metadata_seen = metadata_thread is None
    while remaining:
        if tick:
            tick()
        block = _read(source, min(CHUNK_SIZE, remaining))
        if not block:
            raise ValueError('native_history_prefix_changed')
        digest.update(block)
        remaining -= len(block)
        if not metadata_seen:
            split = block.find(b'\n')
            first.extend(block if split < 0 else block[:split + 1])
            if len(first) > MAX_RECORD_BYTES:
                raise ValueError('native_history_record_oversized')
            if split >= 0:
                _session_metadata(_record(bytes(first)), metadata_thread)
                metadata_seen = True
                first.clear()
    if not metadata_seen:
        raise ValueError('native_history_metadata_incomplete')
    after = _metadata(source)
    if (after['size'] == before['size'] and any(after[key] != before[key]
            for key in ('mtime_ns', 'ctime_ns'))):
        raise ValueError('native_history_prefix_changed')
    return digest.hexdigest()


def capture_prefix(path, thread_id, tick=None):
    """Capture a finite original byte prefix without requiring an idle writer.

    Only the session_meta first line must be complete here; an append in flight
    may leave a partial later record. Two bounded reads agree on the selected
    bytes. This is not a lock against an owner-UID concurrent writer.
    """
    path, thread_id = _path(path), _thread(thread_id)
    opened, ancestors = _open(path)
    with opened as source:
        initial = _metadata(source)
        if tick:
            tick()
        _check_binding(path, source, ancestors, initial, append=True)
        first = _prefix_read(source, initial['size'], tick, metadata_thread=thread_id)
        _check_binding(path, source, ancestors, initial, append=True)
        second = _prefix_read(source, initial['size'], tick)
        _check_binding(path, source, ancestors, initial, append=True)
        if first != second:
            raise ValueError('native_history_prefix_changed')
    fingerprint = {key: initial[key] for key in IDENTITY_FIELDS + ('size',)}
    fingerprint['sha256'] = first
    return {'path': str(path), 'fingerprint': fingerprint}


def _prefix(value, path):
    if (not isinstance(value, dict) or set(value) != {'path', 'fingerprint'}
            or not isinstance(value['path'], str) or value['path'] != str(path)
            or not isinstance(value['fingerprint'], dict)
            or set(value['fingerprint']) != PREFIX_FIELDS):
        raise ValueError('native_history_prefix_invalid')
    try:
        expected = sessions._expected_fingerprint(value['fingerprint'])
    except ValueError:
        raise ValueError('native_history_prefix_invalid') from None
    if not expected['size']:
        raise ValueError('native_history_prefix_invalid')
    return expected


def _turns(value):
    if (not isinstance(value, (list, tuple, set, frozenset)) or any(
            not isinstance(identity, str) or not identity.strip() for identity in value)
            or len(set(value)) != len(value)):
        raise ValueError('native_history_turns_invalid')
    return {identity: {'started': False, 'completed': False} for identity in value}


def _turn_record(value, end_offset, prefix_size, turns):
    if value['type'] != 'event_msg' or not isinstance(value.get('payload'), dict):
        return
    payload = value['payload']
    if not isinstance(payload.get('type'), str):
        return
    kind = {'task_started': 'started', 'turn_started': 'started',
            'task_complete': 'complete', 'turn_complete': 'complete',
            'turn_aborted': 'aborted'}.get(payload.get('type'))
    identity = payload.get('turn_id')
    if kind is None:
        return  # Unknown and compacted native content remains opaque.
    after_prefix = end_offset > prefix_size
    if not isinstance(identity, str) or not identity.strip():
        if kind == 'aborted' and any(
                selected['started'] and not selected['completed'] for selected in turns.values()):
            raise ValueError('native_history_turn_aborted')
        if after_prefix:
            raise ValueError('native_history_turns_invalid')
        return  # Do not reinterpret unrelated older native event formats.
    if identity not in turns:
        if after_prefix:
            raise ValueError('native_history_turn_unsettled')
        return  # Historical errors/interruptions are not this run's completion.
    selected = turns[identity]
    if kind == 'aborted' or (kind == 'complete' and payload.get('error') is not None):
        raise ValueError('native_history_turn_aborted')
    if kind == 'started':
        if selected['started'] or selected['completed']:
            raise ValueError('native_history_turns_invalid')
        selected['started'] = True
    elif not selected['started'] or selected['completed']:
        raise ValueError('native_history_turns_invalid')
    else:
        selected['completed'] = True


def verify_history(path, thread_id, prefix, closed_turn_ids, tick=None):
    """Verify a closed runtime's complete original JSONL and selected turns.

    No file content is rewritten or returned. A finite per-record bound rejects
    unsupported large records rather than truncating native history. The caller
    must separately prove runtime closure and use the returned fingerprint when
    publishing; this read alone neither settles effects nor forbids native work.
    """
    path, thread_id = _path(path), _thread(thread_id)
    expected, turns = _prefix(prefix, path), _turns(closed_turn_ids)
    opened, ancestors = _open(path)
    with opened as source:
        initial = _metadata(source)
        if any(initial[key] != expected[key] for key in IDENTITY_FIELDS):
            raise ValueError('native_history_identity_changed')
        if initial['size'] < expected['size']:
            raise ValueError('native_history_prefix_changed')
        if tick:
            tick()
        _check_binding(path, source, ancestors, initial)
        digest, prefix_digest = hashlib.sha256(), hashlib.sha256()
        total, offset, records, pending = 0, 0, 0, bytearray()
        while total < initial['size']:
            block = _read(source, min(CHUNK_SIZE, initial['size'] - total))
            if not block:
                raise ValueError('native_history_unstable')
            digest.update(block)
            prefix_digest.update(block[:max(0, expected['size'] - total)])
            total += len(block)
            pending.extend(block)
            while True:
                newline = pending.find(b'\n')
                if newline < 0:
                    if len(pending) > MAX_RECORD_BYTES:
                        raise ValueError('native_history_record_oversized')
                    break
                length = newline + 1
                if length > MAX_RECORD_BYTES:
                    raise ValueError('native_history_record_oversized')
                line = bytes(pending[:length])
                del pending[:length]
                value = _record(line)
                offset += length
                records += 1
                if records == 1:
                    _session_metadata(value, thread_id)
                    if offset > expected['size']:
                        raise ValueError('native_history_prefix_invalid')
                else:
                    _turn_record(value, offset, expected['size'], turns)
            if tick:
                tick()
        if pending:
            raise ValueError('native_history_jsonl_incomplete')
        if not records:
            raise ValueError('native_history_metadata_incomplete')
        if prefix_digest.hexdigest() != expected['sha256']:
            raise ValueError('native_history_prefix_changed')
        if any(not value['started'] or not value['completed'] for value in turns.values()):
            raise ValueError('native_history_turn_unsettled')
        _check_binding(path, source, ancestors, initial)
    fingerprint = dict(initial, sha256=digest.hexdigest())
    return {'path': str(path), 'fingerprint': fingerprint}
