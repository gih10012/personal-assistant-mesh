"""Real SQLite/HTTP independent state, synthetic transport/owner proofs only."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from assistant_mesh.channel import Channel
from assistant_mesh.channel_authority import RemoteChannelState
from assistant_mesh.channel_carrier import CarrierError, CarrierFence, CarrierWitness, PROTOCOL as FENCE_PROTOCOL
from assistant_mesh.channel_state import ChannelState
from assistant_mesh.communication_broker import OWNER_PROTOCOL, serve
from assistant_mesh.communication_inbox import CommunicationInbox, deliver_once
from assistant_mesh.communication_producer import relay_store_class
from assistant_mesh.communication_migration import export_transfer, install_prepared, activate_prepared, verify_prepared
from assistant_mesh.communication_store import CommunicationStore, message_identity, RECEIPT_PROTOCOL
from assistant_mesh.networking import digest
from assistant_mesh.remote_notifications import PROTOCOL as NOTIFY_PROTOCOL, RemoteNotifications, text_fingerprint
from assistant_mesh.store import Store, Conflict
from tests.test_legacy_store_carrier_backport import _original_source, _patched_source, _load_store


class Client:
    def __init__(self, url, token):
        self.url, self.token = url, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(self, path, body):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=4) as response:
            return json.loads(response.read())


class CommunicationFaultDomainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Exact frozen original Store plus its ingestion-only extraction, not
        # today's native goal implementation and not a production constructor.
        cls.TaskStore = _load_store(_patched_source(_original_source()))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='communication-split-')
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.original = self.TaskStore(self.root / 'original.sqlite', recover_inflight=False)
        Path(self.original.path).chmod(0o600)
        self.account = {'baseurl': 'https://fixture.weixin.qq.com', 'bot_token': 'synthetic-token',
            'ilink_user_id': 'fixture-owner', 'ilink_bot_id': 'fixture-bot'}
        self.binding = digest([NOTIFY_PROTOCOL, self.account['baseurl'], 'fixture-owner', 'fixture-bot'])
        self.notifications = RemoteNotifications(self.original, 'cloud', route_id=self.binding)
        self.original.set('cursor', 'original-cursor')
        self.original.set('owner_context', {'token': 'synthetic-original-context', 'version': 'original', 'created': 1})
        self.original.set('activate_after_ms', 0)
        self.old = self.message('old-original')
        self.original.ingest([self.old], 'original-cursor', 'fixture-owner', 'fixture-bot')
        self.original.enqueue('old-unknown', 'synthetic old send')
        with self.original.transaction() as db:
            db.execute("UPDATE outbox SET status='unknown' WHERE id='old-unknown'")
            self.old_send = dict(db.execute("SELECT * FROM outbox WHERE id='old-unknown'").fetchone())
        self.transfer = export_transfer(self.original.path, 'cloud', 'owner-ref', self.binding, 'cutover-one')
        self.path = self.root / 'independent.sqlite'
        self.independent = install_prepared(self.transfer, self.path, self.transfer['sha256'])
        self.http = self.thread = self.api = None
        self.errors, self.wire = [], []

    def tearDown(self):
        if self.http:
            self.http.shutdown()
        if self.thread:
            self.thread.join(4)
            self.assertFalse(self.thread.is_alive())
        self.tmp.cleanup()

    def message(self, identity, text='synthetic new native work'):
        return {'message_id': identity, 'from_user_id': 'fixture-owner', 'to_user_id': 'fixture-bot',
            'message_type': 1, 'create_time_ms': int(time.time() * 1000), 'context_token': 'synthetic-context',
            'item_list': [{'type': 1, 'text_item': {'text': text}}]}

    @staticmethod
    def cutover(identity, sha, proof_ref):
        # TRUSTED SYNTHETIC TEST ONLY; never installed by JSON/CLI.
        return {'protocol': 'mesh-communication-cutover-proof/1',
            'communication_id': identity['communication_id'], 'import_sha256': sha, 'proof_ref': proof_ref,
            'source_channel_writers_stopped': True, 'source_canonical_writes_frozen': True,
            'external_operations_quiescent': True, 'original_identity_preserved': True}

    @staticmethod
    def handoff(before, holder, proof_ref):
        # TRUSTED SYNTHETIC TEST ONLY; no actual iLink/quiescence assertion.
        return {'protocol': 'mesh-channel-handoff-proof/1', 'account_binding': before['account_binding'],
            'canonical_authority': before['canonical_authority'], 'from_holder': before['holder'],
            'from_epoch': before['epoch'], 'to_holder': holder, 'proof_ref': proof_ref,
            'checkpoint_revision': before['checkpoint_revision'] + 1,
            'checkpoint_sha256': digest('synthetic-closed-communication-state'),
            'migration_ready': True, 'original_history_closed': True, 'original_execution_closed': True,
            'previous_carrier_stopped': True, 'external_operations_quiescent': True}

    def activate(self):
        return activate_prepared(self.independent, 'synthetic-cutover-proof', self.cutover, self.transfer)

    def start_broker(self):
        account = self.root / 'account.json'
        account.write_text(json.dumps(self.account))
        account.chmod(0o600)
        peers = []
        self.tokens = {}
        for name, role in (('carrier', 'channel_carrier'), ('laptop', 'executor'),
                           ('notifier', 'notifier'), ('viewer', 'viewer')):
            token = name + '-' + 'a' * 64
            self.tokens[name] = token
            file = self.root / (name + '.token')
            file.write_text(token)
            file.chmod(0o600)
            peer = {'role': role, 'token_file': str(file)}
            if role == 'channel_carrier':
                peer.update(node='laptop', channel={'owner_ref': 'owner-ref',
                    'canonical_authority': 'cloud', 'account_binding': self.binding})
            if role == 'executor':
                peer['execution_authority'] = 'laptop'
            if role == 'notifier':
                peer.update(node='source-node', capabilities=['owner.notify'])
            peers.append(peer)
        config = {'protocol': OWNER_PROTOCOL, 'database': str(self.path), 'account_file': str(account),
            'node_id': 'laptop', 'port': 0, 'peers': peers, 'channel_carrier': {
                'owner_ref': 'owner-ref', 'witness_id': 'independent-witness', 'transport_mode': 'disabled'}}
        ready = threading.Event()
        def connected(http, api, channel):
            self.http, self.api = http, api
            self.assertIsNone(channel)
            api.ilink.request_override = self.transport
            ready.set()
        def run():
            try:
                serve(config, ready=connected, handoff_verifier=self.handoff)
            except Exception as error:
                self.errors.append(error)
                ready.set()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(5))
        if self.errors:
            raise self.errors[0]
        url = 'http://127.0.0.1:' + str(self.http.server_address[1])
        self.clients = {name: Client(url, token) for name, token in self.tokens.items()}

    def transport(self, endpoint, body, timeout):
        self.wire.append((endpoint, body))
        if endpoint == 'getupdates':
            return {'msgs': [self.message('new-after-split')], 'get_updates_buf': 'independent-next'}
        return {'message_id': 'synthetic-independent-accept'}

    def grant(self):
        client = self.clients['carrier']
        client.request('/channel-carrier/candidate', {'contract': {'protocol': FENCE_PROTOCOL,
            'node': 'laptop', 'owner_ref': 'owner-ref', 'account_binding': self.binding,
            'canonical_authority': 'cloud', 'private_credential_ref': 'synthetic-ref', 'revision': 1,
            'runtime': {'adapter': 'ilink/0.3.0', 'fence_protocol': FENCE_PROTOCOL,
                        'transport_available': True, 'private_state': True}}, 'ttl': 300})
        return self.api.channel_authority.witness.grant(self.binding, 0, 'laptop', 60, 'synthetic-handoff')

    def test_independent_import_has_no_runnable_task_or_native_tables_and_keeps_old_ids(self):
        with self.independent.transaction() as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse(names & {'tasks', 'leader', 'native_sessions', 'session_chunks', 'steering'})
            row = dict(db.execute("SELECT * FROM outbox WHERE id='old-unknown'").fetchone())
            self.assertEqual(self.old_send, row)
            self.assertEqual([], list(db.execute('SELECT * FROM communication_events')))
            reference = json.loads(db.execute('SELECT reference FROM communication_task_refs WHERE task_id=?',
                                              (message_identity(self.old),)).fetchone()[0])
        self.assertEqual('cloud', reference['authority'])
        self.assertFalse(reference['runnable_copy'])
        self.assertFalse(self.independent.status()['activated'])

    def test_default_no_owner_verifier_or_posted_bool_can_activate(self):
        with self.assertRaisesRegex(ValueError, 'communication_owner_verifier_missing'):
            activate_prepared(self.independent, 'proof')
        with self.assertRaisesRegex(ValueError, 'communication_owner_verifier_missing'):
            activate_prepared(self.independent, 'proof', {'source_channel_writers_stopped': True})
        with self.assertRaises(PermissionError):
            self.independent.enqueue('new-send', 'synthetic message')

    def test_external_original_pin_and_new_destination_are_required(self):
        changed = copy.deepcopy(self.transfer)
        changed['payload']['tables']['outbox'][0]['status'] = 'pending'
        changed['sha256'] = digest(changed['payload'])
        with self.assertRaisesRegex(ValueError, 'communication_external_pin_mismatch'):
            install_prepared(changed, self.root / 'tampered.sqlite', self.transfer['sha256'])
        with self.assertRaisesRegex(ValueError, 'communication_destination_exists'):
            install_prepared(self.transfer, self.path, self.transfer['sha256'])
        old = next(row for row in self.transfer['payload']['tables']['outbox'] if row['id'] == 'old-unknown')
        self.assertEqual(self.old_send['client_id'], old['client_id'])

    def test_activation_checks_actual_candidate_rows_and_partial_prepare_is_not_usable(self):
        with self.independent.transaction() as db:
            db.execute("UPDATE outbox SET status='pending' WHERE id='old-unknown'")
        with self.assertRaisesRegex(ValueError, 'communication_candidate_rows_changed'):
            self.activate()
        with self.independent.transaction() as db:
            db.execute("UPDATE outbox SET status='unknown' WHERE id='old-unknown'")
            db.execute('UPDATE communication_identity SET prepared=0')
        with self.assertRaisesRegex(ValueError, 'communication_candidate_not_complete_or_already_active'):
            self.activate()
        self.assertFalse(self.independent.status()['activated'])

    def test_actual_identity_and_reference_lookup_columns_are_pinned_before_owner_verifier(self):
        transfer = copy.deepcopy(self.transfer)
        original_task = message_identity(self.old)
        transfer['payload']['control_references'].append({'authority': 'cloud',
            'control_id': 'original-control', 'task_id': original_task,
            'row_sha256': digest('synthetic-control-source-row')})
        transfer['payload']['legacy_relay_provenance'].append({'request_id': 'original-relay',
            'peer': 'original-peer', 'post_attempted': 1, 'state': 'unknown'})
        transfer['sha256'] = digest(transfer['payload'])
        changes = (
            ("UPDATE communication_identity SET identity=?,import_sha256=?",
             (json.dumps(dict(transfer['payload']['identity'], origin_authority='other-origin')), '0' * 64)),
            ("UPDATE communication_task_refs SET authority='other-authority'", ()),
            ("UPDATE communication_control_refs SET task_id='other-task'", ()),
            ("UPDATE communication_legacy_relays SET request_id='other-request'", ()),
        )
        for index, (statement, values) in enumerate(changes):
            with self.subTest(statement=statement):
                candidate = install_prepared(transfer, self.root / ('pinned-%d.sqlite' % index), transfer['sha256'])
                self.assertTrue(verify_prepared(candidate, transfer, transfer['sha256'])['candidate_bytes_verified'])
                with candidate.transaction() as db:
                    db.execute(statement, values)
                verifier = mock.Mock(side_effect=AssertionError('must reject before owner approval'))
                with self.assertRaisesRegex(ValueError, 'communication_candidate_(identity|references)_changed'):
                    activate_prepared(candidate, 'proof', verifier, transfer)
                verifier.assert_not_called()
                self.assertFalse(candidate.status()['activated'])

    def test_frozen_witness_incarnation_and_unknown_intent_transfer_without_reset(self):
        witness = CarrierWitness(self.original, 'original-witness', self.handoff)
        witness.enroll(self.binding, 'owner-ref', 'cloud')
        ChannelState(self.original, witness)
        contract = {'protocol': FENCE_PROTOCOL, 'node': 'cloud', 'owner_ref': 'owner-ref',
            'account_binding': self.binding, 'canonical_authority': 'cloud', 'revision': 1,
            'private_credential_ref': 'synthetic-reference', 'runtime': {
                'adapter': 'ilink/0.3.0', 'fence_protocol': FENCE_PROTOCOL,
                'transport_available': True, 'private_state': True}}
        witness.candidate('cloud', contract, 300)
        term = witness.grant(self.binding, 0, 'cloud', 60, 'synthetic-proof')
        begun = witness.begin('cloud', term, 'original-unknown-operation', 'send', {
            'authority': 'cloud', 'outbox_id': self.old_send['id'],
            'client_id': self.old_send['client_id']}, digest('synthetic-original-wire'))
        ticket = CarrierFence._ticket(begun)
        dispatched = witness.dispatch('cloud', ticket)
        witness.transition('cloud', CarrierFence._ticket(dispatched), 'unknown')
        witness.freeze(self.binding, term['epoch'])
        original_incarnation = witness.incarnation
        transfer = export_transfer(self.original.path, 'cloud', 'owner-ref', self.binding, 'frozen-transfer')
        store = install_prepared(transfer, self.root / 'fenced.sqlite', transfer['sha256'])
        reopened = CarrierWitness(store, 'original-witness')
        self.assertEqual(original_incarnation, reopened.incarnation)
        self.assertTrue(reopened.inspect(self.binding)['frozen'])
        self.assertEqual(1, reopened.inspect(self.binding)['epoch'])
        with store.transaction() as db:
            row = dict(db.execute("SELECT * FROM channel_carrier_intents WHERE operation_id='original-unknown-operation'").fetchone())
        self.assertEqual('unknown', row['state'])
        self.assertEqual(self.old_send['client_id'], json.loads(row['identity'])['client_id'])

    def test_actual_http_channel_operates_without_original_vps_store(self):
        self.activate()
        self.start_broker()
        # Simulate unavailable original execution DB, not another cloned Store.
        Path(self.original.path).rename(self.root / 'original-stopped.sqlite')
        term = self.grant()
        state = RemoteChannelState(self.clients['carrier'], str(self.root / 'carrier-spool.sqlite'))
        channel = Channel(state, self.api.ilink, CarrierFence(self.clients['carrier'], term))
        self.assertEqual(1, channel.poll_once())
        self.assertEqual('independent-next', self.independent.get('cursor'))
        self.assertEqual(1, len(self.independent.pending()))
        with self.independent.transaction() as db:
            self.assertEqual('unknown', db.execute("SELECT status FROM outbox WHERE id='old-unknown'").fetchone()[0])
        report = self.clients['viewer'].request('/communication/status', {})
        self.assertEqual('laptop', report['physical_host'])
        self.assertEqual('cloud', report['origin_execution_authority'])

    def test_existing_notification_route_and_original_unknown_are_preserved(self):
        self.activate()
        self.start_broker()
        route = self.api.notifications.route_id
        self.assertEqual(self.notifications.route_id, route)
        text = 'synthetic same source notification'
        payload = {'request_id': 'new-notification', 'text': text,
                   'fingerprint': text_fingerprint(text), 'route_id': route}
        first = self.clients['notifier'].request('/v1/mesh/notify', payload)
        second = self.clients['notifier'].request('/v1/mesh/notify', payload)
        self.assertEqual(first, second)
        with self.independent.transaction() as db:
            db.execute("UPDATE outbox SET status='unknown' WHERE id=?", (first['outbox_id'],))
            client_id = db.execute('SELECT client_id FROM outbox WHERE id=?', (first['outbox_id'],)).fetchone()[0]
        third = self.clients['notifier'].request('/v1/mesh/notify', payload)
        self.assertEqual('unknown', third['status'])
        with self.independent.transaction() as db:
            self.assertEqual(client_id, db.execute('SELECT client_id FROM outbox WHERE id=?',
                                                  (first['outbox_id'],)).fetchone()[0])
        self.assertEqual([], self.wire)

    def test_new_input_local_admission_and_lost_ack_readback_use_same_message_task(self):
        self.activate()
        self.start_broker()
        message = self.message('new-local-input')
        with self.independent.transaction() as db:
            self.independent._ingest_in_db(db, [message], 'new-cursor', 'fixture-owner', 'fixture-bot')
        local = self.TaskStore(self.root / 'local-task-store.sqlite', recover_inflight=False,
                      notification_policy={'mode': 'private'}, node_id='laptop')
        inbox = CommunicationInbox(local, 'laptop', self.independent.identity['communication_id'])
        identity = message_identity(message)
        self.clients['laptop'].request('/communication/prepare', {'message_id': identity, 'authority': 'laptop'})
        event = self.clients['laptop'].request('/communication/event', {'message_id': identity})
        inbox.deliver(event)  # actual original task commit; simulate lost ACK
        with mock.patch.object(inbox, 'deliver', side_effect=AssertionError('must not POST again')):
            reply = deliver_once(self.clients['laptop'], inbox, identity)
        self.assertEqual('confirmed', reply['state'])
        self.assertEqual(identity, reply['task_id'])
        self.assertEqual('confirmed', deliver_once(self.clients['laptop'], inbox, identity)['state'])
        with local.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
        # A later /pause belongs to that local task, not old VPS merely because
        # its transport originally came from the same personal account.
        command = self.message('pause-local', '/pause ' + identity)
        with self.independent.transaction() as db:
            self.independent._ingest_in_db(db, [command], 'next-cursor', 'fixture-owner', 'fixture-bot')
        self.assertEqual('laptop', self.independent.pending()[0]['required_authority'])

    def test_attempted_unknown_delivery_never_reroutes_or_reposts_and_other_input_remains_free(self):
        self.activate()
        messages = [self.message('unknown-first'), self.message('independent-second')]
        with self.independent.transaction() as db:
            self.independent._ingest_in_db(db, messages, 'cursor', 'fixture-owner', 'fixture-bot')
        first, second = map(message_identity, messages)
        self.assertTrue(self.independent.prepare_delivery(first, 'cloud')['fresh'])
        self.independent.mark_delivery_unknown(first, 'cloud')
        self.assertFalse(self.independent.prepare_delivery(first, 'cloud')['fresh'])
        with self.assertRaises(Conflict):
            self.independent.prepare_delivery(first, 'laptop')
        self.assertTrue(self.independent.prepare_delivery(second, 'laptop')['fresh'])
        command = self.message('pause-original', '/pause ' + message_identity(self.old))
        with self.independent.transaction() as db:
            self.independent._ingest_in_db(db, [command], 'cursor', 'fixture-owner', 'fixture-bot')
        with self.assertRaises(PermissionError):
            self.independent.prepare_delivery(message_identity(command), 'laptop')

    def test_role_cannot_grant_activate_read_another_executor_or_forge_source_node(self):
        self.activate()
        self.start_broker()
        for role, path, body in (('viewer', '/communication/activate', {}),
                ('carrier', '/channel-carrier/grant', {}), ('notifier', '/communication/pending', {'limit': 20}),
                ('laptop', '/communication/prepare', {'message_id': 'not-found', 'authority': 'cloud'})):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.clients[role].request(path, body)
            self.assertEqual(403, error.exception.code)
            error.exception.close()

    def test_atomic_poll_queue_and_cursor_rollback_on_partial_invalid_batch(self):
        self.activate()
        before = self.independent.get('cursor')
        invalid = self.message('invalid-batch-message')
        invalid['item_list'] = 'not-a-list'
        with self.assertRaises(ValueError):
            with self.independent.transaction() as db:
                self.independent._ingest_in_db(db, [self.message('valid-before-error'), invalid],
                    'must-not-commit', 'fixture-owner', 'fixture-bot')
        self.assertEqual(before, self.independent.get('cursor'))
        self.assertEqual([], self.independent.pending())

    def test_opt_in_source_producer_redirects_implicit_notices_but_never_old_unknown(self):
        producer = relay_store_class(self.TaskStore)(self.root / 'producer.sqlite', recover_inflight=False,
            notification_policy={'mode': 'private', 'owner_relay': {'peer': 'communication-laptop', 'authority': 'cloud'}},
            node_id='cloud')
        producer.ingest([self.message('producer-message')], 'projection', 'fixture-owner', 'fixture-bot')
        ack = 'ack-' + message_identity(self.message('producer-message'))
        # Raw message's timestamp/context do not affect a present message_id.
        with producer.transaction() as db:
            row = dict(db.execute('SELECT * FROM outbox WHERE id=?', (ack,)).fetchone())
            self.assertEqual('relay', row['delivery_route'])
            self.assertEqual(0, db.execute('SELECT post_attempted FROM notification_relays WHERE request_id=?',
                                         (ack,)).fetchone()[0])
            columns = tuple(self.old_send)
            db.execute('INSERT INTO outbox(' + ','.join(columns) + ') VALUES(' +
                       ','.join('?' for _ in columns) + ')', tuple(self.old_send[key] for key in columns))
        producer.enqueue('old-unknown', self.old_send['body'])
        with producer.transaction() as db:
            self.assertEqual(self.old_send, dict(db.execute("SELECT * FROM outbox WHERE id='old-unknown'").fetchone()))
            self.assertIsNone(db.execute("SELECT * FROM notification_relays WHERE request_id='old-unknown'").fetchone())

    def test_registered_project_control_stays_own_authority_and_cannot_hijack_old_ref(self):
        self.activate()
        self.independent.register_reference('laptop', 'new-local-project-child', digest('synthetic-row'),
                                            control_id='new-local-approval')
        command = self.message('control-new-local', '/approve new-local-approval')
        with self.independent.transaction() as db:
            self.independent._ingest_in_db(db, [command], 'next', 'fixture-owner', 'fixture-bot')
        self.assertTrue(self.independent.prepare_delivery(message_identity(command), 'laptop')['fresh'])
        with self.assertRaises(Conflict):
            self.independent.register_reference('laptop', message_identity(self.old), digest('wrong-row'))

    def test_independent_authority_relocation_keeps_unknown_delivery_queue_not_requeued(self):
        self.activate()
        message = self.message('unknown-during-broker-move')
        identity = message_identity(message)
        with self.independent.transaction() as db:
            self.independent._ingest_in_db(db, [message], 'move-cursor', 'fixture-owner', 'fixture-bot')
        self.independent.prepare_delivery(identity, 'laptop')
        self.independent.mark_delivery_unknown(identity, 'laptop')
        transfer = export_transfer(self.path, 'cloud', 'owner-ref', self.binding, 'broker-relocation')
        moved = install_prepared(transfer, self.root / 'moved-communication.sqlite', transfer['sha256'])
        self.assertEqual(self.independent.identity, moved.identity)
        self.assertEqual('unknown', moved.pending()[0]['state'])
        self.assertEqual('laptop', moved.pending()[0]['target_authority'])
        self.assertEqual(1, moved.pending()[0]['post_attempted'])
        self.assertFalse(moved.status()['activated'])

    def test_prepared_broker_executor_cannot_reconcile_or_mutate_imported_unknown(self):
        self.activate()
        message = self.message('original-unknown-before-prepared')
        identity = message_identity(message)
        with self.independent.transaction() as db:
            self.independent._ingest_in_db(db, [message], 'move-cursor', 'fixture-owner', 'fixture-bot')
        self.independent.prepare_delivery(identity, 'laptop')
        self.independent.mark_delivery_unknown(identity, 'laptop')
        transfer = export_transfer(self.path, 'cloud', 'owner-ref', self.binding, 'inactive-relocation')
        self.path = self.root / 'inactive-communication.sqlite'
        self.independent = install_prepared(transfer, self.path, transfer['sha256'])
        before = self.independent.pending()
        self.start_broker()  # disabled transport may inspect; no activation or carrier grant
        receipt = {'protocol': RECEIPT_PROTOCOL,
            'communication_id': self.independent.identity['communication_id'],
            'message_id': identity, 'body_sha256': digest(message),
            'execution_authority': 'laptop', 'task_id': None, 'accepted': 0}
        receipt['receipt_sha256'] = digest(receipt)
        for path, body in (('/communication/receipt', {'message_id': identity, 'receipt': receipt}),
                           ('/communication/unknown', {'message_id': identity})):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                self.clients['laptop'].request(path, body)
            self.assertEqual(403, error.exception.code)
            error.exception.close()
        self.assertEqual(before, self.independent.pending())
        self.assertFalse(self.independent.status()['activated'])
        self.assertEqual([], self.wire)


if __name__ == '__main__':
    unittest.main()
