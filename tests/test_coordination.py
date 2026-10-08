import json
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.store import Store, Conflict


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.store = Store(Path(self.temp.name) / 'db', clock=lambda: self.now)
        self.store.heartbeat('n', ['leader', 'agent'])
        self.identifier = self.store.create_task('parent')
        self.task = self.store.claim('n')

    def tearDown(self):
        self.temp.cleanup()

    def action(self, name, args, key='call'):
        return self.store.agent_action(self.identifier, 'n', self.task['epoch'], key, name, args)

    def message(self, identifier, text):
        return {'message_id': identifier, 'from_user_id': 'owner', 'to_user_id': 'bot', 'message_type': 1,
                'item_list': [{'type': 1, 'text_item': {'text': text}}]}

    def test_delegate_idempotent_and_parent_automatically_resumes(self):
        child = self.action('delegate', {'input': 'child'})
        self.assertEqual(child, self.action('delegate', {'input': 'child'}))
        self.store.update_task(self.identifier, 'n', self.task['epoch'], status='waiting_children')
        claimed = self.store.claim('n')
        self.assertEqual(child['id'], claimed['id'])
        self.store.update_task(claimed['id'], 'n', claimed['epoch'], result='child done', status='completed')
        resumed = self.store.claim('n')
        self.assertEqual(self.identifier, resumed['id'])
        self.assertEqual('child done', resumed['children'][0]['result'])
        self.assertEqual({}, self.store.status()['outbox'])

    def test_fast_child_completion_does_not_lose_parent_wakeup(self):
        self.action('delegate', {'input': 'child'})
        child = self.store.claim('n')
        self.store.update_task(child['id'], 'n', child['epoch'], result='done', status='completed')
        self.store.update_task(self.identifier, 'n', self.task['epoch'], status='waiting_children')
        self.assertEqual('pending', self.store.task_status(self.identifier)['status'])

    def test_stale_leader_cannot_mutate_memory_or_create_children(self):
        self.now += 91
        with self.assertRaises(Conflict):
            self.action('remember', {'text': 'stale'})
        with self.assertRaises(Conflict):
            self.action('delegate', {'input': 'stale'})

    def test_memory_survives_new_store_and_is_in_claim_context(self):
        self.action('remember', {'text': 'Codex first, Pi fallback'})
        self.store.update_task(self.identifier, 'n', self.task['epoch'], status='completed')
        other = Store(self.store.path, clock=lambda: self.now)
        other.create_task('next')
        self.assertEqual('Codex first, Pi fallback', other.claim('n')['memories'][0]['text'])

    def test_action_key_reuse_with_changed_content_rejected(self):
        self.action('notify', {'text': 'first'})
        with self.assertRaises(Conflict):
            self.action('notify', {'text': 'different'})

    def test_task_id_content_is_checked(self):
        args = {'task_id': 'stable', 'context': {'agent_id': 'dot'}}
        self.store.create_task('input', **args)
        self.store.create_task('input', **args)
        with self.assertRaises(Conflict):
            self.store.create_task('other', **args)

    def test_backend_failure_after_native_turn_is_not_replayed(self):
        self.store.update_task(self.identifier, 'n', self.task['epoch'],
                               checkpoint={'side_effect_started': True}, status='waiting_backend')
        self.now += 61
        self.store.heartbeat('n', ['leader', 'agent'])
        self.assertIsNone(self.store.claim('n'))
        self.assertEqual('needs_review', self.store.task_status(self.identifier)['status'])

    def test_wechat_resume_cannot_replay_effectful_paused_task(self):
        self.store.update_task(self.identifier, 'n', self.task['epoch'], checkpoint={'side_effect_started': True})
        self.store.ingest([self.message('pause', '/pause ' + self.identifier)], 'paused', 'owner', 'bot')
        paused = self.store.task_status(self.identifier)
        self.store.ingest([self.message('unsafe-resume', '/resume ' + self.identifier),
                           self.message('next', 'another task')], 'after-resume', 'owner', 'bot')
        self.assertEqual(paused, self.store.task_status(self.identifier))
        self.assertEqual({'paused': 1, 'pending': 1}, self.store.status()['tasks'])
        self.assertEqual('after-resume', self.store.get('cursor'))
        with self.assertRaises(Conflict):
            self.store.update_task(self.identifier, 'n', self.task['epoch'])

    def test_operator_resume_cannot_replay_effectful_task(self):
        self.store.update_task(self.identifier, 'n', self.task['epoch'], checkpoint={'side_effect_started': True})
        self.store.control_task(self.identifier, 'pause')
        paused = self.store.task_status(self.identifier)
        with self.assertRaises(Conflict):
            self.store.control_task(self.identifier, 'resume')
        self.assertEqual(paused, self.store.task_status(self.identifier))

    def test_resuming_running_task_does_not_start_second_turn(self):
        before = self.store.task_status(self.identifier)
        self.assertEqual('running', self.store.control_task(self.identifier, 'resume')['status'])
        self.assertEqual(before, self.store.task_status(self.identifier))
        self.assertIsNone(self.store.claim('n'))

    def test_paused_parent_resumes_waiting_for_unfinished_children(self):
        child = self.action('delegate', {'input': 'child'})
        self.store.update_task(self.identifier, 'n', self.task['epoch'], status='waiting_children')
        self.store.ingest([self.message('pause-parent', '/pause ' + self.identifier)], 'c1', 'owner', 'bot')
        self.store.ingest([self.message('resume-parent', '/resume ' + self.identifier)], 'c2', 'owner', 'bot')
        self.assertEqual('waiting_children', self.store.task_status(self.identifier)['status'])
        claimed = self.store.claim('n')
        self.assertEqual(child['id'], claimed['id'])
        self.store.update_task(child['id'], 'n', claimed['epoch'], result='done', status='completed')
        self.assertEqual(self.identifier, self.store.claim('n')['id'])

    def test_paused_parent_is_not_woken_when_children_finish(self):
        child = self.action('delegate', {'input': 'child'})
        self.store.update_task(self.identifier, 'n', self.task['epoch'], status='waiting_children')
        self.store.control_task(self.identifier, 'pause')
        self.store.control_task(self.identifier, 'pause')  # must not forget former waiting state
        claimed = self.store.claim('n')
        self.assertEqual(child['id'], claimed['id'])
        self.store.update_task(child['id'], 'n', claimed['epoch'], result='done', status='completed')
        self.assertEqual('paused', self.store.task_status(self.identifier)['status'])
        self.assertIsNone(self.store.claim('n'))
        self.assertEqual('pending', self.store.control_task(self.identifier, 'resume')['status'])
        self.assertEqual(self.identifier, self.store.claim('n')['id'])

    def test_pause_resume_preserves_native_goal_wake_delay(self):
        self.store.update_task(self.identifier, 'n', self.task['epoch'], status='continuing')
        self.now += 5
        self.store.control_task(self.identifier, 'pause')
        self.now += 5
        self.assertEqual('continuing', self.store.control_task(self.identifier, 'resume')['status'])
        self.assertIsNone(self.store.claim('n'))
        self.now += 21
        self.store.heartbeat('n', ['leader', 'agent'])
        self.assertEqual(self.identifier, self.store.claim('n')['id'])

    def test_resuming_overdue_goal_makes_it_eligible_without_losing_checkpoint(self):
        checkpoint = {'thread_id': 'continuous-thread', 'goal': {'objective': 'finish', 'status': 'active'}}
        self.store.update_task(self.identifier, 'n', self.task['epoch'], checkpoint=checkpoint, status='continuing')
        self.store.control_task(self.identifier, 'pause')
        self.now += 31
        self.assertEqual('pending', self.store.control_task(self.identifier, 'resume')['status'])
        claimed = self.store.claim('n')
        self.assertEqual(checkpoint['goal'], claimed['checkpoint']['goal'])
        self.assertEqual(checkpoint['thread_id'], claimed['checkpoint']['thread_id'])

    def test_review_cannot_be_bypassed_by_pause_resume(self):
        self.store.update_task(self.identifier, 'n', self.task['epoch'], status='needs_review')
        with self.assertRaises(Conflict):
            self.store.control_task(self.identifier, 'resume')
        self.store.control_task(self.identifier, 'pause')
        with self.assertRaises(Conflict):
            self.store.control_task(self.identifier, 'resume')

    def test_uncertain_effects_fence_following_tasks_in_same_native_scope(self):
        self.store.update_task(self.identifier, 'n', self.task['epoch'],
                               checkpoint={'side_effect_started': True}, status='waiting_backend')
        self.store.create_task('next leader task')
        independent = self.store.create_task('other project', ['agent'],
                                             context={'session_scope': 'project:other:researcher'})
        claimed = self.store.claim('n')
        self.assertEqual(independent, claimed['id'])
        self.store.update_task(claimed['id'], 'n', claimed['epoch'], status='completed')
        self.now += 61
        self.store.heartbeat('n', ['leader', 'agent'])
        self.assertIsNone(self.store.claim('n'))
        self.assertEqual('needs_review', self.store.task_status(self.identifier)['status'])

    def test_legacy_pending_effect_marker_cannot_be_replayed(self):
        # A prior release or interrupted operator transition may have left this
        # invalid combination. Claim must still not replay native effects.
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='pending',checkpoint=? WHERE id=?",
                       (json.dumps({'side_effect_started': True}), self.identifier))
        self.assertIsNone(self.store.claim('n'))
        self.assertEqual('needs_review', self.store.task_status(self.identifier)['status'])


if __name__ == '__main__':
    unittest.main()
