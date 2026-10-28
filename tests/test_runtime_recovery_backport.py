"""Only pinned actual-source overlays and temporary ledgers; no live services."""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from tests.test_phone_frozen_candidate import BASE, SNAPSHOT, SNAPSHOT_SHA


ROOT = Path(__file__).resolve().parents[1]
PHONE = Path('/var/tmp/amita-phone-release-20261028/receiver-source')
CONTROL = Path('/home/gihggiy/.local/share/amita/control/20261026.2/assistant_mesh')
CONTROL_SHA = '1d6feb38369d62d09f8bdf4f05a998fd8fb614892d0a43766459ca25be347271'
INIT_SHA = '3c382e0fac38eb8650a900925a2cf3edd64e4fd766d3b0d8a5c0d578c582899b'


def load(name, path, paths=None):
    spec = importlib.util.spec_from_file_location(name, str(path), submodule_search_locations=paths)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RuntimeRecoveryBackportTests(unittest.TestCase):
    def setUp(self):
        self.builder = load('_recovery_backport_builder', ROOT / 'deploy/build-runtime-recovery-backport.py')
        self.temp = tempfile.TemporaryDirectory(prefix='amita-recovery-backport.')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @unittest.skipUnless(SNAPSHOT.is_file() and BASE.is_dir() and PHONE.is_dir(), 'actual source snapshots required')
    def test_actual_receiver_helpers_fence_unknown_in_original_transaction_and_preserve_constructor(self):
        candidate = self.root / 'receiver'
        report = self.builder.build_receiver(SNAPSHOT, candidate, snapshot_sha256=SNAPSHOT_SHA,
            phone_candidate=PHONE, installed_phone_root=str(PHONE),
            installed_methods_source=str(SNAPSHOT.parent / 'resolved/amita_task_methods.py'))
        self.assertLess(report['source_bytes'], 40000)
        self.assertEqual(set(('assistant_mesh/__init__.py', 'assistant_mesh/amita_task_methods.py',
                             'assistant_mesh/operation_isolation.py', 'launch_receiver.py')),
                         set(report['source_sha256']))
        self.assertFalse(report['incumbent_files_modified'])
        self.assertFalse(report['ledger_opened'])
        self.assertNotIn('def __init__', (candidate / 'assistant_mesh/amita_task_methods.py').read_text())
        package_name = '_recovery_actual_receiver'
        load(package_name, candidate / 'assistant_mesh/__init__.py', [
            str(candidate / 'assistant_mesh'), str(PHONE / 'assistant_mesh'),
            str(SNAPSHOT.parent / 'resolved'), str(SNAPSHOT.parent / 'incumbent/assistant_mesh'), str(BASE)])
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in list(sys.modules)
                                if name == package_name or name.startswith(package_name + '.')])
        def module(name):
            return importlib.import_module(package_name + '.' + name)
        store_module, methods = module('store'), module('amita_task_methods')
        original_bytes = (SNAPSHOT.parent / 'resolved/store.py').read_bytes()
        self.assertEqual(hashlib.sha256(original_bytes).hexdigest(),
                         hashlib.sha256(Path(store_module.__file__).read_bytes()).hexdigest())
        isolated = module('operation_isolation').make_store_class(store_module.Store, methods.Store)
        entrance = module('entrance_runtime').entrance_store_class(isolated,
            {'owner_ref': 'synthetic-owner', 'conversation_id': 'synthetic-conversation', 'agent_id': 'synthetic-claw'})
        selected = module('task_transaction_adapter').make_store_class(entrance, methods.Store)
        self.assertIs(store_module.Store.__init__, isolated.__init__)
        now = [1000]
        store = selected(self.root / 'original.db', clock=lambda: now[0], recover_inflight=False)
        store.heartbeat('node', ['leader', 'agent'])
        unknown = store.create_task('synthetic unknown', task_id='original-unknown')
        task = store.claim('node')
        self.assertEqual(unknown, task['id'])
        checkpoint = {'side_effect_started': False, 'thread_id': 'original-thread', 'runtime_failure': {
            'native_start_attempted': True, 'outcome': 'unknown', 'attempt_epoch': task['epoch'],
            'phase': 'native_finish', 'category': 'codex_disconnected'}}
        # Same fenced private helper used by receipt-bearing dispatch. Its hold
        # must commit in this transaction even when an old Worker reports wait.
        with store.transaction() as db:
            store._update_task_in_db(db, unknown, 'node', task['epoch'], checkpoint=checkpoint,
                                     status='waiting_backend')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (unknown,)).fetchone()
            self.assertEqual(('needs_review', task['epoch']), (row['status'], row['epoch']))
        self.assertEqual({'requeued': 0}, store.retry_waiting())
        with self.assertRaises(store_module.Conflict):
            store.control_task(unknown, 'resume')
        with store.transaction() as db:
            # A legacy already-persisted wait is fenced without a new claim.
            db.execute("UPDATE tasks SET status='waiting_backend',deadline=0 WHERE id=?", (unknown,))
        self.assertIsNone(store.claim('node'))
        independent = store.create_task('independent preflight quota', task_id='safe-preflight',
                                         context={'session_scope': 'independent'})
        safe = store.claim('node')
        self.assertEqual(independent, safe['id'])
        store.update_task(independent, 'node', safe['epoch'], checkpoint={'runtime_failure': {
            'native_start_attempted': False, 'outcome': 'unknown', 'category': 'codex_usage_limit_exceeded'}},
            status='waiting_backend')
        self.assertEqual('waiting_backend', store.task_status(independent)['status'])
        now[0] += 61
        store.heartbeat('node', ['leader', 'agent'])
        retry = store.claim('node')
        self.assertEqual((independent, safe['epoch'] + 1), (retry['id'], retry['epoch']))
        with store.transaction() as db:
            held = db.execute('SELECT * FROM tasks WHERE id=?', (unknown,)).fetchone()
            self.assertEqual(('needs_review', task['epoch']), (held['status'], held['epoch']))
            self.assertEqual(checkpoint['runtime_failure'], json.loads(held['checkpoint'])['runtime_failure'])

    @unittest.skipUnless(CONTROL.is_dir(), 'actual local control source required')
    def test_actual_control_overlay_changes_only_recover_and_readmits_same_owner(self):
        candidate = self.root / 'control'
        report = self.builder.build_control(CONTROL / 'rolling_runtime.py', candidate,
            source_sha256=CONTROL_SHA, package_init=CONTROL / '__init__.py', init_sha256=INIT_SHA)
        self.assertLess(report['source_bytes'], 10000)
        package_name = '_recovery_actual_control'
        load(package_name, candidate / 'assistant_mesh/__init__.py', [str(candidate / 'assistant_mesh')])
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in list(sys.modules)
                                if name == package_name or name.startswith(package_name + '.')])
        runtime = importlib.import_module(package_name + '.rolling_runtime')
        old = runtime._recovery_incumbent
        self.assertIs(runtime.RuntimeStore, old.RuntimeStore)
        self.assertIs(runtime.Supervisor.reconcile, old.Supervisor.__mro__[1].reconcile)
        self.assertEqual(['_recover'], [name for name in runtime.Supervisor.__dict__ if not name.startswith('__')])
        supervisor = runtime.Supervisor(self.root / 'state')
        store = supervisor.store
        source = store.stage('source-stage', {'kind': 'worker', 'component': 'unit', 'version': 'actual',
            'protocol_min': 1, 'protocol_max': 1, 'argv': [sys.executable, '-c', 'pass'],
            'cwd': str(self.root), 'autorestart': True})['runner']
        with store.transaction() as db:
            db.execute("UPDATE runners SET state='standby',ready=1,pid=?,birth=?,heartbeat=? WHERE id=?",
                       (os.getpid(), runtime.LinuxProcess.identity(os.getpid()), __import__('time').time(), source))
        store.activate('original-activation', source)
        with store.transaction() as db:
            db.execute("UPDATE runners SET state='standby',note='rollback_standby' WHERE id=?", (source,))
            before = dict(store._runner(db, source))
            supervisor._recover(db, __import__('time').time())
            after = dict(store._runner(db, source))
            self.assertEqual('active', after['state'])
            self.assertEqual((before['id'], before['pid'], before['birth']),
                             (after['id'], after['pid'], after['birth']))
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM runners').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
