"""Bounded Firefox Native Messaging bridge to three owner-ingress operations.

The installed launcher fixes a private client file; messages cannot select a
URL, credential, path, method, native tool or shell. Firefox's extension ID and
the extension's active-tab check are not proof of a ChatGPT login. Dedicated
Mesh ingress identity is assigned by the authority, not browser/page content.
One sendNativeMessage process handles one request, never retries or starts a
worker/model. Stdout contains only framed, explicitly projected JSON.
"""
import json
import os
import re
import stat
import struct
import sys
import urllib.error
from pathlib import Path

from .ingress import Ingress, PROTOCOL as INGRESS_PROTOCOL, _identifier
from .ingress_results import PROTOCOL as RESULT_PROTOCOL, _public_reference, _text
from .worker import Client


EXTENSION_ID = 'personal-assistant-mesh@gih10012.local'
HOST_NAME = 'personal_assistant_mesh'
MAX_INPUT_FRAME = 32768
MAX_OUTPUT_FRAME = 65536
MAX_TASK_TEXT = 16384
_ROUTES = {'submit': '/v1/ingress/tasks', 'status': '/v1/ingress/task/status',
           'result': '/v1/ingress/task/result'}
_STATUSES = frozenset(('pending', 'running', 'completed', 'failed', 'waiting_auth',
                       'waiting_backend', 'waiting_children', 'continuing',
                       'needs_review', 'paused', 'waiting_remote', 'unrecognized'))


class NativeMessageError(ValueError):
    """Fixed error categories only, never exception strings or credentials."""


def _unique(items):
    value = {}
    for key, item in items:
        if key in value:
            raise NativeMessageError('invalid_native_json')
        value[key] = item
    return value


def _nonfinite(value):
    raise NativeMessageError('invalid_native_json')


def _json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise NativeMessageError('invalid_native_json') from None


def _read_exact(stream, size, allow_eof=False):
    chunks, remaining = [], size
    while remaining:
        part = stream.read(remaining)
        if not isinstance(part, bytes):
            raise NativeMessageError('invalid_native_frame')
        if not part:
            if allow_eof and remaining == size:
                return None
            raise NativeMessageError('invalid_native_frame')
        if len(part) > remaining:
            raise NativeMessageError('invalid_native_frame')
        chunks.append(part)
        remaining -= len(part)
    return b''.join(chunks)


def read_message(stream):
    header = _read_exact(stream, 4, allow_eof=True)
    if header is None:
        return None
    size = struct.unpack('=I', header)[0]
    if not 1 <= size <= MAX_INPUT_FRAME:
        raise NativeMessageError('invalid_native_frame')
    raw = _read_exact(stream, size)
    try:
        value = json.loads(raw.decode('utf8'), object_pairs_hook=_unique,
                           parse_constant=_nonfinite)
        _json_bytes(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise NativeMessageError('invalid_native_json') from None


def write_message(stream, value):
    raw = _json_bytes(value)
    if not 1 <= len(raw) <= MAX_OUTPUT_FRAME:
        raise NativeMessageError('native_response_too_large')
    framed = struct.pack('=I', len(raw)) + raw
    remaining = memoryview(framed)
    while remaining:
        count = stream.write(remaining)
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= len(remaining):
            raise NativeMessageError('native_output_unavailable')
        remaining = remaining[count:]
    stream.flush()


def validate_message(value):
    if (not isinstance(value, dict) or set(value) != {'schema', 'action', 'payload'}
            or type(value.get('schema')) is not int or value['schema'] != 1
            or not isinstance(value.get('action'), str) or value['action'] not in _ROUTES):
        raise NativeMessageError('invalid_native_message')
    action, payload = value['action'], value['payload']
    try:
        if action == 'submit':
            request_id, body = Ingress._submit_body(payload)
            if len(body['input'].encode('utf8')) > MAX_TASK_TEXT:
                raise ValueError('too_large')
            body = dict(body, request_id=request_id)
        else:
            if not isinstance(payload, dict) or set(payload) != {'request_id'}:
                raise ValueError('shape')
            body = {'request_id': _identifier(payload['request_id'], 'request_id')}
    except (ValueError, UnicodeError):
        raise NativeMessageError('invalid_native_message') from None
    return action, body


def _task_id(value):
    if not isinstance(value, str) or re.fullmatch(r'ingress-[0-9a-f]{64}', value) is None:
        raise NativeMessageError('invalid_authority_response')
    return value


def project_response(action, request_id, value):
    """Return only the narrow public ingress contract, never raw authority JSON."""
    try:
        if not isinstance(value, dict) or value.get('request_id') != request_id:
            raise ValueError('identity')
        _task_id(value.get('task_id'))
        if value.get('execution_verified') is not False or value.get('native_tools_intercepted') is not False:
            raise ValueError('verification')
        if action in ('submit', 'status'):
            fields = {'format', 'request_id', 'task_id', 'accepted', 'status', 'status_recognized',
                      'result_available', 'execution_verified', 'native_tools_intercepted'}
            if action == 'submit':
                fields.add('task_created')
            if (set(value) != fields or value['format'] != INGRESS_PROTOCOL
                    or value['accepted'] is not True or value['status'] not in _STATUSES
                    or type(value['status_recognized']) is not bool
                    or value['status_recognized'] != (value['status'] != 'unrecognized')
                    or type(value['result_available']) is not bool
                    or action == 'submit' and type(value['task_created']) is not bool):
                raise ValueError('status_shape')
            result = {key: value[key] for key in fields}
        else:
            fields = {'format', 'publication_id', 'request_id', 'task_id', 'published', 'result',
                      'review_verification', 'task_binding_verified', 'execution_verified',
                      'artifact_content_verified', 'native_tools_intercepted'}
            if (set(value) != fields or value['format'] != RESULT_PROTOCOL or value['published'] is not True
                    or value['review_verification'] != 'authenticated_owner_approval'
                    or value['task_binding_verified'] is not True or value['artifact_content_verified'] is not False):
                raise ValueError('result_shape')
            _identifier(value['publication_id'], 'publication_id')
            body = value['result']
            if not isinstance(body, dict) or set(body) != {'summary', 'artifacts'}:
                raise ValueError('result_body')
            summary = _text(body['summary'], 4096, 'summary')
            if not isinstance(body['artifacts'], list) or len(body['artifacts']) > 8:
                raise ValueError('artifact_shape')
            artifacts = []
            for item in body['artifacts']:
                if (not isinstance(item, dict) or not {'url', 'label'} <= set(item)
                        or set(item) - {'url', 'label', 'declared_sha256'}):
                    raise ValueError('artifact_shape')
                artifact = {'url': _public_reference(item['url']), 'label': _text(item['label'], 200, 'label')}
                if 'declared_sha256' in item:
                    if not isinstance(item['declared_sha256'], str) or re.fullmatch(r'[0-9a-f]{64}', item['declared_sha256']) is None:
                        raise ValueError('artifact_sha')
                    artifact['declared_sha256'] = item['declared_sha256']
                artifacts.append(artifact)
            result = {key: value[key] for key in fields - {'result'}}
            result['result'] = {'summary': summary, 'artifacts': artifacts}
        output = {'schema': 1, 'ok': True, 'action': action, 'data': result,
                  'account_verified': False, 'retry_with_new_id': False}
        if len(_json_bytes(output)) > MAX_OUTPUT_FRAME:
            raise ValueError('size')
        return output
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise NativeMessageError('invalid_authority_response') from None


def dispatch(client, message):
    action, payload = validate_message(message)
    try:
        value = client.request(_ROUTES[action], payload)
        return project_response(action, payload['request_id'], value)
    except urllib.error.HTTPError as error:
        # Only HTTP status class is exposed, never URL/reason/private body.
        try:
            error.close()
        except Exception:
            pass
        rejected = error.code in (400, 401, 403, 404, 409, 413)
        return {'schema': 1, 'ok': False, 'error': 'authority_rejected' if rejected else 'authority_unavailable',
                'outcome': 'rejected' if rejected else 'unknown', 'retry_with_new_id': False}
    except Exception:
        # A lost/invalid response can follow a committed submission. No retry,
        # compensating write or replacement ID is performed here.
        return {'schema': 1, 'ok': False, 'error': 'authority_response_unknown',
                'outcome': 'unknown', 'retry_with_new_id': False}


def run_once(input_stream, output_stream, client):
    try:
        message = read_message(input_stream)
        if message is None:
            return 0
        value = dispatch(client, message)
    except Exception:
        value = {'schema': 1, 'ok': False, 'error': 'invalid_native_message',
                 'outcome': 'not_attempted', 'retry_with_new_id': False}
    write_message(output_stream, value)
    return 0


def _private_bytes(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise NativeMessageError('native_config_unavailable')
    for ancestor in path.parents:
        metadata = ancestor.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in (0, os.getuid())
                or metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX):
            raise NativeMessageError('native_config_unavailable')
    parent, named = path.parent.lstat(), path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700 or not stat.S_ISREG(named.st_mode)
            or named.st_uid != os.getuid() or stat.S_IMODE(named.st_mode) != 0o600 or named.st_nlink != 1):
        raise NativeMessageError('native_config_unavailable')
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        opened = os.fstat(source.fileno())
        identity = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_mode,
                                 item.st_nlink, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if identity(opened) != identity(named):
            raise NativeMessageError('native_config_unavailable')
        raw = source.read(32769)
        if len(raw) > 32768 or identity(os.fstat(source.fileno())) != identity(opened):
            raise NativeMessageError('native_config_unavailable')
        return raw


def client_from_config(path):
    try:
        config = json.loads(_private_bytes(path).decode('utf8'), object_pairs_hook=_unique, parse_constant=_nonfinite)
        if (not isinstance(config, dict) or not {'control_url', 'token_file'} <= set(config)
                or set(config) - {'control_url', 'token_file', 'unix_socket'}
                or not isinstance(config['control_url'], str) or not isinstance(config['token_file'], str)):
            raise ValueError('config_shape')
        # Validate private secret shape before constructing the existing client;
        # plaintext is never returned, logged or included in a model message.
        _private_bytes(config['token_file'])
        return Client(config)
    except Exception:
        raise NativeMessageError('native_config_unavailable') from None


def main(config_path, browser_args=None, input_stream=None, output_stream=None):
    """Installed launcher supplies config_path; Firefox supplies exactly two args."""
    args = sys.argv[1:] if browser_args is None else browser_args
    incoming = sys.stdin.buffer if input_stream is None else input_stream
    outgoing = sys.stdout.buffer if output_stream is None else output_stream
    try:
        if (not isinstance(args, (list, tuple)) or len(args) != 2
                or not isinstance(args[0], str) or not Path(args[0]).is_absolute()
                or args[1] != EXTENSION_ID):
            raise NativeMessageError('native_caller_not_allowed')
        client = client_from_config(config_path)
    except Exception:
        write_message(outgoing, {'schema': 1, 'ok': False, 'error': 'native_host_unavailable',
                                 'outcome': 'not_attempted', 'retry_with_new_id': False})
        return 1
    return run_once(incoming, outgoing, client)
