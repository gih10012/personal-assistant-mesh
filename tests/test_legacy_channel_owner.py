"""Actual owner IPC/thread boundaries; all transport/account data synthetic."""
import contextlib
import io
import json
import math
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from assistant_mesh import legacy_channel_owner as owner
from assistant_mesh import server
from assistant_mesh.channel import Channel, ChannelError, ChannelLoopLifecycle
from assistant_mesh.store import Store


class HeldTransport:
    account = {'ilink_user_id': 'synthetic-owner', 'ilink_bot_id': 'synthetic-bot',
               'bot_token': 'DO_NOT_PUBLISH_SYNTHETIC_TOKEN'}

    def __init__(self):
        self.polled, self.sent = threading.Event(), threading.Event()
        self.release_poll, self.release_send = threading.Event(), threading.Event()
        self.calls, self.send_error = [], None

    def poll(self, cursor):
        self.calls.append(('poll', cursor))
        self.polled.set()
        if not self.release_poll.wait(5):
            raise RuntimeError('synthetic callback deadline')
        return {'msgs': [], 'get_updates_buf': 'synthetic-next-cursor'}

    def send(self, row):
        self.calls.append(('send', row['client_id']))
        self.sent.set()
        if not self.release_send.wait(5):
            raise RuntimeError('synthetic callback deadline')
        if self.send_error:
            raise ChannelError(self.send_error)
        return {'message_id': 'synthetic-wire-acceptance'}


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(socket, 'SO_PEERCRED'),
                     'Linux owned Unix IPC deployment example')
class LegacyChannelOwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mesh-owner-')
        self.root = Path(self.tmp.name)
        self.path = self.root / 'owner.sock'
        self.store = Store(self.root / 'fixture.sqlite')
        self.transport = HeldTransport()
        self.channel = Channel(self.store, self.transport)
        self.group = self.control = None

    def tearDown(self):
        self.transport.release_poll.set()
        self.transport.release_send.set()
        if self.control:
            self.control.close()
        if self.group:
            self.group.stop(2)
        self.tmp.cleanup()

    def start(self):
        self.group = ChannelLoopLifecycle(self.channel).start()
        self.assertTrue(self.transport.polled.wait(1))
        self.control = owner.LegacyChannelOwnerControl(self.path)
        self.control.attach(object(), SimpleNamespace(channel_authority=None), self.channel)
        return self.status()

    def status(self, **kwargs):
        return owner.request_owner(self.path, expected_pid=os.getpid(), **kwargs)

    def stop(self, current=None, timeout=0):
        current = self.status() if current is None else current
        return owner.request_owner(self.path, 'stop', instance_id=current['instance_id'],
                                   expected_pid=os.getpid(), timeout=timeout)

    def raw(self, value):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(self.path))
            connection.sendall(value)
            raw = bytearray()
            while b'\n' not in raw:
                part = connection.recv(1024)
                if not part:
                    break
                raw.extend(part)
            return json.loads(raw)

    def test_actual_socket_owner_peer_identity_and_complete_counter_snapshot(self):
        report = self.start()
        self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))
        self.assertTrue(stat.S_ISSOCK(self.path.stat().st_mode))
        self.assertEqual(os.getpid(), report['owner_pid'])
        self.assertEqual(os.geteuid(), report['owner_uid'])
        self.assertEqual(32, len(report['instance_id']))
        self.assertIs(self.group, self.control._group)
        self.assertEqual(self.group.snapshot(), report['lifecycle'])
        self.assertEqual({'poll', 'send'}, set(report['lifecycle']['loop_calls']))
        self.assertTrue(report['lifecycle']['actual_owned_thread_handles'])
        rendered = json.dumps(report)
        self.assertNotIn('DO_NOT_PUBLISH_SYNTHETIC_TOKEN', rendered)
        self.assertNotIn('synthetic-owner', rendered)
        self.assertNotIn(str(self.store.path), rendered)
        for claim in ('handoff_authorized', 'canonical_channel_state_closed_verified',
                      'external_operation_quiescence_verified', 'native_features_restricted'):
            self.assertFalse(report['lifecycle'][claim])

    def test_zero_timeout_keeps_same_handles_then_observes_actual_returns(self):
        original = self.start()
        handles = dict(self.group._threads)
        stopped = self.stop(original)
        self.assertTrue(stopped['lifecycle']['stop_requested'])
        self.assertFalse(stopped['lifecycle']['local_stop_closed'])
        self.assertTrue(stopped['lifecycle']['loop_calls']['poll']['active'])
        self.transport.release_poll.set()
        self.transport.release_send.set()
        ended = self.stop(original, 2)
        self.assertTrue(ended['lifecycle']['local_stop_closed'])
        self.assertEqual(handles, self.group._threads)
        self.assertEqual(original['instance_id'], ended['instance_id'])
        self.assertEqual(['poll', 'send'], ended['lifecycle']['loop_returns_observed'])
        self.assertTrue(all(row['admitted'] == row['returned'] and not row['active']
                            for row in ended['lifecycle']['loop_calls'].values()))
        self.assertEqual(1, sum(kind == 'poll' for kind, value in self.transport.calls))
        self.assertFalse(ended['lifecycle']['handoff_authorized'])

    def test_returned_unknown_send_is_not_replayed_or_used_as_permission(self):
        self.store.enqueue('synthetic-send', 'DO_NOT_PUBLISH_SYNTHETIC_BODY')
        self.transport.send_error = 'network_timeout'
        report = self.start()
        self.assertTrue(self.transport.sent.wait(1))
        with self.store.transaction() as db:
            before = dict(db.execute('SELECT * FROM outbox WHERE id=?', ('synthetic-send',)).fetchone())
        self.stop(report)
        self.transport.release_poll.set()
        self.transport.release_send.set()
        ended = self.stop(report, 2)
        with self.store.transaction() as db:
            after = dict(db.execute('SELECT * FROM outbox WHERE id=?', ('synthetic-send',)).fetchone())
        self.assertEqual(before['client_id'], after['client_id'])
        self.assertEqual('unknown', after['status'])
        self.assertEqual(1, sum(kind == 'send' for kind, value in self.transport.calls))
        self.assertTrue(ended['lifecycle']['local_stop_closed'])
        self.assertFalse(ended['lifecycle']['canonical_channel_state_closed_verified'])
        self.assertFalse(ended['lifecycle']['handoff_authorized'])
        self.assertNotIn('DO_NOT_PUBLISH_SYNTHETIC_BODY', json.dumps(ended))

    def test_stop_waits_for_entire_finish_send_not_transport_return(self):
        self.store.enqueue('synthetic-send', 'synthetic body')
        entered, release = threading.Event(), threading.Event()
        original = self.store.finish_send

        def held_finish(*args):
            entered.set()
            if not release.wait(4):
                raise RuntimeError('synthetic commit deadline')
            return original(*args)

        with patch.object(self.store, 'finish_send', side_effect=held_finish):
            try:
                self.transport.release_send.set()
                report = self.start()
                self.assertTrue(entered.wait(1))
                unclosed = self.stop(report)
                self.assertTrue(unclosed['lifecycle']['loop_calls']['send']['active'])
                self.assertEqual(0, unclosed['lifecycle']['loop_calls']['send']['returned'])
                self.assertFalse(unclosed['lifecycle']['local_stop_closed'])
                self.transport.release_poll.set()
                release.set()
                self.assertTrue(self.stop(report, 2)['lifecycle']['local_stop_closed'])
            finally:
                release.set()

    def test_stop_rejects_stale_instance_without_setting_channel_stop(self):
        self.start()
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_request_rejected'):
            owner.request_owner(self.path, 'stop', instance_id='0' * 32, timeout=0)
        self.assertFalse(self.channel.stop.is_set())

    def test_client_verifies_actual_peer_pid_before_sending_stop(self):
        report = self.start()
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_socket_peer_mismatch'):
            owner.request_owner(self.path, 'stop', instance_id=report['instance_id'], timeout=0,
                                expected_pid=os.getpid() + 1)
        self.assertFalse(self.channel.stop.is_set())

    def test_peer_uid_is_checked_before_read_or_execute(self):
        self.start()
        with patch.object(owner, '_credentials', return_value=(os.getpid(), os.geteuid() + 1)):
            response = self.raw(json.dumps({'protocol': owner.PROTOCOL, 'action': 'status'}).encode() + b'\n')
        self.assertEqual('owner_peer_not_authorized', response['error'])
        self.assertNotIn('lifecycle', response)
        self.assertFalse(self.channel.stop.is_set())

    def test_unknown_actions_extra_fields_and_invalid_frames_have_no_effect(self):
        self.start()
        cases = [b'[]\n', b'NaN\n', b'{}\n{}\n', b'x' * 4096 + b'\n']
        for action in ('start', 'reset', 'grant', 'freeze', 'load_module', 'shutdown'):
            cases.append(json.dumps({'protocol': owner.PROTOCOL, 'action': action}).encode() + b'\n')
        cases.append(json.dumps({'protocol': owner.PROTOCOL, 'action': 'status',
                                 'module': 'synthetic'}).encode() + b'\n')
        for case in cases:
            with self.subTest(case=case[:32]):
                self.assertFalse(self.raw(case)['ok'])
                self.assertFalse(self.channel.stop.is_set())

    def test_stop_requires_instance_and_rejects_nonfinite_or_unbounded_timeouts(self):
        report = self.start()
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_instance_required'):
            owner.request_owner(self.path, 'stop', timeout=0)
        for timeout in (True, -1, 60.1, math.nan, math.inf, '5', 10 ** 1000):
            with self.subTest(timeout=type(timeout).__name__):
                with self.assertRaisesRegex(owner.OwnerControlError, 'owner_stop_timeout_invalid'):
                    self.stop(report, timeout)
        self.assertFalse(self.channel.stop.is_set())

    def test_close_private_ipc_does_not_stop_or_replace_group(self):
        self.start()
        original = self.group
        self.control.close()
        self.assertIs(original, self.channel.loop_lifecycle)
        self.assertFalse(self.channel.stop.is_set())
        self.assertTrue(self.group.snapshot()['threads_alive']['poll'])
        self.assertFalse(self.path.exists())
        self.control.close()

    def test_existing_socket_or_file_is_never_unlinked_or_replaced(self):
        self.start()
        original = self.path.stat().st_ino
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_socket_unavailable'):
            owner.LegacyChannelOwnerControl(self.path)
        self.assertEqual(original, self.path.stat().st_ino)
        other = self.root / 'existing'
        other.write_bytes(b'synthetic original')
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_socket_unavailable'):
            owner.LegacyChannelOwnerControl(other)
        self.assertEqual(b'synthetic original', other.read_bytes())

    def test_close_keeps_replacement_at_same_socket_path(self):
        self.start()
        self.path.unlink()
        self.path.write_bytes(b'synthetic replacement')
        self.control.close()
        self.assertEqual(b'synthetic replacement', self.path.read_bytes())

    def test_parent_must_be_real_owned_private_directory(self):
        other = self.root / 'other'
        other.mkdir(mode=0o700)
        os.chmod(str(other), 0o755)
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_socket_parent_invalid'):
            owner.LegacyChannelOwnerControl(other / 'control.sock')
        link = self.root / 'alias'
        link.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_socket_parent_invalid'):
            owner.LegacyChannelOwnerControl(link / 'control.sock')
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_socket_path_invalid'):
            owner.LegacyChannelOwnerControl('relative.sock')

    def test_control_refuses_missing_unstarted_fenced_or_supervised_group(self):
        self.control = owner.LegacyChannelOwnerControl(self.path)
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_legacy_group_unavailable'):
            self.control.attach(None, SimpleNamespace(), self.channel)
        self.group = ChannelLoopLifecycle(self.channel)
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_legacy_group_unavailable'):
            self.control.attach(None, SimpleNamespace(), self.channel)
        self.group.start()
        self.assertTrue(self.transport.polled.wait(1))
        for marker in ('carrier_fence', '_supervisor'):
            with patch.object(self.channel, marker, object()):
                with self.assertRaisesRegex(owner.OwnerControlError, 'owner_legacy_group_unavailable'):
                    self.control.attach(None, SimpleNamespace(), self.channel)
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_legacy_group_unavailable'):
            self.control.attach(None, SimpleNamespace(channel_authority=object()), self.channel)
        self.control.attach(None, SimpleNamespace(), self.channel)
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_control_already_attached_or_closed'):
            self.control.attach(None, SimpleNamespace(), self.channel)

    def test_socket_mode_change_refused_by_client(self):
        self.start()
        os.chmod(str(self.path), 0o644)
        with self.assertRaisesRegex(owner.OwnerControlError, 'owner_socket_invalid'):
            self.status()
        self.assertFalse(self.channel.stop.is_set())

    def test_real_server_carrier_stop_keeps_http_and_unrelated_native_rows(self):
        self.store.enqueue('synthetic-inflight', 'synthetic body')
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET status='submitting' WHERE id='synthetic-inflight'")
            db.execute("INSERT INTO steering VALUES('synthetic-steer','synthetic-task',1,'synthetic text','submitting')")
        self.store.heartbeat('synthetic-worker', ['agent'])
        task_id = self.store.create_task('unrelated synthetic Leader task', required=['agent'])
        self.store.claim('synthetic-worker')
        before = self.store.task_status(task_id)
        original_store = server.Store
        ready = threading.Event()
        observed, failure = {}, []
        config = {'database': self.store.path, 'ilink_account': 'synthetic-unused-private-ref', 'port': 0}
        original_attach = owner.LegacyChannelOwnerControl.attach

        def make_api(config):
            observed['store_factory_scoped'] = server.Store is not original_store
            return SimpleNamespace(store=server.Store(config['database']),
                                   ilink=self.transport, channel_authority=None)

        def capture(control, http_server, api, channel):
            original_attach(control, http_server, api, channel)
            observed.update(server=http_server, channel=channel,
                            factory_restored_before_http=server.Store is original_store)
            ready.set()

        def run():
            try:
                owner.serve_with_owner_control(config, self.path)
            except BaseException as error:
                failure.append(type(error).__name__)

        with patch.object(server, 'API', side_effect=make_api), \
                patch.object(owner.LegacyChannelOwnerControl, 'attach', capture):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(ready.wait(2))
                self.assertTrue(self.transport.polled.wait(1))
                current = self.status()
                stopped = self.stop(current)
                self.assertFalse(stopped['lifecycle']['local_stop_closed'])
                self.transport.release_poll.set()
                self.transport.release_send.set()
                self.assertTrue(self.stop(current, 2)['lifecycle']['local_stop_closed'])
                address = observed['server'].server_address
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open('http://127.0.0.1:%d/healthz' % address[1], timeout=2) as response:
                    self.assertEqual({'ok': True, 'service': 'assistant-mesh'}, json.loads(response.read()))
                self.assertTrue(thread.is_alive())
                self.assertTrue(observed['store_factory_scoped'])
                self.assertTrue(observed['factory_restored_before_http'])
                self.assertEqual(before, self.store.task_status(task_id))
                with self.store.transaction() as db:
                    self.assertEqual('submitting', db.execute("SELECT status FROM outbox WHERE id='synthetic-inflight'").fetchone()[0])
                    self.assertEqual('submitting', db.execute("SELECT state FROM steering WHERE id='synthetic-steer'").fetchone()[0])
            finally:
                self.transport.release_poll.set()
                self.transport.release_send.set()
                if observed.get('server'):
                    observed['server'].shutdown()  # Test owner, never IPC action.
                thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], failure)
        self.assertIs(original_store, server.Store)
        self.assertFalse(self.path.exists())

    def test_factory_restored_on_constructor_failure_and_no_socket_leak(self):
        original = server.Store
        config = {'ilink_account': 'synthetic-unused-private-ref'}
        with patch.object(server, 'serve', side_effect=RuntimeError('DO_NOT_PUBLISH_SYNTHETIC_FAILURE')):
            with self.assertRaises(RuntimeError):
                owner.serve_with_owner_control(config, self.path)
        self.assertIs(original, server.Store)
        self.assertFalse(self.path.exists())
        self.assertFalse(owner._SERVE_LOCK.locked())

    def test_wrapper_requires_legacy_config_and_no_paid_or_fenced_expansion(self):
        cases = [None, {}, {'ilink_account': 'synthetic', 'channel_carrier': {}},
                 {'ilink_account': 'synthetic', 'notification_policy': {'mode': 'private'}},
                 {'ilink_account': 'synthetic', 'notification_policy': 'invalid'}]
        for config in cases:
            with self.subTest(config=config):
                with patch.object(server, 'serve') as serve:
                    with self.assertRaisesRegex(owner.OwnerControlError, 'owner_legacy_config_required'):
                        owner.serve_with_owner_control(config, self.path)
                    serve.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_cli_failure_never_prints_private_config_or_exception(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(owner, 'private_json', side_effect=ValueError('DO_NOT_PUBLISH_PRIVATE_VALUE')), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = owner.main(['--socket', str(self.path), 'serve', '--config', 'DO_NOT_PUBLISH_CONFIG_REF'])
        self.assertEqual(2, result)
        self.assertEqual('', output.getvalue())
        self.assertEqual({'ok': False, 'error': 'owner_control_unconfirmed'}, json.loads(errors.getvalue()))

    def test_other_platform_refused_not_replaced_with_unverified_tcp(self):
        with patch.object(owner.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(owner.OwnerControlError, 'owner_peer_credentials_unsupported'):
                owner.LegacyChannelOwnerControl(self.path)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
