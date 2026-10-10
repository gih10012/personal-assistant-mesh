"""Bounded RFC 6455 JSON transport to an owner-private Unix socket.

This optional transport does not create a server, select a model, manage auth or
change native permissions. It has no dependency beyond Python 3.6's stdlib.
Receive timeouts retain complete and partial frames; failed writes must not be
retried because part of the message may already have reached the native server.
"""
import base64
import errno
import hashlib
import json
import math
import os
import re
import select
import socket
import stat
import struct
import threading
import time


class NativeSocketError(ValueError):
    """A fixed, sanitized local transport or WebSocket protocol error."""


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeSocketError('native_socket_timeout_invalid')
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise NativeSocketError('native_socket_timeout_invalid') from None
    if not math.isfinite(value) or value <= 0:
        raise NativeSocketError('native_socket_timeout_invalid')
    return float(value)


def _private_socket(path):
    """Validate every lexical parent without following symlinks.

    Writable sticky ancestors (for example a system temporary directory) are
    allowed, but the immediate parent must belong to this owner and be 0700.
    No platform without this ownership contract is silently accepted.
    """
    if not hasattr(socket, 'AF_UNIX') or not hasattr(os, 'geteuid'):
        raise NativeSocketError('native_socket_unsupported')
    try:
        path = os.fspath(path)
    except TypeError:
        raise NativeSocketError('native_socket_path_invalid') from None
    if (not isinstance(path, str) or not path.startswith('/') or '\x00' in path
            or any(part in ('.', '..') for part in path.split('/'))
            or path.endswith('/')):
        raise NativeSocketError('native_socket_path_invalid')
    path = os.path.normpath('/' + path.lstrip('/'))
    try:
        os.fsencode(path)
    except UnicodeError:
        raise NativeSocketError('native_socket_path_invalid') from None
    uid = os.geteuid()
    parents, parent = [], os.path.dirname(path)
    while True:
        parents.append(parent)
        if parent == '/':
            break
        parent = os.path.dirname(parent)
    try:
        for parent in reversed(parents):
            entry = os.lstat(parent)
            if not stat.S_ISDIR(entry.st_mode) or entry.st_uid not in (0, uid):
                raise NativeSocketError('native_socket_path_not_private')
            if entry.st_mode & 0o022 and not entry.st_mode & stat.S_ISVTX:
                raise NativeSocketError('native_socket_path_not_private')
        final_parent = os.lstat(parents[0])
        if final_parent.st_uid != uid or stat.S_IMODE(final_parent.st_mode) != 0o700:
            raise NativeSocketError('native_socket_path_not_private')
        entry = os.lstat(path)
        if (not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != uid
                or stat.S_IMODE(entry.st_mode) & 0o077):
            raise NativeSocketError('native_socket_path_not_private')
    except OSError:
        raise NativeSocketError('native_socket_path_unavailable') from None
    return path, (entry.st_dev, entry.st_ino, entry.st_uid, stat.S_IMODE(entry.st_mode))


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise NativeSocketError('websocket_json_duplicate_key')
        result[key] = value
    return result


def _json_constant(value):
    raise NativeSocketError('websocket_json_invalid')


class UnixWebSocket:
    """One full-duplex JSON-object connection; at most one receiver at a time.

    ``timeout`` bounds connecting, handshaking and each send. ``recv_json`` has
    its own total deadline, including control frames. Concurrent writes are
    serialized without altering the receiving socket's timeout. ``close`` is
    idempotent. EOF, receive timeout and protocol/local errors remain distinct.
    """

    LIMIT = 8 * 1024 * 1024
    HEADER_LIMIT = 16384
    _GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
    _TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

    def __init__(self, path, timeout=10):
        self.timeout = _timeout(timeout)
        self.sock = None
        self.buffer = bytearray()
        self.fragment = bytearray()
        self.fragmenting = False
        self._closed = False
        self._send_lock = threading.Lock()
        self._receive_lock = threading.Lock()
        path, identity = _private_socket(path)
        deadline = time.monotonic() + self.timeout
        try:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(self._remaining(deadline))
                self.sock.connect(path)
                self.sock.setblocking(False)
            except socket.timeout:
                raise socket.timeout('websocket_timeout') from None
            except OSError as exc:
                if exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT):
                    raise NativeSocketError('native_socket_unsupported') from None
                raise NativeSocketError('native_socket_connect_failed') from None
            if _private_socket(path)[1] != identity:
                raise NativeSocketError('native_socket_path_changed')
            self._handshake(deadline)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout('websocket_timeout')
        return remaining

    def _ready(self, deadline, writing=False):
        while True:
            if self._closed:
                if writing:
                    raise NativeSocketError('native_socket_closed')
                raise EOFError('websocket_eof')
            try:
                readers, writers, _ = select.select(
                    [] if writing else [self.sock], [self.sock] if writing else [],
                    [], self._remaining(deadline))
            except InterruptedError:
                continue
            except socket.timeout:
                raise socket.timeout('websocket_timeout') from None
            except (OSError, ValueError):
                if self._closed and not writing:
                    raise EOFError('websocket_eof') from None
                raise NativeSocketError('native_socket_io_failed') from None
            if readers or writers:
                return
            raise socket.timeout('websocket_timeout')

    def _more(self, deadline, amount):
        while True:
            self._ready(deadline)
            try:
                chunk = self.sock.recv(min(65536, amount))
            except (BlockingIOError, InterruptedError):
                continue
            except socket.timeout:
                raise socket.timeout('websocket_timeout') from None
            except OSError:
                raise NativeSocketError('native_socket_io_failed') from None
            if not chunk:
                raise EOFError('websocket_eof')
            self.buffer.extend(chunk)
            return

    def _ensure(self, length, deadline):
        while len(self.buffer) < length:
            self._more(deadline, length - len(self.buffer))

    def _write(self, data, deadline):
        view = memoryview(data)
        while view:
            self._ready(deadline, writing=True)
            try:
                written = self.sock.send(view)
            except (BlockingIOError, InterruptedError):
                continue
            except socket.timeout:
                raise socket.timeout('websocket_timeout') from None
            except OSError:
                raise NativeSocketError('native_socket_io_failed') from None
            if written <= 0:
                raise NativeSocketError('native_socket_io_failed')
            view = view[written:]

    def _handshake(self, deadline):
        try:
            key = base64.b64encode(os.urandom(16))
        except OSError:
            raise NativeSocketError('native_socket_io_failed') from None
        request = (b'GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                   b'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                   b'Sec-WebSocket-Key: ' + key + b'\r\n\r\n')
        self._write(request, deadline)
        while True:
            end = self.buffer.find(b'\r\n\r\n')
            if end >= 0:
                if end + 4 > self.HEADER_LIMIT:
                    raise NativeSocketError('websocket_header_limit')
                break
            if len(self.buffer) >= self.HEADER_LIMIT:
                raise NativeSocketError('websocket_header_limit')
            self._more(deadline, min(4096, self.HEADER_LIMIT - len(self.buffer)))
        raw = bytes(self.buffer[:end])
        del self.buffer[:end + 4]
        try:
            lines = raw.decode('ascii').split('\r\n')
        except UnicodeError:
            raise NativeSocketError('websocket_header_invalid') from None
        if not re.fullmatch(r'HTTP/1\.1 101(?: [\x20-\x7e]*)?', lines[0]):
            raise NativeSocketError('websocket_upgrade_status')
        headers = {}
        for line in lines[1:]:
            if ':' not in line:
                raise NativeSocketError('websocket_header_invalid')
            name, value = line.split(':', 1)
            if (not self._TOKEN.fullmatch(name)
                    or any(ord(char) < 32 and char != '\t' or ord(char) == 127
                           for char in value)):
                raise NativeSocketError('websocket_header_invalid')
            name = name.lower()
            if name in headers:
                raise NativeSocketError('websocket_duplicate_header')
            headers[name] = value.strip(' \t')
        expected = base64.b64encode(hashlib.sha1(key + self._GUID).digest()).decode('ascii')
        if (headers.get('sec-websocket-accept') != expected
                or headers.get('upgrade', '').lower() != 'websocket'
                or 'upgrade' not in [part.strip().lower() for part in
                                     headers.get('connection', '').split(',')]
                or 'sec-websocket-extensions' in headers
                or 'sec-websocket-protocol' in headers
                or headers.get('sec-websocket-version', '13') != '13'):
            raise NativeSocketError('websocket_upgrade_invalid')

    def _send(self, opcode, data, deadline=None):
        if len(data) > self.LIMIT:
            raise NativeSocketError('websocket_message_limit')
        if opcode >= 8 and len(data) > 125:
            raise NativeSocketError('websocket_control_invalid')
        send_deadline = time.monotonic() + self.timeout
        deadline = min(deadline, send_deadline) if deadline is not None else send_deadline
        if not self._send_lock.acquire(timeout=self._remaining(deadline)):
            raise socket.timeout('websocket_timeout')
        try:
            length = len(data)
            if length < 126:
                header = bytes((0x80 | opcode, 0x80 | length))
            elif length <= 65535:
                header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack('!H', length)
            else:
                header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack('!Q', length)
            mask = os.urandom(4)
            encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
            self._write(header + mask + encoded, deadline)
        except (NativeSocketError, socket.timeout):
            # A partial outbound frame is not a resumable/retryable operation.
            self.close()
            raise
        except OSError:
            self.close()
            raise NativeSocketError('native_socket_io_failed') from None
        finally:
            self._send_lock.release()

    def send_json(self, value):
        if not isinstance(value, dict):
            raise NativeSocketError('websocket_json_object_required')
        try:
            data = json.dumps(value, ensure_ascii=False, allow_nan=False,
                              separators=(',', ':')).encode('utf8')
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            raise NativeSocketError('websocket_json_invalid') from None
        self._send(1, data)

    def _receive(self, deadline):
        while True:
            self._remaining(deadline)
            self._ensure(2, deadline)
            first, second = self.buffer[:2]
            opcode, final = first & 15, bool(first & 128)
            if first & 112 or second & 128 or opcode not in (0, 1, 8, 9, 10):
                raise NativeSocketError('websocket_frame_invalid')
            length, offset = second & 127, 2
            if opcode >= 8 and (not final or length > 125):
                raise NativeSocketError('websocket_control_invalid')
            if length in (126, 127):
                amount = 2 if length == 126 else 8
                self._ensure(2 + amount, deadline)
                value = struct.unpack('!H' if amount == 2 else '!Q',
                                      bytes(self.buffer[2:2 + amount]))[0]
                if value < (126 if amount == 2 else 65536) or value & (1 << 63):
                    raise NativeSocketError('websocket_length_invalid')
                length, offset = value, 2 + amount
            if length > self.LIMIT or (opcode < 8 and len(self.fragment) + length > self.LIMIT):
                raise NativeSocketError('websocket_message_limit')
            if opcode == 1 and self.fragmenting:
                raise NativeSocketError('websocket_fragment_overlap')
            if opcode == 0 and not self.fragmenting:
                raise NativeSocketError('websocket_fragment_unexpected')
            self._ensure(offset + length, deadline)
            data = bytes(self.buffer[offset:offset + length])
            del self.buffer[:offset + length]
            if opcode == 8:
                self._close_frame(data, deadline)
            if opcode == 9:
                self._send(10, data, deadline)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                self.fragment[:] = data
            else:
                self.fragment.extend(data)
            self.fragmenting = not final
            if final:
                raw = bytes(self.fragment)
                self.fragment[:] = b''
                try:
                    value = json.loads(raw.decode('utf8'), object_pairs_hook=_json_pairs,
                                       parse_constant=_json_constant)
                except (ValueError, UnicodeError, RecursionError):
                    raise NativeSocketError('websocket_json_invalid') from None
                if not isinstance(value, dict):
                    raise NativeSocketError('websocket_json_object_required')
                return value

    def _close_frame(self, data, deadline):
        if len(data) == 1:
            raise NativeSocketError('websocket_close_invalid')
        if data:
            code = struct.unpack('!H', data[:2])[0]
            if (code not in (1000, 1001, 1002, 1003, 1007, 1008, 1009,
                             1010, 1011, 1012, 1013, 1014)
                    and not 3000 <= code <= 4999):
                raise NativeSocketError('websocket_close_invalid')
            try:
                data[2:].decode('utf8')
            except UnicodeError:
                raise NativeSocketError('websocket_close_invalid') from None
        try:
            self._send(8, data, deadline)
        except (NativeSocketError, socket.timeout):
            pass
        raise EOFError('websocket_close')

    def recv_json(self, timeout=10):
        deadline = time.monotonic() + _timeout(timeout)
        if not self._receive_lock.acquire(timeout=self._remaining(deadline)):
            raise socket.timeout('websocket_timeout')
        try:
            return self._receive(deadline)
        except (NativeSocketError, EOFError):
            self.close()
            raise
        finally:
            self._receive_lock.release()

    def close(self):
        if not self._closed:
            self._closed = True
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.sock.close()
                except OSError:
                    pass
