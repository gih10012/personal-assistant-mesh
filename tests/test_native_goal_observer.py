"""Read-only observation contracts; no native account/model/live validation."""
import copy
import queue
import signal
import time
import unittest
from unittest import mock

from assistant_mesh.codex import Codex, CodexError
from assistant_mesh.native_goal_observer import NativeGoalObserver, validate_goal_continuity


def fixture_goal(**changes):
    value = {'threadId': 'original-thread', 'objective': 'Original owner goal',
             'status': 'active', 'createdAt': 10, 'updatedAt': 20,
             'tokensUsed': 50, 'timeUsedSeconds': 10, 'tokenBudget': None}
    value.update(changes)
    return value


class GoalContinuityTests(unittest.TestCase):
    def test_same_goal_counters_can_grow_and_natural_terminal_is_not_reset(self):
        baseline = fixture_goal()
        current = fixture_goal(status='complete', tokensUsed=70, updatedAt=21)
        self.assertEqual(current, validate_goal_continuity(baseline, current, 'original-thread'))
        self.assertEqual('active', baseline['status'])

    def test_identity_budget_or_counter_replacement_is_unknown(self):
        for change in ({'threadId': 'other'}, {'objective': 'replacement'}, {'createdAt': 11},
                       {'tokenBudget': 100}, {'tokensUsed': 49}, {'timeUsedSeconds': 9},
                       {'updatedAt': 19}, {'status': 'invented'}, {'status': []}, {'status': {}},
                       {'tokensUsed': True},
                       {'tokensUsed': float('inf')}, {'timeUsedSeconds': -1}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(CodexError, 'continuity_unverified'):
                    validate_goal_continuity(fixture_goal(), fixture_goal(**change), 'original-thread')

    def test_missing_actual_baseline_never_reconstructed(self):
        for baseline in (None, {}, dict(fixture_goal(), createdAt=None)):
            with self.assertRaisesRegex(CodexError, 'continuity_unverified'):
                validate_goal_continuity(baseline, fixture_goal(), 'original-thread')


class NativeObserverTests(unittest.TestCase):
    path = '/owned/selected.jsonl'

    def observer(self):
        observer = NativeGoalObserver.__new__(NativeGoalObserver)
        observer.observed_thread_id, observer.observer_tick = 'original-thread', mock.Mock()
        observer.observer_deadline = time.monotonic() + 3
        observer.events, observer.deferred = queue.Queue(), []
        observer._rpc_pending, observer._term_sent = set(), False
        observer._socket_owner_verified = True
        observer._native_cleanup_forced = observer._reader_forced_close = False
        observer.reader = mock.Mock()
        observer.reader.is_alive.return_value = False
        observer.process = mock.Mock(pid=777)
        observer.transport_name, observer.native_drain_active = 'unix', False
        observer._native_socket = mock.Mock()
        return observer

    def turn(self, identity='one', **values):
        return dict({'id': identity, 'items': [], 'itemsView': 'full', 'status': 'completed',
                     'error': None}, **values)

    def test_readonly_rpc_methods_never_send_start_resume_or_goal_writes(self):
        observer = self.observer()
        for method in ('thread/resume', 'thread/start', 'turn/start', 'thread/fork',
                       'thread/goal/set', 'thread/goal/clear', 'turn/steer', 'turn/interrupt'):
            with self.subTest(method=method):
                with self.assertRaisesRegex(CodexError, 'observer_rpc_blocked'):
                    observer.rpc(method, {'threadId': 'original-thread'})
        observer._native_socket.send_json.assert_not_called()
        with self.assertRaisesRegex(CodexError, 'observer_rpc_blocked'):
            observer.rpc('thread/goal/get', {'threadId': 'other-thread'})

    def test_no_host_callback_or_response_for_unexpected_request(self):
        observer = self.observer()
        observer.events.put({'id': 'request', 'method': 'item/tool/call',
                             'params': {'threadId': 'original-thread'}})
        with self.assertRaisesRegex(CodexError, 'observer_unexpected_work'):
            observer.event(.1)
        observer._native_socket.send_json.assert_not_called()

    def test_new_work_notification_is_unknown_even_for_another_thread(self):
        observer = self.observer()
        for method in ('turn/started', 'item/started', 'thread/started'):
            with self.assertRaisesRegex(CodexError, 'observer_unexpected_work'):
                observer._inspect_message({'method': method, 'params': {'threadId': 'other'}})

    def test_malformed_response_identity_is_fixed_unknown_not_type_error(self):
        observer = self.observer()
        observer._rpc_pending.add(1)
        for identity in ([], {}, True, None, ''):
            with self.assertRaisesRegex(CodexError, 'observer_boundary_unknown'):
                observer._inspect_message({'id': identity, 'result': {}})

    def test_event_wait_ticks_without_exhausting_external_lease(self):
        observer = self.observer()
        with self.assertRaisesRegex(CodexError, 'codex_timeout'):
            observer.event(.01)
        self.assertGreaterEqual(observer.observer_tick.call_count, 2)

    def test_tick_overrun_never_returns_an_arrived_response(self):
        observer = self.observer()
        observer._rpc_pending.add(1)
        observer.events.put({'id': 1, 'result': {}})
        observer.observer_tick.side_effect = lambda: setattr(observer, 'observer_deadline', 0)
        with self.assertRaisesRegex(CodexError, 'observer_timeout'):
            observer.event(.1)

    def test_loaded_lists_are_exhausted_and_target_is_never_loaded(self):
        observer = self.observer()
        observer.rpc = mock.Mock(side_effect=[{'data': ['other'], 'nextCursor': 'next'},
                                              {'data': [], 'nextCursor': None}])
        self.assertIs(True, observer.unloaded())
        self.assertEqual('next', observer.rpc.call_args_list[1][0][1]['cursor'])
        observer.rpc = mock.Mock(return_value={'data': ['original-thread'], 'nextCursor': None})
        with self.assertRaisesRegex(CodexError, 'thread_loaded'):
            observer.unloaded()

    def test_loaded_or_turn_cursor_cycle_cannot_grant_complete_observation(self):
        observer = self.observer()
        observer.rpc = mock.Mock(return_value={'data': [], 'nextCursor': 'cycle'})
        for call in (observer.unloaded, lambda: observer.closed_turns(self.path, {'one'})):
            with self.assertRaisesRegex(CodexError, 'pagination_invalid'):
                call()

    def test_closed_turns_page_all_ids_full_and_leave_old_failures_intact(self):
        observer = self.observer()
        old = self.turn('old', status='interrupted', error={'message': 'historical'})
        observer.rpc = mock.Mock(side_effect=[{'data': [old, self.turn()], 'nextCursor': 'next'},
                                              {'data': [self.turn('two')], 'nextCursor': None}])
        self.assertEqual(['one', 'two'], observer.closed_turns(self.path, {'one', 'two'}))
        for call in observer.rpc.call_args_list:
            self.assertEqual('full', call[0][1]['itemsView'])
            self.assertEqual('asc', call[0][1]['sortDirection'])
        self.assertEqual('interrupted', old['status'])

    def test_official_default_items_view_is_full_but_explicit_summary_rejected(self):
        observer = self.observer()
        turn = self.turn()
        turn.pop('itemsView')
        observer.rpc = mock.Mock(return_value={'data': [turn], 'nextCursor': None})
        self.assertEqual(['one'], observer.closed_turns(self.path, {'one'}))
        for view in ('summary', 'notLoaded', None):
            observer.rpc = mock.Mock(return_value={
                'data': [self.turn(itemsView=view)], 'nextCursor': None})
            with self.assertRaisesRegex(CodexError, 'snapshot_invalid'):
                observer.closed_turns(self.path, {'one'})

    def test_non_natural_or_missing_completed_turn_never_verified(self):
        observer = self.observer()
        for data in ([], [self.turn(status='failed')], [self.turn(error={'message': 'failed'})]):
            observer.rpc = mock.Mock(return_value={'data': data, 'nextCursor': None})
            with self.assertRaisesRegex(CodexError, 'turn_unsettled'):
                observer.closed_turns(self.path, {'one'})

    def test_duplicate_turn_or_truncated_pagination_is_unknown(self):
        observer = self.observer()
        for result in ({'data': [self.turn(), self.turn()], 'nextCursor': None},
                       {'data': [self.turn()]}, {'data': [] , 'nextCursor': ''}):
            observer.rpc = mock.Mock(return_value=result)
            with self.assertRaises(CodexError):
                observer.closed_turns(self.path, {'one'})

    def test_only_explicit_method_missing_uses_actual_legacy_full_read(self):
        observer = self.observer()
        observer.last_protocol_error = {'code': -32601}
        observer.rpc = mock.Mock(side_effect=[CodexError('codex_rpc_failed_thread_turns_list'),
            {'thread': {'id': 'original-thread', 'status': {'type': 'notLoaded'},
                        'path': self.path, 'turns': [self.turn()]}}])
        self.assertEqual(['one'], observer.closed_turns(self.path, {'one'}))
        self.assertIs(True, observer.rpc.call_args_list[1][0][1]['includeTurns'])
        for error in (CodexError('codex_timeout'), CodexError('codex_rpc_failed_thread_turns_list')):
            observer.last_protocol_error = {'code': -32603}
            observer.rpc = mock.Mock(side_effect=error)
            with self.assertRaises(CodexError):
                observer.closed_turns(self.path, {'one'})
            self.assertEqual(1, observer.rpc.call_count)

    def test_not_loaded_is_not_interchangeable_with_idle_or_different_path(self):
        observer = self.observer()
        for changes in ({'status': {'type': 'idle'}}, {'path': '/other'}, {'id': 'other'}):
            thread = dict({'id': 'original-thread', 'status': {'type': 'notLoaded'},
                           'path': self.path}, **changes)
            observer.rpc = mock.Mock(return_value={'thread': thread})
            with self.assertRaisesRegex(CodexError, 'snapshot_invalid'):
                observer.read_thread(self.path)

    def test_observer_shutdown_requires_its_own_natural_zero_and_eof(self):
        observer = self.observer()
        observer.process.poll.side_effect = lambda: 0 if observer._term_sent else None
        observer.events.put({'eof': True})
        with mock.patch('assistant_mesh.native_goal_observer.os.kill') as kill:
            proof = observer.finish_read_only()
        kill.assert_called_once_with(777, signal.SIGTERM)
        self.assertIs(True, proof['reader_eof'])
        self.assertNotIn('unloaded_before', proof)  # Shutdown alone did not read loaded/list.
        self.assertNotIn('unloaded_after', proof)
        observer.process.wait.assert_called_once_with(timeout=0)
        for code, forced in ((1, False), (0, True)):
            observer = self.observer()
            observer.process.poll.side_effect = lambda: code if observer._term_sent else None
            observer._native_cleanup_forced = forced
            observer.events.put({'eof': True})
            with mock.patch('assistant_mesh.native_goal_observer.os.kill'):
                with self.assertRaisesRegex(CodexError, 'boundary_unknown'):
                    observer.finish_read_only()

    def test_invalid_optin_configuration_precedes_any_auth_lookup(self):
        for config in ({'native_goal_yield': 'yes'}, {'native_goal_yield': True},
                       {'native_goal_yield': True, 'native_goal_drain': True,
                        'native_transport': 'stdio'}):
            with mock.patch('assistant_mesh.codex.discover_codex_auth') as auth:
                with self.assertRaises(CodexError):
                    Codex(config)
                auth.assert_not_called()
