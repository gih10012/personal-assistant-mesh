"""Opt-in admission drain fixtures, not a live goal/history yielded seal."""
import copy
import queue
import signal
import unittest
from unittest import mock

from assistant_mesh.codex import Codex, CodexError


class NativeGoalDrainTests(unittest.TestCase):
    thread = 'original-thread'
    turn = 'original-turn'

    def event(self, method, **params):
        return {'method': method, 'params': dict(threadId=self.thread, **params)}

    def started(self, identity):
        return self.event('turn/started', turn={'id': identity, 'status': 'inProgress'})

    def completed(self, identity=None, status='completed'):
        return self.event('turn/completed', turn={'id': identity or self.turn, 'status': status})

    def request(self, identity='original-request', turn=None):
        return dict(self.event('item/tool/call', turnId=turn or self.turn,
                               name='fixture_tool', arguments='{}'), id=identity)

    def runtime(self):
        # Actual start initializes lifecycle; fake RPC/child uses no credentials,
        # native executable, external network or model.
        agent = Codex.__new__(Codex)
        agent.config = {'workspace': '/fixture', 'native_goal_drain': True,
                        'goal': {'objective': 'Original owner goal', 'status': 'active'}}
        agent.tools = []
        agent.on_interaction = agent.on_tool = None
        agent.events, agent.deferred, agent.timeline = queue.Queue(), [], []
        agent._term_sent = agent._native_cleanup_forced = agent._reader_forced_close = False
        agent.native_drain_active = False
        agent.transport_name, agent._socket_owner_verified = 'unix', True
        agent._rpc_pending, agent.rpc_depth = set(), 0
        agent.serial = 0
        agent.process = mock.Mock(pid=424242)
        agent.process.poll.side_effect = lambda: 0 if getattr(agent, '_native_drain_eof', False) else None
        agent.process.wait.return_value = 0
        agent.reader = mock.Mock()
        agent.reader.is_alive.return_value = False
        agent._native_socket = mock.Mock()

        def rpc(method, params, **kwargs):
            if method == 'thread/start':
                return {'thread': {'id': self.thread, 'model': 'fixture', 'status': {'type': 'active'}}}
            if method == 'turn/start':
                return {'turn': {'id': self.turn, 'status': 'inProgress'}}
            if method == 'thread/goal/get':
                return {'goal': None}
            if method == 'thread/goal/set':
                return {'goal': dict(params, tokensUsed=0, timeUsedSeconds=0)}
            raise AssertionError('Unexpected fixture RPC: ' + method)

        agent.rpc = mock.Mock(side_effect=rpc)

        def activity(name, value):
            agent.timeline.append((name, copy.deepcopy(value)))
            return {'ok': True, 'thread_id': self.thread, 'pid': agent.process.pid}

        agent.on_activity = activity
        agent.start('Original bounded instruction')
        agent._consume_finish_event(self.started(self.turn), [])
        agent.timeline.clear()
        agent.rpc.reset_mock()
        return agent

    def tail(self, agent, *values):
        agent.deferred.extend(copy.deepcopy(values or (self.completed(),)))
        agent.deferred.extend([self.event('thread/status/changed', status={'type': 'idle'}), {'eof': True}])

    def finish(self, agent, **options):
        with mock.patch('assistant_mesh.codex.os.kill') as kill:
            answer = agent.finish(timeout=options.pop('timeout', .3), yield_requested=lambda: True, **options)
        return answer, kill

    def test_gate_durable_ack_one_owned_signal_precede_pump_and_remain_unsealed(self):
        agent = self.runtime()
        self.tail(agent)
        activity = agent.on_activity

        def ack(name, value):
            if name == 'native_drain_intent':
                self.assertIs(True, agent.native_drain_active)
                self.assertIs(False, agent._term_sent)
            return activity(name, value)

        agent.on_activity = ack
        answer, kill = self.finish(agent, tick=lambda: agent.timeline.append(('tick', None)))
        kill.assert_called_once_with(agent.process.pid, signal.SIGTERM)
        self.assertEqual(['native_drain_intent', 'native_drain_signal', 'tick'],
                         [name for name, value in agent.timeline[:3]])
        self.assertTrue(answer)
        self.assertEqual('drained_unverified', agent.native_lifecycle['outcome'])
        self.assertFalse(agent.native_lifecycle['settled'])
        self.assertFalse(agent.native_lifecycle['quiescent'])
        self.assertTrue(agent.native_lifecycle['runtime_closed'])
        self.assertEqual('active', agent.native_lifecycle['goal']['status'])
        agent.rpc.assert_not_called()
        agent.process.wait.assert_called_once_with(timeout=0)
        agent.reader.join.assert_called_once()

    def test_admitted_host_request_responds_once_original_id_after_signal(self):
        agent = self.runtime()
        agent.on_tool = mock.Mock(return_value={'content': [{'type': 'text', 'text': 'fixture'}]})
        self.tail(agent, self.request(), self.completed())
        self.finish(agent)
        agent.on_tool.assert_called_once()
        agent._native_socket.send_json.assert_called_once_with(
            {'id': 'original-request', 'result': agent.on_tool.return_value})
        self.assertEqual('responded', next(iter(agent._native_host_receipts.values()))['status'])
        names = [name for name, value in agent.timeline]
        self.assertLess(names.index('native_drain_signal'), names.index('native_host_intent'))
        self.assertEqual(1, agent.native_lifecycle['drain_evidence']['host_request_count'])

    def test_late_admitted_turns_are_all_naturally_completed(self):
        agent = self.runtime()
        self.tail(agent, self.completed(), self.started('admitted-auto-turn'),
                  self.completed('admitted-auto-turn'))
        self.finish(agent)
        self.assertEqual({self.turn, 'admitted-auto-turn'}, set(
            agent.native_lifecycle['drain_evidence']['started_turn_ids']))

    def test_queued_observation_marker_does_not_invalidate_newer_idle(self):
        agent = self.runtime()
        value = self.started(self.turn)
        agent._record_native_drain_event(value)
        agent._native_drain_idle = True
        agent._consume_finish_event(value, [])
        self.assertTrue(agent._native_drain_idle)

    def test_default_disabled_stdio_or_unverified_transport_never_drain_signals(self):
        for change, category in (({'config': {}}, 'codex_goal_coordination_yield_unavailable'),
                ({'transport_name': 'stdio'}, 'codex_native_drain_transport_unverified'),
                ({'_socket_owner_verified': False}, 'codex_native_drain_transport_unverified')):
            with self.subTest(change=change):
                agent = self.runtime()
                for name, value in change.items():
                    setattr(agent, name, value)
                with mock.patch('assistant_mesh.codex.os.kill') as kill:
                    with self.assertRaisesRegex(CodexError, category):
                        agent._begin_native_goal_drain()
                kill.assert_not_called()

    def test_missing_drain_ack_closes_gate_without_sending_signal(self):
        agent = self.runtime()
        agent.on_activity = mock.Mock(return_value=None)
        with mock.patch('assistant_mesh.codex.os.kill') as kill:
            with self.assertRaisesRegex(CodexError, 'codex_native_drain_intent_unconfirmed'):
                agent._begin_native_goal_drain()
        kill.assert_not_called()
        self.assertTrue(agent.native_drain_active)
        self.assertEqual('unknown', agent.native_lifecycle['drain_intent']['status'])

    def test_lease_loss_before_signal_propagates_and_does_not_retry(self):
        agent = self.runtime()
        agent.on_activity = mock.Mock(side_effect=ValueError('lease_lost'))
        with mock.patch('assistant_mesh.codex.os.kill') as kill:
            with self.assertRaisesRegex(ValueError, 'lease_lost'):
                agent._begin_native_goal_drain()
            with self.assertRaisesRegex(CodexError, 'codex_native_drain_boundary_unknown'):
                agent._begin_native_goal_drain()
        kill.assert_not_called()

    def test_ambiguous_signal_delivery_never_retries(self):
        agent = self.runtime()
        with mock.patch('assistant_mesh.codex.os.kill', side_effect=OSError('lost signal')) as kill:
            with self.assertRaisesRegex(CodexError, 'codex_native_drain_signal_unknown'):
                agent._begin_native_goal_drain()
            with self.assertRaisesRegex(CodexError, 'codex_native_drain_boundary_unknown'):
                agent._begin_native_goal_drain()
        self.assertEqual(1, kill.call_count)
        self.assertTrue(agent._term_sent)

    def test_pending_work_prevents_boundary_signal(self):
        for name, value in (('rpc_depth', 1), ('_rpc_pending', {8}),
                ('_pending_goal_input', 'unsubmitted'), ('_held_host_requests', [{}]),
                ('_native_host_receipts', {'8': {'status': 'unknown'}})):
            with self.subTest(name=name):
                agent = self.runtime()
                setattr(agent, name, value)
                with mock.patch('assistant_mesh.codex.os.kill') as kill:
                    with self.assertRaisesRegex(CodexError, 'codex_native_drain_boundary_unknown'):
                        agent._begin_native_goal_drain()
                kill.assert_not_called()

    def test_start_and_rpc_cannot_clear_drain_gate_or_send_new_work(self):
        for flag in ('native_drain_active', '_term_sent', 'outcome'):
            with self.subTest(flag=flag):
                agent = self.runtime()
                if flag == 'outcome':
                    agent.native_lifecycle['outcome'] = 'drained_unverified'
                else:
                    setattr(agent, flag, True)
                with self.assertRaisesRegex(CodexError, 'codex_native_drain_rpc_blocked'):
                    agent.start('Never create a new identity', {'thread_id': self.thread})
                agent.rpc.assert_not_called()
        agent = self.runtime()
        agent.native_drain_active = True
        with self.assertRaisesRegex(CodexError, 'codex_native_drain_rpc_blocked'):
            Codex.rpc(agent, 'turn/start', {'threadId': self.thread})
        with self.assertRaisesRegex(CodexError, 'codex_native_drain_rpc_blocked'):
            agent.send({'method': 'turn/steer', 'params': {}})
        agent._native_socket.send_json.assert_not_called()

    def test_invalid_host_identity_is_fixed_unknown_before_callback(self):
        for params in ({'turnId': []}, {'turnId': {}}, {'turnId': ''},
                       {'turnId': 'never-started'}, {'threadId': 'foreign'}):
            with self.subTest(params=params):
                agent = self.runtime()
                agent.on_tool = mock.Mock()
                value = self.request()
                value['params'].update(params)
                with self.assertRaisesRegex(CodexError, 'codex_native_drain_request_invalid'):
                    agent._handle_host_request(value)
                agent.on_tool.assert_not_called()
        for identity in (True, None, [], {}, '', 'a' * 1025):
            with self.subTest(identity=repr(identity)[:30]):
                agent = self.runtime()
                with self.assertRaisesRegex(CodexError, 'codex_native_drain_request_invalid'):
                    agent._handle_host_request(self.request(identity))

    def test_duplicate_request_and_unknown_reply_effect_are_never_replayed(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                agent = self.runtime()
                agent.on_tool = mock.Mock(return_value={})
                if fail:
                    agent._native_socket.send_json.side_effect = OSError('lost reply')
                    with self.assertRaises(CodexError):
                        agent._handle_host_request(self.request())
                else:
                    agent._handle_host_request(self.request())
                with self.assertRaisesRegex(CodexError, 'codex_native_drain_request_duplicate'):
                    agent._handle_host_request(self.request())
                self.assertEqual(1, agent.on_tool.call_count)

    def test_host_intent_ack_is_required_before_any_tool_effect(self):
        agent = self.runtime()
        agent.on_tool = mock.Mock()
        agent.on_activity = mock.Mock(return_value=None)
        with self.assertRaisesRegex(CodexError, 'codex_native_drain_intent_unconfirmed'):
            agent._handle_host_request(self.request())
        agent.on_tool.assert_not_called()
        self.assertEqual('unknown', next(iter(agent._native_host_receipts.values()))['status'])

    def test_malformed_turn_identity_never_raises_bare_type_error(self):
        for identity in ([], {}, True, None, ''):
            for method in ('turn/completed', 'item/started'):
                with self.subTest(identity=identity, method=method):
                    agent = self.runtime()
                    value = self.event(method, turn={'id': identity, 'status': 'completed'}, turnId=identity)
                    with self.assertRaisesRegex(CodexError, 'codex_native_drain_turn_unsettled'):
                        agent._record_native_drain_event(value)

    def test_non_natural_completion_never_seals(self):
        for status in ('interrupted', 'failed', None):
            with self.subTest(status=status):
                agent = self.runtime()
                self.tail(agent, self.completed(status=status))
                with self.assertRaises(CodexError):
                    self.finish(agent)
                self.assertFalse(agent.native_lifecycle['settled'])

    def test_missing_completion_nonzero_exit_or_forced_cleanup_stays_unknown(self):
        for change in ('missing', 'nonzero', 'forced', 'reader_closed', 'not_idle'):
            with self.subTest(change=change):
                agent = self.runtime()
                self.tail(agent)
                if change == 'missing':
                    agent.deferred.pop(0)
                elif change == 'nonzero':
                    agent.process.poll.side_effect = lambda: 1 if agent._native_drain_eof else None
                elif change == 'forced':
                    agent._native_cleanup_forced = True
                elif change == 'reader_closed':
                    agent._reader_forced_close = True
                else:
                    agent.deferred.pop(1)
                with self.assertRaisesRegex(CodexError, 'codex_native_drain_boundary_unknown'):
                    self.finish(agent)
                self.assertFalse(agent.native_lifecycle['settled'])

    def test_eof_then_more_messages_or_unexpected_response_is_unknown(self):
        for values in ([{'eof': True}, self.completed()], [{'id': 17, 'result': {}}]):
            with self.subTest(values=values):
                agent = self.runtime()
                agent.deferred = copy.deepcopy(values)
                with self.assertRaisesRegex(CodexError, 'codex_native_drain_boundary_unknown'):
                    self.finish(agent)

    def test_missing_eof_or_unreaped_reader_times_out_unsettled(self):
        for eof in (False, True):
            with self.subTest(eof=eof):
                agent = self.runtime()
                self.tail(agent)
                if eof:
                    agent.reader.is_alive.return_value = True
                else:
                    agent.deferred.pop()
                with self.assertRaisesRegex(CodexError, 'codex_native_drain_timeout'):
                    self.finish(agent, timeout=.01)
                self.assertFalse(agent.native_lifecycle['settled'])

    def test_tick_overrun_cannot_consume_next_host_request_or_grant_success(self):
        agent = self.runtime()
        agent.on_tool = mock.Mock()
        self.tail(agent, self.request(), self.completed())
        clock = [0.0]

        def tick():
            clock[0] = 2.0

        with mock.patch('assistant_mesh.codex.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(CodexError, 'codex_native_drain_timeout'):
                self.finish(agent, timeout=1, tick=tick)
        agent.on_tool.assert_not_called()

    def test_callback_overrun_retains_effect_receipt_but_never_success(self):
        agent = self.runtime()
        self.tail(agent, self.request(), self.completed())
        clock = [0.0]

        def tool(params):
            clock[0] = 2.0
            return {}

        agent.on_tool = tool
        with mock.patch('assistant_mesh.codex.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(CodexError, 'codex_native_drain_timeout'):
                self.finish(agent, timeout=1)
        self.assertEqual('responded', next(iter(agent._native_host_receipts.values()))['status'])
        self.assertFalse(agent.native_lifecycle['runtime_closed'])

    def test_invalid_opt_in_is_rejected_before_auth_or_process(self):
        with mock.patch('assistant_mesh.codex.subprocess.Popen') as process:
            for flag in ('true', 1, None, []):
                with self.subTest(flag=flag):
                    with self.assertRaisesRegex(CodexError, 'invalid_native_goal_drain_flag'):
                        Codex({'native_goal_drain': flag})
            process.assert_not_called()

    def test_global_or_foreign_notifications_before_start_do_not_require_thread_state(self):
        agent = Codex.__new__(Codex)
        agent.config = {'native_goal_drain': True}
        for params in ({}, {'threadId': None}, {'threadId': 'not-started-here'}):
            value = {'method': 'thread/status/changed', 'params': dict(params, status={'type': 'idle'})}
            agent._record_native_drain_event(value)
            self.assertNotIn('_mesh_drain_observed', value)
        self.assertFalse(hasattr(agent, '_native_started_turns'))

    def test_host_request_before_known_thread_is_fixed_unknown_not_callback(self):
        agent = self.runtime()
        del agent.thread_id
        agent.on_tool = mock.Mock()
        with self.assertRaisesRegex(CodexError, 'codex_native_drain_request_invalid'):
            agent._handle_host_request(self.request())
        agent.on_tool.assert_not_called()


if __name__ == '__main__':
    unittest.main()
