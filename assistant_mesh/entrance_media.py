"""Private archive -> existing WeChat skill download bridge; never polls/sends.

Call stage only with the trusted channel's original message, not model arguments.
Returned paths are for the authorized native entrance/Leader context, not HTTP.
Download success is deliberately NOT evidence that an agent understood a file.
"""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

PROTOCOL = 'mesh-entrance-media/1'
MAX_RECORD = 1024 * 1024
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
KINDS = {2: 'image', 3: 'voice', 4: 'file', 5: 'video'}
UTC = datetime.timezone.utc
DOWNLOAD_CODES = frozenset(('entrance_media_timeout', 'entrance_media_runtime_unavailable',
    'entrance_media_sdk_unavailable', 'entrance_media_download_failed',
    'entrance_media_origin_refused', 'entrance_media_too_large',
    'entrance_media_size_mismatch', 'entrance_media_hash_mismatch',
    'entrance_media_http_client_error', 'entrance_media_http_server_error'))


class EntranceMediaError(ValueError):
    """Fixed, non-sensitive codes only; do not print the underlying exception."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('utf8')


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _reference(value):
    if value is None:
        return None
    if type(value) is int:
        return str(value)  # Never round a platform ID through float/JavaScript.
    if not isinstance(value, str) or not 0 < len(value) <= 1024:
        raise EntranceMediaError('entrance_reference_invalid')
    return value


def _utc(value):
    if value is None:
        return None
    try:
        if type(value) in (int, float) and math.isfinite(value):
            result = datetime.datetime.fromtimestamp(value, UTC)
        elif isinstance(value, str):
            result = datetime.datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ') if '.' in value else datetime.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
            result = result.replace(tzinfo=UTC)
        else:
            raise ValueError()
        return result.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError, OSError):
        raise EntranceMediaError('entrance_observation_time_invalid') from None


def message_metadata(message, source_ref, received_at, processed_at=None,
                     request_id=None, task_id=None, reply_to=None,
                     direction='inbound', conversation_id=None, sent_at=None):
    """Keep original and observation times separate; no processing-time fallback.

    iLink's known original field is create_time_ms. Its raw integer/string is
    retained as a string so an ID or millisecond count never loses JSON precision.
    The complete original message remains in the caller's private archive.
    """
    if not isinstance(message, dict) or direction not in ('inbound', 'outbound'):
        raise EntranceMediaError('entrance_message_invalid')
    if source_ref is None:
        raise EntranceMediaError('entrance_reference_invalid')
    raw = message.get('create_time_ms')
    original = {'raw': str(raw) if type(raw) in (int, float, str) else None,
                'unit': 'milliseconds', 'utc': None, 'known': False}
    if type(raw) is int or isinstance(raw, str) and re.fullmatch('[0-9]{1,18}', raw):
        try:
            milliseconds = int(raw)
            if milliseconds > 0:
                date = datetime.datetime(1970, 1, 1, tzinfo=UTC) + datetime.timedelta(milliseconds=milliseconds)
                original.update(utc=date.isoformat(timespec='milliseconds').replace('+00:00', 'Z'), known=True)
        except (ValueError, OverflowError):
            pass
    return {'message_id': _reference(message.get('message_id')), 'source_ref': _reference(source_ref),
            'direction': direction, 'conversation_id': _reference(conversation_id),
            'original_time': original, 'display_timezone': 'Asia/Shanghai', 'received_at': _utc(received_at),
            'processed_at': _utc(processed_at), 'sent_at': _utc(sent_at),
            'reply_to': _reference(reply_to), 'request_id': _reference(request_id),
            'task_id': _reference(task_id)}


def _directory(path, create=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path,) + tuple(path.parents)):
        raise EntranceMediaError('entrance_private_directory_required')
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    value = path.stat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700:
        raise EntranceMediaError('entrance_private_directory_required')
    return path


def _read(path, maximum=MAX_RECORD):
    _directory(path.parent)
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            value = os.fstat(stream.fileno())
            if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                    or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1
                    or value.st_size > maximum):
                raise EntranceMediaError('entrance_private_file_required')
            data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise EntranceMediaError('entrance_record_too_large')
            return data
    except OSError:
        raise EntranceMediaError('entrance_private_file_unavailable') from None


def _write_once(path, data):
    """Complete atomic first-write, never replace an earlier record or asset."""
    _directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.entrance-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, str(path))
        except FileExistsError:
            if _read(path, max(MAX_RECORD, len(data))) != data:
                raise EntranceMediaError('entrance_original_record_changed')
    finally:
        os.unlink(temporary)


# A download-only child isolates optional modern SDK imports from old core Python.
# No account state, token, getupdates, cursor, login, WeChatBot or send is invoked.
# Original CDN/key material enters stdin, never argv, stdout errors or public API.
_SDK_CHILD = r'''
import hashlib, importlib.util, json, os, pathlib, stat, sys
try:
    folder, media_sha, ilink_sha = sys.argv[1:]
    modules = []
    for name, expected in [('ilink', ilink_sha), ('ilink_media', media_sha)]:
        path = pathlib.Path(folder) / (name + '.py')
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022:
            raise ValueError()
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError()
        spec = importlib.util.spec_from_file_location(name, str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        exec(compile(raw, str(path), 'exec'), module.__dict__)
        modules.append(module)
    import importlib.metadata
    if importlib.metadata.version('wechatbot-sdk') != '0.3.0':
        raise ValueError()
    value = json.loads(sys.stdin.buffer.read(1048577))
    data = modules[1].transfer(modules[1].download_async(value['record'], value['max_bytes']))
    sys.stdout.buffer.write(data)
except ImportError:
    sys.exit(21)
except Exception as error:
    code = str(error)
    known = {'BOT_UNTRUSTED_MEDIA_ORIGIN': 23, 'BOT_MEDIA_TIMEOUT': 24,
        'BOT_MEDIA_TOO_LARGE': 25, 'BOT_MEDIA_SIZE_MISMATCH': 26,
        'BOT_MEDIA_HASH_MISMATCH': 27}
    status = known.get(code, 22)
    if code.startswith('BOT_MEDIA_DOWNLOAD_HTTP_4'):
        status = 28
    elif code.startswith('BOT_MEDIA_DOWNLOAD_HTTP_5'):
        status = 29
    sys.exit(status)
'''


class SkillMediaDownloader:
    """Explicit owner-installed, hash-pinned existing wechat-personal component.

    Configure from private owner config, NEVER from a model/tool argument.
    Python may point to an SDK venv or an owner-installed fixed container launcher
    forwarding Python arguments/stdin. Nothing installs or logs in here.
    """
    def __init__(self, media_helper_file, helper_sha256, ilink_sha256, python=None):
        self.path = Path(media_helper_file)
        if (not self.path.is_absolute() or self.path.name != 'ilink_media.py'
                or any(p.is_symlink() for p in (self.path,) + tuple(self.path.parents))
                or any(not isinstance(v, str) or re.fullmatch('[0-9a-f]{64}', v) is None
                       for v in (helper_sha256, ilink_sha256))):
            raise EntranceMediaError('entrance_skill_config_invalid')
        self.helper_sha256, self.ilink_sha256 = helper_sha256, ilink_sha256
        self.python = str(Path(python or sys.executable).resolve(strict=True))
        executable = Path(self.python).stat()
        if executable.st_uid not in (0, os.getuid()) or executable.st_mode & 0o022:
            raise EntranceMediaError('entrance_skill_python_untrusted')

    def __call__(self, record, maximum):
        try:
            result = subprocess.run([self.python, '-I', '-c', _SDK_CHILD, str(self.path.parent),
                                     self.helper_sha256, self.ilink_sha256],
                                    input=_json({'record': record, 'max_bytes': maximum}),
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=55, check=False)
        except subprocess.TimeoutExpired:
            raise EntranceMediaError('entrance_media_timeout') from None
        except OSError:
            raise EntranceMediaError('entrance_media_runtime_unavailable') from None
        if result.returncode:
            codes = {21: 'entrance_media_sdk_unavailable', 23: 'entrance_media_origin_refused',
                     24: 'entrance_media_timeout', 25: 'entrance_media_too_large',
                     26: 'entrance_media_size_mismatch', 27: 'entrance_media_hash_mismatch',
                     28: 'entrance_media_http_client_error', 29: 'entrance_media_http_server_error'}
            raise EntranceMediaError(codes.get(result.returncode, 'entrance_media_download_failed'))
        return result.stdout


class EntranceMedia:
    def __init__(self, root, downloader=None, max_bytes=DEFAULT_MAX_BYTES, clock=None):
        if type(max_bytes) is not int or not 0 < max_bytes <= 100 * 1024 * 1024:
            raise EntranceMediaError('entrance_media_limit_invalid')
        self.root = _directory(root, create=True)
        self.records = _directory(self.root / 'records', create=True)
        self.downloads = _directory(self.root / 'downloads', create=True)
        self.receipts = _directory(self.root / 'receipts', create=True)
        self.downloader, self.max_bytes = downloader, max_bytes
        self.clock = clock or (lambda: datetime.datetime.now(UTC).timestamp())

    def stage(self, message, source_ref, received_at, processed_at=None,
              request_id=None, task_id=None, reply_to=None,
              direction='inbound', conversation_id=None, sent_at=None):
        """Private disk only; safe before/inside original admission transaction.

        If that transaction rolls back, an unreferenced private record can remain;
        it does not poll, consume a cursor, execute, or imply task acceptance.
        """
        metadata = message_metadata(message, source_ref, received_at, processed_at,
            request_id, task_id, reply_to, direction, conversation_id, sent_at)
        items = message.get('item_list', [])
        if not isinstance(items, list) or len(items) > 100 or not all(isinstance(i, dict) for i in items):
            raise EntranceMediaError('entrance_message_items_invalid')
        attachments = []
        for index, item in enumerate(items):
            item_type = item.get('type')
            if type(item_type) is int and item_type == 1:
                continue
            kind = KINDS.get(item_type, 'unrecognized') if type(item_type) is int else 'unrecognized'
            content = item.get(kind + '_item')
            supported = kind in KINDS.values() and isinstance(content, dict) and isinstance(content.get('media'), dict)
            record = {'protocol': PROTOCOL, 'message': metadata, 'item_index': index,
                      'kind': kind, 'item': item, 'supported': supported}
            data = _json(record)
            if len(data) > MAX_RECORD:
                raise EntranceMediaError('entrance_record_too_large')
            attachment_id = _sha(data)
            _write_once(self.records / (attachment_id + '.json'), data)
            attachments.append({'attachment_id': attachment_id, 'item_index': index, 'kind': kind,
                                'state': 'staged' if supported else 'not_supported',
                                'understanding_verified': False})
        return {'protocol': PROTOCOL, 'message': metadata, 'attachments': attachments}

    def materialize(self, attachment_id):
        """Original-ID local/CDN read only; never resend an inbound message."""
        if not isinstance(attachment_id, str) or re.fullmatch('[0-9a-f]{64}', attachment_id) is None:
            raise EntranceMediaError('entrance_attachment_id_invalid')
        data = _read(self.records / (attachment_id + '.json'))
        if _sha(data) != attachment_id:
            raise EntranceMediaError('entrance_original_record_changed')
        try:
            record = json.loads(data)
        except (ValueError, UnicodeError):
            raise EntranceMediaError('entrance_original_record_changed') from None
        if not isinstance(record, dict) or record.get('protocol') != PROTOCOL:
            raise EntranceMediaError('entrance_original_record_changed')
        base = {'protocol': PROTOCOL, 'attachment_id': attachment_id, 'message': record['message'],
                'kind': record['kind'], 'item_index': record['item_index'],
                'understanding_verified': False, 'desktop_required': False}
        receipt_path = self.receipts / (attachment_id + '.json')
        if receipt_path.exists() or receipt_path.is_symlink():
            try:
                receipt = json.loads(_read(receipt_path))
            except (ValueError, UnicodeError):
                raise EntranceMediaError('entrance_cached_receipt_changed') from None
            if (not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in base.items())
                    or receipt.get('state') != 'available'
                    or not isinstance(receipt.get('asset_name'), str)
                    or not re.fullmatch(attachment_id + r'\.[a-z0-9]{1,10}', receipt['asset_name'])):
                raise EntranceMediaError('entrance_cached_receipt_changed')
            path = self.downloads / receipt.get('asset_name', '')
            if path.parent != self.downloads or path.name != receipt.get('asset_name'):
                raise EntranceMediaError('entrance_cached_receipt_changed')
            payload = _read(path, self.max_bytes)
            if len(payload) != receipt.get('size') or _sha(payload) != receipt.get('sha256'):
                raise EntranceMediaError('entrance_cached_asset_changed')
            return dict(receipt, path=str(path), cache_hit=True)
        if not record['supported']:
            return dict(base, state='not_supported', code='entrance_media_reference_unavailable')
        if self.downloader is None:
            return dict(base, state='unavailable', code='entrance_media_not_configured')
        content = record['item'][record['kind'] + '_item']
        try:
            payload = self.downloader({'kind': record['kind'], 'content': content}, self.max_bytes)
        except EntranceMediaError as error:
            return dict(base, state='unavailable', code=str(error) if str(error) in DOWNLOAD_CODES
                        else 'entrance_media_download_failed')
        except Exception:
            return dict(base, state='unavailable', code='entrance_media_download_failed')
        if not isinstance(payload, bytes) or len(payload) > self.max_bytes:
            raise EntranceMediaError('entrance_media_size_invalid')
        name = content.get('file_name', '')
        suffix = Path(name).suffix.lower() if isinstance(name, str) else ''
        if record['kind'] == 'image':
            suffix = '.png' if payload.startswith(b'\x89PNG\r\n\x1a\n') else '.jpg' if payload.startswith(b'\xff\xd8\xff') else '.image'
        if not re.fullmatch(r'\.[a-z0-9]{1,10}', suffix):
            suffix = {'voice': '.silk', 'video': '.mp4'}.get(record['kind'], '.bin')
        asset_name = attachment_id + suffix
        _write_once(self.downloads / asset_name, payload)
        receipt = dict(base, state='available', asset_name=asset_name, size=len(payload),
                       sha256=_sha(payload), file_name=name if isinstance(name, str) else '',
                       downloaded_at=_utc(self.clock()))
        _write_once(receipt_path, _json(receipt))
        return dict(receipt, path=str(self.downloads / asset_name), cache_hit=False)


def public_projection(receipt):
    """Metadata-only operational view, NOT the native agent's private context."""
    keys = ('protocol', 'attachment_id', 'kind', 'item_index', 'state', 'code',
            'size', 'sha256', 'understanding_verified', 'desktop_required', 'cache_hit')
    return {key: receipt[key] for key in keys if key in receipt}
