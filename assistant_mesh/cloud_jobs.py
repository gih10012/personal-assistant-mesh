"""Private, durable adapter for the official `codex cloud` CLI.

One CLI launch per intent, NOT provider exactly-once delivery. No cloud jobs are
created on construction/import; only submit(..., authorized=True) may launch
exec. The integrating authority must check actual owner/task authorization.
This module has no Store, HTTP, enrollment, native-history or apply integration.
"""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
from collections import namedtuple
from pathlib import Path
from urllib.parse import urlsplit

from .store import Conflict


PROTOCOL = 'codex-cloud-job/1'
CommandResult = namedtuple('CommandResult', 'returncode stdout stderr')
_PROVIDER_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_REF = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._/-]{0,255}\Z')
_STATUSES = frozenset(('pending', 'ready', 'applied', 'error'))
_FIELDS = frozenset(('mesh_task_id', 'mesh_epoch', 'request_id', 'environment_id', 'branch', 'prompt'))
_STATES = frozenset(('intent', 'submitting', 'submission_unknown', 'submitted'))


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _digest(value):
    return hashlib.sha256(_encoded(value).encode('utf8')).hexdigest()


def _text(value, label, maximum=512):
    try:
        valid = (isinstance(value, str) and bool(value.strip())
                 and len(value.encode('utf8')) <= maximum
                 and all(ord(char) >= 32 for char in value))
    except UnicodeError:
        valid = False
    if not valid:
        raise ValueError('invalid_cloud_' + label)
    return value


def _provider_url(value):
    """Only the official CLI's canonical task URL, never fetch this URL."""
    _text(value, 'provider_url', 512)
    try:
        parsed = urlsplit(value)
        prefix = '/codex/tasks/'
        provider = parsed.path[len(prefix):] if parsed.path.startswith(prefix) else ''
        if (parsed.scheme != 'https' or parsed.netloc != 'chatgpt.com'
                or parsed.query or parsed.fragment or '?' in value or '#' in value
                or not _PROVIDER_ID.fullmatch(provider)):
            raise ValueError('unsafe')
    except ValueError:
        raise ValueError('invalid_cloud_provider_url') from None
    if value != 'https://chatgpt.com/codex/tasks/' + provider:
        raise ValueError('invalid_cloud_provider_url')
    return provider


def _private_dir(path, create=False):
    """POSIX example: Windows needs an equivalent private ACL installer."""
    path = Path(path).absolute()
    if os.name != 'posix':
        raise ValueError('cloud_jobs_private_acl_adapter_required')
    for parent in (path,) + tuple(path.parents):
        if parent.is_symlink():
            raise ValueError('cloud_private_directory_symlink')
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    item = path.lstat()
    if (not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid()
            or stat.S_IMODE(item.st_mode) != 0o700):
        raise ValueError('cloud_private_directory_required')
    return path


def _private_file(path, create=False):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    if create:
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    fd = os.open(str(path), flags)
    try:
        item = os.fstat(fd)
        if (not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid()
                or item.st_nlink != 1 or stat.S_IMODE(item.st_mode) != 0o600):
            raise ValueError('cloud_private_regular_file_required')
    finally:
        os.close(fd)


class CommandFailure(RuntimeError):
    """Fixed category only; exceptions must not reflect prompts/auth/stderr."""
    pass


def run_cli(argv, *, input, cwd, env, timeout, max_bytes):
    """No shell, no prompt in argv, private temporary stdout/stderr.

    A timeout kills ONLY this launched CLI, not a Cloud task. A task might have
    been accepted before the CLI dies; the adapter preserves unknown delivery.
    """
    with tempfile.TemporaryFile(dir=str(cwd)) as output, tempfile.TemporaryFile(dir=str(cwd)) as error:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output,
                                   stderr=error, cwd=str(cwd), env=env, shell=False)
        try:
            process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise CommandFailure('cli_timeout') from None
        except BaseException:
            process.kill()
            process.communicate()
            raise
        output.seek(0)
        error.seek(0)
        stdout, stderr = output.read(max_bytes + 1), error.read(max_bytes + 1)
        if len(stdout) > max_bytes or len(stderr) > max_bytes:
            raise CommandFailure('cli_output_limit')
        return CommandResult(process.returncode, stdout, stderr)


class CloudJobs:
    """Trusted local library, not an authentication endpoint.

    `environments` is owner configuration {actual_env_id: [explicit_branches]}.
    Runtime binding locks the executable/home selection across journal reopen.
    Same-UID file writers and account changes inside that home are outside this
    boundary; Root must verify owner identity before effectful integration.
    """
    def __init__(self, state_dir, owner_binding, environments, *, executable,
                 codex_home=None, runner=run_cli, clock=time.time, timeout=60,
                 max_bytes=4 * 1024 * 1024):
        self.owner = _text(owner_binding, 'owner_binding')
        if (not isinstance(environments, dict) or not environments
                or type(timeout) not in (int, float) or not 0 < timeout <= 300
                or type(max_bytes) is not int or not 1024 <= max_bytes <= 16 * 1024 * 1024):
            raise ValueError('invalid_cloud_configuration')
        self.environments = {}
        for env_id, branches in environments.items():
            _text(env_id, 'environment_id')
            if not _PROVIDER_ID.fullmatch(env_id) or not isinstance(branches, (list, tuple)) or not branches:
                raise ValueError('invalid_cloud_environment_configuration')
            if any(not isinstance(branch, str) or not _REF.fullmatch(branch)
                   or '..' in branch or branch.endswith(('/', '.')) for branch in branches):
                raise ValueError('invalid_cloud_branch_configuration')
            self.environments[env_id] = frozenset(branches)
        self.executable = _text(executable, 'executable', 4096)
        if not Path(self.executable).is_absolute():
            raise ValueError('invalid_cloud_executable')
        home = codex_home or os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')
        _text(home, 'codex_home', 4096)
        if not Path(home).is_absolute() or not Path(state_dir).is_absolute():
            raise ValueError('invalid_cloud_codex_home')
        self.environment = dict(os.environ)
        # This managed adapter uses native CLI login, not a new API key/proxy.
        for name in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL',
                     'CODEX_CLOUD_TASKS_BASE_URL', 'CODEX_CLOUD_TASKS_MODE'):
            self.environment.pop(name, None)
        self.environment['CODEX_HOME'] = home
        self.environment['NO_COLOR'] = '1'
        self.namespace = {'schema': '1', 'owner': _digest(self.owner),
                          'runtime': _digest([self.executable, home])}
        self.path = _private_dir(state_dir, create=True)
        self.database = self.path / 'cloud-jobs.sqlite'
        _private_file(self.database, create=True)
        self.runs = _private_dir(self.path / 'runs', create=True)
        self.artifacts = _private_dir(self.path / 'artifacts', create=True)
        self.runner, self.clock, self.timeout, self.max_bytes = runner, clock, timeout, max_bytes
        with self._transaction(initialize=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cloud_job_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS cloud_jobs(
                id TEXT PRIMARY KEY,mesh_task_id TEXT NOT NULL UNIQUE,request_id TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL,fingerprint TEXT NOT NULL,state TEXT NOT NULL,
                attempted INTEGER NOT NULL DEFAULT 0,provider_id TEXT UNIQUE,provider_url TEXT,
                receipt_source TEXT,provider_status TEXT,submission_error TEXT,observation_error TEXT,
                observed REAL,created REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS cloud_job_artifacts(
                job_id TEXT NOT NULL,attempt INTEGER NOT NULL,sha256 TEXT NOT NULL,
                bytes INTEGER NOT NULL,filename TEXT NOT NULL,created REAL NOT NULL,
                PRIMARY KEY(job_id,attempt,sha256))''')
            recorded = dict(db.execute('SELECT key,value FROM cloud_job_metadata'))
            if not recorded:
                if (db.execute('SELECT 1 FROM cloud_jobs LIMIT 1').fetchone()
                        or db.execute('SELECT 1 FROM cloud_job_artifacts LIMIT 1').fetchone()):
                    raise Conflict('cloud_job_namespace_missing')
                for key, value in self.namespace.items():
                    db.execute('INSERT INTO cloud_job_metadata VALUES(?,?)', (key, value))
            elif recorded != self.namespace:
                raise Conflict('cloud_job_namespace_mismatch')

    @contextlib.contextmanager
    def _transaction(self, initialize=False):
        _private_dir(self.path)
        _private_file(self.database)
        db = sqlite3.connect(str(self.database), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            if not initialize and dict(db.execute('SELECT key,value FROM cloud_job_metadata')) != self.namespace:
                raise Conflict('cloud_job_namespace_mismatch')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _intent(self, value, configured=True):
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError('invalid_cloud_job_intent')
        value = dict(value)
        for field in ('mesh_task_id', 'request_id', 'environment_id', 'branch'):
            _text(value[field], field)
        if type(value['mesh_epoch']) is not int or value['mesh_epoch'] < 1:
            raise ValueError('invalid_cloud_mesh_epoch')
        prompt = value['prompt']
        try:
            valid = (isinstance(prompt, str) and bool(prompt.strip())
                     and len(prompt.encode('utf8')) <= 65536 and '\x00' not in prompt)
        except UnicodeError:
            valid = False
        if not valid:
            raise ValueError('invalid_cloud_prompt')
        if configured and value['branch'] not in self.environments.get(value['environment_id'], ()):
            raise PermissionError('cloud_environment_branch_not_configured')
        return value

    def _id(self, value):
        return 'cloud-' + _digest([PROTOCOL, self.owner, value['mesh_task_id'], value['request_id']])

    def _row(self, db, job_id):
        row = db.execute('SELECT * FROM cloud_jobs WHERE id=?', (_text(job_id, 'job_id'),)).fetchone()
        if not row:
            raise PermissionError('cloud_job_not_found')
        try:
            body = self._intent(json.loads(row['body']), configured=False)
            valid = (row['body'] == _encoded(body) and row['fingerprint'] == _digest(body)
                     and row['id'] == self._id(body) and row['mesh_task_id'] == body['mesh_task_id']
                     and row['request_id'] == body['request_id'] and row['state'] in _STATES
                     and row['attempted'] in (0, 1)
                     and ((row['state'] == 'intent') == (row['attempted'] == 0)))
            if row['provider_id'] is not None:
                valid = valid and _provider_url(row['provider_url']) == row['provider_id'] and row['state'] == 'submitted'
            else:
                valid = valid and row['provider_url'] is None and row['state'] != 'submitted'
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise Conflict('cloud_job_journal_invalid')
        return row, body

    @staticmethod
    def _view(row):
        return {'format': PROTOCOL, 'job_id': row['id'], 'mesh_task_id': row['mesh_task_id'],
                'request_id': row['request_id'],
                'submission_state': ('submission_unknown' if row['state'] == 'submitting' else row['state']),
                'cli_attempted': bool(row['attempted']), 'can_submit': row['state'] == 'intent',
                'provider_task_id': row['provider_id'], 'provider_url': row['provider_url'],
                'provider_status': row['provider_status'], 'receipt_source': row['receipt_source'],
                'submission_error': row['submission_error'], 'observation_error': row['observation_error'],
                'cloud_node_enrolled': False, 'execution_verified': False,
                'native_tools_intercepted': False}

    def intent(self, payload):
        """Persist immutable work BEFORE submit; one job per original Mesh task."""
        value = self._intent(payload)
        job_id, body, fingerprint = self._id(value), _encoded(value), _digest(value)
        with self._transaction() as db:
            prior = db.execute('SELECT * FROM cloud_jobs WHERE mesh_task_id=? OR request_id=?',
                               (value['mesh_task_id'], value['request_id'])).fetchall()
            if prior:
                if len(prior) != 1 or prior[0]['id'] != job_id or prior[0]['fingerprint'] != fingerprint:
                    raise Conflict('cloud_job_intent_conflict')
                row, _ = self._row(db, job_id)
                return dict(self._view(row), intent_created=False)
            db.execute('''INSERT INTO cloud_jobs(id,mesh_task_id,request_id,body,fingerprint,state,created)
                VALUES(?,?,?,?,?,'intent',?)''',
                       (job_id, value['mesh_task_id'], value['request_id'], body, fingerprint, self.clock()))
            return dict(self._view(self._row(db, job_id)[0]), intent_created=True)

    def get(self, job_id):
        with self._transaction() as db:
            return self._view(self._row(db, job_id)[0])

    def _invoke(self, job_id, args, prompt=None):
        cwd = _private_dir(self.runs / job_id, create=True)
        result = self.runner([self.executable, 'cloud'] + args,
                             input=None if prompt is None else prompt.encode('utf8'), cwd=cwd,
                             env=dict(self.environment), timeout=self.timeout, max_bytes=self.max_bytes)
        if (type(result.returncode) is not int or not isinstance(result.stdout, bytes)
                or not isinstance(result.stderr, bytes)):
            raise CommandFailure('cli_invalid_result')
        if len(result.stdout) > self.max_bytes or len(result.stderr) > self.max_bytes:
            raise CommandFailure('cli_output_limit')
        return result

    def _error(self, job_id, field, category):
        column = {'submission': 'submission_error', 'observation': 'observation_error'}[field]
        with self._transaction() as db:
            self._row(db, job_id)
            if field == 'submission':
                db.execute("UPDATE cloud_jobs SET state='submission_unknown',submission_error=? WHERE id=? AND provider_id IS NULL",
                           (category, job_id))
            else:
                db.execute('UPDATE cloud_jobs SET ' + column + '=? WHERE id=?', (category, job_id))
        return self.get(job_id)

    def _bind(self, job_id, url, source):
        provider_id = _provider_url(url)
        with self._transaction() as db:
            row, _ = self._row(db, job_id)
            if row['attempted'] != 1:
                raise Conflict('cloud_job_not_attempted')
            if row['provider_id'] is not None:
                if row['provider_id'] != provider_id:
                    raise Conflict('cloud_provider_identity_conflict')
                return self._view(row)
            if db.execute('SELECT 1 FROM cloud_jobs WHERE provider_id=?', (provider_id,)).fetchone():
                raise Conflict('cloud_provider_already_bound')
            db.execute("UPDATE cloud_jobs SET state='submitted',provider_id=?,provider_url=?,receipt_source=?,submission_error=NULL WHERE id=?",
                       (provider_id, url, source, job_id))
            return self._view(self._row(db, job_id)[0])

    def submit(self, job_id, *, authorized=False):
        """Trusted caller must verify current owner/task authority before True.

        Consume launch boundary BEFORE subprocess. Even a crash just before
        exec is conservatively unknown; there is deliberately no reset/retry.
        """
        if authorized is not True:
            raise PermissionError('cloud_submit_authorization_required')
        with self._transaction() as db:
            row, value = self._row(db, job_id)
            if row['state'] != 'intent':
                return self._view(row)
            self._intent(value)  # current deployment may have revoked env/ref
            db.execute("UPDATE cloud_jobs SET state='submitting',attempted=1 WHERE id=?", (job_id,))
        try:
            result = self._invoke(job_id, ['exec', '--env', value['environment_id'], '--branch', value['branch'],
                                          '--attempts', '1', '-'], value['prompt'])
            if result.returncode != 0:
                return self._error(job_id, 'submission', 'cli_exec_nonzero')
            try:
                url = result.stdout.decode('utf8').strip()
                _provider_url(url)
            except (ValueError, UnicodeError):
                return self._error(job_id, 'submission', 'cli_exec_invalid_receipt')
            return self._bind(job_id, url, 'cli_exec_receipt')
        except CommandFailure as error:
            category = str(error) if str(error) in ('cli_timeout', 'cli_output_limit', 'cli_invalid_result') else 'cli_failure'
            return self._error(job_id, 'submission', category)
        except (OSError, subprocess.SubprocessError):
            return self._error(job_id, 'submission', 'cli_runtime_failure')

    def _find(self, job_id, value, provider_id, max_pages):
        cursor, seen = None, set()
        for _ in range(max_pages):
            args = ['list', '--env', value['environment_id'], '--limit', '20', '--json']
            if cursor is not None:
                args += ['--cursor', cursor]
            result = self._invoke(job_id, args)
            if result.returncode != 0:
                raise CommandFailure('cli_list_nonzero')
            try:
                page = json.loads(result.stdout.decode('utf8'))
                if not isinstance(page, dict) or not isinstance(page.get('tasks'), list) or len(page['tasks']) > 20:
                    raise ValueError('page')
                matches = [item for item in page['tasks'] if isinstance(item, dict) and item.get('id') == provider_id]
                if len(matches) > 1:
                    raise ValueError('duplicate')
                if matches:
                    item = matches[0]
                    if (_provider_url(item.get('url')) != provider_id
                            or item.get('environment_id') != value['environment_id']
                            or not isinstance(item.get('status'), str)):
                        raise ValueError('binding')
                    return item['status'].lower() if item['status'].lower() in _STATUSES else 'unrecognized'
                cursor = page.get('cursor')
                if cursor is None:
                    return None
                _text(cursor, 'cursor', 2048)
                if cursor in seen:
                    raise ValueError('cursor_loop')
                seen.add(cursor)
            except (ValueError, TypeError, UnicodeError):
                raise CommandFailure('cli_list_invalid_output') from None
        return None

    def observe(self, job_id, *, max_pages=5):
        """Observe an EXACT bound provider ID; absence never licenses submit."""
        if type(max_pages) is not int or not 1 <= max_pages <= 20:
            raise ValueError('invalid_cloud_page_limit')
        with self._transaction() as db:
            row, value = self._row(db, job_id)
            provider_id = row['provider_id']
        if provider_id is None:
            return self._error(job_id, 'observation', 'provider_identity_unknown')
        try:
            status = self._find(job_id, value, provider_id, max_pages)
        except (CommandFailure, OSError, subprocess.SubprocessError):
            return self._error(job_id, 'observation', 'provider_observation_unavailable')
        if status is None:
            return self._error(job_id, 'observation', 'provider_not_observed')
        with self._transaction() as db:
            row, _ = self._row(db, job_id)
            if row['provider_id'] != provider_id:
                raise Conflict('cloud_provider_identity_conflict')
            db.execute('UPDATE cloud_jobs SET provider_status=?,observed=?,observation_error=NULL WHERE id=?',
                       (status, self.clock(), job_id))
        return self.get(job_id)

    def reconcile(self, job_id, provider_url, *, owner_confirmed=False):
        """Manual correlation only; list membership is NOT causal identity proof.

        Owner must independently confirm the receipt belongs to this intent.
        The adapter additionally checks current-profile ID/env membership.
        It NEVER resets attempted or creates another remote task.
        """
        if owner_confirmed is not True:
            raise PermissionError('cloud_reconciliation_owner_required')
        provider_id = _provider_url(provider_url)
        with self._transaction() as db:
            row, value = self._row(db, job_id)
            if row['attempted'] != 1:
                raise Conflict('cloud_job_not_attempted')
            if row['provider_id'] is not None:
                return self._bind_existing(row, provider_id)
        try:
            status = self._find(job_id, value, provider_id, 5)
        except (CommandFailure, OSError, subprocess.SubprocessError):
            raise Conflict('cloud_reconciliation_observation_unavailable') from None
        if status is None:
            raise Conflict('cloud_reconciliation_provider_not_observed')
        return self._bind(job_id, provider_url, 'owner_reconciliation')

    @staticmethod
    def _bind_existing(row, provider_id):
        if row['provider_id'] != provider_id:
            raise Conflict('cloud_provider_identity_conflict')
        return CloudJobs._view(row)

    def status_diagnostic(self, job_id):
        """Human status is auxiliary. Exit 1 can mean PENDING/APPLIED/ERROR."""
        value = self.get(job_id)
        provider_id = value['provider_task_id']
        if provider_id is None:
            raise Conflict('cloud_provider_identity_unknown')
        try:
            result = self._invoke(job_id, ['status', provider_id])
            match = re.match(rb'^\[(PENDING|READY|APPLIED|ERROR)\] ', result.stdout)
            reported = match.group(1).decode('ascii').lower() if match else None
            valid = ((reported == 'ready' and result.returncode == 0)
                     or (reported in ('pending', 'applied', 'error') and result.returncode == 1))
            return {'job_id': job_id, 'reported_status': reported if valid else None,
                    'cli_exit_code': result.returncode, 'diagnostic_recognized': valid,
                    'may_resubmit': False, 'stdout_sha256': hashlib.sha256(result.stdout).hexdigest(),
                    'stderr_sha256': hashlib.sha256(result.stderr).hexdigest()}
        except (CommandFailure, OSError, subprocess.SubprocessError):
            return {'job_id': job_id, 'diagnostic_recognized': False, 'may_resubmit': False,
                    'error': 'cli_status_unavailable'}

    def fetch_diff(self, job_id):
        """Save exact stdout privately for attempt 1; never apply or execute it."""
        value = self.get(job_id)
        if value['provider_task_id'] is None or value['provider_status'] not in ('ready', 'applied'):
            raise Conflict('cloud_diff_not_ready')
        result = self._invoke(job_id, ['diff', value['provider_task_id'], '--attempt', '1'])
        if result.returncode != 0:
            raise CommandFailure('cli_diff_nonzero')
        checksum = hashlib.sha256(result.stdout).hexdigest()
        destination = self.artifacts / (job_id + '-attempt-1-' + checksum + '.patch')
        _private_dir(self.artifacts)
        fd, temporary = tempfile.mkstemp(prefix='.diff-', dir=str(self.artifacts))
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(result.stdout)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, str(destination))  # atomic create, never overwrite
            except FileExistsError:
                _private_file(destination)
                if hashlib.sha256(destination.read_bytes()).hexdigest() != checksum:
                    raise Conflict('cloud_diff_destination_conflict')
        finally:
            os.unlink(temporary)
        _private_file(destination)
        directory = os.open(str(self.artifacts), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        with self._transaction() as db:
            row, _ = self._row(db, job_id)
            if row['provider_id'] != value['provider_task_id']:
                raise Conflict('cloud_provider_identity_conflict')
            previous = db.execute('SELECT bytes,filename FROM cloud_job_artifacts WHERE job_id=? AND attempt=1 AND sha256=?',
                                  (job_id, checksum)).fetchone()
            if previous and (previous['bytes'] != len(result.stdout) or previous['filename'] != destination.name):
                raise Conflict('cloud_diff_journal_conflict')
            if not previous:
                db.execute('INSERT INTO cloud_job_artifacts VALUES(?,1,?,?,?,?)',
                           (job_id, checksum, len(result.stdout), destination.name, self.clock()))
        return self._artifact_view(job_id, value['provider_task_id'], checksum, len(result.stdout), destination)

    @staticmethod
    def _artifact_view(job_id, provider_id, checksum, size, path):
        return {'job_id': job_id, 'provider_task_id': provider_id, 'attempt': 1,
                'private_path': str(path), 'sha256': checksum, 'bytes': size,
                'applied': False, 'artifact_content_verified': False, 'cloud_node_enrolled': False}

    def saved_diffs(self, job_id):
        """Recover previously captured artifacts offline, even after VM exits."""
        with self._transaction() as db:
            job, _ = self._row(db, job_id)
            rows = db.execute('SELECT * FROM cloud_job_artifacts WHERE job_id=? ORDER BY created,sha256',
                              (job_id,)).fetchall()
        values = []
        _private_dir(self.artifacts)
        for row in rows:
            checksum, size = row['sha256'], row['bytes']
            if (not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum)
                    or type(size) is not int or not 0 <= size <= self.max_bytes or row['attempt'] != 1
                    or row['filename'] != job_id + '-attempt-1-' + checksum + '.patch'):
                raise Conflict('cloud_diff_journal_invalid')
            path = self.artifacts / row['filename']
            _private_file(path)
            if path.stat().st_size != size or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise Conflict('cloud_diff_saved_content_changed')
            values.append(self._artifact_view(job_id, job['provider_id'], checksum, size, path))
        return values
