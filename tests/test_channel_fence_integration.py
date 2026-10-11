"""Actual Channel + Store/facade + witness + synthetic iLink wire callback.

No credentials, real HTTP, SSH, receiver threads, or production state involved.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.channel import Channel, ChannelError, ILink
from assistant_mesh.channel_carrier import CarrierError, CarrierFence, CarrierWitness, PROTOCOL
from assistant_mesh.channel_state import ChannelState
from assistant_mesh.networking import digest
from assistant_mesh.remote_notifications import PROTOCOL as NOTIFY_PROTOCOL
from assistant_mesh.store import Store


class BoundWitnessClient:
    def __init__(self, witness, actor='cloud'):
        self.witness, self.actor = witness, actor
        self.before, self.after = None, None
        self.actions = []
        self.on_action = None

    def request(self, path, payload):
        action = path.rsplit('/', 1)[-1]
        self.actions.append(action)
        if self.before == action:
            raise OSError('synthetic private connection detail')
        if self.on_action:
            self.on_action(action, payload)
        receipt = self.witness.request(self.actor, action, payload)
        if self.after == action:
            raise OSError('synthetic lost acknowledgement')
        return receipt


class SyntheticILink(ILink):
    def __init__(self):
        # Deliberately do not invoke ILink.__init__ or load an account file.
        self.base = 'https://synthetic-fixture.weixin.qq.com'
        self.account = {'ilink_user_id': 'fixture-owner', 'ilink_bot_id': 'fixture-bot',
                        'pending': [], 'cursor': 'never-import-this-account-cursor'}
        self.calls = []
        self.error = None
        self.batch = {'msgs': [], 'get_updates_buf': 'next-canonical-cursor'}
        self.on_wire = None
        self.request_override = self.wire

    def wire(self, endpoint, body, timeout):
        self.calls.append((endpoint, body, timeout))
        if self.on_wire:
            self.on_wire(endpoint, body)
        if self.error:
            raise self.error
        return self.batch if endpoint == 'getupdates' else {'message_id': 'fixture-server-receipt', 'ret': 0}


class ChannelFenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.path = Path(self.tmp.name) / 'original-authority.db'
        self.store = Store(self.path, clock=lambda: self.now)
        self.transport = SyntheticILink()
        self.binding = digest([NOTIFY_PROTOCOL, self.transport.base,
            self.transport.account['ilink_user_id'], self.transport.account['ilink_bot_id']])
        self.store.set('owner_notification_channel_binding', self.binding)
        self.store.set('cursor', 'original-canonical-cursor')
        self.store.set('owner_context', {'version': 'original-context-version',
            'token': 'synthetic-original-private-context', 'created': 1})
        self.witness = CarrierWitness(self.store, 'fixture-witness', self.handoff)
        self.witness.enroll(self.binding, 'owner-ref', 'cloud')
        self.witness.candidate('cloud', {'protocol': PROTOCOL, 'node': 'cloud', 'owner_ref': 'owner-ref',
            'account_binding': self.binding, 'canonical_authority': 'cloud',
            'private_credential_ref': 'synthetic-private-ref', 'revision': 1,
            'runtime': {'adapter': 'ilink/0.3.0', 'fence_protocol': PROTOCOL,
                        'transport_available': True, 'private_state': True}}, 300)
        self.term = self.witness.grant(self.binding, 0, 'cloud', 60, 'synthetic-bootstrap')
        self.client = BoundWitnessClient(self.witness)
        self.fence = CarrierFence(self.client, self.term)
        self.state = ChannelState(self.store, self.witness, account=self.transport.account)
        self.channel = Channel(self.state, self.transport, carrier_fence=self.fence)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def handoff(before, holder, proof_ref):
        # Synthetic trusted verifier, never a real shutdown or migration claim.
        return {'protocol': 'mesh-channel-handoff-proof/1', 'account_binding': before['account_binding'],
            'canonical_authority': before['canonical_authority'], 'from_holder': before['holder'],
            'from_epoch': before['epoch'], 'to_holder': holder, 'proof_ref': proof_ref,
            'checkpoint_revision': before['checkpoint_revision'] + 1,
            'checkpoint_sha256': digest(['fixture-closed-state', before['epoch']]),
            'migration_ready': True, 'original_history_closed': True, 'original_execution_closed': True,
            'previous_carrier_stopped': True, 'external_operations_quiescent': True}

    def message(self):
        return {'message_id': 'fixture-original-owner-message',
            'from_user_id': 'fixture-owner', 'to_user_id': 'fixture-bot', 'message_type': 1,
            'create_time_ms': 2000, 'context_token': 'synthetic-fresh-private-context',
            'item_list': [{'type': 1, 'text_item': {'text': 'synthetic native work'}}]}

    def rows(self, table):
        # Fixed fixture table names only, not untrusted payloads.
        with self.store.transaction() as db:
            return [dict(r) for r in db.execute('SELECT * FROM ' + table)]

    def witness_intents(self):
        return self.rows('channel_carrier_intents')

    def enqueue(self, identity='original-outbox'):
        self.store.enqueue(identity, 'synthetic owner reply')
        return self.rows('outbox')[0]['client_id']

    def test_default_channel_unchanged_import_and_send_without_witness(self):
        source = Store(Path(self.tmp.name) / 'legacy.db', clock=lambda: self.now)
        transport = SyntheticILink()
        transport.account['pending'] = [self.message()]
        legacy = Channel(source, transport)
        self.assertEqual('never-import-this-account-cursor', source.get('cursor'))
        self.assertEqual(1, len(source.inbox()['items']))
        self.assertTrue(legacy.send_once())
        with source.transaction() as db:
            original = dict(db.execute('SELECT * FROM outbox').fetchone())
        self.assertEqual('accepted', original['status'])
        self.assertEqual(original['client_id'], transport.calls[0][1]['msg']['client_id'])
        self.assertEqual([], self.client.actions)

    def test_fenced_constructor_does_not_import_account_pending_or_context(self):
        self.transport.account['pending'] = [self.message()]
        self.transport.account['owner_context'] = {'user_id': 'fixture-owner',
            'token': 'synthetic-account-import-must-not-win'}
        Channel(self.state, self.transport, self.fence)
        self.assertEqual([], self.rows('inbox'))
        self.assertEqual('original-canonical-cursor', self.store.get('cursor'))
        self.assertEqual('synthetic-original-private-context', self.store.get('owner_context')['token'])

    def test_fenced_constructor_requires_original_checkpoint_and_bound_account(self):
        blank = Store(Path(self.tmp.name) / 'blank.db', clock=lambda: self.now)
        with self.assertRaisesRegex(ChannelError, '^channel_fence_checkpoint_required$'):
            Channel(ChannelState(blank, CarrierWitness(blank, 'other-fixture-witness')), self.transport, self.fence)
        self.transport.account['ilink_user_id'] = 'different-owner'
        with self.assertRaisesRegex(ChannelError, '^channel_fence_account_mismatch$'):
            Channel(self.state, self.transport, self.fence)
        self.assertEqual([], self.transport.calls)
        self.assertEqual([], self.rows('inbox'))

    def test_send_passes_actual_original_client_context_wire_and_finish_through_fence(self):
        original = self.enqueue()
        with patch.object(self.store, 'next_send', side_effect=AssertionError('bare next_send forbidden')):
            self.assertTrue(self.channel.send_once())
        self.assertEqual(['begin', 'dispatch', 'returned', 'commit_authorize', 'committed'], self.client.actions)
        endpoint, body, timeout = self.transport.calls[0]
        self.assertEqual(('sendmessage', 15), (endpoint, timeout))
        self.assertEqual(original, body['msg']['client_id'])
        self.assertEqual('fixture-owner', body['msg']['to_user_id'])
        self.assertEqual('synthetic-original-private-context', body['msg']['context_token'])
        row = self.witness_intents()[0]
        self.assertEqual('committed', row['state'])
        self.assertEqual({'authority': 'cloud', 'outbox_id': 'original-outbox', 'client_id': original},
                         json.loads(row['identity']))
        self.assertEqual(digest({'endpoint': endpoint, 'body': body}), row['request_sha256'])
        self.assertEqual('accepted', self.store.send_status('original-outbox')['status'])
        self.assertFalse(self.store.send_status('original-outbox')['detail']['delivery_verified'])
        self.assertFalse(self.channel.send_once())
        self.assertEqual(1, len(self.transport.calls))

    def test_media_wire_fingerprint_uses_original_items_not_text_replacement(self):
        self.store.enqueue_media('original-outbox', [{'type': 2, 'image_item': {'media_ref': 'synthetic-private-reference'}}])
        self.channel.send_once()
        endpoint, body, _ = self.transport.calls[0]
        self.assertEqual(2, body['msg']['item_list'][0]['type'])
        self.assertEqual(digest({'endpoint': endpoint, 'body': body}), self.witness_intents()[0]['request_sha256'])

    def test_poll_commits_original_message_task_cursor_and_context_only_after_commit_permission(self):
        message = self.message()
        self.transport.batch['msgs'] = [message]
        def before(action, payload):
            if action in ('returned', 'commit_authorize'):
                self.assertEqual([], self.rows('inbox'))
                self.assertEqual('original-canonical-cursor', self.store.get('cursor'))
        self.client.on_action = before
        self.assertEqual(1, self.channel.poll_once())
        self.assertEqual('next-canonical-cursor', self.store.get('cursor'))
        self.assertEqual('synthetic-fresh-private-context', self.store.get('owner_context')['token'])
        original_id = hashlib.sha256(json.dumps(['fixture-bot', message['message_id'], None], sort_keys=True).encode()).hexdigest()
        self.assertEqual(original_id, self.rows('inbox')[0]['id'])
        self.assertEqual(original_id, self.rows('tasks')[0]['id'])
        self.assertEqual('committed', self.witness_intents()[0]['state'])
        self.assertEqual('cloud', self.witness_intents()[0]['canonical_authority'])

    def test_empty_poll_may_continue_same_cursor_as_new_real_operation(self):
        self.transport.batch['get_updates_buf'] = 'original-canonical-cursor'
        self.assertEqual(0, self.channel.poll_once())
        self.assertEqual(0, self.channel.poll_once())
        intents = self.witness_intents()
        self.assertEqual(2, len(intents))
        self.assertNotEqual(intents[0]['operation_id'], intents[1]['operation_id'])
        self.assertEqual(2, len(self.transport.calls))

    def test_begin_ack_loss_does_not_send_or_regenerate_original_identity(self):
        original = self.enqueue()
        self.client.after = 'begin'
        with self.assertRaisesRegex(ChannelError, '^channel_fence_unconfirmed$'):
            self.channel.send_once()
        self.assertEqual([], self.transport.calls)
        self.assertEqual(original, self.rows('outbox')[0]['client_id'])
        self.assertNotEqual('pending', self.store.send_status('original-outbox')['status'])
        self.client.after = None
        self.channel.send_once()
        self.assertEqual([], self.transport.calls)

    def test_dispatch_ack_loss_leaves_unsettled_witness_and_no_transport(self):
        self.enqueue()
        self.client.after = 'dispatch'
        with self.assertRaises(ChannelError):
            self.channel.send_once()
        self.assertEqual([], self.transport.calls)
        self.assertEqual('dispatched', self.witness_intents()[0]['state'])
        self.witness.freeze(self.binding, 1)
        with self.assertRaisesRegex(CarrierError, 'carrier_unsettled_intent'):
            self.witness.grant(self.binding, 1, 'cloud', 60, 'must-not-waive-old-ack')

    def test_send_witness_unavailable_does_not_fall_back_to_bare_transport(self):
        original = self.enqueue()
        self.client.before = 'begin'
        with self.assertRaises(ChannelError):
            self.channel.send_once()
        self.assertEqual([], self.transport.calls)
        self.assertEqual(original, self.rows('outbox')[0]['client_id'])
        self.store.create_task('independent native work', ['agent'], task_id='new-local-work')
        self.assertEqual('pending', self.store.task_status('new-local-work')['status'])

    def test_failed_local_dispatch_journal_prevents_actual_wire_call(self):
        self.enqueue()
        with patch.object(self.state, 'channel_mark_dispatched', side_effect=OSError('private local write failed')):
            with self.assertRaises(ChannelError):
                self.channel.send_once()
        self.assertEqual([], self.transport.calls)
        self.assertEqual('unknown', self.witness_intents()[0]['state'])

    def test_send_timeout_commits_original_unknown_but_retains_nonterminal_witness(self):
        original = self.enqueue()
        self.transport.error = ChannelError('network_timeout')
        self.assertTrue(self.channel.send_once())
        outbox = self.store.send_status('original-outbox')
        self.assertEqual('unknown', outbox['status'])
        self.assertEqual('network_timeout', outbox['detail']['code'])
        self.assertEqual(original, self.rows('outbox')[0]['client_id'])
        witness = self.witness_intents()[0]
        self.assertEqual('unknown', witness['state'])
        self.assertIsNotNone(witness['persistence_sha256'])
        self.assertEqual('committed_unknown', self.client.actions[-1])
        self.assertFalse(self.channel.send_once())
        self.assertEqual(1, len(self.transport.calls))
        self.witness.freeze(self.binding, 1)
        with self.assertRaisesRegex(CarrierError, 'carrier_unsettled_intent'):
            self.witness.grant(self.binding, 1, 'cloud', 60, 'socket-timeout-not-hard-deadline')

    def test_unrelated_send_can_proceed_under_same_holder_after_original_unknown(self):
        self.enqueue()
        self.transport.error = ChannelError('network_timeout')
        self.channel.send_once()
        self.transport.error = None
        self.store.enqueue('unrelated-new-outbox', 'synthetic new work reply')
        self.assertTrue(self.channel.send_once())
        self.assertEqual('unknown', self.store.send_status('original-outbox')['status'])
        self.assertEqual('accepted', self.store.send_status('unrelated-new-outbox')['status'])
        self.assertEqual(2, len(self.transport.calls))

    def test_known_context_rejection_and_auth_failure_are_not_transport_unknown(self):
        for code, status in (('business_-2', 'rejected'), ('auth_required', 'waiting_auth')):
            self.store.enqueue('known-' + code, 'synthetic known error reply')
            self.transport.error = ChannelError(code)
            self.assertTrue(self.channel.send_once())
            self.assertEqual(status, self.store.send_status('known-' + code)['status'])
            self.assertFalse(self.channel.send_once())
        self.assertTrue(all(row['state'] == 'committed' for row in self.witness_intents()))

    def test_commit_authorize_ack_loss_does_not_accept_or_retransmit_send(self):
        self.enqueue()
        self.client.after = 'commit_authorize'
        with self.assertRaises(ChannelError):
            self.channel.send_once()
        self.assertEqual(1, len(self.transport.calls))
        self.assertNotEqual('accepted', self.store.send_status('original-outbox')['status'])
        self.assertEqual('unknown', self.witness_intents()[0]['state'])
        self.client.after = None
        self.assertFalse(self.channel.send_once())
        self.assertEqual(1, len(self.transport.calls))

    def test_committed_ack_loss_preserves_actual_acceptance_and_never_resends(self):
        self.enqueue()
        self.client.after = 'committed'
        with self.assertRaises(ChannelError):
            self.channel.send_once()
        self.assertEqual('accepted', self.store.send_status('original-outbox')['status'])
        self.assertEqual('committed', self.witness_intents()[0]['state'])
        self.client.after = None
        self.assertFalse(self.channel.send_once())
        self.assertEqual(1, len(self.transport.calls))

    def test_poll_commit_authorize_ack_loss_preserves_old_cursor_and_creates_no_task(self):
        self.transport.batch['msgs'] = [self.message()]
        self.client.after = 'commit_authorize'
        with self.assertRaises(ChannelError):
            self.channel.poll_once()
        self.assertEqual([], self.rows('inbox'))
        self.assertEqual([], self.rows('tasks'))
        self.assertEqual('original-canonical-cursor', self.store.get('cursor'))
        self.client.after = None
        with self.assertRaises((ChannelError, CarrierError)):
            self.channel.poll_once()
        self.assertEqual(1, len(self.transport.calls))

    def test_poll_committed_ack_loss_never_creates_second_task_or_repolls_old_intent(self):
        self.transport.batch['msgs'] = [self.message()]
        self.client.after = 'committed'
        with self.assertRaises(ChannelError):
            self.channel.poll_once()
        self.assertEqual(1, len(self.rows('tasks')))
        self.assertEqual('next-canonical-cursor', self.store.get('cursor'))
        self.client.after = None
        # New work may poll from the NOW-durable cursor, not replay the old IO.
        self.assertEqual(0, self.channel.poll_once())
        self.assertEqual(1, len(self.rows('tasks')))
        self.assertEqual('next-canonical-cursor', self.transport.calls[-1][1]['get_updates_buf'])

    def test_poll_timeout_records_error_and_holds_cursor_instead_of_starting_another_pull(self):
        self.transport.error = ChannelError('network_timeout')
        with self.assertRaisesRegex(ChannelError, '^network_timeout$'):
            self.channel.poll_once()
        self.assertEqual('original-canonical-cursor', self.store.get('cursor'))
        self.assertEqual([], self.rows('inbox'))
        self.assertEqual('unknown', self.witness_intents()[0]['state'])
        self.assertIsNotNone(self.witness_intents()[0]['persistence_sha256'])
        self.transport.error = None
        with self.assertRaises((ChannelError, CarrierError)):
            self.channel.poll_once()
        self.assertEqual(1, len(self.transport.calls))

    def test_invalid_poll_batch_rolls_back_original_task_and_cursor_despite_returned_http(self):
        first, broken = self.message(), self.message()
        broken['message_id'] = 'fixture-broken-message'
        broken['item_list'] = {}
        self.transport.batch['msgs'] = [first, broken]
        with self.assertRaisesRegex(ChannelError, '^channel_fence_unconfirmed$'):
            self.channel.poll_once()
        self.assertEqual([], self.rows('inbox'))
        self.assertEqual([], self.rows('tasks'))
        self.assertEqual('original-canonical-cursor', self.store.get('cursor'))
        self.assertEqual('unknown', self.witness_intents()[0]['state'])

    def test_expired_or_frozen_holder_cannot_use_original_channel_as_backdoor(self):
        self.enqueue()
        self.now += 61
        with self.assertRaises((ChannelError, CarrierError)):
            self.channel.send_once()
        with self.assertRaises((ChannelError, CarrierError)):
            self.channel.poll_once()
        self.assertEqual([], self.transport.calls)
        self.assertEqual('pending', self.store.send_status('original-outbox')['status'])

    def test_unknown_persists_across_actual_store_and_channel_restart(self):
        self.enqueue()
        self.transport.error = ChannelError('network_timeout')
        self.channel.send_once()
        self.transport.error = None
        restarted = Store(self.path, clock=lambda: self.now)
        witness = CarrierWitness(restarted, 'fixture-witness', self.handoff)
        state = ChannelState(restarted, witness, account=self.transport.account)
        channel = Channel(state, self.transport, CarrierFence(BoundWitnessClient(witness), self.term))
        self.assertFalse(channel.send_once())
        self.assertEqual('unknown', restarted.send_status('original-outbox')['status'])
        self.assertEqual(1, len(self.transport.calls))

    def test_mutated_sdk_diagnostic_cannot_leak_private_text_into_shared_error(self):
        self.enqueue()
        self.transport.error = ChannelError('synthetic-private-token-and-chat')
        self.channel.send_once()
        status = self.store.send_status('original-outbox')
        self.assertEqual('unknown', status['status'])
        self.assertEqual('transport_failed', status['detail']['code'])
        self.assertNotIn('synthetic-private-token-and-chat', json.dumps(status))


if __name__ == '__main__':
    unittest.main()
