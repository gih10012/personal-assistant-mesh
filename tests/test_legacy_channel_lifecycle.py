"""Actual local Thread/callback lifecycle, with synthetic transport only.

No production receiver, network, SSH, credential, witness or handoff grant.
"""
import math
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from assistant_mesh.channel import Channel, ChannelError, ChannelLoopLifecycle
from assistant_mesh.server import _start_channel
from assistant_mesh.store import Store


class HeldTransport:
    account = {'ilink_user_id': 'synthetic-owner', 'ilink_bot_id': 'synthetic-bot'}

    def __init__(self):
        self.polled, self.sent = threading.Event(), threading.Event()
        self.release_poll, self.release_send = threading.Event(), threading.Event()
        self.calls, self.send_error = [], None

    def poll(self, cursor):
        self.calls.append(('poll', cursor))
        self.polled.set()
        if not self.release_poll.wait(3):
            raise RuntimeError('synthetic test callback deadline')
        return {'msgs': [], 'get_updates_buf': 'synthetic-next-cursor'}

    def send(self, row):
        self.calls.append(('send', row['client_id']))
        self.sent.set()
        if not self.release_send.wait(3):
            raise RuntimeError('synthetic test callback deadline')
        if self.send_error:
            raise ChannelError(self.send_error)
        return {'message_id': 'synthetic-wire-acceptance'}


class LegacyChannelLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'original.db')
        self.transport = HeldTransport()
        self.channel = Channel(self.store, self.transport)
        self.group = None

    def tearDown(self):
        self.transport.release_poll.set()
        self.transport.release_send.set()
        if self.group:
            self.group.stop(2)
        self.tmp.cleanup()

    def start(self):
        self.group = ChannelLoopLifecycle(self.channel).start()
        return self.group

    def row(self, request='synthetic-original-send'):
        with self.store.transaction() as db:
            return dict(db.execute('SELECT * FROM outbox WHERE id=?', (request,)).fetchone())

    def test_server_retains_original_legacy_group_and_actual_callbacks(self):
        self.store.enqueue('synthetic-original-send', 'synthetic body')
        original_client = self.row()['client_id']
        self.store.heartbeat('synthetic-worker', ['agent'])
        task_id = self.store.create_task('unrelated synthetic Leader task', required=['agent'])
        task = self.store.claim('synthetic-worker')
        before = self.store.task_status(task_id)
        api = SimpleNamespace(channel_authority=None, ilink=self.transport, store=self.store)
        self.channel, supervisor = _start_channel(api, {})
        self.group = self.channel.loop_lifecycle
        self.assertIsNone(supervisor)
        self.assertIsNone(self.channel.carrier_fence)
        self.assertTrue(self.transport.polled.wait(1))
        self.assertTrue(self.transport.sent.wait(1))
        current = self.group.snapshot()
        self.assertTrue(current['actual_owned_thread_handles'])
        self.assertEqual({'poll': True, 'send': True}, current['threads_alive'])
        self.assertEqual({'poll', 'send'}, set(current['running_loops']))
        self.assertTrue(all(row['active'] for row in current['loop_calls'].values()))
        self.assertFalse(self.group.stop(0))
        self.assertFalse(self.group.snapshot()['local_stop_closed'])
        self.transport.release_poll.set()
        self.transport.release_send.set()
        self.assertTrue(self.group.stop(2))
        closed = self.group.snapshot()
        self.assertTrue(closed['local_stop_closed'])
        self.assertEqual(['poll', 'send'], closed['loop_returns_observed'])
        self.assertTrue(all(row['admitted'] == row['returned'] == 1
                            for row in closed['loop_calls'].values()))
        self.assertEqual(original_client, self.row()['client_id'])
        self.assertEqual('accepted', self.row()['status'])
        self.assertEqual('synthetic-next-cursor', self.store.get('cursor'))
        self.assertEqual(before, self.store.task_status(task_id))
        self.assertEqual(task['id'], task_id)
        for key in ('external_operation_quiescence_verified',
                    'canonical_channel_state_closed_verified', 'handoff_authorized',
                    'native_features_restricted'):
            self.assertFalse(closed[key])

    def test_stop_waits_for_canonical_callback_return_not_just_transport_return(self):
        self.store.enqueue('synthetic-original-send', 'synthetic body')
        self.transport.release_send.set()
        commit_entered, commit_release = threading.Event(), threading.Event()
        original_finish = self.store.finish_send

        def held_finish(*args):
            commit_entered.set()
            if not commit_release.wait(3):
                raise RuntimeError('synthetic callback commit deadline')
            return original_finish(*args)

        with patch.object(self.store, 'finish_send', side_effect=held_finish):
            try:
                self.start()
                self.assertTrue(commit_entered.wait(1))
                self.assertTrue(self.transport.polled.wait(1))
                self.assertFalse(self.group.stop(0))
                calls = self.group.snapshot()['loop_calls']
                self.assertTrue(calls['send']['active'])
                self.assertEqual(0, calls['send']['returned'])
                commit_release.set()
                self.transport.release_poll.set()
                self.assertTrue(self.group.stop(2))
            finally:
                commit_release.set()
        self.assertTrue(self.group.snapshot()['local_stop_closed'])

    def test_timeout_is_unclosed_then_same_handles_can_be_observed_after_return(self):
        self.start()
        self.assertTrue(self.transport.polled.wait(1))
        original_handles = dict(self.group._threads)
        self.assertFalse(self.group.stop(0.01))
        report = self.group.snapshot()
        self.assertTrue(report['stop_requested'])
        self.assertTrue(report['threads_alive']['poll'])
        self.assertEqual(1, report['loop_calls']['poll']['admitted'])
        self.assertEqual(0, report['loop_calls']['poll']['returned'])
        self.assertFalse(report['local_callbacks_returned'])
        self.transport.release_poll.set()
        self.assertTrue(self.group.stop(2))
        self.assertEqual(original_handles, self.group._threads)
        self.assertEqual(1, len(self.transport.calls))
        with self.assertRaisesRegex(ChannelError, '^channel_loop_lifecycle_already_started$'):
            self.group.start()

    def test_returned_unknown_send_keeps_original_identity_and_no_permission(self):
        self.store.enqueue('synthetic-original-send', 'synthetic body')
        original_client = self.row()['client_id']
        self.transport.send_error = 'network_timeout'
        self.transport.release_send.set()
        self.start()
        self.assertTrue(self.transport.sent.wait(1))
        self.assertTrue(self.transport.polled.wait(1))
        self.assertFalse(self.group.stop(0))
        self.transport.release_poll.set()
        self.assertTrue(self.group.stop(2))
        row = self.row()
        self.assertEqual('unknown', row['status'])
        self.assertEqual(original_client, row['client_id'])
        self.assertFalse(self.channel.send_once())
        self.assertEqual(1, sum(kind == 'send' for kind, value in self.transport.calls))
        self.assertTrue(self.group.snapshot()['local_stop_closed'])
        self.assertFalse(self.group.snapshot()['canonical_channel_state_closed_verified'])
        self.assertFalse(self.group.snapshot()['handoff_authorized'])

    def test_never_started_or_missing_handles_are_not_stop_closure(self):
        self.group = ChannelLoopLifecycle(self.channel)
        self.assertFalse(self.group.snapshot()['owned_threads_stopped'])
        self.assertFalse(self.group.stop(0))
        self.assertFalse(self.group.snapshot()['local_stop_closed'])
        self.assertEqual([], self.transport.calls)
        with self.assertRaises(ChannelError):
            self.group.start()

    def test_new_callback_cannot_be_admitted_after_stop_even_with_direct_loop_call(self):
        self.start()
        self.assertTrue(self.transport.polled.wait(1))
        self.group.stop(0)
        self.transport.release_poll.set()
        self.assertTrue(self.group.stop(2))
        before = self.channel.loop_calls()
        self.channel.run_poll()
        self.channel.run_send()
        self.assertEqual(before, self.channel.loop_calls())
        self.assertEqual(1, len(self.transport.calls))

    def test_rejects_duplicate_group_and_preexisting_channel_loop(self):
        self.start()
        self.assertTrue(self.transport.polled.wait(1))
        with self.assertRaisesRegex(ChannelError, '^channel_loop_lifecycle_unavailable$'):
            ChannelLoopLifecycle(self.channel)
        self.transport.release_poll.set()
        self.assertTrue(self.group.stop(2))
        channel = Channel(self.store, self.transport)
        entered, release = threading.Event(), threading.Event()

        def held():
            entered.set()
            release.wait(2)

        with patch.object(channel, 'poll_once', side_effect=held):
            original = threading.Thread(target=channel.run_poll)
            original.start()
            try:
                self.assertTrue(entered.wait(1))
                group = ChannelLoopLifecycle(channel)
                with self.assertRaisesRegex(ChannelError, '^channel_loop_already_running$'):
                    group.start()
                self.assertFalse(group.snapshot()['actual_owned_thread_handles'])
            finally:
                channel.stop.set()
                release.set()
                original.join(2)
            self.assertFalse(original.is_alive())

    def test_invalid_stop_timeout_does_not_request_stop(self):
        self.group = ChannelLoopLifecycle(self.channel)
        for timeout in (True, False, -1, 61, math.inf, math.nan, '1', None):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.group.stop(timeout)
        self.assertFalse(self.channel.stop.is_set())
        self.assertEqual([], self.transport.calls)

    def test_partial_start_preserves_actual_handles_and_missing_return(self):
        original_thread = threading.Thread

        class FailedSecondStart(original_thread):
            def start(self):
                if self.name.endswith('-send'):
                    raise RuntimeError('synthetic private startup detail')
                return super().start()

        self.group = ChannelLoopLifecycle(self.channel)
        with patch('assistant_mesh.channel.threading.Thread', FailedSecondStart):
            with self.assertRaisesRegex(ChannelError, '^channel_loop_start_failed$'):
                self.group.start()
        self.assertFalse(self.group.stop(2))
        report = self.group.snapshot()
        self.assertTrue(report['start_failed'])
        self.assertFalse(report['actual_owned_thread_handles'])
        self.assertFalse(report['owned_threads_stopped'])
        self.assertFalse(report['local_stop_closed'])
        self.assertNotIn('private', str(report))
        self.assertEqual({'poll', 'send'}, set(self.group._threads))
        with self.assertRaises(ChannelError):
            self.group.start()

    def test_stop_called_from_owned_callback_does_not_join_itself(self):
        entered = threading.Event()
        results = []

        def self_stop():
            entered.set()
            results.append(self.group.stop(0))

        self.group = ChannelLoopLifecycle(self.channel)
        with patch.object(self.channel, 'poll_once', side_effect=self_stop), \
                patch.object(self.channel, 'send_once', return_value=False):
            self.group.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.group.stop(2))
        self.assertEqual([False], results)
        self.assertTrue(self.group.snapshot()['local_stop_closed'])
        self.assertEqual([], self.transport.calls)


if __name__ == '__main__':
    unittest.main()
