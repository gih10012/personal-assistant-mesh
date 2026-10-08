"""Private synthetic layout fixtures; no actual account/model/network access."""
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from assistant_mesh.runtime_health import MAX_MANIFEST_BYTES, UnknownLayout, diagnose
from assistant_mesh.worker import Worker
from scripts.runtime_doctor import main as doctor_main


PRIVATE = 'PRIVATE_CONFIG_AUTH_AND_PROVIDER_BODY_NEVER_OUTPUT'


class RuntimeHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = self.root / 'package'
        self.package.mkdir(mode=0o700)
        for directory in ('bin', 'codex-resources', 'codex-path'):
            (self.package / directory).mkdir(mode=0o700)
        self.executable = self.package / 'bin/codex'
        self.executable.write_bytes(b'fixture: no program should be executed')
        self.executable.chmod(0o755)
        self.helper = self.package / 'bin/codex-code-mode-host'
        self.helper.write_bytes(b'fixture: no helper should be executed')
        self.helper.chmod(0o755)
        self.manifest = self.package / 'codex-package.json'
        self.metadata = {'layoutVersion': 1, 'variant': 'codex', 'version': '0.159.2',
                         'target': 'x86_64-unknown-linux-musl', 'entrypoint': 'bin/codex',
                         'resourcesDir': 'codex-resources', 'pathDir': 'codex-path'}
        self.write_manifest()
        self.auth = self.root / 'auth.json'
        self.auth.write_text(json.dumps({'tokens': PRIVATE}))
        self.auth.chmod(0o600)
        self.config = {'node_id': PRIVATE, 'token_file': str(self.auth),
                       'codex': {'executable': str(self.executable), 'auth_home': str(self.root),
                                 'network_env_file': str(self.auth), 'instructions': PRIVATE,
                                 'sandbox': 'danger-full-access', 'approval_policy': 'never'}}
        self.worker_path = self.root / 'worker.json'
        self.worker_path.write_text(json.dumps(self.config))
        self.worker_path.chmod(0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.metadata))
        self.manifest.chmod(0o644)

    def report(self, config=None):
        report = diagnose(self.config if config is None else config)
        self.assertNotIn(PRIVATE, json.dumps(report))
        self.assertNotIn(str(self.root), json.dumps(report))
        return report

    def codex(self, config=None):
        return self.report(config)['runtimes'][0]

    def doctor(self, path=None):
        output = io.StringIO()
        with patch('sys.argv', ['runtime_doctor', '--config', str(path or self.worker_path)]):
            with contextlib.redirect_stdout(output):
                doctor_main()
        self.assertNotIn(PRIVATE, output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        return json.loads(output.getvalue())

    def test_canonical_layout_is_only_metadata_not_integrity_or_execution_proof(self):
        report = self.report()
        self.assertTrue(report['read_only'])
        self.assertEqual('configured_layout_only', report['observation'])
        self.assertFalse(report['active_process_verified'])
        for key in ('native_tools_intercepted', 'network_accessed', 'inference_started',
                    'automatic_repair', 'task_replayed', 'session_replaced'):
            self.assertFalse(report[key])
        native = report['runtimes'][0]
        self.assertEqual('complete', native['layout'])
        self.assertEqual('runtime_layout_present', native['code'])
        self.assertEqual([], native['missing'])
        self.assertEqual({'version': '0.159.2', 'target': 'x86_64-unknown-linux-musl',
                          'layout_version': 1}, native['manifest'])
        self.assertEqual('not_verified', native['package_provenance'])
        self.assertFalse(native['execution_verified'])
        self.assertEqual('not_checked', native['model_availability'])
        self.assertEqual('not_checked', native['network_availability'])

    def test_only_manifest_is_opened_no_credentials_network_or_subprocess(self):
        opened = []
        original_open = os.open
        def guarded_open(path, flags, *args, **kwargs):
            opened.append(str(path))
            self.assertEqual(str(self.manifest), str(path))
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            return original_open(path, flags, *args, **kwargs)
        with patch('assistant_mesh.runtime_health.os.open', side_effect=guarded_open):
            with patch('assistant_mesh.config.discover_codex_auth', side_effect=AssertionError(PRIVATE)):
                with patch('assistant_mesh.config.read_secret', side_effect=AssertionError(PRIVATE)):
                    with patch('subprocess.Popen', side_effect=AssertionError(PRIVATE)):
                        with patch('socket.create_connection', side_effect=AssertionError(PRIVATE)):
                            with patch('urllib.request.urlopen', side_effect=AssertionError(PRIVATE)):
                                self.assertEqual('complete', self.codex()['layout'])
        self.assertEqual([str(self.manifest)], opened)

    def test_layout_observation_does_not_modify_any_fixture_or_configuration(self):
        paths = [p for p in self.root.rglob('*') if p.is_file()]
        before = {str(p): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in paths}
        original_config = copy.deepcopy(self.config)
        self.report()
        after = {str(p): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(original_config, self.config)

    def test_missing_helper_and_canonical_resource_directories_are_explicit(self):
        self.helper.unlink()
        (self.package / 'codex-resources').rmdir()
        (self.package / 'codex-path').rmdir()
        native = self.codex()
        self.assertEqual('missing', native['layout'])
        self.assertEqual('runtime_required_components_missing', native['code'])
        self.assertEqual(['code_mode_host', 'resources', 'path'], native['missing'])

    def test_missing_manifest_directory_fields_are_observed_as_missing(self):
        del self.metadata['resourcesDir']
        del self.metadata['pathDir']
        self.write_manifest()
        native = self.codex()
        self.assertEqual('missing', native['layout'])
        self.assertEqual(['resources', 'path'], native['missing'])

    def test_missing_entrypoint_and_nonexecutable_helper_are_not_present(self):
        self.executable.unlink()
        self.helper.chmod(0o600)
        native = self.codex()
        self.assertEqual('missing', native['layout'])
        self.assertEqual('missing', native['components']['entrypoint'])
        self.assertEqual('not_executable', native['components']['code_mode_host'])

    def test_custom_wrapper_standalone_and_pi_are_unknown_not_broken(self):
        wrapper = self.root / 'codex-official'
        wrapper.write_text(PRIVATE)
        wrapper.chmod(0o700)
        config = {'codex': {'executable': str(wrapper)},
                  'pi': {'executable': str(self.auth), 'model': PRIVATE}}
        with patch('assistant_mesh.runtime_health.os.open', side_effect=AssertionError(PRIVATE)):
            report = self.report(config)
        self.assertEqual(['unknown', 'unknown'], [r['layout'] for r in report['runtimes']])
        self.assertEqual('runtime_layout_contract_not_available', report['runtimes'][1]['code'])
        self.manifest.unlink()
        native = self.codex()
        self.assertEqual('unknown', native['layout'])
        self.assertEqual('runtime_manifest_not_found', native['code'])

    def test_unresolved_or_relative_entrypoints_are_unknown(self):
        with patch('assistant_mesh.runtime_health.shutil.which', return_value=None):
            self.assertEqual('unknown', self.codex({'codex': {}})['layout'])
        for executable in ('relative/bin/codex', '', None, 'name\n' + PRIVATE):
            with self.subTest(executable=executable):
                native = self.codex({'codex': {'executable': executable}})
                self.assertEqual('unknown', native['layout'])

    def test_path_lookup_does_not_execute_the_program(self):
        with patch('assistant_mesh.runtime_health.shutil.which', return_value=str(self.executable)):
            with patch('subprocess.Popen', side_effect=AssertionError(PRIVATE)):
                self.assertEqual('complete', self.codex({'codex': {'executable': 'codex'}})['layout'])

    def test_symlinked_entrypoint_or_parent_is_unknown(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.package, target_is_directory=True)
        self.assertEqual('runtime_entrypoint_indirect',
                         self.codex({'codex': {'executable': str(alias / 'bin/codex')}})['code'])
        self.executable.unlink()
        self.executable.symlink_to(self.auth)
        with patch('assistant_mesh.runtime_health.os.open', side_effect=AssertionError(PRIVATE)):
            self.assertEqual('unknown', self.codex()['layout'])

    def test_symlink_manifest_and_helper_never_follow_credential_contents(self):
        self.manifest.unlink()
        self.manifest.symlink_to(self.auth)
        with patch('assistant_mesh.runtime_health.os.open', side_effect=AssertionError(PRIVATE)):
            self.assertEqual('runtime_entrypoint_indirect', self.codex()['code'])
        self.manifest.unlink()
        self.write_manifest()
        self.helper.unlink()
        self.helper.symlink_to(self.auth)
        self.assertEqual('runtime_entrypoint_indirect', self.codex()['code'])

    def test_manifest_hardlinks_writable_or_oversized_files_are_not_read(self):
        self.manifest.unlink()
        os.link(str(self.auth), str(self.manifest))
        with patch('assistant_mesh.runtime_health.os.open', side_effect=AssertionError(PRIVATE)):
            self.assertEqual('runtime_manifest_untrusted', self.codex()['code'])
        self.manifest.unlink()
        self.write_manifest()
        self.manifest.chmod(0o666)
        with patch('assistant_mesh.runtime_health.os.open', side_effect=AssertionError(PRIVATE)):
            self.assertEqual('runtime_manifest_untrusted', self.codex()['code'])
        self.manifest.write_bytes(b'x' * (MAX_MANIFEST_BYTES + 1))
        self.manifest.chmod(0o644)
        with patch('assistant_mesh.runtime_health.os.open', side_effect=AssertionError(PRIVATE)):
            self.assertEqual('runtime_manifest_untrusted', self.codex()['code'])

    def test_noncanonical_manifest_fields_never_report_native_layout_complete(self):
        original = dict(self.metadata)
        for key, value in (('variant', 'custom'), ('layoutVersion', 2), ('entrypoint', 'other/codex'),
                           ('entrypoint', '../auth.json'), ('entrypoint', str(self.auth)),
                           ('resourcesDir', 'bin'), ('pathDir', 'bin'), ('version', PRIVATE),
                           ('target', PRIVATE + '\n'), ('version', True)):
            with self.subTest(field=key, value=value):
                self.metadata = dict(original, **{key: value})
                self.write_manifest()
                self.assertEqual('unknown', self.codex()['layout'])

    def test_malformed_manifest_and_all_observer_errors_are_fixed_and_redacted(self):
        self.manifest.write_text(PRIVATE)
        self.assertEqual('runtime_layout_unreadable', self.codex()['code'])
        self.write_manifest()
        for error, code in ((OSError(PRIVATE), 'runtime_layout_unreadable'),
                            (RuntimeError(PRIVATE), 'runtime_diagnosis_unavailable'),
                            (UnknownLayout(PRIVATE), 'runtime_diagnosis_unavailable')):
            with self.subTest(kind=type(error).__name__):
                with patch('assistant_mesh.runtime_health._codex', side_effect=error):
                    self.assertEqual(code, self.codex()['code'])

    def test_configuration_errors_are_fixed_not_a_credential_discovery_request(self):
        self.assertEqual('runtime_configuration_invalid', self.report([])['error'])
        self.assertEqual('runtime_configuration_not_available', self.report({})['error'])
        self.assertEqual('unknown', self.codex({'codex': PRIVATE})['layout'])

    def test_cli_reads_private_configuration_but_no_referenced_auth_or_network(self):
        with patch('assistant_mesh.config.read_secret', side_effect=AssertionError(PRIVATE)):
            with patch('assistant_mesh.config.discover_codex_auth', side_effect=AssertionError(PRIVATE)):
                with patch('subprocess.Popen', side_effect=AssertionError(PRIVATE)):
                    self.assertEqual('complete', self.doctor()['runtimes'][0]['layout'])

    def test_cli_configuration_errors_are_fixed_json_without_traceback(self):
        for mode, body in ((0o644, PRIVATE), (0o600, PRIVATE)):
            self.worker_path.write_text(body)
            self.worker_path.chmod(mode)
            output = io.StringIO()
            with patch('sys.argv', ['runtime_doctor', '--config', str(self.worker_path)]):
                with contextlib.redirect_stdout(output):
                    with self.assertRaises(SystemExit) as failed:
                        doctor_main()
            self.assertEqual(2, failed.exception.code)
            self.assertEqual('runtime_doctor_configuration_unavailable', json.loads(output.getvalue())['error'])
            self.assertNotIn(PRIVATE, output.getvalue())
            self.assertNotIn(str(self.root), output.getvalue())

    def test_gateway_local_diagnosis_needs_no_heartbeat_lease_or_remote_authority(self):
        client = Mock()
        client.request.side_effect = OSError(PRIVATE)
        worker = Worker(self.config, client=client)
        worker.tick = Mock(side_effect=OSError(PRIVATE))
        original = copy.deepcopy(worker.config)
        response = worker.on_tool({'tool': 'mesh', 'arguments': {'action': 'runtime_diagnose', 'arguments': {}}})
        self.assertTrue(response['success'])
        self.assertEqual('complete', json.loads(response['contentItems'][0]['text'])['runtimes'][0]['layout'])
        client.request.assert_not_called()
        worker.tick.assert_not_called()
        self.assertEqual(original, worker.config)
        self.assertIsNone(worker.current)
        self.assertIsNone(worker.agent)

    def test_gateway_rejects_all_caller_selected_paths_configs_and_identities(self):
        client = Mock()
        worker = Worker(self.config, client=client)
        worker.tick = Mock(side_effect=OSError(PRIVATE))
        for arguments in ({'config': str(self.auth)}, {'path': str(self.auth)}, {'actor': 'operator'},
                          {'node': 'other-node'}, {'execute': True}, None, [], PRIVATE):
            with self.subTest(arguments=arguments):
                response = worker.on_tool({'tool': 'mesh', 'arguments': {
                    'action': 'runtime_diagnose', 'arguments': arguments}})
                self.assertFalse(response['success'])
                self.assertEqual('invalid_runtime_diagnose_arguments',
                                 json.loads(response['contentItems'][0]['text'])['error'])
                self.assertNotIn(PRIVATE, json.dumps(response))
        response = worker.on_tool({'tool': 'mesh', 'arguments': {
            'action': 'runtime_diagnose', 'arguments': {}, 'config': str(self.auth)}})
        self.assertFalse(response['success'])
        client.request.assert_not_called()
        worker.tick.assert_not_called()

    def test_gateway_unexpected_observer_exception_is_fixed(self):
        worker = Worker(self.config, client=Mock())
        with patch('assistant_mesh.worker.diagnose_runtime', side_effect=RuntimeError(PRIVATE)):
            response = worker.on_tool({'tool': 'mesh', 'arguments': {'action': 'runtime_diagnose'}})
        self.assertFalse(response['success'])
        self.assertEqual('runtime_diagnosis_unavailable', json.loads(response['contentItems'][0]['text'])['error'])
        self.assertNotIn(PRIVATE, json.dumps(response))

    def test_resource_reference_is_advisory_cli_handles_without_running_diagnosis(self):
        client = Mock()
        client.request.side_effect = OSError(PRIVATE)
        worker = Worker(self.config, client=client, config_path=self.worker_path)
        with patch('assistant_mesh.worker.diagnose_runtime', side_effect=AssertionError(PRIVATE)):
            reference = worker.resource_reference()
        access = reference['runtime_access']
        self.assertEqual({'action': 'runtime_diagnose', 'arguments': {}}, access['local_read_tool'])
        self.assertEqual('unavailable', reference['discovery_status'])
        self.assertEqual(str(self.worker_path), access['cli']['doctor'][-1])
        self.assertEqual(str(self.worker_path), access['cli']['probe'][-1])
        self.assertEqual(str(self.worker_path), access['cli']['switch'][4])
        for key in ('automatic_repair', 'task_replayed', 'session_replaced'):
            self.assertFalse(access[key])
        self.assertNotIn(PRIVATE, json.dumps(reference))
        self.assertNotIn(str(self.auth), json.dumps(reference))

    def test_other_mesh_actions_still_enforce_core_tick_without_a_diagnostic_gate(self):
        worker = Worker(self.config, client=Mock())
        worker.tick = Mock(side_effect=OSError(PRIVATE))
        with patch('assistant_mesh.worker.diagnose_runtime', side_effect=AssertionError(PRIVATE)):
            with self.assertRaises(OSError):
                worker.on_tool({'tool': 'mesh', 'callId': 'existing-call',
                                'arguments': {'action': 'children', 'arguments': {}}})
        worker.tick.assert_called_once_with(force=True)


if __name__ == '__main__':
    unittest.main()
