import json
from pathlib import Path
import tempfile
import unittest

from assistant_mesh.store import Conflict, Store


class StoreAtomicTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'ledger.sqlite')
        self.store.heartbeat('node', ['agent'])

    def task(self, label):
        return self.store.create_task(label, ['agent'],
            context={'session_scope': 'independent:' + label})

    def test_explicit_selection_does_not_claim_or_modify_other_tasks(self):
        first, second = self.task('first'), self.task('second')
        self.assertIsNone(self.store.claim('node', task_ids=[]))
        claimed = self.store.claim('node', task_ids=[second])
        self.assertEqual(second, claimed['id'])
        with self.store.transaction() as db:
            self.assertEqual(('pending', 0), tuple(db.execute(
                'SELECT status,epoch FROM tasks WHERE id=?', (first,)).fetchone()))
        self.assertEqual(first, self.store.claim('node')['id'])
        with self.assertRaisesRegex(ValueError, 'invalid_claim_task_ids'):
            self.store.claim('node', task_ids=second)

    def test_claim_and_receipt_roll_back_together(self):
        task = self.task('atomic-claim')
        with self.store.transaction() as db:
            db.execute('CREATE TABLE request_receipts(id TEXT PRIMARY KEY, body TEXT)')
        with self.assertRaisesRegex(RuntimeError, 'interrupted-before-receipt'):
            with self.store.transaction() as db:
                claimed = self.store._claim_in_db(db, 'node', task_ids=[task])
                self.assertEqual(task, claimed['id'])
                raise RuntimeError('interrupted-before-receipt')
        with self.store.transaction() as db:
            self.assertEqual(('pending', 0), tuple(db.execute(
                'SELECT status,epoch FROM tasks WHERE id=?', (task,)).fetchone()))
            claimed = self.store._claim_in_db(db, 'node', task_ids=[task])
            db.execute('INSERT INTO request_receipts VALUES(?,?)',
                ('pull-1', json.dumps({'id': task, 'epoch': claimed['epoch']})))
        with self.store.transaction() as db:
            receipt = json.loads(db.execute('SELECT body FROM request_receipts WHERE id=?',
                                           ('pull-1',)).fetchone()[0])
            self.assertEqual((task, 1), (receipt['id'], receipt['epoch']))

    def test_report_receipt_and_final_hook_share_the_fenced_transaction(self):
        task = self.task('atomic-report')
        claimed = self.store.claim('node', task_ids=[task])
        with self.store.transaction() as db:
            db.execute('CREATE TABLE reports(id TEXT PRIMARY KEY, body TEXT)')
            db.execute('CREATE TABLE natural_finals(task TEXT PRIMARY KEY)')
        def final_hook(db, row, node, epoch, checkpoint, result, status):
            if status == 'completed':
                db.execute('INSERT INTO natural_finals VALUES(?)', (row['id'],))
        self.store._conversation_final_in_db = final_hook
        with self.assertRaisesRegex(RuntimeError, 'lost-before-commit'):
            with self.store.transaction() as db:
                self.store._update_task_in_db(db, task, 'node', claimed['epoch'],
                    checkpoint={'side_effect_started': False}, result='done', status='completed')
                raise RuntimeError('lost-before-commit')
        with self.store.transaction() as db:
            self.assertEqual('running', db.execute('SELECT status FROM tasks WHERE id=?',
                                                 (task,)).fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM natural_finals').fetchone()[0])
            self.store._update_task_in_db(db, task, 'node', claimed['epoch'],
                checkpoint={'side_effect_started': False}, result='done', status='completed')
            db.execute('INSERT INTO reports VALUES(?,?)', ('report-1', 'done'))
        with self.assertRaises(Conflict):
            with self.store.transaction() as db:
                self.store._update_task_in_db(db, task, 'node', claimed['epoch'], status='completed')
        with self.store.transaction() as db:
            self.assertEqual((1, 1), (db.execute('SELECT COUNT(*) FROM natural_finals').fetchone()[0],
                                     db.execute('SELECT COUNT(*) FROM reports').fetchone()[0]))


if __name__ == '__main__':
    unittest.main()
