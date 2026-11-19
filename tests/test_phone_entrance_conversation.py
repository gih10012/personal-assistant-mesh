"""Phone and Claw use the same existing entrance ledger across disconnects."""
import json
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

    def test_shared_turn_receives_branch_history_without_reissuing_work(self):
        original = dict(event_id='offline-note', created_at_ms=1700000000000, text='my label is blue')
        self.call('send', branch_id='branch-1', **original)
        branch = self.store.claim('entry')
        self.assertNotEqual(self.scope['scope'], branch['scope'])
        self.store.update_task(branch['id'], 'entry', branch['epoch'], {}, 'done', 'completed')
        self.call('send', event_id='after-return', created_at_ms=1700000000100, text='which label?')
        shared = self.store.claim('entry')
        self.assertEqual(self.scope['scope'], shared['scope'])
        self.assertIn('my label is blue', shared['input'])
        self.store.update_task(shared['id'], 'entry', shared['epoch'], {}, 'blue', 'completed')
        self.call('send', event_id='later', created_at_ms=1700000000200, text='okay')
        self.assertNotIn('my label is blue', self.store.claim('entry')['input'])


    def test_fresh_claw_message_can_reply_despite_an_unresolved_shared_turn(self):
        original = dict(event_id='prior', created_at_ms=1700000000000, text='previous work')
        self.call('send', **original)
        blocked = self.store.claim('entry')
        self.store.update_task(blocked['id'], 'entry', blocked['epoch'],
                               {'side_effect_started': True}, 'unknown', 'needs_review')
        message = {'message_id':'new-claw', 'create_time_ms':1700000001000,
                   'from_user_id':'owner', 'to_user_id':'bot', 'message_type':1,
                   'context_token':'fixture-only',
                   'item_list':[{'type':1,'text_item':{'text':'new request'}}]}
        self.store.ingest([message], 'new-batch', 'owner', 'bot')
        task = self.store.claim('entry')
        self.assertTrue(task['scope'].startswith('entrance:claw-branch:'))
        self.assertIn('new request', task['input'])
        self.assertEqual([self.scope['capability']], json.loads(task['required']))
        event = task['context']['entrance']['event_id']
        self.store.agent_action(task['id'], 'entry', task['epoch'], 'reply-claw', 'entrance_reply',
                                {'text':'accepted', 'observed_latest_message_id':event})
        with self.store.transaction() as db:
            old = db.execute('SELECT status,checkpoint FROM tasks WHERE id=?', (blocked['id'],)).fetchone()
            self.assertEqual('needs_review', old['status'])
            self.assertTrue(json.loads(old['checkpoint'])['side_effect_started'])
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM outbox WHERE body='accepted'").fetchone()[0])
