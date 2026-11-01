"""Pinned worker assembly and synthetic native protocol; no model/account IO."""
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from assistant_mesh import native_worker_release as release
from assistant_mesh.reasoning import make_codex
from assistant_mesh.task_scoped_profile import TaskScopedProfile
from tests import test_native_goal_lifecycle as lifecycle_fixtures


ROOT = Path(__file__).resolve().parents[1]
PHONE = 'leader:project:amita-harmony-onboarding'
S2 = 'leader:project:amita-control-quorum'
OLD_ROOT = Path('/home/gihggiy/.local/share/amita/worker-releases/20261028.1')
OLD_PATHS = [OLD_ROOT / 'assistant_mesh', Path('/home/gihggiy/.local/share/amita/worker-releases/20261026.2/assistant_mesh'),
             Path('/home/gihggiy/.local/share/personal-assistant-mesh-tools/leader-governance-20261021/runtime/assistant_mesh'),
             Path('/home/gihggiy/.local/share/personal-assistant-mesh-runtime/3198c877234a8fd0c6912fdbe218f47185effedc/assistant_mesh')]


def load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, str(path), submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    return module


class NativeWorkerProfileTests(unittest.TestCase):
    def test_original_phone_binding_and_additional_profile_feed_actual_native_fields(self):
        loaded = []
        files = {'/fixture/s2-profile.json': {'model': 'fixture-sol', 'reasoning_effort': 'high', 'revision': 1}}
        def read(path):
            loaded.append(path); return copy.deepcopy(files[path])
        factory = release.profile_factory(TaskScopedProfile, read, [{'scope': S2, 'profile_path': '/fixture/s2-profile.json'}])
        original_phone = mock.Mock(return_value={'model': 'original-phone', 'reasoning_effort': 'high'})
        profile = factory(original_phone, (PHONE,))
        worker = SimpleNamespace(current=None); profile.bind(worker)
        self.assertIsNone(profile(None)); self.assertEqual([], loaded)
        worker.current = {'scope': PHONE}; self.assertEqual('original-phone', profile(None)['model'])
        original_phone.assert_called_once()
        worker.current = {'scope': S2}
        calls, receipts = [], []
        class Native:
            provider = None
            config = {}
            model = 'old-default'
            def rpc(self, method, params, timeout=30):
                calls.append((method, copy.deepcopy(params)))
                if method == 'model/list':
                    return {'data': [{'id': name, 'model': name, 'supportedReasoningEfforts': [{'reasoningEffort': effort}]}
                                     for name, effort in [('fixture-sol', 'high'), ('future-qualified', 'xhigh')]]}
                if method == 'thread/resume':
                    return {'model': params['model'], 'modelProvider': 'openai',
                            'reasoningEffort': params['config']['model_reasoning_effort'], 'thread': {'id': params['threadId']}}
                return {'turn': {'id': 'new-explicit-turn'}}
        agent = make_codex(Native, profile, lambda agent, receipt: receipts.append(receipt), thread_profile=True)()
        for model, effort in [('fixture-sol', 'high'), ('future-qualified', 'xhigh')]:
            files['/fixture/s2-profile.json'].update(model=model, reasoning_effort=effort)
            agent.rpc('thread/resume', {'threadId': 'original-thread', 'excludeTurns': True})
            self.assertEqual((model, effort), (calls[-1][1]['model'], calls[-1][1]['config']['model_reasoning_effort']))
            self.assertTrue(receipts[-1]['native_configuration_verified'])
            agent.rpc('turn/start', {'threadId': 'original-thread', 'input': [{'type': 'text', 'text': 'ONLY remaining work'}],
                                     'collaborationMode': {'mode': 'default', 'settings': {'model': 'old-default'}}})
            sent = calls[-1][1]
            self.assertEqual((model, effort, effort), (sent['model'], sent['effort'], sent['collaborationMode']['settings']['reasoning_effort']))
            self.assertFalse(receipts[-1]['effective_verified'])
        worker.current = {'scope': 'unrelated-owner-scope'}
        self.assertIsNone(profile(None))

    def test_modern_claimed_execution_keeps_original_config_callbacks_and_one_claim(self):
        timeline = []
        task = {'id': 'original-task', 'epoch': 2, 'scope': S2, 'checkpoint': {}}
        config = {'codex': {'auth_home': '/fixture/original-auth', 'workspace': '/fixture/original-workspace'},
                  'codex_accounts': [{'auth_home': '/fixture/original-auth'}]}
        class Original:
            def __init__(self, config):
                self.config = config; self.client = SimpleNamespace(request=lambda path, body: timeline.append(path) or {'task': task})
                self.agent = None; self.current = None; self.wait_children = False
            def open_backend(self):
                timeline.append(copy.deepcopy(self.config)); return 'original backend constructor'
            def on_tool(self, call):
                return {'contentItems': [{'text': '{"continue_after_children":false}'}]}
            def on_interaction(self, *args):
                return 'original callback'
        strategy = SimpleNamespace(run_claimed=lambda worker, claimed: timeline.append(claimed) or True,
            on_activity=lambda *args: None, tick=lambda *args, **kwargs: timeline.append('tick'),
            _native_yield_tick=lambda *args, **kwargs: None, _checked_native_yield_seal=lambda *args: None)
        worker = release.make_worker(Original, strategy)(config)
        self.assertTrue(worker.run_once())
        self.assertEqual(1, timeline.count('/v1/claim'))
        self.assertIs(task, timeline[-1])
        self.assertEqual('original backend constructor', worker.open_backend())
        selected = timeline[-1]
        self.assertEqual('/fixture/original-auth', selected['codex']['auth_home'])
        self.assertEqual('unix', selected['codex']['native_transport'])
        self.assertIs(config, worker.config)
        self.assertNotIn('native_transport', config['codex'])
        worker.current = task; worker.on_tool({'tool': 'mesh_wait_children'})
        self.assertFalse(worker.wait_children)
        self.assertEqual('original callback', worker.on_interaction())


class NativeWorkerReleaseSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='native-worker-release-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.builder = load('_native_worker_release_builder', ROOT / 'deploy/build-native-worker-release.py')
        launcher, init = OLD_ROOT / 'launch_worker.py', OLD_ROOT / 'assistant_mesh/__init__.py'
        if not launcher.is_file() or not init.is_file():
            raise unittest.SkipTest('captured original launcher sources unavailable')
        manifest = {}
        for name in cls.builder.INCUMBENT_MODULES:
            path = next((root / (name + '.py') for root in OLD_PATHS if (root / (name + '.py')).is_file()), None)
            if path is None:
                raise unittest.SkipTest('captured original worker module unavailable: ' + name)
            manifest[name] = {'snapshot': str(path), 'installed': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        source_manifest = cls.root / 'incumbent-sources.json'; source_manifest.write_text(json.dumps(manifest))
        cls.report = cls.builder.build(cls.root / 'release', launcher=launcher,
            launcher_sha256=hashlib.sha256(launcher.read_bytes()).hexdigest(), installed_launcher=str(launcher),
            package_init=init, init_sha256=hashlib.sha256(init.read_bytes()).hexdigest(), installed_init=str(init),
            incumbent_manifest=source_manifest, policy_file='/fixture/private/release-policy.json')
        cls.package = '_synthetic_native_worker_release'
        load(cls.package, cls.root / 'release/assistant_mesh/__init__.py', True)
        cls.codex = importlib.import_module(cls.package + '.native_worker_runtime.codex')
        cls.addClassCleanup(lambda: [sys.modules.pop(name, None) for name in list(sys.modules)
                                    if name == cls.package or name.startswith(cls.package + '.')])

    def test_source_imports_keep_authority_absent_and_never_open_account_or_model(self):
        self.assertFalse(self.report['model_called'])
        self.assertFalse(self.report['authority_store_api_deployed'])
        forbidden = {'assistant_mesh/' + name + '.py' for name in ('store', 'server', 'worker', 'codex', 'governance_worker', 'metadata_diagnostics')}
        self.assertFalse(forbidden.intersection(self.report['source_sha256']))
        code = """
import sqlite3,subprocess,runpy,sys
sqlite3.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('source import opens ledger'))
subprocess.Popen=lambda *a,**k: (_ for _ in ()).throw(AssertionError('source import starts native'))
runpy.run_path('launch_worker.py',run_name='source_import_only')
assert 'assistant_mesh.store' not in sys.modules
assert 'assistant_mesh.server' not in sys.modules
print('source-only import; no account/model/authority')
"""
        result = subprocess.run([sys.executable, '-S', '-B', '-c', code], cwd=str(self.root / 'release'),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def runtime(self, requested=True, actual=None):
        fixture = lifecycle_fixtures.NativeGoalLifecycleTests()
        agent = fixture.runtime(requested=requested, actual=actual)
        agent.__class__ = self.codex.Codex
        return agent, fixture

    def test_same_thread_goal_none_reads_before_new_turn_and_never_creates_config_goal(self):
        agent, fixture = self.runtime(requested=True, actual=None)
        accepted = {'protocol': 'owner-native-partial-continuation/1', 'disposition': 'continue_remaining',
                    'source_thread_id': fixture.thread, 'source_turn_id': 'original-failed-turn',
                    'goal_sha256': hashlib.sha256(b'null').hexdigest()}
        state = agent.start('Continue ONLY remaining source work; existing worktree is already created.',
                            {'thread_id': fixture.thread, 'turn_id': 'original-failed-turn', 'goal': None,
                             'owner_partial_continuation': accepted})
        methods = [name for name, body in agent.calls]
        self.assertNotIn('thread/start', methods); self.assertNotIn('thread/goal/set', methods)
        self.assertLess(methods.index('thread/resume'), methods.index('thread/goal/get'))
        self.assertLess(methods.index('thread/goal/get'), methods.index('turn/start'))
        self.assertEqual(fixture.thread, state['thread_id'])
        self.assertFalse(agent.native_lifecycle['goal_managed'])

    def test_sealed_plain_turn_releases_original_rolling_gate_after_final_ack(self):
        agent, fixture = self.runtime(requested=False, actual=None)
        agent.config['native_goal_drain'] = True
        agent.start('Complete the synthetic source-only task')
        turn = agent.turn_id
        agent.deferred = [fixture.started(turn), fixture.message(turn, 'source work completed'), fixture.completed(turn)]
        agent.finish(timeout=1)
        rollout = self.root / 'plain-terminal-boundary.jsonl'
        records = [{'type': 'session_meta', 'payload': {'id': fixture.thread}},
                   {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn}},
                   {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn}}]
        rollout.write_text(''.join(json.dumps(row) + '\n' for row in records))
        rollout.chmod(0o600)
        agent.rollout_path = str(rollout)
        sealed = agent.seal_native_lifecycle()
        self.assertTrue(sealed['settled'])
        self.assertTrue(sealed['runtime_closed'])
        rolling = load(self.package + '.original_rolling_gate', OLD_PATHS[1] / 'rolling_worker.py')
        task = {'id': 'synthetic-completed-task', 'epoch': 2, 'checkpoint': {
            'thread_id': fixture.thread, 'turn_id': turn, 'side_effect_started': True,
            'runtime_failure': {'attempt_epoch': 1, 'native_start_attempted': True}}}
        requests, local = [], []

        def request(route, body):
            requests.append((route, copy.deepcopy(body)))
            return {'task': copy.deepcopy(task)} if route == '/v1/claim' else {'ok': True}

        def control(action, body=None):
            local.append((action, copy.deepcopy(body)))
            return {'admitted': True} if action == 'admit' else {'ok': True}

        gate = rolling.ClaimGate(SimpleNamespace(request=request), SimpleNamespace(call=control))
        claimed = gate.request('/v1/claim', {})['task']
        gate.request('/v1/task/update', {'id': claimed['id'], 'epoch': claimed['epoch'],
            'checkpoint': {'side_effect_started': False, 'native_lifecycle': sealed, 'goal': None},
            'status': 'completed'})
        gate.finish_boundary(SimpleNamespace(current=None, agent=None,
            _native_starting=False, _native_turn_live=None))
        self.assertFalse(gate.unknown)
        self.assertIsNone(gate.task)
        self.assertEqual([('checkpoint', {'task': {'id': task['id'], 'epoch': 2}, 'safe': True})],
                         [row for row in local if row[0] == 'checkpoint'])
        self.assertEqual(1, sum(route == '/v1/claim' for route, body in requests))

    def test_plain_turn_with_incomplete_history_never_publishes_terminal_boundary(self):
        agent, fixture = self.runtime(requested=False, actual=None)
        agent.config['native_goal_drain'] = True
        agent.start('Complete the synthetic source-only task')
        turn = agent.turn_id
        agent.deferred = [fixture.started(turn), fixture.message(turn, 'answer'), fixture.completed(turn)]
        agent.finish(timeout=1)
        rollout = self.root / 'incomplete-terminal-boundary.jsonl'
        records = [{'type': 'session_meta', 'payload': {'id': fixture.thread}},
                   {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn}}]
        rollout.write_text(''.join(json.dumps(row) + '\n' for row in records))
        rollout.chmod(0o600)
        agent.rollout_path = str(rollout)
        with self.assertRaisesRegex(ValueError, 'native_history_turn_unsettled'):
            agent.seal_native_lifecycle()
        self.assertFalse(agent.native_lifecycle['settled'])
        self.assertNotEqual('terminal', agent.native_lifecycle.get('outcome'))

    def test_active_goal_refuses_terminal_seal_before_runtime_close(self):
        agent, fixture = self.runtime(requested=False, actual=None)
        agent.config['native_goal_drain'] = True
        agent.start('Complete the synthetic source-only task')
        turn = agent.turn_id
        agent.deferred = [fixture.started(turn), fixture.completed(turn)]
        agent.finish(timeout=1)
        agent.native_lifecycle['goal'] = fixture.goal('active')
        with self.assertRaisesRegex(ValueError, 'codex_native_not_quiescent'):
            agent.seal_native_lifecycle()
        agent.close.assert_not_called()
        self.assertFalse(agent.native_lifecycle['settled'])
        self.assertNotEqual('terminal', agent.native_lifecycle.get('outcome'))

    def test_mismatched_real_goal_never_releases_resumed_host_requests_or_starts_turn(self):
        fixture = lifecycle_fixtures.NativeGoalLifecycleTests()
        agent, _ = self.runtime(requested=False, actual=fixture.goal())
        agent.on_tool = mock.Mock()
        original_rpc = agent.rpc
        def rpc(method, params, **kwargs):
            if method == 'thread/resume':
                self.assertTrue(agent._hold_host_requests)
                agent._held_host_requests.append({'id': 9, 'method': 'item/tool/call', 'params': {'threadId': fixture.thread}})
            return original_rpc(method, params, **kwargs)
        agent.rpc = rpc
        with self.assertRaises(ValueError):
            agent.start('Only remaining work', {'thread_id': fixture.thread, 'turn_id': 'original-failed-turn',
                'owner_partial_continuation': {'protocol': 'owner-native-partial-continuation/1',
                    'disposition': 'continue_remaining', 'source_thread_id': fixture.thread,
                    'source_turn_id': 'original-failed-turn', 'goal_sha256': hashlib.sha256(b'null').hexdigest()}})
        self.assertFalse(any(name in ('turn/start', 'thread/goal/set', 'turn/steer') for name, body in agent.calls))
        agent.on_tool.assert_not_called()
