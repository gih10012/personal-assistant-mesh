"""Synthetic shared witness / two authenticated clients; no real channel IO."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path

from assistant_mesh.channel_carrier import CarrierError, CarrierFence, CarrierWitness, PROTOCOL
from assistant_mesh.networking import digest
from assistant_mesh.store import Store


class BoundClient:
    def __init__(self, witness, actor):
        self.witness, self.actor = witness, actor
        self.fail_before = None
        self.fail_after = None
        self.tamper = None
        self.requests = []

    def request(self, path, payload):
        action = path.split('/')[-1]
        self.requests.append(action)
        if self.fail_before == action:
            raise OSError('private credential and chat must not be printed')
        result = self.witness.request(self.actor, action, payload)
        if self.fail_after == action:
            raise OSError('lost acknowledgement, private credential and chat')
        return self.tamper(action, result) if self.tamper else result


class ChannelCarrierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.path = Path(self.tmp.name) / 'witness.db'
        self.store = Store(self.path, clock=lambda: self.now)
        self.binding = 'a' * 64
        self.witness = CarrierWitness(self.store, 'owner-witness', self.handoff, self.operation_proof)
        self.witness.enroll(self.binding, 'owner-ref', 'cloud')
        self.announce('cloud')
        self.announce('laptop')
        self.term = self.witness.grant(self.binding, 0, 'cloud', 60, 'bootstrap-proof')
        self.client = BoundClient(self.witness, 'cloud')
        self.fence = CarrierFence(self.client, self.term)
        self.calls, self.persisted = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def announce(self, node, revision=1, available=True, private=True, ttl=300):
        contract = {'protocol': PROTOCOL, 'node': node, 'owner_ref': 'owner-ref',
            'account_binding': self.binding, 'canonical_authority': 'cloud',
            'private_credential_ref': 'synthetic-private-ref', 'revision': revision,
            'runtime': {'adapter': 'ilink/0.3.0', 'fence_protocol': PROTOCOL,
                        'transport_available': available, 'private_state': private}}
        return self.witness.candidate(node, contract, ttl)

    @staticmethod
    def handoff(before, holder, proof_ref):
        # TRUSTED SYNTHETIC verifier only. This is not evidence for deployment.
        return {'protocol': 'mesh-channel-handoff-proof/1',
            'account_binding': before['account_binding'], 'canonical_authority': before['canonical_authority'],
            'from_holder': before['holder'], 'from_epoch': before['epoch'], 'to_holder': holder,
            'proof_ref': proof_ref, 'checkpoint_revision': before['checkpoint_revision'] + 1,
            'checkpoint_sha256': digest(['synthetic-closed-state', before['epoch']]),
            'migration_ready': True, 'original_history_closed': True, 'original_execution_closed': True,
            'previous_carrier_stopped': True, 'external_operations_quiescent': True}

    @staticmethod
    def operation_proof(receipt, proof_ref):
        from assistant_mesh.channel_carrier import _TICKET_KEYS
        return {'protocol': 'mesh-channel-operation-proof/1',
            'ticket_sha256': digest({key: receipt[key] for key in _TICKET_KEYS}),
            'proof_ref': proof_ref, 'external_operation_quiescent': True,
            'admitted_caller_quiescent': True,
            'original_identity_preserved': True, 'canonical_persistence_complete': True,
            'persistence_sha256': digest('synthetic-preserved-original-unknown-row')}

    @staticmethod
    def send_identity(outbox='original-outbox', client='original-client'):
        return {'authority': 'cloud', 'outbox_id': outbox, 'client_id': client}

    @staticmethod
    def poll_identity(cursor='original-cursor'):
        return {'authority': 'cloud', 'cursor_sha256': digest(cursor)}

    def run_operation(self, operation='operation-one', kind='send', identity=None, fence=None, call=None, persist=None):
        def send():
            self.calls.append(operation)
            return {'message_id': 'synthetic-provider-accept', 'delivery_verified': False}

        def save(result, ticket):
            self.persisted.append((result, ticket))
            return digest(['canonical-original-receipt', operation])

        return (fence or self.fence).run(operation, kind,
            identity or (self.send_identity() if kind == 'send' else self.poll_identity()),
            digest(['concrete-wire-request', operation]), call or send, persist or save)

    def begin(self, operation='operation-one', kind='send', identity=None):
        return self.witness.begin('cloud', self.term, operation, kind,
            identity or (self.send_identity() if kind == 'send' else self.poll_identity()), digest(operation))

    @staticmethod
    def ticket(receipt):
        return CarrierFence._ticket(receipt)

    def intent(self, operation='operation-one'):
        with self.store.transaction() as db:
            return dict(db.execute('SELECT * FROM channel_carrier_intents WHERE operation_id=?',
                                   (operation,)).fetchone())

    def test_candidate_is_not_holder_and_no_receiver_or_credential_loading(self):
        result = self.announce('laptop', 2)
        self.assertFalse(result['holder_granted'])
        self.assertEqual('cloud', self.witness.inspect(self.binding)['holder'])
        self.assertEqual([], self.calls)
        self.assertFalse(hasattr(self.witness, 'transport'))

    def test_candidate_must_be_bound_to_authenticated_actor_owner_and_authority(self):
        with self.store.transaction() as db:
            import json
            contract = json.loads(db.execute("SELECT contract FROM channel_carrier_candidates WHERE node='laptop'").fetchone()[0])
        for field, wrong in (('node', 'someone-else'), ('owner_ref', 'wrong-owner'),
                             ('canonical_authority', 'laptop'), ('account_binding', 'b' * 64)):
            changed = copy.deepcopy(contract)
            changed[field] = wrong
            with self.assertRaises(CarrierError):
                self.witness.candidate('laptop', changed)

    def test_candidate_revision_cannot_rewrite_same_revision_or_regress(self):
        self.announce('laptop', 2)
        with self.assertRaisesRegex(CarrierError, 'carrier_candidate_revision_conflict'):
            self.announce('laptop', 1)
        with self.assertRaisesRegex(CarrierError, 'carrier_candidate_revision_conflict'):
            self.announce('laptop', 2, available=False)
        self.announce('laptop', 3, available=False)
        self.witness.freeze(self.binding, 1)
        with self.assertRaisesRegex(CarrierError, 'carrier_candidate_unavailable'):
            self.witness.grant(self.binding, 1, 'laptop', 60, 'proof')

    def test_default_witness_has_no_grant_or_unknown_resolution_verifier(self):
        plain = CarrierWitness(self.store, 'owner-witness')
        plain.freeze(self.binding, 1)
        with self.assertRaisesRegex(CarrierError, 'carrier_handoff_verifier_missing'):
            plain.grant(self.binding, 1, 'laptop', 60, 'proof')
        with self.assertRaisesRegex(CarrierError, 'carrier_operation_verifier_missing'):
            plain.reconcile(self.binding, 'operation-one', 'proof')

    def test_success_exactly_one_dispatch_then_canonical_persistence(self):
        result = self.run_operation()
        self.assertFalse(result['delivery_verified'])
        self.assertEqual(['operation-one'], self.calls)
        self.assertEqual(1, len(self.persisted))
        self.assertEqual('cloud', self.persisted[0][1]['canonical_authority'])
        self.assertEqual(['begin', 'dispatch', 'returned', 'commit_authorize', 'committed'], self.client.requests)
        self.assertEqual('committed', self.intent()['state'])
        self.assertEqual(digest(self.send_identity()), self.intent()['identity_sha256'])

    def test_repeat_operation_never_calls_external_or_persist_again(self):
        self.run_operation()
        with self.assertRaises(CarrierError):
            self.run_operation()
        self.assertEqual(['operation-one'], self.calls)
        self.assertEqual(1, len(self.persisted))

    def test_original_send_cannot_be_reissued_under_a_new_operation_id(self):
        self.run_operation()
        with self.assertRaises(CarrierError):
            self.run_operation('renamed-operation')
        self.assertEqual(['operation-one'], self.calls)

    def test_unreachable_witness_has_no_cached_permission_to_send(self):
        self.client.fail_before = 'begin'
        with self.assertRaisesRegex(CarrierError, '^carrier_witness_unavailable$'):
            self.run_operation()
        self.assertEqual([], self.calls)
        self.assertEqual([], self.persisted)

    def test_begin_ack_loss_is_not_permission_to_dispatch_or_retry(self):
        self.client.fail_after = 'begin'
        with self.assertRaisesRegex(CarrierError, 'carrier_witness_unavailable'):
            self.run_operation()
        self.assertEqual('prepared', self.intent()['state'])
        self.client.fail_after = None
        with self.assertRaises(CarrierError):
            self.run_operation()
        self.assertEqual([], self.calls)
        self.witness.freeze(self.binding, 1)
        self.assertEqual('aborted', self.intent()['state'])

    def test_dispatch_ack_loss_is_durable_and_blocks_transfer_after_expiry(self):
        self.client.fail_after = 'dispatch'
        with self.assertRaisesRegex(CarrierError, 'carrier_witness_unavailable'):
            self.run_operation()
        self.assertEqual([], self.calls)
        self.assertEqual('dispatched', self.intent()['state'])
        self.now += 61
        self.witness.freeze(self.binding, 1)
        with self.assertRaisesRegex(CarrierError, 'carrier_unsettled_intent'):
            self.witness.grant(self.binding, 1, 'laptop', 60, 'stopped-proof')
        reopened = CarrierWitness(Store(self.path, clock=lambda: self.now), 'owner-witness', self.handoff)
        with self.assertRaisesRegex(CarrierError, 'carrier_unsettled_intent'):
            reopened.grant(self.binding, 1, 'laptop', 60, 'stopped-proof')

    def test_live_admitted_call_blocks_takeover_even_with_a_stopped_claim(self):
        def call():
            self.calls.append('live')
            self.now += 61
            self.witness.freeze(self.binding, 1)
            with self.assertRaisesRegex(CarrierError, 'carrier_unsettled_intent'):
                self.witness.grant(self.binding, 1, 'laptop', 60, 'synthetic-stopped-claim')
            return {'known': 'returned'}
        self.run_operation(call=call)
        self.assertEqual('committed', self.intent()['state'])
        self.assertEqual('cloud', self.witness.inspect(self.binding)['holder'])
        new = self.witness.grant(self.binding, 1, 'laptop', 60, 'verified-handoff')
        self.assertEqual(2, new['epoch'])

    def test_reply_not_yet_canonically_persisted_is_still_a_handoff_blocker(self):
        first = self.begin()
        dispatched = self.witness.dispatch('cloud', self.ticket(first))
        ticket = self.ticket(dispatched)
        self.witness.transition('cloud', ticket, 'returned', digest('original-reply'))
        self.witness.freeze(self.binding, 1)
        with self.assertRaisesRegex(CarrierError, 'carrier_unsettled_intent'):
            self.witness.grant(self.binding, 1, 'laptop', 60, 'proof')

    def test_lost_commit_authorization_never_calls_persist(self):
        self.client.fail_after = 'commit_authorize'
        with self.assertRaisesRegex(CarrierError, 'carrier_persistence_unconfirmed'):
            self.run_operation()
        self.assertEqual(['operation-one'], self.calls)
        self.assertEqual([], self.persisted)
        self.assertEqual('unknown', self.intent()['state'])
        self.client.fail_after = None
        with self.assertRaises(CarrierError):
            self.run_operation()
        self.assertEqual([], self.persisted)

    def test_persistence_ack_loss_does_not_duplicate_effect_or_local_commit(self):
        self.client.fail_after = 'committed'
        with self.assertRaisesRegex(CarrierError, 'carrier_persistence_unconfirmed'):
            self.run_operation()
        self.assertEqual('committed', self.intent()['state'])
        self.assertEqual(1, len(self.persisted))
        self.client.fail_after = None
        with self.assertRaises(CarrierError):
            self.run_operation()
        self.assertEqual(['operation-one'], self.calls)
        self.assertEqual(1, len(self.persisted))

    def test_transport_exception_fixed_error_preserves_unknown_original_identity(self):
        def fail():
            self.calls.append('failed')
            raise OSError('private-token secret chat')
        with self.assertRaisesRegex(CarrierError, '^carrier_transport_unknown$'):
            self.run_operation(call=fail)
        self.assertEqual('unknown', self.intent()['state'])
        self.assertEqual(digest(self.send_identity()), self.intent()['identity_sha256'])
        self.assertIsNone(self.intent()['persistence_sha256'])
        self.witness.freeze(self.binding, 1)
        with self.assertRaisesRegex(CarrierError, 'carrier_unsettled_intent'):
            self.witness.grant(self.binding, 1, 'laptop', 60, 'proof')

    def test_unknown_send_does_not_block_unrelated_new_send_under_same_holder(self):
        def fail():
            raise OSError('timeout')
        with self.assertRaises(CarrierError):
            self.run_operation(call=fail)
        with self.assertRaises(CarrierError):
            self.run_operation('new-id-for-old-send')
        self.run_operation('genuinely-new-send', identity=self.send_identity('new-outbox', 'new-client'))
        self.assertEqual('unknown', self.intent()['state'])
        self.assertEqual('committed', self.intent('genuinely-new-send')['state'])

    def test_unknown_poll_prevents_overlapping_pull_from_same_cursor(self):
        def fail():
            raise OSError('timeout')
        with self.assertRaises(CarrierError):
            self.run_operation(kind='poll', call=fail)
        with self.assertRaises(CarrierError):
            self.run_operation('next-poll', kind='poll')
        self.assertEqual([], self.calls)

    def test_completed_empty_poll_can_repeat_cursor_but_not_operation(self):
        self.run_operation(kind='poll')
        self.run_operation('next-poll', kind='poll')
        self.assertEqual(['operation-one', 'next-poll'], self.calls)

    def test_persistence_failure_keeps_reply_digest_and_no_transfer(self):
        def fail(result, ticket):
            raise OSError('private canonical DB failure')
        with self.assertRaisesRegex(CarrierError, '^carrier_persistence_unconfirmed$'):
            self.run_operation(persist=fail)
        self.assertEqual('unknown', self.intent()['state'])
        self.assertIsNotNone(self.intent()['outcome_sha256'])
        self.witness.freeze(self.binding, 1)
        with self.assertRaisesRegex(CarrierError, 'carrier_unsettled_intent'):
            self.witness.grant(self.binding, 1, 'laptop', 60, 'proof')

    def test_expiry_without_frozen_verified_handoff_is_not_takeover(self):
        self.now += 61
        with self.assertRaisesRegex(CarrierError, 'carrier_handoff_not_frozen'):
            self.witness.grant(self.binding, 1, 'laptop', 60, 'ping-failed')
        with self.assertRaises(CarrierError):
            self.run_operation()
        with self.assertRaisesRegex(CarrierError, 'carrier_admission_closed'):
            self.witness.renew('cloud', self.term)

    def test_checkpoint_audit_false_cannot_grant_even_after_freeze(self):
        self.witness.freeze(self.binding, 1)
        def audit_only(before, holder, proof_ref):
            proof = self.handoff(before, holder, proof_ref)
            proof['migration_ready'] = False
            return proof
        self.witness.handoff_verifier = audit_only
        with self.assertRaisesRegex(CarrierError, 'carrier_handoff_proof_invalid'):
            self.witness.grant(self.binding, 1, 'laptop', 60, 'audit-only')

    def test_missing_quiescence_or_history_closure_cannot_grant(self):
        self.witness.freeze(self.binding, 1)
        for missing in ('original_history_closed', 'original_execution_closed',
                        'previous_carrier_stopped', 'external_operations_quiescent'):
            def wrong(before, holder, proof_ref):
                proof = self.handoff(before, holder, proof_ref)
                proof[missing] = False
                return proof
            self.witness.handoff_verifier = wrong
            with self.assertRaisesRegex(CarrierError, 'carrier_handoff_proof_invalid'):
                self.witness.grant(self.binding, 1, 'laptop', 60, 'proof')

    def test_frozen_prepared_ticket_cannot_late_dispatch(self):
        prepared = self.begin()
        self.witness.freeze(self.binding, 1)
        self.witness.grant(self.binding, 1, 'laptop', 60, 'proof')
        with self.assertRaisesRegex(CarrierError, 'carrier_admission_closed'):
            self.witness.dispatch('cloud', self.ticket(prepared))
        self.assertEqual('aborted', self.intent()['state'])

    def test_new_carrier_reuses_original_authority_and_rejects_stale_old_holder(self):
        self.witness.freeze(self.binding, 1)
        new = self.witness.grant(self.binding, 1, 'laptop', 60, 'proof')
        with self.assertRaises(CarrierError):
            self.run_operation()
        fence = CarrierFence(BoundClient(self.witness, 'laptop'), new)
        self.run_operation('new-carrier-send', fence=fence)
        self.assertEqual('cloud', self.intent('new-carrier-send')['canonical_authority'])
        self.assertEqual('laptop', self.intent('new-carrier-send')['holder'])
        self.assertEqual(2, self.intent('new-carrier-send')['epoch'])
        with self.assertRaises(CarrierError):
            self.run_operation('wrong-authority', fence=fence, identity={'authority': 'laptop',
                     'outbox_id': 'another', 'client_id': 'another-client'})

    def test_unknown_resolution_requires_verified_quiescence_and_preserves_send_dedupe(self):
        def fail():
            raise OSError('timeout')
        with self.assertRaises(CarrierError):
            self.run_operation(call=fail)
        self.witness.freeze(self.binding, 1)
        def wrong(receipt, ref):
            proof = self.operation_proof(receipt, ref)
            proof['external_operation_quiescent'] = False
            return proof
        self.witness.operation_verifier = wrong
        with self.assertRaisesRegex(CarrierError, 'carrier_operation_proof_invalid'):
            self.witness.reconcile(self.binding, 'operation-one', 'original-reconcile')
        self.witness.operation_verifier = self.operation_proof
        self.witness.reconcile(self.binding, 'operation-one', 'original-reconcile')
        new = self.witness.grant(self.binding, 1, 'laptop', 60, 'proof')
        fence = CarrierFence(BoundClient(self.witness, 'laptop'), new)
        with self.assertRaises(CarrierError):
            self.run_operation('relabel-old-unknown', fence=fence)
        self.assertEqual([], self.calls)

    def test_no_active_http_is_not_proof_that_a_paused_admitted_caller_cannot_resume(self):
        receipt = self.begin()
        self.witness.dispatch('cloud', self.ticket(receipt))
        self.witness.freeze(self.binding, 1)
        def paused(receipt, ref):
            proof = self.operation_proof(receipt, ref)
            proof['admitted_caller_quiescent'] = False
            return proof
        self.witness.operation_verifier = paused
        with self.assertRaisesRegex(CarrierError, 'carrier_operation_proof_invalid'):
            self.witness.reconcile(self.binding, 'operation-one', 'no-active-http-only')
        self.assertEqual('dispatched', self.intent()['state'])

    def test_only_one_concurrent_dispatch_at_shared_witness(self):
        clients = [BoundClient(self.witness, 'cloud'), BoundClient(self.witness, 'cloud')]
        started, release, barrier = threading.Event(), threading.Event(), threading.Barrier(3)
        errors, calls = [], []
        def external():
            calls.append('external')
            started.set()
            self.assertTrue(release.wait(10))
            return {'accepted': True}
        def worker(index):
            barrier.wait()
            try:
                self.run_operation('racing-' + str(index), fence=CarrierFence(clients[index], self.term), call=external)
            except CarrierError as exc:
                errors.append(str(exc))
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        self.assertTrue(started.wait(10))
        release.set()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(['external'], calls)
        self.assertEqual(1, len(errors))

    def test_two_concurrent_grants_cas_only_one_wins(self):
        self.witness.freeze(self.binding, 1)
        barrier = threading.Barrier(2)
        original = self.witness.handoff_verifier
        def verify(before, holder, proof_ref):
            result = original(before, holder, proof_ref)
            barrier.wait(10)
            return result
        self.witness.handoff_verifier = verify
        results, errors = [], []
        def grant(holder):
            try:
                results.append(self.witness.grant(self.binding, 1, holder, 60, 'proof-' + holder))
            except CarrierError as exc:
                errors.append(str(exc))
        threads = [threading.Thread(target=grant, args=(holder,)) for holder in ('laptop', 'cloud')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(results))
        self.assertEqual(['carrier_term_conflict'], errors)

    def test_api_actor_cannot_be_supplied_by_payload_or_access_operator_grants(self):
        with self.assertRaisesRegex(CarrierError, 'carrier_protocol_invalid'):
            self.witness.request('laptop', 'dispatch', {'actor': 'cloud', 'ticket': {}})
        with self.assertRaisesRegex(CarrierError, 'carrier_route_not_authorized'):
            self.witness.request('laptop', 'grant', {})
        with self.assertRaises(CarrierError):
            CarrierFence(BoundClient(self.witness, 'laptop'), self.term).run('impersonated', 'send',
                self.send_identity(), digest('request'), lambda: self.calls.append('bad'), lambda r, t: digest('persist'))
        self.assertEqual([], self.calls)

    def test_wrong_witness_or_replaced_ledger_incarnation_has_no_cached_right(self):
        other_store = Store(Path(self.tmp.name) / 'unrelated.db', clock=lambda: self.now)
        other = CarrierWitness(other_store, 'owner-witness', self.handoff)
        other.enroll(self.binding, 'owner-ref', 'cloud')
        self.assertNotEqual(self.witness.incarnation, other.incarnation)
        with self.assertRaises(CarrierError):
            self.run_operation(fence=CarrierFence(BoundClient(other, 'cloud'), self.term))
        self.assertEqual([], self.calls)
        with self.assertRaisesRegex(CarrierError, 'carrier_witness_identity_changed'):
            CarrierWitness(self.store, 'different-witness')

    def test_tampered_dispatch_receipt_never_calls_external(self):
        def tamper(action, result):
            if action == 'dispatch':
                result['holder'] = 'laptop'
            return result
        self.client.tamper = tamper
        with self.assertRaisesRegex(CarrierError, 'carrier_receipt_invalid'):
            self.run_operation()
        self.assertEqual([], self.calls)
        self.assertEqual('dispatched', self.intent()['state'])

    def test_clock_regression_rejects_shared_io_but_native_store_work_remains_available(self):
        self.now = 999
        with self.assertRaises(CarrierError):
            self.run_operation()
        self.assertEqual([], self.calls)
        self.store.create_task('new independent native work', ['agent'], task_id='local-new-work')
        with self.store.transaction() as db:
            row = db.execute("SELECT input FROM tasks WHERE id='local-new-work'").fetchone()
        self.assertEqual('new independent native work', row['input'])

    def test_unavailable_candidate_cannot_renew_and_foreign_holder_cannot_renew(self):
        self.announce('cloud', 2, available=False)
        with self.assertRaisesRegex(CarrierError, 'carrier_candidate_unavailable'):
            self.witness.renew('cloud', self.term)
        with self.assertRaises(CarrierError):
            self.witness.renew('laptop', self.term)

    def test_original_provider_rejection_is_outcome_not_delivery_or_auto_retry(self):
        self.run_operation(call=lambda: {'status': 'rejected', 'code': 'business_-2', 'delivery_verified': False})
        with self.assertRaises(CarrierError):
            self.run_operation('new-id-same-rejection')
        self.assertEqual(1, len(self.persisted))
        # Only an already-authorized original outbox retry with its original
        # NEW attempt client identity is distinct; the witness does not invent it.
        self.run_operation('original-allowed-retry', identity=self.send_identity(client='known-rejection-next-client'))
        self.assertEqual('committed', self.intent('original-allowed-retry')['state'])


if __name__ == '__main__':
    unittest.main()
