"""Local CLI routing/redaction only; no real auth, code execution or services."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from assistant_mesh.cli import main
from assistant_mesh.tool_lifecycle import ToolLifecycleError


class ToolLifecycleCLITests(unittest.TestCase):
    def setUp(self):
        self.mask = os.umask(0o077)
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-tool-cli.')
        self.root = Path(self.temp.name)
        token = self.root / 'not-read.token'
        token.write_text('owned-test-token-not-real-' + 'x' * 40)
        token.chmod(0o600)
        self.config = self.root / 'client.json'
        self.config.write_text(json.dumps({'control_url': 'http://127.0.0.1:1',
                                          'token_file': str(self.root / 'not-read.token')}))
        self.config.chmod(0o600)
        self.base = ['mesh', '--config', str(self.config), 'tool-release',
                     '--owner-config', 'DO_NOT_PRINT', '--state-dir', 'DO_NOT_PRINT',
                     '--operation-id', 'release-1']
        self.controller = Mock()
        self.controller.stage.return_value = {'state': 'staged', 'execution_verified': False}
        self.controller.publish.return_value = {'state': 'published', 'execution_verified': False}
        self.controller.activate.return_value = {'state': 'manifest_activated', 'runtime_loaded_verified': False}
        self.controller.inspect.return_value = {'state': 'staged', 'execution_verified': False}
        self.controller.prepare_rollback.return_value = {'state': 'staged', 'execution_verified': False}

    def tearDown(self):
        self.temp.cleanup()
        os.umask(self.mask)

    def invoke(self, extra=None, argv=None):
        out, err, code = io.StringIO(), io.StringIO(), 0
        with patch('sys.argv', (argv or self.base) + (extra or [])), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main()
            except SystemExit as error:
                code = error.code
        output = out.getvalue() + err.getvalue()
        self.assertNotIn('DO_NOT_PRINT', output)
        self.assertNotIn(str(self.root), output)
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_explicit_stage_routes_only_to_offline_stage(self):
        with patch('assistant_mesh.tool_lifecycle.ToolLifecycle', return_value=self.controller):
            code, lines = self.invoke(['--action', 'stage', '--candidate-config', 'candidate', '--publication-config', 'publication'])
        self.assertEqual(0, code)
        self.assertEqual('staged', lines[0]['state'])
        self.controller.stage.assert_called_once_with('release-1', 'candidate', 'publication')
        self.controller.publish.assert_not_called()
        self.controller.activate.assert_not_called()

    def test_other_actions_do_not_publish_activate_or_execute_implicitly(self):
        with patch('assistant_mesh.tool_lifecycle.ToolLifecycle', return_value=self.controller):
            for action in ('publish', 'activate', 'inspect'):
                self.assertEqual(0, self.invoke(['--action', action])[0])
                getattr(self.controller, action).assert_called_once_with('release-1')
            self.assertEqual(0, self.invoke(['--action', 'prepare-rollback', '--retained-operation-id', 'old-1', '--epoch', '3'])[0])
        self.controller.prepare_rollback.assert_called_once_with('release-1', 'old-1', 3)
        self.controller.stage.assert_not_called()

    def test_unknown_publication_has_nonzero_process_exit_without_replay(self):
        self.controller.publish.return_value = {'state': 'unknown', 'retry_with_new_id': False}
        with patch('assistant_mesh.tool_lifecycle.ToolLifecycle', return_value=self.controller):
            code, lines = self.invoke(['--action', 'publish'])
        self.assertEqual(2, code)
        self.assertEqual('unknown', lines[0]['state'])
        self.controller.publish.assert_called_once()
        self.controller.stage.assert_not_called()

    def test_bad_arguments_redact_before_controller_construction(self):
        cases = [[], ['--action', 'DO_NOT_PRINT'], ['--action', 'stage'],
                 ['--action', 'inspect', '--payload-file', 'DO_NOT_PRINT'],
                 ['--action', 'inspect', '--epoch', '1'],
                 ['--action', 'prepare-rollback', '--retained-operation-id', 'old', '--epoch', '0'],
                 ['--action', 'prepare-rollback', '--retained-operation-id', 'old', '--epoch', 'DO_NOT_PRINT'],
                 ['--action', 'inspect', '--DO_NOT_PRINT', 'DO_NOT_PRINT']]
        with patch('assistant_mesh.tool_lifecycle.ToolLifecycle') as controller:
            for args in cases:
                with self.subTest(args=args):
                    code, lines = self.invoke(args)
                    self.assertEqual(2, code)
                    self.assertEqual(['tool_arguments_invalid'], lines[0]['diagnostics'])
            controller.assert_not_called()

    def test_config_and_unexpected_errors_are_fixed_categories(self):
        self.config.chmod(0o644)
        self.assertEqual(['tool_client_config_invalid'], self.invoke(['--action', 'inspect'])[1][0]['diagnostics'])
        self.config.chmod(0o600)
        for error, expected in ((ToolLifecycleError('lifecycle_busy'), 'lifecycle_busy'),
                                (ToolLifecycleError('DO_NOT_PRINT'), 'tool_lifecycle_failed'),
                                (RuntimeError('DO_NOT_PRINT'), 'tool_lifecycle_failed')):
            with patch('assistant_mesh.tool_lifecycle.ToolLifecycle', side_effect=error):
                code, lines = self.invoke(['--action', 'inspect'])
                self.assertEqual(2, code)
                self.assertEqual([expected], lines[0]['diagnostics'])

    def test_missing_config_is_redacted_and_no_installation_occurs(self):
        with patch('assistant_mesh.tool_lifecycle.ToolLifecycle') as controller:
            code, lines = self.invoke(argv=['mesh', 'tool-release', '--action', 'inspect'])
        self.assertEqual(2, code)
        self.assertEqual(['tool_arguments_invalid'], lines[0]['diagnostics'])
        controller.assert_not_called()

    def test_existing_commands_reject_tool_options_without_importing_controller(self):
        with patch('assistant_mesh.tool_lifecycle.ToolLifecycle') as controller:
            with patch('sys.argv', ['mesh', '--config', str(self.config), 'status', '--operation-id', 'release-1']), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(2, caught.exception.code)
            controller.assert_not_called()


if __name__ == '__main__':
    unittest.main()
