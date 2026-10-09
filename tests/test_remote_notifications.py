"""Local authenticated API fixtures only: no real iLink receiver or send."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from assistant_mesh.channel import Channel, ChannelError
from assistant_mesh.networking import digest
from assistant_mesh.remote_notifications import (PROTOCOL, RemoteNotifications,
                                                  receipt_id, text_fingerprint)
from assistant_mesh.server import API, serve
from assistant_mesh.store import Conflict, Store


class RemoteNotificationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.account = {'baseurl': 'https://fixture.weixin.qq.com',
                        'bot_token': 'private-fixture-token',
                        'ilink_user_id': 'private-fixture-owner',
                        'ilink_bot_id': 'private-fixture-bot'}
        self.account_path = self.root / 'account.json'
        self.write_account()
        self.worker = {'role': 'worker', 'node': 'laptop', 'capabilities': ['owner.notify']}
        self.config = {'database': str(self.root / 'db'), 'node_id': 'cloud',
                       'ilink_account': str(self.account_path), 'peers': []}
        self.api = API(self.config)
        self.payload = {'request_id': 'notify.1:project/test', 'text': 'bounded fixture text',
                        'fingerprint': text_fingerprint('bounded fixture text'),
                        'route_id': self.api.notifications.route_id}

    def tearDown(self):
        self.tmp.cleanup()

    def write_account(self):
        self.account_path.write_text(json.dumps(self.account), encoding='utf8')
        self.account_path.chmod(0o600)

    def post(self, payload=None, peer=None, status=False, api=None):
        value = self.payload if payload is None else payload
        if status and payload is None:
            value = {key: self.payload[key] for key in ('request_id', 'fingerprint')}
        return (api or self.api).dispatch('POST', '/v1/mesh/notify/status' if status else '/v1/mesh/notify',
                                          value, self.worker if peer is None else peer)

    def rows(self, table='outbox'):
        with self.api.store.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]


class RemoteNotificationTests(RemoteNotificationFixture):
    def test_immutable_receive_receipt_and_exact_sanitized_metadata(self):
        result = self.post()
        self.assertEqual({'protocol', 'authority', 'route_id', 'source_node', 'request_id', 'fingerprint',
                          'outbox_id', 'found', 'status', 'idempotent_receive', 'delivery_verified'}, set(result))
        self.assertEqual(PROTOCOL, result['protocol'])
        self.assertEqual('cloud', result['authority'])
        self.assertEqual('laptop', result['source_node'])
        self.assertEqual(receipt_id('laptop', self.payload['request_id']), result['outbox_id'])
        self.assertTrue(result['found'])
        self.assertEqual('pending', result['status'])
        self.assertFalse(result['delivery_verified'])
        binding = digest([PROTOCOL, 'https://fixture.weixin.qq.com',
                          'private-fixture-owner', 'private-fixture-bot'])
        incarnation = self.api.store.get('owner_notification_receiver_incarnation')
        self.assertEqual(64, len(incarnation))
        self.assertEqual(digest([PROTOCOL, binding, incarnation]), result['route_id'])
        public = json.dumps(result)
        for private in (self.payload['text'], self.account['bot_token'], self.account['ilink_user_id'],
                        self.account['ilink_bot_id'], str(self.account_path)):
            self.assertNotIn(private, public)
        self.assertEqual('channel', self.rows()[0]['delivery_route'])
        self.assertIsNone(self.rows()[0]['media_items'])

    def test_missing_status_is_ready_proof_without_enqueue(self):
        result = self.post(status=True)
        self.assertFalse(result['found'])
        self.assertEqual('not_found', result['status'])
        self.assertEqual(self.api.notifications.route_id, result['route_id'])
        self.assertEqual([], self.rows())
        self.assertEqual([], self.rows('mesh_notify_receipts'))

    def test_repeat_receive_and_status_never_enqueue_again(self):
        first = self.post()
        before = self.rows(), self.rows('mesh_notify_receipts')
        with patch.object(self.api.store, '_enqueue', side_effect=AssertionError('re-enqueue')):
            self.assertEqual(first, self.post())
            self.assertEqual(first, self.post(status=True))
        self.assertEqual(before, (self.rows(), self.rows('mesh_notify_receipts')))

    def test_missing_invalid_or_changed_pinned_route_rejects_before_enqueue(self):
        missing = dict(self.payload)
        missing.pop('route_id')
        with patch.object(self.api.store, '_enqueue', side_effect=AssertionError('wrong route enqueue')):
            for body in (missing, dict(self.payload, route_id=None), dict(self.payload, route_id='A' * 64)):
                with self.assertRaises(ValueError):
                    self.post(body)
            with self.assertRaises(Conflict):
                self.post(dict(self.payload, route_id='0' * 64))
        self.assertEqual([], self.rows())
        self.assertEqual([], self.rows('mesh_notify_receipts'))

    def test_replaced_server_cannot_enqueue_to_new_owner_after_old_status(self):
        route = self.post(status=True)['route_id']
        self.account['ilink_user_id'] = 'different-fixture-owner'
        self.write_account()
        replaced = API(dict(self.config, database=str(self.root / 'replacement.db')))
        self.assertNotEqual(route, replaced.notifications.route_id)
        with patch.object(replaced.store, '_enqueue', side_effect=AssertionError('retargeted enqueue')):
            with self.assertRaises(Conflict):
                self.post(dict(self.payload, route_id=route), api=replaced)
        self.assertEqual({}, replaced.store.status()['outbox'])

    def test_lost_first_ack_new_database_same_account_cannot_reenqueue(self):
        ready = self.post(status=True)
        self.assertFalse(ready['found'])
        self.post()  # Simulate successful receive, but the sender loses its ACK.
        original_rows = self.rows(), self.rows('mesh_notify_receipts')
        replaced = API(dict(self.config, database=str(self.root / 'new-same-account.db')))
        missing = self.post(api=replaced, status=True)
        self.assertFalse(missing['found'])
        self.assertNotEqual(ready['route_id'], missing['route_id'])
        with patch.object(replaced.store, '_enqueue', side_effect=AssertionError('duplicate enqueue')):
            with self.assertRaises(Conflict):
                self.post(dict(self.payload, route_id=ready['route_id']), api=replaced)
        self.assertEqual({}, replaced.store.status()['outbox'])
        self.assertEqual(original_rows, (self.rows(), self.rows('mesh_notify_receipts')))

    def test_database_restart_preserves_receiver_incarnation_and_effective_route(self):
        incarnation = self.api.store.get('owner_notification_receiver_incarnation')
        route = self.post(status=True)['route_id']
        accepted = self.post()
        restarted = API(self.config)
        self.assertEqual(incarnation, restarted.store.get('owner_notification_receiver_incarnation'))
        self.assertEqual(route, restarted.notifications.route_id)
        with patch.object(restarted.store, '_enqueue', side_effect=AssertionError('restart replay')):
            self.assertEqual(accepted, self.post(api=restarted))
            self.assertEqual(accepted, self.post(api=restarted, status=True))

    def test_invalid_stored_incarnation_fails_closed_without_reset(self):
        key = 'owner_notification_receiver_incarnation'
        binding = self.api.store.get('owner_notification_channel_binding')
        original = self.api.store.get(key)
        for invalid in (None, True, 0, [], {}, '', '0' * 63, '0' * 65, 'A' * 64):
            self.api.store.set(key, invalid)
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    Conflict, 'owner_notification_incarnation_invalid'):
                API(self.config)
            self.assertEqual(invalid, self.api.store.get(key))
            self.assertEqual(binding, self.api.store.get('owner_notification_channel_binding'))
        with self.api.store.transaction() as db:
            db.execute('UPDATE meta SET value=? WHERE key=?', ('{not-json', key))
        with self.assertRaisesRegex(Conflict, 'owner_notification_incarnation_invalid'):
            API(self.config)
        with self.api.store.transaction() as db:
            self.assertEqual('{not-json', db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0])
        self.api.store.set(key, original)

    def test_binding_and_incarnation_initialization_roll_back_together(self):
        store = Store(self.root / 'binding-rollback.db')
        with store.transaction() as db:
            db.execute("CREATE TRIGGER fail_binding BEFORE INSERT ON meta WHEN NEW.key='owner_notification_channel_binding' BEGIN SELECT RAISE(ABORT,'fail'); END")
        with self.assertRaises(Exception):
            RemoteNotifications(store, 'cloud', route_id='0' * 64)
        self.assertIsNone(store.get('owner_notification_channel_binding'))
        self.assertIsNone(store.get('owner_notification_receiver_incarnation'))
        with store.transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mesh_notify_receipts'").fetchone())

    def test_same_id_different_content_conflicts_without_mutation(self):
        self.post()
        before = self.rows(), self.rows('mesh_notify_receipts')
        other = dict(self.payload, text='different', fingerprint=text_fingerprint('different'))
        with self.assertRaises(Conflict):
            self.post(other)
        with self.assertRaises(Conflict):
            self.post({'request_id': self.payload['request_id'], 'fingerprint': other['fingerprint']}, status=True)
        self.assertEqual(before, (self.rows(), self.rows('mesh_notify_receipts')))

    def test_distinct_principals_do_not_alias_or_read_each_other(self):
        first = self.post()
        second_peer = dict(self.worker, role='agent_peer', node='other')
        absent = self.post(peer=second_peer, status=True)
        self.assertFalse(absent['found'])
        second = self.post(peer=second_peer)
        self.assertNotEqual(first['outbox_id'], second['outbox_id'])
        self.assertEqual(2, len(self.rows()))

    def test_unicode_source_node_is_supported_without_identity_alias(self):
        result = self.post(peer=dict(self.worker, node='笔记本'))
        self.assertEqual('笔记本', result['source_node'])
        self.assertNotEqual(receipt_id('laptop', self.payload['request_id']), result['outbox_id'])

    def test_roles_node_and_explicit_capability_are_required_for_both_routes(self):
        peers = [dict(self.worker, role='operator'), dict(self.worker, role='viewer'),
                 dict(self.worker, role='agent'), dict(self.worker, node=''),
                 dict(self.worker, node=None), dict(self.worker, capabilities=[]),
                 dict(self.worker, capabilities='owner.notify')]
        for peer in peers:
            for status in (False, True):
                with self.subTest(peer=peer, status=status), self.assertRaises(PermissionError):
                    self.post(peer=peer, status=status)
        self.assertEqual([], self.rows())

    def test_revoked_capability_denies_late_status_and_receive(self):
        self.post()
        self.worker['capabilities'].remove('owner.notify')
        for status in (False, True):
            with self.assertRaises(PermissionError):
                self.post(status=status)
        self.assertEqual(1, len(self.rows()))

    def test_private_or_absent_channel_rejects_before_enqueue(self):
        for index, config in enumerate((dict(self.config, ilink_account=None),
                                       dict(self.config, notification_policy={'mode': 'private'}),
                                       dict(self.config, node_id=None))):
            config['database'] = str(self.root / ('denied-%d.db' % index))
            api = API(config)
            with patch.object(api.store, '_enqueue', side_effect=AssertionError('must reject first')):
                for status in (False, True):
                    with self.assertRaises(PermissionError):
                        self.post(api=api, status=status)
            self.assertEqual({}, api.store.status()['outbox'])

    def test_unconfigured_direct_relay_cannot_make_readiness_claim(self):
        relay = RemoteNotifications(self.api.store, 'cloud')
        with self.assertRaises(PermissionError):
            relay.receive('laptop', self.payload)
        with self.assertRaises(PermissionError):
            relay.status('laptop', {key: self.payload[key] for key in ('request_id', 'fingerprint')})

    def test_only_post_without_query_is_authorized(self):
        for method, path in (('GET', '/v1/mesh/notify'), ('GET', '/v1/mesh/notify/status'),
                             ('POST', '/v1/mesh/notify?actor=operator'),
                             ('POST', '/v1/mesh/notify/status?target=other')):
            with self.assertRaises(PermissionError):
                self.api.dispatch(method, path, self.payload, self.worker)

    def test_exact_payload_rejects_identity_target_and_context_fields(self):
        for key in ('node', 'source_node', 'actor', 'role', 'target', 'recipient', 'to',
                    'channel', 'token', 'context', 'protocol', 'authority'):
            for status in (False, True):
                value = {key: self.payload[key] for key in ('request_id', 'fingerprint')} if status else dict(self.payload)
                value[key] = 'claimed-privilege'
                with self.subTest(key=key, status=status), self.assertRaises(ValueError):
                    self.post(value, status=status)
        for invalid in ([], None, {}, {'request_id': 'x'}, {'fingerprint': '0' * 64}):
            with self.assertRaises(ValueError):
                self.api.dispatch('POST', '/v1/mesh/notify', invalid, self.worker)

    def test_strict_request_id_and_fingerprint_validation(self):
        for identity in ('', 'a' * 201, '\nabc', 'abc\n', 'with space', '中文', '/start',
                         True, 3, ['list'], 'nul\x00byte', 'tab\tbyte', 'del\x7fbyte'):
            for status in (False, True):
                value = dict(self.payload, request_id=identity)
                if status:
                    value.pop('text')
                with self.subTest(identity=identity, status=status), self.assertRaises(ValueError):
                    self.post(value, status=status)
        for expected in ('', '0' * 63, '0' * 65, 'A' * 64, 'g' * 64, True, None, '0' * 64 + '\n'):
            with self.subTest(fingerprint=expected), self.assertRaises(ValueError):
                self.post(dict(self.payload, fingerprint=expected))

    def test_text_fingerprint_utf8_byte_limit_and_surrogate_validation(self):
        for text in ('', 'a' * 16001, '中' * 5334, True, None, [], '\ud800'):
            with self.subTest(text_type=type(text).__name__), self.assertRaises(ValueError):
                self.post(dict(self.payload, text=text))
        with self.assertRaises(ValueError):
            self.post(dict(self.payload, fingerprint='0' * 64))
        for index, text in enumerate(('a' * 16000, '中' * 5333 + 'x')):
            value = dict(self.payload, request_id='limit-%d' % index, text=text, fingerprint=text_fingerprint(text))
            self.assertTrue(self.post(value)['found'])

    def test_unknown_waiting_auth_and_other_statuses_are_never_replayed(self):
        outbox = self.post()['outbox_id']
        for status in ('pending', 'submitting', 'accepted', 'rejected', 'unknown', 'waiting_auth'):
            with self.api.store.transaction() as db:
                db.execute('UPDATE outbox SET status=?,detail=? WHERE id=?',
                           (status, json.dumps({'token': 'private-detail', 'message_id': 'private-server-id'}), outbox))
            before = self.rows()
            with patch.object(self.api.store, '_enqueue', side_effect=AssertionError('replay')):
                for query in (False, True):
                    result = self.post(status=query)
                    self.assertEqual(status, result['status'])
                    self.assertFalse(result['delivery_verified'])
                    self.assertNotIn('private-detail', json.dumps(result))
            self.assertEqual(before, self.rows())

    def test_channel_send_uses_bound_owner_without_exposing_server_receipt(self):
        calls = []
        def transport(endpoint, body, timeout):
            calls.append((endpoint, body, timeout))
            return {'message_id': 'private-fixture-server-receipt'}
        self.api.ilink.request_override = transport
        channel = Channel(self.api.store, self.api.ilink)
        self.post()
        self.assertTrue(channel.send_once())
        self.assertFalse(channel.send_once())
        self.assertEqual(1, len(calls))
        self.assertEqual('sendmessage', calls[0][0])
        self.assertEqual(self.account['ilink_user_id'], calls[0][1]['msg']['to_user_id'])
        response = self.post(status=True)
        self.assertEqual('accepted', response['status'])
        self.assertFalse(response['delivery_verified'])
        self.assertNotIn('private-fixture-server-receipt', json.dumps(response))

    def test_actual_channel_timeout_is_unknown_and_receive_does_not_resend(self):
        calls = []
        def timeout(endpoint, body, seconds):
            calls.append(endpoint)
            raise ChannelError('network_timeout')
        self.api.ilink.request_override = timeout
        channel = Channel(self.api.store, self.api.ilink)
        self.post()
        self.assertTrue(channel.send_once())
        for status in (False, True):
            self.assertEqual('unknown', self.post(status=status)['status'])
        self.assertFalse(channel.send_once())
        self.assertEqual(['sendmessage'], calls)
        self.assertEqual(1, len(self.rows()))

    def test_receipt_and_enqueue_rollback_together(self):
        with self.api.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_notification BEFORE INSERT ON mesh_notify_receipts BEGIN SELECT RAISE(ABORT,'fail'); END")
        with self.assertRaises(Exception):
            self.post()
        self.assertEqual([], self.rows())
        self.assertEqual([], self.rows('mesh_notify_receipts'))

    def test_legacy_matching_outbox_collision_fails_closed(self):
        identity = receipt_id('laptop', self.payload['request_id'])
        self.api.store.enqueue(identity, self.payload['text'])
        before = self.rows()
        with self.assertRaises(Conflict):
            self.post()
        self.assertEqual(before, self.rows())
        self.assertEqual([], self.rows('mesh_notify_receipts'))

    def test_broken_receipt_missing_outbox_is_not_reported_healthy(self):
        identity = self.post()['outbox_id']
        with self.api.store.transaction() as db:
            db.execute('DELETE FROM outbox WHERE id=?', (identity,))
        for query in (False, True):
            with self.assertRaises(Conflict):
                self.post(status=query)
        self.assertEqual([], self.rows())

    def test_tampered_outbox_or_receipt_cannot_be_rebound_or_repaired(self):
        mutations = (('outbox', 'fingerprint', '0' * 64), ('outbox', 'body', 'tampered'),
                     ('outbox', 'media_items', '[]'), ('outbox', 'delivery_route', 'private'),
                     ('outbox', 'status', 'claimed-delivered'),
                     ('mesh_notify_receipts', 'outbox_id', 'other'),
                     ('mesh_notify_receipts', 'fingerprint', '0' * 64))
        for table, column, changed in mutations:
            with self.subTest(table=table, column=column):
                self.post()
                with self.api.store.transaction() as db:
                    original = db.execute('SELECT ' + column + ' FROM ' + table).fetchone()[0]
                    db.execute('UPDATE ' + table + ' SET ' + column + '=?', (changed,))
                before = self.rows(), self.rows('mesh_notify_receipts')
                for status in (False, True):
                    with self.assertRaises(Conflict):
                        self.post(status=status)
                self.assertEqual(before, (self.rows(), self.rows('mesh_notify_receipts')))
                with self.api.store.transaction() as db:
                    db.execute('UPDATE ' + table + ' SET ' + column + '=?', (original,))

    def test_channel_binding_is_persistent_and_not_token_or_context_dependent(self):
        binding = self.api.notifications.route_id
        account_binding = self.api.store.get('owner_notification_channel_binding')
        self.account.update(bot_token='new-private-token', cursor='different', owner_context={'token': 'secret'})
        self.write_account()
        self.assertEqual(binding, API(self.config).notifications.route_id)
        self.assertEqual(account_binding, self.api.store.get('owner_notification_channel_binding'))
        self.assertTrue(self.post()['found'])

    def test_changed_owner_bot_or_origin_refuses_startup_without_retargeting(self):
        self.post()
        before = self.rows()
        binding = self.api.store.get('owner_notification_channel_binding')
        for key, new in (('ilink_user_id', 'other-owner'), ('ilink_bot_id', 'other-bot'),
                         ('baseurl', 'https://alternate.weixin.qq.com')):
            old = self.account[key]
            self.account[key] = new
            self.write_account()
            with self.assertRaises(Conflict):
                API(self.config)
            self.assertEqual(binding, self.api.store.get('owner_notification_channel_binding'))
            self.assertEqual(before, self.rows())
            self.account[key] = old
        self.write_account()

    def test_unconfigured_startup_never_resets_persisted_binding(self):
        binding = self.api.notifications.route_id
        account_binding = self.api.store.get('owner_notification_channel_binding')
        cold = API(dict(self.config, ilink_account=None))
        self.assertIsNone(cold.notifications.route_id)
        self.assertEqual(account_binding, cold.store.get('owner_notification_channel_binding'))
        self.assertEqual(binding, API(self.config).notifications.route_id)

    def test_account_file_edit_does_not_change_pinned_live_sender(self):
        binding = self.api.notifications.route_id
        self.account['ilink_user_id'] = 'unexpected-owner'
        self.write_account()
        result = self.post()
        self.assertEqual(binding, result['route_id'])
        self.assertEqual('private-fixture-owner', self.api.ilink.account['ilink_user_id'])

    def test_parallel_cold_api_schema_startup_and_duplicate_receive(self):
        errors, results = [], []
        cold_config = dict(self.config, database=str(self.root / 'cold.db'))
        barrier = threading.Barrier(8)
        def start():
            try:
                barrier.wait(5)
                api = API(cold_config)
                ready = self.post(api=api, status=True)
                results.append(self.post(dict(self.payload, route_id=ready['route_id']), api=api))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=start) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(8, len(results))
        self.assertTrue(all(result == results[0] for result in results))
        cold = Store(cold_config['database'], recover_inflight=False)
        with cold.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM mesh_notify_receipts').fetchone()[0])

    def test_receipt_namespace_hash_has_no_concatenation_alias(self):
        self.assertNotEqual(receipt_id('a:b', 'c'), receipt_id('a', 'b:c'))
        self.assertEqual('mesh-notify-' + hashlib.sha256(
            '["mesh-notify/1","laptop","x"]'.encode('utf8')).hexdigest(), receipt_id('laptop', 'x'))

    def test_local_agent_explicit_relay_notify_requires_its_own_capability(self):
        config = dict(self.config, database=str(self.root / 'local-relay.db'), node_id='laptop',
                      ilink_account=None, notification_policy={
                          'mode': 'private', 'owner_relay': {'peer': 'cloud', 'authority': 'cloud'}})
        api = API(config)
        api.store.heartbeat('laptop', ['agent'])
        api.store.create_task('local fixture', required=['agent'])
        task = api.store.claim('laptop')
        payload = {'task_id': task['id'], 'epoch': task['epoch'], 'call_id': 'notify-fixture',
                   'action': 'notify', 'arguments': {'text': 'explicit fixture notice'}}
        for grants in ([], 'owner.notify'):
            with self.assertRaises(PermissionError):
                api.dispatch('POST', '/v1/agent/action', payload, dict(self.worker, capabilities=grants))
        self.assertEqual({}, api.store.status()['outbox'])
        result = api.dispatch('POST', '/v1/agent/action', payload, self.worker)
        self.assertEqual('relay', result['delivery_route'])
        with api.store.transaction() as db:
            self.assertEqual('relay', db.execute('SELECT delivery_route FROM outbox').fetchone()[0])

    def test_local_private_notify_without_relay_does_not_need_channel_grant(self):
        api = API(dict(self.config, database=str(self.root / 'local-private.db'), node_id='laptop',
                       ilink_account=None, notification_policy={'mode': 'private'}))
        api.store.heartbeat('laptop', ['agent'])
        api.store.create_task('local fixture', required=['agent'])
        task = api.store.claim('laptop')
        result = api.dispatch('POST', '/v1/agent/action', {
            'task_id': task['id'], 'epoch': task['epoch'], 'call_id': 'private-notify',
            'action': 'notify', 'arguments': {'text': 'only private ledger'}},
                             dict(self.worker, capabilities=[]))
        self.assertEqual('recorded_private', result['status'])
        self.assertEqual('private', result['delivery_route'])
        self.assertFalse(result['delivery_verified'])


class DormantChannel:
    """Suppress both channel background loops in the HTTP acceptance fixture."""
    def __init__(self, store, transport):
        self.store, self.transport, self.stop = store, transport, threading.Event()

    def run_poll(self):
        pass

    def run_send(self):
        pass


class RemoteNotificationHTTPTests(RemoteNotificationFixture):
    def setUp(self):
        super().setUp()
        self.tokens = {'worker': 'w' * 64, 'other': 'x' * 64, 'peer': 'p' * 64,
                       'operator': 'o' * 64, 'viewer': 'v' * 64, 'denied': 'd' * 64}
        peers = [('worker', self.worker), ('other', dict(self.worker, node='other')),
                 ('peer', dict(self.worker, role='agent_peer', node='agent-peer')),
                 ('operator', dict(self.worker, role='operator')),
                 ('viewer', dict(self.worker, role='viewer')),
                 ('denied', dict(self.worker, capabilities=[]))]
        config = dict(self.config, port=0, peers=[])
        for name, peer in peers:
            token_file = self.root / (name + '.token')
            token_file.write_text(self.tokens[name])
            token_file.chmod(0o600)
            config['peers'].append(dict(peer, token_file=str(token_file)))
        ready, self.holder = threading.Event(), {}
        def started(server, api, channel):
            self.holder.update(server=server, api=api, channel=channel)
            ready.set()
        self.channel_patch = patch('assistant_mesh.server.Channel', DormantChannel)
        self.channel_patch.start()
        self.thread = threading.Thread(target=serve, args=(config, started), daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(5))
        self.url = 'http://127.0.0.1:%d' % self.holder['server'].server_address[1]
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.holder['server'].shutdown()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.channel_patch.stop()
        super().tearDown()

    def http(self, token='worker', payload=None, status=False, path=None):
        body = self.payload if payload is None else payload
        if status and payload is None:
            body = {key: self.payload[key] for key in ('request_id', 'fingerprint')}
        request = Request(self.url + (path or ('/v1/mesh/notify/status' if status else '/v1/mesh/notify')),
                          data=json.dumps(body, ensure_ascii=True).encode('utf8'),
                          headers={'Authorization': 'Bearer ' + self.tokens.get(token, token),
                                   'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode('utf8'))
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode('utf8'))

    def test_http_authenticated_node_bound_receiver(self):
        for name, code in (('incorrect', 401), ('operator', 403), ('viewer', 403), ('denied', 403),
                           ('worker', 200), ('peer', 200)):
            for status in (True, False):
                self.assertEqual(code, self.http(name, status=status)[0])
        self.assertEqual(2, len(self.rows()))
        self.assertIsInstance(self.holder['channel'], DormantChannel)

    def test_http_lost_response_repeats_original_id_without_new_send(self):
        self.http()  # Owner fixture deliberately discards the returned receipt.
        before = self.rows()
        with patch.object(self.holder['api'].store, '_enqueue', side_effect=AssertionError('second enqueue')):
            code, receipt = self.http(status=True)
            self.assertEqual(200, code)
            self.assertTrue(receipt['found'])
            self.assertEqual(receipt, self.http()[1])
        self.assertEqual(before, self.rows())

    def test_http_parallel_duplicate_requests_create_one_receipt_and_outbox(self):
        results, errors = [], []
        barrier = threading.Barrier(8)
        def send():
            try:
                barrier.wait(5)
                results.append(self.http())
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=send) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(8, len(results))
        self.assertTrue(all(code == 200 for code, body in results))
        self.assertTrue(all(body == results[0][1] for code, body in results))
        self.assertEqual(1, len(self.rows()))
        self.assertEqual(1, len(self.rows('mesh_notify_receipts')))

    def test_http_fingerprint_conflict_bad_body_and_missing_queries(self):
        self.assertEqual(200, self.http(status=True)[0])
        self.assertEqual([], self.rows())
        self.assertEqual(200, self.http()[0])
        changed = dict(self.payload, text='changed', fingerprint=text_fingerprint('changed'))
        self.assertEqual(409, self.http(payload=changed)[0])
        for bad in (dict(self.payload, fingerprint='0' * 64), dict(self.payload, actor='operator'),
                    dict(self.payload, text='\ud800'), dict(self.payload, route_id=None), []):
            code, response = self.http(payload=bad)
            self.assertEqual(400, code)
            self.assertEqual({'error': 'invalid_request'}, response)
        self.assertEqual(1, len(self.rows()))

    def test_http_wrong_pinned_route_conflicts_without_receipt_or_send(self):
        code, body = self.http(payload=dict(self.payload, route_id='0' * 64))
        self.assertEqual(409, code)
        self.assertEqual({'error': 'notification_channel_binding_mismatch'}, body)
        self.assertEqual([], self.rows())
        self.assertEqual([], self.rows('mesh_notify_receipts'))

    def test_http_revoked_peer_cannot_query_existing_receipt(self):
        self.assertEqual(200, self.http()[0])
        authenticated_peer = next(peer for token, peer in self.holder['api'].peers
                                  if peer['node'] == 'laptop' and peer['role'] == 'worker')
        authenticated_peer['capabilities'] = []
        for status in (False, True):
            self.assertEqual((403, {'error': 'route_not_authorized'}), self.http(status=status))
        self.assertEqual(1, len(self.rows()))


if __name__ == '__main__':
    unittest.main()
