"""Model-independent local process supervisor (Linux, Python >= 3.8).

This is a control journal, not another task/effect ledger. Business claims,
sessions and receipts remain at their existing authority. Nothing imports a
model, reads an account, starts iLink or retries an uncertain business action.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time


PROTOCOL = 1
MAX_MESSAGE = 256 * 1024
LIVE_STATES = ('starting', 'standby', 'active', 'draining', 'retained')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _name(value):
    if (not isinstance(value, str) or not value or len(value) > 160
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise ValueError('runtime_identity_invalid')
    return value


def _error(error):
    code = str(error)
    if code.startswith('runtime_') and len(code) <= 100 and all(c.islower() or c == '_' for c in code):
        return code
    return 'runtime_request_rejected'


def private_directory(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path,) + tuple(path.parents)):
        raise ValueError('runtime_directory_symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    found = path.stat()
    if found.st_uid != os.getuid() or stat.S_IMODE(found.st_mode) != 0o700:
        raise ValueError('runtime_directory_requires_owned_0700')
    return path


class LinuxProcess:
    """PID identity only; no timeout is treated as death or permission to kill.

    Another OS adapter must implement identity/alive/launch with a stable birth
    identity and detached child ownership. It must never infer quiescence from
    PID existence, signal a process group, or auto-relaunch uncertain work.
    """
    @staticmethod
    def identity(pid):
        try:
            raw = Path('/proc/{}/stat'.format(pid)).read_text()
            fields = raw[raw.rindex(')') + 2:].split()
            if fields[0] == 'Z':
                return None
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return '{}:{}'.format(boot, fields[19])
        except (OSError, ValueError, IndexError):
            return None

    @classmethod
    def alive(cls, pid, identity):
        return bool(pid and identity and cls.identity(pid) == identity)

    @staticmethod
    def launch(spec, env, log):
        return subprocess.Popen(spec['argv'], cwd=spec['cwd'], env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True)


def validate_spec(spec):
    if not isinstance(spec, dict) or spec.get('kind') != 'worker':
        raise ValueError('runtime_worker_adapter_required')
    for key in ('component', 'version'):
        _name(spec.get(key))
    if (type(spec.get('protocol_min')) is not int
            or type(spec.get('protocol_max')) is not int
            or not spec['protocol_min'] <= PROTOCOL <= spec['protocol_max']):
        raise ValueError('runtime_protocol_incompatible')
    argv, cwd, env = spec.get('argv'), spec.get('cwd'), spec.get('env', {})
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(a, str) or not a or '\0' in a for a in argv)
            or not Path(argv[0]).is_absolute() or not isinstance(cwd, str)
            or not Path(cwd).is_absolute() or not Path(cwd).is_dir()
            or not isinstance(env, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) or not k
                or '=' in k or '\0' in k + v or k.startswith('AMITA_')
                for k, v in env.items())):
        raise ValueError('runtime_launch_spec_invalid')
    return json.loads(_json(spec))


class RuntimeStore:
    def __init__(self, directory):
        self.directory = private_directory(directory)
        self.path = self.directory / 'runtime.sqlite3'
        if self.path.is_symlink():
            raise ValueError('runtime_database_symlink')
        with self.transaction() as db:
            schema = db.execute('PRAGMA user_version').fetchone()[0]
            if schema not in (0, 1):
                raise ValueError('runtime_schema_incompatible')
            db.execute('''CREATE TABLE IF NOT EXISTS runners (
                id TEXT PRIMARY KEY, component TEXT NOT NULL, version TEXT NOT NULL,
                spec TEXT NOT NULL, token TEXT NOT NULL, state TEXT NOT NULL,
                pid INTEGER, birth TEXT, ready INTEGER NOT NULL DEFAULT 0,
                heartbeat REAL, created REAL NOT NULL, previous TEXT,
                work TEXT, note TEXT, last_checkpoint TEXT)''')
            # Additive only: adjacent deployments retain the same rows, IDs,
            # protocol and schema major. No restore over a running authority.
            if 'last_checkpoint' not in {r[1] for r in db.execute('PRAGMA table_info(runners)')}:
                db.execute('ALTER TABLE runners ADD COLUMN last_checkpoint TEXT')
            db.execute('''CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, request TEXT NOT NULL,
                result TEXT NOT NULL, created REAL NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS supervisor (pid INTEGER, birth TEXT)')
            db.execute('PRAGMA user_version=1')
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def transaction(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _runner(db, runner):
        row = db.execute('SELECT * FROM runners WHERE id=?', (_name(runner),)).fetchone()
        if row is None:
            raise ValueError('runtime_runner_missing')
        return row

    def operation(self, operation, kind, request, apply):
        _name(operation)
        encoded = _json(request)
        with self.transaction() as db:
            old = db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone()
            if old:
                if old['kind'] != kind or old['request'] != encoded:
                    raise ValueError('runtime_operation_conflict')
                return json.loads(old['result'])
            result = apply(db)
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?)',
                (operation, kind, encoded, _json(result), time.time()))
            return result

    def stage(self, operation, spec):
        spec = validate_spec(spec)

        def apply(db):
            runner = secrets.token_hex(16)
            db.execute('''INSERT INTO runners
                (id,component,version,spec,token,state,created) VALUES (?,?,?,?,?,'queued',?)''',
                (runner, spec['component'], spec['version'], _json(spec),
                 secrets.token_hex(32), time.time()))
            return {'runner': runner, 'state': 'queued'}
        return self.operation(operation, 'stage', spec, apply)

    def activate(self, operation, runner):
        def apply(db):
            candidate = self._runner(db, runner)
            if (candidate['state'] != 'standby' or not candidate['ready']
                    or not LinuxProcess.alive(candidate['pid'], candidate['birth'])
                    or candidate['heartbeat'] is None
                    or candidate['heartbeat'] < time.time() - 10):
                raise ValueError('runtime_candidate_not_ready')
            previous = db.execute("SELECT id FROM runners WHERE component=? AND state='active'",
                                  (candidate['component'],)).fetchone()
            db.execute("UPDATE runners SET state='draining',note='upgrade' WHERE component=? AND state='active'",
                       (candidate['component'],))
            old = previous['id'] if previous else None
            db.execute("UPDATE runners SET state='active',previous=?,note=NULL WHERE id=?", (old, runner))
            return {'runner': runner, 'active': True, 'previous': old}
        return self.operation(operation, 'activate', {'runner': runner}, apply)

    def drain(self, operation, runner):
        def apply(db):
            row = self._runner(db, runner)
            if row['state'] in LIVE_STATES + ('queued',):
                state = 'stopped' if row['state'] == 'queued' else 'draining'
                db.execute('UPDATE runners SET state=?,note=? WHERE id=?', (state, 'owner_drain', runner))
            return {'runner': runner, 'claim_admission': False}
        return self.operation(operation, 'drain', {'runner': runner}, apply)

    def runner_request(self, runner, token, action, payload, peer_pid):
        with self.transaction() as db:
            row = self._runner(db, runner)
            if not isinstance(token, str) or not secrets.compare_digest(row['token'], token):
                raise ValueError('runtime_runner_auth_failed')
            identity = LinuxProcess.identity(peer_pid)
            if (not identity or (row['pid'] is not None and
                    (row['pid'] != peer_pid or row['birth'] != identity))):
                raise ValueError('runtime_runner_process_mismatch')
            if action == 'ready':
                if row['state'] not in ('starting', 'standby'):
                    raise ValueError('runtime_ready_state_invalid')
                db.execute('''UPDATE runners SET pid=?,birth=?,ready=1,
                    state='standby',heartbeat=? WHERE id=?''', (peer_pid, identity, time.time(), runner))
            else:
                if not row['ready'] or row['state'] not in LIVE_STATES:
                    raise ValueError('runtime_runner_not_registered')
                db.execute('UPDATE runners SET heartbeat=? WHERE id=?', (time.time(), runner))
                work = json.loads(row['work']) if row['work'] else None
                if action == 'admit':
                    if work and work.get('phase') == 'claiming' and work.get('claim') == payload.get('claim'):
                        return {'admitted': True, 'claim': work['claim']}
                    if row['state'] != 'active' or work is not None:
                        return {'admitted': False, 'state': row['state']}
                    claim = _name(payload.get('claim'))
                    db.execute('UPDATE runners SET work=? WHERE id=?',
                        (_json({'phase': 'claiming', 'claim': claim}), runner))
                    return {'admitted': True, 'claim': claim}
                elif action == 'bind':
                    if not work or work.get('phase') != 'claiming' or payload.get('claim') != work['claim']:
                        raise ValueError('runtime_claim_mismatch')
                    if payload.get('task') is None:
                        db.execute('UPDATE runners SET work=NULL WHERE id=?', (runner,))
                    else:
                        task = payload['task']
                        _name(task.get('id'))
                        if type(task.get('epoch')) is not int or task['epoch'] < 1:
                            raise ValueError('runtime_task_identity_invalid')
                        db.execute('UPDATE runners SET work=? WHERE id=?',
                            (_json({'phase': 'running', 'task': task}), runner))
                elif action == 'unknown':
                    if work:
                        work['phase'] = 'unknown'
                        db.execute('UPDATE runners SET work=?,note=? WHERE id=?',
                            (_json(work), 'receipt_reconciliation_required', runner))
                elif action == 'checkpoint':
                    task = payload.get('task')
                    if (payload.get('safe') is True and isinstance(task, dict)
                            and row['last_checkpoint'] == _json(task)):
                        return {'state': row['state'], 'busy': row['work'] is not None, 'protocol': PROTOCOL}
                    if (not work or work.get('phase') != 'running' or not isinstance(task, dict)
                            or task.get('id') != work['task']['id']
                            or task.get('epoch') != work['task']['epoch']):
                        raise ValueError('runtime_checkpoint_identity_mismatch')
                    # The trusted adapter reports only AFTER authority ACK,
                    # native seal/session save, and return from run_once.
                    if payload.get('safe') is not True:
                        raise ValueError('runtime_safe_checkpoint_required')
                    db.execute('UPDATE runners SET work=NULL,last_checkpoint=? WHERE id=?', (_json(task), runner))
                elif action == 'quiescent':
                    if work is not None or row['state'] not in ('draining', 'retained'):
                        raise ValueError('runtime_runner_not_quiescent')
                    state = 'stopped' if row['note'] == 'owner_drain' else 'standby'
                    db.execute('UPDATE runners SET state=?,note=? WHERE id=?',
                        (state, 'cooperative_exit' if state == 'stopped' else 'rollback_standby', runner))
                elif action == 'stopped':
                    if work is not None or row['state'] not in ('draining', 'retained'):
                        raise ValueError('runtime_runner_not_quiescent')
                    db.execute("UPDATE runners SET state='stopped',note='cooperative_exit' WHERE id=?", (runner,))
                elif action != 'heartbeat':
                    raise ValueError('runtime_action_unknown')
            found = self._runner(db, runner)
            return {'state': found['state'], 'busy': found['work'] is not None, 'protocol': PROTOCOL}

    def status(self):
        # Also usable when the supervisor/runner is down. No model/account IO.
        return read_status(self.path)


def read_status(path):
    path = Path(path)
    db = sqlite3.connect('file:{}?mode=ro'.format(path.as_posix()), uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        parent = db.execute('SELECT * FROM supervisor LIMIT 1').fetchone()
        parent_alive = bool(parent and LinuxProcess.alive(parent['pid'], parent['birth']))
        runners = []
        for row in db.execute('SELECT * FROM runners ORDER BY created'):
            item = {key: row[key] for key in ('id', 'component', 'version', 'state', 'pid',
                'ready', 'heartbeat', 'created', 'previous', 'note')}
            item['alive'] = LinuxProcess.alive(row['pid'], row['birth'])
            item['work'] = json.loads(row['work']) if row['work'] else None
            item['claim_admission'] = parent_alive and row['state'] == 'active' and item['alive'] and item['work'] is None
            runners.append(item)
        operations = [{key: row[key] for key in ('id', 'kind', 'created')}
                      for row in db.execute('SELECT * FROM operations ORDER BY created DESC LIMIT 50')]
        return {'protocol': PROTOCOL, 'schema': 1, 'runners': runners,
            'operations': operations, 'supervisor_alive': parent_alive, 'model_availability': 'not_checked',
            'business_queue': 'existing_authority', 'receiver_started': False}
    finally:
        db.close()


class Supervisor:
    def __init__(self, directory, heartbeat_timeout=15, ready_timeout=15):
        if sys.platform != 'linux':
            raise ValueError('runtime_linux_adapter_required')
        self.store = RuntimeStore(directory)
        self.socket_path = str(self.store.directory / 'control.sock')
        if len(os.fsencode(self.socket_path)) > 103:
            raise ValueError('runtime_socket_path_too_long')
        self.heartbeat_timeout, self.ready_timeout = heartbeat_timeout, ready_timeout
        self.stop = threading.Event()
        self.children = {}
        self.server = None
        self.lock = None

    def start(self):
        self.lock = (self.store.directory / 'supervisor.lock').open('a+')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            self.lock = None
            raise ValueError('runtime_supervisor_already_running') from None
        if os.path.lexists(self.socket_path):
            found = os.lstat(self.socket_path)
            if not stat.S_ISSOCK(found.st_mode) or found.st_uid != os.getuid():
                raise ValueError('runtime_socket_unsafe')
            os.unlink(self.socket_path)
        supervisor = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(5)
                pid, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    return
                raw = self.rfile.readline(MAX_MESSAGE + 1)
                try:
                    if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
                        raise ValueError('runtime_message_invalid')
                    request = json.loads(raw.decode('utf8'))
                    response = {'ok': True, 'result': supervisor.dispatch(request, pid)}
                except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
                    response = {'ok': False, 'error': _error(error)}
                self.wfile.write((_json(response) + '\n').encode('utf8'))

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        self.server = Server(self.socket_path, Handler)
        os.chmod(self.socket_path, 0o600)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        with self.store.transaction() as db:
            db.execute('DELETE FROM supervisor')
            db.execute('INSERT INTO supervisor VALUES (?,?)', (os.getpid(), LinuxProcess.identity(os.getpid())))
        return self

    def dispatch(self, request, peer_pid):
        if request.get('protocol') != PROTOCOL:
            raise ValueError('runtime_protocol_incompatible')
        action, body = request['action'], request.get('body', {})
        if request.get('runner') is not None:
            return self.store.runner_request(request['runner'], request.get('token'), action, body, peer_pid)
        if action == 'status':
            return self.store.status()
        if action == 'stage':
            return self.store.stage(body['operation'], body['spec'])
        if action == 'activate':
            return self.store.activate(body['operation'], body['runner'])
        if action == 'drain':
            return self.store.drain(body['operation'], body['runner'])
        raise ValueError('runtime_action_unknown')

    def _launch_one(self):
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM runners WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return
            # Commit intent before Popen. A crash in this window is UNKNOWN,
            # never an automatic second launch of the same command.
            db.execute("UPDATE runners SET state='starting',heartbeat=? WHERE id=?", (time.time(), row['id']))
        spec = json.loads(row['spec'])
        env = dict(os.environ, **spec.get('env', {}))
        env.update(AMITA_RUNTIME_SOCKET=self.socket_path, AMITA_RUNNER_ID=row['id'], AMITA_RUNNER_TOKEN=row['token'])
        try:
            with (self.store.directory / (row['id'] + '.log')).open('ab') as log:
                child = LinuxProcess.launch(spec, env, log)
            self.children[row['id']] = child
            birth = LinuxProcess.identity(child.pid)
            with self.store.transaction() as db:
                current = self.store._runner(db, row['id'])
                if current['pid'] is None:
                    db.execute('UPDATE runners SET pid=?,birth=? WHERE id=?', (child.pid, birth, row['id']))
        except OSError:
            with self.store.transaction() as db:
                db.execute("UPDATE runners SET state='failed',note='launch_failed' WHERE id=?", (row['id'],))

    @staticmethod
    def _fallback(db, row):
        if row['state'] != 'active' or not row['previous']:
            return
        previous = db.execute('SELECT * FROM runners WHERE id=?', (row['previous'],)).fetchone()
        if (previous and previous['ready'] and previous['state'] in ('draining', 'standby')
                and LinuxProcess.alive(previous['pid'], previous['birth'])
                and previous['heartbeat'] and previous['heartbeat'] > time.time() - 10
                and previous['note'] != 'owner_drain'):
            db.execute("UPDATE runners SET state='active',note='automatic_rollback' WHERE id=?", (previous['id'],))

    def reconcile(self):
        self._launch_one()
        now = time.time()
        with self.store.transaction() as db:
            for row in db.execute("SELECT * FROM runners WHERE state IN ('starting','standby','active','draining','retained')").fetchall():
                alive = LinuxProcess.alive(row['pid'], row['birth'])
                if row['pid'] is None:
                    if now - (row['heartbeat'] or row['created']) <= self.ready_timeout:
                        continue
                    state, note = 'retained', 'launch_outcome_unknown'
                elif not alive:
                    state, note = 'failed', 'process_exited_work_retained' if row['work'] else 'process_exited'
                elif not row['ready'] and now - (row['heartbeat'] or row['created']) > self.ready_timeout:
                    state, note = 'retained', 'ready_timeout_process_retained'
                elif row['ready'] and now - row['heartbeat'] > self.heartbeat_timeout:
                    state, note = 'retained', 'heartbeat_stale_process_retained'
                else:
                    continue
                db.execute('UPDATE runners SET state=?,note=? WHERE id=?', (state, note, row['id']))
                self._fallback(db, row)
        for runner, child in list(self.children.items()):
            if child.poll() is not None:
                del self.children[runner]

    def run(self):
        self.start()
        try:
            while not self.stop.wait(0.2):
                self.reconcile()
        finally:
            self.close()

    def close(self):
        self.stop.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        if self.lock:
            with self.store.transaction() as db:
                db.execute('DELETE FROM supervisor WHERE pid=?', (os.getpid(),))
            self.lock.close()
            self.lock = None
        # Deliberately leave every runner/native process alive. A restarted
        # parent reconnects using the same durable runner/PID birth identities.


def request(socket_path, action, body=None, runner=None, token=None):
    path = Path(socket_path)
    parent, found = path.parent.stat(), path.lstat()
    if (path.is_symlink() or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISSOCK(found.st_mode) or found.st_uid != os.getuid()
            or stat.S_IMODE(found.st_mode) != 0o600):
        raise ValueError('runtime_socket_unsafe')
    payload = {'protocol': PROTOCOL, 'action': action, 'body': body or {}}
    if runner:
        payload.update(runner=runner, token=token)
    raw = (_json(payload) + '\n').encode('utf8')
    if len(raw) > MAX_MESSAGE:
        raise ValueError('runtime_message_too_large')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(5)
        channel.connect(str(path))
        channel.sendall(raw)
        with channel.makefile('rb') as response:
            raw = response.readline(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
            raise ValueError('runtime_response_invalid')
        value = json.loads(raw.decode('utf8'))
        if value.get('ok') is not True:
            raise ValueError(value.get('error', 'runtime_request_rejected'))
        return value['result']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, help='private local supervisor directory')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('serve')
    sub.add_parser('status')
    for command in ('stage', 'roll', 'activate', 'drain'):
        child = sub.add_parser(command)
        child.add_argument('--operation-id', required=True)
        if command in ('stage', 'roll'):
            child.add_argument('--spec', required=True, help='owned 0600 JSON launch spec')
            if command == 'roll':
                child.add_argument('--ready-timeout', type=float, default=20)
        else:
            child.add_argument('--runner', required=True)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('command required')
    try:
        directory = Path(args.state).absolute()
        endpoint = str(directory / 'control.sock')
        if args.command == 'serve':
            import signal
            supervisor = Supervisor(directory)
            signal.signal(signal.SIGTERM, lambda *_: supervisor.stop.set())
            signal.signal(signal.SIGINT, lambda *_: supervisor.stop.set())
            supervisor.run()
            return 0
        if args.command == 'status':
            result = read_status(directory / 'runtime.sqlite3')
        elif args.command in ('stage', 'roll'):
            from .config import private_json
            result = request(endpoint, 'stage', {'operation': args.operation_id, 'spec': private_json(args.spec)})
            if args.command == 'roll':
                deadline = time.monotonic() + max(0, min(args.ready_timeout, 300))
                while True:
                    status = request(endpoint, 'status')
                    row = next(r for r in status['runners'] if r['id'] == result['runner'])
                    if row['state'] in ('standby', 'active'):
                        result = request(endpoint, 'activate', {'operation': args.operation_id + ':activate', 'runner': result['runner']})
                        break
                    if row['state'] in ('failed', 'retained', 'stopped') or time.monotonic() >= deadline:
                        raise ValueError('runtime_candidate_not_ready_previous_unchanged')
                    time.sleep(0.1)
        else:
            result = request(endpoint, args.command, {'operation': args.operation_id, 'runner': args.runner})
        print(_json(result))
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print(_json({'ok': False, 'error': _error(error),
            'automatic_business_retry': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
