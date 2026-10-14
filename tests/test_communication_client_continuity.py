"""Actual exact old CLI/SQLite/loopback HTTP; synthetic account/proofs only.

fixtures/claw_remote_original.py is the owner's inspected remote module,
SHA-pinned by the source-overlay builder. No personal state or WeChat IO.
"""
import copy
import hashlib
import importlib
import importlib.util
import json
import sys
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from assistant_mesh.communication_client import PROTOCOL, ORIGIN_HEADER, IDENTITY_HEADER, owner_request
from assistant_mesh.communication_inbox import CommunicationInbox
from assistant_mesh.communication_migration import export_transfer, install_prepared, verify_prepared
from assistant_mesh.communication_producer import relay_store_class
from assistant_mesh.communication_runtime import Dispatcher
from assistant_mesh.communication_node import validate_configs
from assistant_mesh.networking import digest
from assistant_mesh.notification_relay import NotificationRelay
from tests import test_communication_faultdomain as fixtures
from tests.test_legacy_fenced_receiver import _source_base


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class PrivateAccess:
    @staticmethod
    def private_read(path):
        return Path(path).read_text()
    @staticmethod
    def private_write(path, value):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(0o600)


class CommunicationClientContinuityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.CommunicationFaultDomainTests.setUpClass.__func__(cls)

    def setUp(self):
        fixtures.CommunicationFaultDomainTests.setUp(self)
        self.origin = 'http://127.0.0.1:19999'
        token = self.root / 'owner-client.token'
        token.write_text('owner-client-' + 'z' * 64)
        token.chmod(0o600)
        notify = self.root / 'local-notify.token'
        notify.write_text('local-notify-' + 'z' * 64)
        notify.chmod(0o600)
        self.additional_peers = [
            {'role': 'owner_client', 'token_file': str(token), 'owner_ref': 'owner-ref',
             'origin_authority': 'cloud', 'legacy_control_url': self.origin},
            {'role': 'notifier', 'node': 'laptop', 'capabilities': ['owner.notify'], 'token_file': str(notify)}]
        self.access = PrivateAccess()
        builder = module(Path(__file__).parents[1] / 'deploy/build-claw-client-overlay.py', 'claw_overlay_builder')
        builder.build(Path(__file__).parent / 'fixtures/claw_remote_original.py', self.root / 'cli-overlay')
        self.legacy = module(self.root / 'cli-overlay/ilink_remote.py', 'claw_original_overlay')
        self.cli_root = self.root / 'cli-state'
        self.cli_root.mkdir(mode=0o700)
        self.token, self.notify_token = token, notify

    def tearDown(self):
        fixtures.CommunicationFaultDomainTests.tearDown(self)

    message = fixtures.CommunicationFaultDomainTests.message
    cutover = staticmethod(fixtures.CommunicationFaultDomainTests.cutover)
    handoff = staticmethod(fixtures.CommunicationFaultDomainTests.handoff)
    activate = fixtures.CommunicationFaultDomainTests.activate
    start_broker = fixtures.CommunicationFaultDomainTests.start_broker
    transport = fixtures.CommunicationFaultDomainTests.transport

    def connect(self):
        self.activate()
        self.start_broker()
        endpoint = 'http://127.0.0.1:' + str(self.http.server_address[1])
        self.config = {'control_url': self.origin, 'token_file': str(self.token),
            'mesh_communication': {'protocol': PROTOCOL, 'control_url': endpoint,
                'token_file': str(self.token), 'communication_id': self.independent.identity['communication_id'],
                'legacy_control_url': self.origin}}
        return self.config

    def args(self, operation='send', request_id='new-message', text='synthetic original text', **values):
        return types.SimpleNamespace(operation=operation, request_id=request_id, text=text,
            text_file=None, image=None, file=None, video=None, limit=20, max_bytes=1024,
            **values)

    def reserve(self, identity, text):
        fingerprint = hashlib.sha256(json.dumps([self.origin, '/v1/notify', {'text': text}],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.access.private_write(self.cli_root / 'remote-sends' / (identity + '.json'),
                                  json.dumps({'fingerprint': fingerprint}))

    def test_exact_old_cli_replay_uses_original_imported_id_unknown_and_never_posts(self):
        self.connect()
        self.reserve('old-unknown', self.old_send['body'])
        before = self.independent.send_status('old-unknown')
        real = self.legacy.request
        paths = []
        def only_lookup(config, access, path, body=None):
            paths.append(path)
            self.assertNotEqual('/v1/notify', path)
            return real(config, access, path, body)
        with mock.patch.object(self.legacy, 'request', side_effect=only_lookup):
            result = self.legacy.run(self.args(request_id='old-unknown', text=self.old_send['body']),
                                     self.access, self.cli_root, self.config)
        self.assertEqual(['/v1/notify/status'], paths)
        self.assertTrue(result['outcome_unknown'])
        self.assertTrue(result['replayed'])
        self.assertEqual(before, self.independent.send_status('old-unknown'))
        with self.independent.transaction() as db:
            self.assertEqual(self.old_send['client_id'], db.execute(
                "SELECT client_id FROM outbox WHERE id='old-unknown'").fetchone()[0])
        self.assertEqual([], self.wire)

    def test_lost_submit_ack_original_reservation_readback_does_not_resubmit(self):
        self.connect()
        real = self.legacy.request
        submitted = []
        def lose_ack(config, access, path, body=None):
            value = real(config, access, path, body)
            if path == '/v1/notify':
                submitted.append(path)
                raise ValueError('simulated-lost-original-ack')
            return value
        with mock.patch.object(self.legacy, 'request', side_effect=lose_ack):
            first = self.legacy.run(self.args(), self.access, self.cli_root, self.config)
        self.assertTrue(first['outcome_unknown'])
        journal = (self.cli_root / 'remote-sends/new-message.json').read_bytes()
        second = self.legacy.run(self.args(), self.access, self.cli_root, self.config)
        self.assertEqual('pending', second['status'])
        self.assertTrue(second['replayed'])
        self.assertEqual(1, len(submitted))
        self.assertEqual(journal, (self.cli_root / 'remote-sends/new-message.json').read_bytes())
        self.assertEqual(self.origin, self.config['control_url'])

    def test_changed_logical_origin_or_payload_rejected_without_new_reservation(self):
        self.connect()
        changed = copy.deepcopy(self.config)
        changed['control_url'] = 'http://127.0.0.1:18888'
        with self.assertRaisesRegex(ValueError, 'BOT_REMOTE_ORIGINAL_ORIGIN_CHANGED'):
            self.legacy.run(self.args(), self.access, self.cli_root, changed)
        self.assertFalse((self.cli_root / 'remote-sends').exists())
        self.reserve('old-unknown', self.old_send['body'])
        with self.assertRaisesRegex(ValueError, 'BOT_REQUEST_ID_CONFLICT'):
            self.legacy.run(self.args(request_id='old-unknown', text='changed body'),
                            self.access, self.cli_root, self.config)

    def test_old_owner_status_and_archive_wire_work_without_original_execution_database(self):
        self.connect()
        Path(self.original.path).rename(self.root / 'original-unavailable.sqlite')
        status = self.legacy.run(self.args(operation='status'), self.access, self.cli_root, self.config)
        self.assertEqual('BOT_REMOTE_STATUS', status['code'])
        self.assertIsNone(status['leader'])
        updates = self.legacy.run(self.args(operation='updates'), self.access, self.cli_root, self.config)
        self.assertEqual('old-original', updates['items'][0]['message_id'])
        self.assertEqual(1, updates['next_cursor'])
        self.assertEqual([], self.wire)

    def test_owner_binding_checked_before_write_and_owner_token_cannot_operate_tasks(self):
        self.connect()
        before = self.independent.status()['outbox']
        bad = copy.deepcopy(self.config)
        bad['mesh_communication']['communication_id'] = '0' * 64
        with self.assertRaises(ValueError):
            owner_request(bad, self.access, '/v1/notify', {'request_id': 'forbidden', 'text': 'synthetic'})
        self.assertEqual(before, self.independent.status()['outbox'])
        headers = {'Authorization': 'Bearer ' + self.token.read_text(),
            ORIGIN_HEADER: self.origin, IDENTITY_HEADER: self.independent.identity['communication_id']}
        request = urllib.request.Request(self.config['mesh_communication']['control_url'] + '/v1/tasks',
            data=b'{}', headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request)
        self.assertEqual(403, error.exception.code)
        error.exception.close()

    def test_new_media_rejected_before_upload_reservation_or_request(self):
        self.connect()
        args = self.args()
        args.file = self.root / 'nonexistent-do-not-read.txt'
        with mock.patch.object(self.legacy, 'request', side_effect=AssertionError('no request')):
            value = self.legacy.run(args, self.access, self.cli_root, self.config)
        self.assertEqual('BOT_REMOTE_MEDIA_UNSUPPORTED', value['code'])
        self.assertFalse(value['outcome_unknown'])
        self.assertFalse((self.cli_root / 'remote-sends').exists())
        self.assertFalse((self.cli_root / 'remote-uploads').exists())

    def test_existing_reserved_media_only_reads_original_media_outbox(self):
        self.connect()
        media = self.root / 'original.txt'
        media.write_bytes(b'synthetic-original')
        item = {'type': 4, 'file_item': {'file_name': 'original.txt', 'synthetic': 'private-reference'}}
        identity = 'old-media'
        self.access.private_write(self.cli_root / 'remote-uploads' / (identity + '.json'), json.dumps({
            'fingerprint': hashlib.sha256(json.dumps(['file', media.name,
                hashlib.sha256(media.read_bytes()).hexdigest()]).encode()).hexdigest(),
            'item': item, 'attachment_id': 'synthetic-attachment'}))
        self.access.private_write(self.cli_root / 'remote-sends' / (identity + '.json'), json.dumps({
            'fingerprint': hashlib.sha256(json.dumps([self.origin, '/v1/notify/media', {'items': [item]}],
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()}))
        serialized = json.dumps([item], sort_keys=True, separators=(',', ':'))
        with self.independent.transaction() as db:
            db.execute('INSERT INTO outbox(id,fingerprint,body,status,client_id,media_items,created) '
                "VALUES(?,?,?,'unknown','original-media-client',?,1)",
                (identity, hashlib.sha256(serialized.encode()).hexdigest(), serialized, serialized))
        args = self.args(request_id=identity)
        args.file = media
        value = self.legacy.run(args, self.access, self.cli_root, self.config)
        self.assertEqual('unknown', value['status'])
        self.assertTrue(value['replayed'])
        self.assertEqual('synthetic-attachment', value['attachment_id'])
        self.assertEqual([], self.wire)

    def test_non_contiguous_original_archive_ordinals_survive_relocation_and_new_append(self):
        with self.original.transaction() as db:
            db.execute('UPDATE inbox SET rowid=42')
        later = self.message('later-original')
        self.original.ingest([later], 'next', 'fixture-owner', 'fixture-bot')
        expected = self.original.inbox(42)
        self.transfer = export_transfer(self.original.path, 'cloud', 'owner-ref', self.binding, 'ordinals')
        self.independent = install_prepared(self.transfer, self.root / 'ordered.sqlite', self.transfer['sha256'])
        self.assertEqual(expected, self.independent.inbox(42))
        self.activate()
        new = self.message('new-append')
        with self.independent.transaction() as db:
            self.independent._ingest_in_db(db, [new], 'fresh', 'fixture-owner', 'fixture-bot')
        self.assertEqual(44, self.independent.inbox(43)['next_cursor'])
        moved = export_transfer(self.independent.path, 'cloud', 'owner-ref', self.binding, 'ordered-again')
        reopened = install_prepared(moved, self.root / 'ordered-again.sqlite', moved['sha256'])
        self.assertEqual(self.independent.inbox(42), reopened.inbox(42))
        with reopened.transaction() as db:
            db.execute('UPDATE communication_inbox_order SET sequence=sequence+100')
        with self.assertRaisesRegex(ValueError, 'communication_candidate_archive_order_changed'):
            verify_prepared(reopened, moved, moved['sha256'])

    def local_inbox(self):
        store = relay_store_class(self.TaskStore)(self.root / 'local.sqlite', recover_inflight=False,
            notification_policy={'mode': 'private', 'owner_relay': {'peer': 'cloud', 'authority': 'cloud'}},
            node_id='laptop')
        inbox = CommunicationInbox(store, 'laptop', self.independent.identity['communication_id'],
            {'required': ['agent', 'mesh.node:laptop'], 'project_id': 'personal-project', 'agent_id': 'local-assistant'})
        return store, inbox

    def input(self, message):
        with self.independent.transaction() as db:
            self.independent._ingest_in_db(db, [message], 'fresh-cursor', 'fixture-owner', 'fixture-bot')
        return fixtures.message_identity(message)

    def test_real_dispatcher_atomic_local_task_and_same_receipt_ack_loss_recovery(self):
        self.connect()
        store, inbox = self.local_inbox()
        identity = self.input(self.message('one-local', '/plan synthetic local work'))
        real = self.clients['laptop'].request
        def lose_receipt_ack(path, body):
            value = real(path, body)
            if path == '/communication/receipt':
                raise ValueError('simulated-lost-receipt-ack')
            return value
        with mock.patch.object(self.clients['laptop'], 'request', side_effect=lose_receipt_ack):
            first = Dispatcher(inbox, self.clients['laptop'], True).step()
        self.assertEqual('unknown', first['deliveries'][0]['state'])
        with mock.patch.object(inbox, 'deliver', side_effect=AssertionError('never re-ingest')):
            Dispatcher(inbox, self.clients['laptop'], True).step()
        with store.transaction() as db:
            task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (identity,)).fetchone())
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
        self.assertEqual(['agent', 'mesh.node:laptop'], json.loads(task['required']))
        self.assertEqual('plan', json.loads(task['context'])['mode'])
        self.assertEqual('confirmed', self.independent.event(identity)['state'])
        second = self.input(self.message('same-local-project'))
        Dispatcher(inbox, self.clients['laptop'], True).step()
        with store.transaction() as db:
            self.assertEqual(task['scope'], db.execute('SELECT scope FROM tasks WHERE id=?', (second,)).fetchone()[0])

    def test_unknown_prepare_and_original_controls_never_ingest_again_or_switch_target(self):
        self.connect()
        store, inbox = self.local_inbox()
        identity = self.input(self.message('lost-before-ingest'))
        self.independent.prepare_delivery(identity, 'laptop')
        self.independent.mark_delivery_unknown(identity, 'laptop')
        control = self.input(self.message('old-control', '/pause ' + fixtures.message_identity(self.old)))
        new = self.input(self.message('unrelated-new'))
        dispatcher = Dispatcher(inbox, self.clients['laptop'], True)
        dispatcher.step()
        with store.transaction() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM tasks WHERE id=?', (identity,)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM inbox WHERE id=?', (control,)).fetchone())
            self.assertIsNotNone(db.execute('SELECT 1 FROM tasks WHERE id=?', (new,)).fetchone())
        self.assertEqual('cloud', self.independent.pending()[0]['required_authority'])
        self.assertEqual('unknown', self.independent.event(identity)['state'])

    def test_separate_notification_client_uses_same_logical_route_and_ignores_old_private_rows(self):
        self.connect()
        store, inbox = self.local_inbox()
        new = self.input(self.message('local-notice'))
        Dispatcher(inbox, self.clients['laptop'], True).step()
        with store.transaction() as db:
            columns = tuple(self.old_send)
            db.execute('INSERT INTO outbox(' + ','.join(columns) + ') VALUES(' +
                ','.join('?' for _ in columns) + ')', tuple(self.old_send[key] for key in columns))
        client = fixtures.Client(self.config['mesh_communication']['control_url'], self.notify_token.read_text())
        relay = NotificationRelay(store, 'laptop', 'cloud', 'cloud', client)
        report = relay.poll_once()
        self.assertEqual('queued', report['state'])
        with store.transaction() as db:
            receipt = json.loads(db.execute('SELECT receipt FROM notification_relays').fetchone()[0])
            self.assertEqual(self.notifications.route_id, receipt['route_id'])
            self.assertEqual('cloud', receipt['authority'])
            self.assertEqual(self.old_send, dict(db.execute("SELECT * FROM outbox WHERE id='old-unknown'").fetchone()))
        self.assertEqual([], self.wire)

    def test_source_producer_rejects_media_before_new_journal_write(self):
        store, inbox = self.local_inbox()
        with self.assertRaisesRegex(ValueError, 'communication_media_relay_unsupported_before_enqueue'):
            store.enqueue_media('new-media', [{'type': 4}])
        with store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])

    def test_node_quiet_policy_keeps_all_other_api_and_a2a_configuration_exact(self):
        quiet = {'node_id': 'laptop', 'database': '/PRIVATE/old.sqlite', 'port': 17681,
            'peers': [{'node': 'cloud', 'role': 'agent_peer', 'token_file': '/PRIVATE/original.token'}],
            'notification_policy': {'mode': 'private'}}
        selected = copy.deepcopy(quiet)
        selected['notification_policy']['owner_relay'] = {'peer': 'cloud', 'authority': 'cloud'}
        validate_configs({}, quiet, selected)
        changed = copy.deepcopy(selected)
        changed['peers'][0]['node'] = 'communication-broker'
        with self.assertRaisesRegex(ValueError, 'communication_node_non_notification_config_changed'):
            validate_configs({}, quiet, changed)
        with self.assertRaisesRegex(ValueError, 'communication_node_separate_relay_policy_required'):
            validate_configs({}, selected, selected)

    def test_many_old_unknowns_do_not_hide_a_new_dispatchable_message(self):
        self.connect()
        store, inbox = self.local_inbox()
        for number in range(25):
            identity = self.input(self.message('unknown-%d' % number))
            self.independent.prepare_delivery(identity, 'cloud')
            self.independent.mark_delivery_unknown(identity, 'cloud')
        fresh = self.input(self.message('new-with-old-unknowns'))
        Dispatcher(inbox, self.clients['laptop'], True, limit=20).step()
        with store.transaction() as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM tasks WHERE id=?', (fresh,)).fetchone())
        with self.independent.transaction() as db:
            self.assertEqual(25, db.execute("SELECT COUNT(*) FROM communication_events WHERE state='unknown'").fetchone()[0])

    def test_exact_frozen_node_api_starts_original_worker_without_old_relay_or_recovery(self):
        # Actual fresh candidate namespace, original Node/Worker/HTTP. No model
        # task is queued, so original idle Worker cannot start native inference.
        base = self.root / 'exact-base' / 'assistant_mesh'
        base.mkdir(parents=True)
        originals = _source_base()
        for name, data in originals.items():
            (base / name).write_bytes(data)
        builder = module(Path(__file__).parents[1] / 'deploy/build-communication-candidate.py',
                         'communication_source_builder')
        candidate = self.root / 'exact-candidate'
        report = builder.build(base.parent, candidate)
        self.assertEqual(38, report['unchanged_original_modules'])
        self.assertEqual(originals['node.py'], (candidate / 'assistant_mesh/node.py').read_bytes())
        self.assertEqual(originals['worker.py'], (candidate / 'assistant_mesh/worker.py').read_bytes())
        alias = '_mesh_client_continuity_exact_node'
        package = types.ModuleType(alias)
        package.__path__, package.__package__ = [str(candidate / 'assistant_mesh')], alias
        sys.modules[alias] = package
        runtime = None
        try:
            entry = importlib.import_module(alias + '.communication_node')
            node_module = importlib.import_module(alias + '.node')
            worker_module = importlib.import_module(alias + '.worker')
            store_module = importlib.import_module(alias + '.store')
            database = self.root / 'exact-node.sqlite'
            store = store_module.Store(database, recover_inflight=False)
            with store.transaction() as db:
                db.execute("INSERT INTO steering VALUES('old-steer','old-task',1,'synthetic','submitting')")
            store.enqueue('old-submitting', 'synthetic historical notification')
            with store.transaction() as db:
                db.execute("UPDATE outbox SET status='submitting' WHERE id='old-submitting'")
            token = str(self.token)
            quiet = {'node_id': 'laptop', 'database': str(database), 'port': 0,
                'notification_policy': {'mode': 'private'}, 'peers': [
                    {'role': 'operator', 'token_file': token},
                    {'role': 'worker', 'node': 'laptop', 'token_file': str(self.notify_token),
                     'capabilities': ['agent', 'mesh.node:laptop', 'owner.notify']},
                    {'role': 'agent_peer', 'node': 'cloud', 'token_file': str(self.root / 'a2a.token'),
                     'capabilities': ['a2a.delegate', 'a2a.report']}],
                'network_peers': {'cloud': {'send_allowed': True}}}
            (self.root / 'a2a.token').write_text('synthetic-a2a-' + 'b' * 64)
            (self.root / 'a2a.token').chmod(0o600)
            producer = copy.deepcopy(quiet)
            producer['notification_policy']['owner_relay'] = {'peer': 'cloud', 'authority': 'cloud'}
            configs = {
                'quiet.json': quiet, 'producer.json': producer,
                'worker.json': {'node_id': 'laptop', 'control_url': 'http://127.0.0.1:0',
                    'token_file': str(self.notify_token), 'capabilities': ['agent', 'mesh.node:laptop', 'owner.notify'],
                    'codex': {'workspace': str(self.root), 'sandbox': 'read-only'}},
                'a2a-client.json': {'control_url': 'http://127.0.0.1:19998',
                                    'token_file': str(self.root / 'a2a.token')}}
            for name, value in configs.items():
                self.access.private_write(self.root / name, json.dumps(value))
            node_config = {'node_id': 'laptop', 'local_server_config': str(self.root / 'quiet.json'),
                'local_worker_config': str(self.root / 'worker.json'), 'start_worker': True,
                'auto_maintenance': False, 'peers': [{'node': 'cloud', 'authority': 'cloud',
                    'client_config': str(self.root / 'a2a-client.json')}]}
            self.access.private_write(self.root / 'node.json', json.dumps(node_config))
            def bounded(original):
                nonlocal runtime
                runtime = original
                original.start()
                try:
                    self.assertIsInstance(original, node_module.Node)
                    self.assertIsInstance(original.worker, worker_module.Worker)
                    self.assertTrue(original.worker_thread.is_alive())
                    self.assertIsNone(original.notification_relay)
                    self.assertIsNone(original.notification_thread)
                    self.assertEqual(configs['a2a-client.json'], original.peers['cloud']['client'])
                    url = 'http://127.0.0.1:' + str(original.server.server_address[1])
                    owner = worker_module.Client({'control_url': url, 'token_file': token})
                    owner.request('/v1/notify', {'request_id': 'new-api-notice', 'text': 'synthetic new notice'})
                    a2a = worker_module.Client({'control_url': url, 'token_file': str(self.root / 'a2a.token')})
                    self.assertEqual('laptop', a2a.request('/v1/mesh/hello')['authority'])
                    with original.store.transaction() as db:
                        self.assertEqual('submitting', db.execute('SELECT state FROM steering').fetchone()[0])
                        self.assertEqual('submitting', db.execute(
                            "SELECT status FROM outbox WHERE id='old-submitting'").fetchone()[0])
                        self.assertEqual('relay', db.execute(
                            "SELECT delivery_route FROM outbox WHERE id='new-api-notice'").fetchone()[0])
                        self.assertEqual(0, db.execute('SELECT post_attempted FROM notification_relays').fetchone()[0])
                finally:
                    original.stop()
            with mock.patch.object(node_module.Node, 'run_forever', bounded):
                entry.serve({'protocol': entry.PROTOCOL, 'node_config': str(self.root / 'node.json'),
                             'task_server_config': str(self.root / 'producer.json')})
            self.assertIs(node_module.Store, store_module.Store)
            # Actual independently configured companion entry, after this
            # fixture's Worker is stopped. Task admission/relay, NOT inference.
            self.connect()
            identity = self.input(self.message('companion-input'))
            url = self.config['mesh_communication']['control_url']
            executor_token = self.root / 'executor.token'
            executor_token.write_text(self.tokens['laptop'])
            executor_token.chmod(0o600)
            self.access.private_write(self.root / 'broker-client.json', json.dumps({
                'control_url': url, 'token_file': str(executor_token)}))
            self.access.private_write(self.root / 'notify-client.json', json.dumps({
                'control_url': url, 'token_file': str(self.notify_token)}))
            companion = importlib.import_module(alias + '.communication_runtime')
            stopped, complete = threading.Event(), threading.Event()
            observations, errors = [], []
            def observed(report):
                observations.append(report)
                if any(row.get('state') == 'confirmed' for row in report.get('deliveries', [])):
                    complete.set()
            def run_companion():
                try:
                    companion.serve({'protocol': companion.PROTOCOL,
                        'task_server_config': str(self.root / 'producer.json'),
                        'broker_client_config': str(self.root / 'broker-client.json'),
                        'notification_client_config': str(self.root / 'notify-client.json'),
                        'communication_id': self.independent.identity['communication_id'],
                        'accept_ordinary': True, 'poll_interval_seconds': 0.1,
                        'new_task_route': {'required': ['agent', 'mesh.node:laptop'],
                            'project_id': 'local-project', 'agent_id': 'local-assistant'}},
                        stop=stopped, on_report=observed)
                except Exception as error:
                    errors.append(error)
                    complete.set()
            thread = threading.Thread(target=run_companion, daemon=True)
            thread.start()
            try:
                self.assertTrue(complete.wait(4))
            finally:
                stopped.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual('confirmed', self.independent.event(identity)['state'])
            with store.transaction() as db:
                task = db.execute('SELECT required FROM tasks WHERE id=?', (identity,)).fetchone()
                self.assertEqual(['agent', 'mesh.node:laptop'], json.loads(task[0]))
                self.assertEqual('submitting', db.execute('SELECT state FROM steering').fetchone()[0])
            self.assertEqual([], self.wire)
        finally:
            if runtime is not None and not runtime.stop_event.is_set():
                runtime.stop()
            for name in tuple(sys.modules):
                if name == alias or name.startswith(alias + '.'):
                    sys.modules.pop(name, None)


if __name__ == '__main__':
    unittest.main()
