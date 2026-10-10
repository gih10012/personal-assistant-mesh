"""Original owned drain plus independent evidence, without a real model."""
import copy
import queue
import time
import unittest
from unittest import mock

from assistant_mesh.codex import Codex, CodexError


class NativeYieldSealTests(unittest.TestCase):
    path = '/owner/native/selected.jsonl'

    def goal(self, **changes):
        value = {'threadId': 'original-thread', 'objective': 'Original goal', 'status': 'active',
                 'createdAt': 10, 'updatedAt': 20, 'tokensUsed': 12,
                 'timeUsedSeconds': 3, 'tokenBudget': None}
        value.update(changes)
        return value

    def agent(self):
        agent = Codex.__new__(Codex)
        agent.config = {'native_goal_drain': True, 'native_goal_yield': True,
                        'native_transport': 'unix'}
        agent.auth_home, agent.rollout_path = '/owner/authorized-profile', self.path
        agent.thread_id, agent.turn_id = 'original-thread', 'original-turn'
        agent.transport_name, agent._socket_owner_verified = 'unix', True
        agent.native_drain_active, agent._term_sent = True, True
        agent._native_drain_eof = agent._native_drain_idle = True
        agent._native_cleanup_forced = agent._reader_forced_close = False
        agent._native_started_turns = {'original-turn'}
        agent._native_completion_statuses = {'original-turn': 'completed'}
        agent._native_host_receipts, agent._rpc_pending = {}, set()
        agent.deferred, agent.events = [], queue.Queue()
        agent.process, agent.reader = mock.Mock(pid=555), mock.Mock()
        agent.process.poll.return_value = 0
        agent.reader.is_alive.return_value = False
        agent.native_lifecycle = {
            'controller_enabled': True, 'goal_managed': True, 'settled': False,
            'quiescent': False, 'runtime_closed': True, 'thread_id': agent.thread_id,
            'outcome': 'drained_unverified', 'goal': self.goal(),
            'pre_drain_goal': self.goal(), 'expected_goal_objective': 'Original goal',
            'drain_intent': {'status': 'sent', 'pid': 555, 'thread_id': agent.thread_id},
            'drain_evidence': {'pid': 555, 'natural_exit_code': 0, 'reader_eof': True,
                'idle_observed': True, 'started_turn_ids': ['original-turn'],
                'natural_completion_statuses': agent._native_completion_statuses.copy()},
            'history_prefix': {'path': self.path, 'fingerprint': {'fixture': 'prefix'}}}
        return agent

    def observation(self, **goal):
        return {'goal': self.goal(**goal), 'thread_status': {'type': 'notLoaded'},
                'independent_observer': {'thread_id': 'original-thread', 'pid': 556,
                    'natural_exit_code': 0, 'reader_eof': True, 'read_only': True,
                    'unloaded_before': True, 'unloaded_after': True,
                    'turns_verified': ['original-turn']}}

    def frozen(self):
        return {'path': self.path, 'fingerprint': {'dev': 1, 'ino': 2, 'uid': 3,
            'mode': 0o600, 'size': 120, 'mtime_ns': 10, 'ctime_ns': 10, 'sha256': 'a' * 64}}

    def seal(self, agent, **goal):
        with mock.patch('assistant_mesh.native_goal_observer.observe_native_yield',
                        return_value=self.observation(**goal)) as observer, \
                mock.patch('assistant_mesh.native_history.verify_history',
                           return_value=self.frozen()) as history:
            result = agent.seal_native_yield(tick=mock.Mock(), timeout=1)
        return result, observer, history

    def test_yield_is_independent_of_terminal_seal_and_keeps_original_goal(self):
        agent = self.agent()
        agent.rpc = agent.seal_native_lifecycle = mock.Mock(side_effect=AssertionError('no RPC'))
        result, observer, history = self.seal(agent, tokensUsed=15, updatedAt=21)
        self.assertEqual('yielded', result['lifecycle']['outcome'])
        self.assertEqual('active', result['lifecycle']['goal']['status'])
        self.assertEqual('original-thread', result['lifecycle']['thread_id'])
        self.assertIs(True, agent.native_lifecycle['settled'])
        self.assertEqual(12, agent.native_lifecycle['pre_drain_goal']['tokensUsed'])
        self.assertEqual(self.frozen()['fingerprint'], result['fingerprint'])
        agent.rpc.assert_not_called()
        self.assertEqual('/owner/authorized-profile', observer.call_args[0][0]['auth_home'])
        self.assertIs(True, observer.call_args[0][0]['strict_auth_home'])
        self.assertEqual({'original-turn'}, history.call_args[0][3])

    def test_actual_terminal_observation_does_not_write_active(self):
        agent = self.agent()
        result, _, _ = self.seal(agent, status='paused')
        self.assertEqual('terminal', result['lifecycle']['outcome'])
        self.assertEqual('paused', result['lifecycle']['goal']['status'])

    def test_default_off_or_incomplete_original_closure_never_spawns_observer(self):
        for change in ('disabled', 'forced', 'not_reaped', 'reader_live', 'pending_request',
                       'pending_rpc', 'missing_eof', 'wrong_path'):
            agent = self.agent()
            if change == 'disabled':
                agent.config['native_goal_yield'] = False
            elif change == 'forced':
                agent._native_cleanup_forced = True
            elif change == 'not_reaped':
                agent.process.poll.return_value = None
            elif change == 'reader_live':
                agent.reader.is_alive.return_value = True
            elif change == 'pending_request':
                agent._native_host_receipts = {'request': {'status': 'unknown'}}
            elif change == 'pending_rpc':
                agent._rpc_pending = {1}
            elif change == 'missing_eof':
                agent._native_drain_eof = False
            else:
                agent.rollout_path = '/other'
            with mock.patch('assistant_mesh.native_goal_observer.observe_native_yield') as observer:
                with self.subTest(change=change), self.assertRaises(CodexError):
                    agent.seal_native_yield()
                observer.assert_not_called()

    def test_history_or_observer_failure_preserves_unsettled_original_lifecycle(self):
        for failure in ('history', 'observer'):
            agent = self.agent()
            before = copy.deepcopy(agent.native_lifecycle)
            with mock.patch('assistant_mesh.native_goal_observer.observe_native_yield',
                            side_effect=CodexError('codex_native_observer_snapshot_invalid')
                            if failure == 'observer' else None, return_value=self.observation()), \
                    mock.patch('assistant_mesh.native_history.verify_history',
                               side_effect=ValueError('native_history_prefix_changed')):
                with self.assertRaises(ValueError):
                    agent.seal_native_yield(timeout=1)
            self.assertEqual(before, agent.native_lifecycle)

    def test_tick_deadline_or_epoch_loss_never_grants_seal(self):
        for effect in (CodexError('codex_native_observer_timeout'), ValueError('lease_lost')):
            agent = self.agent()
            with mock.patch('assistant_mesh.native_goal_observer.observe_native_yield') as observer:
                with self.assertRaises(ValueError):
                    agent.seal_native_yield(tick=mock.Mock(side_effect=effect))
                observer.assert_not_called()
            self.assertIs(False, agent.native_lifecycle['settled'])

    def test_prefix_capture_and_baseline_ack_precede_the_only_signal(self):
        agent = self.agent()
        agent.native_drain_active = agent._term_sent = False
        agent.process.poll.return_value = None
        agent.rpc_depth = 0
        agent.on_activity = mock.Mock(return_value={'ok': True, 'pid': 555,
                                                   'thread_id': agent.thread_id})
        agent.native_lifecycle['runtime_closed'] = False
        with mock.patch('assistant_mesh.native_history.capture_prefix',
                        return_value=agent.native_lifecycle['history_prefix']) as capture, \
                mock.patch('assistant_mesh.codex.os.kill') as kill:
            agent._begin_native_goal_drain(tick=mock.Mock(), deadline=time.monotonic() + 1)
        capture.assert_called_once()
        self.assertEqual(self.goal(), agent.on_activity.call_args_list[0][0][1]['pre_drain_goal'])
        kill.assert_called_once()

    def test_prefix_failure_or_expired_ack_never_sends_drain_signal(self):
        for failed in ('prefix', 'expired_ack'):
            agent = self.agent()
            agent.native_drain_active = agent._term_sent = False
            agent.process.poll.return_value = None
            agent.rpc_depth = 0
            now = [1.]

            def ack(*args):
                if failed == 'expired_ack':
                    now[0] = 3.
                return {'ok': True, 'pid': 555, 'thread_id': agent.thread_id}

            agent.on_activity = ack
            with mock.patch('assistant_mesh.codex.time.monotonic', side_effect=lambda: now[0]), \
                    mock.patch('assistant_mesh.native_history.capture_prefix',
                               return_value=agent.native_lifecycle['history_prefix'],
                               side_effect=ValueError('native_history_prefix_changed')
                               if failed == 'prefix' else None), \
                    mock.patch('assistant_mesh.codex.os.kill') as kill:
                with self.assertRaises(ValueError):
                    agent._begin_native_goal_drain(deadline=2.)
                kill.assert_not_called()
            self.assertIs(True, agent.native_drain_active)
