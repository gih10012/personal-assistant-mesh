"""Exact frozen Store facade, one existing private DB, no production services."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from assistant_mesh import task_transaction_adapter as adapter
from assistant_mesh.store import Conflict, Store
from tests import test_conversation_final as frozen


class TaskTransactionAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Frozen, cls.LegacyConflict, source = frozen.frozen_store()
        assert hashlib.sha256(source).hexdigest() == 'be2df48e03021f48c382efc4690fa7a8ee5e67da8bd93497e725566532e30c0b'
        cls.Adapted = adapter.make_store_class(cls.Frozen, Store)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path, self.now = Path(self.temp.name) / 'original.sqlite', 1000
        self.store = self.Adapted(self.path, clock=lambda: self.now, recover_inflight=False)
        self.store.heartbeat('node', ['agent'])

    def task(self, label, scope=None):
        return self.store.create_task(label, ['agent'],
            context={'session_scope': scope or 'independent:' + label})

    def test_modern_passthrough_and_frozen_constructor_methods_remain_original(self):
        self.assertIs(Store, adapter.make_store_class(Store, Store))
        self.assertIs(self.Adapted, adapter.make_store_class(self.Adapted, Store))
        self.assertIs(self.Frozen.__init__, self.Adapted.__init__)
        self.assertIs(self.Frozen.claim, self.Adapted.claim)
        self.assertIs(self.Frozen.update_task, self.Adapted.update_task)
        self.assertIs(self.Frozen.ingest, self.Adapted.ingest)
        self.assertIs(self.Frozen.transaction, self.Adapted.transaction)
        for name in ('_claim_in_db', '_update_task_in_db'):
            original, bound = getattr(Store, name), getattr(self.Adapted, name)
            self.assertIs(original.__code__, bound.__code__)
            self.assertIs(Conflict, original.__globals__['Conflict'])
            self.assertIs(self.LegacyConflict, bound.__globals__['Conflict'])
            self.assertEqual(set(original.__globals__), set(bound.__globals__))
            self.assertTrue(all(value is bound.__globals__[key] for key, value in original.__globals__.items()
                                if key != 'Conflict'))
        self.assertIs(self.Adapted, self.Adapted._yielded_goal_reserves_scope.__self__)
        self.assertEqual(1, self.Adapted._conversation_final_hook_version)

    def test_filtered_claim_and_receipt_are_atomic_without_touching_other_tasks(self):
        first, second = self.task('first'), self.task('second')
        self.store.elect()
        with self.store.transaction() as db:
            db.execute('CREATE TABLE fixture_claim_receipts(id TEXT PRIMARY KEY, task TEXT)')
            self.assertIsNone(self.store._claim_in_db(db, 'node', task_ids=[]))
        with self.assertRaisesRegex(RuntimeError, 'before-claim-receipt'):
            with self.store.transaction() as db:
                claimed = self.store._claim_in_db(db, 'node', task_ids=[second])
                self.assertEqual(second, claimed['id'])
                raise RuntimeError('before-claim-receipt')
        with self.store.transaction() as db:
            self.assertEqual({first: ('pending', 0), second: ('pending', 0)},
                {row['id']: (row['status'], row['epoch']) for row in
                 db.execute('SELECT id,status,epoch FROM tasks')})
            claimed = self.store._claim_in_db(db, 'node', task_ids=[second])
            db.execute('INSERT INTO fixture_claim_receipts VALUES(?,?)', ('original-pull', claimed['id']))
        with self.store.transaction() as db:
            self.assertEqual(('pending', 0), tuple(db.execute(
                'SELECT status,epoch FROM tasks WHERE id=?', (first,)).fetchone()))
            self.assertEqual(second, db.execute('SELECT task FROM fixture_claim_receipts').fetchone()[0])
        with self.assertRaises(self.LegacyConflict) as caught:
            with self.store.transaction() as db:
                self.store._claim_in_db(db, 'missing-node', task_ids=[first])
        self.assertNotIsInstance(caught.exception, Conflict)

    def test_report_final_hook_and_receipt_rollback_and_legacy_conflict_identity(self):
        identity = self.task('report')
        claimed = self.store.claim('node')
        with self.store.transaction() as db:
            db.execute('CREATE TABLE fixture_reports(id TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE fixture_natural_finals(id TEXT PRIMARY KEY)')
        def final_hook(db, row, node, epoch, checkpoint, result, status):
            if status == 'completed':
                db.execute('INSERT INTO fixture_natural_finals VALUES(?)', (row['id'],))
        self.store._conversation_final_in_db = final_hook
        with self.assertRaisesRegex(RuntimeError, 'before-report-receipt'):
            with self.store.transaction() as db:
                self.store._update_task_in_db(db, identity, 'node', claimed['epoch'],
                    checkpoint={'side_effect_started': False}, result='same result', status='completed')
                raise RuntimeError('before-report-receipt')
        with self.store.transaction() as db:
            self.assertEqual('running', db.execute('SELECT status FROM tasks WHERE id=?', (identity,)).fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM fixture_natural_finals').fetchone()[0])
            self.store._update_task_in_db(db, identity, 'node', claimed['epoch'],
                checkpoint={'side_effect_started': False}, result='same result', status='completed')
            db.execute('INSERT INTO fixture_reports VALUES(?)', ('original-report',))
        with self.assertRaises(self.LegacyConflict) as caught:
            with self.store.transaction() as db:
                self.store._update_task_in_db(db, identity, 'node', claimed['epoch'], status='completed')
        self.assertNotIsInstance(caught.exception, Conflict)
        with self.store.transaction() as db:
            self.assertEqual((1, 1), (db.execute('SELECT COUNT(*) FROM fixture_natural_finals').fetchone()[0],
                                     db.execute('SELECT COUNT(*) FROM fixture_reports').fetchone()[0]))

    def test_passive_reopen_preserves_running_media_state_and_yielded_scope(self):
        identity = self.task('running')
        claimed = self.store.claim('node')
        with self.store.transaction() as db:
            db.execute('CREATE TABLE fixture_media(id TEXT PRIMARY KEY, payload TEXT)')
            db.execute('INSERT INTO fixture_media VALUES(?,?)', ('original-media', 'retained-reference'))
        reopened = self.Adapted(self.path, clock=lambda: self.now, recover_inflight=False)
        with reopened.transaction() as db:
            self.assertEqual(('running', claimed['epoch']), tuple(db.execute(
                'SELECT status,epoch FROM tasks WHERE id=?', (identity,)).fetchone()))
            self.assertEqual('retained-reference', db.execute('SELECT payload FROM fixture_media').fetchone()[0])
        goal = {'threadId': 'same-thread', 'objective': 'original goal', 'status': 'active'}
        checkpoint = dict(side_effect_started=False, harness='codex', thread_id='same-thread', goal=goal,
            native_lifecycle=dict(outcome='yielded', controller_enabled=True, goal_managed=True,
                settled=True, runtime_closed=True, thread_id='same-thread', goal=goal,
                expected_goal_objective=goal['objective']))
        self.assertTrue(reopened._yielded_active_goal_checkpoint(checkpoint))
        parent, competing = self.task('yielded', 'same-native-scope'), self.task('competing', 'same-native-scope')
        with reopened.transaction() as db:
            db.execute("UPDATE tasks SET status='waiting_children',checkpoint=? WHERE id=?",
                       (json.dumps(checkpoint), parent))
            self.assertIsNone(reopened._claim_in_db(db, 'node', task_ids=[competing]))


if __name__ == '__main__':
    unittest.main()
