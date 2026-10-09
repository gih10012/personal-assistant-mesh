"""Explicit CLI lifecycle/sanitization plus real local HTTP; no deployments."""
import builtins
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from assistant_mesh.cli import main
from assistant_mesh.provider_runtime import ProviderRuntimeError
from assistant_mesh.server import serve
from tests import test_provider_runtime as runtime_fixture


def report(status='ok', diagnostics=None, recoverable=True):
    return {'status': status, 'diagnostics': diagnostics or [], 'recoverable': recoverable,
        'dispatch': [{'result_reference': 'DO_NOT_PRINT', 'scope': {'secret': 'DO_NOT_PRINT'}}],
        'reconciled': [], 'local_unsettled': 0}


class ProviderCLITests(unittest.TestCase):
    def setUp(self):
        self.original_umask = os.umask(0o077)
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-provider-cli.')
        self.root = Path(self.temp.name)
        self.token = self.root / 'token'
        self.token.write_text('not-real-owned-test-token-' + 'x' * 40)
        self.token.chmod(0o600)
        self.config = self.root / 'client.json'
        self.config.write_text(json.dumps({'control_url': 'http://127.0.0.1:1', 'token_file': str(self.token)}))
        self.config.chmod(0o600)
        self.owner = self.root / 'owner.json'
        self.owner.write_text(json.dumps({'schema': 1, 'provider': 'node:fixture', 'authority_id': 'fixture',
            'journal': str(self.root / 'provider.db'), 'adapters': [], 'poll_interval_seconds': 0.01}))
        self.owner.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()
        os.umask(self.original_umask)

    def invoke(self, action='poll', extra=None, argv=None):
        argv = argv or ['mesh', '--config', str(self.config), 'provider', '--owner-config', str(self.owner), '--action', action]
        output, errors, code = io.StringIO(), io.StringIO(), 0
        with patch('sys.argv', argv + (extra or [])), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                main()
            except SystemExit as error:
                code = error.code
        text = output.getvalue() + errors.getvalue()
        self.assertNotIn('DO_NOT_PRINT', text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(self.token.read_text(), text)
        return code, [json.loads(line) for line in output.getvalue().splitlines() if line]

    def test_explicit_poll_safe_summary_omits_results_scopes_and_private_paths(self):
        runtime = Mock()
        runtime.poll_once.return_value = report()
        with patch('assistant_mesh.provider_runtime.ProviderRuntime', return_value=runtime):
            code, lines = self.invoke()
        self.assertEqual(0, code)
        self.assertEqual(1, lines[0]['dispatch_count'])
        self.assertNotIn('dispatch', lines[0])
        self.assertFalse(lines[0]['native_tools_intercepted'])
        self.assertFalse(lines[0]['automatic_invocation_replay'])

    def test_poll_and_finite_serve_degraded_have_actual_exit_two(self):
        degraded = report('degraded', ['authority_unavailable'])
        runtime = Mock()
        runtime.poll_once.return_value = degraded
        runtime.serve.return_value = {'cycles': 1, 'last_report': degraded}
        with patch('assistant_mesh.provider_runtime.ProviderRuntime', return_value=runtime):
            self.assertEqual(2, self.invoke()[0])
            self.assertEqual(2, self.invoke('serve', ['--max-cycles', '1'])[0])

    def test_invalid_arguments_fail_before_runtime_creation_and_redact_unknown_values(self):
        cases = [('poll', ['--max-cycles', '1']), ('serve', ['--max-cycles', '0']),
                 ('serve', ['--max-cycles', '-1']), ('serve', ['--max-cycles', 'DO_NOT_PRINT']),
                 ('DO_NOT_PRINT', []), ('serve', ['--DO_NOT_PRINT', 'DO_NOT_PRINT']),
                 ('poll', ['--payload-file', 'DO_NOT_PRINT'])]
        with patch('assistant_mesh.provider_runtime.ProviderRuntime') as runtime:
            for action, extra in cases:
                with self.subTest(action=action, extra=extra):
                    code, lines = self.invoke(action, extra)
                    self.assertEqual(2, code)
                    self.assertEqual(['provider_arguments_invalid'], lines[0]['diagnostics'])
            runtime.assert_not_called()

    def test_missing_action_owner_or_config_is_not_an_implicit_poll(self):
        base = ['mesh', '--config', str(self.config), 'provider']
        with patch('assistant_mesh.provider_runtime.ProviderRuntime') as runtime:
            for argv in (base, base + ['--action', 'poll'], ['mesh', 'provider', '--action', 'poll']):
                self.assertEqual(2, self.invoke(argv=argv)[0])
            runtime.assert_not_called()

    def test_bad_private_client_config_is_fixed_category_not_exception_or_path(self):
        self.config.chmod(0o644)
        code, lines = self.invoke()
        self.assertEqual(2, code)
        self.assertEqual(['provider_client_config_invalid'], lines[0]['diagnostics'])
        self.config.chmod(0o600)
        self.config.write_text(json.dumps({'control_url': 'https://DO_NOT_PRINT@invalid', 'token_file': str(self.token)}))
        self.assertEqual(2, self.invoke()[0])

    def test_bad_owner_config_and_fatal_runtime_errors_are_sanitized(self):
        self.owner.chmod(0o644)
        code, lines = self.invoke('describe')
        self.assertEqual(2, code)
        self.assertEqual(['owner_config_unavailable'], lines[0]['diagnostics'])
        self.owner.chmod(0o600)
        for error in (ProviderRuntimeError('provider_journal_failed'), ProviderRuntimeError('DO_NOT_PRINT'),
                      RuntimeError('DO_NOT_PRINT'), SystemExit('DO_NOT_PRINT')):
            with self.subTest(kind=type(error).__name__), patch(
                    'assistant_mesh.provider_runtime.ProviderRuntime', side_effect=error):
                code, lines = self.invoke()
                self.assertEqual(2, code)
                self.assertEqual('error', lines[-1]['status'])

    def test_worker_and_node_defaults_never_import_or_start_provider(self):
        original_import = builtins.__import__
        imports = []
        def guarded_import(name, *args, **kwargs):
            if name.endswith('provider_runtime'):
                imports.append(name)
                raise AssertionError('default command imported provider')
            return original_import(name, *args, **kwargs)
        for command, target in (('worker', 'assistant_mesh.worker.Worker'), ('node', 'assistant_mesh.node.run')):
            with self.subTest(command=command), patch(target), patch('builtins.__import__', side_effect=guarded_import):
                self.invoke(argv=['mesh', '--config', str(self.config), command])
        self.assertEqual([], imports)

    def test_signal_stop_is_graceful_preserves_degraded_report_and_restores_handlers(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        runtime = Mock()
        def served(stop, on_report, max_cycles):
            degraded = report('degraded', ['local_outcome_unresolved'])
            on_report(degraded)
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            self.assertTrue(stop.is_set())
            return {'cycles': 1, 'last_report': degraded}
        runtime.serve.side_effect = served
        with patch('assistant_mesh.provider_runtime.ProviderRuntime', return_value=runtime):
            code, lines = self.invoke('serve')
        self.assertEqual(0, code)
        self.assertEqual('degraded', lines[-1]['status'])
        self.assertTrue(lines[-1]['stop_requested'])
        for number, handler in previous.items():
            self.assertIs(handler, signal.getsignal(number))

    def test_fatal_serve_restores_handlers_and_zero_cycle_stop_is_explicit(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        runtime = Mock()
        runtime.serve.side_effect = ProviderRuntimeError('provider_journal_failed')
        with patch('assistant_mesh.provider_runtime.ProviderRuntime', return_value=runtime):
            self.assertEqual(2, self.invoke('serve')[0])
        for number, handler in previous.items():
            self.assertIs(handler, signal.getsignal(number))
        def stopped(stop, on_report, max_cycles):
            stop.set()
            return {'cycles': 0, 'last_report': None}
        runtime.serve.side_effect = stopped
        with patch('assistant_mesh.provider_runtime.ProviderRuntime', return_value=runtime):
            code, lines = self.invoke('serve')
        self.assertEqual(0, code)
        self.assertEqual('stopped', lines[-1]['status'])
        self.assertEqual(0, lines[-1]['cycles'])

    def test_non_main_thread_never_sets_process_signal_handlers(self):
        runtime = Mock()
        runtime.serve.return_value = {'cycles': 1, 'last_report': report()}
        results = []
        with patch('assistant_mesh.provider_runtime.ProviderRuntime', return_value=runtime), patch('signal.signal') as changed:
            thread = threading.Thread(target=lambda: results.append(self.invoke('serve', ['--max-cycles', '1'])))
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            changed.assert_not_called()
        self.assertEqual(0, results[0][0])

    def test_changed_summary_only_stream_is_flushed_and_unknown_diagnostic_is_redacted(self):
        runtime = Mock()
        def served(stop, on_report, max_cycles):
            value = report('degraded', ['DO_NOT_PRINT'])
            on_report(value)
            on_report(value)
            return {'cycles': 2, 'last_report': value}
        runtime.serve.side_effect = served
        with patch('assistant_mesh.provider_runtime.ProviderRuntime', return_value=runtime):
            code, lines = self.invoke('serve', ['--max-cycles', '2'])
        self.assertEqual(2, code)
        self.assertEqual(2, len(lines))  # one changed stream line + final cycles
        self.assertEqual(['provider_diagnostic_unknown'], lines[0]['diagnostics'])

    @contextlib.contextmanager
    def actual_http(self):
        fixture = runtime_fixture.ProviderRuntimeTests('test_real_private_module_executes_only_after_admission_and_preserves_arbitrary_context')
        fixture.setUp()
        ready, holder = threading.Event(), {}
        token = fixture.root / 'control.token'
        token.write_text('owned-local-test-not-a-real-key-' + 'x' * 40)
        token.chmod(0o600)
        cfg = dict(fixture.api.config, port=0, peers=[{'role': 'worker', 'node': 'cloud', 'token_file': str(token)}])
        def on_ready(server, api, channel):
            api.store.clock = lambda: 1000.0
            holder['server'] = server
            ready.set()
        thread = threading.Thread(target=serve, args=(cfg, on_ready), daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            client = fixture.root / 'client.json'
            client.write_text(json.dumps({'control_url': 'http://127.0.0.1:' + str(holder['server'].server_address[1]),
                                          'token_file': str(token)}))
            client.chmod(0o600)
            yield fixture, client
        finally:
            if 'server' in holder:
                holder['server'].shutdown()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            fixture.tearDown()

    def test_real_describe_initializes_journal_but_does_not_execute_or_advertise(self):
        with self.actual_http() as (fixture, client):
            new_journal = fixture.root / 'fresh-journal.db'
            fixture.manifest['journal'] = str(new_journal)
            fixture.write_config()
            code, lines = self.invoke(argv=['mesh', '--config', str(client), 'provider',
                '--owner-config', str(fixture.config_path), '--action', 'describe'])
            self.assertEqual(0, code)
            self.assertTrue(new_journal.exists())
            self.assertFalse(fixture.import_marker.exists())
            self.assertTrue(lines[0]['journal_initialized'])
            self.assertFalse(lines[0]['advertised'])
            self.assertFalse(lines[0]['module_execution_started'])
            self.assertEqual(1, len(fixture.api.resources.discover()['capabilities']))

    def test_real_http_poll_executes_installed_module_and_subprocess_exit_two_is_real(self):
        with self.actual_http() as (fixture, client):
            fixture.reserve()
            code, lines = self.invoke(argv=['mesh', '--config', str(client), 'provider',
                '--owner-config', str(fixture.config_path), '--action', 'poll'])
            self.assertEqual(0, code)
            self.assertEqual(['v1'], fixture.calls())
            self.assertEqual(1, lines[0]['dispatch_count'])
        self.config.write_text(json.dumps({'control_url': 'http://127.0.0.1',
            'unix_socket': str(self.root / 'missing-authority.sock'), 'token_file': str(self.token)}))
        child = subprocess.run([sys.executable, '-m', 'assistant_mesh', '--config', str(self.config), 'provider',
            '--owner-config', str(self.owner), '--action', 'poll'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10)
        self.assertEqual(2, child.returncode)
        value = json.loads(child.stdout.decode())
        self.assertEqual(['authority_unavailable'], value['diagnostics'])
        self.assertTrue(value['recoverable'])
        self.assertEqual(b'', child.stderr)

    def test_service_example_is_optional_and_does_not_add_native_restrictions(self):
        source = (Path(__file__).resolve().parents[1] / 'deploy/assistant-mesh-provider.service.example').read_text()
        active = [line for line in source.splitlines() if line and not line.startswith('#')]
        self.assertIn('RestartPreventExitStatus=2', active)
        self.assertTrue(any('provider --owner-config' in line and '--action serve' in line for line in active))
        self.assertFalse(any(line.startswith(('Requires=', 'PartOf=', 'BindsTo=', 'NoNewPrivileges=',
                                             'PrivateTmp=', 'IPAddressDeny=', 'RestrictAddressFamilies=')) for line in active))


if __name__ == '__main__':
    unittest.main()
