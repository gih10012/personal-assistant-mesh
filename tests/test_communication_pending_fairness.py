"""Actual broker SQLite + authenticated loopback; no iLink, model or production."""
import json
import unittest
import urllib.error
from unittest import mock

from assistant_mesh.communication_inbox import CommunicationInbox
from assistant_mesh.communication_producer import relay_store_class
from assistant_mesh.communication_runtime import Dispatcher
from assistant_mesh.communication_store import message_identity
from tests import test_communication_faultdomain as fixtures


class CommunicationPendingFairnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.CommunicationFaultDomainTests.setUpClass.__func__(cls)

    def setUp(self):
        fixtures.CommunicationFaultDomainTests.setUp(self)
        self.cloud_token = self.root / 'cloud-executor.token'
        self.cloud_token.write_text('cloud-executor-' + 'z' * 64)
        self.cloud_token.chmod(0o600)
        self.additional_peers = [{'role': 'executor', 'execution_authority': 'cloud',
                                  'token_file': str(self.cloud_token)}]
        self.activate()
        self.start_broker()
        self.clients['cloud'] = fixtures.Client(self.clients['laptop'].url, self.cloud_token.read_text())

    def tearDown(self):
        fixtures.CommunicationFaultDomainTests.tearDown(self)

    message = fixtures.CommunicationFaultDomainTests.message
    cutover = staticmethod(fixtures.CommunicationFaultDomainTests.cutover)
    handoff = staticmethod(fixtures.CommunicationFaultDomainTests.handoff)
    activate = fixtures.CommunicationFaultDomainTests.activate
    start_broker = fixtures.CommunicationFaultDomainTests.start_broker
    transport = fixtures.CommunicationFaultDomainTests.transport

    def input(self, identity, text='synthetic local work', media=False):
        message = self.message(identity, text)
        if media:
            message['item_list'] = [{'type': 4, 'file_item': {'file_name': 'synthetic.txt',
                                                           'content_downloaded': False}}]
        with self.independent.transaction() as db:
            self.independent._ingest_in_db(db, [message], 'new-cursor', 'fixture-owner', 'fixture-bot')
        return message_identity(message)

    def inbox(self, authority='laptop'):
        store = relay_store_class(self.TaskStore)(self.root / (authority + '-tasks.sqlite'),
            recover_inflight=False, notification_policy={'mode': 'private'}, node_id=authority)
        inbox = CommunicationInbox(store, authority, self.independent.identity['communication_id'])
        return store, inbox

    def events(self):
        with self.independent.transaction() as db:
            return {r['message_id']: dict(r) for r in db.execute('SELECT * FROM communication_events')}

    def test_exact_25_old_unattempted_cloud_controls_do_not_hide_one_new_local_message(self):
        original_target = message_identity(self.old)
        controls = [self.input('old-control-%02d' % n, '/pause ' + original_target) for n in range(25)]
        fresh = self.input('one-fresh-ordinary')
        before = self.events()
        self.assertEqual(set(controls), {k for k, row in before.items() if row['required_authority'] == 'cloud'})
        # The legacy global diagnostic still proves the same bounded head that
        # caused the bug. The broker must filter its own authenticated view.
        self.assertNotIn(fresh, {row['message_id'] for row in self.independent.pending(20)})
        store, inbox = self.inbox()
        report = Dispatcher(inbox, self.clients['laptop'], True, limit=20).step()
        self.assertEqual([fresh], [r['message_id'] for r in report['deliveries']])
        self.assertEqual('confirmed', report['deliveries'][0]['state'])
        with store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(fresh, db.execute('SELECT id FROM tasks').fetchone()[0])
        after = self.events()
        self.assertEqual([before[k] for k in controls], [after[k] for k in controls])
        self.assertEqual([], self.wire)

    def test_policy_filter_precedes_limit_and_controls_keep_their_original_authority(self):
        ordinary = [self.input('ordinary-off-%02d' % n) for n in range(25)]
        self.independent.register_reference('laptop', 'existing-local-task', 'e' * 64)
        control = self.input('local-control-after-ordinary', '/pause existing-local-task')
        before = self.events()
        store, inbox = self.inbox()
        report = Dispatcher(inbox, self.clients['laptop'], False, limit=1).step()
        self.assertEqual([control], [r['message_id'] for r in report['deliveries']])
        self.assertEqual('confirmed', report['deliveries'][0]['state'])
        after = self.events()
        self.assertEqual([before[k] for k in ordinary], [after[k] for k in ordinary])
        with store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertIsNotNone(db.execute('SELECT 1 FROM inbox WHERE id=?', (control,)).fetchone())
        self.assertEqual([], self.wire)

    def test_authenticated_endpoint_cannot_post_another_authority_or_choose_global_candidates(self):
        old = self.input('cloud-only-control', '/pause ' + message_identity(self.old))
        fresh = self.input('legacy-ordinary')
        laptop = self.clients['laptop']
        # The old request body remains accepted but is not a global queue view.
        legacy = laptop.request('/communication/pending', {'limit': 20})
        self.assertEqual([fresh], [r['message_id'] for r in legacy['events']])
        cloud = self.clients['cloud'].request('/communication/pending',
            {'limit': 20, 'accept_ordinary': False})
        self.assertEqual([old], [r['message_id'] for r in cloud['events']])
        for body in ({'limit': 20, 'authority': 'cloud'},
                     {'limit': 20, 'accept_ordinary': True, 'authority': 'cloud'}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                laptop.request('/communication/pending', body)
            self.assertEqual(403, error.exception.code)
            error.exception.close()
        for name in ('notifier', 'viewer', 'carrier'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.clients[name].request('/communication/pending', {'limit': 20, 'accept_ordinary': True})
            self.assertEqual(403, error.exception.code)
            error.exception.close()
        anonymous = fixtures.Client(laptop.url, 'not-a-credential')
        with self.assertRaises(urllib.error.HTTPError) as error:
            anonymous.request('/communication/pending', {'limit': 20, 'accept_ordinary': True})
        self.assertEqual(401, error.exception.code)
        error.exception.close()
        self.assertEqual([], self.wire)

    def test_original_target_unknown_is_receipt_only_even_when_ordinary_policy_is_off(self):
        foreign = self.input('original-cloud-unknown')
        self.independent.prepare_delivery(foreign, 'cloud')
        self.independent.mark_delivery_unknown(foreign, 'cloud')
        own = self.input('original-local-unknown')
        self.independent.prepare_delivery(own, 'laptop')
        self.independent.mark_delivery_unknown(own, 'laptop')
        before = self.events()
        store, inbox = self.inbox()
        real = self.clients['laptop'].request
        paths = []
        def no_prepare_or_event(path, body):
            paths.append(path)
            self.assertNotIn(path, ('/communication/prepare', '/communication/event', '/communication/receipt'))
            return real(path, body)
        with mock.patch.object(self.clients['laptop'], 'request', side_effect=no_prepare_or_event), \
                mock.patch.object(inbox, 'deliver', side_effect=AssertionError('unknown must not deliver')):
            report = Dispatcher(inbox, self.clients['laptop'], False, limit=1).step()
        self.assertEqual(['/communication/pending'], paths)
        self.assertEqual([own], [r['message_id'] for r in report['deliveries']])
        self.assertEqual('unknown', report['deliveries'][0]['state'])
        self.assertEqual(before, self.events())
        with store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0])
        self.assertEqual([], self.wire)

    def test_attempted_own_receipt_can_reconcile_under_disabled_policy_without_redelivery(self):
        identity = self.input('local-accepted-before-ack-loss')
        store, inbox = self.inbox()
        self.independent.prepare_delivery(identity, 'laptop')
        original = inbox.deliver(self.independent.event(identity, 'laptop'))
        self.independent.mark_delivery_unknown(identity, 'laptop')
        before = None
        with store.transaction() as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM tasks')]
        with mock.patch.object(inbox, 'deliver', side_effect=AssertionError('no redelivery')):
            report = Dispatcher(inbox, self.clients['laptop'], False, limit=1).step()
        self.assertEqual('confirmed', report['deliveries'][0]['state'])
        self.assertEqual(original, json.loads(self.independent.event(identity)['receipt']))
        with store.transaction() as db:
            self.assertEqual(before, [tuple(r) for r in db.execute('SELECT * FROM tasks')])
        self.assertEqual([], self.wire)

    def test_media_policy_and_limit_remain_bounded_without_download_or_created_task(self):
        media = [self.input('media-%02d' % n, media=True) for n in range(25)]
        self.independent.register_reference('laptop', 'local-existing', 'f' * 64)
        control = self.input('control-after-media', '/resume local-existing')
        before = self.events()
        store, inbox = self.inbox()
        disabled = Dispatcher(inbox, self.clients['laptop'], False, limit=1).step()
        self.assertEqual([control], [r['message_id'] for r in disabled['deliveries']])
        after_control = self.events()
        self.assertEqual([before[k] for k in media], [after_control[k] for k in media])
        # Preserve prior accepted-ordinary behavior: old Store saves a media
        # reference/unsupported notice, not attachment content or a model task.
        enabled = Dispatcher(inbox, self.clients['laptop'], True, limit=1).step()
        self.assertEqual(1, len(enabled['deliveries']))
        self.assertIn(enabled['deliveries'][0]['message_id'], media)
        with store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
        self.assertEqual(24, sum(row['state'] == 'queued' for row in self.events().values()))
        for invalid in (0, 101, True):
            with self.assertRaises(ValueError):
                self.independent.pending(invalid, 'laptop', True)
        for invalid in (0, 1, 'yes', None):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.clients['laptop'].request('/communication/pending',
                    {'limit': 20, 'accept_ordinary': invalid})
            self.assertEqual(400, error.exception.code)
            error.exception.close()
        self.assertEqual([], self.wire)
