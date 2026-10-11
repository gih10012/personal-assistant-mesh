"""Original-authority state with a real Store/Witness and synthetic outcomes.

No iLink requests, credentials, production state, receiver threads, or HA proof.
The synthetic verifier permits exercising grants; it does not attest shutdown.
"""
import base64
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.channel import ILink
from assistant_mesh.channel_authority import ChannelAuthority
from assistant_mesh.channel_carrier import CarrierError, CarrierFence, CarrierWitness, PROTOCOL
from assistant_mesh.channel_state import ChannelState
from assistant_mesh.networking import digest
from assistant_mesh.store import Store


class WitnessClient:
    def __init__(self, witness, actor):
        self.witness, self.actor = witness, actor

    def request(self, path, payload):
        return self.witness.request(self.actor, path.rsplit('/', 1)[-1], payload)


class ChannelStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'original-authority.db'
        self.now = 1000
        self.store = Store(self.path, clock=lambda: self.now)
        self.binding = 'a' * 64
        self.account = {'ilink_user_id': 'synthetic-owner', 'ilink_bot_id': 'synthetic-bot'}
        self.store.set('owner_notification_channel_binding', self.binding)
        self.store.set('cursor', 'original-cursor')
        self.context = {'token': 'synthetic-private-context', 'version': 'original-context', 'created': 1}
        self.store.set('owner_context', self.context)
        self.witness = CarrierWitness(self.store, 'synthetic-witness', self.handoff)
        self.witness.enroll(self.binding, 'owner-ref', 'cloud')
        for node in ('cloud', 'laptop'):
            self.witness.candidate(node, {'protocol': PROTOCOL, 'node': node, 'owner_ref': 'owner-ref',
                'account_binding': self.binding, 'canonical_authority': 'cloud',
                'private_credential_ref': 'synthetic-private-ref', 'revision': 1,
                'runtime': {'adapter': 'ilink/0.3.0', 'fence_protocol': PROTOCOL,
                            'transport_available': True, 'private_state': True}}, 300)
        self.term = self.witness.grant(self.binding, 0, 'cloud', 60, 'synthetic-bootstrap')
        self.state = ChannelState(self.store, self.witness, self.account)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def handoff(before, holder, proof_ref):
        return {'protocol': 'mesh-channel-handoff-proof/1',
            'account_binding': before['account_binding'], 'canonical_authority': before['canonical_authority'],
            'from_holder': before['holder'], 'from_epoch': before['epoch'], 'to_holder': holder,
            'proof_ref': proof_ref, 'checkpoint_revision': before['checkpoint_revision'] + 1,
            'checkpoint_sha256': digest(['synthetic-state-only', before['epoch']]),
            'migration_ready': True, 'original_history_closed': True, 'original_execution_closed': True,
            'previous_carrier_stopped': True, 'external_operations_quiescent': True}

    def rows(self, table):
        # All table/column names in these tests are fixed fixture identifiers.
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]

    def canonical(self, operation):
        return next(row for row in self.rows('channel_canonical_intents') if row['operation_id'] == operation)

    def witnessed(self, operation):
        return next(row for row in self.rows('channel_carrier_intents') if row['operation_id'] == operation)

    def message(self, identity='owner-message', items=None):
        return {'message_id': identity, 'from_user_id': self.account['ilink_user_id'],
            'to_user_id': self.account['ilink_bot_id'], 'message_type': 1, 'create_time_ms': 2000,
            'context_token': 'synthetic-updated-context',
            'item_list': [{'type': 1, 'text_item': {'text': 'synthetic original work'}}] if items is None else items}

    def poll_outcome(self, messages=None):
        return {'status': 'updates', 'batch': {'msgs': [self.message()] if messages is None else messages,
            'get_updates_buf': 'next-cursor', 'private_auxiliary': {'retained': [1, 2, 3]}}}

    @staticmethod
    def send_outcome(status='accepted'):
        detail = {'delivery_verified': False}
        if status == 'accepted':
            detail['message_id'] = 'synthetic-provider-receipt'
            return {'status': status, 'detail': detail, 'response': {'message_id': detail['message_id'], 'ret': 0}}
        detail['code'] = 'business_-2' if status == 'rejected' else 'network_timeout'
        return {'status': status, 'detail': detail}

    def prepare_send(self, identity='original-outbox', text='synthetic original response'):
        self.store.enqueue(identity, text)
        return self.state.channel_prepare_send(self.term)

    def dispatch(self, prepared, kind, term=None):
        term = self.term if term is None else term
        operation = prepared['operation_id']
        identity = json.loads(self.canonical(operation)['identity'])
        first = self.witness.begin(term['holder'], term, operation, kind, identity,
                                   digest(['synthetic-wire-request', operation]))
        receipt = self.witness.dispatch(term['holder'], CarrierFence._ticket(first))
        ticket = CarrierFence._ticket(receipt)
        self.state.channel_mark_dispatched(operation, ticket)
        return ticket

    def authorize(self, prepared, kind, outcome, term=None):
        ticket = self.dispatch(prepared, kind, term)
        self.state.channel_record_result(prepared['operation_id'], outcome)
        self.witness.transition(ticket['holder'], ticket, 'returned', digest(outcome))
        self.witness.transition(ticket['holder'], ticket, 'commit_authorize')
        return ticket

    def commit_send(self, prepared, ticket, outcome):
        return self.state.channel_commit_send(prepared['operation_id'], ticket,
                                              outcome['status'], outcome['detail'])

    def commit_poll(self, prepared, ticket, outcome):
        return self.state.channel_commit_poll(prepared['operation_id'], ticket, outcome,
                                              self.account['ilink_user_id'], self.account['ilink_bot_id'])

    def test_facade_cannot_use_a_witness_on_an_independent_store(self):
        other = Store(Path(self.tmp.name) / 'independent.db', clock=lambda: self.now)
        with self.assertRaisesRegex(CarrierError, '^channel_canonical_witness_mismatch$'):
            ChannelState(other, self.witness)

    def test_frozen_term_cannot_claim_an_original_outbox(self):
        self.store.enqueue('original-outbox', 'synthetic reply')
        before = self.rows('outbox')
        self.witness.freeze(self.binding, self.term['epoch'])
        with self.assertRaisesRegex(CarrierError, '^carrier_admission_closed$'):
            self.state.channel_prepare_send(self.term)
        self.assertEqual(before, self.rows('outbox'))
        self.assertEqual([], self.rows('channel_canonical_intents'))

    def test_expired_term_cannot_claim_or_prepare_poll(self):
        self.store.enqueue('original-outbox', 'synthetic reply')
        before = self.rows('outbox')
        self.now += 61
        for prepare in (lambda: self.state.channel_prepare_send(self.term),
                        lambda: self.state.channel_prepare_poll(self.term, 'original-cursor')):
            with self.assertRaisesRegex(CarrierError, '^carrier_admission_closed$'):
                prepare()
        self.assertEqual(before, self.rows('outbox'))
        self.assertEqual([], self.rows('channel_canonical_intents'))

    def test_handoff_rejects_old_term_before_claim_and_keeps_original_authority(self):
        self.store.enqueue('original-outbox', 'synthetic reply')
        before = self.rows('outbox')
        self.witness.freeze(self.binding, self.term['epoch'])
        new = self.witness.grant(self.binding, self.term['epoch'], 'laptop', 60, 'synthetic-controlled-handoff')
        with self.assertRaisesRegex(CarrierError, '^carrier_term_conflict$'):
            self.state.channel_prepare_send(self.term)
        self.assertEqual(before, self.rows('outbox'))
        prepared = self.state.channel_prepare_send(new)
        row = self.canonical(prepared['operation_id'])
        self.assertEqual(('laptop', 2), (row['holder'], row['epoch']))
        self.assertEqual('cloud', json.loads(row['identity'])['authority'])
        self.assertEqual(before[0]['client_id'], prepared['row']['client_id'])

    def test_stale_cursor_does_not_create_intent_or_change_original_state(self):
        for cursor in ('obsolete-cursor', None):
            with self.assertRaisesRegex(CarrierError, '^channel_original_cursor_changed$'):
                self.state.channel_prepare_poll(self.term, cursor)
        self.assertEqual('original-cursor', self.store.get('cursor'))
        self.assertEqual([], self.rows('channel_canonical_intents'))

    def test_authority_begin_requires_exact_original_wire_snapshot(self):
        account_path = Path(self.tmp.name) / 'synthetic-private-account.json'
        account_path.write_text(json.dumps(dict(self.account,
            baseurl='https://synthetic.weixin.qq.com', bot_token='synthetic-unused-token')))
        account_path.chmod(0o600)
        transport = ILink(account_path, request=lambda *unused: self.fail('No wire calls are authorized'))
        authority = ChannelAuthority(self.store, {'witness_id': 'synthetic-witness',
            'owner_ref': 'owner-ref', 'transport_mode': 'disabled'}, 'cloud', transport, self.binding)
        client = authority.bound_client('cloud')
        send = self.prepare_send()
        poll = self.state.channel_prepare_poll(self.term, 'original-cursor')
        original_send = transport.send_body(send['row'])
        modified = []
        for key, value in (('client_id', 'another-client'), ('context_token', 'another-context'),
                           ('to_user_id', 'another-recipient')):
            body = copy.deepcopy(original_send)
            body['msg'][key] = value
            modified.append(body)
        body = copy.deepcopy(original_send)
        body['msg']['item_list'] = [{'type': 1, 'text_item': {'text': 'another-body'}}]
        modified.append(body)
        cases = ((send, 'send', 'sendmessage', original_send, modified),
                 (poll, 'poll', 'getupdates', {'get_updates_buf': 'original-cursor'},
                  [{'get_updates_buf': 'obsolete-cursor'}]))
        for prepared, kind, endpoint, original_body, changed_bodies in cases:
            operation = prepared['operation_id']
            payload = {'term': self.term, 'operation_id': operation, 'kind': kind,
                'identity': json.loads(self.canonical(operation)['identity'])}
            for changed_body in changed_bodies:
                with self.subTest(kind=kind, changed_body=changed_body):
                    forged = dict(payload, request_sha256=digest({'endpoint': endpoint, 'body': changed_body}))
                    with self.assertRaisesRegex(CarrierError, '^channel_original_request_changed$'):
                        client.request('/channel-carrier/begin', forged)
                    self.assertFalse(any(row['operation_id'] == operation
                                         for row in self.rows('channel_carrier_intents')))
            exact = dict(payload, request_sha256=digest({'endpoint': endpoint, 'body': original_body}))
            result = client.request('/channel-carrier/begin', exact)
            self.assertEqual(exact['request_sha256'], result['request_sha256'])
            self.assertEqual('prepared', self.canonical(operation)['state'])
        self.assertEqual('original-cursor', self.store.get('cursor'))
        self.assertEqual('submitting', self.rows('outbox')[0]['status'])

    def test_prepare_preserves_original_send_and_does_not_begin_witness_io(self):
        self.store.enqueue_media('original-outbox', [{'type': 2, 'image_item': {'media_ref': 'synthetic-private-ref'}}])
        original = self.rows('outbox')[0]
        prepared = self.state.channel_prepare_send(self.term)
        snapshot = json.loads(self.canonical(prepared['operation_id'])['snapshot'])
        for key in ('id', 'client_id', 'body', 'fingerprint', 'media_items', 'delivery_route'):
            self.assertEqual(original[key], prepared['row'][key])
            self.assertEqual(original[key], snapshot[key])
        self.assertEqual(self.context['token'], prepared['row']['context_token'])
        self.assertEqual(self.context['version'], prepared['row']['context_version'])
        self.assertEqual('submitting', self.rows('outbox')[0]['status'])
        self.assertEqual([], self.rows('channel_carrier_intents'))

    def test_unsettled_prepare_does_not_consume_another_pending_outbox(self):
        first = self.prepare_send()
        self.store.enqueue('another-outbox', 'synthetic separate work')
        before = self.rows('outbox')
        with self.assertRaisesRegex(CarrierError, '^channel_original_intent_unsettled$'):
            self.state.channel_prepare_send(self.term)
        self.assertEqual(before, self.rows('outbox'))
        self.assertEqual([first['operation_id']], [row['operation_id'] for row in self.rows('channel_canonical_intents')])

    def test_dispatch_requires_actual_witness_admission_and_exact_ticket(self):
        prepared = self.prepare_send()
        operation = prepared['operation_id']
        identity = json.loads(self.canonical(operation)['identity'])
        receipt = self.witness.begin('cloud', self.term, operation, 'send', identity, digest('synthetic-request'))
        with self.assertRaises(CarrierError):
            self.state.channel_mark_dispatched(operation, CarrierFence._ticket(receipt))
        receipt = self.witness.dispatch('cloud', CarrierFence._ticket(receipt))
        ticket = CarrierFence._ticket(receipt)
        altered = copy.deepcopy(ticket)
        altered['identity']['client_id'] = 'wrong-original-client'
        with self.assertRaises(CarrierError):
            self.state.channel_mark_dispatched(operation, altered)
        self.assertEqual('prepared', self.canonical(operation)['state'])
        self.assertIsNone(self.canonical(operation)['ticket'])
        self.state.channel_mark_dispatched(operation, ticket)
        with self.assertRaisesRegex(CarrierError, '^channel_original_dispatch_already_recorded$'):
            self.state.channel_mark_dispatched(operation, ticket)

    def test_result_spool_requires_dispatch_and_preserves_first_full_result(self):
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        operation, outcome = prepared['operation_id'], self.poll_outcome()
        with self.assertRaises(CarrierError):
            self.state.channel_record_result(operation, outcome)
        self.dispatch(prepared, 'poll')
        self.state.channel_record_result(operation, outcome)
        saved = self.canonical(operation)
        changed = copy.deepcopy(outcome)
        changed['batch']['private_auxiliary']['retained'].append(4)
        with self.assertRaises(CarrierError):
            self.state.channel_record_result(operation, changed)
        self.assertEqual(saved, self.canonical(operation))
        self.assertEqual(outcome, json.loads(saved['outcome']))
        self.assertEqual(digest(outcome), saved['outcome_sha256'])
        self.assertEqual([], self.rows('inbox'))
        self.assertEqual('original-cursor', self.store.get('cursor'))

    def test_result_spool_and_original_ticket_survive_store_restart(self):
        prepared = self.prepare_send()
        outcome = self.send_outcome()
        ticket = self.authorize(prepared, 'send', outcome)
        old = self.canonical(prepared['operation_id'])
        self.store = Store(self.path, clock=lambda: self.now)
        self.witness = CarrierWitness(self.store, 'synthetic-witness', self.handoff)
        self.state = ChannelState(self.store, self.witness, self.account)
        self.assertEqual(old, self.canonical(prepared['operation_id']))
        self.assertEqual('unknown', self.store.send_status('original-outbox')['status'])
        with self.assertRaisesRegex(CarrierError, '^channel_original_intent_unsettled$'):
            self.store.enqueue('another-outbox', 'synthetic separate work')
            self.state.channel_prepare_send(self.term)
        receipt = self.commit_send(prepared, ticket, outcome)
        self.witness.transition('cloud', ticket, 'committed', receipt)
        self.assertEqual('accepted', self.store.send_status('original-outbox')['status'])
        self.assertEqual(prepared['row']['client_id'], self.rows('outbox')[0]['client_id'])
        self.assertEqual('committed', self.witnessed(prepared['operation_id'])['state'])

    def test_commit_requires_witness_authorization_and_exact_saved_outcome(self):
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        outcome = self.poll_outcome()
        ticket = self.dispatch(prepared, 'poll')
        self.state.channel_record_result(prepared['operation_id'], outcome)
        with self.assertRaises(CarrierError):
            self.commit_poll(prepared, ticket, outcome)
        self.witness.transition('cloud', ticket, 'returned', digest(outcome))
        self.witness.transition('cloud', ticket, 'commit_authorize')
        changed = copy.deepcopy(outcome)
        changed['batch']['get_updates_buf'] = 'invented-cursor'
        with self.assertRaisesRegex(CarrierError, '^channel_original_outcome_changed$'):
            self.commit_poll(prepared, ticket, changed)
        self.assertEqual([], self.rows('inbox'))
        self.assertIsNone(self.canonical(prepared['operation_id'])['persistence_sha256'])

    def test_poll_commit_rejects_changed_cursor_and_owner_without_ingest(self):
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        outcome = self.poll_outcome()
        ticket = self.authorize(prepared, 'poll', outcome)
        with self.assertRaisesRegex(CarrierError, '^channel_original_owner_changed$'):
            self.state.channel_commit_poll(prepared['operation_id'], ticket, outcome, 'different-owner', 'synthetic-bot')
        self.store.set('cursor', 'concurrently-changed-cursor')
        with self.assertRaisesRegex(CarrierError, '^channel_original_cursor_changed$'):
            self.commit_poll(prepared, ticket, outcome)
        self.assertEqual([], self.rows('inbox'))
        self.assertEqual([], self.rows('tasks'))
        self.assertEqual(self.context, self.store.get('owner_context'))

    def test_partial_invalid_poll_batch_rolls_back_inbox_task_context_cursor_and_receipt(self):
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        outcome = self.poll_outcome([self.message('first-valid'), self.message('second-invalid', items='not-an-item-list')])
        ticket = self.authorize(prepared, 'poll', outcome)
        with self.assertRaisesRegex(ValueError, '^invalid_items$'):
            self.commit_poll(prepared, ticket, outcome)
        self.assertEqual([], self.rows('inbox'))
        self.assertEqual([], self.rows('tasks'))
        self.assertEqual([], self.rows('outbox'))
        self.assertEqual('original-cursor', self.store.get('cursor'))
        self.assertEqual(self.context, self.store.get('owner_context'))
        row = self.canonical(prepared['operation_id'])
        self.assertEqual('returned', row['state'])
        self.assertEqual(outcome, json.loads(row['outcome']))
        self.assertIsNone(row['persistence_sha256'])

    def install_receipt_failure(self):
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER synthetic_fail_canonical_receipt BEFORE UPDATE OF persistence_sha256 "
                       "ON channel_canonical_intents WHEN NEW.persistence_sha256 IS NOT NULL "
                       "BEGIN SELECT RAISE(ABORT,'synthetic receipt write failure'); END")

    def test_poll_receipt_failure_rolls_back_all_original_ingestion(self):
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        outcome = self.poll_outcome()
        ticket = self.authorize(prepared, 'poll', outcome)
        self.install_receipt_failure()
        with self.assertRaises(sqlite3.IntegrityError):
            self.commit_poll(prepared, ticket, outcome)
        self.assertEqual([], self.rows('inbox'))
        self.assertEqual([], self.rows('tasks'))
        self.assertEqual([], self.rows('outbox'))
        self.assertEqual('original-cursor', self.store.get('cursor'))
        self.assertEqual(self.context, self.store.get('owner_context'))
        self.assertIsNone(self.state.receipt(prepared['operation_id'])['persistence_sha256'])
        self.assertEqual('committing', self.witnessed(prepared['operation_id'])['state'])

    def test_send_receipt_failure_rolls_back_original_outbox_settlement(self):
        prepared = self.prepare_send()
        outcome = self.send_outcome()
        ticket = self.authorize(prepared, 'send', outcome)
        before = self.rows('outbox')
        self.install_receipt_failure()
        with self.assertRaises(sqlite3.IntegrityError):
            self.commit_send(prepared, ticket, outcome)
        self.assertEqual(before, self.rows('outbox'))
        self.assertIsNone(self.state.receipt(prepared['operation_id'])['persistence_sha256'])

    def test_lost_poll_write_ack_can_read_same_receipt_without_reingesting(self):
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        outcome = self.poll_outcome()
        ticket = self.authorize(prepared, 'poll', outcome)
        receipt = self.commit_poll(prepared, ticket, outcome)
        self.assertEqual(1, receipt['accepted'])
        before = {table: self.rows(table) for table in ('inbox', 'tasks', 'outbox', 'channel_canonical_intents')}
        self.witness.transition('cloud', ticket, 'unknown')
        self.now += 1
        self.assertEqual(receipt, self.commit_poll(prepared, ticket, outcome))
        for table, rows in before.items():
            self.assertEqual(rows, self.rows(table))
        self.assertEqual(receipt['sha256'], self.state.receipt(prepared['operation_id'])['persistence_sha256'])

    def test_original_admitted_result_can_drain_after_freeze_and_expiry(self):
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        outcome = self.poll_outcome()
        ticket = self.authorize(prepared, 'poll', outcome)
        self.now += 61
        self.witness.freeze(self.binding, self.term['epoch'])
        with self.assertRaisesRegex(CarrierError, '^carrier_unsettled_intent$'):
            self.witness.grant(self.binding, self.term['epoch'], 'laptop', 60, 'synthetic-proof')
        receipt = self.commit_poll(prepared, ticket, outcome)
        self.witness.transition('cloud', ticket, 'committed', receipt['sha256'])
        new = self.witness.grant(self.binding, self.term['epoch'], 'laptop', 60, 'synthetic-proof')
        self.assertEqual(('laptop', 2, 'cloud'), (new['holder'], new['epoch'], new['canonical_authority']))
        self.assertEqual(1, len(self.rows('tasks')))

    def test_carrier_handoff_preserves_live_original_task_history_and_steering(self):
        # Real canonical storage calls; no native model or imported checkpoint.
        self.store.heartbeat('original-worker', ['leader'])
        original_id = self.store.create_task('synthetic original running work')
        task = self.store.claim('original-worker')
        history = b'{"type":"session_meta","payload":{"id":"synthetic-original-thread"}}\n'
        history += b'{"type":"compacted","native":true,"retained":"original bytes"}\n'
        self.store.session_action(original_id, 'original-worker', task['epoch'], 'upload',
            {'harness': 'codex', 'part': 0, 'data': base64.b64encode(history).decode('ascii')})
        self.store.session_action(original_id, 'original-worker', task['epoch'], 'commit',
            {'harness': 'codex', 'parts': 1, 'state': {'thread_id': 'synthetic-original-thread'}})
        self.store.update_task(original_id, 'original-worker', task['epoch'],
                               checkpoint={'side_effect_started': True, 'thread_id': 'synthetic-original-thread'})
        self.store.steer(original_id, 'synthetic original direction', 'original-steering')
        before_task = next(row for row in self.rows('tasks') if row['id'] == original_id)
        before = {table: self.rows(table) for table in ('native_sessions', 'session_chunks', 'steering', 'leader')}
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        outcome = self.poll_outcome()
        ticket = self.authorize(prepared, 'poll', outcome)
        receipt = self.commit_poll(prepared, ticket, outcome)
        self.witness.transition('cloud', ticket, 'committed', receipt['sha256'])
        self.witness.freeze(self.binding, self.term['epoch'])
        new = self.witness.grant(self.binding, self.term['epoch'], 'laptop', 60, 'synthetic-carrier-only-proof')
        self.assertEqual('cloud', new['canonical_authority'])
        self.assertEqual(before_task, next(row for row in self.rows('tasks') if row['id'] == original_id))
        for table, rows in before.items():
            self.assertEqual(rows, self.rows(table))
        saved = self.store.session_action(original_id, 'original-worker', task['epoch'], 'download',
                                           {'harness': 'codex', 'part': 0})
        self.assertEqual(history, base64.b64decode(saved['data']))

    def test_only_explicit_rejection_under_new_context_creates_one_distinct_retry(self):
        prepared = self.prepare_send()
        outcome = self.send_outcome('rejected')
        ticket = self.authorize(prepared, 'send', outcome)
        receipt = self.commit_send(prepared, ticket, outcome)
        self.witness.transition('cloud', ticket, 'committed', receipt)
        self.assertIsNone(self.state.channel_prepare_send(self.term))
        self.store.set('owner_context', {'token': 'synthetic-new-context', 'version': 'new-context', 'created': 2})
        retry = self.state.channel_prepare_send(self.term)
        self.assertEqual(prepared['row']['id'], retry['row']['id'])
        self.assertNotEqual(prepared['row']['client_id'], retry['row']['client_id'])
        self.assertEqual(1, self.rows('outbox')[0]['retry_count'])
        retry_ticket = self.authorize(retry, 'send', outcome)
        retry_receipt = self.commit_send(retry, retry_ticket, outcome)
        self.witness.transition('cloud', retry_ticket, 'committed', retry_receipt)
        self.store.set('owner_context', {'token': 'synthetic-third-context', 'version': 'third-context', 'created': 3})
        self.assertIsNone(self.state.channel_prepare_send(self.term))
        self.assertEqual(2, len(self.rows('channel_carrier_intents')))

    def test_uncertain_guard_preserves_full_returned_batch_and_blocks_replacement_poll(self):
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        outcome = self.poll_outcome()
        ticket = self.dispatch(prepared, 'poll')
        self.state.channel_record_result(prepared['operation_id'], outcome)
        self.state.channel_mark_uncertain(prepared['operation_id'])
        self.witness.transition('cloud', ticket, 'unknown')
        self.assertEqual(outcome, json.loads(self.canonical(prepared['operation_id'])['outcome']))
        self.assertEqual(1, self.state.receipt(prepared['operation_id'])['uncertain'])
        with self.assertRaisesRegex(CarrierError, '^channel_original_intent_unsettled$'):
            self.state.channel_prepare_poll(self.term, 'original-cursor')
        self.assertEqual('original-cursor', self.store.get('cursor'))
        self.assertEqual([], self.rows('tasks'))

    def test_committed_unknown_retains_original_send_and_blocks_handoff_after_restart(self):
        prepared = self.prepare_send()
        operation, outcome = prepared['operation_id'], self.send_outcome('unknown')
        ticket = self.authorize(prepared, 'send', outcome)
        receipt = self.commit_send(prepared, ticket, outcome)
        self.witness.transition('cloud', ticket, 'committed_unknown', receipt)
        self.assertEqual('unknown', self.store.send_status('original-outbox')['status'])
        self.assertEqual('unknown', self.witnessed(operation)['state'])
        self.assertEqual(receipt, self.witnessed(operation)['persistence_sha256'])
        self.assertEqual(1, self.state.receipt(operation)['uncertain'])
        self.assertIsNone(self.state.channel_prepare_send(self.term))
        self.store = Store(self.path, clock=lambda: self.now)
        self.witness = CarrierWitness(self.store, 'synthetic-witness', self.handoff)
        self.state = ChannelState(self.store, self.witness, self.account)
        self.witness.freeze(self.binding, self.term['epoch'])
        with self.assertRaisesRegex(CarrierError, '^carrier_unsettled_intent$'):
            self.witness.grant(self.binding, self.term['epoch'], 'laptop', 60, 'synthetic-proof')
        self.assertEqual(prepared['row']['client_id'], self.rows('outbox')[0]['client_id'])
        self.assertEqual(outcome, json.loads(self.canonical(operation)['outcome']))

    def test_unknown_send_allows_distinct_new_work_only_in_same_holder_term(self):
        prepared = self.prepare_send()
        outcome = self.send_outcome('unknown')
        ticket = self.authorize(prepared, 'send', outcome)
        receipt = self.commit_send(prepared, ticket, outcome)
        self.witness.transition('cloud', ticket, 'committed_unknown', receipt)
        second = self.prepare_send('separate-outbox', 'synthetic separate response')
        self.assertNotEqual(prepared['operation_id'], second['operation_id'])
        self.assertNotEqual(prepared['row']['client_id'], second['row']['client_id'])
        self.assertEqual('separate-outbox', second['row']['id'])
        self.dispatch(second, 'send')
        self.assertEqual('unknown', self.store.send_status('original-outbox')['status'])

    def test_late_uncertain_report_does_not_downgrade_known_accepted_receipt(self):
        prepared = self.prepare_send()
        outcome = self.send_outcome()
        ticket = self.authorize(prepared, 'send', outcome)
        receipt = self.commit_send(prepared, ticket, outcome)
        self.witness.transition('cloud', ticket, 'committed', receipt)
        before = self.rows('outbox'), self.canonical(prepared['operation_id'])
        self.now += 1
        self.state.channel_mark_uncertain(prepared['operation_id'])
        self.assertEqual(before, (self.rows('outbox'), self.canonical(prepared['operation_id'])))
        self.assertEqual(receipt, self.commit_send(prepared, ticket, outcome))

    def assert_send_mutation_rejected(self, key, changed):
        prepared = self.prepare_send()
        outcome = self.send_outcome()
        ticket = self.authorize(prepared, 'send', outcome)
        original = self.rows('outbox')[0]
        with self.store.transaction() as db:
            db.execute('UPDATE outbox SET ' + key + '=? WHERE id=?', (changed, original['id']))
        with self.assertRaisesRegex(CarrierError, '^channel_original_outbox_changed$'):
            self.commit_send(prepared, ticket, outcome)
        self.assertIsNone(self.state.receipt(prepared['operation_id'])['persistence_sha256'])

    def test_send_commit_rejects_mutated_original_client(self):
        self.assert_send_mutation_rejected('client_id', 'changed-client')

    def test_send_commit_rejects_mutated_original_body(self):
        self.assert_send_mutation_rejected('body', 'changed-body')

    def test_send_commit_rejects_mutated_original_media(self):
        self.assert_send_mutation_rejected('media_items', '[{"type":2}]')

    def test_send_commit_rejects_mutated_original_route(self):
        self.assert_send_mutation_rejected('delivery_route', 'private')

    def test_send_commit_rejects_mutated_original_context_version(self):
        self.assert_send_mutation_rejected('context_version', 'changed-context')

    def test_on_dispatch_failure_does_not_call_transport_and_keeps_original_guard(self):
        prepared = self.prepare_send()
        operation = prepared['operation_id']
        identity = json.loads(self.canonical(operation)['identity'])
        calls = []
        fence = CarrierFence(WitnessClient(self.witness, 'cloud'), self.term)

        def admitted(ticket):
            self.state.channel_mark_dispatched(operation, ticket)
            raise OSError('synthetic local durability failure after admission')

        with self.assertRaisesRegex(CarrierError, '^carrier_transport_unknown$'):
            fence.run(operation, 'send', identity, digest('synthetic-request'),
                      lambda: calls.append('wire'), lambda result, ticket: digest(result), on_dispatch=admitted)
        self.state.channel_mark_uncertain(operation)
        self.assertEqual([], calls)
        self.assertEqual('unknown', self.witnessed(operation)['state'])
        self.assertEqual('unknown', self.store.send_status('original-outbox')['status'])
        self.witness.freeze(self.binding, self.term['epoch'])
        with self.assertRaisesRegex(CarrierError, '^carrier_unsettled_intent$'):
            self.witness.grant(self.binding, self.term['epoch'], 'laptop', 60, 'synthetic-proof')


if __name__ == '__main__':
    unittest.main()
