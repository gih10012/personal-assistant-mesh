"""Phone and Claw use the same existing entrance ledger across disconnects."""
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.entrance import entrance_store_class, validate_policy
from assistant_mesh.phone_chat import PhoneChat
from assistant_mesh.store import Conflict
from tests.test_entrance import POLICY
from tests.test_legacy_store_carrier_backport import _load_store, _original_source, _patched_source


class PhoneConversationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store_type = _load_store(_patched_source(_original_source()))
        self.store = entrance_store_class(store_type, POLICY)(Path(directory.name) / 'chat.sqlite')
        self.scope = validate_policy(POLICY)
        self.store.heartbeat('entry', [self.scope['capability']])
        self.chat = PhoneChat(self.store)
        self.node = {'node_id': 'phone', 'session_id': 'original-session'}

    def call(self, action, **arguments):
        return self.chat.request(self.node, 'mesh_chat_' + action,
                                 dict(arguments, session_id=self.node['session_id']))

    def test_original_scope_reply_retry_and_import(self):
        self.assertEqual('shared', self.call('status')['mode'])
        message = dict(event_id='original', created_at_ms=1700000000000, text='remember this')
        first = self.call('send', **message)
        self.assertEqual(first, self.call('send', **message))
        with self.assertRaises(Conflict):
            self.call('send', **dict(message, text='changed'))
        task = self.store.claim('entry')
        self.assertEqual(self.scope['scope'], task['scope'])
        self.store.agent_action(task['id'], 'entry', task['epoch'], 'reply-call', 'entrance_reply',
                                {'text':'remembered', 'observed_latest_message_id':'original'})
        events = self.call('events', after=0)['events']
        self.assertEqual(['original'], [e['event_id'] for e in events if e['kind']=='message'])
        self.assertEqual(['remembered'], [e['text'] for e in events if e['kind']=='reply'])
        imported = dict(event_id='historical', created_at_ms=1699999999999, role='user', text='old note')
        for _ in range(2):
            self.call('merge', branch_id='offline-branch', events=[imported])
        with self.store.transaction() as db:
            self.assertEqual('original', self.store._entrance_current(db)['latest_inbound_event_id'])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        history = [e for e in self.call('events', after=0)['events'] if e['kind'] == 'history']
        self.assertEqual([('historical', 'user', 1699999999999)],
                         [(e['event_id'], e['role'], e['created_at_ms']) for e in history])
