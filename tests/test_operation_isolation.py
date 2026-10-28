"""Real task/native-session admission, exact frozen Store, no provider calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest

from assistant_mesh import operation_isolation as isolation
from assistant_mesh import native_goal_scoped
from assistant_mesh.entrance import entrance_store_class
from assistant_mesh.store import Conflict, Store
from tests import test_conversation_final as frozen


ROOT = Path(__file__).resolve().parents[1]
OLD_THREAD = '10000000-0000-4000-8000-000000000001'
NEW_THREAD = '10000000-0000-4000-8000-000000000002'


class OperationIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Frozen, cls.LegacyConflict, source = frozen.frozen_store()
        cls.source = source
        assert hashlib.sha256(source).hexdigest() == 'be2df48e03021f48c382efc4690fa7a8ee5e67da8bd93497e725566532e30c0b'
        cls.Adapted = isolation.make_store_class(cls.Frozen, Store)

    def variants(self):
        for label, cls, conflict in (('current', Store, Conflict), ('frozen', self.Adapted, self.LegacyConflict)):
            with self.subTest(base=label), tempfile.TemporaryDirectory(prefix='mesh-operation-isolation.') as directory:
                store = cls(Path(directory) / 'original.sqlite', clock=lambda: 1000, recover_inflight=False)
                store.heartbeat('node', ['leader', 'agent'], score=10)
                store.elect()
                yield store, conflict

    def task(self, store, name, **context):
        context.setdefault('operation_id', 'operation-' + name)
        return store.create_task(name, task_id='task-' + name, context=context)

    def old_unknown(self, store):
        identity = self.task(store, 'unknown-notice')
        with store.transaction() as db:
            checkpoint = {'side_effect_started': True, 'operation_id': 'operation-unknown-notice',
                          'harness': 'codex', 'thread_id': OLD_THREAD}
            db.execute("UPDATE tasks SET status='needs_review',epoch=7,checkpoint=? WHERE id=?", (json.dumps(checkpoint), identity))
            db.execute('INSERT INTO native_sessions VALUES(?,?,?,?,?,?)',
                ('leader:owner', 'node', 'codex', json.dumps(checkpoint), 'original-history', 999))
            return identity, dict(db.execute('SELECT * FROM tasks WHERE id=?', (identity,)).fetchone()), \
                dict(db.execute('SELECT * FROM native_sessions WHERE scope=?', ('leader:owner',)).fetchone())

    def grant(self, store, task, *, resources=None, **changes):
        value = dict(declaration_id='declaration-' + task, task_id=task,
                     operation_id='operation-' + task[len('task-'):], resources=resources or ['file:independent-report'],
                     independent_of=isolation.inspect_blockers(store, task),
                     reason='Distinct authorized business work; no retry or reuse of the original notice or thread', authorized=True)
        value.update(changes)
        return isolation.declare(store, **value)

    def test_real_claim_session_storage_and_continuation_keep_business_identity_memory_and_unknown(self):
        for store, conflict in self.variants():
            old, original_task, original_native = self.old_unknown(store)
            candidate = self.task(store, 'independent-report')
            with store.transaction() as db:
                db.execute('INSERT INTO memories VALUES(?,?,?,?)', ('owner-memory', 'owner', 'Existing authorized business memory', 999))
                db.execute("INSERT INTO tasks(id,input,required,status,created,context,scope) VALUES(?,?,?,?,?,?,?)",
                    ('independently-paused', 'Paused unrelated business', '["leader"]', 'paused', 998, '{}', 'paused:separate'))
            self.assertIsNone(store.claim('node'))
            grant = self.grant(store, candidate)
            task = store.claim('node', task_ids=[candidate])
            self.assertEqual((candidate, 'leader:owner', 1), (task['id'], task['scope'], task['epoch']))
            self.assertEqual('operation-independent-report', task['execution_resource']['operation_id'])
            self.assertEqual({}, task['sessions'])
            self.assertIsNone(task['session'])
            self.assertEqual('Existing authorized business memory', task['memories'][0]['text'])
            for method, payload in (('session_action', {'harness': 'codex', 'state': {'thread_id': OLD_THREAD}}),
                                    ('update_task', {'harness': 'codex', 'thread_id': OLD_THREAD})):
                with self.subTest(admission=method), self.assertRaises(conflict):
                    if method == 'session_action':
                        store.session_action(candidate, 'node', task['epoch'], 'commit', payload)
                    else:
                        store.update_task(candidate, 'node', task['epoch'], checkpoint=payload)
            state = {'harness': 'codex', 'thread_id': NEW_THREAD, 'codex_node': 'node', 'side_effect_started': False}
            receipt = store.session_action(candidate, 'node', task['epoch'], 'commit', {'harness': 'codex', 'state': state})
            self.assertEqual('leader:owner', receipt['scope'])
            store.update_task(candidate, 'node', task['epoch'], checkpoint=state, result='Independent result', status='completed')
            continuation = {'continuation_id': 'original-continuation', 'task_id': candidate, 'expected_epoch': task['epoch'],
                'task_result_sha256': hashlib.sha256(b'Independent result').hexdigest(), 'instruction': 'New follow-up to the independent report'}
            self.assertTrue(store.continue_task(continuation)['accepted'])
            resumed = store.claim('node', task_ids=[candidate])
            self.assertEqual((candidate, 2, NEW_THREAD), (resumed['id'], resumed['epoch'], resumed['session']['state']['thread_id']))
            self.assertEqual(grant['native_scope'], resumed['session']['scope'])
            with store.transaction() as db:
                self.assertEqual(original_task, dict(db.execute('SELECT * FROM tasks WHERE id=?', (old,)).fetchone()))
                self.assertEqual(original_native, dict(db.execute('SELECT * FROM native_sessions WHERE scope=?', ('leader:owner',)).fetchone()))
                self.assertEqual('paused', db.execute('SELECT status FROM tasks WHERE id=?', ('independently-paused',)).fetchone()[0])

    def test_unknown_without_effect_marker_is_fenced_on_current_and_frozen_authority(self):
        for store, conflict in self.variants():
            old, _, _ = self.old_unknown(store)
            failure = {'native_start_attempted': True, 'outcome': 'unknown', 'attempt_epoch': 7}
            with store.transaction() as db:
                checkpoint = json.loads(db.execute('SELECT checkpoint FROM tasks WHERE id=?', (old,)).fetchone()[0])
                checkpoint.update(side_effect_started=False, runtime_failure=failure)
                db.execute("UPDATE tasks SET status='waiting_backend',deadline=0,checkpoint=? WHERE id=?",
                           (json.dumps(checkpoint), old))
            candidate = self.task(store, 'independent-without-effect-marker')
            self.assertEqual([old], [p['task_id'] for p in isolation.inspect_blockers(store, candidate)])
            self.assertEqual({'requeued': 0}, store.retry_waiting())
            with self.assertRaises(conflict):
                with store.transaction() as db:
                    store._control_task(db, old, 'resume')
            self.assertIsNone(store.claim('node', task_ids=[old]))
            # A distinct authorized grant isolates only the original resource.
            self.grant(store, candidate)
            task = store.claim('node')
            self.assertEqual(candidate, task['id'])
            with store.transaction() as db:
                held = db.execute('SELECT * FROM tasks WHERE id=?', (old,)).fetchone()
                self.assertEqual(('needs_review', 7), (held['status'], held['epoch']))
                self.assertEqual(checkpoint, json.loads(held['checkpoint']))

    def test_changed_proof_and_shared_exclusive_resource_wait_without_blocking_independent_candidates(self):
        for store, conflict in self.variants():
            old, unused_task, unused_native = self.old_unknown(store)
            stale = self.task(store, 'stale-proof')
            self.grant(store, stale)
            with store.transaction() as db:
                row = db.execute('SELECT checkpoint FROM tasks WHERE id=?', (old,)).fetchone()
                checkpoint = dict(json.loads(row['checkpoint']), receipt_observation='Original result remains unknown')
                db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (json.dumps(checkpoint), old))
            self.assertIsNone(store.claim('node', task_ids=[stale]))
            fresh = self.task(store, 'fresh-independent')
            self.grant(store, fresh, resources=['file:other-business'])
            task = store.claim('node')
            self.assertEqual(fresh, task['id'])
            store.update_task(fresh, 'node', task['epoch'], checkpoint={'side_effect_started': True}, status='needs_review')
            overlap = self.task(store, 'same-exclusive-file')
            self.grant(store, overlap, resources=['file:other-business'])
            self.assertIsNone(store.claim('node', task_ids=[overlap]))
            with store.transaction() as db:
                self.assertEqual(('pending', 0), tuple(db.execute('SELECT status,attempts FROM tasks WHERE id=?', (stale,)).fetchone()))
                self.assertEqual(('needs_review', 1), tuple(db.execute('SELECT status,attempts FROM tasks WHERE id=?', (fresh,)).fetchone()))

    def test_task_json_is_not_a_grant_and_neither_old_attempts_nor_yielded_goals_can_be_rebound(self):
        for store, conflict in self.variants():
            self.old_unknown(store)
            candidate = self.task(store, 'json-untrusted', operation_isolation={'approved': True, 'new_thread': True})
            self.assertIsNone(store.claim('node', task_ids=[candidate]))
            with self.assertRaises(PermissionError):
                self.grant(store, candidate, authorized=False)
            with self.assertRaisesRegex(ValueError, 'unstarted_task_required'):
                self.grant(store, 'task-unknown-notice')
            goal = {'threadId': OLD_THREAD, 'objective': 'Original yielded goal', 'status': 'active'}
            yielded = {'side_effect_started': False, 'harness': 'codex', 'thread_id': OLD_THREAD, 'goal': goal,
                'native_lifecycle': {'outcome': 'yielded', 'controller_enabled': True, 'goal_managed': True,
                    'settled': True, 'runtime_closed': True, 'thread_id': OLD_THREAD, 'goal': goal,
                    'expected_goal_objective': goal['objective']}}
            with store.transaction() as db:
                db.execute("UPDATE tasks SET status='waiting_children',checkpoint=? WHERE id=?",
                           (json.dumps(yielded), 'task-unknown-notice'))
            self.grant(store, candidate)
            self.assertIsNone(store.claim('node', task_ids=[candidate]))

    def test_authenticated_live_parent_can_grant_only_its_actual_unstarted_child(self):
        for store, conflict in self.variants():
            self.old_unknown(store)
            parent = self.task(store, 'live-parent', session_scope='project:live-parent')
            parent_task = store.claim('node', task_ids=[parent])
            child = store.create_task('Actual authorized independent child', parent_id=parent, task_id='task-child',
                context={'operation_id': 'operation-child'})
            with self.assertRaises(PermissionError):
                self.grant(store, child, authorized=False, parent=(parent, 'wrong-node', parent_task['epoch']))
            self.grant(store, child, authorized=False, parent=(parent, 'node', parent_task['epoch']))
            self.assertEqual(child, store.claim('node', task_ids=[child])['id'])

    def test_frozen_constructor_entrance_media_and_final_hooks_remain_in_same_database(self):
        self.assertIs(self.Frozen.__init__, self.Adapted.__init__)
        self.assertIs(Store, isolation.make_store_class(Store, Store))
        media = object()
        selected = entrance_store_class(self.Adapted,
            {'owner_ref': 'fixture-owner', 'conversation_id': 'fixture-claw', 'agent_id': 'fixture-entrance'}, media)
        with tempfile.TemporaryDirectory(prefix='mesh-isolated-frozen-entrance.') as directory:
            path = Path(directory) / 'original.sqlite'
            store = selected(path, clock=lambda: 1000, recover_inflight=True)
            store.heartbeat('node', ['leader'])
            store.enqueue('old-media-outbox', 'Original media reference')
            with store.transaction() as db:
                db.execute("UPDATE outbox SET status='submitting' WHERE id='old-media-outbox'")
                db.execute('CREATE TABLE fixture_finals(task TEXT PRIMARY KEY)')
            reopened = selected(path, clock=lambda: 1000, recover_inflight=True)
            self.assertIs(media, reopened.entrance_media)
            self.assertEqual(str(path), reopened.path)
            with reopened.transaction() as db:
                self.assertEqual('submitting', db.execute("SELECT status FROM outbox WHERE id='old-media-outbox'").fetchone()[0])
            task = self.task(reopened, 'final-hook')
            claimed = reopened.claim('node')
            def final_hook(db, row, node, epoch, checkpoint, result, status):
                if status == 'completed':
                    db.execute('INSERT INTO fixture_finals VALUES(?)', (row['id'],))
            reopened._conversation_final_in_db = final_hook
            with self.assertRaisesRegex(RuntimeError, 'before-receipt'):
                with reopened.transaction() as db:
                    reopened._update_task_in_db(db, task, 'node', claimed['epoch'], status='completed')
                    raise RuntimeError('before-receipt')
            with reopened.transaction() as db:
                self.assertEqual(0, db.execute('SELECT COUNT(*) FROM fixture_finals').fetchone()[0])
            reopened.update_task(task, 'node', claimed['epoch'], status='completed')
            with self.assertRaises(self.LegacyConflict):
                reopened.update_task(task, 'node', claimed['epoch'], status='completed')
            with reopened.transaction() as db:
                self.assertEqual(1, db.execute('SELECT COUNT(*) FROM fixture_finals').fetchone()[0])

    def test_generated_native_goal_admission_retains_executor_authority_on_an_isolated_resource(self):
        spec = importlib.util.spec_from_file_location('_operation_isolation_goal_builder', str(ROOT / 'deploy/build-native-goal-scoped-candidate.py'))
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        source = builder.authority_source((ROOT / 'assistant_mesh/store.py').read_text(), self.source.decode('utf8'))
        module = types.ModuleType('assistant_mesh._isolation_fixture.authority')
        module.__package__ = 'assistant_mesh._isolation_fixture'
        exec(compile(source, '<generated-isolated-goal-authority>', 'exec'), module.__dict__)
        policy = {'format': native_goal_scoped.FORMAT, 'enabled': True, 'revision': 1,
            'authority': 'fixture-authority', 'owner_id': 'fixture-owner', 'scopes': ['leader:owner'],
            'executors': ['node'], 'activated_at': 999}
        selected = native_goal_scoped.make_store_class(Store, module.AuthorityMethods, policy)
        with tempfile.TemporaryDirectory(prefix='mesh-operation-isolated-goal.') as directory:
            store = selected(Path(directory) / 'original.sqlite', clock=lambda: 1000, recover_inflight=False)
            store.heartbeat('node', ['leader', 'agent'])
            store.elect()
            self.old_unknown(store)
            task = self.task(store, 'scoped-independent')
            self.grant(store, task)
            self.assertIsNone(store.claim('node', task_ids=[task]))
            store._native_goal_caller.node = 'node'
            claimed = store.claim('node', task_ids=[task])
            self.assertEqual(task, claimed['id'])
            self.assertEqual('leader:owner', claimed['native_goal_profile']['scope'])
            self.assertIsNone(claimed['session'])
