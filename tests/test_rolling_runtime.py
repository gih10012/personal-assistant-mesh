"""Critical admission/receipt boundaries and isolated real child processes.

No models, production paths, accounts, receivers, GUI or original tasks.
"""
import copy
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from assistant_mesh.rolling_runtime import (LinuxProcess, RuntimeStore, request,
                                            read_status, validate_spec)
from assistant_mesh.rolling_worker import ClaimGate, safe_boundary


ROOT = Path(__file__).resolve().parents[1]


def spec(directory, version='v1', **changes):
    value = {'kind': 'worker', 'component': 'unit-leader', 'version': version,
        'protocol_min': 1, 'protocol_max': 2, 'argv': [sys.executable, '-c', 'pass'],
        'cwd': str(directory)}
    value.update(changes)
    return value


class RollingRuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='amita-store-')
        self.addCleanup(self.temporary.cleanup)
        self.store = RuntimeStore(self.temporary.name)

    def runner(self, version):
        identifier = self.store.stage('stage-' + version, spec(self.temporary.name, version))['runner']
        with self.store.transaction() as db:
            db.execute("UPDATE runners SET state='starting' WHERE id=?", (identifier,))
            token = db.execute('SELECT token FROM runners WHERE id=?', (identifier,)).fetchone()[0]
        self.store.runner_request(identifier, token, 'ready', {}, os.getpid())
        return identifier, token

    def call(self, runner, action, body=None):
        return self.store.runner_request(runner[0], runner[1], action, body or {}, os.getpid())

    def test_adjacent_version_stage_idempotence_and_incompatible_candidate_preserve_active(self):
        old = self.runner('v1')
        self.store.activate('active-v1', old[0])
        candidate = spec(self.temporary.name, 'v2')
        first = self.store.stage('upgrade-v2', candidate)
        self.assertEqual(first, self.store.stage('upgrade-v2', candidate))
        with self.assertRaisesRegex(ValueError, 'operation_conflict'):
            self.store.stage('upgrade-v2', spec(self.temporary.name, 'different'))
        with self.assertRaisesRegex(ValueError, 'protocol_incompatible'):
            self.store.stage('incompatible', spec(self.temporary.name, 'bad', protocol_min=2))
        with self.assertRaisesRegex(ValueError, 'candidate_not_ready'):
            self.store.activate('not-yet-ready', first['runner'])
        self.assertEqual('active', self.call(old, 'heartbeat')['state'])
        self.assertEqual(2, len(self.store.status()['runners']))
        with self.assertRaisesRegex(ValueError, 'worker_adapter_required'):
            validate_spec(spec(self.temporary.name, kind='receiver'))

    def test_drain_admission_barrier_keeps_original_task_and_unknown_work(self):
        old, new = self.runner('v1'), self.runner('v2')
        self.store.activate('active-v1', old[0])
        self.assertTrue(self.call(old, 'admit', {'claim': 'original-claim'})['admitted'])
        self.store.activate('active-v2', new[0])
        # One permit issued BEFORE drain can finish; no later old claim enters.
        task = {'id': 'original-task', 'epoch': 9}
        self.call(old, 'bind', {'claim': 'original-claim', 'task': task})
        self.assertFalse(self.call(old, 'admit', {'claim': 'later'})['admitted'])
        self.assertTrue(self.call(new, 'admit', {'claim': 'new-independent'})['admitted'])
        self.call(old, 'unknown')
        with self.assertRaisesRegex(ValueError, 'checkpoint_identity_mismatch'):
            self.call(old, 'checkpoint', {'task': task, 'safe': True})
        restored = RuntimeStore(self.temporary.name).status()
        work = next(r for r in restored['runners'] if r['id'] == old[0])['work']
        self.assertEqual({'phase': 'unknown', 'task': task}, work)

    def test_safe_checkpoint_then_standby_allows_explicit_rollback_without_new_identity(self):
        old, new = self.runner('v1'), self.runner('v2')
        self.store.activate('active-v1', old[0])
        self.call(old, 'admit', {'claim': 'c'})
        task = {'id': 'same-task', 'epoch': 3}
        self.call(old, 'bind', {'claim': 'c', 'task': task})
        self.store.activate('active-v2', new[0])
        with self.assertRaisesRegex(ValueError, 'not_quiescent'):
            self.call(old, 'quiescent')
        self.call(old, 'checkpoint', {'task': task, 'safe': True})
        self.call(old, 'checkpoint', {'task': task, 'safe': True})  # Lost control ACK is idempotent.
        self.assertEqual('standby', self.call(old, 'quiescent')['state'])
        self.assertFalse(self.call(old, 'admit', {'claim': 'denied'})['admitted'])
        self.store.activate('rollback-original', old[0])
        self.assertTrue(self.call(old, 'admit', {'claim': 'accepted'})['admitted'])


class _Session:
    def __init__(self):
        self.busy, self.actions = False, []

    def call(self, action, body=None):
        self.actions.append((action, copy.deepcopy(body)))
        if action == 'admit':
            if self.busy:
                return {'admitted': False}
            self.busy = True
            return {'admitted': True}
        if action == 'checkpoint' or (action == 'bind' and body['task'] is None):
            self.busy = False
        return {'state': 'active', 'busy': self.busy}


class _Authority:
    def __init__(self, task):
        self.task, self.calls = task, []
        self.fail_update, self.fail_claim = False, False

    def request(self, route, body=None):
        self.calls.append((route, copy.deepcopy(body)))
        if route == '/v1/claim':
            if self.fail_claim:
                raise OSError('lost claim response')
            return {'task': copy.deepcopy(self.task)}
        if route == '/v1/task/update' and self.fail_update:
            raise OSError('lost final ACK')
        return {'ok': True}


class RollingWorkerBoundaryTests(unittest.TestCase):
    def task(self):
        return {'id': 'original-task', 'epoch': 8, 'checkpoint': {'thread_id': 'original-thread',
            'operation_id': 'original-operation', 'side_effect_started': True},
            'sessions': {'codex': {'thread_id': 'original-thread'}}}

    def test_active_goal_first_turn_and_unreaped_runtime_never_count_as_safe(self):
        task = self.task()
        task['checkpoint']['side_effect_started'] = False
        goal = {'status': 'active', 'threadId': 'original-thread', 'objective': 'ongoing goal'}
        task['checkpoint']['goal'] = goal
        final = {'status': 'waiting_children'}
        self.assertFalse(safe_boundary(task, final))
        life = {'controller_enabled': True, 'goal_managed': True, 'settled': True,
            'runtime_closed': False, 'thread_id': 'original-thread', 'outcome': 'yielded', 'goal': goal}
        task['checkpoint']['native_lifecycle'] = life
        self.assertFalse(safe_boundary(task, final))
        life['runtime_closed'] = True
        self.assertTrue(safe_boundary(task, final))
        self.assertFalse(safe_boundary(task, {'status': 'completed'}))
        life['goal'] = dict(goal, objective='changed goal')
        self.assertFalse(safe_boundary(task, final))

    def test_original_identity_receipt_and_return_from_worker_required_for_checkpoint(self):
        session, authority = _Session(), _Authority(self.task())
        gate = ClaimGate(authority, session)
        original = gate.request('/v1/claim', {})['task']
        self.assertEqual(self.task(), original)
        final = {'id': original['id'], 'epoch': original['epoch'],
            'checkpoint': {'side_effect_started': False, 'goal': None}, 'status': 'completed'}
        gate.request('/v1/task/update', final)
        worker = type('Worker', (), {'current': None, 'agent': None})()
        gate.finish_boundary(worker)
        self.assertFalse(session.busy)
        checkpoint = [body for action, body in session.actions if action == 'checkpoint'][0]
        self.assertEqual({'id': 'original-task', 'epoch': 8}, checkpoint['task'])
        self.assertEqual(final, authority.calls[-1][1])
        self.assertEqual('original-operation', original['checkpoint']['operation_id'])
        self.assertEqual({'thread_id': 'original-thread'}, original['sessions']['codex'])

    def test_lost_claim_or_finalize_ack_never_replays_and_unknown_stays_local(self):
        for lost in ('claim', 'finalize'):
            with self.subTest(lost=lost):
                session, authority = _Session(), _Authority(self.task())
                gate = ClaimGate(authority, session)
                if lost == 'claim':
                    authority.fail_claim = True
                    with self.assertRaises(OSError):
                        gate.request('/v1/claim', {})
                else:
                    gate.request('/v1/claim', {})
                    authority.fail_update = True
                    with self.assertRaises(OSError):
                        gate.request('/v1/task/update', {'id': 'original-task', 'epoch': 8,
                            'checkpoint': {'side_effect_started': False}, 'status': 'completed'})
                self.assertTrue(gate.unknown)
                self.assertEqual({'task': None}, gate.request('/v1/claim', {}))
                self.assertEqual(1, sum(route == '/v1/claim' for route, _ in authority.calls))
                self.assertTrue(session.busy)
        # A stale preflight failure cannot override this epoch's active goal.
        task = self.task()
        task['checkpoint'].update(side_effect_started=False,
            goal={'status': 'active'}, runtime_failure={'attempt_epoch': 7, 'native_start_attempted': False})
        self.assertFalse(safe_boundary(task, {'status': 'completed'}))

    def test_current_or_live_native_reference_blocks_even_confirmed_final_update(self):
        session, authority = _Session(), _Authority(self.task())
        gate = ClaimGate(authority, session)
        gate.request('/v1/claim', {})
        gate.request('/v1/task/update', {'id': 'original-task', 'epoch': 8,
            'checkpoint': {'side_effect_started': False}, 'status': 'completed'})
        gate.finish_boundary(type('Worker', (), {'current': None, 'agent': object()})())
        self.assertTrue(gate.unknown)
        self.assertFalse(any(action == 'checkpoint' for action, _ in session.actions))

    def test_lost_safe_control_checkpoint_ack_only_retries_same_local_metadata(self):
        session, authority = _Session(), _Authority(self.task())
        gate = ClaimGate(authority, session)
        gate.request('/v1/claim', {})
        gate.request('/v1/task/update', {'id': 'original-task', 'epoch': 8,
            'checkpoint': {'side_effect_started': False}, 'status': 'completed'})
        call = session.call
        failed = []

        def lost_once(action, body=None):
            result = call(action, body)
            if action == 'checkpoint' and not failed:
                failed.append(True)
                raise OSError('lost control ACK')
            return result
        session.call = lost_once
        with self.assertRaises(OSError):
            gate.finish_boundary(type('Worker', (), {'current': None, 'agent': None})())
        self.assertFalse(gate.unknown)
        gate.flush_checkpoint()
        checkpoints = [body for action, body in session.actions if action == 'checkpoint']
        self.assertEqual(2, len(checkpoints))
        self.assertEqual(checkpoints[0], checkpoints[1])
        self.assertEqual(2, len(authority.calls))
        self.assertIsNone(gate.pending_checkpoint)


CHILD = r'''
import os,sys,threading,time
from pathlib import Path
from assistant_mesh.rolling_worker import run_worker
assert 'assistant_mesh.codex' not in sys.modules
marker=Path(sys.argv[1])
class Client:
    def request(self,route,body=None):
        return {'task':None} if route=='/v1/claim' else {'ok':True}
class Worker:
    def __init__(self):
        marker.write_text('activated')
        self.client=Client(); self.stop=threading.Event(); self.current=None; self.agent=None
    def run_once(self):
        self.client.request('/v1/claim',{})
run_worker(Worker)
'''


@unittest.skipUnless(sys.platform == 'linux', 'Linux process adapter')
class RollingRuntimeProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='amita-proc-')
        self.root = Path(self.temporary.name)
        self.state = self.root / 'control'
        self.socket = str(self.state / 'control.sock')
        self.processes = []
        self.addCleanup(self.cleanup)
        self.parent = self.start_parent()
        self.wait(lambda: Path(self.socket).exists())

    def start_parent(self):
        child = subprocess.Popen([sys.executable, '-m', 'assistant_mesh.rolling_runtime',
            '--state', str(self.state), 'serve'], cwd=str(ROOT),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(child)
        return child

    def cleanup(self):
        # Only processes created by THIS test in its private temp directory.
        try:
            rows = read_status(self.state / 'runtime.sqlite3')['runners']
        except (OSError, ValueError, sqlite3.Error):
            rows = []
        for row in rows:
            if row['alive']:
                try:
                    os.kill(row['pid'], signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for child in self.processes:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        self.temporary.cleanup()

    def wait(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = condition()
                if value:
                    return value
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.05)
        self.fail('isolated process condition timed out')

    def row(self, runner):
        return next(r for r in request(self.socket, 'status')['runners'] if r['id'] == runner)

    def stage(self, version, argv=None):
        marker = self.root / (version + '.marker')
        value = spec(self.root, version, argv=argv or [sys.executable, '-c', CHILD, str(marker)],
                     env={'PYTHONPATH': str(ROOT)})
        runner = request(self.socket, 'stage', {'operation': 'stage-' + version, 'spec': value})['runner']
        return runner, marker

    def activate(self, runner, operation):
        self.wait(lambda: self.row(runner)['state'] == 'standby')
        return request(self.socket, 'activate', {'operation': operation, 'runner': runner})

    def test_real_cli_standby_roll_rollback_and_parent_restart_keep_control_durable(self):
        old, old_marker = self.stage('old')
        self.wait(lambda: self.row(old)['state'] == 'standby')
        self.assertFalse(old_marker.exists())  # Factory/account/model path not entered.
        self.activate(old, 'activate-old')
        self.wait(old_marker.exists)
        new, new_marker = self.stage('new')
        self.wait(lambda: self.row(new)['state'] == 'standby')
        self.assertFalse(new_marker.exists())
        launch = spec(self.root, 'new', argv=[sys.executable, '-c', CHILD, str(new_marker)],
                      env={'PYTHONPATH': str(ROOT)})
        launch_path = self.root / 'launch.json'
        launch_path.write_text(json.dumps(launch))
        launch_path.chmod(0o600)
        roll = subprocess.run([sys.executable, '-m', 'assistant_mesh.rolling_runtime',
            '--state', str(self.state), 'roll', '--operation-id', 'stage-new',
            '--spec', str(launch_path)], cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, timeout=8)
        self.assertEqual(0, roll.returncode, roll.stderr)
        self.assertEqual(old, json.loads(roll.stdout)['previous'])
        self.wait(new_marker.exists)
        self.wait(lambda: self.row(old)['state'] == 'standby')
        self.assertFalse(self.row(old)['claim_admission'])
        # Controlled death is limited to this newly-created harmless process.
        os.kill(self.row(new)['pid'], signal.SIGTERM)
        self.wait(lambda: self.row(old)['state'] == 'active')
        self.assertEqual('failed', self.row(new)['state'])
        old_pid = self.row(old)['pid']
        self.parent.terminate()
        self.parent.wait(timeout=5)
        offline = read_status(self.state / 'runtime.sqlite3')
        self.assertFalse(offline['supervisor_alive'])
        self.assertEqual(old_pid, next(r for r in offline['runners'] if r['id'] == old)['pid'])
        # Queue records survive loss of the stable parent; children reconnect.
        self.parent = self.start_parent()
        self.wait(lambda: request(self.socket, 'status')['supervisor_alive'])
        self.wait(lambda: self.row(old)['heartbeat'] > time.time() - 2)
        self.assertEqual(old_pid, self.row(old)['pid'])
        self.assertEqual(2, len(request(self.socket, 'status')['runners']))
        cli = subprocess.run([sys.executable, '-m', 'assistant_mesh.rolling_runtime',
            '--state', str(self.state), 'status'], cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, timeout=5)
        self.assertEqual(0, cli.returncode, cli.stderr)
        self.assertEqual('not_checked', json.loads(cli.stdout)['model_availability'])

    def test_failed_standby_and_duplicate_stage_do_not_disrupt_active_runner(self):
        old, marker = self.stage('old')
        self.activate(old, 'activate-old')
        self.wait(marker.exists)
        bad_spec = spec(self.root, 'bad', argv=[sys.executable, '-c', 'raise SystemExit(3)'])
        first = request(self.socket, 'stage', {'operation': 'same-stage', 'spec': bad_spec})
        self.assertEqual(first, request(self.socket, 'stage', {'operation': 'same-stage', 'spec': bad_spec}))
        self.wait(lambda: self.row(first['runner'])['state'] == 'failed')
        self.assertEqual('active', self.row(old)['state'])
        self.assertTrue(self.row(old)['alive'])
        self.assertEqual(2, len(request(self.socket, 'status')['runners']))


if __name__ == '__main__':
    unittest.main()
