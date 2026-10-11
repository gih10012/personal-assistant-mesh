"""Real authenticated loopback RPC; synthetic iLink, NOT production HA."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError

from assistant_mesh.channel import Channel, ChannelError
from assistant_mesh.channel_authority import RemoteChannelState
from assistant_mesh.channel_carrier import CarrierError, CarrierFence, PROTOCOL
from assistant_mesh.networking import digest
from assistant_mesh.remote_notifications import PROTOCOL as NOTIFY_PROTOCOL
from assistant_mesh.server import API, serve
from assistant_mesh.worker import Client
from tests import test_channel_carrier as carrier_tests


class ChannelAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.account = {'baseurl': 'https://fixture.weixin.qq.com', 'bot_token': 'synthetic-private-token',
                        'ilink_user_id': 'fixture-owner', 'ilink_bot_id': 'fixture-bot'}
        path = self.root / 'account.json'
        path.write_text(json.dumps(self.account))
        path.chmod(0o600)
        self.binding = digest([NOTIFY_PROTOCOL, self.account['baseurl'], 'fixture-owner', 'fixture-bot'])
        peers = []
        for name in ('laptop', 'cloud', 'operator', 'viewer', 'worker'):
            token = self.root / (name + '.token')
            token.write_text(name + '-' + 'a' * 64)
            token.chmod(0o600)
            peer = {'role': name if name in ('operator', 'viewer') else 'worker', 'token_file': str(token)}
            if name in ('laptop', 'cloud'):
                peer.update(role='channel_carrier', node=name, channel={
                    'owner_ref': 'fixture-self', 'canonical_authority': 'cloud', 'account_binding': self.binding})
            elif name == 'worker':
                peer.update(node='worker', capabilities=['owner.notify'])
            peers.append(peer)
        self.config = {'node_id': 'cloud', 'port': 0, 'database': str(self.root / 'original.db'),
            'peers': peers, 'ilink_account': str(path), 'channel_carrier': {
                'witness_id': 'fixture-witness', 'owner_ref': 'fixture-self', 'transport_mode': 'disabled'}}
        ready = threading.Event()
        self.server, self.api = None, None
        def started(server, api, channel):
            self.server, self.api, self.local_channel = server, api, channel
            ready.set()
        self.thread = threading.Thread(target=serve, args=(self.config, started), daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(5))
        url = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.clients = {name: Client({'control_url': url, 'token_file': str(self.root / (name + '.token'))})
                        for name in ('laptop', 'cloud', 'operator', 'viewer', 'worker')}
        self.authority = self.api.channel_authority
        self.witness = self.authority.witness
        # Test-only trusted verifier; never installed in deployment config.
        self.witness.handoff_verifier = carrier_tests.ChannelCarrierTests.handoff
        self.api.store.set('cursor', 'original-cursor')
        self.api.store.set('owner_context', {'token': 'original-context', 'version': 'original-context-version'})
        self.wire = []
        def transport(endpoint, body, timeout):
            self.wire.append((endpoint, body))
            if endpoint == 'getupdates':
                return {'msgs': [], 'get_updates_buf': 'next-cursor'}
            return {'message_id': 'synthetic-provider-accept'}
        self.api.ilink.request_override = transport

    def tearDown(self):
        if self.server:
            self.server.shutdown()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def request(self, action, payload, node='laptop'):
        return self.clients[node].request('/channel-carrier/' + action, payload)

    def failure(self, name, path, payload, code):
        with self.assertRaises(HTTPError) as result:
            self.clients[name].request(path, payload)
        self.assertEqual(code, result.exception.code)
        result.exception.close()

    def grant(self, node='laptop'):
        term = self.witness.inspect(self.binding)
        self.request('candidate', {'contract': {'protocol': PROTOCOL, 'node': node,
            'owner_ref': 'fixture-self', 'canonical_authority': 'cloud', 'account_binding': self.binding,
            'private_credential_ref': 'synthetic-private-reference', 'revision': 1,
            'runtime': {'adapter': 'ilink/0.3.0', 'fence_protocol': PROTOCOL,
                        'transport_available': True, 'private_state': True}}, 'ttl': 300}, node)
        if not term['frozen']:
            self.witness.freeze(self.binding, term['epoch'])
        return self.witness.grant(self.binding, term['epoch'], node, 60, 'fixture-proof')

    def remote(self, term, node='laptop'):
        state = RemoteChannelState(self.clients[node], str(self.root / (node + '-spool.db')))
        return Channel(state, self.api.ilink, CarrierFence(self.clients[node], term)), state

    def outbox(self, request='original-send'):
        with self.api.store.transaction() as db:
            return dict(db.execute('SELECT * FROM outbox WHERE id=?', (request,)).fetchone())

    def test_disabled_authority_retains_notification_route_without_receiver(self):
        self.assertIsNone(self.local_channel)
        self.assertIsNotNone(self.api.ilink)
        original_route = self.api.notifications.route_id
        text = 'synthetic bounded notification'
        receipt = self.clients['worker'].request('/v1/mesh/notify', {
            'request_id': 'original-notify', 'text': text, 'fingerprint': __import__('hashlib').sha256(text.encode()).hexdigest(),
            'route_id': original_route})
        self.assertEqual('pending', receipt['status'])
        self.assertEqual(original_route, receipt['route_id'])
        self.assertEqual([], self.wire)

    def test_authenticated_remote_send_and_poll_commit_original_authority(self):
        self.api.store.enqueue('original-send', 'original private body')
        original_client = self.outbox()['client_id']
        channel, state = self.remote(self.grant())
        self.assertTrue(channel.send_once())
        self.assertFalse(channel.send_once())
        self.assertEqual('accepted', self.outbox()['status'])
        self.assertEqual(original_client, self.outbox()['client_id'])
        self.assertEqual(original_client, self.wire[0][1]['msg']['client_id'])
        self.assertEqual(0, channel.poll_once())
        self.assertEqual('next-cursor', self.api.store.get('cursor'))
        with self.api.store.transaction() as db:
            rows = list(db.execute('SELECT * FROM channel_canonical_intents'))
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row['persistence_sha256'] for row in rows))
        self.assertTrue(all(state.spool(row['operation_id'])['outcome'] for row in rows))
        self.assertEqual(2, len(self.wire))

    def test_two_carriers_controlled_handoff_uses_same_outbox_and_cursor(self):
        first, unused = self.remote(self.grant('laptop'), 'laptop')
        self.api.store.enqueue('original-send', 'one')
        first.send_once()
        first.poll_once()
        second, unused = self.remote(self.grant('cloud'), 'cloud')
        self.assertFalse(second.send_once())
        self.api.store.enqueue('second-send', 'two')
        second.send_once()
        second.poll_once()
        with self.assertRaises(HTTPError) as stale:
            first.store.channel_prepare_poll(first.carrier_fence.term, 'next-cursor')
        self.assertEqual(409, stale.exception.code)
        stale.exception.close()
        sends = [body['msg']['client_id'] for endpoint, body in self.wire if endpoint == 'sendmessage']
        self.assertEqual(2, len(sends))
        self.assertEqual(2, len(set(sends)))

    def test_carrier_cannot_access_any_global_or_operator_worker_routes(self):
        for path, body in (('/v1/status', None), ('/v1/resources', None), ('/v1/mesh/hello', None),
                           ('/v1/heartbeat', {}), ('/v1/task', {}), ('/v1/task/continue', {}),
                           ('/v1/mesh/notify', {}), ('/v1/allocation/action', {}),
                           ('/channel-carrier/grant', {}), ('/channel-carrier/freeze', {})):
            self.failure('laptop', path, body, 403)
        for name in ('operator', 'viewer', 'worker'):
            self.failure(name, '/channel-carrier/inspect', {}, 403)
        for path in ('/channel-carrier/inspect?x=1', '/channel-carrier/inspect?'):
            if path.endswith('?'):
                # A literal empty query is normalized by HTTP transport; direct
                # server dispatch must still reject ambiguous syntax later.
                continue
            self.failure('laptop', path, {}, 403)

    def test_candidate_binding_or_actor_cannot_be_selected_in_payload(self):
        term = self.grant('laptop')
        self.failure('cloud', '/channel-carrier/state/prepare_poll', {'term': term, 'cursor': 'original-cursor'}, 403)
        self.failure('laptop', '/channel-carrier/state/get', {'key': 'native_sessions', 'default': None}, 409)
        self.failure('laptop', '/channel-carrier/inspect', {'actor': 'operator'}, 409)

    def test_raw_digest_cannot_claim_canonical_result_or_persistence(self):
        term = self.grant()
        self.api.store.enqueue('original-send', 'body')
        prepared = self.request('state/prepare_send', {'term': term})
        row, operation = prepared['row'], prepared['operation_id']
        identity = {'authority': 'cloud', 'outbox_id': row['id'], 'client_id': row['client_id']}
        begin = self.request('begin', {'term': term, 'operation_id': operation, 'kind': 'send',
            'identity': identity, 'request_sha256': digest({'endpoint': 'sendmessage', 'body': self.api.ilink.send_body(row)})})
        ticket = CarrierFence._ticket(self.request('dispatch', {'ticket': CarrierFence._ticket(begin)}))
        self.failure('laptop', '/channel-carrier/returned', {'ticket': ticket, 'sha256': digest('invented')}, 409)
        self.failure('laptop', '/channel-carrier/committed', {'ticket': ticket, 'sha256': digest('invented')}, 409)
        self.assertEqual([], self.wire)

    def test_result_rpc_ack_loss_keeps_local_full_spool_and_original_unknown(self):
        term = self.grant()
        self.api.store.enqueue('original-send', 'body')
        original = self.outbox()['client_id']
        real = self.clients['laptop']
        class LostWriteACK:
            def request(self, path, body=None):
                if path.endswith('/record_result'):
                    raise OSError('fixture: failed before authority write')
                return real.request(path, body)
        state = RemoteChannelState(LostWriteACK(), str(self.root / 'lost-spool.db'))
        channel = Channel(state, self.api.ilink, CarrierFence(real, term))
        with self.assertRaises(ChannelError):
            channel.send_once()
        with self.api.store.transaction() as db:
            operation = db.execute('SELECT operation_id FROM channel_canonical_intents').fetchone()[0]
        reopened = RemoteChannelState(real, str(self.root / 'lost-spool.db'))
        self.assertEqual('accepted', reopened.spool(operation)['outcome']['status'])
        self.assertEqual('unknown', self.outbox()['status'])
        self.assertEqual(original, self.outbox()['client_id'])
        self.assertEqual(1, len(self.wire))
        self.witness.freeze(self.binding, term['epoch'])
        with self.assertRaisesRegex(CarrierError, 'carrier_unsettled_intent'):
            self.witness.grant(self.binding, term['epoch'], 'cloud', 60, 'fixture-proof')

    def test_full_large_poll_batch_crosses_128k_but_remains_bounded(self):
        channel, state = self.remote(self.grant())
        payload = {'msgs': [], 'get_updates_buf': 'next-cursor', 'private_raw': 'x' * (150 * 1024)}
        self.api.ilink.request_override = lambda *unused: payload
        self.assertEqual(0, channel.poll_once())
        with self.api.store.transaction() as db:
            row = db.execute('SELECT outcome FROM channel_canonical_intents').fetchone()
        self.assertEqual(payload, json.loads(row[0])['batch'])
        self.failure('operator', '/v1/task', {'raw': 'x' * (150 * 1024)}, 413)

    def test_only_original_trusted_reconciliation_unblocks_unknown_poll_after_handoff(self):
        channel, state = self.remote(self.grant())
        def uncertain(*unused):
            raise ChannelError('network_timeout')
        self.api.ilink.request_override = uncertain
        with self.assertRaises(ChannelError):
            channel.poll_once()
        with self.api.store.transaction() as db:
            operation = db.execute('SELECT operation_id FROM channel_canonical_intents').fetchone()[0]
        original = self.authority.state.receipt(operation)
        self.witness.freeze(self.binding, 1)
        def original_proof(receipt, proof_ref):
            proof = carrier_tests.ChannelCarrierTests.operation_proof(receipt, proof_ref)
            proof['persistence_sha256'] = original['persistence_sha256']
            return proof
        self.witness.operation_verifier = original_proof  # synthetic fixture only
        self.witness.reconcile(self.binding, operation, 'fixture-original-quiescence')
        next_channel, unused = self.remote(self.grant('cloud'), 'cloud')
        self.api.ilink.request_override = lambda *unused: {'msgs': [], 'get_updates_buf': 'next-cursor'}
        self.assertEqual(0, next_channel.poll_once())
        self.assertEqual(original, self.authority.state.receipt(operation))
        self.assertEqual('unknown', state.spool(operation)['outcome']['status'])

    def test_duplicate_carrier_privileged_token_rejected_before_business_db_open(self):
        config = copy.deepcopy(self.config)
        config['database'] = str(self.root / 'must-not-open.db')
        config['peers'][0]['token_file'] = config['peers'][2]['token_file']
        with self.assertRaisesRegex(ValueError, 'ambiguous_channel_carrier_credential'):
            API(config)
        self.assertFalse(Path(config['database']).exists())

    def test_server_default_has_no_grant_verifier_even_after_candidate_report(self):
        self.witness.handoff_verifier = None
        with self.assertRaisesRegex(CarrierError, 'carrier_handoff_verifier_missing'):
            self.grant()
        self.assertIsNone(self.witness.inspect(self.binding)['holder'])
        self.assertEqual([], self.wire)
