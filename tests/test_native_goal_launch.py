"""Only new launcher seams; use an existing built source artifact when supplied.

No listener, native process/model, running ledger or original business case.
"""
import importlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests import test_native_goal_scoped as scoped_fixture

ROOT, load = scoped_fixture.ROOT, scoped_fixture.load


class NativeGoalLaunchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (os.environ.get('MESH_NATIVE_GOAL_TEST_SOURCE')
                  or sys._xoptions.get('mesh_native_goal_test_source'))
        if source:
            cls.target = Path(source)
            cls.package_name = '_fixture_native_goal_launch'
            cls.package = load(cls.package_name, cls.target / 'assistant_mesh/__init__.py', True)
            cls.scope = importlib.import_module(cls.package_name + '.native_goal_scoped')
        else:
            # CI may reuse the existing historical fixture builder. The owner
            # handoff supplies its already-built source, so it builds nothing.
            scoped_fixture.NativeGoalScopedTests.setUpClass.__func__(cls)
        cls.package.__path__.append(str(ROOT / 'assistant_mesh'))
        cls.launch = load(cls.package_name + '.native_goal_launch',
                          ROOT / 'assistant_mesh/native_goal_launch.py')
        cls.entrance = importlib.import_module(cls.package_name + '.entrance_runtime')
        cls.server = importlib.import_module(cls.package_name + '.server')
        cls.owner = importlib.import_module(cls.package_name + '.legacy_channel_owner')

    def policy(self, **changes):
        value = {'format': self.scope.FORMAT, 'enabled': True, 'revision': 1,
            'authority': 'synthetic-authority', 'owner_id': 'synthetic-owner',
            'scopes': ['project:future-goal'], 'executors': ['node'], 'activated_at': 999}
        value.update(changes)
        return value

    def test_default_and_nonexecutor_keep_original_profiled_entrance(self):
        original = self.entrance.EntranceWorker
        legacy = mock.Mock(return_value=original)
        with mock.patch.object(self.launch, 'assemble_worker') as assembly:
            self.assertIsNone(self.launch.load_policy())
            self.assertIs(original, self.launch.compose_worker(original, object, legacy))
            self.assertIs(original, self.launch.compose_worker(original, object, legacy,
                policy=self.policy(enabled=False), node='node'))
            self.assertIs(original, self.launch.compose_worker(original, object, legacy,
                policy=self.policy(), node='existing-claw'))
            self.assertEqual(3, legacy.call_count)
            assembly.assert_not_called()
        legacy.reset_mock()
        with self.assertRaisesRegex(ValueError, 'existing_governance_required'):
            self.launch.compose_worker(original, object, legacy,
                policy=self.policy(), node='node')
        legacy.assert_not_called()

    def test_selected_worker_keeps_closed_failure_and_outer_dual_save_observers(self):
        governance = importlib.import_module(self.package_name + '.governance_worker')
        metadata = importlib.import_module(self.package_name + '.metadata_diagnostics')
        sessions = importlib.import_module(self.package_name + '.sessions')
        goal_sessions = importlib.import_module(self.package_name + '.native_goal_runtime.sessions')
        codex = importlib.import_module(self.package_name + '.codex')
        for module in (sessions, goal_sessions):
            self.addCleanup(setattr, module, 'save', module.save)
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = metadata.MetadataDiagnostics(Path(directory) / 'metadata.jsonl', enabled=True)
            legacy = mock.Mock()
            cls = self.launch.compose_worker(self.entrance.EntranceWorker, codex.Codex, legacy,
                policy=self.policy(), node='node', governance_factory=governance.make_worker,
                governance=object(), governance_scopes=self.policy()['scopes'],
                diagnostics=diagnostics, save_observer=metadata.install_save_observer,
                worker_observer=metadata.observe_worker)
            legacy.assert_not_called()
            self.assertTrue(sessions.save._private_metadata_observer)
            self.assertTrue(goal_sessions.save._private_metadata_observer)
            names = [base.__name__ for base in cls.__mro__]
            self.assertEqual(['ObservedWorker', 'ScopedWorker', 'GovernedWorker', 'EntranceWorker'], names[:4])
            entrance_policy = {'owner_ref': 'owner', 'conversation_id': 'unit-claw', 'agent_id': 'claw'}
            entrance_module = importlib.import_module(self.package_name + '.entrance')
            config = {'node_id': 'node', 'entrance': entrance_policy,
                'capabilities': [entrance_module.validate_policy(entrance_policy)['capability']],
                'codex': {'auth_home': '/fixture/unchanged-home', 'strict_auth_home': True,
                          'workspace': directory}}
            worker = cls(config, client=mock.Mock())
            backend_names = [base.__name__ for base in worker.backend.__mro__]
            self.assertEqual('ObservedBackend', backend_names[0])
            self.assertIn('QuotaObservedCodex', backend_names)
            self.assertIn('ScopedBackend', backend_names)
            self.assertTrue(callable(worker._save_closed_failure))
            self.assertIn('不是Mesh Leader', worker.config['codex']['instructions'])

    def _receiver(self, directory, policy, granted=True):
        events, stores, recovered = [], [], []
        original_store = self.server.Store
        original_factory = self.entrance.entrance_store_class
        original_init = original_store.__init__
        peer = {'role': 'worker', 'node': 'node', 'capabilities': [self.scope.CAPABILITY]}
        final_hook = object()

        class Control:
            def __init__(inner, socket_path):
                events.append('control-created')
            def attach(inner, http, api, channel):
                events.append('control-attached')
            def close(inner):
                events.append('control-closed')

        def construct(instance, *args, **kwargs):
            recovered.append(kwargs.get('recover_inflight'))
            original_init(instance, *args, **kwargs)

        def old_ready(http, api, channel):
            self.assertIs(original_store, self.server.Store)
            self.assertIs(original_factory, self.entrance.entrance_store_class)
            events.append('original-ingress-final-emit-feed')
            api.ingress = types.SimpleNamespace(authority=policy['authority'], owner_id=policy['owner_id'])
            api.store._conversation_final_in_db = final_hook
            api.close = lambda: events.append('original-api-close')

        def original_serve(config, ready):
            store = self.server.Store(Path(directory) / 'only-ledger', recover_inflight=True)
            stores.append(store)
            def dispatch(method, path, payload, caller):
                self.assertIs(final_hook, store._conversation_final_in_db)
                return getattr(store._native_goal_caller, 'node', None)
            api = types.SimpleNamespace(store=store, peers=[(b'SYNTHETIC', peer)] if granted else [],
                                        dispatch=dispatch, close=lambda: events.append('bare-api-close'))
            try:
                ready(object(), api, object())
                caller = peer if granted else {}
                node = api.dispatch('POST', '/unit-read', {}, caller)
                self.assertEqual('node' if policy['enabled'] else None, node)
                self.assertIsNone(getattr(store._native_goal_caller, 'node', None))
                return 'original-service-return'
            finally:
                api.close()

        config = {'entrance': {'owner_ref': 'owner', 'conversation_id': 'unit', 'agent_id': 'claw'},
                  'ilink_account': '/fixture/not-opened.json'}
        with mock.patch.object(self.server, 'serve', original_serve), \
                mock.patch.object(original_store, '__init__', construct), \
                mock.patch.object(self.owner, 'LegacyChannelOwnerControl', Control):
            try:
                result = self.launch.serve_entrance(config, '/fixture/no-socket', policy=policy, ready=old_ready)
            finally:
                self.assertIs(original_factory, self.entrance.entrance_store_class)
                self.assertIs(original_store, self.server.Store)
                self.assertEqual([False], recovered)
                self.assertEqual(1, len(stores))
                self.assertEqual('control-closed', events[-1])
                self.assertIn('original-api-close', events)
        return result, stores[0]

    def test_receiver_retains_single_passive_store_original_ready_close_and_disabled_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            result, store = self._receiver(directory, self.policy())
            self.assertEqual('original-service-return', result)
            with store.transaction() as db:
                self.assertEqual(1, db.execute('SELECT COUNT(*) FROM native_goal_scope_bindings').fetchone()[0])
            result, disabled = self._receiver(directory, self.policy(enabled=False), granted=False)
            self.assertEqual('original-service-return', result)
            self.assertTrue(disabled._native_goal_store_bound)
            disabled.heartbeat('node', ['leader'])
            identity = disabled.create_task('future fixture only',
                context={'session_scope': self.policy()['scopes'][0]})
            self.assertIsNone(disabled.claim('node'))
            with disabled.transaction() as db:
                self.assertEqual('pending', db.execute('SELECT status FROM tasks WHERE id=?', (identity,)).fetchone()[0])

    def test_receiver_missing_grant_and_pre_ready_error_restore_existing_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PermissionError, 'existing_worker_grant_required'):
                self._receiver(directory, self.policy(), granted=False)
        previous = self.entrance.entrance_store_class
        with mock.patch.object(self.entrance, 'serve_entrance', side_effect=ValueError('before-ready')):
            with self.assertRaisesRegex(ValueError, 'before-ready'):
                self.launch.serve_entrance({}, '/fixture/no-socket', policy=self.policy(), ready=lambda *args: None)
        self.assertIs(previous, self.entrance.entrance_store_class)
        self.assertFalse(self.launch._RECEIVER_COMPOSITION.locked())


if __name__ == '__main__':
    unittest.main()
