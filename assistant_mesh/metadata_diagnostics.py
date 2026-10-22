"""Opt-in private protocol metadata, never payloads, retries or recovery.

Compose around the original frozen Worker and its already assembled backend.
Imports create no files, subprocesses, services or model requests.
"""
import contextlib
import datetime
import errno
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import urllib.error


_LOCAL = threading.local()
_RPC = frozenset(('initialize', 'account/read', 'account/rateLimits/read', 'model/list',
    'thread/start', 'thread/resume', 'thread/read', 'thread/goal/get', 'thread/goal/set',
    'turn/start', 'turn/steer', 'turn/interrupt'))
_ROUTES = {'/v1/session': 'control.session', '/v1/claim': 'control.claim',
    '/v1/heartbeat': 'control.heartbeat', '/v1/task/update': 'control.task_update',
    '/v1/agent/action': 'control.agent_action', '/v1/steering': 'control.steering',
    '/v1/interaction': 'control.interaction', '/v1/resources': 'control.resources'}
_METHODS = _RPC | frozenset(_ROUTES.values())
_ACTIONS = frozenset(('upload', 'commit', 'download', 'children', 'notify', 'delegate',
    'wait_children', 'memory', 'plan', 'status'))
_KINDS = frozenset(('client_request', 'native_rpc', 'native_rollout', 'native_goal', 'native_finish', 'native_close',
                    'session_save', 'session_tick'))
_IDENTITY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$')


def _integer(value, maximum=2 ** 63 - 1):
    return value if type(value) is int and 0 <= value <= maximum else None


def _identity(value):
    return value if isinstance(value, str) and _IDENTITY.fullmatch(value) else None


def _bytes(value):
    try:
        return len(json.dumps(value).encode()) if value is not None else 0
    except Exception:
        return None  # Observation cannot replace the original serialization error.


def _exception(exc):
    result = {'error_family': 'other_exception', 'http_status': None, 'errno': None}
    if isinstance(exc, urllib.error.HTTPError):
        result['error_family'] = 'http_error'
        result['http_status'] = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else None
    elif isinstance(exc, TimeoutError):
        result['error_family'] = 'timeout'
    elif isinstance(exc, urllib.error.URLError):
        result['error_family'] = 'url_error'
        if isinstance(exc.reason, OSError):
            result['errno'] = _integer(exc.reason.errno, 4095)
    elif isinstance(exc, OSError):
        result['error_family'] = 'os_error'
    elif isinstance(exc, ValueError):
        result['error_family'] = 'value_error'
    if isinstance(exc, OSError) and result['errno'] is None:
        result['errno'] = _integer(exc.errno, 4095)
    return result


def _native_error(record, value):
    """Only numeric JSON-RPC/upstream codes from a newly observed error."""
    if not isinstance(value, dict):
        return
    code = value.get('code')
    if type(code) is int and -99999 <= code <= 99999:
        record['native_error_code'] = code
    candidates = [value]
    if isinstance(value.get('data'), dict):
        candidates.append(value['data'])
        if isinstance(value['data'].get('codexErrorInfo'), dict):
            candidates.append(value['data']['codexErrorInfo'])
    for candidate in candidates:
        status = candidate.get('httpStatusCode')
        if type(status) is int and 100 <= status <= 599:
            record['http_status'] = status
            break


class MetadataDiagnostics:
    def __init__(self, path=None, enabled=False):
        self.enabled = enabled is True
        self.path, self.dropped = path, 0

    @contextlib.contextmanager
    def bind(self, task_provider):
        previous = getattr(_LOCAL, 'frame', None)
        _LOCAL.frame = {'recorder': self, 'task_provider': task_provider, 'session': None}
        try:
            yield
        finally:
            _LOCAL.frame = previous

    def _task(self):
        try:
            frame = getattr(_LOCAL, 'frame', None)
            value = frame['task_provider']() if frame and frame['recorder'] is self else None
            return {'task_id': _identity(value.get('id')), 'epoch': _integer(value.get('epoch'))} if isinstance(value, dict) else {'task_id': None, 'epoch': None}
        except Exception:
            return {'task_id': None, 'epoch': None}

    def _write(self, value):
        """A write failure is intentionally invisible to native execution."""
        if not self.enabled:
            return
        descriptor = None
        try:
            path = Path(self.path)
            if not path.is_absolute() or '..' in path.parts:
                raise OSError(errno.EPERM, 'private_log_required')
            for parent in (path.parent,) + tuple(path.parent.parents):
                entry = parent.lstat()
                if (not stat.S_ISDIR(entry.st_mode) or entry.st_uid not in (0, os.getuid())
                        or (entry.st_mode & 0o022 and not entry.st_mode & stat.S_ISVTX)):
                    raise OSError(errno.EPERM, 'private_log_required')
            if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
                raise OSError(errno.EPERM, 'private_log_required')
            descriptor = os.open(str(path), os.O_APPEND | os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW |
                                 getattr(os, 'O_NONBLOCK', 0), 0o600)
            entry = os.fstat(descriptor)
            if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid()
                    or entry.st_nlink != 1 or entry.st_mode & 0o077):
                raise OSError(errno.EPERM, 'private_log_required')
            data = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
            if os.write(descriptor, data) != len(data):
                raise OSError(errno.ENOSPC, 'private_log_incomplete')
            os.fsync(descriptor)
        except Exception:
            self.dropped += 1
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @contextlib.contextmanager
    def span(self, kind, method=None, action=None, part=None, payload_bytes=None):
        started = time.monotonic()
        record = dict(self._task(), schema='mesh-private-metadata/1',
            kind=kind if kind in _KINDS else 'other',
            method=method if isinstance(method, str) and method in _METHODS else 'other',
            action=action if isinstance(action, str) and action in _ACTIONS else None,
            part=_integer(part), payload_bytes=_integer(payload_bytes),
            outcome='returned', http_status=None, errno=None, error_family=None)
        try:
            yield record
        except BaseException as exc:
            record['outcome'] = 'raised'
            try:
                record.update({key: value for key, value in _exception(exc).items() if value is not None})
            except Exception:
                self.dropped += 1
            raise  # Preserve the exact original exception object and traceback.
        finally:
            try:
                frame = getattr(_LOCAL, 'frame', None)
                session = frame.get('session') if frame and frame['recorder'] is self else None
                if session is not None:
                    record['substep'] = session['phase'] + ('_tick' if kind == 'session_tick' else '')
                    record['session_part'] = session['part']
                else:
                    record['substep'] = kind if kind in _KINDS else 'other'
                record['elapsed_ms'] = round(max(0, time.monotonic() - started) * 1000, 3)
                record['observed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self._write(record)
            except Exception:
                self.dropped += 1


def observe_client(client, recorder):
    if not recorder.enabled:
        return client

    class ObservedClient:
        def __getattr__(self, name):
            return getattr(client, name)

        def request(self, path, body=None):
            # Never log raw URL/query/path, arbitrary action values or body.
            route = path.split('?', 1)[0] if isinstance(path, str) else None
            route = route if route in _ROUTES else None
            action = body.get('action') if isinstance(body, dict) else None
            payload = body.get('payload') if isinstance(body, dict) else None
            part = payload.get('part') if isinstance(payload, dict) else None
            frame = getattr(_LOCAL, 'frame', None)
            if (route == '/v1/session' and frame and frame['recorder'] is recorder
                    and frame.get('session') is not None):
                frame['session'].update(phase='session_' + (action if action in ('upload', 'commit', 'download') else 'other'),
                                        part=_integer(part))
            with recorder.span('client_request', method=_ROUTES.get(route), action=action, part=part,
                               payload_bytes=_bytes(body)) as record:
                record['http_method'] = 'POST' if body is not None else 'GET'
                return client.request(path, body)  # Exactly one original call.

    return ObservedClient()


def observe_backend(backend, recorder):
    if not recorder.enabled or not isinstance(backend, type):
        return backend

    class ObservedBackend(backend):
        def rpc(self, method, params, *args, **kwargs):
            before = getattr(self, 'last_protocol_error', None)
            with recorder.span('native_rpc', method=method, payload_bytes=_bytes(params)) as record:
                try:
                    return super(ObservedBackend, self).rpc(method, params, *args, **kwargs)
                except BaseException:
                    try:
                        current = getattr(self, 'last_protocol_error', None)
                        if current is not before:
                            _native_error(record, current)
                    except Exception:
                        recorder.dropped += 1
                    raise

        def native_rollout(self, *args, **kwargs):
            with recorder.span('native_rollout'):
                return super(ObservedBackend, self).native_rollout(*args, **kwargs)

        def goal(self, *args, **kwargs):
            with recorder.span('native_goal'):
                return super(ObservedBackend, self).goal(*args, **kwargs)

        def finish(self, *args, **kwargs):
            with recorder.span('native_finish'):
                return super(ObservedBackend, self).finish(*args, **kwargs)

        def close(self, *args, **kwargs):
            with recorder.span('native_close'):
                return super(ObservedBackend, self).close(*args, **kwargs)

    return ObservedBackend


def install_save_observer(sessions_module, enabled=False):
    """Wrap this process's save function; only opted-in Worker threads observe.

    No copy of save's compression/upload loop, no second save, no extra tick.
    Unbound/disabled callers go straight to the original function.
    """
    if enabled is not True:
        return False
    original = sessions_module.save
    if getattr(original, '_private_metadata_observer', False):
        return True

    def observed_save(*args, **kwargs):
        frame = getattr(_LOCAL, 'frame', None)
        if not frame or not frame['recorder'].enabled:
            return original(*args, **kwargs)
        recorder = frame['recorder']
        previous = frame['session']
        frame['session'] = {'phase': 'session_pack', 'part': None}
        positional, options = list(args), dict(kwargs)
        tick = positional[6] if len(positional) > 6 else options.get('tick')
        if callable(tick):
            def observed_tick(*tick_args, **tick_kwargs):
                with recorder.span('session_tick'):
                    return tick(*tick_args, **tick_kwargs)
            if len(positional) > 6:
                positional[6] = observed_tick
            else:
                options['tick'] = observed_tick
        try:
            with recorder.span('session_save'):
                return original(*positional, **options)
        finally:
            frame['session'] = previous

    observed_save._private_metadata_observer = True
    sessions_module.save = observed_save
    return True


def observe_worker(worker_class, recorder):
    if not recorder.enabled:
        return worker_class

    class ObservedWorker(worker_class):
        def __init__(self, *args, **kwargs):
            super(ObservedWorker, self).__init__(*args, **kwargs)
            self.client = observe_client(self.client, recorder)
            # Wrap AFTER governance/EntranceWorker assemble their decorators;
            # preserve role profiles, closed-failure hooks and native methods.
            self.backend = observe_backend(self.backend, recorder)

        def run_once(self):
            with recorder.bind(lambda: self.current):
                return super(ObservedWorker, self).run_once()

    return ObservedWorker
