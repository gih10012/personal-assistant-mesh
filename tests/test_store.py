import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.store import Conflict, Store


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = Store(Path(self.tmp.name) / 'state.sqlite', clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def message(self, identifier='1', text='hello', **extra):
        value = {'message_id': identifier, 'from_user_id': 'owner', 'to_user_id': 'bot',
                 'message_type': 1, 'create_time_ms': 100, 'context_token': 'PRIVATE_CONTEXT',
                 'item_list': [{'type': 1, 'text_item': {'text': text}}]}
        value.update(extra)
        return value

    def test_batch_and_cursor_atomic(self):
        with self.assertRaises(ValueError):
            self.store.ingest([self.message(), self.message('2', item_list={})], 'bad', 'owner', 'bot')
        self.assertIsNone(self.store.get('cursor'))
        self.assertEqual({}, self.store.status()['tasks'])
        self.assertEqual([], self.store.inbox()['items'])

    def test_replay_deduplicates_messages_and_tasks(self):
        self.assertEqual(1, self.store.ingest([self.message()], 'c1', 'owner', 'bot'))
        self.assertEqual(0, self.store.ingest([self.message()], 'c2', 'owner', 'bot'))
        self.assertEqual({'pending': 1}, self.store.status()['tasks'])
        self.assertEqual('c2', self.store.get('cursor'))

    def test_other_sender_and_groups_cannot_create_jobs(self):
        self.store.ingest([self.message(from_user_id='stranger'), self.message('2', group_id='g')], 'c', 'owner', 'bot')
        self.assertEqual({}, self.store.status()['tasks'])
        self.assertIsNone(self.store.get('owner_context'))

    def test_archive_never_exposes_reply_token(self):
        self.store.ingest([self.message()], 'c', 'owner', 'bot')
        self.assertNotIn('PRIVATE_CONTEXT', json.dumps(self.store.inbox()))

    def test_single_leader_and_stale_completion_rejected(self):
        self.store.heartbeat('laptop', ['leader'], 10)
        self.store.heartbeat('cloud', ['leader'], 5)
        task_id = self.store.create_task('test')
        task = self.store.claim('laptop')
        self.assertIsNone(self.store.claim('cloud'))
        self.now += 91
        self.store.heartbeat('cloud', ['leader'], 5)
        taken = self.store.claim('cloud')
        self.assertEqual(task_id, taken['id'])
        self.assertGreater(taken['epoch'], task['epoch'])
        with self.assertRaises(Conflict):
            self.store.update_task(task_id, 'laptop', task['epoch'], result='old', status='completed')
        self.store.update_task(task_id, 'cloud', taken['epoch'], result='new', status='completed')

    def test_heartbeat_and_checkpoint_extend_leases(self):
        self.store.heartbeat('n', ['leader'])
        self.store.create_task('test')
        task = self.store.claim('n')
        for _ in range(20):
            self.now += 15
            self.store.heartbeat('n', ['leader'])
            self.store.update_task(task['id'], 'n', task['epoch'], {'step': 'x'})
        self.assertEqual('running', self.store.task_status(task['id'])['status'])

    def test_capability_requirement(self):
        self.store.heartbeat('cloud', ['leader'])
        self.store.create_task('needs laptop', ['native.wechat'])
        self.assertIsNone(self.store.claim('cloud'))

    def test_effectful_interruption_needs_review(self):
        self.store.heartbeat('n', ['leader'])
        self.store.create_task('write')
        task = self.store.claim('n')
        self.store.update_task(task['id'], 'n', task['epoch'], {'side_effect_started': True})
        self.now += 91
        self.store.heartbeat('n', ['leader'])
        self.assertIsNone(self.store.claim('n'))
        self.assertEqual('needs_review', self.store.task_status(task['id'])['status'])

    def test_send_request_id_conflict(self):
        self.store.enqueue('x', 'hello')
        self.store.enqueue('x', 'hello')
        with self.assertRaises(Conflict):
            self.store.enqueue('x', 'changed')
        self.assertEqual({'pending': 1}, self.store.status()['outbox'])

    def test_unknown_send_not_retried_after_restart(self):
        self.store.enqueue('x', 'hello')
        self.store.next_send()
        new = Store(self.store.path, clock=lambda: self.now)
        self.assertEqual('unknown', new.send_status('x')['status'])
        self.assertIsNone(new.next_send())

    def test_rejected_send_only_retries_on_fresh_context(self):
        self.store.enqueue('x', 'hello')
        first = self.store.next_send()
        self.store.finish_send('x', 'rejected')
        self.assertIsNone(self.store.next_send())
        self.store.ingest([self.message(text='ClawBot 自动刷新 test')], 'c', 'owner', 'bot')
        second = self.store.next_send()
        self.assertEqual(first['client_id'], second['client_id'])
        self.store.finish_send('x', 'rejected')
        self.assertIsNone(self.store.next_send())

    def test_pause_invalidates_running_worker(self):
        self.store.heartbeat('n', ['leader'])
        task_id = self.store.create_task('test')
        task = self.store.claim('n')
        self.store.ingest([self.message(text='/pause ' + task_id)], 'c', 'owner', 'bot')
        with self.assertRaises(Conflict):
            self.store.update_task(task_id, 'n', task['epoch'])
        self.assertIsNone(self.store.claim('n'))

    def test_status_is_not_a_model_task(self):
        self.store.ingest([self.message(text='/status')], 'c', 'owner', 'bot')
        self.assertEqual({}, self.store.status()['tasks'])
        self.assertEqual({'pending': 1}, self.store.status()['outbox'])

    def test_budget_default_is_no_spending(self):
        with self.assertRaises(Conflict):
            self.store.budget_reserve('x', 1, {})

    def test_concurrent_budget_reservations_cannot_overspend(self):
        policy = {'monthly_minor': 100, 'automatic_minor': 100}
        def reserve(i):
            try:
                self.store.budget_reserve(str(i), 60, policy)
                return True
            except Conflict:
                return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(1, sum(pool.map(reserve, [1, 2])))

    def test_budget_idempotency(self):
        policy = {'monthly_minor': 100, 'automatic_minor': 100}
        self.store.budget_reserve('x', 60, policy)
        self.store.budget_reserve('x', 60, policy)
        with self.assertRaises(Conflict):
            self.store.budget_reserve('x', 50, policy)

    def test_state_permissions(self):
        self.assertEqual(0o600, os.stat(self.store.path).st_mode & 0o777)


if __name__ == '__main__':
    unittest.main()
