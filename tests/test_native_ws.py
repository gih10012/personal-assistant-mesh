import base64
import hashlib
import json
import os
import socket
import stat
import struct
import tempfile
import threading
import unittest
from unittest import mock

from assistant_mesh.native_ws import NativeSocketError, UnixWebSocket


GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def frame(data=b'', opcode=1, final=True):
    first = opcode | (128 if final else 0)
    if len(data) < 126:
        return bytes((first, len(data))) + data
    if len(data) <= 65535:
        return bytes((first, 126)) + struct.pack('!H', len(data)) + data
    return bytes((first, 127)) + struct.pack('!Q', len(data)) + data


def exact(peer, length):
    value = b''
    while len(value) < length:
        part = peer.recv(length - len(value))
        if not part:
            raise EOFError('test_peer_eof')
        value += part
    return value


def client_frame(peer):
    first, second = exact(peer, 2)
    if not second & 128:
        raise AssertionError('client_frame_not_masked')
    length = second & 127
    if length == 126:
        length = struct.unpack('!H', exact(peer, 2))[0]
    elif length == 127:
        length = struct.unpack('!Q', exact(peer, 8))[0]
    mask = exact(peer, 4)
    raw = exact(peer, length)
    return first, bytes(value ^ mask[index % 4] for index, value in enumerate(raw))


def upgrade(key):
    accept = base64.b64encode(hashlib.sha1(key + GUID).digest())
    return (b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
            b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')


class Fixture:
    def __init__(self, test, behavior=None, response=None, initial=b'', directory=None):
        self.test = test
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.path = self.temporary.name + '/socket'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        os.chmod(self.path, 0o600)
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.peer = None
        self.errors = []
        self.done = threading.Event()
        self.request = None

        def serve():
            try:
                self.peer = self.listener.accept()[0]
                self.peer.settimeout(2)
                request = b''
                while b'\r\n\r\n' not in request:
                    part = self.peer.recv(1024)
                    if not part:
                        return  # A path-identity refusal can precede HTTP.
                    request += part
                    if len(request) > 4096:
                        raise AssertionError('test_request_limit')
                self.request = request
                key = [line.split(b': ', 1)[1] for line in request.split(b'\r\n')
                       if line.startswith(b'Sec-WebSocket-Key: ')][0]
                raw = (response or upgrade)(key)
                self.peer.sendall(raw + initial)
                if behavior:
                    behavior(self.peer)
            except BaseException as exc:
                self.errors.append(exc)
            finally:
                if self.peer is not None:
                    self.peer.close()
                self.listener.close()
                self.done.set()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        test.addCleanup(self.cleanup)

    def finish(self):
        self.test.assertTrue(self.done.wait(3), 'fixture_did_not_finish')
        self.test.assertEqual(self.errors, [])

    def cleanup(self):
        self.listener.close()
        if self.peer is not None:
            try:
                self.peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.thread.join(3)
        self.temporary.cleanup()
        self.test.assertFalse(self.thread.is_alive(), 'fixture_thread_leaked')


@unittest.skipUnless(hasattr(socket, 'AF_UNIX') and hasattr(os, 'geteuid'),
                     'optional owner-private Unix transport is unavailable')
class NativeWebSocket(unittest.TestCase):
    def connect(self, fixture, timeout=1):
        client = UnixWebSocket(fixture.path, timeout=timeout)
        self.addCleanup(client.close)
        return client

    def test_masked_json_unicode_and_handshake(self):
        received = []
        fixture = Fixture(self, lambda peer: received.append(client_frame(peer)))
        client = self.connect(fixture)
        client.send_json({'text': '连续记忆', 'id': 2})
        fixture.finish()
        self.assertEqual(received[0][0], 129)
        self.assertEqual(json.loads(received[0][1].decode('utf8')),
                         {'text': '连续记忆', 'id': 2})
        self.assertIn(b'Sec-WebSocket-Version: 13\r\n', fixture.request)
        key = [line.split(b': ', 1)[1] for line in fixture.request.split(b'\r\n')
               if line.startswith(b'Sec-WebSocket-Key: ')][0]
        self.assertEqual(len(base64.b64decode(key)), 16)

    def test_masked_extended_lengths(self):
        for size in (200, 70000):
            with self.subTest(size=size):
                received = []
                fixture = Fixture(self, lambda peer: received.append(client_frame(peer)))
                client = self.connect(fixture)
                client.send_json({'text': 'x' * size})
                fixture.finish()
                self.assertEqual(received[0][0], 129)
                self.assertEqual(json.loads(received[0][1].decode()), {'text': 'x' * size})

    def test_concurrent_writes_are_whole_masked_frames(self):
        received, errors = [], []
        fixture = Fixture(self, lambda peer: received.extend([client_frame(peer), client_frame(peer)]))
        client = self.connect(fixture)
        ready = threading.Barrier(3)

        def send(number):
            try:
                ready.wait(2)
                client.send_json({'id': number, 'data': 'x' * 200000})
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=send, args=(number,)) for number in (1, 2)]
        for worker in workers:
            worker.start()
        ready.wait(2)
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        fixture.finish()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(json.loads(raw.decode())['id'] for _, raw in received), [1, 2])
        self.assertTrue(all(first == 129 for first, _ in received))

    def test_handshake_preserves_pipelined_complete_messages(self):
        fixture = Fixture(self, initial=frame(b'{"id":1}') + frame(b'{"id":2}'))
        client = self.connect(fixture)
        self.assertEqual(client.recv_json(), {'id': 1})
        self.assertEqual(client.recv_json(), {'id': 2})
        fixture.finish()

    def test_fragmented_utf8_ping_and_ignored_pong(self):
        data = '{"x":"中"}'.encode('utf8')
        cut = data.index('中'.encode('utf8')) + 1
        replies = []
        initial = (frame(data[:cut], final=False) + frame(b'ping', opcode=9)
                   + frame(b'ignored', opcode=10) + frame(data[cut:], opcode=0))
        fixture = Fixture(self, lambda peer: replies.append(client_frame(peer)), initial=initial)
        client = self.connect(fixture)
        self.assertEqual(client.recv_json(), {'x': '中'})
        fixture.finish()
        self.assertEqual(replies, [(138, b'ping')])

    def test_control_payload_does_not_count_toward_fragment_limit(self):
        data = b'{"x":"' + b'x' * 120 + b'"}'
        self.assertEqual(len(data), 128)
        replies = []
        fixture = Fixture(self, lambda peer: replies.append(client_frame(peer)), initial=(
            frame(data[:100], final=False) + frame(b'p' * 125, opcode=9)
            + frame(data[100:], opcode=0)))
        client = self.connect(fixture)
        client.LIMIT = 128
        self.assertEqual(client.recv_json(), {'x': 'x' * 120})
        fixture.finish()
        self.assertEqual(replies, [(138, b'p' * 125)])

    def partial_timeout(self, prefix, suffix, expected, fragment=b''):
        first_sent, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def serve(peer):
            peer.sendall(prefix)
            first_sent.set()
            if not release.wait(2):
                raise AssertionError('test_release_missing')
            peer.sendall(suffix)

        fixture = Fixture(self, serve)
        client = self.connect(fixture)
        self.assertTrue(first_sent.wait(2))
        with self.assertRaisesRegex(socket.timeout, '^websocket_timeout$'):
            client.recv_json(.02)
        self.assertFalse(client._closed)
        self.assertEqual(bytes(client.fragment), fragment)
        self.assertTrue(client.buffer)
        release.set()
        self.assertEqual(client.recv_json(), expected)
        fixture.finish()

    def test_partial_base_header_survives_timeout(self):
        self.partial_timeout(b'\x81', b'\x07{"x":2}', {'x': 2})

    def test_partial_extended_header_survives_timeout(self):
        data = b'{"x":"' + b'x' * 192 + b'"}'
        self.assertEqual(len(data), 200)
        raw = frame(data)
        self.partial_timeout(raw[:3], raw[3:], {'x': 'x' * 192})

    def test_partial_payload_survives_timeout(self):
        raw = frame(b'{"x":2}')
        self.partial_timeout(raw[:5], raw[5:], {'x': 2})

    def test_completed_fragment_and_partial_continuation_survive_timeout(self):
        self.partial_timeout(frame(b'{"x', final=False) + b'\x80\x04":',
                             b'2}', {'x': 2}, fragment=b'{"x')

    def test_control_frames_do_not_reset_receive_deadline(self):
        got_pong, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def serve(peer):
            peer.sendall(frame(b'p', opcode=9))
            self.assertEqual(client_frame(peer), (138, b'p'))
            got_pong.set()
            release.wait(2)

        fixture = Fixture(self, serve)
        client = self.connect(fixture)
        with self.assertRaisesRegex(socket.timeout, '^websocket_timeout$'):
            client.recv_json(.03)
        self.assertTrue(got_pong.wait(2))
        self.assertFalse(client._closed)
        release.set()
        fixture.finish()

    def test_receive_lock_timeout_keeps_connection_and_buffer(self):
        fixture = Fixture(self, initial=frame(b'{"id":1}'))
        client = self.connect(fixture)
        client._receive_lock.acquire()
        try:
            with self.assertRaisesRegex(socket.timeout, '^websocket_timeout$'):
                client.recv_json(.02)
            self.assertFalse(client._closed)
        finally:
            client._receive_lock.release()
        self.assertEqual(client.recv_json(), {'id': 1})
        fixture.finish()

    def test_pong_write_uses_receive_deadline_and_failed_write_closes(self):
        fixture = Fixture(self, initial=frame(b'p', opcode=9))
        client = self.connect(fixture)
        recorded = []

        def failed_write(data, deadline):
            recorded.append(deadline)
            raise socket.timeout('websocket_timeout')

        with mock.patch.object(client, '_write', side_effect=failed_write):
            with self.assertRaisesRegex(socket.timeout, '^websocket_timeout$'):
                client.recv_json(.03)
        self.assertEqual(len(recorded), 1)
        self.assertTrue(client._closed)
        fixture.finish()

    def test_eof_is_distinct_and_fixed(self):
        fixture = Fixture(self)
        client = self.connect(fixture)
        with self.assertRaisesRegex(EOFError, '^websocket_eof$'):
            client.recv_json()
        fixture.finish()
        self.assertTrue(client._closed)

    def test_valid_close_is_echoed_masked_then_eof(self):
        data = struct.pack('!H', 1000) + '完成'.encode('utf8')
        replies = []
        fixture = Fixture(self, lambda peer: replies.append(client_frame(peer)),
                          initial=frame(data, opcode=8))
        client = self.connect(fixture)
        with self.assertRaisesRegex(EOFError, '^websocket_close$'):
            client.recv_json()
        fixture.finish()
        self.assertEqual(replies, [(136, data)])
        self.assertTrue(client._closed)

    def test_empty_close_is_valid(self):
        replies = []
        fixture = Fixture(self, lambda peer: replies.append(client_frame(peer)),
                          initial=frame(opcode=8))
        client = self.connect(fixture)
        with self.assertRaisesRegex(EOFError, '^websocket_close$'):
            client.recv_json()
        fixture.finish()
        self.assertEqual(replies, [(136, b'')])

    def assert_protocol(self, raw, error):
        fixture = Fixture(self, initial=raw)
        client = self.connect(fixture)
        with self.assertRaisesRegex(NativeSocketError, '^' + error + '$'):
            client.recv_json()
        self.assertTrue(client._closed)
        fixture.finish()

    def test_close_validation(self):
        invalid = [b'x', struct.pack('!H', 1000) + b'\xff']
        invalid.extend(struct.pack('!H', code) for code in
                       (999, 1004, 1005, 1006, 1015, 2000, 5000))
        for data in invalid:
            with self.subTest(data=data):
                self.assert_protocol(frame(data, opcode=8), 'websocket_close_invalid')

    def test_frame_flags_binary_and_reserved_opcodes_refused(self):
        for raw in (b'\xc1\x00', b'\xa1\x00', b'\x91\x00', b'\x81\x80',
                    b'\x82\x00', b'\x83\x00', b'\x8b\x00'):
            with self.subTest(raw=raw):
                self.assert_protocol(raw, 'websocket_frame_invalid')

    def test_control_fragment_or_extended_length_refused(self):
        for raw in (b'\x09\x00', b'\x08\x00', b'\x89\x7e', b'\x8a\x7f'):
            with self.subTest(raw=raw):
                self.assert_protocol(raw, 'websocket_control_invalid')

    def test_nonminimal_and_high_bit_lengths_refused(self):
        for raw in (b'\x81\x7e\x00\x01', b'\x81\x7f' + struct.pack('!Q', 65535),
                    b'\x81\x7f' + struct.pack('!Q', 1 << 63)):
            with self.subTest(raw=raw):
                self.assert_protocol(raw, 'websocket_length_invalid')

    def test_unexpected_and_overlapping_fragments_refused(self):
        self.assert_protocol(frame(b'{}', opcode=0), 'websocket_fragment_unexpected')
        self.assert_protocol(frame(b'{', final=False) + frame(b'{}'),
                             'websocket_fragment_overlap')

    def test_declared_frame_limit_rejected_before_payload_read(self):
        raw = b'\x81\x7f' + struct.pack('!Q', UnixWebSocket.LIMIT + 1)
        self.assert_protocol(raw, 'websocket_message_limit')

    def test_cumulative_fragment_limit(self):
        fixture = Fixture(self, initial=frame(b'x' * 100, final=False)
                          + frame(b'x' * 50, opcode=0))
        client = self.connect(fixture)
        client.LIMIT = 128
        with self.assertRaisesRegex(NativeSocketError, '^websocket_message_limit$'):
            client.recv_json()
        fixture.finish()

    def test_invalid_utf8_json_duplicate_keys_and_nonfinite_values(self):
        for raw in (b'\xff', b'{', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":1,"x":2}', b'{"a":{"x":1,"x":2}}'):
            with self.subTest(raw=raw[:40]):
                self.assert_protocol(frame(raw), 'websocket_json_invalid')

    def test_parser_recursion_errors_are_sanitized(self):
        fixture = Fixture(self, initial=frame(b'{}'))
        client = self.connect(fixture)
        with mock.patch('assistant_mesh.native_ws.json.loads',
                        side_effect=RecursionError('secret_parser_details')):
            with self.assertRaisesRegex(NativeSocketError, '^websocket_json_invalid$'):
                client.recv_json()
        self.assertTrue(client._closed)
        fixture.finish()

    def test_json_must_be_object(self):
        for data in (b'[]', b'null', b'"text"', b'1', b'true'):
            with self.subTest(data=data):
                self.assert_protocol(frame(data), 'websocket_json_object_required')

    def test_invalid_send_is_sanitized_without_poisoning_connection(self):
        received = []
        fixture = Fixture(self, lambda peer: received.append(client_frame(peer)))
        client = self.connect(fixture)
        for value in ([], None, 'private_data'):
            with self.assertRaisesRegex(NativeSocketError, '^websocket_json_object_required$'):
                client.send_json(value)
        for value in ({'x': float('nan')}, {'x': object()}, {'x': '\ud800'}):
            with self.assertRaisesRegex(NativeSocketError, '^websocket_json_invalid$'):
                client.send_json(value)
        client.send_json({'id': 1})
        fixture.finish()
        self.assertEqual(received, [(129, b'{"id":1}')])

    def test_outbound_message_limit_before_write(self):
        received = []
        fixture = Fixture(self, lambda peer: received.append(client_frame(peer)))
        client = self.connect(fixture)
        client.LIMIT = 32
        with self.assertRaisesRegex(NativeSocketError, '^websocket_message_limit$'):
            client.send_json({'x': 'secret' * 10})
        client.send_json({'id': 1})
        fixture.finish()
        self.assertEqual(received, [(129, b'{"id":1}')])

    def test_close_idempotent_and_closed_operations_distinct(self):
        fixture = Fixture(self)
        client = self.connect(fixture)
        client.close()
        client.close()
        with self.assertRaisesRegex(EOFError, '^websocket_eof$'):
            client.recv_json()
        with self.assertRaisesRegex(NativeSocketError, '^native_socket_closed$'):
            client.send_json({'id': 1})
        fixture.finish()

    def test_upgrade_status_and_headers_are_strict(self):
        modifications = [
            (lambda raw: raw.replace(b'HTTP/1.1 101', b'HTTP/1.0 101'), 'websocket_upgrade_status'),
            (lambda raw: raw.replace(b'HTTP/1.1 101', b'HTTP/1.1 200'), 'websocket_upgrade_status'),
            (lambda raw: raw.replace(b'Upgrade: websocket', b'Upgrade: other'), 'websocket_upgrade_invalid'),
            (lambda raw: raw.replace(b'Connection: Upgrade', b'Connection: close'), 'websocket_upgrade_invalid'),
            (lambda raw: raw.replace(b'Sec-WebSocket-Accept: ', b'Sec-WebSocket-Accept: wrong'), 'websocket_upgrade_invalid'),
            (lambda raw: raw.replace(b'Upgrade: websocket', b'Upgrade : websocket'), 'websocket_header_invalid'),
            (lambda raw: raw.replace(b'Upgrade: websocket', b'Upgrade websocket'), 'websocket_header_invalid'),
            (lambda raw: raw.replace(b'Upgrade: websocket', b' Upgrade: websocket'), 'websocket_header_invalid'),
            (lambda raw: raw.replace(b'Upgrade: websocket', b'Upgrade: web\x00socket'), 'websocket_header_invalid'),
            (lambda raw: raw.replace(b'Upgrade: websocket', b'Upgrade: web\xffsocket'), 'websocket_header_invalid'),
            (lambda raw: raw.replace(b'Upgrade: websocket', b'Upgrade: websocket\r\nuPGRADE: websocket'), 'websocket_duplicate_header'),
            (lambda raw: raw[:-2] + b'Sec-WebSocket-Extensions: \r\n\r\n', 'websocket_upgrade_invalid'),
            (lambda raw: raw[:-2] + b'Sec-WebSocket-Protocol: json\r\n\r\n', 'websocket_upgrade_invalid'),
            (lambda raw: raw[:-2] + b'Sec-WebSocket-Version: 12\r\n\r\n', 'websocket_upgrade_invalid'),
        ]
        for transform, error in modifications:
            with self.subTest(error=error):
                fixture = Fixture(self, response=lambda key, transform=transform: transform(upgrade(key)))
                with self.assertRaisesRegex(NativeSocketError, '^' + error + '$'):
                    UnixWebSocket(fixture.path)
                fixture.finish()

    def test_upgrade_case_and_connection_tokens(self):
        fixture = Fixture(self, response=lambda key: upgrade(key).replace(
            b'Upgrade: websocket', b'uPGRADE:\tWebSocket').replace(
                b'Connection: Upgrade', b'Connection: keep-alive, uPGRADE'), initial=frame(b'{}'))
        client = self.connect(fixture)
        self.assertEqual(client.recv_json(), {})
        fixture.finish()

    def test_private_parent_under_sticky_writable_ancestor(self):
        outer = tempfile.TemporaryDirectory()
        self.addCleanup(outer.cleanup)
        os.chmod(outer.name, 0o777 | stat.S_ISVTX)
        fixture = Fixture(self, initial=frame(b'{}'), directory=outer.name)
        client = self.connect(fixture)
        self.assertEqual(client.recv_json(), {})
        fixture.finish()

    def test_double_root_slash_is_normalized_without_parent_loop(self):
        fixture = Fixture(self, initial=frame(b'{}'))
        client = UnixWebSocket('/' + fixture.path)
        self.addCleanup(client.close)
        self.assertEqual(client.recv_json(), {})
        fixture.finish()

    def test_handshake_header_limit_with_or_without_terminator(self):
        for terminate in (False, True):
            fixture = Fixture(self, response=lambda key, terminate=terminate:
                              b'HTTP/1.1 101 Switching Protocols\r\nX: '
                              + b'x' * UnixWebSocket.HEADER_LIMIT
                              + (b'\r\n\r\n' if terminate else b''))
            with self.assertRaisesRegex(NativeSocketError, '^websocket_header_limit$'):
                UnixWebSocket(fixture.path)
            fixture.finish()

    def test_partial_handshake_eof_distinct(self):
        fixture = Fixture(self, response=lambda key: b'HTTP/1.1 101\r\n')
        with self.assertRaisesRegex(EOFError, '^websocket_eof$'):
            UnixWebSocket(fixture.path)
        fixture.finish()

    def test_select_errors_are_sanitized_and_close(self):
        fixture = Fixture(self)
        client = self.connect(fixture)
        with mock.patch('assistant_mesh.native_ws.select.select',
                        side_effect=OSError('secret_socket_details')):
            with self.assertRaisesRegex(NativeSocketError, '^native_socket_io_failed$'):
                client.recv_json()
        self.assertTrue(client._closed)
        fixture.finish()

    def test_expired_io_deadline_remains_distinct_timeout(self):
        fixture = Fixture(self)
        client = self.connect(fixture)
        with self.assertRaisesRegex(socket.timeout, '^websocket_timeout$'):
            client._ready(-1)
        self.assertFalse(client._closed)
        fixture.finish()

    def test_random_source_write_failure_is_sanitized(self):
        fixture = Fixture(self)
        client = self.connect(fixture)
        with mock.patch('assistant_mesh.native_ws.os.urandom',
                        side_effect=OSError('private_entropy_details')):
            with self.assertRaisesRegex(NativeSocketError, '^native_socket_io_failed$'):
                client.send_json({'id': 1})
        self.assertTrue(client._closed)
        fixture.finish()


@unittest.skipUnless(hasattr(socket, 'AF_UNIX') and hasattr(os, 'geteuid'),
                     'optional owner-private Unix transport is unavailable')
class PrivateNativeSocket(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = self.temporary.name + '/socket'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        os.chmod(self.path, 0o600)
        self.addCleanup(self.listener.close)

    def test_socket_permissions_refuse_other_users(self):
        os.chmod(self.path, 0o660)
        with self.assertRaisesRegex(NativeSocketError, '^native_socket_path_not_private$'):
            UnixWebSocket(self.path)

    def test_final_parent_must_be_private_0700(self):
        for mode in (0o755, 0o770, 0o777 | stat.S_ISVTX):
            os.chmod(self.temporary.name, mode)
            with self.assertRaisesRegex(NativeSocketError, '^native_socket_path_not_private$'):
                UnixWebSocket(self.path)

    def test_missing_regular_or_symlink_socket_refused(self):
        with self.assertRaisesRegex(NativeSocketError, '^native_socket_path_unavailable$'):
            UnixWebSocket(self.temporary.name + '/missing')
        regular = self.temporary.name + '/regular'
        with open(regular, 'wb'):
            pass
        with self.assertRaisesRegex(NativeSocketError, '^native_socket_path_not_private$'):
            UnixWebSocket(regular)
        link = self.temporary.name + '/link'
        os.symlink(self.path, link)
        with self.assertRaisesRegex(NativeSocketError, '^native_socket_path_not_private$'):
            UnixWebSocket(link)

    def test_symlink_parent_refused(self):
        link = self.temporary.name + '/linked'
        os.symlink(self.temporary.name, link)
        with self.assertRaisesRegex(NativeSocketError, '^native_socket_path_not_private$'):
            UnixWebSocket(link + '/socket')

    def test_writable_nonsticky_ancestor_refused(self):
        nested = self.temporary.name + '/private'
        os.mkdir(nested, 0o700)
        path = nested + '/socket'
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(peer.close)
        peer.bind(path)
        os.chmod(path, 0o600)
        os.chmod(self.temporary.name, 0o777)
        with self.assertRaisesRegex(NativeSocketError, '^native_socket_path_not_private$'):
            UnixWebSocket(path)

    def test_nonowned_socket_or_parent_refused(self):
        original = os.lstat
        for target in (self.path, self.temporary.name):
            def foreign(path, target=target):
                result = original(path)
                if path == target:
                    values = list(result)
                    values[4] = os.geteuid() + 10000
                    return os.stat_result(values)
                return result
            with mock.patch('assistant_mesh.native_ws.os.lstat', side_effect=foreign):
                with self.assertRaisesRegex(NativeSocketError, '^native_socket_path_not_private$'):
                    UnixWebSocket(self.path)

    def test_connect_failure_is_sanitized(self):
        # A real, owner-private socket exists but has not started listening.
        with self.assertRaisesRegex(NativeSocketError, '^native_socket_connect_failed$'):
            UnixWebSocket(self.path)

    def test_invalid_path_shapes_and_unicode_are_sanitized(self):
        for path in ('relative', '/a/../b', '/a/./b', '/a\x00b', '/a/',
                     b'/bytes', None, '/\ud800'):
            with self.subTest(path=repr(path)):
                with self.assertRaisesRegex(NativeSocketError, '^native_socket_path_invalid$'):
                    UnixWebSocket(path)

    def test_invalid_timeouts_are_sanitized(self):
        for timeout in (True, None, '10', 0, -1, float('nan'), float('inf'), 10 ** 10000):
            with self.subTest(timeout=type(timeout).__name__):
                with self.assertRaisesRegex(NativeSocketError, '^native_socket_timeout_invalid$'):
                    UnixWebSocket(self.path, timeout=timeout)

    def test_unsupported_platform_is_explicit(self):
        with mock.patch.object(socket, 'AF_UNIX'):
            delattr(socket, 'AF_UNIX')
            with self.assertRaisesRegex(NativeSocketError, '^native_socket_unsupported$'):
                UnixWebSocket(self.path)

    def test_socket_replacement_after_connect_refused(self):
        fixture = Fixture(self)
        with mock.patch('assistant_mesh.native_ws._private_socket',
                        side_effect=[(fixture.path, (1, 1)), (fixture.path, (1, 2))]):
            with self.assertRaisesRegex(NativeSocketError, '^native_socket_path_changed$'):
                UnixWebSocket(fixture.path)
        fixture.finish()


if __name__ == '__main__':
    unittest.main()
