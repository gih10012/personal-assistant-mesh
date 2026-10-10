"""Offline Store prerequisites; not evidence of a native process yielding."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.store import Conflict, Store


class GoalScopeReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.store = Store(Path(self.temp.name) / 'ledger.sqlite', clock=lambda: self.now)
        self.store.heartbeat('owner-node', ['leader', 'agent', 'mesh.node:owner-node'])
        self.thread = '01a12439-0b17-7911-a944-72d8e56a68b9'
        self.goal = {'threadId': self.thread, 'objective': 'Original authorized parent work',
                     'status': 'active', 'tokensUsed': 275, 'timeUsedSeconds': 5}
        self.parent = self.store.create_task('Parent work', task_id='original-parent',
            context={'goal': {'objective': self.goal['objective'], 'status': 'active'},
                     'goal_mode': 'owner-native', 'project_id': 'original-project'})
        self.task = self.store.claim('owner-node')
        self.epoch = self.task['epoch']
        self.store.update_task(self.parent, 'owner-node', self.epoch,
            checkpoint={'thread_id': self.thread, 'harness': 'codex',
                        'side_effect_started': True})

    def yielded_checkpoint(self):
        return {'thread_id': self.thread, 'harness': 'codex', 'side_effect_started': False,
                'goal': copy.deepcopy(self.goal), 'native_lifecycle': {
                    'controller_enabled': True, 'goal_managed': True, 'settled': True,
                    'runtime_closed': True, 'outcome': 'yielded', 'thread_id': self.thread,
                    'expected_goal_objective': self.goal['objective'],
                    'goal': copy.deepcopy(self.goal)}}

    def checkpoint(self, task_id=None):
        with self.store.transaction() as db:
            return json.loads(db.execute('SELECT checkpoint FROM tasks WHERE id=?',
                                         (task_id or self.parent,)).fetchone()[0])

    def delegate(self, arguments=None, action_id='original-child-call'):
        return self.store.agent_action(self.parent, 'owner-node', self.epoch, action_id,
            'delegate', arguments or {'input': 'Bounded specialist work',
                                      'required': ['child-node']})

    def yield_parent(self, checkpoint=None):
        return self.store.update_task(self.parent, 'owner-node', self.epoch,
            checkpoint=checkpoint or self.yielded_checkpoint(), status='waiting_children')

    def finish_child(self, child):
        self.store.heartbeat('child-worker', ['agent', 'child-node'])
        claimed = self.store.claim('child-worker')
        self.assertEqual(child['id'], claimed['id'])
        self.store.update_task(child['id'], 'child-worker', claimed['epoch'],
                               checkpoint={'side_effect_started': False}, status='completed')
        return claimed

    def test_delegated_child_does_not_inherit_parent_goal_or_goal_mode(self):
        child = self.delegate()
        with self.store.transaction() as db:
            context = json.loads(db.execute('SELECT context FROM tasks WHERE id=?',
                                           (child['id'],)).fetchone()[0])
        self.assertNotIn('goal', context)
        self.assertNotIn('goal_mode', context)
        self.assertEqual('original-project', context['project_id'])
        self.assertEqual('project:original-project:specialist', context['session_scope'])
        with self.store.transaction() as db:
            parent_context = json.loads(db.execute('SELECT context FROM tasks WHERE id=?',
                                                   (self.parent,)).fetchone()[0])
        self.assertIn('goal', parent_context)
        self.assertEqual('owner-native', parent_context['goal_mode'])

    def test_legacy_existing_child_context_is_not_rewritten_on_receipt_retry(self):
        child = self.delegate()
        with self.store.transaction() as db:
            context = json.loads(db.execute('SELECT context FROM tasks WHERE id=?',
                                           (child['id'],)).fetchone()[0])
            context['goal'] = {'objective': 'Historical parent goal'}
            db.execute('UPDATE tasks SET context=? WHERE id=?', (json.dumps(context), child['id']))
        self.assertEqual(child, self.delegate())
        with self.store.transaction() as db:
            actual = json.loads(db.execute('SELECT context FROM tasks WHERE id=?',
                                          (child['id'],)).fetchone()[0])
        self.assertEqual(context, actual)

    def test_delegation_preserves_authority_native_permissions_and_machine_fences(self):
        context = {'goal': {'objective': 'Parent only', 'tokenBudget': 100},
                   'goal_mode': 'owner-native', 'project_id': 'project',
                   'authority': 'original-authority',
                   'session_scope': 'peer-project:original-parent',
                   'origin': {'kind': 'a2a', 'peer': 'original-peer'},
                   'sandbox': 'danger-full-access', 'approval_policy': 'never',
                   'native_permissions': {'shell': True, 'network': True, 'files': True}}
        parent = self.store.create_task('Independent scoped parent', required=['agent', 'mesh.node:owner-node'],
            task_id='peer-parent', context=context)
        task = self.store.claim('owner-node')
        self.assertEqual(parent, task['id'])
        child = self.store.agent_action(parent, 'owner-node', task['epoch'], 'peer-child-call',
            'delegate', {'input': 'Child work', 'agent_id': 'continuous-specialist'})
        with self.store.transaction() as db:
            row = db.execute('SELECT context,required FROM tasks WHERE id=?', (child['id'],)).fetchone()
        actual = json.loads(row['context'])
        for key in ('authority', 'origin', 'sandbox', 'approval_policy', 'native_permissions'):
            self.assertEqual(context[key], actual[key])
        self.assertNotIn('goal', actual)
        self.assertNotIn('goal_mode', actual)
        self.assertTrue(actual['session_scope'].startswith('peer-project:'))
        self.assertIn('mesh.node:owner-node', json.loads(row['required']))

    def test_explicit_child_goal_is_unsupported_not_silently_created(self):
        for field in ('goal', 'goal_mode'):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, 'child_native_goal_unsupported'):
                    self.delegate({'input': 'Child', field: {'objective': 'Not authorized'}},
                                  action_id='unsupported-' + field)
        self.assertEqual([], self.store.agent_action(self.parent, 'owner-node', self.epoch,
            'inspect-children', 'children', {})['tasks'])

    def test_waiting_yielded_parent_blocks_same_scope_peer_without_clearing_goal(self):
        self.delegate()
        self.yield_parent()
        peer = self.store.create_task('Separate owner request', task_id='same-scope-peer')
        self.assertIsNone(self.store.claim('owner-node'))
        self.assertEqual('pending', self.store.task_status(peer)['status'])
        checkpoint = self.checkpoint()
        self.assertFalse(checkpoint['side_effect_started'])
        self.assertEqual(self.goal, checkpoint['goal'])
        self.assertEqual('waiting_children', self.store.task_status(self.parent)['status'])

    def test_different_scope_remains_available_while_parent_goal_reserved(self):
        self.delegate()
        self.yield_parent()
        self.store.create_task('Same scope cannot steal goal', task_id='blocked-peer')
        independent = self.store.create_task('Different scoped work', task_id='different-scope',
                                             context={'agent_id': 'independent-leader'})
        self.assertEqual(independent, self.store.claim('owner-node')['id'])

    def test_child_runs_then_wakes_same_parent_with_fresh_epoch(self):
        child = self.delegate()
        self.yield_parent()
        self.finish_child(child)
        status = self.store.task_status(self.parent)
        self.assertEqual('pending', status['status'])
        self.assertIsNone(status['node'])
        self.assertEqual(self.epoch, status['epoch'])
        self.assertEqual(self.thread, status['native']['thread_id'])
        resumed = self.store.claim('owner-node')
        self.assertEqual(self.parent, resumed['id'])
        self.assertEqual(self.epoch + 1, resumed['epoch'])
        self.assertEqual(self.thread, resumed['checkpoint']['thread_id'])
        self.assertEqual(self.goal, resumed['checkpoint']['goal'])
        with self.assertRaisesRegex(Conflict, 'stale_task_lease'):
            self.store.update_task(self.parent, 'owner-node', self.epoch)

    def test_children_completed_before_yield_make_original_parent_pending(self):
        child = self.delegate()
        self.finish_child(child)
        self.assertEqual('running', self.store.task_status(self.parent)['status'])
        self.yield_parent()
        self.assertEqual('pending', self.store.task_status(self.parent)['status'])
        resumed = self.store.claim('owner-node')
        self.assertEqual(self.parent, resumed['id'])
        self.assertEqual(self.epoch + 1, resumed['epoch'])

    def check_preflight_backoff(self, status, seconds):
        child = self.delegate()
        self.finish_child(child)
        self.yield_parent()
        resumed = self.store.claim('owner-node')
        self.store.update_task(self.parent, 'owner-node', resumed['epoch'],
            checkpoint={'runtime_failure': {'phase': 'backend_preflight',
                        'native_start_attempted': False}}, status=status)
        peer = self.store.create_task('Peer cannot take suspended goal', task_id='backoff-peer')
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET created=? WHERE id=?', (999, peer))
        self.assertIsNone(self.store.claim('owner-node'))
        self.assertEqual(status, self.store.task_status(self.parent)['status'])
        self.assertFalse(self.checkpoint()['side_effect_started'])
        self.now += seconds + 1
        self.store.heartbeat('owner-node', ['leader', 'agent', 'mesh.node:owner-node'])
        claimed = self.store.claim('owner-node')
        self.assertEqual(self.parent, claimed['id'])
        self.assertEqual(resumed['epoch'] + 1, claimed['epoch'])
        self.assertEqual(self.goal, claimed['checkpoint']['goal'])
        self.assertEqual('pending', self.store.task_status(peer)['status'])

    def test_yielded_goal_failed_backend_preflight_keeps_original_backoff_and_claim(self):
        self.check_preflight_backoff('waiting_backend', 60)

    def test_yielded_goal_failed_auth_preflight_keeps_original_backoff_and_claim(self):
        self.check_preflight_backoff('waiting_auth', 300)

    def test_wake_pending_reservation_blocks_even_an_older_same_scope_peer(self):
        child = self.delegate()
        self.yield_parent()
        peer = self.store.create_task('Older ordered request', task_id='older-peer')
        # Fixture-only ordering ensures the peer is examined before its goal
        # owner. Do not rely on parent creation order to preserve ownership.
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET created=? WHERE id=?', (999, peer))
        self.finish_child(child)
        self.assertEqual(self.parent, self.store.claim('owner-node')['id'])
        self.assertEqual('pending', self.store.task_status(peer)['status'])

    def test_partial_or_fake_yielded_seal_cannot_clear_effect_guard(self):
        changes = [
            ('native_lifecycle', 'settled', False),
            ('native_lifecycle', 'settled', 1),
            ('native_lifecycle', 'runtime_closed', False),
            ('native_lifecycle', 'controller_enabled', False),
            ('native_lifecycle', 'goal_managed', False),
            ('native_lifecycle', 'thread_id', 'other-thread'),
            ('native_lifecycle', 'expected_goal_objective', 'replacement'),
            ('goal', 'threadId', 'other-thread'),
            ('goal', 'status', 'paused'),
            ('goal', 'objective', ''),
            ('goal', 'tokensUsed', 0),
        ]
        for section, key, value in changes:
            with self.subTest(section=section, key=key, value=value):
                checkpoint = self.yielded_checkpoint()
                checkpoint[section][key] = value
                with self.assertRaisesRegex(Conflict, 'native_goal_yield_unsettled'):
                    self.yield_parent(checkpoint)
                self.assertTrue(self.checkpoint()['side_effect_started'])
                self.assertEqual('running', self.store.task_status(self.parent)['status'])

    def test_yielded_active_goal_cannot_finalize_completed_to_release_reservation(self):
        with self.assertRaisesRegex(Conflict, 'native_goal_yield_requires_child_wait'):
            self.store.update_task(self.parent, 'owner-node', self.epoch,
                checkpoint=self.yielded_checkpoint(), status='completed')
        self.assertTrue(self.checkpoint()['side_effect_started'])

    def test_reservation_requires_explicit_false_guard_and_complete_binding(self):
        checkpoint = self.yielded_checkpoint()
        self.assertTrue(self.store._yielded_goal_reserves_scope('waiting_children', checkpoint))
        for key in ('side_effect_started', 'thread_id', 'harness', 'goal', 'native_lifecycle'):
            with self.subTest(key=key):
                incomplete = copy.deepcopy(checkpoint)
                incomplete.pop(key)
                self.assertFalse(self.store._yielded_goal_reserves_scope('waiting_children', incomplete))
        checkpoint['side_effect_started'] = 0
        self.assertFalse(self.store._yielded_goal_reserves_scope('waiting_children', checkpoint))

    def test_stopped_goals_or_unrelated_statuses_do_not_create_reservation(self):
        for status in ('complete', 'paused', 'blocked', 'budgetLimited', 'usageLimited'):
            checkpoint = self.yielded_checkpoint()
            checkpoint['goal']['status'] = status
            checkpoint['native_lifecycle']['goal']['status'] = status
            self.assertFalse(self.store._yielded_goal_reserves_scope('waiting_children', checkpoint))
        for status in ('running', 'completed', 'failed', 'paused', 'needs_review', 'continuing'):
            self.assertFalse(self.store._yielded_goal_reserves_scope(status, self.yielded_checkpoint()))

    def test_legacy_waiting_context_without_yielded_goal_is_unchanged(self):
        self.delegate()
        self.store.update_task(self.parent, 'owner-node', self.epoch,
            checkpoint={'side_effect_started': False, 'goal': copy.deepcopy(self.goal)},
            status='waiting_children')
        peer = self.store.create_task('Ordinary next owner work', task_id='legacy-peer')
        self.assertEqual(peer, self.store.claim('owner-node')['id'])
        self.assertEqual('waiting_children', self.store.task_status(self.parent)['status'])

    def test_effectful_wait_cannot_be_converted_into_zero_effect_by_coordination(self):
        self.delegate()
        self.store.update_task(self.parent, 'owner-node', self.epoch, status='waiting_children')
        self.assertTrue(self.checkpoint()['side_effect_started'])
        self.store.create_task('Cannot bypass original unknown', task_id='unknown-peer')
        self.assertIsNone(self.store.claim('owner-node'))


if __name__ == '__main__':
    unittest.main()
