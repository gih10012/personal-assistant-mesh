"""Actual private Unix-socket HTTP; never contact external hosts or APIs."""
import hashlib
import json
import os
import socketserver
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.worker import Client


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class UnixClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.directory.chmod(0o700)
        self.token = self.directory / 'test-control.token'
        self.token.write_text(hashlib.sha256(b'unix-client-test-only').hexdigest())
        self.token.chmod(0o600)
        self.socket = self.directory / 'control.sock'
        self.seen, self.tcp_seen = [], []
        test = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.respond()
            def do_POST(self):
                self.respond()
            def respond(self):
                records = test.seen if isinstance(self.server, UnixHTTPServer) else test.tcp_seen
                size = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(size) if size else None
                records.append({'method': self.command, 'path': self.path,
                                'authorization': self.headers.get('Authorization'),
                                'body': json.loads(raw.decode()) if raw is not None else None})
                status = int(self.path[1:]) if self.path in ('/400', '/403', '/409') else 200
                data = json.dumps({'transport': 'unix', 'path': self.path, 'ok': status == 200}).encode()
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', test.tcp_url + '/credential-leak')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if self.path == '/advertised-large':
                    self.send_response(200)
                    self.send_header('Content-Length', '9000000')
                    self.end_headers()
                    return
                if self.path == '/streamed-large':
                    self.send_response(200)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(b'x' * 64)
                    self.close_connection = True
                    return
                if self.path == '/invalid-json':
                    data = b'not-json'
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.unix = UnixHTTPServer(str(self.socket), Handler)
        self.socket.chmod(0o600)
        self.tcp = HTTPServer(('127.0.0.1', 0), Handler)
        self.tcp_url = 'http://127.0.0.1:' + str(self.tcp.server_address[1])
        self.threads = []
        for server in (self.unix, self.tcp):
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.config = {'control_url': self.tcp_url, 'token_file': str(self.token), 'unix_socket': str(self.socket)}

    def tearDown(self):
        for server, thread in zip((self.unix, self.tcp), self.threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.directory.chmod(0o700)
        self.tmp.cleanup()

    def test_actual_http_get_and_post_over_unix_with_header_auth(self):
        client = Client(self.config)
        self.assertEqual('unix', client.request('/ok?read=1')['transport'])
        client.request('/write', {'node': 'test-only', 'input': '你好'})
        self.assertEqual(['GET', 'POST'], [record['method'] for record in self.seen])
        self.assertEqual('/ok?read=1', self.seen[0]['path'])
        self.assertEqual({'node': 'test-only', 'input': '你好'}, self.seen[1]['body'])
        self.assertTrue(all(record['authorization'].startswith('Bearer ') for record in self.seen))
        self.assertEqual([], self.tcp_seen)

    def test_url_base_path_and_query_keep_same_client_contract(self):
        client = Client(dict(self.config, control_url=self.tcp_url + '/private-prefix/'))
        client.request('/v1/mesh/task?id=test-message')
        self.assertEqual('/private-prefix/v1/mesh/task?id=test-message', self.seen[0]['path'])

    def test_http_errors_keep_status_semantics_and_close_response(self):
        for status in (400, 403, 409):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                Client(self.config).request('/' + str(status), {})
            self.assertEqual(status, failure.exception.code)
            self.assertTrue(failure.exception.closed)
        self.assertEqual([], self.tcp_seen)

    def test_redirect_never_sends_credential_to_tcp_destination(self):
        with self.assertRaisesRegex(ValueError, 'control_redirect_blocked'):
            Client(self.config).request('/redirect')
        self.assertEqual([], self.tcp_seen)
        self.assertEqual(1, len(self.seen))

    def test_proxy_and_tcp_connection_functions_never_used(self):
        with patch('assistant_mesh.worker.urllib.request.build_opener', side_effect=AssertionError('proxy used')):
            with patch('assistant_mesh.worker.socket.create_connection', side_effect=AssertionError('TCP used')):
                self.assertTrue(Client(self.config).request('/ok')['ok'])
        self.assertEqual([], self.tcp_seen)

    def test_missing_socket_allowed_in_config_but_no_tcp_fallback(self):
        client = Client(dict(self.config, unix_socket=str(self.directory / 'not-connected-yet.sock')))
        with self.assertRaises(OSError):
            client.request('/ok')
        self.assertEqual([], self.tcp_seen)
        self.assertEqual([], self.seen)

    def test_regular_file_is_not_a_socket_and_never_falls_back(self):
        path = self.directory / 'not-socket'
        path.write_text('fixture')
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, '0600_socket'):
            Client(dict(self.config, unix_socket=str(path))).request('/ok')
        self.assertEqual([], self.tcp_seen)

    def test_socket_permissions_checked_on_each_request(self):
        client = Client(self.config)
        self.assertTrue(client.request('/ok')['ok'])
        self.socket.chmod(0o666)
        with self.assertRaisesRegex(ValueError, '0600_socket'):
            client.request('/ok')
        self.socket.chmod(0o600)
        self.assertTrue(client.request('/ok')['ok'])
        self.assertEqual(2, len(self.seen))

    def test_parent_must_be_private_owned_directory(self):
        client = Client(self.config)
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, '0700_parent'):
            client.request('/ok')
        self.directory.chmod(0o700)
        actual_uid = os.getuid()
        with patch('assistant_mesh.worker.os.getuid', return_value=actual_uid + 1):
            with self.assertRaisesRegex(ValueError, '0700_parent'):
                client.request('/ok')
        self.assertEqual([], self.seen)

    def test_socket_owner_checked_not_just_parent(self):
        original = Path.lstat
        selected = self.socket
        def different_owner(path):
            value = original(path)
            if path == selected:
                fields = list(value)
                fields[4] += 1
                return os.stat_result(fields)
            return value
        client = Client(self.config)
        with patch('assistant_mesh.worker.Path.lstat', different_owner):
            with self.assertRaisesRegex(ValueError, '0600_socket'):
                client.request('/ok')
        self.assertEqual([], self.seen)

    def test_socket_and_parent_symlinks_rejected(self):
        alias = self.directory / 'socket-link'
        alias.symlink_to(self.socket)
        with self.assertRaisesRegex(ValueError, '0600_socket'):
            Client(dict(self.config, unix_socket=str(alias))).request('/ok')
        parent = self.directory / 'parent-link'
        parent.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'non_symlink_parent'):
            Client(dict(self.config, unix_socket=str(parent / 'control.sock'))).request('/ok')
        self.assertEqual([], self.seen)

    def test_absolute_path_and_loopback_http_url_required(self):
        for path in ('relative.sock', '', '/private/../elsewhere.sock', '/private/null\0.sock'):
            with self.assertRaisesRegex(ValueError, 'requires_absolute_path'):
                Client(dict(self.config, unix_socket=path))
        for url in ('https://127.0.0.1', 'https://example.invalid'):
            with self.assertRaisesRegex(ValueError, 'requires_http_loopback_url'):
                Client(dict(self.config, control_url=url))

    def test_response_length_is_bounded_before_and_after_read(self):
        with self.assertRaisesRegex(ValueError, 'control_response_too_large'):
            Client(self.config).request('/advertised-large')
        with patch('assistant_mesh.worker.MAX_CONTROL_RESPONSE_BYTES', 16):
            with self.assertRaisesRegex(ValueError, 'control_response_too_large'):
                Client(self.config).request('/streamed-large')

    def test_invalid_json_is_not_a_success_or_fallback(self):
        with self.assertRaises(ValueError):
            Client(self.config).request('/invalid-json')
        self.assertEqual([], self.tcp_seen)

    def test_socket_without_listener_fails_without_fallback(self):
        self.unix.shutdown()
        self.threads[0].join(timeout=3)
        self.unix.server_close()
        with self.assertRaises(OSError):
            Client(self.config).request('/ok')
        self.assertEqual([], self.tcp_seen)

    def test_tcp_transport_still_works_without_socket_setting(self):
        config = dict(self.config)
        config.pop('unix_socket')
        self.assertTrue(Client(config).request('/ok')['ok'])
        self.assertEqual(1, len(self.tcp_seen))
        self.assertEqual([], self.seen)


if __name__ == '__main__':
    unittest.main()
