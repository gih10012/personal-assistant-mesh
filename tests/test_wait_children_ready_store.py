"""Managed wait readiness is an immutable, lease-fenced child snapshot."""
import json
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.store import Conflict, Store


class WaitChildrenReadyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.store = Store(Path(self.temp.name) / 'db', clock=lambda: self.now)
        self.store.heartbeat('node', ['leader', 'agent'])
        self.parent = self.store.create_task('parent')
        self.task = self.store.claim('node')
        self.guard = {'side_effect_started': True,
                      'native_execution_intent': {'settled': False, 'term_sent': False}}
        self.store.update_task(self.parent, 'node', self.task['epoch'], checkpoint=self.guard)

    def wait(self, call_id='wait'):
        return self.store.agent_action(self.parent, 'node', self.task['epoch'],
                                       call_id, 'wait_children', {})

    def checkpoint(self):
        with self.store.transaction() as db:
            row = db.execute('SELECT checkpoint FROM tasks WHERE id=?', (self.parent,)).fetchone()
            return json.loads(row['checkpoint'])

    def child(self, identity, status=None, result=None):
        child = self.store.agent_action(self.parent, 'node', self.task['epoch'],
            'delegate:' + identity, 'delegate', {'input': identity, 'agent_id': identity})
        if status is not None:
            claimed = self.store.claim('node')
            self.assertEqual(child['id'], claimed['id'])
            if status == 'paused':
                self.store.control_task(child['id'], 'pause')
            elif status != 'running':
                if status == 'waiting_children':
                    self.store.agent_action(child['id'], 'node', claimed['epoch'],
                        'grandchild:' + identity, 'delegate', {'input': 'grandchild',
                            'agent_id': 'grandchild', 'required': ['fixture-unavailable']})
                self.store.update_task(child['id'], 'node', claimed['epoch'], status=status, result=result)
        return child['id']

    def assert_guard_preserved(self):
        checkpoint = self.checkpoint()
        for key, value in self.guard.items():
            self.assertEqual(value, checkpoint[key])
        self.assertEqual('running', self.store.task_status(self.parent)['status'])

    def test_empty_children_return_ready_snapshot_not_yield(self):
        response = self.wait()
        self.assertIs(False, response['continue_after_children'])
        self.assertEqual([], response['tasks'])
        self.assertIs(False, self.checkpoint()['wait_children_requested'])
        self.assert_guard_preserved()

    def test_all_terminal_children_return_actual_results_without_settling_parent(self):
        expected = []
        for status in ('completed', 'failed', 'needs_review'):
            identity = self.child(status, status, 'result:' + status)
            expected.append({'id': identity, 'status': status, 'result': 'result:' + status})
        response = self.wait()
        self.assertIs(False, response['continue_after_children'])
        self.assertEqual(expected, response['tasks'])
        self.assertIs(False, self.checkpoint()['wait_children_requested'])
        self.assertIn('needs_review', response['instruction'])
        self.assert_guard_preserved()

    def test_nonterminal_states_still_require_wait_even_without_immediate_execution(self):
        for status in ('running', 'paused', 'waiting_auth', 'waiting_backend', 'waiting_children', 'pending'):
            with self.subTest(status=status):
                identity = self.child(status, None if status == 'pending' else status)
                response = self.wait('wait:' + status)
                self.assertIs(True, response['continue_after_children'])
                self.assertIn({'id': identity, 'status': status, 'result': None}, response['tasks'])
                self.assertIs(True, self.checkpoint()['wait_children_requested'])
                self.assert_guard_preserved()

    def test_mixed_children_include_finished_result_but_keep_pending_wait(self):
        done = self.child('done', 'completed', 'actual result')
        pending = self.child('pending')
        response = self.wait()
        self.assertIs(True, response['continue_after_children'])
        self.assertEqual([{'id': done, 'status': 'completed', 'result': 'actual result'},
                          {'id': pending, 'status': 'pending', 'result': None}], response['tasks'])
        self.assert_guard_preserved()

    def test_same_native_call_keeps_old_snapshot_new_native_call_reads_ready(self):
        child = self.child('specialist')
        original = self.wait('native-call-1')
        claimed = self.store.claim('node')
        self.assertEqual(child, claimed['id'])
        self.store.update_task(child, 'node', claimed['epoch'], status='completed', result='actual marker')
        self.assertEqual(original, self.wait('native-call-1'))
        self.assertIs(True, self.checkpoint()['wait_children_requested'])
        current = self.wait('native-call-2')
        self.assertIs(False, current['continue_after_children'])
        self.assertEqual('actual marker', current['tasks'][0]['result'])
        self.assertIs(False, self.checkpoint()['wait_children_requested'])
        self.assert_guard_preserved()

    def test_retried_ready_snapshot_does_not_hide_new_child_from_fresh_call(self):
        original = self.wait('native-call-1')
        self.child('later')
        self.assertEqual(original, self.wait('native-call-1'))
        self.assertIs(True, self.wait('native-call-2')['continue_after_children'])
        self.assert_guard_preserved()

    def test_stale_parent_lease_cannot_write_wait_or_journal(self):
        before = self.checkpoint()
        self.now += 91
        with self.assertRaises(Conflict):
            self.wait()
        self.assertEqual(before, self.checkpoint())
        with self.store.transaction() as db:
            self.assertIsNone(db.execute('SELECT * FROM agent_actions WHERE id=?', ('wait',)).fetchone())


if __name__ == '__main__':
    unittest.main()
