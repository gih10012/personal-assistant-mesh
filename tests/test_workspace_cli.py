"""Native CLI isolation; no production enrollment, service or model calls."""
import contextlib
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh import workspace_cli
from assistant_mesh.workspace import WorkspaceError
from tests import test_workspace as workspace_fixture


class WorkspaceCLITests(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace_fixture.WorkspaceTests()
        self.fixture.setUp()
        self.root = self.fixture.root
        self.client_file = self.root / 'client.json'
        self.client_sha = self.save(self.client_file, {'node_id': 'worker'})
        self.config = {'format': 'mesh-workspace-owner/1', 'client_config': str(self.client_file),
            'client_config_sha256': self.client_sha, 'authority': 'cloud', 'node': 'worker',
            'journal_path': str(self.root / 'cli.db'), 'artifact_dir': str(self.fixture.destination),
            'artifact_readers': {}}
        self.config_file = self.root / 'owner.json'
        self.save(self.config_file, self.config)
        self.payload_file = self.root / 'payload.json'
        self.arguments = dict(self.fixture.arguments)
        self.arguments.pop('operation_id')
        self.save(self.payload_file, self.arguments)

    def tearDown(self):
        self.fixture.tearDown()

    @staticmethod
    def save(path, value):
        raw = json.dumps(value, sort_keys=True).encode('utf8')
        path.write_bytes(raw)
        path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    def run_cli(self, action, **kwargs):
        return workspace_cli.execute(str(self.config_file), 'parent', self.fixture.task['epoch'],
            action, client_factory=lambda config: self.fixture.client, **kwargs)

    def test_native_entry_discover_and_original_remote_call_status(self):
        value = self.run_cli('discover')
        self.assertEqual('mesh-routing-context/1', value['format'])
        result = self.run_cli('call', operation_id='op', payload_file=str(self.payload_file))
        self.assertEqual('observed', result['phase'])
        again = self.run_cli('call', operation_id='op', payload_file=str(self.payload_file))
        self.assertEqual(result['request_sha256'], again['request_sha256'])
        self.assertEqual(['propose', 'delegate', 'link'], self.fixture.writes())
        self.assertEqual('observed', self.run_cli('status', operation_id='op')['phase'])

    def test_reader_absence_does_not_reinvoke(self):
        self.run_cli('call', operation_id='op', payload_file=str(self.payload_file))
        before = self.fixture.writes()
        self.assertEqual('artifact_unavailable', self.run_cli('artifact', operation_id='op')['phase'])
        self.assertEqual(before, self.fixture.writes())

    def test_authenticated_node_is_owner_config_not_payload(self):
        for key, value in (('node', 'other'), ('task_id', 'other'), ('epoch', 2),
                           ('artifact_readers', {}), ('client_config', '/PRIVATE_SECRET')):
            with self.subTest(key=key):
                self.save(self.payload_file, dict(self.arguments, **{key: value}))
                with self.assertRaises(WorkspaceError):
                    self.run_cli('call', operation_id='op', payload_file=str(self.payload_file))
        self.assertEqual([], self.fixture.writes())

    def test_original_client_config_is_hash_pinned_before_connection(self):
        self.save(self.client_file, {'node_id': 'worker', 'changed': True})
        with self.assertRaisesRegex(WorkspaceError, 'owner_file_changed'):
            self.run_cli('discover')
        self.assertEqual([], self.fixture.client.calls)

    def test_world_readable_or_linked_configs_refused(self):
        self.config_file.chmod(0o644)
        with self.assertRaises(Exception):
            self.run_cli('discover')
        self.config_file.chmod(0o600)
        alias = self.root / 'alias.json'
        alias.symlink_to(self.config_file)
        with self.assertRaises(Exception):
            workspace_cli.load_workspace(str(alias), 'parent', 1,
                                         lambda config: self.fixture.client)

    def test_duplicate_json_key_and_public_worktree_refused(self):
        self.config_file.write_bytes(b'{"format":"one","format":"two"}')
        with self.assertRaises(Exception):
            self.run_cli('discover')
        self.save(self.config_file, self.config)
        (self.root / '.git').mkdir()
        with self.assertRaisesRegex(WorkspaceError, 'private_path_required'):
            self.run_cli('discover')
        self.assertEqual([], self.fixture.client.calls)

    def test_invalid_arguments_have_no_runtime_initialization(self):
        for action, arguments in (('call', {}), ('status', {}),
                ('artifact', {'operation_id': 'op', 'payload_file': str(self.payload_file)}),
                ('discover', {'operation_id': 'op'})):
            with self.subTest(action=action), self.assertRaises(WorkspaceError):
                self.run_cli(action, **arguments)
        self.assertFalse((self.root / 'cli.db').exists())

    def test_cli_diagnostics_never_echo_paths_or_transport_exceptions(self):
        stream = io.StringIO()
        with mock.patch.object(workspace_cli, 'execute', side_effect=RuntimeError('PRIVATE_TOKEN_PATH')):
            with contextlib.redirect_stdout(stream):
                self.assertEqual(2, workspace_cli.main(['--config', '/PRIVATE_TOKEN_PATH',
                    '--task-id', 'parent', '--epoch', '1', 'discover']))
        self.assertNotIn('PRIVATE_TOKEN_PATH', stream.getvalue())
        self.assertFalse(json.loads(stream.getvalue())['native_tools_intercepted'])

    def test_command_reader_uses_whole_shell_safe_token_and_complete_bytes(self):
        script = self.root / 'owned_reader.py'
        script.write_text('import base64,json,sys\n'
            'request=json.loads(base64.urlsafe_b64decode(sys.argv[2]+"="*(-len(sys.argv[2])%4)))\n'
            'if sys.argv[1]=="manifest": print(json.dumps(request))\n'
            'else: sys.stdout.buffer.write(bytes(range(256))*600)\n')
        script.chmod(0o600)
        binding = {'manifest_argv': [sys.executable, str(script), 'manifest', '{request_base64url}'],
                   'read_argv': [sys.executable, str(script), 'read', '{request_base64url}'],
                   'timeout_seconds': 5}
        reader = workspace_cli.CommandArtifactReader(binding)
        message = 'id; touch /PRIVATE_PATH $(do-not-execute)'
        self.assertEqual(message, reader.manifest(message, 'original-op', 'original-cap')['message_id'])
        raw = reader.read(message, 'original-op', 'original-cap')
        self.assertEqual(bytes(range(256)) * 600, raw)
        self.assertEqual(153600, len(raw))

    def test_command_reader_fails_nonzero_or_oversize_without_stderr(self):
        script = self.root / 'unavailable_reader.py'
        script.write_text('import sys\n'
                          'sys.stderr.write("PRIVATE_TOKEN")\n'
                          'if sys.argv[1]=="manifest": sys.stdout.write("x"*16385)\n'
                          'else: sys.exit(7)\n')
        script.chmod(0o600)
        reader = workspace_cli.CommandArtifactReader({
            'manifest_argv': [sys.executable, str(script), 'manifest', '{request_base64url}'],
            'read_argv': [sys.executable, str(script), 'read', '{request_base64url}'],
            'timeout_seconds': 5})
        for action in ('manifest', 'read'):
            with self.subTest(action=action), self.assertRaisesRegex(WorkspaceError, '^workspace_original_reader_unavailable$'):
                getattr(reader, action)('original-msg', 'original-op', 'original-cap')

    def test_owner_command_cannot_embed_raw_placeholders(self):
        for token in ('prefix-{request_base64url}', '{operation_id}', '{request_base64url};touch x'):
            with self.subTest(token=token), self.assertRaises(WorkspaceError):
                workspace_cli.CommandArtifactReader({
                    'manifest_argv': [sys.executable, token],
                    'read_argv': [sys.executable, '{request_base64url}'], 'timeout_seconds': 5})


if __name__ == '__main__':
    unittest.main()
