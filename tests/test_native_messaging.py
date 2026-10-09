"""Offline Native Messaging protocol/permission tests; no real browser or RPC."""
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from assistant_mesh import native_messaging as native


ROOT = Path(__file__).resolve().parents[1]
REQUEST = 'mesh-firefox-' + '1' * 32
TASK = 'ingress-' + '2' * 64


def packet(value):
    raw = json.dumps(value, ensure_ascii=False).encode('utf8')
    return struct.pack('=I', len(raw)) + raw


def status(action='status', request_id=REQUEST):
    value = {'format': 'owner-ingress/1', 'request_id': request_id,
             'task_id': TASK, 'accepted': True, 'status': 'completed',
             'status_recognized': True, 'result_available': True,
             'execution_verified': False, 'native_tools_intercepted': False}
    if action == 'submit':
        value['task_created'] = False
    return value


def approved():
    return {'format': 'owner-ingress-result/1', 'publication_id': 'owner-publication-1',
            'request_id': REQUEST, 'task_id': TASK, 'published': True,
            'result': {'summary': '已审查的有限摘要。', 'artifacts': [
                {'url': 'https://github.com/example-owner/project/blob/main/report.md',
                 'label': '公开引用', 'declared_sha256': 'a' * 64}]},
            'review_verification': 'authenticated_owner_approval',
            'task_binding_verified': True, 'execution_verified': False,
            'artifact_content_verified': False, 'native_tools_intercepted': False}


def message(action='submit', **extra):
    payload = {'request_id': REQUEST}
    if action == 'submit':
        payload['input'] = '明确的开放任务，不选择 native context。'
    payload.update(extra)
    return {'schema': 1, 'action': action, 'payload': payload}


class PartialInput(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 3))


class PartialOutput(io.BytesIO):
    def write(self, value):
        return super().write(value[:2])


class NativeFramingTests(unittest.TestCase):
    def test_four_byte_native_endian_unicode_and_partial_reads(self):
        value = message(input='任务：中文与 🐾')
        framed = packet(value)
        self.assertEqual(len(framed) - 4, struct.unpack('=I', framed[:4])[0])
        self.assertEqual(value, native.read_message(PartialInput(framed)))

    def test_partial_writes_preserve_complete_single_frame(self):
        outgoing = PartialOutput()
        value = {'schema': 1, '文本': '结果'}
        native.write_message(outgoing, value)
        self.assertEqual(value, native.read_message(io.BytesIO(outgoing.getvalue())))

    def test_empty_input_is_clean_exit_no_stdout_and_no_rpc(self):
        outgoing, client = io.BytesIO(), mock.Mock()
        self.assertEqual(0, native.run_once(io.BytesIO(), outgoing, client))
        self.assertEqual(b'', outgoing.getvalue())
        client.request.assert_not_called()

    def test_truncated_header_or_body_and_zero_length_rejected(self):
        for raw in (b'\x01', b'\x01\x00\x00', struct.pack('=I', 0),
                    struct.pack('=I', 3) + b'{}'):
            with self.subTest(raw=raw), self.assertRaises(native.NativeMessageError):
                native.read_message(io.BytesIO(raw))

    def test_oversize_is_rejected_before_reading_body(self):
        incoming = mock.Mock()
        incoming.read.return_value = struct.pack('=I', native.MAX_INPUT_FRAME + 1)
        with self.assertRaises(native.NativeMessageError):
            native.read_message(incoming)
        incoming.read.assert_called_once_with(4)

    def test_duplicate_keys_nonfinite_invalid_utf8_and_surrogates(self):
        for raw in (b'{"action":"submit","action":"status"}', b'{"n":NaN}',
                    b'{"n":Infinity}', b'"\xff"', b'"\\ud800"'):
            with self.subTest(raw=raw), self.assertRaises(native.NativeMessageError):
                native.read_message(io.BytesIO(struct.pack('=I', len(raw)) + raw))

    def test_response_size_and_nonfinite_bounded_before_any_write(self):
        for value in ({'value': 'x' * native.MAX_OUTPUT_FRAME}, {'value': float('nan')}):
            outgoing = io.BytesIO()
            with self.assertRaises(native.NativeMessageError):
                native.write_message(outgoing, value)
            self.assertEqual(b'', outgoing.getvalue())

    def test_only_first_packet_consumed_no_batch_or_replay(self):
        client = mock.Mock()
        client.request.return_value = status('submit')
        outgoing = io.BytesIO()
        incoming = io.BytesIO(packet(message()) + packet(message('status')))
        native.run_once(incoming, outgoing, client)
        self.assertEqual(1, client.request.call_count)
        self.assertEqual(message('status'), native.read_message(incoming))
        decoded = io.BytesIO(outgoing.getvalue())
        self.assertTrue(native.read_message(decoded)['ok'])
        self.assertIsNone(native.read_message(decoded))


class NativeContractTests(unittest.TestCase):
    def test_exact_three_routes_and_original_id_body_only(self):
        for action, route, reply in (
                ('submit', '/v1/ingress/tasks', status('submit')),
                ('status', '/v1/ingress/task/status', status()),
                ('result', '/v1/ingress/task/result', approved())):
            client = mock.Mock()
            client.request.return_value = reply
            response = native.dispatch(client, message(action))
            client.request.assert_called_once_with(route, message(action)['payload'])
            self.assertEqual(REQUEST, response['data']['request_id'])
            self.assertFalse(response['account_verified'])
            self.assertFalse(response['retry_with_new_id'])

    def test_no_parameter_can_pick_config_path_url_role_shell_or_global_method(self):
        for field in ('config_path', 'token_file', 'url', 'method', 'role', 'subject',
                      'source', 'task_id', 'capabilities', 'session_scope', 'shell', 'required'):
            client = mock.Mock()
            with self.subTest(field=field), self.assertRaises(native.NativeMessageError):
                native.dispatch(client, message(**{field: 'injection'}))
            client.request.assert_not_called()
        for action in ('GET', 'shell', 'claim', 'task/update', 'operator', 'steer'):
            with self.subTest(action=action), self.assertRaises(native.NativeMessageError):
                native.validate_message(message(action))

    def test_schema_must_be_integer_and_outer_payload_closed(self):
        for value in (None, [], {'schema': True, 'action': 'submit', 'payload': {}},
                      dict(message(), config_path='/private/file'),
                      {'schema': 1, 'action': 'status', 'payload': {'request_id': REQUEST, 'input': 'extra'}}):
            with self.subTest(value=value), self.assertRaises(native.NativeMessageError):
                native.validate_message(value)

    def test_text_utf8_byte_limit_labels_are_requested_not_native_context(self):
        original = message(input='中' * (native.MAX_TASK_TEXT // 3),
                           project_id='开放项目', agent_id='希望的工作者')
        self.assertEqual(original['payload'], native.validate_message(original)[1])
        for text in (' ', '\ud800', '中' * (native.MAX_TASK_TEXT // 3 + 1)):
            with self.subTest(text_length=len(text)), self.assertRaises(native.NativeMessageError):
                native.validate_message(message(input=text))

    def test_extra_private_response_field_entire_response_rejected_not_leaked(self):
        client = mock.Mock()
        client.request.return_value = dict(status('submit'), raw_result='PRIVATE_NATIVE_RESULT')
        reply = native.dispatch(client, message())
        self.assertFalse(reply['ok'])
        self.assertEqual('unknown', reply['outcome'])
        self.assertNotIn('PRIVATE_', json.dumps(reply))
        self.assertEqual(1, client.request.call_count)

    def test_wrong_request_task_or_verification_flags_not_success(self):
        for patch in ({'request_id': 'another'}, {'task_id': 'ordinary-global-task'},
                      {'execution_verified': True}, {'native_tools_intercepted': True},
                      {'status_recognized': False}, {'status': 'arbitrary_private_status'},
                      {'status': 'unrecognized', 'status_recognized': True}):
            with self.subTest(patch=patch), self.assertRaises(native.NativeMessageError):
                native.project_response('status', REQUEST, dict(status(), **patch))
        value = dict(status(), status='unrecognized', status_recognized=False)
        self.assertFalse(native.project_response('status', REQUEST, value)['data']['status_recognized'])

    def test_approved_summary_public_references_only_not_url_content_verified(self):
        value = native.project_response('result', REQUEST, approved())
        self.assertEqual(approved(), value['data'])
        self.assertFalse(value['data']['artifact_content_verified'])
        self.assertFalse(value['data']['execution_verified'])
        self.assertEqual('authenticated_owner_approval', value['data']['review_verification'])

    def test_result_rejects_raw_content_local_private_token_urls_and_extra_fields(self):
        for url in ('file:///private/result.txt', 'https://localhost/result',
                    'https://127.0.0.1/result', 'https://10.0.0.1/result',
                    'https://user:password@example.com/result',
                    'https://example.com/result?token=secret', 'https://example.com/result#secret'):
            value = approved()
            value['result']['artifacts'][0]['url'] = url
            with self.subTest(url=url), self.assertRaises(native.NativeMessageError):
                native.project_response('result', REQUEST, value)
        for field, inserted in (('checkpoint', {'native_thread': 'PRIVATE'}),
                                ('raw_result', 'PRIVATE_RESULT')):
            value = dict(approved(), **{field: inserted})
            with self.subTest(field=field), self.assertRaises(native.NativeMessageError):
                native.project_response('result', REQUEST, value)

    def test_error_text_headers_paths_and_private_http_body_never_echoed(self):
        for failure, expected in (
                (RuntimeError('Bearer PRIVATE_TOKEN /private/config'), 'authority_response_unknown'),
                (urllib.error.HTTPError('https://private/url', 403, 'Bearer PRIVATE_TOKEN',
                                        {'secret': 'PRIVATE'}, io.BytesIO(b'PRIVATE_HTTP_BODY')), 'authority_rejected'),
                (urllib.error.HTTPError('https://private/url', 500, 'PRIVATE_SERVER_ERROR',
                                        {}, io.BytesIO(b'PRIVATE_HTTP_BODY')), 'authority_unavailable')):
            client = mock.Mock()
            client.request.side_effect = failure
            reply = native.dispatch(client, message())
            self.assertEqual(expected, reply['error'])
            self.assertFalse(reply['retry_with_new_id'])
            self.assertNotIn('PRIVATE', json.dumps(reply))
            client.request.assert_called_once()

    def test_invalid_input_emits_only_framed_error_never_rpc(self):
        incoming = io.BytesIO(packet(dict(message(), token='PRIVATE_TOKEN')))
        outgoing, client = io.BytesIO(), mock.Mock()
        native.run_once(incoming, outgoing, client)
        response = native.read_message(io.BytesIO(outgoing.getvalue()))
        self.assertEqual('not_attempted', response['outcome'])
        self.assertNotIn('PRIVATE', json.dumps(response))
        client.request.assert_not_called()


class NativeDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-native-tests.')
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        self.token = self.directory / 'ingress-token'
        self.token.write_text('PRIVATE_INGRESS_TOKEN_' + 'x' * 32, encoding='utf8')
        self.token.chmod(0o600)
        self.config = self.directory / 'client.json'
        self.config.write_text(json.dumps({'control_url': 'http://127.0.0.1:18765',
                                          'token_file': str(self.token)}), encoding='utf8')
        self.config.chmod(0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def test_private_fixed_config_constructs_existing_client_without_rpc(self):
        client = native.client_from_config(self.config)
        self.assertEqual('PRIVATE_INGRESS_TOKEN_' + 'x' * 32, client.token)
        self.assertEqual('http://127.0.0.1:18765', client.url)

    def test_config_token_private_permissions_hardlinks_and_symlinks_required(self):
        for target in (self.config, self.token):
            target.chmod(0o644)
            with self.subTest(target=target.name), self.assertRaises(native.NativeMessageError):
                native.client_from_config(self.config)
            target.chmod(0o600)
        alias = self.directory / 'linked-token'
        alias.symlink_to(self.token)
        with self.assertRaises(native.NativeMessageError):
            native._private_bytes(alias)
        alias.unlink()
        os.link(self.token, alias)
        with self.assertRaises(native.NativeMessageError):
            native.client_from_config(self.config)
        alias.unlink()
        self.directory.chmod(0o755)
        with self.assertRaises(native.NativeMessageError):
            native.client_from_config(self.config)

    def test_relative_ancestor_symlink_directory_or_oversize_config_rejected(self):
        with self.assertRaises(native.NativeMessageError):
            native._private_bytes('relative.json')
        with self.assertRaises(native.NativeMessageError):
            native._private_bytes(self.directory)
        alias = self.directory / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(native.NativeMessageError):
            native._private_bytes(alias / 'client.json')
        self.config.write_text('x' * 32769)
        with self.assertRaises(native.NativeMessageError):
            native.client_from_config(self.config)

    def test_shared_nonsticky_writable_ancestor_rejected_without_permission_repair(self):
        unsafe = self.directory / 'shared-ancestor'
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        inner = unsafe / 'private'
        inner.mkdir(mode=0o700)
        target = inner / 'client.json'
        target.write_text('{}')
        target.chmod(0o600)
        with self.assertRaises(native.NativeMessageError):
            native._private_bytes(target)
        self.assertEqual(0o777, unsafe.stat().st_mode & 0o777)

    def test_untrusted_configuration_shape_and_plain_http_remote_rejected(self):
        valid = {'control_url': 'http://127.0.0.1:18765', 'token_file': str(self.token)}
        for patch in ({'url': 'https://evil.invalid'}, {'shell': 'fish'}, {'role': 'operator'},
                      {'control_url': 'http://example.com'},
                      {'control_url': 'https://user:secret@example.com'},
                      {'control_url': 'https://example.com?token=private'}):
            self.config.write_text(json.dumps(dict(valid, **patch)))
            with self.subTest(patch=patch), self.assertRaises(native.NativeMessageError):
                native.client_from_config(self.config)

    def test_wrong_browser_id_and_extra_args_rejected_before_credential_read(self):
        for args in ([], ['https://fake/path', native.EXTENSION_ID],
                     ['/absolute/native.json', 'wrong@extension'],
                     ['/absolute/native.json', native.EXTENSION_ID, '/injected/config']):
            outgoing = io.BytesIO()
            with mock.patch.object(native, 'client_from_config') as configure:
                result = native.main(self.config, args, io.BytesIO(packet(message())), outgoing)
            configure.assert_not_called()
            self.assertEqual(1, result)
            self.assertEqual('native_host_unavailable', native.read_message(io.BytesIO(outgoing.getvalue()))['error'])

    def test_fixed_launcher_main_does_not_accept_a_config_message_or_echo_errors(self):
        client = mock.Mock()
        client.request.return_value = status('submit')
        outgoing = io.BytesIO()
        with mock.patch.object(native, 'client_from_config', return_value=client) as configure:
            result = native.main(self.config, ['/manifest.json', native.EXTENSION_ID],
                                 io.BytesIO(packet(message())), outgoing)
        configure.assert_called_once_with(self.config)
        self.assertEqual(0, result)
        self.assertTrue(native.read_message(io.BytesIO(outgoing.getvalue()))['ok'])
        outgoing = io.BytesIO()
        with mock.patch.object(native, 'client_from_config', side_effect=RuntimeError('PRIVATE_PATH_OR_TOKEN')):
            native.main(self.config, ['/manifest.json', native.EXTENSION_ID], io.BytesIO(), outgoing)
        self.assertNotIn(b'PRIVATE', outgoing.getvalue())

    def test_manifest_fixed_id_narrow_permissions_no_automatic_page_injection(self):
        directory = ROOT / 'browser/firefox-mesh'
        manifest = json.loads((directory / 'manifest.json').read_text())
        self.assertEqual(native.EXTENSION_ID, manifest['browser_specific_settings']['gecko']['id'])
        self.assertEqual({'activeTab', 'nativeMessaging', 'storage'}, set(manifest['permissions']))
        for field in ('content_scripts', 'host_permissions', 'externally_connectable'):
            self.assertNotIn(field, manifest)
        host = json.loads((directory / 'native-host.json.example').read_text())
        self.assertEqual([native.EXTENSION_ID], host['allowed_extensions'])
        self.assertEqual(native.HOST_NAME, host['name'])
        self.assertEqual('stdio', host['type'])
        self.assertNotIn('allowed_origins', host)
        source = (directory / 'popup.js').read_text()
        self.assertNotIn('.innerHTML', source)
        self.assertIn('.textContent', source)

    def test_unsigned_package_exact_allowlist_no_config_test_launcher_or_overwrite(self):
        output = self.directory / 'offline-development.xpi'
        script = ROOT / 'browser/firefox-mesh/build.py'
        completed = subprocess.run(['python', str(script), str(output)], capture_output=True,
                                   text=True, timeout=10)
        self.assertEqual(0, completed.returncode, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertFalse(receipt['signed'])
        self.assertFalse(receipt['installed'])
        self.assertFalse(receipt['account_verified'])
        expected = {'manifest.json', 'background.js', 'popup.html', 'popup.js', 'popup.css', 'LICENSE'}
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(expected, set(archive.namelist()))
            for name in expected - {'LICENSE'}:
                self.assertEqual((ROOT / 'browser/firefox-mesh' / name).read_bytes(), archive.read(name))
        before = output.read_bytes()
        duplicate = subprocess.run(['python', str(script), str(output)], capture_output=True,
                                   text=True, timeout=10)
        self.assertEqual(1, duplicate.returncode)
        self.assertEqual(before, output.read_bytes())

    @unittest.skipUnless(shutil.which('node'), 'Node unavailable; offline JS test remains in package')
    def test_offline_mock_firefox_state_permission_and_lost_reply_contracts(self):
        result = subprocess.run(['node', str(ROOT / 'browser/firefox-mesh/test_background.js')],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('offline contracts passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
