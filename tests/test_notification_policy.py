"""Notification routing is additive; legacy/private work is never opted in."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from assistant_mesh.store import Conflict, Store


class NotificationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'ledger.sqlite'
        self.now = 1000.0
        self.policy = {'mode': 'private', 'owner_relay': {'peer': 'cloud', 'authority': 'cloud'}}

    def tearDown(self):
        self.temporary.cleanup()

    def store(self, policy=None):
        return Store(self.path, clock=lambda: self.now, notification_policy=policy, node_id='laptop')

    @staticmethod
    def running(store):
        store.heartbeat('laptop', ['agent'])
        identity = store.create_task('local work', ['agent'])
        return identity, store.claim('laptop')

    def test_legacy_eight_pending_records_are_not_retroactively_enrolled(self):
        old = self.store()
        for index in range(8):
            old.enqueue('historical-' + str(index), 'old local result ' + str(index))
        with old.transaction() as db:
            before = [dict(row) for row in db.execute('SELECT * FROM outbox ORDER BY id')]
        current = self.store(self.policy)
        with current.transaction() as db:
            after = [dict(row) for row in db.execute('SELECT * FROM outbox ORDER BY id')]
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])
        self.assertEqual(before, after)
        current.enqueue('historical-0', 'old local result 0')
        with current.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])
        self.assertEqual('channel', current.send_status('historical-0')['delivery_route'])

    def test_private_default_without_relay_is_truthfully_recorded_only(self):
        store = self.store({'mode': 'private'})
        identity, task = self.running(store)
        answer = store.agent_action(identity, 'laptop', task['epoch'], 'notify-1', 'notify', {'text': 'need owner'})
        self.assertEqual('recorded_private', answer['status'])
        self.assertFalse(answer['delivery_verified'])
        self.assertEqual('recorded_private', store.send_status(answer['id'])['status'])
        self.assertIsNone(store.next_send())
        with store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])

    def test_automatic_completion_and_review_remain_private(self):
        store = self.store(self.policy)
        identity, task = self.running(store)
        store.update_task(identity, 'laptop', task['epoch'], result='native work result', status='completed')
        review_id, review = self.running(store)
        store.update_task(review_id, 'laptop', review['epoch'], {'side_effect_started': True})
        self.now += 91
        store.heartbeat('laptop', ['agent'])
        self.assertIsNone(store.claim('laptop'))
        self.assertEqual('needs_review', store.task_status(review_id)['status'])
        with store.transaction() as db:
            rows = list(db.execute('SELECT delivery_route FROM outbox'))
            self.assertEqual(2, len(rows))
            self.assertTrue(all(row['delivery_route'] == 'private' for row in rows))
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])
        self.assertIsNone(store.next_send())

    def test_explicit_notify_and_relay_intent_share_one_atomic_action(self):
        store = self.store(self.policy)
        identity, task = self.running(store)
        args = {'text': 'explicit owner notification'}
        answer = store.agent_action(identity, 'laptop', task['epoch'], 'notify-once', 'notify', args)
        repeated = store.agent_action(identity, 'laptop', task['epoch'], 'notify-once', 'notify', args)
        self.assertEqual(answer, repeated)
        self.assertEqual('relay', answer['delivery_route'])
        with store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])
            relay = dict(db.execute('SELECT * FROM notification_relays').fetchone())
            self.assertEqual(('laptop', 'cloud', 'cloud'), (relay['source_node'], relay['peer'], relay['authority']))
            self.assertEqual(hashlib.sha256(args['text'].encode()).hexdigest(), relay['fingerprint'])
            self.assertEqual(answer['id'], relay['request_id'])
            self.assertEqual(0, relay['post_attempted'])
        self.assertIsNone(store.next_send())
        with self.assertRaises(Conflict):
            store.agent_action(identity, 'laptop', task['epoch'], 'notify-once', 'notify', {'text': 'different'})
        with store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])

    def test_stale_task_cannot_create_notification(self):
        store = self.store(self.policy)
        identity, task = self.running(store)
        self.now += 91
        with self.assertRaises(Conflict):
            store.agent_action(identity, 'laptop', task['epoch'], 'expired', 'notify', {'text': 'no send'})
        with store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_route_change_does_not_retarget_existing_intent(self):
        store = self.store(self.policy)
        store.enqueue('owner-intent', 'original')
        changed = self.store({'mode': 'private', 'owner_relay': {'peer': 'another', 'authority': 'other-authority'}})
        changed.enqueue('owner-intent', 'original')
        with changed.transaction() as db:
            row = dict(db.execute('SELECT * FROM notification_relays').fetchone())
        self.assertEqual(('cloud', 'cloud'), (row['peer'], row['authority']))
        self.assertEqual(0, row['attempt'])

    def test_existing_private_record_is_never_promoted_by_same_id(self):
        self.store({'mode': 'private'}).enqueue('private-record', 'local')
        store = self.store(self.policy)
        store.enqueue('private-record', 'local')
        self.assertEqual('recorded_private', store.send_status('private-record')['status'])
        with store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])

    def test_media_is_not_implicitly_routed_through_text_relay(self):
        store = self.store(self.policy)
        store.enqueue_media('local-media', [{'type': 2, 'image_item': {'media': 'test-only'}}])
        self.assertEqual('recorded_private', store.send_status('local-media')['status'])
        self.assertIsNone(store.next_send())
        with store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])

    def test_invalid_policy_is_rejected_without_broadening_native_authority(self):
        for policy in ({}, {'mode': 'other'}, {'mode': 'private', 'extra': True},
                       {'mode': 'channel', 'owner_relay': {'peer': 'cloud', 'authority': 'cloud'}},
                       {'mode': 'private', 'owner_relay': {'peer': 'laptop', 'authority': 'cloud'}},
                       {'mode': 'private', 'owner_relay': {'peer': 'cloud', 'authority': 'cloud', 'recipient': 'any'}}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.store(policy)

    def test_relay_request_id_validation_is_atomic(self):
        store = self.store(self.policy)
        for identifier in ('space id', '中文', 'x' * 201):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                store.enqueue(identifier, 'text')
        with store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM notification_relays').fetchone()[0])

    def test_legacy_relay_attempts_migrate_conservatively_without_replaying(self):
        with closing(sqlite3.connect(str(self.path))) as db, db:
            db.execute('''CREATE TABLE notification_relays(
                request_id TEXT PRIMARY KEY, source_node TEXT NOT NULL,
                peer TEXT NOT NULL, authority TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempt INTEGER NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, receipt TEXT, error TEXT,
                created REAL NOT NULL, updated REAL NOT NULL)''')
            for identity, attempt in (('never-attempted', 0), ('ambiguous', 1)):
                db.execute('''INSERT INTO notification_relays
                    (request_id,source_node,peer,authority,fingerprint,attempt,created,updated)
                    VALUES(?,?,?,?,?,?,?,?)''', (identity, 'laptop', 'cloud', 'cloud', '1' * 64, attempt, self.now, self.now))
        store = self.store(self.policy)
        with store.transaction() as db:
            flags = dict(db.execute('SELECT request_id,post_attempted FROM notification_relays'))
        self.assertEqual({'never-attempted': 0, 'ambiguous': 1}, flags)
        with store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
