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

from assistant_mesh.rolling_runtime import (LinuxProcess, RuntimeStore, Supervisor, request,
                                            read_status, validate_spec)
from assistant_mesh.rolling_worker import ClaimGate, safe_boundary


ROOT = Path(__file__).resolve().parents[1]


def spec(directory, version='v1', **changes):
    value = {'kind': 'worker', 'component': 'unit-leader', 'version': version,
        'protocol_min': 1, 'protocol_max': 2, 'argv': [sys.executable, '-c', 'pass'],
        'cwd': str(directory), 'autorestart': False}
    value.update(changes)
    return value


class RollingRuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='amita-store-')
        self.addCleanup(self.temporary.cleanup)
        self.store = RuntimeStore(self.temporary.name)

    def runner(self, version, **changes):
        identifier = self.store.stage('stage-' + version, spec(self.temporary.name, version, **changes))['runner']
        with self.store.transaction() as db:
            db.execute("UPDATE runners SET state='starting',pid=?,birth=? WHERE id=?",
                       (os.getpid(), LinuxProcess.identity(os.getpid()), identifier))
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

    def test_only_idle_current_owner_is_readmitted_after_cooperative_reconnect(self):
        owner = self.runner('owner', autorestart=True)
        self.store.activate('activate-owner', owner[0])
        other = self.runner('other')
        supervisor = Supervisor(self.temporary.name)
        with self.store.transaction() as db:
            db.execute("UPDATE runners SET state='retained' WHERE id=?", (owner[0],))
            db.execute("UPDATE runners SET state='stopped' WHERE id=?", (other[0],))
        self.call(owner, 'quiescent')
        # Each candidate violation or unfenced peer must keep admission closed.
        cases = [
            ("UPDATE runners SET work=? WHERE id=?", (json.dumps({'phase': 'unknown'}), owner[0])),
            ("UPDATE runners SET note='owner_drain' WHERE id=?", (owner[0],)),
            ("UPDATE runners SET heartbeat=0 WHERE id=?", (owner[0],)),
            ("UPDATE runners SET birth=NULL WHERE id=?", (owner[0],)),
            ("UPDATE runners SET state='standby' WHERE id=?", (other[0],)),
            ("UPDATE runners SET state='active' WHERE id=?", (other[0],)),
            ("UPDATE runners SET state='retained',pid=NULL,birth=NULL WHERE id=?", (other[0],)),
            ("UPDATE runners SET state='queued' WHERE id=?", (other[0],)),
            ("UPDATE recovery SET enabled=0", ()),
        ]
        for sql, args in cases:
            with self.subTest(sql=sql), self.store.transaction() as db:
                db.execute("UPDATE runners SET state='standby',work=NULL,note='rollback_standby',heartbeat=?,birth=? WHERE id=?",
                           (time.time(), LinuxProcess.identity(os.getpid()), owner[0]))
                db.execute("UPDATE runners SET state='stopped' WHERE id=?", (other[0],))
                db.execute('UPDATE recovery SET enabled=1')
                db.execute(sql, args)
                supervisor._recover(db, time.time())
                self.assertEqual('standby', self.store._runner(db, owner[0])['state'])
        with self.store.transaction() as db:
            db.execute('UPDATE recovery SET enabled=1')
            supervisor._recover(db, time.time())
            self.assertEqual('active', self.store._runner(db, owner[0])['state'])
            self.assertEqual(2, db.execute('SELECT COUNT(*) FROM runners').fetchone()[0])
        self.assertTrue(self.call(owner, 'admit', {'claim': 'first-after-reconnect'})['admitted'])

    def test_automatic_recovery_transfers_owner_before_clearing_pending(self):
        old = self.runner('source', autorestart=True)
        self.store.activate('activate-source', old[0])
        recovered = self.runner('recovered', autorestart=True)
        supervisor = Supervisor(self.temporary.name)
        with self.store.transaction() as db:
            db.execute("UPDATE runners SET state='failed',death_confirmed=1 WHERE id=?", (old[0],))
            db.execute('UPDATE recovery SET pending=?', (recovered[0],))
            supervisor._recover(db, time.time())
            row = db.execute('SELECT * FROM recovery').fetchone()
            self.assertEqual(recovered[0], row['source'])
            db.execute('UPDATE recovery SET activated_at=?', (time.time() - 31,))
            supervisor._recover(db, time.time())
            self.assertIsNone(db.execute('SELECT pending FROM recovery').fetchone()[0])
            db.execute("UPDATE runners SET state='retained' WHERE id=?", (recovered[0],))
        self.call(recovered, 'quiescent')
        with self.store.transaction() as db:
            supervisor._recover(db, time.time())
            self.assertEqual('active', self.store._runner(db, recovered[0])['state'])


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


RECOVERY_CHILD = r'''
import os,sys,time,secrets
from pathlib import Path
fail,first,effects,database=map(Path,sys.argv[1:])
if fail.exists(): raise SystemExit(3)
from assistant_mesh.rolling_runtime import request
endpoint=os.environ['AMITA_RUNTIME_SOCKET']; runner=os.environ['AMITA_RUNNER_ID']; token=os.environ['AMITA_RUNNER_TOKEN']
def rpc(action,body=None):
    while True:
        try: return request(endpoint,action,body,runner,token)
        except (OSError,ValueError):
            if action!='heartbeat': raise
            time.sleep(.05)
while True:
    try: rpc('ready'); break
    except (OSError,ValueError): time.sleep(.05)
while rpc('heartbeat')['state']!='active': time.sleep(.05)
from assistant_mesh.store import Store
store=Store(database,recover_inflight=False)
def claim():
    if not rpc('admit',{'claim':(call:=secrets.token_hex(8))}).get('admitted'): return None
    store.heartbeat('recovery-node',['agent'])
    task=store.claim('recovery-node')
    rpc('bind',{'claim':call,'task':{k:task[k] for k in ('id','epoch')} if task else None})
    return task
def effect(text):
    with effects.open('a') as output: output.write(text+'\n')
if not first.exists():
    task=claim()
    store.update_task(task['id'],'recovery-node',task['epoch'],checkpoint={'side_effect_started':True,
        'operation_id':'original-operation','thread_id':'original-thread'})
    effect('original')
    first.write_text(task['id'])
    while True:
        rpc('heartbeat'); time.sleep(.05)
while True:
    status=rpc('heartbeat')
    if status['state']=='active' and not status['busy']:
        task=claim()
        if task:
            effect(task['input'])
            store.update_task(task['id'],'recovery-node',task['epoch'],
                checkpoint={'side_effect_started':False},status='completed')
            rpc('checkpoint',{'task':{k:task[k] for k in ('id','epoch')},'safe':True})
    time.sleep(.05)
'''


UNKNOWN_CHILD = r'''
import os,sys,time
from pathlib import Path
from assistant_mesh.rolling_runtime import request
started,ready,pause=map(Path,sys.argv[1:])
started.write_text(str(os.getpid()))
while not ready.exists(): time.sleep(.02)
def rpc(action,body=None):
    return request(os.environ['AMITA_RUNTIME_SOCKET'],action,body,
        os.environ['AMITA_RUNNER_ID'],os.environ['AMITA_RUNNER_TOKEN'])
while True:
    try: rpc('ready',{'pid':1,'work':None}); break
    except (OSError,ValueError): time.sleep(.05)
while True:
    if not pause.exists(): rpc('heartbeat')
    time.sleep(.05)
'''


@unittest.skipUnless(sys.platform == 'linux', 'Linux process adapter')
class RollingRuntimeRecoveryProcessTests(unittest.TestCase):
    # Reuse only the existing private-process setup, never inherit/re-run its
    # completed test methods or touch the standing runtime on this host.
    start_parent = RollingRuntimeProcessTests.start_parent
    wait = RollingRuntimeProcessTests.wait
    row = RollingRuntimeProcessTests.row
    activate = RollingRuntimeProcessTests.activate

    def setUp(self):
        self.extra_children = {}
        RollingRuntimeProcessTests.setUp(self)

    def cleanup(self):
        # Stop recovery before terminating only our owned temporary children.
        for child in self.processes:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        for pid, birth in self.extra_children.items():
            if LinuxProcess.alive(pid, birth):
                os.kill(pid, signal.SIGTERM)
        RollingRuntimeProcessTests.cleanup(self)

    def launch(self, operation, value):
        return request(self.socket, 'stage', {'operation': operation, 'spec': value})['runner']

    def test_confirmed_boot_death_recovers_with_persistent_backoff_and_original_authority_guard(self):
        from assistant_mesh.store import Store
        database = self.root / 'authority.sqlite3'
        authority = Store(database, recover_inflight=False)
        original = authority.create_task('original', ['agent'], context={'session_scope': 'original-scope'})
        fail, first, effects = [self.root / name for name in ('fail', 'first', 'effects')]
        value = spec(self.root, autorestart=True, restart_backoff={'initial': .2, 'maximum': .8},
            argv=[sys.executable, '-c', RECOVERY_CHILD, str(fail), str(first), str(effects), str(database)],
            env={'PYTHONPATH': str(ROOT)})
        # Missing autorestart on an already stored owner spec defaults True;
        # its exact request/fingerprint must stay unchanged after parent restart.
        value.pop('autorestart')
        old = self.launch('original-owner-operation', value)
        self.activate(old, 'original-owner-activation')
        self.wait(first.exists)
        self.assertEqual(original, first.read_text())
        old_pid = self.row(old)['pid']
        self.parent.terminate(); self.parent.wait(timeout=5)
        os.kill(old_pid, signal.SIGTERM)
        self.wait(lambda: not LinuxProcess.alive(old_pid, self.row_offline(old)['birth']))
        independent = authority.create_task('independent', ['agent'], context={'session_scope': 'independent-scope'})
        with authority.transaction() as db:
            db.execute('UPDATE tasks SET deadline=0 WHERE id=?', (original,))
        fail.touch()
        with RuntimeStore(self.state).transaction() as db:
            # A persisted prior boot plus PID reuse cannot authorize killing
            # this test process. It proves only that the old identity is dead.
            db.execute('UPDATE runners SET pid=?,birth=? WHERE id=?',
                (os.getpid(), '00000000-0000-0000-0000-000000000000:1', old))
        self.parent = self.start_parent()
        self.wait(lambda: request(self.socket, 'status')['supervisor_alive'])
        self.assertEqual({'runner': old, 'state': 'queued'}, request(self.socket, 'stage',
            {'operation': 'original-owner-operation', 'spec': value}))
        self.wait(lambda: request(self.socket, 'status')['recovery'][0]['failures'] >= 2)
        status = request(self.socket, 'status')
        attempts = [row for row in status['runners'] if row['restart_of']]
        self.assertGreaterEqual(len(attempts), 2)
        self.assertGreaterEqual(attempts[1]['created'] - attempts[0]['created'], .35)
        self.assertLessEqual(len(attempts), 3)
        self.assertTrue(status['supervisor_alive'])
        self.assertTrue(self.row(old)['death_confirmed'])
        self.assertEqual({'id': original, 'epoch': 1}, self.row(old)['work']['task'])
        fail.unlink()
        recovered = self.wait(lambda: next((row for row in request(self.socket, 'status')['runners']
            if row['id'] != old and row['state'] == 'active'), None))
        self.wait(lambda: effects.read_text().splitlines() == ['original', 'independent'])
        self.assertNotEqual(old_pid, recovered['pid'])
        self.assertNotEqual(old, recovered['id'])
        with authority.transaction() as db:
            retained = db.execute('SELECT * FROM tasks WHERE id=?', (original,)).fetchone()
            self.assertEqual(('needs_review', 1), (retained['status'], retained['epoch']))
            checkpoint = json.loads(retained['checkpoint'])
            self.assertTrue(checkpoint['side_effect_started'])
            self.assertEqual(('original-operation', 'original-thread'),
                (checkpoint['operation_id'], checkpoint['thread_id']))
            self.assertEqual('completed', db.execute('SELECT status FROM tasks WHERE id=?',
                                                   (independent,)).fetchone()[0])
        self.assertTrue(any(op['kind'] == 'recover' for op in request(self.socket, 'status')['operations']))

    def row_offline(self, runner):
        db = sqlite3.connect(str(self.state / 'runtime.sqlite3'))
        db.row_factory = sqlite3.Row
        try:
            return dict(db.execute('SELECT * FROM runners WHERE id=?', (runner,)).fetchone())
        finally:
            db.close()

    def test_unknown_launch_real_peer_can_reconnect_without_clearing_work_or_timeout_restart(self):
        old_marker = self.root / 'old.marker'
        old = self.launch('old-source', spec(self.root, autorestart=True,
            restart_backoff={'initial': .2, 'maximum': .8},
            argv=[sys.executable, '-c', CHILD, str(old_marker)], env={'PYTHONPATH': str(ROOT)}))
        self.activate(old, 'active-old')
        self.wait(old_marker.exists)
        started, ready, pause = [self.root / name for name in ('started', 'ready', 'pause')]
        value = spec(self.root, 'unknown-candidate', autorestart=True,
            argv=[sys.executable, '-c', UNKNOWN_CHILD, str(started), str(ready), str(pause)],
            env={'PYTHONPATH': str(ROOT)})
        unknown = self.launch('unknown-original-operation', value)
        self.wait(started.exists)
        peer_pid = int(started.read_text())
        self.extra_children[peer_pid] = LinuxProcess.identity(peer_pid)
        old_pid, old_birth = self.row(old)['pid'], self.row_offline(old)['birth']
        self.parent.terminate(); self.parent.wait(timeout=5)
        os.kill(old_pid, signal.SIGTERM)
        self.wait(lambda: not LinuxProcess.alive(old_pid, old_birth))
        work = {'phase': 'unknown', 'task': {'id': 'original-held-task', 'epoch': 7}}
        with RuntimeStore(self.state).transaction() as db:
            db.execute("UPDATE runners SET pid=NULL,birth=NULL,state='retained',note='launch_outcome_unknown',work=? WHERE id=?",
                       (json.dumps(work), unknown))
        self.parent = self.start_parent()
        self.wait(lambda: request(self.socket, 'status')['supervisor_alive'])
        token = self.row_offline(unknown)['token']
        with self.assertRaisesRegex(ValueError, 'process_mismatch'):
            request(self.socket, 'ready', {'pid': peer_pid, 'work': None}, unknown, token)
        self.assertEqual(work, self.row(unknown)['work'])
        ready.touch()
        self.wait(lambda: self.row(unknown)['ready'])
        self.assertEqual(peer_pid, self.row(unknown)['pid'])
        self.assertEqual(work, self.row(unknown)['work'])
        pause.touch()
        time.sleep(.15)
        with RuntimeStore(self.state).transaction() as db:
            db.execute('UPDATE runners SET heartbeat=? WHERE id=?', (time.time() - 100, unknown))
        self.wait(lambda: self.row(unknown)['state'] == 'retained')
        status = request(self.socket, 'status')
        self.assertEqual(2, len(status['runners']))
        self.assertEqual('alive', self.row(unknown)['process_observation'])
        self.assertFalse(self.row(unknown)['death_confirmed'])
        self.assertEqual(work, self.row(unknown)['work'])
        self.assertFalse(any(op['kind'] == 'recover' for op in status['operations']))


if __name__ == '__main__':
    unittest.main()
