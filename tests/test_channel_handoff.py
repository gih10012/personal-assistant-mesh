"""Real private SQLite/RPC/thread handles; synthetic iLink, NOT live HA proof."""
import contextlib
import copy
import json
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from assistant_mesh.channel_authority import ChannelAuthority, validate_config
from assistant_mesh.channel_carrier import CarrierError, CarrierWitness
from assistant_mesh.channel_checkpoint import export_checkpoint, write_checkpoint
from assistant_mesh.channel_handoff import collect_readiness
from assistant_mesh.channel_supervisor import ChannelSupervisor
from assistant_mesh.networking import canonical
from assistant_mesh.remote_notifications import RemoteNotifications
from assistant_mesh.store import Store
from tests import test_channel_authority as authority_fixture
from tests import test_channel_fence_integration as fence_fixture


class ChannelHandoffTests(unittest.TestCase):
    def setUp(self):
        # Reuse real authenticated loopback HTTP fixture, not its assertions.
        self.fixture = authority_fixture.ChannelAuthorityTests()
        self.fixture.setUp()
        self.root, self.authority = self.fixture.root, self.fixture.authority
        self.store, self.witness = self.fixture.api.store, self.authority.witness
        self.binding = self.fixture.binding
        self.store.set('owner_context', {'token': 'synthetic-private-context',
            'version': 'original-context-version', 'created': 1})
        self.supervisor = None
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        if self.supervisor:
            self.assertTrue(self.supervisor.stop(3))
        self.fixture.tearDown()

    def collect(self, **changes):
        arguments = dict(database=self.store.path, canonical_authority='cloud',
            account_binding=self.binding, owner_ref='fixture-self',
            witness_id='fixture-witness', incarnation=self.witness.incarnation)
        arguments.update(changes)
        return collect_readiness(**arguments)

    def rows(self, table):
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]

    def dump(self):
        with self.store.transaction() as db:
            return list(db.iterdump())

    def checkpoint(self):
        artifact = export_checkpoint(self.store.path, 'cloud', self.binding, 'fixture-checkpoint')
        destination = self.root / 'checkpoint.json'
        write_checkpoint(artifact, destination, 'cloud', self.binding, artifact['sha256'])
        return destination, artifact['sha256']

    def completed_channel(self):
        channel, state = self.fixture.remote(self.fixture.grant())
        self.store.enqueue('original-send', 'synthetic complete private message')
        self.assertTrue(channel.send_once())
        self.assertEqual(0, channel.poll_once())
        self.witness.freeze(self.binding, channel.carrier_fence.term['epoch'])
        return channel, state

    def test_default_verifier_none_still_rejects_owner_grant(self):
        authority = ChannelAuthority(self.store, self.authority.config, 'cloud',
            self.fixture.api.ilink, self.binding)
        self.assertIsNone(authority.witness.handoff_verifier)
        self.assertIsNone(authority.witness.operation_verifier)
        self.fixture.request('candidate', {'contract': self.contract(), 'ttl': 300})
        with self.assertRaisesRegex(CarrierError, '^carrier_handoff_verifier_missing$'):
            authority.witness.grant(self.binding, 0, 'laptop', 60, 'owner-ref')
        self.assertEqual([], self.fixture.wire)

    def contract(self):
        return {'protocol': 'mesh-channel-carrier/1', 'node': 'laptop',
            'owner_ref': 'fixture-self', 'account_binding': self.binding,
            'canonical_authority': 'cloud', 'private_credential_ref': 'synthetic-reference',
            'revision': 1, 'runtime': {'adapter': 'ilink/0.3.0',
                'fence_protocol': 'mesh-channel-carrier/1',
                'transport_available': True, 'private_state': True}}

    def test_owner_python_hook_receives_actual_before_term(self):
        observed = []
        def owner_hook(before, holder, reference):
            observed.append((copy.deepcopy(before), holder, reference))
            # Synthetic fixture only: NOT a production closure verifier.
            return fence_fixture.ChannelFenceIntegrationTests.handoff(before, holder, reference)
        operation_hook = lambda *unused: None
        authority = ChannelAuthority(self.store, self.authority.config, 'cloud',
            self.fixture.api.ilink, self.binding, handoff_verifier=owner_hook,
            operation_verifier=operation_hook)
        self.fixture.request('candidate', {'contract': self.contract(), 'ttl': 300})
        before = authority.witness.inspect(self.binding)
        term = authority.witness.grant(self.binding, 0, 'laptop', 60, 'fixture-only-proof')
        self.assertEqual([(before, 'laptop', 'fixture-only-proof')], observed)
        self.assertEqual(1, term['epoch'])
        self.assertIs(operation_hook, authority.witness.operation_verifier)
        self.assertEqual([], self.fixture.wire)

    def test_noncallable_hook_rejected_before_any_witness_initialization(self):
        before = self.dump()
        with self.assertRaisesRegex(CarrierError, '^channel_owner_verifier_invalid$'):
            ChannelAuthority(self.store, self.authority.config, 'cloud', self.fixture.api.ilink,
                self.binding, handoff_verifier={'migration_ready': True})
        self.assertEqual(before, self.dump())

    def test_configuration_cannot_load_module_hook_or_posted_proof(self):
        for key in ('handoff_verifier', 'operation_verifier', 'verifier_module', 'proof'):
            config = dict(self.authority.config, **{key: 'some.module:grant'})
            with self.assertRaises(CarrierError):
                validate_config(config)
        for action in ('grant', 'freeze', 'reconcile', 'readiness'):
            with self.assertRaises(HTTPError) as error:
                self.fixture.request(action, {'migration_ready': True})
            self.assertEqual(403, error.exception.code)
            error.exception.close()

    def test_full_results_receipts_spools_and_pinned_snapshot_read_only(self):
        unused, state = self.completed_channel()
        path, pinned = self.checkpoint()
        before = self.dump()
        calls = list(self.fixture.wire)
        with patch.object(Store, '__init__', side_effect=AssertionError('no mutable Store')):
            with patch.object(CarrierWitness, 'inspect', side_effect=AssertionError('inspect writes clock')):
                report = self.collect(checkpoint_path=path, checkpoint_sha256=pinned,
                                      spool_paths=[state.journal_path])
        self.assertEqual(before, self.dump())
        self.assertEqual(calls, self.fixture.wire)
        checks = report['checks']
        self.assertEqual('checked', checks['original_witness']['state'])
        self.assertEqual(2, checks['full_canonical_intents_and_receipts']['durable_receipts_recomputed'])
        self.assertEqual(2, checks['private_original_result_spools']['full_outcomes_matched'])
        self.assertEqual(0, checks['private_original_result_spools']['canonical_dispatches_not_in_supplied_spools'])
        self.assertEqual('checked', checks['externally_pinned_audit_checkpoint']['state'])
        self.assertFalse(report['migration_ready'])
        self.assertFalse(report['grant_authorized'])
        self.assertFalse(report['native_features_restricted'])
        self.assertIn('independent_external_and_admitted_caller_quiescence', report['missing'])
        serialized = json.dumps(report)
        for secret in ('synthetic complete private message', 'synthetic-private-context', 'next-cursor'):
            self.assertNotIn(secret, serialized)

    def test_pin_missing_wrong_or_stale_never_becomes_migration_proof(self):
        self.completed_channel()
        path, pin = self.checkpoint()
        self.assertIn('externally_pinned_audit_checkpoint', self.collect(checkpoint_path=path)['missing'])
        bad = self.collect(checkpoint_path=path, checkpoint_sha256='b' * 64)
        self.assertEqual('checkpoint_integrity_mismatch',
            bad['checks']['externally_pinned_audit_checkpoint']['code'])
        self.store.enqueue('new-unrelated-local-work', 'new work remains allowed')
        stale = self.collect(checkpoint_path=path, checkpoint_sha256=pin)
        self.assertEqual('handoff_checkpoint_source_changed',
            stale['checks']['externally_pinned_audit_checkpoint']['code'])
        self.assertFalse(stale['migration_ready'])

    def test_wrong_witness_incarnation_is_not_accepted_from_matching_labels(self):
        report = self.collect(incarnation='c' * 64)
        self.assertEqual('handoff_original_witness_binding_changed',
                         report['checks']['original_witness']['code'])
        self.assertIn('full_canonical_intents_and_receipts', report['missing'])

    def test_legacy_authority_has_missing_witness_without_initialization(self):
        temporary = self.root / 'legacy'
        temporary.mkdir(mode=0o700)
        store = Store(temporary / 'legacy.db')
        RemoteNotifications(store, 'cloud', self.binding)
        store.set('cursor', 'original-legacy-cursor')
        with store.transaction() as db:
            before = list(db.iterdump())
        report = self.collect(database=store.path)
        self.assertEqual('handoff_original_witness_missing', report['checks']['original_witness']['code'])
        with store.transaction() as db:
            self.assertEqual(before, list(db.iterdump()))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='channel_carrier_witness'").fetchone())

    def test_corrupted_full_canonical_receipt_is_missing_not_self_attested(self):
        self.completed_channel()
        with self.store.transaction() as db:
            db.execute("UPDATE channel_canonical_intents SET persistence_sha256=?", ('e' * 64,))
        report = self.collect()
        self.assertEqual('handoff_original_persistence_receipt_changed',
                         report['checks']['full_canonical_intents_and_receipts']['code'])

    def test_tampered_private_spool_does_not_match_original_full_outcome(self):
        unused, state = self.completed_channel()
        with contextlib.closing(sqlite3.connect(state.journal_path)) as db, db:
            db.execute("UPDATE original_channel_results SET outcome=? WHERE operation_id="
                "(SELECT operation_id FROM original_channel_results LIMIT 1)",
                (canonical({'status': 'unknown', 'detail': {'code': 'invented'}}),))
        report = self.collect(spool_paths=[state.journal_path])
        self.assertEqual('handoff_spool_result_invalid',
                         report['checks']['private_original_result_spools']['code'])
        self.assertEqual('checked', report['checks']['managed_original_intents_closed']['state'])

    def test_unknown_outcome_and_full_spool_remain_blocking(self):
        channel, state = self.fixture.remote(self.fixture.grant())
        self.store.enqueue('original-send', 'unknown is not a new pending task')
        from assistant_mesh.channel import ChannelError
        self.fixture.api.ilink.request_override = lambda *unused: (_ for _ in ()).throw(ChannelError('network_timeout'))
        self.assertTrue(channel.send_once())  # Original normalized unknown was persisted.
        self.witness.freeze(self.binding, channel.carrier_fence.term['epoch'])
        before = self.dump()
        report = self.collect(spool_paths=[state.journal_path])
        self.assertEqual(before, self.dump())
        self.assertEqual(1, report['checks']['full_canonical_intents_and_receipts']['unclosed_canonical_intents'])
        self.assertEqual('missing', report['checks']['managed_original_intents_closed']['state'])
        self.assertEqual('unknown', self.fixture.outbox()['status'])

    def test_authoritative_reconciliation_closes_intent_but_retains_unknown(self):
        channel, state = self.fixture.remote(self.fixture.grant())
        self.store.enqueue('original-send', 'retain original unknown evidence')
        from assistant_mesh.channel import ChannelError
        from tests.test_channel_carrier import ChannelCarrierTests
        self.fixture.api.ilink.request_override = lambda *unused: (_ for _ in ()).throw(ChannelError('network_timeout'))
        self.assertTrue(channel.send_once())
        self.witness.freeze(self.binding, channel.carrier_fence.term['epoch'])
        operation = self.rows('channel_canonical_intents')[0]['operation_id']
        original = self.authority.state.receipt(operation)
        def synthetic_independent_proof(receipt, proof_ref):
            proof = ChannelCarrierTests.operation_proof(receipt, proof_ref)
            proof['persistence_sha256'] = original['persistence_sha256']
            return proof
        self.witness.operation_verifier = synthetic_independent_proof
        self.witness.reconcile(self.binding, operation, 'fixture-original-quiescence')
        before = self.dump()
        report = self.collect(spool_paths=[state.journal_path])
        self.assertEqual(before, self.dump())
        self.assertEqual(0, report['checks']['full_canonical_intents_and_receipts']['unclosed_canonical_intents'])
        self.assertEqual('checked', report['checks']['managed_original_intents_closed']['state'])
        self.assertEqual(original, self.authority.state.receipt(operation))
        self.assertEqual('unknown', self.fixture.outbox()['status'])
        self.assertFalse(report['grant_authorized'])
        self.assertEqual('missing', report['checks']['independent_external_and_admitted_caller_quiescence']['state'])

    def test_live_owned_caller_stop_timeout_not_natural_return(self):
        channel, state = self.fixture.remote(self.fixture.grant())
        entered = threading.Event()
        def wire(endpoint, body, timeout):
            if endpoint == 'getupdates':
                entered.set()
                if not self.release.wait(3):
                    raise AssertionError('bounded synthetic callback was not released')
                return {'msgs': [], 'get_updates_buf': 'naturally-returned-original-cursor'}
            return {'message_id': 'synthetic'}
        self.fixture.api.ilink.request_override = wire
        self.supervisor = ChannelSupervisor(channel, renew_seconds=60, renew_interval=20)
        self.supervisor.start()
        self.assertTrue(entered.wait(3))
        self.assertFalse(self.supervisor.stop(0))
        self.witness.freeze(self.binding, channel.carrier_fence.term['epoch'])
        pending = self.collect(caller=self.supervisor, spool_paths=[state.journal_path])
        self.assertGreater(pending['checks']['actual_owned_caller_lifecycle']['threads_still_alive'], 0)
        self.assertEqual('missing', pending['checks']['owned_caller_thread_return']['state'])
        self.assertEqual('missing', pending['checks']['managed_original_intents_closed']['state'])
        self.release.set()
        self.assertTrue(self.supervisor.stop(3))
        returned = self.collect(caller=self.supervisor, spool_paths=[state.journal_path])
        self.assertEqual('checked', returned['checks']['owned_caller_thread_return']['state'])
        self.assertEqual('checked', returned['checks']['managed_original_intents_closed']['state'])
        self.assertEqual(1, returned['checks']['private_original_result_spools']['full_outcomes_matched'])
        self.assertFalse(returned['migration_ready'])
        self.assertFalse(returned['checks']['owned_caller_thread_return']['external_operations_quiescent'])

    def test_json_snapshot_pid_or_unstarted_supervisor_is_not_owned_caller(self):
        for caller in ({'owned_threads_stopped': True}, {'pid': None}, True):
            report = self.collect(caller=caller)
            self.assertEqual('handoff_owned_caller_handle_missing',
                             report['checks']['actual_owned_caller_lifecycle']['code'])
        channel, unused = self.fixture.remote(self.fixture.grant())
        unstarted = ChannelSupervisor(channel)
        report = self.collect(caller=unstarted)
        self.assertEqual('handoff_owned_caller_handle_missing',
                         report['checks']['actual_owned_caller_lifecycle']['code'])

    def test_read_failures_limits_and_missing_file_are_fixed_and_nonmutating(self):
        before = self.dump()
        report = self.collect(max_bytes=1)
        self.assertEqual('checkpoint_limit_exceeded', report['checks']['consistent_canonical_state']['code'])
        report = self.collect(database=self.root / 'does-not-exist.db')
        self.assertNotIn(str(self.root), json.dumps(report))
        self.assertFalse((self.root / 'does-not-exist.db').exists())
        self.assertEqual(before, self.dump())


if __name__ == '__main__':
    unittest.main()
