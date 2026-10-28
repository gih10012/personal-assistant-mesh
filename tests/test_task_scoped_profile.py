"""Three local scope/binding fixtures; no models, runtime state or credentials."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from assistant_mesh.config import private_json
from assistant_mesh.reasoning import make_codex
from assistant_mesh.task_scoped_profile import TaskScopedProfile


ROOT = Path(__file__).resolve().parents[1]
PHONE = 'leader:project:amita-harmony-onboarding'
LAUNCHER = Path('/home/gihggiy/.local/share/amita/worker-releases/20261026.2/launch_worker.py')
NAMESPACE = LAUNCHER.parent / 'assistant_mesh/__init__.py'
LAUNCHER_SHA = 'ab0f67b3b3df1299dee270c6becfaf56c50308756fd6202403c962ebf81e0006'
NAMESPACE_SHA = '2184d58ec06e36619cc9b2258bc1324c5f1e08b1a268b5cbf292771a707d70e4'
WORKSPACE_SCOPES = ('leader:project:personal-assistant-workspace',
    'project:personal-assistant-workspace:workspace-backend',
    'project:personal-assistant-workspace:workspace-frontend',
    'project:personal-assistant-workspace:workspace-skill')


class TaskScopedProfileTests(unittest.TestCase):
    def test_unbound_idle_workspace_owner_and_other_scopes_never_load_phone_profile(self):
        loader = Mock(return_value={'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'})
        profile = TaskScopedProfile(loader, (PHONE,))
        self.assertIsNone(profile(None))
        worker = types.SimpleNamespace(current=None)
        self.assertIs(worker, profile.bind(worker))
        for scope in (None,) + WORKSPACE_SCOPES + ('leader:owner', PHONE + ':other'):
            worker.current = {'scope': scope} if scope else None
            self.assertIsNone(profile(object()))
        loader.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'binding_changed'):
            profile.bind(types.SimpleNamespace(current={'scope': PHONE}))

    def test_phone_private_profile_updates_feed_existing_catalog_and_receipts_without_default_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'owner-profile.json'
            def write(model, effort, revision):
                path.write_text(json.dumps({'model': model, 'reasoning_effort': effort, 'revision': revision}))
                path.chmod(0o600)
            write('gpt-6.1-sol', 'high', 1)
            profile = TaskScopedProfile(lambda agent: private_json(str(path)), (PHONE,))
            worker = types.SimpleNamespace(current={'scope': PHONE})
            profile.bind(worker)
            path.write_text(json.dumps({'model': 'gpt-6.1-sol', 'reasoning_effort': None}))
            with self.assertRaisesRegex(ValueError, 'explicit_selection_required'):
                profile(None)
            write('gpt-6.1-sol', 'high', 1)
            calls, receipts = [], []
            class NativeFixture:
                model = 'original-default'
                config = {'model': 'original-default'}
                provider = None
                def rpc(self, method, params, timeout=30):
                    calls.append((method, params))
                    if method == 'model/list':
                        return {'data': [{'id': name, 'model': name, 'supportedReasoningEfforts': [
                            {'reasoningEffort': effort}]} for name, effort in
                            [('gpt-6.1-sol', 'high'), ('future-qualified-model', 'xhigh')]]}
                    return {'turn': {'id': 'fixture-turn-' + str(len(calls))}}
            agent = make_codex(NativeFixture, profile, lambda agent, receipt: receipts.append(receipt))()
            for model, effort, revision in [('gpt-6.1-sol', 'high', 1), ('future-qualified-model', 'xhigh', 2)]:
                write(model, effort, revision)
                agent.rpc('turn/start', {'threadId': 'original-thread', 'input': []})
                self.assertEqual((model, effort), (calls[-1][1]['model'], calls[-1][1]['effort']))
                self.assertEqual(('accepted', revision, False),
                    (receipts[-1]['status'], receipts[-1]['profile_revision'], receipts[-1]['effective_verified']))
            worker.current = {'scope': 'leader:owner'}
            count = len(calls)
            params = {'threadId': 'original-owner-thread', 'input': []}
            agent.rpc('turn/start', params)
            self.assertEqual(count + 1, len(calls))
            self.assertEqual(params, calls[-1][1])
            self.assertIsNone(agent.reasoning_selection)

    @unittest.skipUnless(LAUNCHER.is_file() and NAMESPACE.is_file(), 'actual PC launcher source required')
    def test_actual_launcher_preserves_governance_receipts_metadata_and_binds_both_factories(self):
        spec = importlib.util.spec_from_file_location('_scoped_worker_builder',
            str(ROOT / 'deploy/build-task-scoped-worker-launcher.py'))
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / 'candidate'
            report = builder.build(LAUNCHER, NAMESPACE, candidate, launcher_sha256=LAUNCHER_SHA,
                namespace_sha256=NAMESPACE_SHA, installed_root='/fixture/20261028.1',
                profile_path='/fixture/private-phone-profile.json')
            self.assertFalse(report['governance_scopes_changed'])
            self.assertFalse(report['effective_model_effort_verified'])
            self.assertEqual({'launch_worker.py', 'assistant_mesh/__init__.py',
                              'assistant_mesh/task_scoped_profile.py'}, set(report['source_sha256']))
            source = (candidate / 'launch_worker.py').read_text()
            tree = ast.parse(source)
            scopes = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'SCOPES' for target in node.targets))
            self.assertEqual(WORKSPACE_SCOPES, scopes)
            namespace = (candidate / 'assistant_mesh/__init__.py').read_bytes()
            self.assertTrue(namespace.endswith(NAMESPACE.read_bytes()))
            observed, loaders, configured = [], [], []
            def read_private(path):
                if path == '/fixture/worker.json':
                    return {'codex': {'instructions': 'original instructions'}}
                if path.endswith('/policy.json'):
                    return {}
                self.assertEqual('/fixture/private-phone-profile.json', path)
                return {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'revision': 1}
            class WorkerFixture:
                def __init__(self, config, **options):
                    configured.append((config, options))
                    self.current = None
                def run_once(self):
                    self.assert_bound()
                def assert_bound(self):
                    profile = loaders[-1]
                    self.current = {'scope': PHONE}
                    observed.append((profile.worker is self, profile(None)))
            def make_worker(worker, codex, governance, **options):
                self.assertEqual(WORKSPACE_SCOPES, options['scopes'])
                self.assertTrue(callable(options['receipt_callback']))
                loaders.append(options['profile_loader'])
                return WorkerFixture
            def run_worker(factory):
                worker = factory()
                worker.assert_bound()
                return 0
            def module(name, **fields):
                value = types.ModuleType(name)
                value.__dict__.update(fields)
                return value
            modules = {'assistant_mesh.' + name: module('assistant_mesh.' + name, **fields)
                for name, fields in {
                    'config': {'private_json': read_private}, 'codex': {'Codex': object, 'DEFAULT_INSTRUCTIONS': ''},
                    'worker': {'Worker': WorkerFixture}, 'governance_worker': {'make_worker': make_worker},
                    'leader_governance': {'Governance': lambda *args: object()},
                    'task_scoped_profile': {'TaskScopedProfile': TaskScopedProfile}, 'sessions': {},
                    'metadata_diagnostics': {'MetadataDiagnostics': lambda *args, **kwargs: object(),
                        'install_save_observer': Mock(), 'observe_worker': lambda cls, diagnostics: cls},
                    'rolling_worker': {'run_worker': run_worker}}.items()}
            package = module('assistant_mesh', sessions=modules['assistant_mesh.sessions'])
            package.__path__ = []
            modules['assistant_mesh'] = package
            with patch.dict(sys.modules, modules), patch.object(sys, 'path', list(sys.path)):
                launch = {'__name__': '_synthetic_scoped_launcher'}
                exec(compile(source, '<generated-scoped-launcher>', 'exec'), launch)
                for once in (True, False):
                    argv = ['launch_worker.py', '--config', '/fixture/worker.json'] + (['--once'] if once else [])
                    with patch.object(sys, 'argv', argv), patch.dict(os.environ, {'AMITA_RUNTIME_SOCKET': '/fixture/socket'}):
                        launch['main']()
            self.assertEqual(2, len(observed))
            self.assertTrue(all(bound and selection['reasoning_effort'] == 'high' for bound, selection in observed))
            self.assertTrue(all(options['config_path'] == '/fixture/worker.json' for config, options in configured))


if __name__ == '__main__':
    unittest.main()
