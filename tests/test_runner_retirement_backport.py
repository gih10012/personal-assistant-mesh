"""Three actual-code composition fixtures; only temporary control journals."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from assistant_mesh import runner_retirement as adapter


ROOT = Path(__file__).resolve().parents[1]
ACTUAL = Path.home() / '.local/share/amita/control/20261028.1'
BASE = Path.home() / '.local/share/amita/control/20261026.2'


def load(name, path, paths=None):
    spec = importlib.util.spec_from_file_location(name, str(path), submodule_search_locations=paths)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((ACTUAL / 'assistant_mesh/rolling_runtime.py').is_file(), 'captured actual control sources required')
class RunnerRetirementBackportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='runner-retirement.')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.builder = load('_runner_retirement_builder', ROOT / 'deploy/build-runner-retirement-backport.py')
        self.inputs = dict(runtime=ACTUAL / 'assistant_mesh/rolling_runtime.py',
            installed_runtime=str(ACTUAL / 'assistant_mesh/rolling_runtime.py'),
            base_runtime=BASE / 'assistant_mesh/rolling_runtime.py',
            installed_base_runtime=str(BASE / 'assistant_mesh/rolling_runtime.py'),
            package_init=ACTUAL / 'assistant_mesh/__init__.py', installed_init=str(ACTUAL / 'assistant_mesh/__init__.py'),
            launcher=ACTUAL / 'launch_control.py', installed_launcher=str(ACTUAL / 'launch_control.py'),
            maintenance_client_config='/PRIVATE/fixture-fixed-maintenance-client.json')
        self.candidate = self.root / 'candidate'
        self.report = self.builder.build(self.candidate, **self.inputs)
        self.package = '_retirement_fixture_' + os.urandom(8).hex()
        load(self.package, self.candidate / 'assistant_mesh/__init__.py', [str(self.candidate / 'assistant_mesh')])
        self.runtime = load(self.package + '.rolling_runtime', self.candidate / 'assistant_mesh/rolling_runtime.py')
        self.addCleanup(lambda: [sys.modules.pop(key, None) for key in list(sys.modules)
                                if key == self.package or key.startswith(self.package + '.')])

    def runner(self, store):
        spec = dict(kind='worker', component='fixture-original', version='original', protocol_min=1,
                    protocol_max=1, argv=[sys.executable, '-c', 'pass'], cwd=str(self.root), autorestart=True)
        runner = store.stage('original-stage', spec)['runner']
        birth = self.runtime.LinuxProcess.identity(os.getpid())
        with store.transaction() as db:
            db.execute("UPDATE runners SET state='starting',pid=?,birth=? WHERE id=?", (os.getpid(), birth, runner))
            token = db.execute('SELECT token FROM runners WHERE id=?', (runner,)).fetchone()[0]
        store.runner_request(runner, token, 'ready', {}, os.getpid())
        store.activate('original-activate', runner)
        work = ' { "task" : {"epoch":7,"id":"same-original-task"}, "phase":"unknown", "extra":{"receipt":"preserved"} } '
        with store.transaction() as db:
            db.execute('UPDATE runners SET work=? WHERE id=?', (work, runner))
        proof = dict(protocol=adapter.PROTOCOL, acceptance_id='original-acceptance', accepted=True,
            evidence_basis='trusted-owner-reviewed-local-effects-and-runtime-closure', task_id='same-original-task',
            source_epoch=7, source_thread_id='original-thread', source_turn_id='original-turn', closure=dict(
                runner_id=runner, runner_pid=os.getpid(), runner_birth=birth,
                kind='owner-observed-native-runtime-closed', native_owner_live=False, owned_children_closed=True,
                forced_cleanup=False, thread_id='original-thread', turn_id='original-turn',
                runtime_work_sha256=hashlib.sha256(self.runtime._json(json.loads(work)).encode('utf8')).hexdigest()))
        return runner, token, work, proof

    def test_pinned_old_composition_preserves_constructors_transactions_recovery_and_refuses_changed_sources(self):
        runtime = self.runtime
        recovery = runtime._retirement_incumbent
        original = recovery._recovery_incumbent
        self.assertIs(runtime.RuntimeStore, original.RuntimeStore)
        self.assertIs(runtime.RuntimeStore.__init__, original.RuntimeStore.__init__)
        self.assertIs(runtime.RuntimeStore.transaction, original.RuntimeStore.transaction)
        self.assertIs(runtime.Supervisor.__init__, recovery.Supervisor.__init__)
        self.assertIs(runtime.Supervisor.start, recovery.Supervisor.start)
        self.assertIs(runtime.Supervisor._recover, recovery.Supervisor._recover)
        self.assertIs(runtime.Supervisor.reconcile, recovery.Supervisor.reconcile)
        self.assertEqual(str(BASE / 'assistant_mesh/rolling_runtime.py'), runtime.RuntimeStore.__init__.__code__.co_filename)
        self.assertEqual(str(BASE / 'assistant_mesh/rolling_runtime.py'), runtime.RuntimeStore.transaction.__wrapped__.__code__.co_filename)
        self.assertEqual(str(ACTUAL / 'assistant_mesh/rolling_runtime.py'), runtime.Supervisor._recover.__code__.co_filename)
        self.assertNotIn('__init__', runtime._RetirementMethods.__dict__)
        self.assertNotIn('transaction', runtime._RetirementMethods.__dict__)
        self.assertTrue(self.report['original_recovery_preserved'])
        self.assertFalse(self.report['ledger_opened'])
        with self.assertRaisesRegex(ValueError, 'new_absolute_destination'):
            self.builder.build(self.candidate, **self.inputs)
        changed = self.root / 'changed-init.py'
        changed.write_bytes((ACTUAL / 'assistant_mesh/__init__.py').read_bytes() + b'\n# changed\n')
        with self.assertRaisesRegex(ValueError, 'source_pin_changed'):
            self.builder.build(self.root / 'refused', **dict(self.inputs, package_init=changed))
        self.assertFalse((self.root / 'refused').exists())

    def test_reviewed_retirement_projects_stopped_only_after_auth_and_retains_full_work_and_receipt(self):
        store = self.runtime.RuntimeStore(self.root / 'control')
        runner, token, work, proof = self.runner(store)
        with self.assertRaisesRegex(ValueError, 'authority_not_installed'):
            store.retire_closed_runner('retire-original', runner, proof['acceptance_id'])
        calls = []
        store.retirement_resolver = adapter.receipt_resolver(type('Client', (), {
            'request': lambda unused, path, body: calls.append((path, body)) or proof})())
        receipt = store.retire_closed_runner('retire-original', runner, proof['acceptance_id'])
        self.assertEqual([(adapter.RECEIPT_PATH, {'acceptance_id': 'original-acceptance'})], calls)
        for supplied, pid, error in (('wrong-token', os.getpid(), 'auth_failed'),
                                     (token, os.getpid() + 10000000, 'process_mismatch')):
            with self.assertRaisesRegex(ValueError, error):
                store.runner_request(runner, supplied, 'heartbeat', {}, pid)
        self.assertEqual({'state': 'stopped', 'busy': True, 'protocol': 1},
                         store.runner_request(runner, token, 'heartbeat', {}, os.getpid()))
        with store.transaction() as db:
            row = dict(store._runner(db, runner))
            saved = dict(db.execute('SELECT * FROM native_closed_runner_retirements WHERE runner=?', (runner,)).fetchone())
            self.assertEqual(work, row['work'])
            self.assertEqual(work, saved['original_work'])
            self.assertEqual(proof, json.loads(saved['receipt']))
            self.assertIsNone(row['last_checkpoint'])
            self.assertEqual('draining', row['state'])
            self.assertEqual(os.getpid(), row['pid'])
            self.assertEqual(0, db.execute('SELECT enabled FROM recovery WHERE source=?', (runner,)).fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM native_closed_runner_retirements WHERE runner=?', (runner,))
        store.retirement_resolver = None
        self.assertEqual(receipt, store.retire_closed_runner('retire-original', runner, proof['acceptance_id']))
        self.assertFalse(receipt['signals_sent'])
        self.assertFalse(receipt['business_guard_changed'])

    def test_original_same_uid_socket_path_accepts_only_ids_and_runner_cannot_supply_proof_or_config(self):
        supervisor = self.runtime.Supervisor(self.root / 'control')
        runner, token, work, proof = self.runner(supervisor.store)
        supervisor.store.retirement_resolver = lambda identity: proof
        body = dict(operation='owner-retire', runner=runner, acceptance_id=proof['acceptance_id'])
        for extra in ({'receipt': proof}, {'config': '/UNTRUSTED/client.json'}, {'resolver': 'custom'}):
            with self.assertRaisesRegex(ValueError, 'control_shape_invalid'):
                supervisor.dispatch(dict(protocol=1, action='retire_closed', body=dict(body, **extra)), os.getpid())
        with self.assertRaisesRegex(ValueError, 'action_unknown'):
            supervisor.dispatch(dict(protocol=1, action='retire_closed', body=body, runner=runner, token=token), os.getpid())
        supervisor.start()
        self.addCleanup(supervisor.close)
        receipt = self.runtime.request(supervisor.socket_path, 'retire_closed', body)
        self.assertTrue(receipt['cooperative_exit_requested'])
        with supervisor.store.transaction() as db:
            self.assertEqual(work, supervisor.store._runner(db, runner)['work'])
        client = adapter.MaintenanceReceiptClient.__new__(adapter.MaintenanceReceiptClient)
        with self.assertRaisesRegex(ValueError, 'receipt_only_client'):
            client.request('/v1/task/control', {'acceptance_id': proof['acceptance_id']})
        launcher = (self.candidate / 'launch_control.py').read_text()
        self.assertIn(repr(self.inputs['maintenance_client_config']), launcher)
        self.assertNotIn('private_json', launcher)
