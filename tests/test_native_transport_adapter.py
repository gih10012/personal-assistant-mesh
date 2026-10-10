"""Adapter integration fixtures; no real app-server, account or model."""
import os
import queue
import signal
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.codex import Codex, CodexError
from assistant_mesh.native_ws import NativeSocketError


class NativeTransportAdapterTests(unittest.TestCase):
    def runtime(self):
        agent = Codex.__new__(Codex)
        agent.transport_name = 'unix'
        agent._native_socket = mock.Mock()
        agent._socket_path = agent._socket_directory = None
        agent._native_cleanup_forced = agent._reader_forced_close = agent._term_sent = False
        agent.events, agent.deferred = queue.Queue(), []
        agent.reader = mock.Mock()
        agent.reader.is_alive.return_value = False
        agent.process = mock.Mock(pid=424242)
        agent.process.poll.return_value = 0
        return agent

    def test_optin_launches_private_unix_without_pipes_or_native_permission_changes(self):
        child = mock.Mock(pid=424242)
        child.poll.return_value = 0
        with mock.patch('assistant_mesh.codex.discover_codex_auth', return_value='/fixture/auth'), \
                mock.patch('assistant_mesh.codex.subprocess.Popen', return_value=child) as spawn, \
                mock.patch.object(Codex, '_connect_native_socket'), \
                mock.patch.object(Codex, '_read'), mock.patch.object(Codex, 'rpc', return_value={}), \
                mock.patch.object(Codex, 'send'):
            agent = Codex({'native_transport': 'unix'})
            try:
                command = spawn.call_args[0][0]
                options = spawn.call_args[1]
                self.assertEqual(['app-server', '--listen', 'unix://' + agent._socket_path], command[-3:])
                self.assertEqual(0o700, os.stat(agent._socket_directory).st_mode & 0o777)
                self.assertEqual(subprocess.DEVNULL, options['stdin'])
                self.assertEqual(subprocess.DEVNULL, options['stdout'])
                self.assertEqual('/fixture/auth', options['env']['CODEX_HOME'])
                self.assertFalse(any('sandbox' in part or 'goal' in part for part in command))
            finally:
                agent.close()

    def test_invalid_transport_fails_before_auth_discovery_or_spawn(self):
        for value in (None, '', 'tcp', True, [], {}):
            with self.subTest(value=value), \
                    mock.patch('assistant_mesh.codex.discover_codex_auth') as auth, \
                    mock.patch('assistant_mesh.codex.subprocess.Popen') as spawn:
                with self.assertRaisesRegex(CodexError, '^invalid_native_transport$'):
                    Codex({'native_transport': value})
                auth.assert_not_called()
                spawn.assert_not_called()

    def test_unsupported_platform_has_no_silent_transport_or_auth_fallback(self):
        with mock.patch('assistant_mesh.codex.os.geteuid', None), \
                mock.patch('assistant_mesh.codex.socket') as sockets, \
                mock.patch('assistant_mesh.codex.discover_codex_auth') as auth:
            del sockets.AF_UNIX
            with self.assertRaisesRegex(CodexError, 'codex_native_transport_unsupported'):
                Codex({'native_transport': 'unix'})
            auth.assert_not_called()

    def test_spawn_failure_remains_original_error_and_removes_empty_temp_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / 'private')
            os.mkdir(target, 0o700)
            with mock.patch('assistant_mesh.codex.tempfile.mkdtemp', return_value=target), \
                    mock.patch('assistant_mesh.codex.discover_codex_auth', return_value='/fixture/auth'), \
                    mock.patch('assistant_mesh.codex.subprocess.Popen', side_effect=OSError('fixture missing')):
                with self.assertRaisesRegex(OSError, '^fixture missing$'):
                    Codex({'native_transport': 'unix'})
            self.assertFalse(os.path.exists(target))

    def test_reader_timeout_preserves_subsequent_message_and_emits_eof(self):
        agent = self.runtime()
        value = {'method': 'turn/started', 'params': {'fixture': True}}
        agent._native_socket.recv_json.side_effect = [socket.timeout(), value, EOFError()]
        agent._read()
        self.assertEqual(value, agent.events.get_nowait())
        self.assertEqual({'eof': True}, agent.events.get_nowait())
        self.assertTrue(agent.events.empty())

    def test_reader_protocol_error_is_fixed_not_private_exception_text(self):
        agent = self.runtime()
        agent._native_socket.recv_json.side_effect = NativeSocketError('PRIVATE_FIXTURE_DO_NOT_EXPOSE')
        agent._read()
        with self.assertRaisesRegex(CodexError, '^codex_native_transport_read_failed$'):
            agent.event(timeout=.1)
        self.assertEqual({'eof': True}, agent.events.get_nowait())

    def test_write_failure_is_unknown_without_automatic_retry(self):
        agent = self.runtime()
        agent._native_socket.send_json.side_effect = NativeSocketError('PRIVATE_FIXTURE_DO_NOT_EXPOSE')
        with self.assertRaisesRegex(CodexError, '^codex_native_transport_write_unknown$'):
            agent.send({'id': 1, 'method': 'turn/steer'})
        agent._native_socket.send_json.assert_called_once()

    def test_only_owned_pid_signaled_once_and_socket_closed_after_reader_drain(self):
        agent = self.runtime()
        agent.process.poll.return_value = None
        agent.process.wait.side_effect = lambda **kwargs: setattr(agent.process.poll, 'return_value', 0)
        with mock.patch('assistant_mesh.codex.os.kill') as kill, mock.patch('assistant_mesh.codex.os.killpg') as group:
            agent.close()
            agent.close()
        kill.assert_called_once_with(424242, signal.SIGTERM)
        group.assert_not_called()
        self.assertFalse(agent._native_cleanup_forced)
        self.assertFalse(agent._reader_forced_close)

    def test_forced_cleanup_retains_unknown_and_never_signals_process_group(self):
        agent = self.runtime()
        agent.process.poll.return_value = None
        calls = []
        def wait(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired('fixture', 5)
            agent.process.poll.return_value = -9
        agent.process.wait.side_effect = wait
        with mock.patch('assistant_mesh.codex.os.kill') as kill, mock.patch('assistant_mesh.codex.os.killpg') as group:
            agent.close()
        self.assertEqual([mock.call(424242, signal.SIGTERM), mock.call(424242, signal.SIGKILL)], kill.call_args_list)
        group.assert_not_called()
        self.assertTrue(agent._native_cleanup_forced)

    def test_terminal_seal_rejects_nonzero_forced_or_reader_forced_close(self):
        for code, forced, reader_forced in ((7, False, False), (0, True, False), (0, False, True)):
            with self.subTest(code=code, forced=forced, reader_forced=reader_forced):
                agent = self.runtime()
                agent.close = mock.Mock()
                agent.process.poll.return_value = code
                agent._native_cleanup_forced, agent._reader_forced_close = forced, reader_forced
                agent.native_lifecycle = {'goal_managed': True, 'quiescent': True, 'settled': False}
                with self.assertRaisesRegex(CodexError, '^codex_native_runtime_exit_unknown$'):
                    agent.seal_native_lifecycle()
                self.assertFalse(agent.native_lifecycle['settled'])

    def test_socket_path_must_be_owned_socket_not_regular_file(self):
        agent = self.runtime()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'not-socket'
            path.touch(mode=0o600)
            agent._socket_path = str(path)
            with self.assertRaisesRegex(CodexError, '^codex_native_transport_path_unsafe$'):
                agent._connect_native_socket()
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_dead_runtime_is_not_retried_with_new_socket_or_account(self):
        agent = self.runtime()
        agent._socket_path = '/fixture-unavailable-socket'
        with self.assertRaisesRegex(CodexError, '^codex_native_transport_start_failed$'):
            agent._connect_native_socket()

    def test_truncated_handshake_is_fixed_backend_failure_not_uncaught_eof(self):
        agent = self.runtime()
        with tempfile.TemporaryDirectory() as directory:
            agent._socket_path = str(Path(directory) / 'socket')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(agent._socket_path)
            with mock.patch('assistant_mesh.codex.UnixWebSocket', side_effect=EOFError('PRIVATE_FIXTURE')):
                with self.assertRaisesRegex(CodexError, '^codex_native_transport_connect_failed$'):
                    agent._connect_native_socket()


class WorkerChildGoalTests(unittest.TestCase):
    def configuration(self, task):
        from assistant_mesh.worker import Worker
        backend = mock.Mock()
        backend.return_value.account.return_value = {'authenticated': True}
        config = {'node_id': 'fixture-node', 'codex': {'goal': {'objective': 'Node Leader only'},
                  'native_transport': 'unix', 'sandbox': 'danger-full-access', 'approval_policy': 'never'}}
        worker = Worker(config, client=mock.Mock(), backend=backend)
        worker.current = task
        worker.open_backend()
        self.assertEqual({'objective': 'Node Leader only'}, config['codex']['goal'])
        result = backend.call_args[0][0]
        self.assertEqual('danger-full-access', result['sandbox'])
        self.assertEqual('never', result['approval_policy'])
        self.assertEqual('unix', result['native_transport'])
        return result

    def test_local_specialist_ignores_node_default_and_legacy_inherited_goal(self):
        config = self.configuration({'parent_id': 'original-parent', 'context': {
            'goal': {'objective': 'Accidentally inherited parent goal'}}})
        self.assertNotIn('goal', config)

    def test_remote_child_without_explicit_goal_does_not_inherit_node_default(self):
        config = self.configuration({'context': {'origin': {'kind': 'a2a', 'parent_ref': 'original-child'}}})
        self.assertNotIn('goal', config)

    def test_remote_child_explicit_a2a_task_goal_is_preserved(self):
        goal = {'objective': 'Explicit permitted peer task'}
        config = self.configuration({'context': {'origin': {'kind': 'a2a', 'parent_ref': 'original-child'},
                                                 'goal': goal}})
        self.assertEqual(goal, config['goal'])

    def test_owner_leader_node_default_is_unchanged(self):
        self.assertEqual({'objective': 'Node Leader only'}, self.configuration({'context': {}})['goal'])
