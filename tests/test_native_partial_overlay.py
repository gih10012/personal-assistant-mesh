"""Additive composition only: captured code/temporary files, no live runtime."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from assistant_mesh import native_partial_adapter as adapter
from assistant_mesh import native_partial_continuation as partial
from assistant_mesh.store import Store


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Methods:
    _assert_continuation_quiescent = Store._assert_continuation_quiescent


class NativePartialAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='partial-adapter-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.secret = 'synthetic-maintenance-' + 'a' * 32
        token = self.root / 'maintenance.token'; token.write_text(self.secret); token.chmod(0o600)
        self.config = {'reviewers': [{'reviewer_id': 'synthetic-owner', 'token_file': str(token)}]}
        self.calls = []
        self.original_dispatch = lambda method, path, payload, peer: self.calls.append((method, path, payload, peer)) or {'original': path}
        self.api = SimpleNamespace(store=Store(self.root / 'ledger.db'), peers=[('old-' + 'b' * 32, {'role': 'operator'})],
                                   dispatch=self.original_dispatch, original_callback=object())

    def test_preserves_original_class_constructor_dispatch_and_requires_explicit_current_coverage(self):
        original_type = type(self.api.store)
        original_constructor = original_type.__init__
        original_callback = self.api.original_callback
        result = adapter.attach(self.api, self.config, Methods)
        self.assertIs(original_type, type(self.api.store))
        self.assertIs(original_constructor, type(self.api.store).__init__)
        self.assertIs(original_callback, self.api.original_callback)
        self.assertFalse(result['coverage_refreshed'])
        self.assertEqual({'original': '/old/phone'}, self.api.dispatch('POST', '/old/phone', {'original': True}, {'role': 'worker'}))
        owner = self.api.peers[-1][1]
        self.assertEqual('native_maintenance', owner['role'])
        self.assertEqual(2, len(self.api.peers))
        self.assertEqual(result, adapter.attach(self.api, self.config, Methods))
        self.assertEqual(2, len(self.api.peers))
        for status in (None, {'manifest': {'tables': {}}, 'coverage_issues': []},
                       {'manifest': {'tables': dict.fromkeys(adapter.REQUIRED_COVERAGE)}, 'coverage_issues': ['configured_coverage_refresh_required'],
                        'ledger_coverage': 'degraded'}):
            if status is None:
                self.api._business_journal = None
            else:
                self.api._business_journal = SimpleNamespace(status=lambda: status)
            with self.assertRaisesRegex(ValueError, 'coverage_refresh_required'):
                self.api.dispatch('POST', partial.PREFIX + 'history', {}, owner)
        self.api._business_journal = SimpleNamespace(status=lambda: {'manifest': {'tables': dict.fromkeys(adapter.REQUIRED_COVERAGE)},
            'coverage_issues': [], 'ledger_coverage': 'declared_tables_current'})
        adapter.require_coverage(self.api)
        with self.assertRaises(partial.Conflict):  # Now reaches shape validation, not a coverage bypass.
            self.api.dispatch('POST', partial.PREFIX + 'history', {}, owner)

    def test_maintenance_is_not_global_management_and_never_uses_shared_role_token(self):
        self.api.peers.append((self.secret, {'role': 'worker', 'node': 'synthetic-node'}))
        with self.assertRaisesRegex(ValueError, 'ambiguous_native_maintenance'):
            adapter.attach(self.api, self.config, Methods)
        self.assertIs(self.original_dispatch, self.api.dispatch)
        self.api.peers.pop(); adapter.attach(self.api, self.config, Methods)
        owner = self.api.peers[-1][1]
        for method, path in (('GET', '/v1/status'), ('POST', '/v1/task/control'),
                             ('POST', partial.PREFIX + 'inspect?alternate=true')):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                self.api.dispatch(method, path, {}, owner)
        with self.assertRaises(PermissionError):
            self.api.dispatch('POST', partial.PREFIX + 'accept', {'completed': True}, {'role': 'operator'})

    def test_retirement_resolver_fetches_original_receipt_instead_of_caller_supplied_body(self):
        calls = []
        client = SimpleNamespace(request=lambda path, body: calls.append((path, body)) or {
            'protocol': partial.PROTOCOL, 'acceptance_id': body['acceptance_id'], 'accepted': True})
        resolve = adapter.receipt_resolver(client)
        self.assertTrue(resolve('original-acceptance')['accepted'])
        self.assertEqual([(partial.PREFIX + 'receipt', {'acceptance_id': 'original-acceptance'})], calls)

    def test_captured_old_store_gets_only_exact_helper_and_original_conflict_type(self):
        source = Path('/var/tmp/amita-business-replica-20261029/snapshot/node/store.py')
        if not source.is_file():
            self.skipTest('captured actual Store source unavailable')
        old = load('_native_partial_captured_old_store', source)
        original = {name: getattr(old.Store, name) for name in ('__init__', 'transaction', 'claim', 'update_task', 'continue_task')}
        self.assertFalse(hasattr(old.Store, '_assert_continuation_quiescent'))
        adapter.install_store_type(old.Store, Methods)
        for name, function in original.items():
            self.assertIs(function, getattr(old.Store, name), name)
        store = old.Store(self.root / 'captured-store-fixture.db', recover_inflight=False)
        with store.transaction() as db:
            for identity, checkpoint in (('original', {'side_effect_started': True}),
                                          ('same-resource-unknown', {'side_effect_started': False,
                                           'runtime_failure': {'native_start_attempted': True, 'outcome': 'unknown'}})):
                db.execute('INSERT INTO tasks(id,input,required,status,checkpoint,created,context,scope) VALUES(?,?,?,?,?,?,?,?)',
                           (identity, 'synthetic', '[]', 'needs_review', json.dumps(checkpoint), 0, '{}', 'synthetic-shared-scope'))
            row = db.execute("SELECT * FROM tasks WHERE id='original'").fetchone()
            with self.assertRaises(old.Conflict):
                store._assert_continuation_quiescent(db, row)


class NativePartialOverlayBuilderTests(unittest.TestCase):
    def setUp(self):
        self.builder = load('_native_partial_overlay_builder', ROOT / 'deploy/build-native-partial-overlay.py')
        self.temp = tempfile.TemporaryDirectory(prefix='partial-overlay-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.namespace = self.root / '__init__.py'
        self.namespace.write_text('__path__.append(' + repr(str(ROOT / 'assistant_mesh')) + ')\n')
        self.launcher = self.root / 'old_receiver.py'
        self.launcher.write_text('import types\n_entry = types.SimpleNamespace()\n')
        self.incumbent_store = self.root / 'store.py'; self.incumbent_store.write_bytes((ROOT / 'assistant_mesh/store.py').read_bytes())

    @staticmethod
    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def arguments(self):
        return dict(package_init=self.namespace, init_sha256=self.sha(self.namespace), installed_init=str(self.namespace),
                    launcher=self.launcher, launcher_sha256=self.sha(self.launcher), installed_launcher=str(self.launcher))

    def test_receiver_contains_only_additive_modules_and_binds_actual_captured_sources(self):
        snapshot = Path('/var/tmp/amita-business-replica-20261029')
        actual_launcher = snapshot / 'receiver-source-v3/launch_receiver.py'
        actual_init = snapshot / 'receiver-source-v3/assistant_mesh/__init__.py'
        actual_store = snapshot / 'snapshot/node/store.py'
        args = self.arguments()
        if actual_launcher.is_file() and actual_init.is_file() and actual_store.is_file():
            args.update(package_init=actual_init, init_sha256=self.sha(actual_init), launcher=actual_launcher,
                        launcher_sha256=self.sha(actual_launcher))
            self.incumbent_store = actual_store
        report = self.builder.build('receiver', self.root / 'new-receiver', **args,
            incumbent_store=self.incumbent_store, store_sha256=self.sha(self.incumbent_store),
            installed_store='/synthetic/incumbent/assistant_mesh/store.py', entry_attribute='_entry',
            maintenance_config='/synthetic/private/maintenance.json')
        paths = set(report['source_sha256'])
        self.assertFalse(paths.intersection('assistant_mesh/' + name + '.py' for name in ('store', 'server', 'worker', 'codex', 'entrance', 'sessions')))
        self.assertFalse(report['ledger_opened']); self.assertFalse(report['incumbent_files_modified'])
        self.assertEqual(list(adapter.GLOBAL_TABLES), report['required_global_tables'])
        body = (self.root / 'new-receiver/launch_receiver.py').read_text()
        self.assertIn('_previous_ready(http_server, api, channel)', body)
        self.assertIn("['_entry']", body)
        self.assertIn('install_schema(api.store, PartialContinuationMethods)', body)
        self.assertNotIn('.refresh(', body)
        methods = load('assistant_mesh._synthetic_native_partial_methods',
                       self.root / 'new-receiver/assistant_mesh/native_partial_methods.py')
        self.assertIs(Store, adapter.install_store_type(Store, methods.PartialContinuationMethods))

    def test_generated_ready_preserves_original_callback_result_before_wrapping_dispatch(self):
        candidate = self.root / 'ready-overlay'
        self.builder.build('receiver', candidate, **self.arguments(),
            incumbent_store=self.incumbent_store, store_sha256=self.sha(self.incumbent_store),
            installed_store=str(self.incumbent_store), entry_attribute='_entry',
            maintenance_config='/synthetic/private/maintenance.json')
        raw = (candidate / 'launch_receiver.py').read_text()
        function = next(item for item in ast.parse(raw).body if isinstance(item, ast.FunctionDef) and item.name == '_ready')
        token = self.root / 'reviewer.token'; token.write_text('synthetic-maintenance-' + 'c' * 32); token.chmod(0o600)
        config = {'reviewers': [{'reviewer_id': 'fixture-owner', 'token_file': str(token)}]}
        events = []
        original_dispatch = lambda *args: {'incumbent_dispatch': True}
        api = SimpleNamespace(store=Store(self.root / 'ready.db'), peers=[], dispatch=original_dispatch)
        def previous(http_server, api, channel):
            events.append((http_server, channel)); api.phone_callback_preserved = True
            return 'ORIGINAL_READY_RESULT'
        def read_private(path):
            self.assertEqual(1, len(events)); return config
        namespace = {'install_schema': adapter.install_schema, 'attach': adapter.attach,
                     'PartialContinuationMethods': Methods, 'private_json': read_private, '_previous_ready': previous}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<synthetic-ready-overlay>', 'exec'), namespace)
        self.assertEqual('ORIGINAL_READY_RESULT', namespace['_ready']('original-http-server', api, 'original-channel'))
        self.assertEqual([('original-http-server', 'original-channel')], events)
        self.assertTrue(api.phone_callback_preserved)
        self.assertEqual({'incumbent_dispatch': True}, api.dispatch('POST', '/old/entry', {}, {'role': 'worker'}))
        self.assertIs(original_dispatch, api._native_partial_maintenance['previous_dispatch'])

    def test_worker_guard_is_exact_addition_to_explicit_modern_codex_and_legacy_is_rejected(self):
        codex = self.root / 'codex.py'
        modern = ("class Codex:\n    def _read_goal_snapshot(self, checkpoint=None):\n"
                  "        response = {'goal': None}\n        self._validate_goal_snapshot(response['goal'])\n"
                  "        goal = response['goal']\n        self._consume_prior_notifications()\n        return goal\n")
        codex.write_text(modern)
        report = self.builder.build('worker', self.root / 'new-worker', **self.arguments(),
                                    codex=codex, codex_sha256=self.sha(codex), installed_codex=str(codex))
        patched = (self.root / 'new-worker/assistant_mesh/codex.py').read_text()
        self.assertEqual(modern, patched.replace('        from .native_partial_continuation import validate_resume_goal\n        validate_resume_goal(checkpoint, goal)\n', '', 1))
        self.assertFalse(report['model_profile_modified'])
        codex.write_text('class Codex:\n    def start(self):\n        return None\n')
        with self.assertRaisesRegex(ValueError, 'selected_modern_goal_namespace_required'):
            self.builder.build('worker', self.root / 'rejected-legacy-worker', **self.arguments(),
                               codex=codex, codex_sha256=self.sha(codex), installed_codex=str(codex))
        self.assertFalse((self.root / 'rejected-legacy-worker').exists())

    def test_source_pin_failure_never_imports_incumbent_or_writes_candidate(self):
        self.launcher.write_text("raise RuntimeError('THIS INCUMBENT MUST NEVER EXECUTE WHILE BUILDING')\n")
        args = self.arguments(); args['launcher_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'source_pin_changed'):
            self.builder.build('receiver', self.root / 'rejected-source', **args,
                incumbent_store=self.incumbent_store, store_sha256=self.sha(self.incumbent_store),
                installed_store=str(self.incumbent_store), entry_attribute='_entry',
                maintenance_config='/synthetic/private/config.json')
        self.assertFalse((self.root / 'rejected-source').exists())
