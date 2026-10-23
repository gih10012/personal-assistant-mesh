"""New scoped seams only; original source objects and synthetic native events.

No actual native RPC, model, HTTP listener, production state or original case.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.test_legacy_fenced_receiver import _source_base


ROOT = Path(__file__).resolve().parents[1]


def load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, str(path),
        submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class NativeGoalScopedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='mesh-goal-scoped-source.')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        historical = cls.root / 'historical' / 'assistant_mesh'
        historical.mkdir(parents=True, mode=0o700)
        for name, raw in _source_base().items():
            (historical / name).write_bytes(raw)
        entrance = load('_goal_scoped_entrance_builder', ROOT / 'deploy/build-entrance-candidate.py')
        entrance.build(historical.parent, cls.root / 'entrance', ROOT)
        final = load('_goal_scoped_final_builder', ROOT / 'deploy/build-conversation-final-candidate.py')
        base = cls.root / 'entrance'
        report = final.build(base, cls.root / 'final', final.inspect_base(base)['base_manifest_sha256'], ROOT)
        cls.builder = load('_goal_scoped_source_builder', ROOT / 'deploy/build-native-goal-scoped-candidate.py')
        cls.target = cls.root / 'scoped'
        cls.report = cls.builder.build(cls.root / 'final', cls.target, report['candidate_manifest_sha256'], ROOT)
        cls.package_name = '_fixture_scoped_goal_candidate'
        cls.package = load(cls.package_name, cls.target / 'assistant_mesh/__init__.py', True)
        cls.scope = __import__(cls.package_name + '.native_goal_scoped', fromlist=['x'])
        cls.runtime = __import__(cls.package_name + '.native_goal_runtime.codex', fromlist=['x'])
        cls.strategy_module = __import__(cls.package_name + '.native_goal_runtime.worker_strategy', fromlist=['x'])
        cls.authority = __import__(cls.package_name + '.native_goal_runtime.authority', fromlist=['x'])
        cls.old_store = __import__(cls.package_name + '.store', fromlist=['x'])
        cls.old_worker = __import__(cls.package_name + '.worker', fromlist=['x'])

    def policy(self, **changes):
        value = {'format': self.scope.FORMAT, 'enabled': True, 'revision': 1,
            'authority': 'synthetic-authority', 'owner_id': 'synthetic-owner',
            'scopes': ['project:future-goal'], 'executors': ['node'], 'activated_at': 999}
        value.update(changes)
        return value

    def test_source_candidate_single_claim_legacy_body_and_pure_import_closure(self):
        self.assertEqual(['assistant_mesh/entrance_runtime.py', 'assistant_mesh/legacy_fenced_receiver.py',
                          'assistant_mesh/worker.py'], self.report['changed_original_files'])
        self.assertFalse(self.report['production_installed'])
        self.assertEqual(12, len(self.report['optional_runtime_modules']))
        inspected = self.builder.inspect_base(self.root / 'final', ROOT)
        self.assertEqual(self.report['base_manifest_sha256'], inspected['base_manifest_sha256'])
        original = (self.root / 'final/assistant_mesh/worker.py').read_text()
        current = (self.target / 'assistant_mesh/worker.py').read_text()
        expected_tail = original[original.index('        self.current = task\n', original.index('    def run_once')):
                                 original.index('\n    def run(self):', original.index('    def run_once'))]
        self.assertEqual(expected_tail, current[current.index('        self.current = task\n', current.index('    def run_claimed')):
                                                current.index('\n    def run(self):', current.index('    def run_claimed'))])
        claims = []
        class Client:
            def request(self, route, body=None):
                claims.append(route)
                return {'task': {'id': 'legacy-task', 'scope': 'legacy'}} if route == '/v1/claim' else {'ok': True}
        worker = self.old_worker.Worker({'codex': {}}, client=Client())
        worker.run_claimed = mock.Mock(return_value='original-claimed-body')
        self.assertEqual('original-claimed-body', worker.run_once())
        self.assertEqual(1, claims.count('/v1/claim'))
        worker.run_claimed.assert_called_once()
        probe = """
import sqlite3,sys
sqlite3.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('import opens ledger'))
from assistant_mesh import native_goal_scoped, server
from assistant_mesh.native_goal_runtime import codex,worker_strategy,authority,native_goal_observer
from assistant_mesh.legacy_fenced_receiver import _check_frozen_source
_check_frozen_source(server)
assert not any(n=='mcp' or n.startswith(('mcp.','fastmcp')) for n in sys.modules)
print('pure source import only')
"""
        result = subprocess.run([sys.executable, '-S', '-B', '-c', probe], cwd=str(self.target),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def api(self, directory, policy=None):
        policy = policy or self.policy()
        store_class = self.scope.make_store_class(self.old_store.Store, self.authority.AuthorityMethods, policy)
        store = store_class(Path(directory) / 'ledger', clock=lambda: 1000, recover_inflight=False)
        node = {'role': 'worker', 'node': 'node', 'capabilities': ['leader', self.scope.CAPABILITY]}
        ingress = types.SimpleNamespace(authority=policy['authority'], owner_id=policy['owner_id'])
        def dispatch(method, path, payload, peer):
            if path == '/v1/claim':
                return {'task': store.claim(peer['node'])}
            if path == '/v1/task/update':
                return store.update_task(payload['id'], peer['node'], payload['epoch'],
                    payload.get('checkpoint'), payload.get('result'), payload.get('status'))
            if path == '/v1/session':
                return store.session_action(payload['task_id'], peer['node'], payload['epoch'], payload['action'], payload['payload'])
            if path == '/v1/agent/action':
                return store.agent_action(payload['task_id'], peer['node'], payload['epoch'], payload['call_id'], payload['action'], payload['arguments'])
            raise AssertionError(path)
        api = types.SimpleNamespace(store=store, ingress=ingress, peers=[(b'SYNTHETIC_ONLY', node)], dispatch=dispatch)
        self.scope.attach_authority(api, policy)
        store.heartbeat('node', ['leader', 'agent'])
        return api, node

    def test_authority_grant_active_yield_scope_guard_and_commit_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            api, peer = self.api(directory)
            store = api.store
            parent = store.create_task('future work', context={'session_scope': 'project:future-goal'})
            other = dict(peer, capabilities=['leader'])
            self.assertIsNone(api.dispatch('POST', '/v1/claim', {}, other)['task'])
            task = api.dispatch('POST', '/v1/claim', {}, peer)['task']
            self.assertEqual(parent, task['id'])
            self.assertEqual(self.policy()['activated_at'], task['native_goal_profile']['activated_at'])
            base = {'id': parent, 'epoch': task['epoch']}
            with self.assertRaises(ValueError):
                api.dispatch('POST', '/v1/task/update', dict(base, checkpoint={'side_effect_started': False}, status='completed'), peer)
            state = {'native_goal_profile': task['native_goal_profile'], 'side_effect_started': True,
                     'harness': 'codex', 'thread_id': 'original-thread'}
            api.dispatch('POST', '/v1/task/update', dict(base, checkpoint=state), peer)
            session = {'task_id': parent, 'epoch': task['epoch'], 'action': 'commit',
                       'payload': {'harness': 'codex', 'parts': 0, 'state': state}}
            self.assertTrue(api.dispatch('POST', '/v1/session', session, peer)['ok'])
            session['payload']['state'] = dict(state, side_effect_started=False)
            with self.assertRaises(ValueError):
                api.dispatch('POST', '/v1/session', session, peer)
            child = api.dispatch('POST', '/v1/agent/action', {'task_id': parent, 'epoch': task['epoch'],
                'call_id': 'one-child', 'action': 'delegate', 'arguments': {'input': 'child', 'agent_id': 'child'}}, peer)
            api.dispatch('POST', '/v1/task/update', dict(base, status='waiting_children'), peer)
            with store.transaction() as db:
                self.assertTrue(json.loads(db.execute('SELECT checkpoint FROM tasks WHERE id=?', (parent,)).fetchone()[0])['side_effect_started'])
                db.execute("UPDATE tasks SET status='running',node='node',deadline=1090 WHERE id=?", (parent,))
            goal = {'threadId': 'original-thread', 'objective': 'same owner outcome', 'status': 'active'}
            yielded = dict(state, side_effect_started=False, goal=goal,
                native_lifecycle={'controller_enabled': True, 'goal_managed': True, 'settled': True,
                    'runtime_closed': True, 'outcome': 'yielded', 'thread_id': 'original-thread',
                    'goal': goal, 'expected_goal_objective': goal['objective']})
            api.dispatch('POST', '/v1/task/update', dict(base, checkpoint=yielded, status='waiting_children'), peer)
            competing = store.create_task('must not take active parent scope', context={'session_scope': task['scope']})
            claimed = api.dispatch('POST', '/v1/claim', {}, peer)['task']
            self.assertEqual(child['id'], claimed['id'])
            self.assertNotEqual(competing, claimed['id'])
            # Disabled policy retains the admitted scope fence across recreation.
            disabled_api, disabled_peer = self.api(directory, self.policy(enabled=False))
            self.assertIsNone(disabled_api.dispatch('POST', '/v1/claim', {}, disabled_peer)['task'])

    def test_scoped_strategy_keeps_guard_during_failed_commit_and_preserves_legacy_mro(self):
        from tests.test_native_goal_worker import GoalWorkerTests
        fixture = GoalWorkerTests('test_durable_guard_precedes_resume_even_for_read_only_and_settles_after_seal')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        class LegacyBackend:
            def __init__(self, config, **kwargs):
                self.config, self.initialized = config, ['legacy']
            def rpc(self, method, params):
                return 'legacy-rpc'
        class GoalBackend(LegacyBackend):
            def __init__(self, config, **kwargs):
                self.config, self.initialized = config, ['goal']
            def rpc(self, method, params):
                return 'goal-rpc'
        hybrid = self.scope.make_backend(LegacyBackend, GoalBackend)
        class OriginalOuter(hybrid):
            def __init__(self, config, **kwargs):
                super(OriginalOuter, self).__init__(config, **kwargs)
                self.initialized.append('profile-and-failure')
            def rpc(self, method, params):
                return 'outer:' + super(OriginalOuter, self).rpc(method, params)
        plain = OriginalOuter({})
        goal = OriginalOuter({self.scope.FLAG: True, 'native_transport': 'unix',
                              'native_goal_drain': True, 'native_goal_yield': True})
        self.assertEqual(['legacy', 'profile-and-failure'], plain.initialized)
        self.assertEqual(['goal', 'profile-and-failure'], goal.initialized)
        self.assertEqual('outer:goal-rpc', goal.rpc('x', {}))
        from assistant_mesh.governance_worker import make_worker as governance_factory
        governance = object()  # Constructor composition only; no model/route call.
        assembled = self.scope.assemble_worker(self.old_worker.Worker, LegacyBackend,
            self.policy(), governance_factory=governance_factory, governance=governance,
            governance_scopes=['project:future-goal'],
            components=(GoalBackend, self.strategy_module.WorkerStrategy))
        assembled_worker = assembled({'node_id': 'fixture-node', 'codex': {}}, client=fixture.client)
        self.assertIsNotNone(assembled_worker.backend._native_goal_backend_version)
        self.assertIn('QuotaObservedCodex', [cls.__name__ for cls in assembled_worker.backend.__mro__])
        with self.assertRaisesRegex(ValueError, 'native_goal_scope_quality_policy_required'):
            self.scope.assemble_worker(self.old_worker.Worker, LegacyBackend, self.policy(),
                governance_factory=governance_factory, governance=governance, governance_scopes=[],
                components=(GoalBackend, self.strategy_module.WorkerStrategy))
        disabled = self.scope.assemble_worker(self.old_worker.Worker, LegacyBackend,
            self.policy(enabled=False), governance_factory=governance_factory,
            governance=governance, governance_scopes=[])
        self.assertFalse(hasattr(disabled, '_native_goal_backend_version'))
        worker_class = self.scope.make_worker(self.old_worker.Worker, hybrid,
            self.strategy_module.WorkerStrategy, self.policy())
        task = fixture.task
        task.update(scope='project:future-goal', created=1000,
                    native_goal_profile=self.scope._profile(self.policy(), 'project:future-goal', 999))
        worker = worker_class({'node_id': 'fixture-node', 'codex': {'auth_home': '/fixture/auth'}},
                             client=fixture.client, backend=hybrid)
        fixture.worker = worker
        worker.harness = 'codex'
        worker.resource_reference = mock.Mock(return_value={})
        worker.open_backend = mock.Mock(return_value=fixture.agent)
        saved = []
        def failed_commit(client, original, node, harness, state, *args, **kwargs):
            saved.append(copy.deepcopy(state))
            raise OSError('synthetic commit response lost')
        fixture.agent.native_lifecycle['history_fingerprint'] = {'synthetic': True}
        with mock.patch.object(self.strategy_module.sessions, 'save', side_effect=failed_commit):
            worker.run_once()
        self.assertTrue(saved[-1]['side_effect_started'])
        self.assertTrue(task['checkpoint']['side_effect_started'])
        self.assertEqual(1, sum(route == '/v1/claim' for route, unused in fixture.client.calls))
        self.assertNotIn('completed', [body.get('status') for route, body in fixture.client.calls if route == '/v1/task/update'])

    def test_actual_controller_algorithms_adopt_immediate_auto_turn_and_reject_abort_without_goal_reset(self):
        from tests.test_native_goal_lifecycle import NativeGoalLifecycleTests
        fixture = NativeGoalLifecycleTests()
        actual = fixture.goal(createdAt=100, updatedAt=101)
        agent = fixture.runtime(requested=False, actual=actual)
        agent.__class__ = self.runtime.Codex
        agent.config.update(native_goal_drain=True, native_goal_yield=True, native_transport='unix')
        agent.start('new explicit input', {'thread_id': fixture.thread})
        agent.deferred = [fixture.started('auto-first'), fixture.message('auto-first', 'first output'),
                          fixture.completed('auto-first'), fixture.started('auto-second'),
                          fixture.completed('auto-second', status='interrupted')]
        with self.assertRaisesRegex(ValueError, '^codex_turn_failed$'):
            agent.finish(timeout=1)
        self.assertEqual(['auto-first', 'auto-second'], agent.native_lifecycle['turn_ids'])
        self.assertFalse(agent.native_lifecycle['settled'])
        self.assertEqual(actual['createdAt'], agent.native_lifecycle['goal_baseline']['createdAt'])
        self.assertEqual(actual['tokensUsed'], agent.native_lifecycle['goal']['tokensUsed'])
        self.assertEqual(1, sum(method == 'turn/steer' for method, unused in agent.calls))
        self.assertFalse(any(method in ('thread/goal/set', 'turn/start') for method, unused in agent.calls))
        newer = dict(actual, updatedAt=105, tokensUsed=150)
        agent._store_goal_snapshot(newer)
        with self.assertRaisesRegex(ValueError, 'codex_native_goal_continuity_unverified'):
            agent._store_goal_snapshot(dict(actual, updatedAt=106, tokensUsed=140))
        # Exercise the generated close/history algorithm on synthetic native
        # JSONL; process closure here is a mock, never native acceptance.
        with tempfile.TemporaryDirectory() as directory:
            completed = fixture.runtime(requested=False,
                actual=fixture.goal('complete', createdAt=100, updatedAt=101))
            completed.__class__ = self.runtime.Codex
            completed.config.update(native_goal_drain=True, native_goal_yield=True, native_transport='unix')
            completed.start('one synthetic input')
            completed.deferred = [fixture.started(completed.turn_id), fixture.completed(completed.turn_id)]
            completed.finish(timeout=1)
            rollout = Path(directory) / 'native.jsonl'
            records = [{'type': 'session_meta', 'payload': {'id': fixture.thread}},
                       {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': completed.turn_id}},
                       {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': completed.turn_id}}]
            raw = ''.join(json.dumps(record) + '\n' for record in records).encode()
            rollout.write_bytes(raw); rollout.chmod(0o600)
            completed.rollout_path = str(rollout)
            tick = mock.Mock()
            calls = len(completed.calls)
            seal = completed.seal_native_lifecycle(tick=tick)
            self.assertTrue(seal['settled'])
            self.assertEqual({completed.turn_id: 'completed'}, seal['completion_statuses'])
            self.assertEqual(len(raw), seal['history_fingerprint']['size'])
            self.assertGreater(tick.call_count, 0)
            self.assertEqual(calls, len(completed.calls))  # No post-close native RPC.

    def test_real_fingerprinted_persistence_keeps_session_guard_until_final_and_never_repeats_lost_commit(self):
        import base64
        import gzip
        import hashlib
        import os
        import stat
        for lose_commit in (False, True):
            with self.subTest(lose_commit=lose_commit), tempfile.TemporaryDirectory() as directory:
                api, peer = self.api(directory)
                store = api.store
                identity = store.create_task('future owned persistence', context={'session_scope': 'project:future-goal'})
                raw = b'SYNTHETIC FILE BYTES; native closure is a fake boundary in this unit.\n' * 2000
                rollout = Path(directory) / 'source.jsonl'
                rollout.write_bytes(raw); rollout.chmod(0o600)
                class Client:
                    def __init__(self):
                        self.calls = []
                    def request(self, route, body=None):
                        self.calls.append((route, copy.deepcopy(body)))
                        if route == '/v1/heartbeat':
                            return store.heartbeat('node', body['capabilities'])
                        if route == '/v1/steering':
                            return {'steering': None}
                        result = api.dispatch('POST', route, body or {}, peer)
                        if (lose_commit and route == '/v1/session' and body['action'] == 'commit'
                                and body['payload'].get('parts', 0) > 0):
                            raise OSError('synthetic lost original commit response')
                        return result
                client = Client()
                class EmptyBackend:
                    pass
                hybrid = self.scope.make_backend(EmptyBackend, EmptyBackend)
                worker_class = self.scope.make_worker(self.old_worker.Worker, hybrid,
                    self.strategy_module.WorkerStrategy, self.policy())
                worker = worker_class({'node_id': 'node', 'codex': {'auth_home': '/fixture/native'}},
                    client=client, backend=hybrid)
                worker.harness = 'codex'
                worker.resource_reference = mock.Mock(return_value={})
                goal = {'threadId': 'synthetic-thread', 'objective': 'fixture', 'status': 'complete',
                        'createdAt': 100, 'updatedAt': 101, 'tokensUsed': 12, 'timeUsedSeconds': 3, 'tokenBudget': None}
                class Agent:
                    auth_home = '/fixture/native'
                    turn_id = None
                    rpc_depth = 0
                    native_drain_active = False
                    def __init__(self):
                        self.native_lifecycle = {'controller_enabled': True, 'goal_managed': True,
                            'thread_id': 'synthetic-thread', 'settled': False, 'runtime_closed': False,
                            'quiescent': False, 'goal': goal, 'turn_ids': [], 'completed_turn_ids': []}
                        self.closed = False
                    def __enter__(self):
                        return self
                    def __exit__(self, *unused):
                        self.closed = True
                    def start(self, text, resume):
                        worker.on_activity('native_start_intent', {'thread_id': None, 'method': 'thread/start'})
                        worker.on_activity('session_ready', {'thread_id': 'synthetic-thread'})
                        self.turn_id = 'first'
                        worker.on_activity('native_turn_adopted', {'thread_id': 'synthetic-thread', 'turn_id': 'first'})
                        return {'thread_id': 'synthetic-thread', 'turn_id': 'first'}
                    def finish(self, **unused):
                        self.turn_id = 'second'
                        self.native_lifecycle.update(turn_ids=['first', 'second'], completed_turn_ids=['first', 'second'],
                            completion_statuses={'first': 'completed', 'second': 'completed'}, quiescent=True)
                        worker.on_activity('native_turn_adopted', {'thread_id': 'synthetic-thread', 'turn_id': 'second'})
                        worker.on_activity('native_goal_snapshot', {'thread_id': 'synthetic-thread', 'goal': goal})
                        return 'synthetic native answer'
                    def native_rollout(self):
                        assert not self.closed, 'no post-close native read'
                        return rollout
                    def seal_native_lifecycle(self, tick=None):
                        self.closed = True
                        info = rollout.stat()
                        self.native_lifecycle.update(settled=True, runtime_closed=True,
                            history_fingerprint={'dev': info.st_dev, 'ino': info.st_ino, 'uid': info.st_uid,
                                'mode': stat.S_IMODE(info.st_mode), 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                                'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns})
                agent = Agent()
                worker.open_backend = mock.Mock(return_value=agent)
                worker.run_once()
                commits = [body for route, body in client.calls if route == '/v1/session'
                           and body['action'] == 'commit' and body['payload'].get('parts', 0) > 0]
                self.assertEqual(1, len(commits))
                self.assertTrue(commits[0]['payload']['state']['side_effect_started'])
                with store.transaction() as db:
                    row = db.execute('SELECT * FROM tasks WHERE id=?', (identity,)).fetchone()
                    checkpoint = json.loads(row['checkpoint'])
                    session = db.execute('SELECT * FROM native_sessions WHERE scope=?', (row['scope'],)).fetchone()
                    self.assertIsNotNone(session)
                    self.assertTrue(json.loads(session['state'])['side_effect_started'])
                    chunks = b''.join(part[0] for part in db.execute('SELECT body FROM session_chunks WHERE id=? ORDER BY part',
                                                                   (session['artifact'],)))
                    self.assertEqual(raw, gzip.decompress(chunks))
                    self.assertEqual(lose_commit, checkpoint['side_effect_started'])
                    self.assertEqual('waiting_backend' if lose_commit else 'completed', row['status'])
                    self.assertEqual(1, row['epoch'])
                self.assertTrue(agent.closed)
                self.assertEqual(1, sum(route == '/v1/claim' for route, unused in client.calls))

    def test_paused_yielded_parent_keeps_scope_until_its_original_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            api, peer = self.api(directory)
            store = api.store
            parent = store.create_task('owned parent', context={'session_scope': 'project:future-goal'})
            task = api.dispatch('POST', '/v1/claim', {}, peer)['task']
            child = api.dispatch('POST', '/v1/agent/action', {'task_id': parent, 'epoch': task['epoch'],
                'call_id': 'pause-child', 'action': 'delegate', 'arguments': {'input': 'bounded child'}}, peer)
            goal = {'threadId': 'same-thread', 'objective': 'same goal', 'status': 'active'}
            checkpoint = {'native_goal_profile': task['native_goal_profile'], 'side_effect_started': False,
                'harness': 'codex', 'thread_id': 'same-thread', 'goal': goal,
                'native_lifecycle': {'controller_enabled': True, 'goal_managed': True, 'settled': True,
                    'runtime_closed': True, 'outcome': 'yielded', 'thread_id': 'same-thread',
                    'goal': goal, 'expected_goal_objective': goal['objective']}}
            api.dispatch('POST', '/v1/task/update', {'id': parent, 'epoch': task['epoch'],
                'checkpoint': checkpoint, 'status': 'waiting_children'}, peer)
            with store.transaction() as db:
                store._control_task(db, parent, 'pause')
                self.assertEqual('waiting_children', db.execute('SELECT paused_status FROM tasks WHERE id=?', (parent,)).fetchone()[0])
            store.clock = lambda: 1001
            competing = store.create_task('cannot take paused Goal', context={'session_scope': task['scope']})
            claimed_child = api.dispatch('POST', '/v1/claim', {}, peer)['task']
            self.assertEqual(child['id'], claimed_child['id'])
            api.dispatch('POST', '/v1/task/update', {'id': child['id'], 'epoch': claimed_child['epoch'],
                'checkpoint': {'side_effect_started': False}, 'status': 'completed'}, peer)
            self.assertIsNone(api.dispatch('POST', '/v1/claim', {}, peer)['task'])
            with store.transaction() as db:
                self.assertEqual('pending', store._control_task(db, parent, 'resume')['status'])
            resumed = api.dispatch('POST', '/v1/claim', {}, peer)['task']
            self.assertEqual(parent, resumed['id'])
            self.assertNotEqual(competing, resumed['id'])
            self.assertEqual(goal, resumed['checkpoint']['goal'])

    def test_guarded_session_continuation_requires_original_final_proof_and_committed_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            api, peer = self.api(directory)
            store = api.store
            identity = store.create_task('settled original', context={'session_scope': 'project:future-goal'})
            task = api.dispatch('POST', '/v1/claim', {}, peer)['task']
            thread = '00000000-0000-0000-0000-000000000123'
            raw = b'SYNTHETIC SEALED HISTORY FOR ORIGINAL CONTINUATION\n'
            rollout = Path(directory) / 'original.jsonl'
            rollout.write_bytes(raw); rollout.chmod(0o600)
            fingerprint = dict(self.strategy_module.sessions._fingerprint_metadata(rollout.stat()),
                               sha256=hashlib.sha256(raw).hexdigest())
            goal = {'threadId': thread, 'objective': 'original outcome', 'status': 'complete',
                    'createdAt': 100, 'updatedAt': 101, 'tokensUsed': 12, 'timeUsedSeconds': 3, 'tokenBudget': None}
            lifecycle = {'controller_enabled': True, 'goal_managed': True, 'settled': True,
                'runtime_closed': True, 'quiescent': True, 'thread_id': thread, 'turn_id': 'finished',
                'turn_ids': ['finished'], 'completed_turn_ids': ['finished'],
                'completion_statuses': {'finished': 'completed'}, 'goal': goal,
                'history_fingerprint': fingerprint, 'thread_status': {'type': 'idle'}}
            intent = {'task_id': identity, 'task_epoch': task['epoch'], 'harness': 'codex',
                      'thread_id': thread, 'settled': True}
            state = {'native_goal_profile': task['native_goal_profile'], 'side_effect_started': True,
                'harness': 'codex', 'codex_node': 'node', 'thread_id': thread, 'goal': goal,
                'native_lifecycle': lifecycle, 'native_execution_intent': intent}
            class Client:
                def request(self, route, body):
                    return api.dispatch('POST', route, body, peer)
            saved = self.strategy_module.sessions.save(Client(), task, 'node', 'codex', state, rollout,
                                                        expected_fingerprint=fingerprint)
            self.assertTrue(saved['artifact_saved'])
            result = 'original verified result'
            api.dispatch('POST', '/v1/task/update', {'id': identity, 'epoch': task['epoch'],
                'checkpoint': dict(state, side_effect_started=False), 'result': result, 'status': 'completed'}, peer)
            payload = {'continuation_id': 'owner-new-input', 'task_id': identity, 'expected_epoch': task['epoch'],
                'task_result_sha256': hashlib.sha256(result.encode()).hexdigest(), 'instruction': 'new authorized work'}
            with store.transaction() as db:
                native = dict(db.execute('SELECT * FROM native_sessions WHERE scope=?', (task['scope'],)).fetchone())
                db.execute('UPDATE native_sessions SET artifact=NULL WHERE scope=?', (task['scope'],))
            with self.assertRaisesRegex(ValueError, 'continuation_native_source_changed'):
                store.continue_task(payload)  # A metadata-only session is insufficient.
            stale = copy.deepcopy(state)
            stale['native_execution_intent']['task_epoch'] += 1
            with store.transaction() as db:
                db.execute('UPDATE native_sessions SET artifact=?,state=? WHERE scope=?',
                           (native['artifact'], json.dumps(stale), task['scope']))
            with self.assertRaisesRegex(ValueError, 'continuation_native_source_changed'):
                store.continue_task(payload)
            with store.transaction() as db:
                db.execute('UPDATE native_sessions SET state=? WHERE scope=?', (native['state'], task['scope']))
                final_checkpoint = db.execute('SELECT checkpoint FROM tasks WHERE id=?', (identity,)).fetchone()[0]
                missing_proof = json.loads(final_checkpoint)
                missing_proof['native_lifecycle']['settled'] = False
                db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (json.dumps(missing_proof), identity))
            with self.assertRaisesRegex(ValueError, 'continuation_native_source_changed'):
                store.continue_task(payload)
            with store.transaction() as db:
                db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (final_checkpoint, identity))
            accepted = store.continue_task(payload)
            self.assertTrue(accepted['accepted'])
            self.assertEqual(identity, accepted['task_id'])
            self.assertEqual(accepted, store.continue_task(payload))  # Original acceptance receipt only.
            with store.transaction() as db:
                self.assertTrue(json.loads(db.execute('SELECT state FROM native_sessions WHERE scope=?',
                                                     (task['scope'],)).fetchone()[0])['side_effect_started'])
                self.assertEqual('pending', db.execute('SELECT status FROM tasks WHERE id=?', (identity,)).fetchone()[0])

    def test_zero_new_turn_terminal_resume_seals_but_late_work_does_not(self):
        from tests.test_native_goal_lifecycle import NativeGoalLifecycleTests
        fixture = NativeGoalLifecycleTests()
        actual = fixture.goal('paused', createdAt=100, updatedAt=102)
        prior = {'outcome': 'yielded', 'thread_id': fixture.thread, 'settled': True, 'runtime_closed': True,
                 'goal': fixture.goal('active', createdAt=100, updatedAt=101)}
        def runtime():
            agent = fixture.runtime(requested=False, actual=actual)
            agent.__class__ = self.runtime.Codex
            agent.config.update(native_goal_drain=True, native_goal_yield=True, native_transport='unix')
            agent.transport_name, agent._native_cleanup_forced, agent._reader_forced_close = 'unix', False, False
            agent.start('must preserve owner pause', {'thread_id': fixture.thread, 'native_lifecycle': prior})
            agent.finish(timeout=1)
            return agent
        with tempfile.TemporaryDirectory() as directory:
            agent = runtime()
            rollout = Path(directory) / 'closed-original.jsonl'
            raw = (json.dumps({'type': 'session_meta', 'payload': {'id': fixture.thread}}) + '\n').encode()
            rollout.write_bytes(raw); rollout.chmod(0o600)
            agent.rollout_path = str(rollout)
            calls = len(agent.calls)
            seal = agent.seal_native_lifecycle()
            self.assertTrue(seal['zero_turn_terminal'])
            self.assertTrue(seal['settled'])
            self.assertEqual([], seal['turn_ids'])
            self.assertEqual({}, seal['completion_statuses'])
            self.assertEqual(len(raw), seal['history_fingerprint']['size'])
            self.assertEqual(calls, len(agent.calls))
            self.assertFalse(any(method in ('turn/start', 'turn/steer', 'thread/goal/set') for method, unused in agent.calls))
            api, peer = self.api(directory)
            identity = api.store.create_task('paused before wake', context={'session_scope': 'project:future-goal'})
            task = api.dispatch('POST', '/v1/claim', {}, peer)['task']
            checkpoint = {'native_goal_profile': task['native_goal_profile'], 'side_effect_started': False,
                'harness': 'codex', 'thread_id': fixture.thread, 'goal': actual, 'native_lifecycle': seal,
                'native_execution_intent': {'task_id': identity, 'task_epoch': task['epoch'], 'harness': 'codex',
                                          'thread_id': fixture.thread, 'settled': True}}
            self.assertTrue(api.dispatch('POST', '/v1/task/update', {'id': identity, 'epoch': task['epoch'],
                'checkpoint': checkpoint, 'status': 'needs_review'}, peer)['ok'])
            late = runtime()
            late.rollout_path = str(rollout)
            late.deferred = [fixture.started('late-unadmitted-turn')]
            with self.assertRaisesRegex(ValueError, 'codex_native_late_work'):
                late.seal_native_lifecycle()
            self.assertFalse(late.native_lifecycle['settled'])
            self.assertNotIn('zero_turn_terminal', late.native_lifecycle)

    def test_yielded_resume_validates_budget_and_updated_time_before_input(self):
        from tests.test_native_goal_lifecycle import NativeGoalLifecycleTests
        fixture = NativeGoalLifecycleTests()
        original = fixture.goal('active', createdAt=100, updatedAt=110, tokenBudget=1000)
        prior = {'outcome': 'yielded', 'thread_id': fixture.thread, 'settled': True,
                 'runtime_closed': True, 'goal': original}
        for changes in ({'tokenBudget': 2000}, {'updatedAt': 109}):
            with self.subTest(changes=changes):
                agent = fixture.runtime(requested=False, actual=dict(original, **changes))
                agent.__class__ = self.runtime.Codex
                with self.assertRaisesRegex(ValueError, 'codex_native_goal_continuity_unverified'):
                    agent.start('new input must not be delivered', {'thread_id': fixture.thread, 'native_lifecycle': prior})
                self.assertFalse(any(method in ('turn/steer', 'turn/start', 'thread/goal/set') for method, unused in agent.calls))
        valid = fixture.runtime(requested=False, actual=dict(original, updatedAt=111))
        valid.__class__ = self.runtime.Codex
        valid.before_reply['thread/goal/get'] = [fixture.started('same-goal-auto-turn')]
        valid.start('new owner input once', {'thread_id': fixture.thread, 'native_lifecycle': prior})
        self.assertEqual(1, sum(method == 'turn/steer' for method, unused in valid.calls))


if __name__ == '__main__':
    unittest.main()
