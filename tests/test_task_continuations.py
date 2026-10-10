"""Isolated authority transitions, not proof of native execution or recovery."""
import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

from assistant_mesh.remote import Remote
from assistant_mesh.server import API
from assistant_mesh.store import Conflict, Store


class TaskContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.api = API({'database': str(Path(self.temp.name) / 'mesh.db'), 'peers': []})
        self.store = self.api.store
        self.store.clock = lambda: self.now
        self.owner = {'role': 'operator'}
        self.thread = '11111111-1111-4111-8111-111111111111'
        self.original_input = '  旧任务：只执行一次\n保持原字节。  '
        self.original_result = '  已结算结果\n非公开完整内容。\n'
        self.context = {'project_id': 'fixture-project', 'session_scope': 'fixture:owner',
                        'nested': {'retained': ['original']}}
        self.store.heartbeat('node-a', ['leader', 'agent'])
        self.store.create_task(self.original_input, task_id='original-task', context=self.context)
        self.task = self.store.claim('node-a')
        self.checkpoint = {'harness': 'codex', 'thread_id': self.thread, 'turn_id': 'original-turn',
                           'codex_node': 'node-a', 'codex_auth_home': '/fixture/native-home',
                           'side_effect_started': False, 'answer': 'retained original answer',
                           'plan': {'original': True}}
        self.store.session_action(self.task['id'], 'node-a', self.task['epoch'], 'commit',
                                  {'harness': 'codex', 'state': self.checkpoint, 'parts': 0})
        self.store.update_task(self.task['id'], 'node-a', self.task['epoch'], self.checkpoint,
                               self.original_result, 'completed')
        self.payload = {'continuation_id': 'original-continuation', 'task_id': self.task['id'],
                        'expected_epoch': self.task['epoch'],
                        'task_result_sha256': hashlib.sha256(self.original_result.encode('utf8')).hexdigest(),
                        'instruction': '只进行本人此次新增的工作。'}

    def tearDown(self):
        self.temp.cleanup()

    def row(self, table='tasks', key='id', identity=None):
        with self.store.transaction() as db:
            value = db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?',
                               (identity or self.task['id'],)).fetchone()
            return dict(value) if value else None

    def change(self, **values):
        with self.store.transaction() as db:
            for key, value in values.items():
                db.execute('UPDATE tasks SET ' + key + '=? WHERE id=?', (value, self.task['id']))

    def continuation_count(self):
        with self.store.transaction() as db:
            return db.execute('SELECT COUNT(*) FROM task_continuations').fetchone()[0]

    def continue_task(self, payload=None, peer=None, method='POST', path='/v1/task/continue'):
        return self.api.dispatch(method, path, self.payload if payload is None else payload,
                                 self.owner if peer is None else peer)

    def allocation(self, state='reserved', dispatch_state='pending', settlement=None, task_id=None):
        # Exact existing authority schema, no provider execution or fabricated result claim.
        with self.store.transaction() as db:
            db.execute('''INSERT INTO managed_allocations VALUES(
                'original-operation','fixture-fingerprint','node:node-a',?,?,'node-a',?,
                '{}','{}',?,1,900,900,900)''',
                       (task_id or self.task['id'], self.task['epoch'], self.task['leader_epoch'], state))
            db.execute('''INSERT INTO managed_dispatch VALUES(
                'original-operation','fixture-capability','node:provider',?,1,
                'original-effect-receipt',?,900,900)''', (dispatch_state, settlement))

    def remote_mapping(self, parent_id):
        Remote(self.store, SimpleNamespace(store=self.store))  # Schema only; no remote call.
        with self.store.transaction() as db:
            db.execute('''INSERT INTO remote_delegations VALUES(
                'original-remote-child',?,1,'node-a','original-call',
                'fixture-peer','original-message','fixture-fingerprint',NULL,NULL,0,900,NULL)''',
                       (parent_id,))

    def test_acceptance_preserves_identity_old_bytes_and_normal_claim_epoch(self):
        before = self.row()
        native = self.row('native_sessions', 'scope', before['scope'])
        receipt = self.continue_task()
        after = self.row()
        self.assertTrue(receipt['accepted'])
        self.assertEqual('pending', receipt['task_state_at_acceptance'])
        self.assertFalse(receipt['native_turn_started'])
        self.assertFalse(receipt['native_history_modified'])
        self.assertFalse(receipt['effect_markers_changed'])
        self.assertFalse(receipt['native_tools_intercepted'])
        for field in ('id', 'scope', 'epoch', 'checkpoint', 'context', 'required', 'parent_id',
                      'attempts', 'created', 'leader_epoch'):
            self.assertEqual(before[field], after[field], field)
        self.assertEqual('pending', after['status'])
        self.assertIsNone(after['node'])
        self.assertIsNone(after['deadline'])
        self.assertIsNone(after['result'])
        self.assertIn(self.payload['instruction'], after['input'])
        self.assertNotIn(self.original_input, after['input'])
        journal = self.row('task_continuations', 'continuation_id', self.payload['continuation_id'])
        for field in ('input', 'checkpoint', 'result'):
            self.assertEqual(before[field].encode('utf8'), journal['previous_' + field].encode('utf8'))
        self.assertEqual(native, self.row('native_sessions', 'scope', before['scope']))
        claimed = self.store.claim('node-a')
        self.assertEqual(self.task['id'], claimed['id'])
        self.assertEqual(before['epoch'] + 1, claimed['epoch'])
        self.assertEqual(self.thread, claimed['sessions']['codex']['state']['thread_id'])

    def test_same_id_restart_reads_receipt_without_resetting_later_effectful_attempt(self):
        receipt = self.continue_task()
        claimed = self.store.claim('node-a')
        self.store.update_task(claimed['id'], 'node-a', claimed['epoch'], {'side_effect_started': True})
        before = self.row()
        restored = Store(self.store.path, clock=lambda: self.now)
        self.assertEqual(receipt, restored.continue_task(dict(self.payload)))
        self.assertEqual(before, self.row())
        self.assertEqual(1, self.continuation_count())

    def test_same_id_different_content_is_conflict_not_new_work(self):
        self.continue_task()
        before = self.row()
        for field, value in (('instruction', '另一个动作'), ('task_id', 'another-task'),
                             ('expected_epoch', self.task['epoch'] + 1),
                             ('task_result_sha256', '0' * 64)):
            with self.subTest(field=field), self.assertRaisesRegex(Conflict, 'id_content_conflict'):
                self.continue_task(dict(self.payload, **{field: value}))
        self.assertEqual(before, self.row())
        self.assertEqual(1, self.continuation_count())

    def test_strict_bounded_payload_types_reject_before_mutation(self):
        invalid = [None, [], dict(self.payload, force=True),
                   {key: value for key, value in self.payload.items() if key != 'instruction'}]
        for field, values in {'continuation_id': ['', '\x00id', 'x' * 201, False],
                              'task_id': ['', 10], 'expected_epoch': [True, 1.0, 0, -1, 1 << 63],
                              'task_result_sha256': ['A' * 64, '0' * 63, False],
                              'instruction': ['', ' ', '\x00', 'x' * 65536, False]}.items():
            invalid.extend(dict(self.payload, **{field: value}) for value in values)
        before = self.row()
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.continue_task(value)
        self.assertEqual(before, self.row())
        self.assertEqual(0, self.continuation_count())

    def test_not_completed_paused_and_refused_states_are_not_recovered(self):
        before = self.row()
        for state in ('running', 'pending', 'paused', 'failed', 'needs_review', 'unknown',
                      'waiting_backend', 'waiting_auth', 'waiting_children', 'continuing'):
            self.change(status=state)
            snapshot = self.row()
            with self.subTest(state=state), self.assertRaisesRegex(Conflict, 'task_not_settled'):
                self.continue_task()
            self.assertEqual(snapshot, self.row())
        self.change(status=before['status'], paused_status='needs_review')
        with self.assertRaisesRegex(Conflict, 'task_not_settled'):
            self.continue_task()
        self.assertEqual(0, self.continuation_count())

    def test_epoch_and_full_result_hash_are_atomic_compare_and_swap(self):
        for payload in (dict(self.payload, expected_epoch=self.task['epoch'] + 1),
                        dict(self.payload, task_result_sha256='0' * 64)):
            with self.assertRaises(Conflict):
                self.continue_task(payload)
        self.change(result=self.original_result.rstrip())
        with self.assertRaisesRegex(Conflict, 'result_changed'):
            self.continue_task()
        self.change(result=None)
        with self.assertRaisesRegex(Conflict, 'result_changed'):
            self.continue_task()
        self.assertEqual(0, self.continuation_count())
        self.change(result=self.original_result)
        barrier = Barrier(2)

        def competing(identity):
            barrier.wait(timeout=5)
            try:
                return self.store.continue_task(dict(self.payload, continuation_id=identity))
            except Conflict as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(competing, ('continuation-a', 'continuation-b')))
        self.assertEqual(1, sum(isinstance(value, dict) and value['accepted'] for value in outcomes))
        self.assertEqual(1, outcomes.count('continuation_task_not_settled'))
        self.assertEqual(1, self.continuation_count())

    def test_only_explicit_false_original_effect_marker_is_eligible(self):
        for marker in (True, None, 0, '', 'false'):
            checkpoint = dict(self.checkpoint, side_effect_started=marker)
            self.change(checkpoint=json.dumps(checkpoint))
            with self.subTest(marker=marker), self.assertRaisesRegex(Conflict, 'effects_require_review'):
                self.continue_task()
            self.assertEqual(marker, json.loads(self.row()['checkpoint'])['side_effect_started'])
        checkpoint = dict(self.checkpoint)
        del checkpoint['side_effect_started']
        self.change(checkpoint=json.dumps(checkpoint))
        with self.assertRaisesRegex(Conflict, 'effects_require_review'):
            self.continue_task()
        self.assertEqual(0, self.continuation_count())

    def test_active_child_descendant_and_linked_child_effects_block(self):
        self.store.create_task('child fixture', ['agent'], self.task['id'], 'original-child',
                               {'session_scope': 'fixture:child'})
        with self.assertRaisesRegex(Conflict, 'children_not_settled'):
            self.continue_task()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='completed',checkpoint=? WHERE id='original-child'",
                       (json.dumps({'side_effect_started': True}),))
        with self.assertRaisesRegex(Conflict, 'children_not_settled'):
            self.continue_task()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET checkpoint='{}' WHERE id='original-child'")
        self.store.create_task('grandchild fixture', ['agent'], 'original-child', 'original-grandchild',
                               {'session_scope': 'fixture:grandchild'})
        with self.assertRaisesRegex(Conflict, 'children_not_settled'):
            self.continue_task()
        self.assertEqual(0, self.continuation_count())
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='completed',checkpoint='{}' WHERE id='original-grandchild'")
        self.allocation('unknown', 'unknown', task_id='original-grandchild')
        before = self.row('managed_allocations', 'operation_id', 'original-operation')
        with self.assertRaisesRegex(Conflict, 'managed_effect_not_settled'):
            self.continue_task()
        self.assertEqual(before, self.row('managed_allocations', 'operation_id', 'original-operation'))
        with self.store.transaction() as db:
            db.execute("UPDATE managed_allocations SET state='completed'")
        with self.assertRaisesRegex(Conflict, 'managed_effect_not_settled'):
            self.continue_task()
        settlement = json.dumps({'outcome': 'stopped', 'evidence': {'resource_quiescent': True,
                                 'result_reference': 'fixture:never-accepted'}})
        with self.store.transaction() as db:
            db.execute("UPDATE managed_dispatch SET state='stopped',settlement=?", (settlement,))
        self.remote_mapping('original-grandchild')
        with self.assertRaisesRegex(Conflict, 'remote_child_not_settled'):
            self.continue_task()
        with self.store.transaction() as db:
            db.execute("UPDATE remote_delegations SET terminal=1,remote_status='completed'")
        self.assertTrue(self.continue_task()['accepted'])
        self.assertEqual(1, self.continuation_count())

    def test_other_live_scope_lease_or_unknown_effect_does_not_get_bypassed(self):
        self.store.create_task('another fixture task', task_id='scope-peer', context=self.context)
        peer = self.store.claim('node-a')
        with self.assertRaisesRegex(Conflict, 'scope_not_quiescent'):
            self.continue_task()
        self.store.update_task(peer['id'], 'node-a', peer['epoch'], {'side_effect_started': True},
                               status='needs_review')
        self.now += 200
        with self.assertRaisesRegex(Conflict, 'scope_not_quiescent'):
            self.continue_task()
        self.assertEqual(0, self.continuation_count())

    def test_completed_same_scope_peer_false_checkpoint_does_not_hide_managed_or_remote_unknown(self):
        self.store.create_task('same-scope fixture', task_id='scope-peer', context=self.context)
        peer = self.store.claim('node-a')
        self.store.update_task(peer['id'], 'node-a', peer['epoch'], {'side_effect_started': False},
                               'settled peer result', 'completed')
        self.allocation('unknown', 'unknown', task_id=peer['id'])
        original = self.row()
        hold = self.row('managed_allocations', 'operation_id', 'original-operation')
        dispatch = self.row('managed_dispatch', 'operation_id', 'original-operation')
        with self.assertRaisesRegex(Conflict, 'managed_effect_not_settled'):
            self.continue_task()
        self.assertEqual(original, self.row())
        self.assertEqual(hold, self.row('managed_allocations', 'operation_id', 'original-operation'))
        self.assertEqual(dispatch, self.row('managed_dispatch', 'operation_id', 'original-operation'))
        with self.store.transaction() as db:
            db.execute("UPDATE managed_allocations SET state='completed'")
        with self.assertRaisesRegex(Conflict, 'managed_effect_not_settled'):
            self.continue_task()
        settlement = json.dumps({'outcome': 'stopped', 'evidence': {'resource_quiescent': True,
                                 'result_reference': 'fixture:settled-peer'}})
        with self.store.transaction() as db:
            db.execute("UPDATE managed_dispatch SET state='stopped',settlement=?", (settlement,))
        self.remote_mapping(peer['id'])
        mapping = self.row('remote_delegations', 'child_id', 'original-remote-child')
        with self.assertRaisesRegex(Conflict, 'remote_child_not_settled'):
            self.continue_task()
        self.assertEqual(mapping, self.row('remote_delegations', 'child_id', 'original-remote-child'))
        self.assertEqual(original, self.row())
        self.assertEqual(0, self.continuation_count())

    def test_unrelated_scope_managed_and_remote_unknown_remain_untouched_without_blocking(self):
        self.store.create_task('independent fixture', task_id='independent-task',
                               context={'session_scope': 'fixture:independent'})
        peer = self.store.claim('node-a')
        self.store.update_task(peer['id'], 'node-a', peer['epoch'], {'side_effect_started': False},
                               'settled independent result', 'completed')
        self.allocation('unknown', 'unknown', task_id=peer['id'])
        self.remote_mapping(peer['id'])
        identities = (('tasks', 'id', peer['id']),
                      ('managed_allocations', 'operation_id', 'original-operation'),
                      ('managed_dispatch', 'operation_id', 'original-operation'),
                      ('remote_delegations', 'child_id', 'original-remote-child'))
        before = [self.row(*identity) for identity in identities]
        self.assertTrue(self.continue_task()['accepted'])
        self.assertEqual(before, [self.row(*identity) for identity in identities])

    def test_all_held_allocation_states_remain_held_even_after_deadline(self):
        self.allocation()
        for state in ('reserved', 'accepted', 'running', 'unknown', 'unrecognized-future-state'):
            with self.store.transaction() as db:
                db.execute('UPDATE managed_allocations SET state=?', (state,))
            snapshot = self.row('managed_allocations', 'operation_id', 'original-operation')
            with self.subTest(state=state), self.assertRaisesRegex(Conflict, 'managed_effect_not_settled'):
                self.continue_task()
            self.assertEqual(snapshot, self.row('managed_allocations', 'operation_id', 'original-operation'))
        self.assertEqual(0, self.continuation_count())

    def test_dispatch_unknown_or_missing_settlement_cannot_hide_behind_completed_allocation(self):
        self.allocation('completed', 'unknown')
        for state in ('pending', 'accepted', 'running', 'unknown', 'completed', 'stopped'):
            with self.store.transaction() as db:
                db.execute('UPDATE managed_dispatch SET state=?', (state,))
            with self.subTest(state=state), self.assertRaisesRegex(Conflict, 'managed_effect_not_settled'):
                self.continue_task()
        with self.store.transaction() as db:
            settlement = json.dumps({'outcome': 'stopped', 'evidence': {'resource_quiescent': True,
                                     'result_reference': 'fixture:original-result'}})
            db.execute("UPDATE managed_dispatch SET settlement=?,state='stopped'", (settlement,))
        before = self.row('managed_dispatch', 'operation_id', 'original-operation')
        self.assertTrue(self.continue_task()['accepted'])
        self.assertEqual(before, self.row('managed_dispatch', 'operation_id', 'original-operation'))

    def test_unsettled_original_steering_is_not_replayed_or_erased(self):
        with self.store.transaction() as db:
            db.execute('INSERT INTO steering(id,task_id,epoch,text,state) VALUES(?,?,?,?,?)',
                       ('original-steer', self.task['id'], self.task['epoch'], 'old steering', 'unknown'))
        for state in ('pending', 'submitting', 'unknown'):
            with self.store.transaction() as db:
                db.execute('UPDATE steering SET state=?', (state,))
            with self.assertRaisesRegex(Conflict, 'steering_not_settled'):
                self.continue_task()
        with self.store.transaction() as db:
            db.execute("UPDATE steering SET state='submitted'")
        before = self.row('steering', 'id', 'original-steer')
        self.assertTrue(self.continue_task()['accepted'])
        self.assertEqual(before, self.row('steering', 'id', 'original-steer'))

    def test_native_source_requires_original_codex_or_pi_identity_without_import(self):
        for state in (dict(self.checkpoint, harness='pi'),
                      dict(self.checkpoint, side_effect_started=True)):
            with self.store.transaction() as db:
                db.execute('UPDATE native_sessions SET state=?', (json.dumps(state),))
            with self.assertRaisesRegex(Conflict, 'native_source_changed'):
                self.continue_task()
        with self.store.transaction() as db:
            db.execute('UPDATE native_sessions SET state=?', (json.dumps({'thread_id': 'another-thread'}),))
        with self.assertRaisesRegex(Conflict, 'native_source_changed'):
            self.continue_task()
        checkpoint = dict(self.checkpoint, harness='codex', thread_id='noncanonical-codex-id')
        self.change(checkpoint=json.dumps(checkpoint))
        with self.assertRaisesRegex(Conflict, 'native_identity_required'):
            self.continue_task()
        checkpoint.update(harness='pi', thread_id='pi-original-session')
        self.change(checkpoint=json.dumps(checkpoint))
        with self.assertRaisesRegex(Conflict, 'native_identity_required'):
            self.continue_task()
        checkpoint['pi_session_file'] = '/fixture/pi/original-session.jsonl'
        self.change(checkpoint=json.dumps(checkpoint))
        with self.store.transaction() as db:
            db.execute('DELETE FROM native_sessions')
            db.execute('INSERT INTO native_sessions VALUES(?,?,?,?,?,?)',
                       (self.task['scope'], 'node-a', 'pi',
                        json.dumps(dict(checkpoint, pi_session_file='/fixture/pi/another-session.jsonl')),
                        None, self.now))
        with self.assertRaisesRegex(Conflict, 'native_source_changed'):
            self.continue_task()
        with self.store.transaction() as db:
            db.execute('UPDATE native_sessions SET state=?', (json.dumps(checkpoint),))
        before = self.row('native_sessions', 'scope', self.task['scope'])
        receipt = self.continue_task()
        self.assertEqual({'harness': 'pi', 'thread_id': 'pi-original-session'}, receipt['native'])
        self.assertEqual(before, self.row('native_sessions', 'scope', self.task['scope']))

    def test_route_is_operator_only_and_rejects_query_method_or_role_spoof(self):
        for peer in ({'role': 'worker', 'node': 'node-a'}, {'role': 'viewer'},
                     {'role': 'viewer', 'node': 'node-a'}, {'role': 'agent_peer', 'node': 'node-a'},
                     {'role': 'ingress', 'node': 'node-a'}, {'role': 'unknown'}):
            with self.subTest(peer=peer), self.assertRaises(PermissionError):
                self.continue_task(peer=peer)
        for method, path in (('GET', '/v1/task/continue'), ('POST', '/v1/task/continue?force=1'),
                             ('POST', '/v1/task/continue#fragment')):
            with self.assertRaises(PermissionError):
                self.continue_task(method=method, path=path)
        with self.assertRaises(ValueError):
            self.continue_task(dict(self.payload, role='operator'))
        self.assertEqual(0, self.continuation_count())


if __name__ == '__main__':
    unittest.main()
