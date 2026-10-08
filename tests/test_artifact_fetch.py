"""Real loopback HTTP range fixtures; no public download, model, or credentials."""
import contextlib
import hashlib
import io
import json
import os
import re
import socketserver
import tempfile
import threading
import time
import unittest
import urllib.request
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.fetch_artifact import ArtifactFetchError, _SafeRedirect, fetch_artifact, main


class FixtureServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class ArtifactFetchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / 'artifact.tar.gz'
        self.body = bytes(range(256)) * 1025 + b'not-a-fixed-version'
        self.digest = hashlib.sha256(self.body).hexdigest()
        self.requests, self.active, self.peak = [], 0, 0
        self.lock, self.all_started = threading.Lock(), threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *args):
                pass

            def do_GET(self):
                with owner.lock:
                    owner.requests.append((self.path, dict(self.headers)))
                    owner.active += 1
                    owner.peak = max(owner.peak, owner.active)
                    if owner.active >= 4:
                        owner.all_started.set()
                try:
                    if self.path == '/parallel':
                        owner.all_started.wait(2)
                    if self.path == '/redirect':
                        self.send_response(302)
                        self.send_header('Location', '/valid')
                        self.end_headers()
                        return
                    if self.path == '/unsafe-redirect':
                        self.send_response(302)
                        self.send_header('Location', 'http://example.invalid/private?token=DO_NOT_PRINT')
                        self.end_headers()
                        return
                    if self.path == '/unavailable':
                        self.send_error(503)
                        return
                    start, end = map(int, re.fullmatch(r'bytes=([0-9]+)-([0-9]+)', self.headers['Range']).groups())
                    part = owner.body[start:end + 1]
                    self.send_response(200 if self.path == '/ignore-range' else 206)
                    if self.path != '/missing-range':
                        self.send_header('Content-Range', 'bytes {}-{}/{}'.format(
                            start + (1 if self.path == '/wrong-start' else 0), end,
                            len(owner.body) + (1 if self.path == '/wrong-total' else 0)))
                    if self.path == '/duplicate-range':
                        self.send_header('Content-Range', 'bytes {}-{}/{}'.format(start, end, len(owner.body)))
                    if self.path == '/encoded':
                        self.send_header('Content-Encoding', 'gzip')
                    if self.path != '/extra':
                        self.send_header('Content-Length', str(len(part) + (1 if self.path == '/wrong-length' else 0)))
                    if self.path == '/duplicate-length':
                        self.send_header('Content-Length', str(len(part)))
                    self.end_headers()
                    if self.path == '/short':
                        part = part[:-1]
                    if self.path == '/extra':
                        part += b'x'
                    if self.path == '/drip':
                        for byte in part[:100]:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.01)
                    else:
                        self.wfile.write(part)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with owner.lock:
                        owner.active -= 1

        self.server = FixtureServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temporary.cleanup()

    def fetch(self, mode='/valid', **changes):
        options = {'workers': 4, 'timeout': 2, 'deadline': 5, 'allow_loopback_http': True}
        options.update(changes)
        return fetch_artifact(self.base + mode, len(self.body), self.digest, self.output, **options)

    def assert_clean(self):
        self.assertFalse(self.output.exists())
        self.assertEqual([], list(self.root.iterdir()))

    def test_real_parallel_exact_ranges_hash_permissions_and_no_credentials(self):
        result = self.fetch('/parallel')
        self.assertTrue(result['sha256_verified'])
        self.assertFalse(result['already_present'])
        self.assertGreaterEqual(self.peak, 2)
        self.assertEqual(self.body, self.output.read_bytes())
        self.assertEqual(0o600, self.output.stat().st_mode & 0o777)
        self.assertEqual([self.output], list(self.root.iterdir()))
        ranges = []
        for _, headers in self.requests:
            ranges.append(tuple(map(int, re.fullmatch(r'bytes=([0-9]+)-([0-9]+)', headers['Range']).groups())))
            self.assertEqual('identity', headers['Accept-Encoding'])
            self.assertFalse(set(name.lower() for name in headers) & {'cookie', 'authorization', 'proxy-authorization'})
        ranges.sort()
        self.assertEqual(4, len(ranges))
        self.assertEqual(0, ranges[0][0])
        self.assertEqual(len(self.body) - 1, ranges[-1][1])
        self.assertTrue(all(left[1] + 1 == right[0] for left, right in zip(ranges, ranges[1:])))

    def test_redirect_keeps_range_and_safe_chain(self):
        self.fetch('/redirect')
        self.assertEqual(self.body, self.output.read_bytes())
        self.assertEqual(8, len(self.requests))
        self.assertTrue(all('Range' in headers for _, headers in self.requests))

    def test_incorrect_status_range_encoding_or_lengths_never_publish(self):
        for endpoint in ('/ignore-range', '/wrong-start', '/wrong-total', '/missing-range',
                         '/duplicate-range', '/encoded', '/wrong-length', '/duplicate-length',
                         '/short', '/extra', '/unavailable', '/unsafe-redirect'):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ArtifactFetchError) as error:
                    self.fetch(endpoint)
                self.assertNotIn('DO_NOT_PRINT', str(error.exception))
                self.assertNotIn('http://', str(error.exception))
                self.assertNotIn('https://', str(error.exception))
                self.assert_clean()

    def test_wrong_whole_hash_is_not_published(self):
        with self.assertRaisesRegex(ArtifactFetchError, 'sha256_mismatch'):
            fetch_artifact(self.base + '/valid', len(self.body), '0' * 64, self.output,
                           allow_loopback_http=True)
        self.assert_clean()

    def test_existing_complete_artifact_is_idempotent_without_network(self):
        self.output.write_bytes(self.body)
        self.output.chmod(0o600)
        result = self.fetch()
        self.assertTrue(result['already_present'])
        self.assertEqual([], self.requests)
        self.assertEqual(self.body, self.output.read_bytes())

    def test_existing_incorrect_output_is_never_overwritten(self):
        self.output.write_bytes(b'user-owned-existing')
        self.output.chmod(0o600)
        with self.assertRaisesRegex(ArtifactFetchError, 'existing_output_mismatch'):
            self.fetch()
        self.assertEqual(b'user-owned-existing', self.output.read_bytes())
        self.assertEqual([], self.requests)

    def test_public_existing_file_is_not_silently_chmodded(self):
        self.output.write_bytes(self.body)
        self.output.chmod(0o644)
        with self.assertRaisesRegex(ArtifactFetchError, 'existing_output_mismatch'):
            self.fetch()
        self.assertEqual(0o644, self.output.stat().st_mode & 0o777)

    def test_output_symlink_parent_symlink_and_shared_parent_are_rejected(self):
        target = self.root / 'target'
        target.write_bytes(b'user-file')
        self.output.symlink_to(target)
        with self.assertRaisesRegex(ArtifactFetchError, 'symlink_output'):
            self.fetch()
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(b'user-file', target.read_bytes())
        linked_parent = self.root / 'parent-link'
        linked_parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ArtifactFetchError, 'symlink_output'):
            fetch_artifact(self.base + '/valid', len(self.body), self.digest, linked_parent / 'other',
                           allow_loopback_http=True)
        shared = self.root / 'shared'
        shared.mkdir(mode=0o777)
        shared.chmod(0o777)
        with self.assertRaisesRegex(ArtifactFetchError, 'parent_must_be_owned'):
            fetch_artifact(self.base + '/valid', len(self.body), self.digest, shared / 'other',
                           allow_loopback_http=True)
        self.assertEqual([], self.requests)

    def test_publish_race_never_overwrites_and_complete_identical_race_is_idempotent(self):
        original_link = os.link
        for raced_body in (b'another-writer', self.body):
            with self.subTest(correct=raced_body == self.body):
                def raced_link(source, name, **kwargs):
                    self.output.write_bytes(raced_body)
                    self.output.chmod(0o600)
                    return original_link(source, name, **kwargs)
                with patch('scripts.fetch_artifact.os.link', side_effect=raced_link):
                    if raced_body == self.body:
                        self.assertTrue(self.fetch()['already_present'])
                    else:
                        with self.assertRaisesRegex(ArtifactFetchError, 'existing_output_mismatch'):
                            self.fetch()
                self.assertEqual(raced_body, self.output.read_bytes())
                self.assertEqual([self.output], list(self.root.iterdir()))
                self.output.unlink()

    def test_small_artifact_reduces_worker_count(self):
        self.body = b'x'
        self.digest = hashlib.sha256(self.body).hexdigest()
        result = self.fetch()
        self.assertEqual(1, result['workers'])
        self.assertEqual('bytes=0-0', self.requests[0][1]['Range'])

    def test_overall_deadline_rejects_slow_trickle_without_partial_publication(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ArtifactFetchError, 'deadline'):
            self.fetch('/drip', workers=1, timeout=0.2, deadline=0.08)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assert_clean()

    def test_explicit_identity_options_and_public_https_initial_url_required(self):
        urls = ['http://example.invalid/a', 'http://localhost/a', 'https://user:secret@example.invalid/a',
                'https://example.invalid/a?token=DO_NOT_PRINT', 'https://example.invalid/a#fragment',
                'https://example.invalid/a\nAuthorization: secret']
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(ArtifactFetchError) as error:
                    fetch_artifact(url, len(self.body), self.digest, self.output)
                self.assertNotIn('DO_NOT_PRINT', str(error.exception))
        for changes in ({'size': 0}, {'size': True}, {'sha256': 'not-a-hash'}, {'workers': 0},
                        {'workers': 33}, {'workers': True}, {'timeout': 0}, {'deadline': float('nan')},
                        {'output': Path('relative')}, {'output': self.root / 'missing' / 'file'}):
            with self.subTest(changes=changes):
                options = dict(url=self.base + '/valid', size=len(self.body), sha256=self.digest,
                               output=self.output, allow_loopback_http=True)
                options.update(changes)
                with self.assertRaises(ArtifactFetchError):
                    fetch_artifact(**options)
        self.assertEqual([], self.requests)
        self.assert_clean()

    def test_https_downgrade_userinfo_and_nonloopback_http_redirect_are_rejected(self):
        request = urllib.request.Request('https://github.com/owner/repo/artifact')
        handler = _SafeRedirect(time.monotonic() + 5, allow_loopback_http=True)
        for target in ('http://127.0.0.1/artifact', 'https://user:secret@example.invalid/a',
                       'http://example.invalid/a'):
            with self.subTest(target=target):
                with self.assertRaises(ArtifactFetchError):
                    handler.redirect_request(request, None, 302, '', {}, target)
        redirected = handler.redirect_request(request, None, 302, '', {},
                                               'https://release-assets.githubusercontent.com/a?sig=private')
        self.assertEqual('https', urllib.parse.urlsplit(redirected.full_url).scheme)

    def test_redirect_body_is_closed_without_unbounded_read(self):
        request = urllib.request.Request('https://github.com/owner/repo/artifact')
        request.timeout = 2
        headers = HTTPMessage()
        headers.add_header('Location', 'https://release-assets.githubusercontent.com/a?sig=DO_NOT_PRINT')
        handler = _SafeRedirect(time.monotonic() + 5)
        closed, opened = [], []

        def forbidden_read(*args):
            self.fail('redirect body must not be read into memory')

        handler.parent = SimpleNamespace(open=lambda request, **kwargs: opened.append(request) or 'response')
        response = SimpleNamespace(close=lambda: closed.append(True), read=forbidden_read)
        self.assertEqual('response', handler.http_error_302(request, response, 302, '', headers))
        self.assertEqual([True], closed)
        self.assertEqual(1, len(opened))

    def test_cli_sanitizes_errors_and_has_no_token_or_header_options(self):
        output = io.StringIO()
        argv = ['fetch_artifact', '--url', 'https://example.invalid/a?token=DO_NOT_PRINT',
                '--size', str(len(self.body)), '--sha256', self.digest, '--output', str(self.output)]
        with patch('sys.argv', argv), contextlib.redirect_stdout(output):
            self.assertEqual(1, main())
        self.assertNotIn('DO_NOT_PRINT', output.getvalue())
        self.assertEqual({'ok': False, 'error': 'artifact_initial_query_not_supported'}, json.loads(output.getvalue()))
        for extra in (['--token', 'DO_NOT_PRINT'], ['--header', 'Authorization: DO_NOT_PRINT']):
            output = io.StringIO()
            with patch('sys.argv', argv + extra), contextlib.redirect_stdout(output):
                self.assertEqual(1, main())
            self.assertNotIn('DO_NOT_PRINT', output.getvalue())
            self.assertEqual('artifact_unsupported_arguments', json.loads(output.getvalue())['error'])
        self.assertEqual([], self.requests)


if __name__ == '__main__':
    unittest.main()
