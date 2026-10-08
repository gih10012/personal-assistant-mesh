"""Authority-clock fixtures for managed Leader terms, not native tool gates."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.networking import Network
from assistant_mesh.remote import Remote
from assistant_mesh.store import Conflict, Store


class LeaderTermFencingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.now = 1000
        self.store = Store(Path(self.temporary.name) / 'ledger.sqlite', clock=lambda: self.now)
        self.store.heartbeat('cloud', ['leader', 'agent'])
        self.task_id = self.store.create_task('selected Leader fixture')
        self.task = self.store.claim('cloud')

    def tearDown(self):
        self.temporary.cleanup()

    def reelect_same_node(self):
        self.now += 80
        self.store.update_task(self.task_id, 'cloud', self.task['epoch'])
        self.now += 11
        self.store.heartbeat('cloud', ['leader', 'agent'])
        leader = self.store.elect()
        self.assertEqual('cloud', leader['node'])
        self.assertGreater(leader['epoch'], self.task['leader_epoch'])
        with self.store.transaction() as db:
            self.assertGreater(db.execute('SELECT deadline FROM tasks WHERE id=?', (self.task_id,)).fetchone()[0], self.now)
        return leader

    def test_same_name_new_term_does_not_revive_old_update_action_or_session(self):
        self.store.agent_action(self.task_id, 'cloud', self.task['epoch'], 'known-call', 'notify', {'text': 'fixture'})
        self.reelect_same_node()
        actions = (
            lambda: self.store.update_task(self.task_id, 'cloud', self.task['epoch']),
            lambda: self.store.agent_action(self.task_id, 'cloud', self.task['epoch'], 'known-call', 'notify', {'text': 'fixture'}),
            lambda: self.store.agent_action(self.task_id, 'cloud', self.task['epoch'], 'new-call', 'delegate', {'input': 'no new child'}),
            lambda: self.store.session_action(self.task_id, 'cloud', self.task['epoch'], 'commit', {'harness': 'codex', 'state': {}}),
            lambda: self.store.session_action(self.task_id, 'cloud', self.task['epoch'], 'upload', {'part': 0, 'data': 'bm8='}),
            lambda: self.store.session_action(self.task_id, 'cloud', self.task['epoch'], 'download', {'part': 0}),
        )
        before = self.store.task_status(self.task_id)
        for action in actions:
            with self.assertRaisesRegex(Conflict, 'stale_task_lease'):
                action()
        self.assertEqual(before, self.store.task_status(self.task_id))
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM agent_actions').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM session_chunks').fetchone()[0])

    def test_new_term_rejects_interactions_approval_and_steering(self):
        self.store.interaction(self.task_id, 'cloud', self.task['epoch'], 'approval', 'approval', {'command': 'fixture'})
        self.store.steer(self.task_id, 'one selected instruction', 'steer')
        self.reelect_same_node()
        with self.assertRaisesRegex(Conflict, 'interaction_not_live'):
            self.store.resolve_interaction('approval', {'decision': 'accept'})
        with self.assertRaisesRegex(Conflict, 'stale_task_lease'):
            self.store.interaction(self.task_id, 'cloud', self.task['epoch'], 'approval')
        with self.assertRaisesRegex(Conflict, 'task_not_running'):
            self.store.steer(self.task_id, 'no new instruction', 'stale-steer')
        with self.assertRaisesRegex(Conflict, 'stale_task_lease'):
            self.store.poll_steering(self.task_id, 'cloud', self.task['epoch'])
        with self.store.transaction() as db:
            self.assertIsNone(db.execute('SELECT answer FROM interactions').fetchone()[0])
            self.assertEqual('pending', db.execute('SELECT state FROM steering').fetchone()[0])

    def test_remote_delegate_rechecks_leader_term_even_for_same_operation(self):
        remote = Remote(self.store, Network(self.store, 'cloud'))
        arguments = {'input': 'remote fixture', 'project_id': 'proof', 'agent_id': 'worker'}
        remote.delegate(self.task_id, 'cloud', self.task['epoch'], 'remote-call', 'laptop', arguments)
        self.reelect_same_node()
        for call in ('remote-call', 'new-remote-call'):
            with self.assertRaisesRegex(Conflict, 'stale_task_lease'):
                remote.delegate(self.task_id, 'cloud', self.task['epoch'], call, 'laptop', arguments)
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM remote_delegations').fetchone()[0])

    def test_fresh_claim_records_both_generations_without_losing_checkpoint(self):
        checkpoint = {'thread_id': 'selected-native-thread', 'side_effect_started': False}
        self.store.update_task(self.task_id, 'cloud', self.task['epoch'], checkpoint=checkpoint)
        self.reelect_same_node()
        self.now += 80
        self.store.heartbeat('cloud', ['leader', 'agent'])
        following = self.store.claim('cloud')
        self.assertEqual(self.task_id, following['id'])
        self.assertGreater(following['epoch'], self.task['epoch'])
        self.assertGreater(following['leader_epoch'], self.task['leader_epoch'])
        self.assertEqual(checkpoint['thread_id'], following['checkpoint']['thread_id'])
        self.store.update_task(self.task_id, 'cloud', following['epoch'])
        with self.assertRaisesRegex(Conflict, 'stale_task_lease'):
            self.store.update_task(self.task_id, 'cloud', self.task['epoch'])

    def test_nonleader_task_remains_live_when_global_leader_changes(self):
        other_id = self.store.create_task('native-independent agent fixture', ['agent'],
                                         context={'session_scope': 'project:independent'})
        other = self.store.claim('cloud')
        self.assertEqual(other_id, other['id'])
        self.assertIsNone(other['leader_epoch'])
        self.now += 80
        self.store.update_task(other_id, 'cloud', other['epoch'])
        self.now += 11
        self.store.heartbeat('cloud', ['leader', 'agent'])
        self.store.elect()
        self.store.update_task(other_id, 'cloud', other['epoch'], status='completed')

    def test_invalid_explicit_epoch_cannot_bypass_worker_binding(self):
        for epoch in (None, True, 1.0, '1'):
            with self.subTest(epoch=epoch), self.assertRaisesRegex(Conflict, 'stale_task_lease'):
                self.store.update_task(self.task_id, 'cloud', epoch)
        with self.assertRaisesRegex(Conflict, 'stale_task_lease'):
            self.store.update_task(self.task_id, None, self.task['epoch'])

    def test_unknown_legacy_active_term_is_not_invented(self):
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET leader_epoch=NULL WHERE id=?', (self.task_id,))
        reopened = Store(self.store.path, clock=lambda: self.now)
        with self.assertRaisesRegex(Conflict, 'stale_task_lease'):
            reopened.update_task(self.task_id, 'cloud', self.task['epoch'])
        self.assertIsNone(reopened.task_status(self.task_id)['leader_epoch'])
        self.assertEqual('running', reopened.task_status(self.task_id)['status'])

    def test_old_schema_migration_keeps_history_and_unknown_leader_binding(self):
        path = Path(self.temporary.name) / 'old.sqlite'
        with sqlite3.connect(str(path)) as db:
            db.execute('''CREATE TABLE tasks(id TEXT PRIMARY KEY,parent_id TEXT,input TEXT NOT NULL,
                required TEXT NOT NULL,status TEXT NOT NULL,node TEXT,epoch INTEGER NOT NULL DEFAULT 0,
                deadline REAL,checkpoint TEXT NOT NULL DEFAULT '{}',result TEXT,created REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0)''')
            db.execute('INSERT INTO tasks(id,input,required,status,node,epoch,deadline,checkpoint,result,created) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?)', ('legacy', 'unchanged fixture', '["leader"]', 'running',
                         'cloud', 4, self.now + 90, '{"thread_id":"unchanged"}', 'unchanged result', self.now))
        migrated = Store(path, clock=lambda: self.now)
        with migrated.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', ('legacy',)).fetchone()
            self.assertEqual('unchanged fixture', row['input'])
            self.assertEqual('unchanged result', row['result'])
            self.assertEqual({'thread_id': 'unchanged'}, json.loads(row['checkpoint']))
            self.assertEqual(4, row['epoch'])
            self.assertIsNone(row['leader_epoch'])


if __name__ == '__main__':
    unittest.main()
