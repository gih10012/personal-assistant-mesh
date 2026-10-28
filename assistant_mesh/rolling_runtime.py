"""Model-independent local process supervisor (Linux, Python >= 3.8).

This is a control journal, not another task/effect ledger. Business claims,
sessions and receipts remain at their existing authority. Nothing imports a
model, reads an account, starts iLink or retries an uncertain business action.
"""
import argparse
import contextlib
import fcntl
import json
import math
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
        return cls.observe(pid, identity) == 'alive'

    @staticmethod
    def observe(pid, identity):
        """Distinguish confirmed death from unavailable process observation."""
        if not pid or not identity:
            return 'unknown'
        try:
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            previous_boot, ticks = identity.rsplit(':', 1)
            if len(boot) != 36 or len(previous_boot) != 36 or not ticks.isdigit():
                return 'unknown'
            if boot != previous_boot:
                return 'dead'
            try:
                raw = Path('/proc/{}/stat'.format(pid)).read_text()
            except FileNotFoundError:
                return 'dead'
            fields = raw[raw.rindex(')') + 2:].split()
            if fields[0] in ('Z', 'X') or fields[19] != ticks:
                return 'dead'
            return 'alive'
        except (OSError, ValueError, IndexError):
            return 'unknown'

    @staticmethod
    def matches_spec(pid, spec):
        """Unknown-launch recovery requires actual kernel observations.

        Direct executable/interpreter argv are supported. If an exec wrapper
        changes these observations, retain unknown instead of guessing identity.
        """
        try:
            root = Path('/proc/{}'.format(pid))
            argv = (root / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
            return (argv == [os.fsencode(value) for value in spec['argv']]
                    and Path(os.readlink(str(root / 'cwd'))).resolve() == Path(spec['cwd']).resolve()
                    and Path(os.readlink(str(root / 'exe'))).resolve() == Path(spec['argv'][0]).resolve())
        except (OSError, ValueError, KeyError):
            return False

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
    restart = spec.get('restart_backoff', {'initial': 1, 'maximum': 60})
    if (type(spec.get('autorestart', True)) is not bool or not isinstance(restart, dict)
            or set(restart) != {'initial', 'maximum'} or any(
                type(restart.get(key)) not in (int, float) or not math.isfinite(restart[key])
                for key in ('initial', 'maximum'))
            or not 0.2 <= restart['initial'] <= restart['maximum'] <= 60):
        raise ValueError('runtime_restart_policy_invalid')
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
                work TEXT, note TEXT, last_checkpoint TEXT, restart_of TEXT,
                death_confirmed INTEGER NOT NULL DEFAULT 0)''')
            # Additive only: adjacent deployments retain the same rows, IDs,
            # protocol and schema major. No restore over a running authority.
            if 'last_checkpoint' not in {r[1] for r in db.execute('PRAGMA table_info(runners)')}:
                db.execute('ALTER TABLE runners ADD COLUMN last_checkpoint TEXT')
            for name, definition in (('restart_of', 'TEXT'), ('death_confirmed', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in {r[1] for r in db.execute('PRAGMA table_info(runners)')}:
                    db.execute('ALTER TABLE runners ADD COLUMN {} {}'.format(name, definition))
            db.execute('''CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, request TEXT NOT NULL,
                result TEXT NOT NULL, created REAL NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS supervisor (pid INTEGER, birth TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS recovery (
                component TEXT PRIMARY KEY, source TEXT NOT NULL, enabled INTEGER NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0, next_attempt REAL, pending TEXT,
                activated_at REAL)''')
            for row in db.execute("SELECT * FROM runners WHERE state='active'").fetchall():
                if db.execute('SELECT 1 FROM recovery WHERE component=?', (row['component'],)).fetchone() is None:
                    self._record_active(db, row)
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

    @staticmethod
    def _record_active(db, row):
        enabled = json.loads(row['spec']).get('autorestart', True)
        db.execute('''INSERT OR REPLACE INTO recovery
            (component,source,enabled,failures,next_attempt,pending,activated_at)
            VALUES (?,?,?,0,NULL,NULL,?)''', (row['component'], row['id'], int(enabled), time.time()))

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
            self._record_active(db, candidate)
            return {'runner': runner, 'active': True, 'previous': old}
        return self.operation(operation, 'activate', {'runner': runner}, apply)

    def drain(self, operation, runner):
        def apply(db):
            row = self._runner(db, runner)
            db.execute('''UPDATE recovery SET enabled=0,next_attempt=NULL
                WHERE component=? AND (source=? OR pending=? OR ?='active')''',
                (row['component'], runner, runner, row['state']))
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
            if (not identity or (row['pid'] is not None and row['pid'] != peer_pid)
                    or (row['birth'] is not None and row['birth'] != identity)):
                raise ValueError('runtime_runner_process_mismatch')
            if action == 'ready':
                recoverable = (row['state'] == 'retained' and row['note'] in
                    ('launch_outcome_unknown', 'process_identity_unknown'))
                if (row['pid'] is None or row['birth'] is None) and not LinuxProcess.matches_spec(
                        peer_pid, json.loads(row['spec'])):
                    raise ValueError('runtime_runner_process_mismatch')
                if row['ready'] and row['state'] in LIVE_STATES:
                    db.execute('UPDATE runners SET pid=?,birth=?,heartbeat=? WHERE id=?',
                               (peer_pid, identity, time.time(), runner))
                elif row['state'] not in ('starting', 'standby') and not recoverable:
                    raise ValueError('runtime_ready_state_invalid')
                else:
                    # Work/last_checkpoint never comes from a ready JSON body.
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
            for key, default in (('restart_of', None), ('death_confirmed', 0)):
                item[key] = row[key] if key in row.keys() else default
            item['process_observation'] = LinuxProcess.observe(row['pid'], row['birth'])
            item['alive'] = LinuxProcess.alive(row['pid'], row['birth'])
            item['work'] = json.loads(row['work']) if row['work'] else None
            item['claim_admission'] = parent_alive and row['state'] == 'active' and item['alive'] and item['work'] is None
            runners.append(item)
        operations = [{key: row[key] for key in ('id', 'kind', 'created')}
                      for row in db.execute('SELECT * FROM operations ORDER BY created DESC LIMIT 50')]
        recovery = ([dict(row) for row in db.execute('SELECT * FROM recovery ORDER BY component')]
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery'").fetchone() else [])
        return {'protocol': PROTOCOL, 'schema': 1, 'runners': runners,
            'operations': operations, 'recovery': recovery,
            'supervisor_alive': parent_alive, 'model_availability': 'not_checked',
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
                db.execute("UPDATE runners SET state='failed',death_confirmed=1,note='launch_failed' WHERE id=?", (row['id'],))

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
            RuntimeStore._record_active(db, previous)

    def _recover(self, db, now):
        for recovery in db.execute('SELECT * FROM recovery WHERE enabled=1').fetchall():
            rows = db.execute('SELECT * FROM runners WHERE component=? ORDER BY created',
                              (recovery['component'],)).fetchall()
            pending = next((row for row in rows if row['id'] == recovery['pending']), None)
            if pending is not None:
                if (pending['state'] == 'standby' and pending['ready'] and not pending['work']
                        and LinuxProcess.alive(pending['pid'], pending['birth'])
                        and pending['heartbeat'] > now - 10
                        and all(row['id'] == pending['id'] or row['state'] == 'stopped'
                            or row['death_confirmed'] or LinuxProcess.observe(row['pid'], row['birth']) == 'dead'
                            for row in rows)):
                    db.execute("UPDATE runners SET state='active',note='automatic_recovery' WHERE id=?", (pending['id'],))
                    # The recovered runner becomes the authorized owner too.
                    # Clearing pending after stability must not lose that owner.
                    db.execute('UPDATE recovery SET source=?,activated_at=? WHERE component=?',
                               (pending['id'], now, recovery['component']))
                    operation = 'recover-activate-' + pending['id']
                    db.execute('INSERT OR IGNORE INTO operations VALUES(?,?,?,?,?)',
                        (operation, 'activate_recovery', _json({'runner': pending['id']}),
                         _json({'runner': pending['id'], 'active': True}), now))
                    continue
                if pending['state'] == 'active':
                    if (recovery['activated_at'] and now - recovery['activated_at'] >= 30
                            and LinuxProcess.alive(pending['pid'], pending['birth'])
                            and pending['heartbeat'] > now - 10):
                        db.execute('''UPDATE recovery SET failures=0,next_attempt=NULL,pending=NULL
                            WHERE component=?''', (recovery['component'],))
                    continue
                if not pending['death_confirmed'] and LinuxProcess.observe(pending['pid'], pending['birth']) != 'dead':
                    continue  # Starting, stale or unknown launch: do not duplicate.
                db.execute('UPDATE recovery SET pending=NULL,next_attempt=NULL WHERE component=?', (recovery['component'],))
            source = self.store._runner(db, recovery['source'])
            # A retained idle owner can reconnect and cooperatively become a
            # standby. Re-admit only that owner, never a staged/old standby or
            # an owner drain. Other possibly live/unknown launches still fence
            # admission; heartbeat timeout alone cannot prove their death.
            if (source['state'] == 'standby' and source['note'] == 'rollback_standby'
                    and source['ready'] and source['work'] is None
                    and LinuxProcess.alive(source['pid'], source['birth'])
                    and source['heartbeat'] is not None and source['heartbeat'] > now - 10
                    and all(row['id'] == source['id'] or row['state'] == 'stopped'
                        or (row['state'] != 'queued' and (row['death_confirmed']
                            or LinuxProcess.observe(row['pid'], row['birth']) == 'dead'))
                        for row in rows)):
                db.execute("UPDATE runners SET state='active',note='automatic_readmission' WHERE id=?", (source['id'],))
                continue
            # Every non-retired process must be CONFIRMED dead. A healthy
            # standby, slow observation, missing PID receipt or unknown launch
            # blocks a replacement; no timer is a process-death proof.
            if any(row['state'] == 'queued' or (
                    row['state'] != 'stopped' and not row['death_confirmed']
                    and LinuxProcess.observe(row['pid'], row['birth']) != 'dead') for row in rows):
                continue
            spec = json.loads(source['spec'])
            timing = spec.get('restart_backoff', {'initial': 1, 'maximum': 60})
            delay = min(timing['maximum'], timing['initial'] * 2 ** min(recovery['failures'], 20))
            deadline = None if pending is not None else recovery['next_attempt']
            if deadline is None:
                db.execute('UPDATE recovery SET next_attempt=? WHERE component=?',
                           (now + delay, recovery['component']))
                continue
            if now < deadline:
                continue
            runner, operation = secrets.token_hex(16), 'recover-' + secrets.token_hex(16)
            previous = rows[-1]['id'] if rows else source['id']
            db.execute('''INSERT INTO runners
                (id,component,version,spec,token,state,created,restart_of)
                VALUES (?,?,?,?,?,'queued',?,?)''', (runner, source['component'], source['version'],
                    source['spec'], secrets.token_hex(32), now, previous))
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?)',
                (operation, 'recover', _json({'source': source['id'], 'previous': previous}),
                 _json({'runner': runner, 'state': 'queued'}), now))
            db.execute('''UPDATE recovery SET failures=failures+1,next_attempt=NULL,pending=?,activated_at=NULL
                WHERE component=?''', (runner, recovery['component']))

    def reconcile(self):
        self._launch_one()
        now = time.time()
        with self.store.transaction() as db:
            for row in db.execute("SELECT * FROM runners WHERE state IN ('starting','standby','active','draining','retained')").fetchall():
                child = self.children.get(row['id'])
                exited = child is not None and child.poll() is not None and row['pid'] in (None, child.pid)
                observation = 'dead' if exited else LinuxProcess.observe(row['pid'], row['birth'])
                if observation == 'dead':
                    state, note = 'failed', 'process_exited_work_retained' if row['work'] else 'process_exited'
                elif row['pid'] is None:
                    if now - (row['heartbeat'] or row['created']) <= self.ready_timeout:
                        continue
                    state, note = 'retained', 'launch_outcome_unknown'
                elif observation == 'unknown':
                    state, note = 'retained', 'process_identity_unknown'
                elif not row['ready'] and now - (row['heartbeat'] or row['created']) > self.ready_timeout:
                    state, note = 'retained', 'ready_timeout_process_retained'
                elif row['ready'] and now - row['heartbeat'] > self.heartbeat_timeout:
                    state, note = 'retained', 'heartbeat_stale_process_retained'
                else:
                    continue
                db.execute('UPDATE runners SET state=?,note=?,death_confirmed=? WHERE id=?',
                           (state, note, int(observation == 'dead'), row['id']))
                self._fallback(db, row)
            self._recover(db, now)
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
