"""Real loopback HTTP range fixtures; no public download, model, or credentials."""
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import socket
import socketserver
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.parse
import urllib.error
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.fetch_artifact import ArtifactFetchError, _ExplicitProxy, _ResumeParts, _SafeRedirect, fetch_artifact, main


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

    @contextlib.contextmanager
    def forward_proxy(self):
        captured, owner = [], self

        class Forward(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                captured.append((self.path, dict(self.headers)))
                target = urllib.parse.urlsplit(self.path)
                if (target.scheme != 'http' or target.hostname != '127.0.0.1'
                        or target.port != owner.server.server_port):
                    self.send_error(400)
                    return
                connection = http.client.HTTPConnection(target.hostname, target.port, timeout=3)
                try:
                    connection.request('GET', target.path, headers={
                        'Range': self.headers['Range'], 'Accept-Encoding': self.headers['Accept-Encoding']})
                    reply = connection.getresponse()
                    body = reply.read()
                    self.send_response(reply.status)
                    for key in ('Content-Range', 'Content-Encoding', 'Location'):
                        if reply.getheader(key) is not None:
                            self.send_header(key, reply.getheader(key))
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    connection.close()

        server = FixtureServer(('127.0.0.1', 0), Forward)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            yield 'http://127.0.0.1:' + str(server.server_port), captured
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_explicit_proxy_real_range_transport_ignores_environment_and_bypass(self):
        with self.forward_proxy() as (endpoint, captured):
            with patch.dict(os.environ, {'http_proxy': 'http://user:DO_NOT_PRINT@invalid:1',
                    'HTTP_PROXY': 'http://invalid:2', 'NO_PROXY': '*', 'no_proxy': '*'}):
                result = self.fetch(proxy_url=endpoint)
        self.assertTrue(result['sha256_verified'])
        self.assertEqual(4, len(captured))
        self.assertTrue(all(url == self.base + '/valid' for url, headers in captured))
        self.assertTrue(all(not set(key.lower() for key in headers) & {
            'cookie', 'authorization', 'proxy-authorization'} for url, headers in captured))
        self.assertEqual(self.body, self.output.read_bytes())

    def test_explicit_proxy_preserves_partial_resume_and_redirect_contract(self):
        lengths = [end - start + 1 for start, end in self.segments()]
        directory = self.checkpoint([lengths[0], 11, 12, 13])
        before = self.checkpoint_snapshot(directory)
        with self.forward_proxy() as (endpoint, captured):
            result = self.fetch('/redirect', proxy_url=endpoint, resume_parts_directory=directory)
        self.assertTrue(result['sha256_verified'])
        self.assertEqual(3, result['network_parts'])
        self.assertEqual(6, len(captured))
        self.assertEqual(before, self.checkpoint_snapshot(directory))

    def test_default_transport_never_inherits_proxy_environment(self):
        with self.forward_proxy() as (endpoint, captured):
            with patch.dict(os.environ, {'http_proxy': endpoint, 'HTTP_PROXY': endpoint,
                                        'NO_PROXY': '', 'no_proxy': ''}):
                self.assertTrue(self.fetch()['sha256_verified'])
        self.assertEqual([], captured)

    def test_explicit_proxy_rejects_credentials_query_fragment_socks_and_controls(self):
        for endpoint in ('http://user:DO_NOT_PRINT@127.0.0.1:7890',
                'https://user@127.0.0.1', 'http://127.0.0.1:7890?token=DO_NOT_PRINT',
                'http://127.0.0.1:7890#secret', 'socks5h://127.0.0.1:7890',
                'http://127.0.0.1:0', 'http://127.0.0.1:7890/path',
                'http://127.0.0.1:7890\nDO_NOT_PRINT'):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ArtifactFetchError) as failed:
                    self.fetch(proxy_url=endpoint)
                self.assertNotIn('DO_NOT_PRINT', str(failed.exception))
                self.assertNotIn('127.0.0.1', str(failed.exception))
        self.assertEqual([], self.requests)
        self.assert_clean()

    def test_explicit_proxy_https_mapping_and_connect_do_not_consult_bypass(self):
        handler = _ExplicitProxy({'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'})
        request = urllib.request.Request('https://example.invalid/artifact')
        with patch('urllib.request.proxy_bypass', side_effect=AssertionError('environment consulted')):
            self.assertIsNone(handler.proxy_open(request, 'http://127.0.0.1:7890', 'https'))
        self.assertEqual('127.0.0.1:7890', request.host)
        self.assertEqual('example.invalid', request._tunnel_host)
        self.assertFalse(any(key.lower() == 'proxy-authorization' for key in request.headers))

    def test_cli_proxy_is_explicit_redacted_and_not_an_implicit_configuration_change(self):
        argv = ['fetch_artifact', '--url', 'https://example.invalid/a', '--size', str(len(self.body)),
            '--sha256', self.digest, '--output', str(self.output), '--proxy-url', 'http://127.0.0.1:7890']
        output = io.StringIO()
        with patch('sys.argv', argv), contextlib.redirect_stdout(output):
            with patch('scripts.fetch_artifact.fetch_artifact', return_value={'sha256': self.digest}) as fetch:
                self.assertEqual(0, main())
        self.assertEqual('http://127.0.0.1:7890', fetch.call_args[1]['proxy_url'])
        self.assertNotIn('127.0.0.1', output.getvalue())
        self.assertNotIn('proxy_url', output.getvalue())

    def segments(self, workers=4):
        quotient, remainder = divmod(len(self.body), min(workers, len(self.body)))
        start, segments = 0, []
        for index in range(min(workers, len(self.body))):
            length = quotient + (1 if index < remainder else 0)
            segments.append((start, start + length - 1))
            start += length
        return segments

    def checkpoint(self, prefixes=None, workers=4):
        directory = self.root / 'checkpoint'
        directory.mkdir(mode=0o700)
        lengths = [end - start + 1 for start, end in self.segments(workers)]
        prefixes = lengths if prefixes is None else prefixes
        for index, ((start, end), prefix) in enumerate(zip(self.segments(workers), prefixes)):
            part = directory / 'part-{:03d}'.format(index)
            part.write_bytes(self.body[start:start + prefix])
            part.chmod(0o600)
        return directory

    def checkpoint_snapshot(self, directory):
        return {part.name: (part.read_bytes(), part.stat().st_mode, part.stat().st_mtime_ns)
                for part in directory.iterdir()}

    def assert_checkpoint_preserved(self, directory, before):
        self.assertEqual(before, self.checkpoint_snapshot(directory))
        self.assertFalse(self.output.exists())
        self.assertEqual([directory], list(self.root.iterdir()))

    def test_resume_partial_and_completed_parts_only_fetches_missing_exact_ranges(self):
        lengths = [end - start + 1 for start, end in self.segments()]
        prefixes = [lengths[0], 17, 0, lengths[3]]
        directory = self.checkpoint(prefixes)
        before = self.checkpoint_snapshot(directory)
        result = self.fetch(resume_parts_directory=directory)
        self.assertEqual(self.body, self.output.read_bytes())
        self.assertTrue(result['sha256_verified'])
        self.assertEqual(sum(prefixes), result['resumed_bytes'])
        self.assertEqual(2, result['network_parts'])
        ranges = sorted(headers['Range'] for _, headers in self.requests)
        segments = self.segments()
        self.assertEqual(sorted(['bytes={}-{}'.format(segments[1][0] + 17, segments[1][1]),
                                 'bytes={}-{}'.format(*segments[2])]), ranges)
        self.assertEqual(before, self.checkpoint_snapshot(directory))
        self.assertEqual(0o600, self.output.stat().st_mode & 0o777)
        self.assertEqual({directory, self.output}, set(self.root.iterdir()))

    def test_resume_all_completed_parts_does_not_use_network(self):
        directory = self.checkpoint()
        before = self.checkpoint_snapshot(directory)
        result = self.fetch(resume_parts_directory=directory)
        self.assertEqual([], self.requests)
        self.assertEqual(0, result['network_parts'])
        self.assertEqual(len(self.body), result['resumed_bytes'])
        self.assertEqual(self.body, self.output.read_bytes())
        self.assertEqual(before, self.checkpoint_snapshot(directory))

    def test_resume_wrong_prefix_still_requires_complete_official_sha(self):
        directory = self.checkpoint([31, 0, 0, 0])
        part = directory / 'part-000'
        part.write_bytes(b'x' + part.read_bytes()[1:])
        before = self.checkpoint_snapshot(directory)
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_sha256_mismatch'):
            self.fetch(resume_parts_directory=directory)
        self.assert_checkpoint_preserved(directory, before)

    def test_resume_requires_exact_all_part_names_and_matching_worker_count(self):
        directory = self.checkpoint([0] * 4)
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_part_set_mismatch'):
            self.fetch(workers=8, resume_parts_directory=directory)
        unknown = directory / 'metadata.json'
        unknown.write_text('not an authorized part name')
        unknown.chmod(0o600)
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_part_set_mismatch'):
            self.fetch(resume_parts_directory=directory)
        unknown.unlink()
        (directory / 'part-003').rename(directory / 'part-3')
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_part_set_mismatch'):
            self.fetch(resume_parts_directory=directory)
        self.assertEqual([], self.requests)
        self.assertFalse(self.output.exists())

    def test_resume_private_directory_and_part_permissions_are_not_chmodded(self):
        directory = self.checkpoint([0] * 4)
        directory.chmod(0o755)
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_private_owned_directory_required'):
            self.fetch(resume_parts_directory=directory)
        self.assertEqual(0o755, directory.stat().st_mode & 0o777)
        directory.chmod(0o700)
        part = directory / 'part-000'
        part.chmod(0o644)
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_private_owned_part_required'):
            self.fetch(resume_parts_directory=directory)
        self.assertEqual(0o644, part.stat().st_mode & 0o777)
        self.assertEqual([], self.requests)

    def test_resume_symlink_directory_part_and_hardlinks_are_rejected(self):
        directory = self.checkpoint([0] * 4)
        alias = self.root / 'checkpoint-link'
        alias.symlink_to(directory, target_is_directory=True)
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_symlink_not_supported'):
            self.fetch(resume_parts_directory=alias)
        alias.unlink()
        part = directory / 'part-000'
        part.unlink()
        part.symlink_to(directory / 'part-001')
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_private_owned_part_required'):
            self.fetch(resume_parts_directory=directory)
        part.unlink()
        os.link(str(directory / 'part-001'), str(part))
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_private_owned_part_required'):
            self.fetch(resume_parts_directory=directory)
        self.assertEqual([], self.requests)

    def test_resume_oversized_prefix_and_nonregular_part_rejected_before_network(self):
        directory = self.checkpoint()
        part = directory / 'part-000'
        part.write_bytes(part.read_bytes() + b'oversized')
        before = self.checkpoint_snapshot(directory)
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_prefix_too_large'):
            self.fetch(resume_parts_directory=directory)
        self.assert_checkpoint_preserved(directory, before)
        part.unlink()
        part.mkdir(mode=0o700)
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_private_owned_part_required'):
            self.fetch(resume_parts_directory=directory)
        self.assertEqual([], self.requests)

    def test_resume_rejects_metadata_owner_mismatch(self):
        directory = self.checkpoint([0] * 4)
        original_stat = os.stat
        def changed_owner(path, *args, **kwargs):
            metadata = original_stat(path, *args, **kwargs)
            if path == 'part-000' and kwargs.get('dir_fd') is not None:
                values = {key: getattr(metadata, key) for key in ('st_dev', 'st_ino', 'st_uid',
                    'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
                values['st_uid'] += 1
                return SimpleNamespace(**values)
            return metadata
        with patch('scripts.fetch_artifact.os.stat', side_effect=changed_owner):
            with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_private_owned_part_required'):
                self.fetch(resume_parts_directory=directory)
        self.assertEqual([], self.requests)

    def test_resume_checkpoint_changed_during_copy_is_rejected_before_network(self):
        directory = self.checkpoint([31] * 4)
        original_read = _ResumeParts._read
        def changed_read(snapshot, name, deadline, destination=None):
            digest = original_read(snapshot, name, deadline, destination)
            if destination is not None and name == 'part-000':
                changed = directory / 'part-003'
                before = changed.stat()
                changed.write_bytes(b'x' * 31)
                # This specifically tests the metadata early-rejection path.
                # Older hosts may coalesce rapid same-size writes into one
                # timestamp tick; the separate whole-hash test rejects bytes.
                os.utime(str(changed), ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
            return digest
        with patch.object(_ResumeParts, '_read', changed_read):
            with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_checkpoint_changed'):
                self.fetch(resume_parts_directory=directory)
        self.assertEqual([], self.requests)
        self.assertFalse(self.output.exists())
        self.assertEqual([directory], list(self.root.iterdir()))

    def test_resume_checkpoint_changed_after_copy_never_publishes(self):
        directory = self.checkpoint([31] * 4)
        from scripts.fetch_artifact import _part
        changed_once = threading.Event()
        def changed_part(*args, **kwargs):
            result = _part(*args, **kwargs)
            with self.lock:
                if not changed_once.is_set():
                    changed_once.set()
                    (directory / 'part-000').write_bytes(b'x' * 31)
            return result
        with patch('scripts.fetch_artifact._part', side_effect=changed_part):
            with self.assertRaisesRegex(ArtifactFetchError, 'artifact_resume_checkpoint_changed'):
                self.fetch(resume_parts_directory=directory)
        self.assertTrue(changed_once.is_set())
        self.assertFalse(self.output.exists())
        self.assertEqual([directory], list(self.root.iterdir()))

    def test_resume_wrong_range_or_deadline_preserves_checkpoint_and_old_output(self):
        directory = self.checkpoint([17] * 4)
        before = self.checkpoint_snapshot(directory)
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_part_range_mismatch'):
            self.fetch('/wrong-start', resume_parts_directory=directory)
        self.assert_checkpoint_preserved(directory, before)
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_fetch_deadline'):
            self.fetch('/drip', timeout=0.2, deadline=0.08, resume_parts_directory=directory)
        self.assert_checkpoint_preserved(directory, before)
        self.output.write_bytes(b'old-user-owned-output')
        self.output.chmod(0o600)
        self.requests.clear()
        with self.assertRaisesRegex(ArtifactFetchError, 'artifact_existing_output_mismatch'):
            self.fetch(resume_parts_directory=directory)
        self.assertEqual(b'old-user-owned-output', self.output.read_bytes())
        self.assertEqual([], self.requests)
        self.assertEqual(before, self.checkpoint_snapshot(directory))

    def controlled_transport_fault(self, directory, phase, error, fault_time, socket_timeout=2):
        """Advance only the fetch clock at a precise network failure boundary."""
        clock, open_timeouts, body_reads, closed = [100.0], [], [], []
        headers = HTTPMessage()
        headers.add_header('Content-Range', 'bytes 17-{}/{}'.format(len(self.body) - 1, len(self.body)))
        headers.add_header('Content-Length', str(len(self.body) - 17))
        owner = self

        class Response:
            def __enter__(response):
                return response

            def __exit__(response, *args):
                closed.append(True)

            def getcode(response):
                return 206

            def geturl(response):
                return owner.base + '/valid'

            def read1(response, size):
                body_reads.append(size)
                if len(body_reads) == 1:
                    return owner.body[17:18]
                clock[0] = fault_time
                raise error

        response = Response()
        response.headers = headers

        def open_response(request, timeout):
            open_timeouts.append(timeout)
            if phase == 'open':
                clock[0] = fault_time
                raise error
            return response

        fetch_clock = SimpleNamespace(monotonic=lambda: clock[0])
        opener = SimpleNamespace(open=open_response)
        with patch('scripts.fetch_artifact.time', fetch_clock):
            with patch('scripts.fetch_artifact.urllib.request.build_opener', return_value=opener):
                with self.assertRaises(ArtifactFetchError) as failed:
                    self.fetch(workers=1, timeout=socket_timeout, deadline=1,
                               resume_parts_directory=directory)
        self.assertEqual([min(socket_timeout, 1.0)], open_timeouts)
        self.assertEqual([] if phase == 'open' else [True], closed)
        self.assertEqual(0 if phase == 'open' else 2, len(body_reads))
        self.assertNotIn('DO_NOT_PRINT', str(failed.exception))
        return str(failed.exception)

    def test_deadline_clipped_open_timeout_preserves_resume_checkpoint(self):
        directory = self.checkpoint([17], workers=1)
        before = self.checkpoint_snapshot(directory)
        for error in (socket.timeout('DO_NOT_PRINT'),
                      urllib.error.URLError(socket.timeout('DO_NOT_PRINT'))):
            with self.subTest(wrapped=isinstance(error, urllib.error.URLError)):
                self.assertEqual('artifact_fetch_deadline',
                    self.controlled_transport_fault(directory, 'open', error, 101.0))
                self.assert_checkpoint_preserved(directory, before)
        self.assertEqual([], self.requests)

    def test_deadline_read_timeout_after_partial_staging_preserves_resume_checkpoint(self):
        directory = self.checkpoint([17], workers=1)
        before = self.checkpoint_snapshot(directory)
        for error in (socket.timeout('DO_NOT_PRINT'),
                      urllib.error.URLError(socket.timeout('DO_NOT_PRINT'))):
            with self.subTest(wrapped=isinstance(error, urllib.error.URLError)):
                self.assertEqual('artifact_fetch_deadline',
                    self.controlled_transport_fault(directory, 'read', error, 101.1))
                self.assert_checkpoint_preserved(directory, before)
        self.assertEqual([], self.requests)

    def test_socket_timeout_before_overall_deadline_is_still_transport_failure(self):
        directory = self.checkpoint([17], workers=1)
        before = self.checkpoint_snapshot(directory)
        for phase in ('open', 'read'):
            for error in (socket.timeout('DO_NOT_PRINT'),
                          urllib.error.URLError(socket.timeout('DO_NOT_PRINT'))):
                with self.subTest(phase=phase, wrapped=isinstance(error, urllib.error.URLError)):
                    self.assertEqual('artifact_transport_failed',
                        self.controlled_transport_fault(directory, phase, error, 100.25,
                                                        socket_timeout=0.25))
                    self.assert_checkpoint_preserved(directory, before)
        self.assertEqual([], self.requests)

    def test_unrelated_transport_failures_at_deadline_are_not_reclassified(self):
        directory = self.checkpoint([17], workers=1)
        before = self.checkpoint_snapshot(directory)
        for phase in ('open', 'read'):
            for error in (ConnectionResetError('DO_NOT_PRINT'), OSError('timed out DO_NOT_PRINT'),
                          urllib.error.URLError(ConnectionResetError('DO_NOT_PRINT')),
                          urllib.error.URLError('timed out DO_NOT_PRINT')):
                with self.subTest(phase=phase, kind=type(error).__name__):
                    self.assertEqual('artifact_transport_failed',
                        self.controlled_transport_fault(directory, phase, error, 101.1))
                    self.assert_checkpoint_preserved(directory, before)
        self.assertEqual([], self.requests)

    def test_http_failure_at_deadline_keeps_http_error_classification(self):
        directory = self.checkpoint([17], workers=1)
        before = self.checkpoint_snapshot(directory)
        error = urllib.error.HTTPError(self.base + '/DO_NOT_PRINT', 503, 'DO_NOT_PRINT', {}, None)
        self.assertEqual('artifact_http_failed',
            self.controlled_transport_fault(directory, 'open', error, 101.1))
        self.assert_checkpoint_preserved(directory, before)
        self.assertEqual([], self.requests)

    def test_cli_explicit_resume_handle_is_passed_without_new_identity_or_secrets(self):
        directory = self.checkpoint([0] * 4)
        argv = ['fetch_artifact', '--url', 'https://example.invalid/a', '--size', str(len(self.body)),
                '--sha256', self.digest, '--output', str(self.output), '--workers', '4',
                '--resume-parts-directory', str(directory)]
        output = io.StringIO()
        with patch('sys.argv', argv), contextlib.redirect_stdout(output):
            with patch('scripts.fetch_artifact.fetch_artifact', return_value={'sha256': self.digest}) as fetch:
                self.assertEqual(0, main())
        self.assertEqual(str(directory), fetch.call_args[1]['resume_parts_directory'])
        self.assertEqual(self.digest, json.loads(output.getvalue())['sha256'])
        self.assertNotIn('url', json.loads(output.getvalue()))
        self.assertEqual([], self.requests)

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

    def raced_existing_output(self, action):
        self.output.write_bytes(self.body)
        self.output.chmod(0o600)
        original_sha256 = hashlib.sha256
        class Digest:
            def __init__(digest):
                digest.real = original_sha256()
            def update(digest, body):
                digest.real.update(body)
            def hexdigest(digest):
                result = digest.real.hexdigest()
                action()
                return result
        with patch('scripts.fetch_artifact.hashlib.sha256', side_effect=Digest):
            with self.assertRaisesRegex(ArtifactFetchError, 'artifact_existing_output_changed'):
                self.fetch()
        self.assertEqual([], self.requests)

    def test_existing_output_changed_after_hash_is_not_trusted(self):
        def mutate():
            before = self.output.stat()
            self.output.write_bytes(b'x' * len(self.body))
            os.utime(str(self.output), ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
        self.raced_existing_output(mutate)
        self.assertEqual(b'x' * len(self.body), self.output.read_bytes())

    def test_existing_output_replaced_after_hash_is_not_trusted(self):
        def replace():
            replacement = self.root / 'replacement'
            replacement.write_bytes(self.body)
            replacement.chmod(0o600)
            os.replace(str(replacement), str(self.output))
        self.raced_existing_output(replace)
        self.assertEqual(self.body, self.output.read_bytes())

    def test_existing_output_disappearing_after_hash_is_not_trusted(self):
        self.raced_existing_output(self.output.unlink)
        self.assertFalse(self.output.exists())

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

    def test_publish_conflict_then_missing_output_never_reports_success(self):
        with patch('scripts.fetch_artifact.os.link', side_effect=FileExistsError('simulated disappearing winner')):
            with self.assertRaisesRegex(ArtifactFetchError, 'artifact_output_changed'):
                self.fetch()
        self.assert_clean()

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
