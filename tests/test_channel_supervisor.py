"""Owned retry/renew supervision with synthetic iLink and private fixture DBs.

No production proof, credentials, network requests, SSH, or receiver deployment.
"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.channel import Channel, ChannelError
from assistant_mesh.channel_authority import ChannelAuthority, RemoteChannelState
from assistant_mesh.channel_carrier import CarrierError, CarrierFence, PROTOCOL
from assistant_mesh.channel_supervisor import BoundedBackoff, ChannelSupervisor, LocalDiagnostics
from assistant_mesh.networking import digest
from assistant_mesh.remote_notifications import PROTOCOL as NOTIFY_PROTOCOL
from assistant_mesh.store import Store
from tests import test_channel_fence_integration as fixtures


class ScriptStop:
    def __init__(self, iterations, on_wait=None):
        self.iterations, self.on_wait = iterations, on_wait
        self.stopped, self.delays = False, []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, delay):
        self.delays.append(delay)
        if self.on_wait:
            self.on_wait(len(self.delays))
        if len(self.delays) >= self.iterations:
            self.set()
        return self.stopped


class SyntheticRPC:
    def __init__(self, bound):
        self.bound, self.actions = bound, []
        self.fail_before, self.drop_after, self.modify = None, None, None
        self.block, self.entered, self.release = None, threading.Event(), threading.Event()

    def request(self, path, payload):
        self.actions.append(path)
        if path == self.fail_before:
            raise OSError('synthetic private endpoint/account/response detail')
        if path == self.block:
            self.entered.set()
            if not self.release.wait(2):
                raise OSError('synthetic bounded fixture blocked')
        receipt = self.bound.request(path, payload)
        if path == self.drop_after:
            raise OSError('synthetic write succeeded but ACK was lost')
        return self.modify(path, receipt) if self.modify else receipt


class ChannelSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.now = 1000
        self.store = Store(self.root / 'original.db', clock=lambda: self.now)
        self.transport = fixtures.SyntheticILink()
        self.binding = digest([NOTIFY_PROTOCOL, self.transport.base,
            self.transport.account['ilink_user_id'], self.transport.account['ilink_bot_id']])
        self.store.set('owner_notification_channel_binding', self.binding)
        self.store.set('cursor', 'original-canonical-cursor')
        self.store.set('owner_context', {'token': 'synthetic-context', 'version': 'original-context'})
        self.authority = ChannelAuthority(self.store, {'witness_id': 'fixture-witness',
            'owner_ref': 'owner-ref'}, 'cloud', self.transport, self.binding)
        self.witness = self.authority.witness
        # Isolated synthetic verifier, never production stopping/closure proof.
        self.witness.handoff_verifier = fixtures.ChannelFenceIntegrationTests.handoff
        self.contract = {'protocol': PROTOCOL, 'node': 'cloud', 'owner_ref': 'owner-ref',
            'account_binding': self.binding, 'canonical_authority': 'cloud',
            'private_credential_ref': 'synthetic-private-ref', 'revision': 1,
            'runtime': {'adapter': 'ilink/0.3.0', 'fence_protocol': PROTOCOL,
                        'transport_available': True, 'private_state': True}}
        self.witness.candidate('cloud', self.contract, 300)
        self.term = self.witness.grant(self.binding, 0, 'cloud', 60, 'synthetic-bootstrap')
        self.rpc = SyntheticRPC(self.authority.bound_client('cloud'))
        self.state = RemoteChannelState(self.rpc, self.root / 'carrier-spool.db')
        self.channel = Channel(self.state, self.transport, CarrierFence(self.rpc, self.term))
        self.rpc.actions.clear()
        self.supervisor = None

    def tearDown(self):
        if self.supervisor:
            self.rpc.release.set()
            self.assertTrue(self.supervisor.stop(2))
        self.tmp.cleanup()

    def supervised(self, **options):
        self.supervisor = ChannelSupervisor(self.channel, **options)
        return self.supervisor

    def outbox(self, request='original-send'):
        with self.store.transaction() as db:
            return dict(db.execute('SELECT * FROM outbox WHERE id=?', (request,)).fetchone())

    def test_poll_remote_failure_retries_without_remote_error_write_and_recovers(self):
        self.rpc.fail_before = '/channel-carrier/state/get'
        self.channel.stop = ScriptStop(3, lambda n: setattr(self.rpc, 'fail_before', None) if n == 2 else None)
        self.channel.run_poll()
        self.assertEqual([2, 4, 1], self.channel.stop.delays)
        self.assertNotIn('/channel-carrier/state/set', self.rpc.actions)
        self.assertEqual(1, len(self.transport.calls))
        self.assertEqual({}, self.channel.local_errors())
        self.assertEqual('next-canonical-cursor', self.store.get('cursor'))

    def test_send_remote_failure_retries_without_remote_error_write_and_recovers(self):
        self.store.enqueue('original-send', 'synthetic body')
        original = self.outbox()['client_id']
        self.rpc.fail_before = '/channel-carrier/state/prepare_send'
        self.channel.stop = ScriptStop(3, lambda n: setattr(self.rpc, 'fail_before', None) if n == 2 else None)
        self.channel.run_send()
        self.assertEqual([1, 2, 0.5], self.channel.stop.delays)
        self.assertNotIn('/channel-carrier/state/set', self.rpc.actions)
        self.assertEqual('accepted', self.store.send_status('original-send')['status'])
        self.assertEqual(original, self.transport.calls[0][1]['msg']['client_id'])

    def test_local_fixed_diagnostic_is_bounded_and_does_not_expose_exception(self):
        self.rpc.fail_before = '/channel-carrier/state/get'
        self.channel.stop = ScriptStop(10)
        self.channel.run_poll()
        self.assertEqual(60, max(self.channel.stop.delays))
        self.assertEqual(1, len(self.channel.local_errors()))
        self.assertEqual('poll_failed', self.channel.local_errors()['poll']['code'])
        self.assertEqual(10, self.channel.local_errors()['poll']['failures'])
        self.assertNotIn('private', str(self.channel.local_errors()))
        self.assertNotIn('/channel-carrier/state/set', self.rpc.actions)

    def test_legacy_default_success_cadence_and_local_error_reporting_preserved(self):
        legacy = Channel(self.store, self.transport)
        self.assertIsNone(legacy._supervisor)
        for lane, delay in (('poll', 1), ('send', 0.5)):
            legacy.stop = ScriptStop(1)
            with patch.object(legacy, lane + '_once', return_value=False):
                getattr(legacy, 'run_' + lane)()
            self.assertEqual([delay], legacy.stop.delays)
        legacy.stop = ScriptStop(1)
        with patch.object(legacy, 'send_once', side_effect=ChannelError('network_error')):
            legacy.run_send()
        self.assertEqual([0.5], legacy.stop.delays)
        self.assertEqual('channel_fence_blocked', self.store.get('channel_error')['code'])
        self.assertEqual([], self.rpc.actions)

    def test_legacy_error_reporting_failure_does_not_kill_loop(self):
        legacy = Channel(self.store, self.transport)
        legacy.stop = ScriptStop(2)
        with patch.object(legacy, 'poll_once', side_effect=ChannelError('network_error')), \
                patch.object(self.store, 'set', side_effect=OSError('synthetic local store unavailable')):
            legacy.run_poll()
        self.assertEqual([2, 4], legacy.stop.delays)
        self.assertTrue(legacy.local_errors()['poll']['report_failed'])
        self.assertEqual('network_error', legacy.local_errors()['poll']['code'])

    def test_backoff_wait_is_interruptible_and_duplicate_loop_does_not_overlap(self):
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(1)
            raise OSError('synthetic unavailable')
        with patch.object(self.channel, 'poll_once', side_effect=blocked) as poll:
            first = threading.Thread(target=self.channel.run_poll)
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                second = threading.Thread(target=self.channel.run_poll)
                second.start()
                second.join(1)
                self.assertFalse(second.is_alive())
                self.channel.stop.set()
                release.set()
                first.join(1)
                self.assertFalse(first.is_alive())
                self.assertEqual(1, poll.call_count)
            finally:
                self.channel.stop.set()
                release.set()
                first.join(2)

    def test_send_result_ack_loss_then_reconnect_never_replays_original(self):
        self.store.enqueue('original-send', 'synthetic body')
        original = self.outbox()['client_id']
        self.rpc.drop_after = '/channel-carrier/state/record_result'
        self.channel.stop = ScriptStop(3, lambda n: setattr(self.rpc, 'drop_after', None))
        self.channel.run_send()
        self.assertEqual(1, len(self.transport.calls))
        row = self.outbox()
        self.assertEqual(('unknown', original), (row['status'], row['client_id']))
        with self.store.transaction() as db:
            intents = list(db.execute('SELECT * FROM channel_canonical_intents'))
        self.assertEqual(1, len(intents))
        self.assertEqual('accepted', self.state.spool(intents[0]['operation_id'])['outcome']['status'])
        self.assertNotIn('/channel-carrier/state/set', self.rpc.actions)

    def test_unknown_poll_retries_guard_not_transport_or_cursor(self):
        self.transport.error = ChannelError('network_timeout')
        self.channel.stop = ScriptStop(3)
        self.channel.run_poll()
        self.assertEqual(1, len(self.transport.calls))
        self.assertEqual('original-canonical-cursor', self.store.get('cursor'))
        with self.store.transaction() as db:
            intents = list(db.execute('SELECT * FROM channel_carrier_intents'))
        self.assertEqual(1, len(intents))
        self.assertEqual('unknown', intents[0]['state'])

    def test_optional_supervisor_inspects_refreshes_explicit_candidate_and_renews_only_original_term(self):
        supervisor = self.supervised(candidate_contract=self.contract)
        with self.assertRaisesRegex(ChannelError, '^channel_fence_blocked$'):
            self.channel.poll_once()
        self.assertEqual([], self.transport.calls)
        self.now = 1010
        renewed = supervisor.maintenance_once()
        self.assertTrue(supervisor.ready.is_set())
        self.assertEqual(1070, renewed['deadline'])
        for key in self.term:
            if key != 'deadline':
                self.assertEqual(self.term[key], renewed[key])
        self.assertEqual(['/channel-carrier/inspect', '/channel-carrier/candidate',
                          '/channel-carrier/renew'], self.rpc.actions)
        self.assertEqual([], self.transport.calls)
        self.assertIsNone(self.store.get('channel_error'))

    def test_candidate_or_renew_ack_loss_closes_ready_then_same_term_readback_recovers(self):
        supervisor = self.supervised(candidate_contract=self.contract)
        for action in ('candidate', 'renew'):
            with self.subTest(action=action):
                self.rpc.actions.clear()
                self.rpc.drop_after = '/channel-carrier/' + action
                with self.assertRaisesRegex(CarrierError, '^channel_renew_failed$'):
                    supervisor.maintenance_once()
                self.assertFalse(supervisor.ready.is_set())
                with self.assertRaisesRegex(ChannelError, '^channel_fence_blocked$'):
                    self.channel.send_once()
                self.rpc.drop_after = None
                self.now += 1
                supervisor.maintenance_once()
                self.assertTrue(supervisor.ready.is_set())
                self.assertEqual(self.term['epoch'], self.channel.carrier_fence.term['epoch'])
                self.assertEqual(self.term['holder'], self.channel.carrier_fence.term['holder'])
                self.assertEqual('/channel-carrier/inspect', self.rpc.actions[-3])
        self.assertEqual([], self.transport.calls)

    def test_supervisor_without_explicit_report_cannot_bypass_candidate_ttl(self):
        supervisor = self.supervised(renew_seconds=300)
        supervisor.maintenance_once()
        self.now = 1280
        supervisor.maintenance_once()
        self.now = 1301  # Current term still valid, original candidate expired.
        with self.assertRaisesRegex(CarrierError, '^channel_renew_failed$'):
            supervisor.maintenance_once()
        self.assertFalse(supervisor.ready.is_set())
        self.assertNotIn('/channel-carrier/candidate', self.rpc.actions)
        self.assertEqual(self.term['epoch'], self.witness.inspect(self.binding)['epoch'])
        self.assertEqual([], self.transport.calls)

    def test_expired_original_term_cannot_be_regranted_by_supervisor(self):
        supervisor = self.supervised(candidate_contract=self.contract)
        self.now = self.term['deadline']
        with self.assertRaisesRegex(CarrierError, '^channel_renew_failed$'):
            supervisor.maintenance_once()
        self.assertFalse(supervisor.ready.is_set())
        self.assertEqual(self.term['epoch'], self.witness.inspect(self.binding)['epoch'])
        self.assertEqual([], self.transport.calls)
        self.assertFalse(any(path.endswith('/grant') or path.endswith('/enroll')
                             or path.endswith('/reconcile') for path in self.rpc.actions))

    def test_changed_holder_frozen_or_witness_cannot_be_adopted_on_reconnect(self):
        supervisor = self.supervised(candidate_contract=self.contract)
        for field, value in (('holder', 'laptop'), ('frozen', True), ('incarnation', 'b' * 64)):
            with self.subTest(field=field):
                self.rpc.actions.clear()
                self.rpc.modify = lambda path, receipt: dict(receipt, **{field: value})
                with self.assertRaisesRegex(CarrierError, '^channel_term_changed$'):
                    supervisor.maintenance_once()
                self.assertFalse(supervisor.ready.is_set())
                self.assertEqual(['/channel-carrier/inspect'], self.rpc.actions)
                self.assertEqual(self.term, self.channel.carrier_fence.term)
        self.rpc.modify = None

    def test_explicit_candidate_must_match_existing_owner_node_and_authority(self):
        for field, value in (('node', 'laptop'), ('owner_ref', 'another-owner'),
                             ('canonical_authority', 'another-authority')):
            with self.subTest(field=field):
                contract = dict(self.contract, **{field: value})
                with self.assertRaisesRegex(CarrierError, '^channel_supervisor_candidate_invalid$'):
                    ChannelSupervisor(self.channel, candidate_contract=contract)
                self.assertIsNone(self.channel._supervisor)
        self.assertEqual([], self.rpc.actions)

    def test_renew_keeps_unknown_operation_barrier_without_replay(self):
        self.store.enqueue('original-send', 'synthetic body')
        self.transport.error = ChannelError('network_timeout')
        self.channel.send_once()
        original = self.outbox()['client_id']
        supervisor = self.supervised(candidate_contract=self.contract)
        supervisor.maintenance_once()
        self.transport.error = None
        self.assertFalse(self.channel.send_once())
        self.assertEqual(1, len(self.transport.calls))
        self.assertEqual(original, self.outbox()['client_id'])
        self.assertEqual('unknown', self.store.send_status('original-send')['status'])
        with self.store.transaction() as db:
            self.assertEqual('unknown', db.execute('SELECT state FROM channel_carrier_intents').fetchone()[0])

    def test_supervisor_start_stop_owns_one_group_and_no_implicit_second_group(self):
        supervisor = self.supervised(renew_interval=0.01, candidate_contract=self.contract)
        polled, sent = threading.Event(), threading.Event()
        with patch.object(self.channel, 'poll_once', side_effect=polled.set), \
                patch.object(self.channel, 'send_once', side_effect=sent.set):
            supervisor.start()
            self.assertTrue(polled.wait(1))
            self.assertTrue(sent.wait(1))
            with self.assertRaisesRegex(CarrierError, '^channel_supervisor_already_started$'):
                supervisor.start()
            with self.assertRaisesRegex(CarrierError, '^channel_supervisor_already_owned$'):
                ChannelSupervisor(self.channel)
            self.assertTrue(supervisor.stop(1))
        snapshot = supervisor.snapshot()
        self.assertFalse(snapshot['ready'])
        self.assertTrue(snapshot['owned_threads_stopped'])
        self.assertEqual({'poll', 'send', 'maintenance'}, set(snapshot['threads_alive']))
        self.assertEqual([], self.transport.calls)

    def test_readiness_failure_does_not_cancel_an_already_dispatched_original_send(self):
        supervisor = self.supervised(candidate_contract=self.contract)
        supervisor.maintenance_once()
        self.store.enqueue('original-send', 'synthetic body')
        original = self.outbox()['client_id']
        entered, release, failures = threading.Event(), threading.Event(), []
        def on_wire(endpoint, body):
            entered.set()
            if not release.wait(2):
                raise OSError('synthetic drain fixture blocked')
        self.transport.on_wire = on_wire
        def send():
            try:
                self.channel.send_once()
            except Exception as error:
                failures.append(type(error).__name__)
        thread = threading.Thread(target=send)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.rpc.fail_before = '/channel-carrier/inspect'
            with self.assertRaisesRegex(CarrierError, '^channel_renew_failed$'):
                supervisor.maintenance_once()
            self.assertFalse(supervisor.ready.is_set())
            self.assertTrue(thread.is_alive())
            release.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], failures)
            self.assertEqual('accepted', self.outbox()['status'])
            self.assertEqual(original, self.outbox()['client_id'])
            self.assertEqual(1, len(self.transport.calls))
        finally:
            release.set()
            thread.join(2)

    def test_bounded_stop_does_not_claim_blocked_rpc_quiescent_or_enable_transport(self):
        supervisor = self.supervised(candidate_contract=self.contract)
        self.rpc.block = '/channel-carrier/renew'
        supervisor.start()
        self.assertTrue(self.rpc.entered.wait(1))
        self.assertFalse(supervisor.stop(0.01))
        self.assertFalse(supervisor.snapshot()['owned_threads_stopped'])
        self.assertFalse(supervisor.ready.is_set())
        self.assertEqual([], self.transport.calls)
        self.rpc.release.set()
        self.assertTrue(supervisor.stop(1))
        self.assertFalse(supervisor.ready.is_set())

    def test_maintenance_retry_wait_is_bounded_and_diagnostic_stays_local(self):
        supervisor = self.supervised(candidate_contract=self.contract)
        self.rpc.fail_before = '/channel-carrier/inspect'
        self.channel.stop = ScriptStop(10)
        supervisor.run_maintenance()
        self.assertEqual(60, max(self.channel.stop.delays))
        self.assertEqual('channel_renew_failed', supervisor.diagnostics.snapshot()['maintenance']['code'])
        self.assertEqual(10, supervisor.diagnostics.snapshot()['maintenance']['failures'])
        self.assertFalse(supervisor.ready.is_set())
        self.assertTrue(all(path == '/channel-carrier/inspect' for path in self.rpc.actions))

    def test_invalid_backoff_and_nonfixed_diagnostics_rejected(self):
        for value in (0, -1, True, float('nan'), 61):
            with self.assertRaises(ValueError):
                BoundedBackoff(value)
        with self.assertRaises(ValueError):
            LocalDiagnostics().record('poll', 'private arbitrary exception text')


if __name__ == '__main__':
    unittest.main()
