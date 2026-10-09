"""Offline MCP framing, managed capability boundaries and actual stdio child."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from assistant_mesh import mcp_entrypoint as mcp


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins' / 'personal-assistant-mesh'
REQUEST = 'mesh-mcp-original-001'
TASK = 'ingress-' + '2' * 64


def rpc(method, params=None, request_id=1):
    result = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None:
        result['params'] = params
    return result


def initialize(version=mcp.PROTOCOL_VERSION):
    return rpc('initialize', {'protocolVersion': version, 'capabilities': {},
                              'clientInfo': {'name': 'offline-client', 'version': '1.0'}})


def ready(client=None):
    client = mock.Mock() if client is None else client
    session = mcp.Session(client)
    session.handle(initialize())
    session.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    return session, client


def status(action='status', request_id=REQUEST):
    value = {'format': 'owner-ingress/1', 'request_id': request_id,
             'task_id': TASK, 'accepted': True, 'status': 'completed',
             'status_recognized': True, 'result_available': True,
             'execution_verified': False, 'native_tools_intercepted': False}
    if action == 'submit':
        value['task_created'] = False
    return value


def approved():
    return {'format': 'owner-ingress-result/1', 'publication_id': 'owner-approved-1',
            'request_id': REQUEST, 'task_id': TASK, 'published': True,
            'result': {'summary': '有限的 owner 审查结果。', 'artifacts': [
                {'url': 'https://github.com/example-owner/repo/blob/main/report.md',
                 'label': '公开成果', 'declared_sha256': 'a' * 64}]},
            'review_verification': 'authenticated_owner_approval',
            'task_binding_verified': True, 'execution_verified': False,
            'artifact_content_verified': False, 'native_tools_intercepted': False}


def call(name='mesh_submit_task', arguments=None):
    if arguments is None:
        arguments = {'request_id': REQUEST}
        if name == 'mesh_submit_task':
            arguments['input'] = '授权给 Mesh 的开放任务。'
    return rpc('tools/call', {'name': name, 'arguments': arguments}, 3)


def lines(*messages):
    return b''.join((json.dumps(item, ensure_ascii=False) + '\n').encode('utf8') for item in messages)


class McpLifecycleTests(unittest.TestCase):
    def test_initialize_is_actual_legacy_version_and_tools_only(self):
        session, client = mcp.Session(mock.Mock()), mock.Mock()
        result = session.handle(initialize())['result']
        self.assertEqual('2025-06-18', result['protocolVersion'])
        self.assertEqual({'tools': {'listChanged': False}}, result['capabilities'])
        self.assertIn('native Shell', result['instructions'])
        self.assertFalse(session.ready)

    def test_newer_or_older_proposals_negotiate_only_supported_version(self):
        for version in ('2026-07-28', '2025-11-25', '2024-11-05'):
            with self.subTest(version=version):
                result = mcp.Session(mock.Mock()).handle(initialize(version))['result']
                self.assertEqual(mcp.PROTOCOL_VERSION, result['protocolVersion'])
                self.assertNotEqual(version, result['protocolVersion'])

    def test_tools_not_available_before_initialized_notification(self):
        session = mcp.Session(mock.Mock())
        for phase in (0, 1):
            if phase:
                session.handle(initialize())
            reply = session.handle(call())
            self.assertEqual(-32000, reply['error']['code'])
        session.client.request.assert_not_called()

    def test_ping_before_and_after_init_has_no_rpc(self):
        session = mcp.Session(mock.Mock())
        self.assertEqual({}, session.handle(rpc('ping'))['result'])
        session.handle(initialize())
        self.assertEqual({}, session.handle(rpc('ping', {'_meta': {'trace': 'ignored'}}))['result'])
        session.client.request.assert_not_called()

    def test_reinitialize_rejected_without_altering_session(self):
        session, client = ready()
        self.assertEqual(-32600, session.handle(initialize())['error']['code'])
        self.assertTrue(session.ready)
        client.request.assert_not_called()

    def test_invalid_initialize_fields_fixed_errors_without_client_echo(self):
        valid = initialize()
        for patch in ({'token_file': 'PRIVATE_PATH'}, {'protocolVersion': None},
                      {'capabilities': []}, {'clientInfo': {'name': 'PRIVATE'}}):
            request = dict(valid, params=dict(valid['params'], **patch))
            reply = mcp.Session(mock.Mock()).handle(request)
            self.assertEqual(-32602, reply['error']['code'])
            self.assertNotIn('PRIVATE', json.dumps(reply))

    def test_only_three_closed_tools_no_parameter_authority_or_configuration(self):
        session, client = ready()
        result = session.handle(rpc('tools/list', {'_meta': {'traceparent': 'ignored'}}))['result']
        self.assertEqual(set(mcp._ACTIONS), {item['name'] for item in result['tools']})
        for tool in result['tools']:
            schema = tool['inputSchema']
            self.assertIs(schema['additionalProperties'], False)
            self.assertEqual({'request_id'}, set(schema['required']) - {'input'})
            self.assertEqual(tool['name'] == 'mesh_submit_task', tool['annotations']['destructiveHint'])
            self.assertEqual(tool['name'] == 'mesh_submit_task', tool['annotations']['openWorldHint'])
            self.assertEqual(tool['name'] != 'mesh_submit_task', tool['annotations']['readOnlyHint'])
            for field in ('token', 'config_path', 'url', 'role', 'subject', 'session_scope', 'shell', 'task_id'):
                self.assertNotIn(field, schema['properties'])
        client.request.assert_not_called()

    def test_pagination_unknown_methods_and_non_object_params_rejected(self):
        session, client = ready()
        for request in (rpc('tools/list', {'cursor': 'PRIVATE'}), rpc('shell', {}),
                        rpc('resources/read', {'uri': 'PRIVATE'}), rpc('tools/list', [])):
            reply = session.handle(request)
            self.assertIn(reply['error']['code'], (-32601, -32602))
            self.assertNotIn('PRIVATE', json.dumps(reply))
        client.request.assert_not_called()

    def test_notifications_including_tool_call_and_cancel_never_write_or_respond(self):
        session, client = ready()
        for method in ('tools/call', 'notifications/cancelled', 'notifications/progress', 'unknown'):
            request = dict(call(), method=method)
            request.pop('id')
            self.assertIsNone(session.handle(request))
        client.request.assert_not_called()

    def test_jsonrpc_ids_preserved_but_null_bool_float_or_oversize_rejected(self):
        session, client = ready()
        for request_id in (1, 0, -1, 'original-jsonrpc-id'):
            self.assertEqual(request_id, session.handle(rpc('ping', request_id=request_id))['id'])
        for request_id in (None, True, 1.5, 2 ** 64, 'x' * 201):
            self.assertEqual(-32600, session.handle(rpc('ping', request_id=request_id))['error']['code'])
        client.request.assert_not_called()

    def test_batches_top_level_extensions_and_null_rejected(self):
        session, client = ready()
        for value in (None, [], [call()], {'jsonrpc': '1.0', 'id': 1, 'method': 'ping'},
                      dict(call(), token_file='PRIVATE')):
            reply = session.handle(value)
            self.assertEqual(-32600, reply['error']['code'])
            self.assertNotIn('PRIVATE', json.dumps(reply))
        client.request.assert_not_called()


class McpManagedBoundaryTests(unittest.TestCase):
    def test_fixed_three_routes_reuse_projection_and_original_request_id(self):
        for name, route, reply in (
                ('mesh_submit_task', '/v1/ingress/tasks', status('submit')),
                ('mesh_task_status', '/v1/ingress/task/status', status()),
                ('mesh_approved_result', '/v1/ingress/task/result', approved())):
            session, client = ready()
            client.request.return_value = reply
            request = call(name)
            result = session.handle(request)['result']
            self.assertFalse(result['isError'])
            self.assertEqual(result['structuredContent'], json.loads(result['content'][0]['text']))
            self.assertEqual(REQUEST, result['structuredContent']['data']['request_id'])
            self.assertFalse(result['structuredContent']['account_verified'])
            client.request.assert_called_once_with(route, request['params']['arguments'])

    def test_input_cannot_pick_identity_native_scope_operator_or_shell(self):
        for field in ('config_path', 'token_file', 'control_url', 'url', 'role', 'owner_id',
                      'subject', 'source', 'task_id', 'required', 'session_scope', 'shell', 'capabilities'):
            session, client = ready()
            request = call(arguments={'request_id': REQUEST, 'input': 'task', field: 'PRIVATE'})
            self.assertEqual(-32602, session.handle(request)['error']['code'])
            client.request.assert_not_called()

    def test_submit_labels_model_readable_without_privilege_and_meta_not_forwarded(self):
        session, client = ready()
        client.request.return_value = status('submit')
        request = call(arguments={'request_id': REQUEST, 'input': 'task', 'project_id': 'project', 'agent_id': 'label'})
        request['params']['_meta'] = {'role': 'operator', 'subject': 'PRIVATE'}
        session.handle(request)
        client.request.assert_called_once_with('/v1/ingress/tasks', request['params']['arguments'])

    def test_unknown_timeout_and_committed_but_unreadable_reply_never_retry(self):
        for failure in (TimeoutError('PRIVATE'), ValueError('PRIVATE'), RuntimeError('PRIVATE')):
            session, client = ready()
            client.request.side_effect = failure
            result = session.handle(call())['result']
            self.assertTrue(result['isError'])
            self.assertEqual('unknown', result['structuredContent']['outcome'])
            self.assertFalse(result['structuredContent']['retry_with_new_id'])
            self.assertNotIn('PRIVATE', json.dumps(result))
            client.request.assert_called_once()

    def test_http_rejections_and_unknown_http_errors_fixed_and_close_body(self):
        for code, outcome in ((403, 'rejected'), (409, 'rejected'), (500, 'unknown')):
            session, client = ready()
            body = io.BytesIO(b'PRIVATE_RESPONSE')
            client.request.side_effect = urllib.error.HTTPError('https://private.invalid', code, 'PRIVATE', {}, body)
            result = session.handle(call())['result']
            self.assertEqual(outcome, result['structuredContent']['outcome'])
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertTrue(body.closed)
            client.request.assert_called_once()

    def test_raw_result_checkpoint_credential_and_mismatched_identity_not_reflected(self):
        for patch in ({'checkpoint': {'auth': 'PRIVATE'}}, {'result': 'PRIVATE'},
                      {'request_id': 'other'}, {'task_id': 'PAM-global'},
                      {'execution_verified': True}):
            session, client = ready()
            client.request.return_value = dict(status('submit'), **patch)
            result = session.handle(call())['result']
            self.assertTrue(result['isError'])
            self.assertNotIn('PRIVATE', json.dumps(result))
            client.request.assert_called_once()

    def test_approved_result_cannot_claim_artifact_verification_or_private_url(self):
        values = [dict(approved(), artifact_content_verified=True),
                  dict(approved(), result={'summary': 'review', 'artifacts': [{'url': 'http://127.0.0.1/secret', 'label': 'PRIVATE'}]})]
        for value in values:
            session, client = ready()
            client.request.return_value = value
            result = session.handle(call('mesh_approved_result'))['result']
            self.assertTrue(result['isError'])
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_incomplete_shapes_unknown_tool_null_label_utf8_limit_no_rpc(self):
        for request in (call('publish'), call(arguments={}),
                        call(arguments={'request_id': REQUEST, 'input': 'x' * 16385}),
                        call(arguments={'request_id': REQUEST, 'input': '汉' * 6000}),
                        call(arguments={'request_id': REQUEST, 'input': 'task', 'agent_id': None}),
                        call('mesh_task_status', {'request_id': REQUEST, 'input': 'task'})):
            session, client = ready()
            self.assertEqual(-32602, session.handle(request)['error']['code'])
            client.request.assert_not_called()


class McpFramingTests(unittest.TestCase):
    def test_newline_unicode_crlf_and_partial_writes(self):
        request = call(arguments={'request_id': REQUEST, 'input': '中文\n多行内容 🐾'})
        self.assertEqual(request, mcp.read_line(io.BytesIO(lines(request))))
        self.assertEqual(request, mcp.read_line(io.BytesIO(lines(request).replace(b'\n', b'\r\n'))))
        class Partial(io.BytesIO):
            def write(self, value):
                return super().write(value[:3])
        output = Partial()
        mcp.write_line(output, {'jsonrpc': '2.0', 'id': '文本', 'result': {}})
        self.assertEqual('文本', json.loads(output.getvalue())['id'])
        self.assertTrue(output.getvalue().endswith(b'\n'))

    def test_duplicate_nonfinite_surrogate_invalid_utf8_and_truncation(self):
        for raw in (b'{"id":1,"id":2}\n', b'{"n":NaN}\n', b'"\\ud800"\n',
                    b'"\xff"\n', b'{}', b'\n'):
            with self.subTest(raw=raw), self.assertRaises(mcp.ProtocolError):
                mcp.read_line(io.BytesIO(raw))

    def test_oversize_input_rejected_with_bounded_read_and_no_resynchronization(self):
        input_stream = mock.Mock()
        input_stream.readline.return_value = b'x' * (mcp.MAX_LINE_BYTES + 1)
        with self.assertRaises(mcp.ProtocolError):
            mcp.read_line(input_stream)
        input_stream.readline.assert_called_once_with(mcp.MAX_LINE_BYTES + 1)

    def test_output_size_bounded_before_write(self):
        output = io.BytesIO()
        with self.assertRaises(mcp.ProtocolError):
            mcp.write_line(output, {'private': 'x' * mcp.MAX_RESPONSE_BYTES})
        self.assertEqual(b'', output.getvalue())

    def test_run_clean_eof_and_json_null_not_treated_as_eof(self):
        for payload, responses in ((b'', 0), (b'null\n', 1)):
            client, output = mock.Mock(), io.BytesIO()
            self.assertEqual(0, mcp.run(io.BytesIO(payload), output, client))
            self.assertEqual(responses, len(output.getvalue().splitlines()))
            client.request.assert_not_called()

    def test_bad_line_returns_fixed_error_and_stops_before_business_request(self):
        client, output = mock.Mock(), io.BytesIO()
        self.assertEqual(1, mcp.run(io.BytesIO(b'PRIVATE\n' + lines(call())), output, client))
        self.assertEqual(-32700, json.loads(output.getvalue())['error']['code'])
        self.assertNotIn(b'PRIVATE', output.getvalue())
        client.request.assert_not_called()

    def test_full_session_output_only_rpc_no_notification_response_or_network(self):
        client, output = mock.Mock(), io.BytesIO()
        messages = lines(initialize(), {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                         rpc('tools/list', request_id=2), rpc('ping', request_id=3))
        self.assertEqual(0, mcp.run(io.BytesIO(messages), output, client))
        result = [json.loads(item) for item in output.getvalue().splitlines()]
        self.assertEqual([1, 2, 3], [item['id'] for item in result])
        client.request.assert_not_called()


class McpPluginChildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-mcp-offline.')
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        self.token = self.directory / 'ingress-token'
        self.token.write_text('OFFLINE_PRIVATE_INGRESS_TOKEN_' + 'x' * 32)
        self.token.chmod(0o600)
        self.config = self.directory / 'client.json'
        self.config.write_text(json.dumps({'control_url': 'http://127.0.0.1:1', 'token_file': str(self.token)}))
        self.config.chmod(0o600)
        self.settings = self.directory / 'mcp-launch.json'
        self.settings.write_text(json.dumps({'runtime_root': str(ROOT), 'client_config': str(self.config)}))
        self.settings.chmod(0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def child(self, messages, extra_args=()):
        return subprocess.run([sys.executable, str(PLUGIN / 'server.py')] + list(extra_args),
                              input=messages, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              cwd=str(PLUGIN), timeout=10,
                              env={'PATH': os.environ.get('PATH', ''),
                                   'PERSONAL_ASSISTANT_MESH_PLUGIN_SETTINGS': str(self.settings)})

    def test_actual_stdio_launcher_child_initialize_tools_list_no_network_or_auth(self):
        child = self.child(lines(initialize(), {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                                 rpc('tools/list', request_id=2), rpc('ping', request_id=3)))
        self.assertEqual(0, child.returncode, child.stderr.decode())
        self.assertEqual(b'', child.stderr)
        responses = [json.loads(item) for item in child.stdout.splitlines()]
        self.assertEqual([1, 2, 3], [item['id'] for item in responses])
        self.assertEqual(set(mcp._ACTIONS), {item['name'] for item in responses[1]['result']['tools']})
        self.assertNotIn(b'PRIVATE', child.stdout)
        self.assertEqual({'ingress-token', 'client.json', 'mcp-launch.json'}, {item.name for item in self.directory.iterdir()})

    def test_private_settings_client_and_token_modes_required_no_auto_repair(self):
        for target in (self.settings, self.config, self.token):
            target.chmod(0o644)
            child = self.child(lines(initialize()))
            self.assertEqual(1, child.returncode)
            self.assertEqual(b'', child.stdout)
            self.assertNotIn(b'PRIVATE', child.stderr)
            self.assertEqual(0o644, target.stat().st_mode & 0o777)
            target.chmod(0o600)

    def test_launcher_extra_args_duplicate_settings_or_symlink_rejected(self):
        self.assertEqual(1, self.child(lines(initialize()), ['/injected/private/path']).returncode)
        self.settings.write_text('{"runtime_root":"PRIVATE","runtime_root":"OTHER","client_config":"PRIVATE"}')
        child = self.child(lines(initialize()))
        self.assertEqual(1, child.returncode)
        self.assertNotIn(b'PRIVATE', child.stderr)
        self.settings.unlink()
        self.settings.symlink_to(self.config)
        self.assertEqual(1, self.child(lines(initialize())).returncode)

    def test_portable_manifests_actual_entrypoint_no_remote_hosting_or_secret(self):
        manifest = json.loads((PLUGIN / 'plugin.json').read_text())
        config = json.loads((PLUGIN / 'mcp.json').read_text())
        self.assertEqual('https://agent-plugins.org/schemas/1.0.0/plugin.schema.json', manifest['$schema'])
        self.assertEqual('personal-assistant-mesh', manifest['name'])
        self.assertLessEqual(len(manifest['extensions']['com.openai']['interface']['shortDescription']), 30)
        self.assertLessEqual(len(manifest['extensions']['com.openai']['interface']['defaultPrompt']), 128)
        server = config['mcpServers']['personal-assistant-mesh']
        self.assertEqual('stdio', server['type'])
        self.assertEqual(['${PLUGIN_ROOT}/server.py'], server['args'])
        self.assertTrue((PLUGIN / 'server.py').is_file())
        self.assertNotIn('env', server)
        self.assertNotIn('url', server)
        self.assertNotIn('apps', manifest['extensions']['com.openai'])
        self.assertNotIn('token_file', json.dumps(config))

    def test_shared_sticky_import_root_not_allowed_even_when_ancestor_is_allowed(self):
        shared = self.directory / 'shared-runtime'
        shared.mkdir(mode=0o700)
        shared.chmod(0o1777)
        self.settings.write_text(json.dumps({'runtime_root': str(shared), 'client_config': str(self.config)}))
        child = self.child(lines(initialize()))
        self.assertEqual(1, child.returncode)
        self.assertEqual(b'', child.stdout)
        self.assertEqual(0o1777, shared.stat().st_mode & 0o7777)

    def test_missing_config_main_emits_no_jsonrpc_or_private_traceback(self):
        output = io.BytesIO()
        with mock.patch.object(mcp.native, 'client_from_config', side_effect=ValueError('PRIVATE')), \
                mock.patch.object(mcp.sys, 'stderr', io.StringIO()) as error:
            self.assertEqual(1, mcp.main('/private/not-configured', io.BytesIO(), output))
        self.assertEqual(b'', output.getvalue())
        self.assertEqual('Mesh MCP unavailable\n', error.getvalue())
