"""Narrow NEW-input continuity after a protocol-confirmed native failure.

Not old-task recovery. A failed/interrupted turn, owned app-server reap AND
actual stdout EOF are required. An unknown turn/timeout has no such permit.
External tool effects remain unknown; the original task is never replayed.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import threading


PROTOCOL = 'mesh-entrance-closed-failure/1'
MAX_LINE = 16 * 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('entrance_failed_history_invalid')
        result[key] = value
    return result


def _constant(unused):
    raise ValueError('entrance_failed_history_invalid')


def history_snapshot(path, thread_id, prefixes=()):
    """Full original JSONL bytes, including failed turns; no summary/repair."""
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts
            or any(parent.is_symlink() for parent in (path,) + tuple(path.parents))):
        raise ValueError('entrance_failed_history_unsafe')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(str(path), flags), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_mode & 0o022):
            raise ValueError('entrance_failed_history_unsafe')
        full, size, count = hashlib.sha256(), 0, 0
        checks = [(item, hashlib.sha256()) for item in prefixes]
        for expected, unused in checks:
            if (not isinstance(expected, dict) or type(expected.get('size')) is not int
                    or not 0 < expected['size'] <= before.st_size
                    or not isinstance(expected.get('sha256'), str)):
                raise ValueError('entrance_failed_history_prefix_invalid')
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE or not line.endswith(b'\n'):
                raise ValueError('entrance_failed_history_incomplete')
            value = json.loads(line.decode('utf8'), object_pairs_hook=_object, parse_constant=_constant)
            if not isinstance(value, dict) or not isinstance(value.get('type'), str):
                raise ValueError('entrance_failed_history_invalid')
            if count == 0 and (value.get('type') != 'session_meta'
                              or value.get('payload', {}).get('id') != thread_id):
                raise ValueError('entrance_failed_history_thread_changed')
            full.update(line)
            for expected, hashed in checks:
                remaining = expected['size'] - size
                if remaining > 0:
                    hashed.update(line[:remaining])
            size, count = size + len(line), count + 1
        after, current = os.fstat(stream.fileno()), path.stat()
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if (not count or size != before.st_size or any(
                getattr(item, key) != getattr(before, key)
                for item in (after, current) for key in fields)):
            raise ValueError('entrance_failed_history_changed')
        if any(hashed.hexdigest() != expected['sha256'] for expected, hashed in checks):
            raise ValueError('entrance_failed_history_prefix_changed')
        return {'size': size, 'sha256': full.hexdigest(), 'records': count,
                'thread_id': thread_id}


def closed_failure_backend(original_backend, callback, prefix_provider):
    """Subclass the real native adapter; never reimplement its RPC/LLM loop."""
    class ClosedFailureBackend(original_backend):
        def __init__(self, *args, **kwargs):
            self._entrance_reader_eof = threading.Event()
            self._entrance_failure_saved = False
            super().__init__(*args, **kwargs)

        def _read(self):
            value = super()._read()
            # The pinned original reader returns normally only at actual EOF.
            # An IO exception is NOT EOF, even though its finally queues eof.
            self._entrance_reader_eof.set()
            return value

        def rpc(self, method, params, *args, **kwargs):
            prefixes = prefix_provider()
            if prefixes and method == 'thread/start':
                raise ValueError('entrance_failed_thread_replacement_forbidden')
            value = super().rpc(method, params, *args, **kwargs)
            if prefixes and method == 'thread/resume':
                thread = value.get('thread', {})
                if thread.get('id') != prefixes[0]['thread_id'] or not thread.get('path'):
                    raise ValueError('entrance_failed_thread_readback_required')
                history_snapshot(thread['path'], thread['id'], [item['history'] for item in prefixes])
            return value

        def __exit__(self, kind, error, traceback):
            original = None
            # These exact errors are raised by finish only AFTER observing the
            # matching native failed turn. Timeout/disconnect/ACK loss NEVER pass.
            if str(error) in ('codex_usage_limit_exceeded', 'codex_turn_failed'):
                completion = getattr(self, 'native_observations', {}).get('turn_completed', {})
                turn = completion.get('turn', {})
                if (completion.get('threadId') == getattr(self, 'thread_id', None)
                        and turn.get('id') == getattr(self, 'turn_id', None)
                        and turn.get('status') in ('failed', 'interrupted')):
                    try:
                        goal = self.goal()  # before close; no post-close RPC
                        # The pinned old adapter can restore a checkpoint goal
                        # on resume. This bounded entrance path permits NO goal
                        # snapshot at all; it never resets old goal accounting.
                        if goal is None:
                            original = self.native_rollout()
                    except Exception:
                        original = None
            returned = super().__exit__(kind, error, traceback)
            if original is not None and not self._entrance_failure_saved:
                process = getattr(self, 'process', None)
                if (process is not None and process.poll() is not None
                        and self._entrance_reader_eof.wait(2)):
                    try:
                        history = history_snapshot(original, self.thread_id,
                            [item['history'] for item in prefix_provider()])
                        proof = {'protocol': PROTOCOL, 'thread_id': self.thread_id,
                            'turn_id': self.turn_id, 'terminal_turn_status': turn['status'],
                            'owned_process_reaped': True, 'owned_reader_eof': True,
                            'runtime_pid': process.pid, 'history': history,
                            'external_effects_settled': False}
                        self._entrance_failure_saved = True  # no second upload after unknown ACK
                        callback(self, original, proof)
                    except Exception:
                        # Keep original native error/history/task guard. Failure
                        # to prove/save continuity is never permission to reset.
                        pass
            return returned
    return ClosedFailureBackend
