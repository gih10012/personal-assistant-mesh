"""Optional local installation/publication, invoked through native Shell.

This is not an HTTP source installer, native-tool allowlist, code sandbox, test
runner, or service supervisor. An owner-authorized local agent authors/tests
arbitrary Python with its existing native tools. This controller snapshots that
work and records a versioned managed-capability deployment. Remote plans never
install source. Same-UID native processes remain trusted OS principals.

Stage is offline. Publication has one durable attempt: after any ambiguous
advertise response, subsequent calls ONLY query the original desired revision.
Activation changes a manifest, not a running process. Journals of actual tool
effects are never opened, reset or replayed here. Rollback is old retained bytes
in a new release/location and a fresh authority epoch, never counter reversal.
An owner-manifest-scoped lock coordinates cooperating local controllers across
state directories, and activation checks exact prior bytes immediately before
replacement. These checks are not protection from malicious same-UID writers
that deliberately ignore the lock and race the final filesystem replacement.
"""
import fcntl
import functools
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
import urllib.error
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from .provider import _checked_path, _private_file
from .provider_runtime import _manifest, _owner_bytes, _unique_object, installed_binding
from .resources import _epoch, _json, _name, _safe_metadata


class ToolLifecycleError(ValueError):
    """Fixed categories only; never return private paths or exception text."""


def _guard(method):
    @functools.wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ToolLifecycleError:
            raise
        except Exception:
            raise ToolLifecycleError('tool_lifecycle_failed') from None
    return guarded


def _digest(value):
    return hashlib.sha256(_json(value, 'lifecycle', 512 * 1024).encode('utf8')).hexdigest()


def _directory(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ToolLifecycleError('lifecycle_private_directory_required')
    for parent in (path,) + tuple(path.parents):
        metadata = parent.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in (0, os.getuid())
                or (metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX)):
            raise ToolLifecycleError('lifecycle_private_directory_required')
    metadata = path.lstat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ToolLifecycleError('lifecycle_private_directory_required')
    return path


def _sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path, data):
    _directory(path.parent)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    _checked_path(path)
    _sync_directory(path.parent)


def _encoded(value):
    return _json(value, 'lifecycle', 512 * 1024).encode('utf8')


def _publication(path):
    try:
        value = json.loads(_owner_bytes(path, 65536, 'publication_unavailable').decode('utf8'),
                           object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {'kind', 'description', 'spec', 'test_reference'}:
            raise ValueError('shape')
        _name(value['kind'], 'kind')
        if not isinstance(value['description'], str) or len(value['description'].encode('utf8')) > 8000:
            raise ValueError('description')
        if not isinstance(value['spec'], dict) or {'managed_adapter', 'tool_test'} & set(value['spec']):
            raise ValueError('spec')
        safe, redacted = _safe_metadata(value['spec'])
        if redacted or _json(safe, 'spec') != _json(value['spec'], 'spec'):
            raise ValueError('private_metadata')
        reference = value['test_reference']
        if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
                or not isinstance(reference['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', reference['sha256'])):
            raise ValueError('reference')
        report = _owner_bytes(reference['path'], 1024 * 1024, 'test_reference_unavailable')
        if hashlib.sha256(report).hexdigest() != reference['sha256']:
            raise ValueError('reference_integrity')
        return value, report
    except Exception:
        raise ToolLifecycleError('tool_publication_invalid') from None


class ToolLifecycle:
    """One owned private state directory, owner manifest and authenticated client.

    Candidate manifests contain exactly one adapter (a release unit, not a tool
    kind restriction). Its epoch is the desired positive authority revision.
    Publication metadata is separate private JSON: kind/description/spec and
    test_reference={path,sha256}. The referenced bytes are retained; they remain
    self-tested evidence, not independent execution/performance verification.
    """
    @_guard
    def __init__(self, client, owner_config_path, state_dir):
        if not callable(getattr(client, 'request', None)):
            raise ToolLifecycleError('lifecycle_client_required')
        self.client = client
        self.owner_path = Path(_checked_path(owner_config_path))
        self.owner = _manifest(self.owner_path)
        self.root = _directory(state_dir)
        owner_digest = hashlib.sha256(str(self.owner_path).encode('utf8')).hexdigest()
        self.owner_lock = self.owner_path.parent / ('tool-owner-' + owner_digest + '.lock')
        # Resolve dangerous aliases BEFORE creating either control file. The
        # entire state subtree is controller-owned releases/journal space, not
        # a valid location for an actual tool-effects journal. Hardlinks and
        # symlinks are rejected by private-file checks when paths already exist.
        journal = Path(self.owner['journal'])
        reserved = {self.owner_path, self.owner_lock, self.root / 'lifecycle.sqlite',
                    self.root / 'lifecycle.lock'}
        if (journal in reserved or journal == self.root or self.root in journal.parents
                or self.owner_path in {self.root / 'lifecycle.sqlite', self.root / 'lifecycle.lock'}):
            raise ToolLifecycleError('lifecycle_provider_journal_collision')
        if journal.exists() or journal.is_symlink():
            _checked_path(journal)
        for entry in self.owner['adapters']:
            if Path(entry['module']) in {self.owner_lock, self.root / 'lifecycle.sqlite', self.root / 'lifecycle.lock'}:
                raise ToolLifecycleError('lifecycle_control_path_collision')
        self.path = _checked_path(self.root / 'lifecycle.sqlite', create=True)
        self.identity = {key: self.owner[key] for key in ('provider', 'authority_id', 'journal')}
        self.identity['owner_config'] = str(self.owner_path)
        with self._db() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables and tables != {'lifecycle_metadata', 'tool_deployments'}:
                raise ToolLifecycleError('lifecycle_journal_invalid')
            if not tables:
                db.execute('CREATE TABLE lifecycle_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.execute('''CREATE TABLE tool_deployments(operation_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,state TEXT NOT NULL,record TEXT NOT NULL,
                    activation TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL)''')
                db.execute('INSERT INTO lifecycle_metadata VALUES(?,?)', ('identity', _json(self.identity, 'identity')))
                db.execute('INSERT INTO lifecycle_metadata VALUES(?,?)', ('schema', '1'))
            if dict(db.execute('SELECT key,value FROM lifecycle_metadata')) != {
                    'schema': '1', 'identity': _json(self.identity, 'identity')}:
                raise ToolLifecycleError('lifecycle_journal_identity_mismatch')
            columns = {row[1] for row in db.execute('PRAGMA table_info(tool_deployments)')}
            if columns != {'operation_id', 'fingerprint', 'state', 'record', 'activation', 'error', 'created', 'updated'}:
                raise ToolLifecycleError('lifecycle_journal_invalid')

    @contextmanager
    def _db(self):
        _checked_path(self.path)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    @contextmanager
    def _file_lock(self, path):
        _directory(path.parent)
        descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            _private_file(path)
            opened, named = os.fstat(descriptor), path.lstat()
            if (opened.st_ino, opened.st_dev) != (named.st_ino, named.st_dev):
                raise ToolLifecycleError('lifecycle_lock_invalid')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ToolLifecycleError('lifecycle_busy') from None
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @contextmanager
    def _lock(self):
        # Same order for every controller avoids deadlocks. Owner lock is shared
        # even when independent deployment state directories were selected.
        with self._file_lock(self.owner_lock):
            with self._file_lock(self.root / 'lifecycle.lock'):
                yield

    def _owner(self):
        value = _manifest(self.owner_path)
        if {key: value[key] for key in ('provider', 'authority_id', 'journal')} != {
                key: self.identity[key] for key in ('provider', 'authority_id', 'journal')}:
            raise ToolLifecycleError('owner_identity_changed')
        return value

    @staticmethod
    def _operation(identity):
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}', identity):
            raise ToolLifecycleError('lifecycle_operation_invalid')
        return identity

    def _get(self, identity):
        self._operation(identity)
        with self._db() as db:
            row = db.execute('SELECT * FROM tool_deployments WHERE operation_id=?', (identity,)).fetchone()
        if not row:
            raise ToolLifecycleError('lifecycle_operation_not_found')
        return dict(row)

    def _set(self, identity, state, error=None, activation=None):
        with self._db() as db:
            if activation is None:
                db.execute('UPDATE tool_deployments SET state=?,error=?,updated=? WHERE operation_id=?',
                           (state, error, time.time(), identity))
            else:
                db.execute('UPDATE tool_deployments SET state=?,error=?,activation=?,updated=? WHERE operation_id=?',
                           (state, error, _json(activation, 'activation', 512 * 1024), time.time(), identity))

    def _validate_record(self, row):
        record = json.loads(row['record'])
        release = _directory(record['release'])
        if release.parent != self.root:
            raise ToolLifecycleError('retained_release_invalid')
        candidate = _manifest(release / 'candidate.json')
        if candidate['adapters'] != [record['entry']]:
            raise ToolLifecycleError('retained_release_invalid')
        _, descriptor, _ = installed_binding(record['entry'])
        if descriptor != record['publication']['spec']['managed_adapter']:
            raise ToolLifecycleError('retained_release_invalid')
        report = _owner_bytes(release / 'test-report', 1024 * 1024, 'retained_test_unavailable')
        if hashlib.sha256(report).hexdigest() != record['test_sha256']:
            raise ToolLifecycleError('retained_release_invalid')
        return record

    def _view(self, row):
        record = self._validate_record(row)
        state = 'unknown' if row['state'] == 'publication_intent' else row['state']
        activation_recorded = row['state'] == 'manifest_activated'
        binding_current = any(entry == record['entry'] for entry in self._owner()['adapters'])
        return {'operation_id': row['operation_id'], 'state': state,
                'capability_id': record['entry']['capability_id'],
                'capability_epoch': record['entry']['capability_epoch'],
                'managed_adapter': record['publication']['spec']['managed_adapter'],
                'test_reference_sha256': record['test_sha256'], 'test_verification': 'self_tested',
                'publication_attempted': row['state'] != 'staged',
                'error': row['error'], 'rollback_of': record.get('rollback_of'),
                # State is deployment history. A later release may have
                # superseded it; neither inspect nor this view queries current
                # authority or proves which code a live process actually uses.
                'activation_recorded': activation_recorded,
                'manifest_binding_current': binding_current,
                'manifest_activated': activation_recorded and binding_current,
                'runtime_reload_required': activation_recorded and binding_current,
                'runtime_loaded_verified': False, 'execution_verified': False,
                'performance_verified': False, 'native_tools_intercepted': False,
                'provider_journal_modified': False, 'automatic_invocation_replay': False}

    @_guard
    def inspect(self, operation_id):
        with self._lock():
            self._owner()
            return self._view(self._get(operation_id))

    def _stage(self, operation_id, candidate, publication, source, report, fingerprint, rollback_of=None):
        try:
            existing = self._get(operation_id)
        except ToolLifecycleError as error:
            if str(error) != 'lifecycle_operation_not_found':
                raise
        else:
            if existing['fingerprint'] != fingerprint:
                raise ToolLifecycleError('lifecycle_operation_conflict')
            return self._view(existing)
        release = Path(tempfile.mkdtemp(prefix='release-', dir=str(self.root)))
        release.chmod(0o700)
        entry = dict(candidate['adapters'][0], module=str(release / 'module.py'))
        _write_new(release / 'module.py', source)
        _write_new(release / 'test-report', report)
        retained = dict(candidate, adapters=[entry])
        _write_new(release / 'candidate.json', _encoded(retained))
        _, descriptor, _ = installed_binding(entry)
        spec = dict(publication['spec'], managed_adapter=descriptor,
                    tool_test={'sha256': publication['test_reference']['sha256'], 'verification': 'self_tested'})
        safe, redacted = _safe_metadata(spec)
        try:
            # The descriptor introduces owner-chosen handle/action strings too:
            # validate the COMPLETE advertised metadata, not only user spec.
            if redacted or _json(safe, 'spec') != _json(spec, 'spec'):
                raise ValueError('private_metadata')
        except Exception:
            raise ToolLifecycleError('tool_publication_invalid') from None
        record = {'release': str(release), 'entry': entry, 'test_sha256': publication['test_reference']['sha256'],
                  'publication': {'kind': publication['kind'], 'description': publication['description'], 'spec': spec},
                  'rollback_of': rollback_of}
        with self._db() as db:
            now = time.time()
            db.execute('INSERT INTO tool_deployments VALUES(?,?,?,?,NULL,NULL,?,?)',
                       (operation_id, fingerprint, 'staged', _json(record, 'record', 512 * 1024), now, now))
        return self._view(self._get(operation_id))

    @_guard
    def stage(self, operation_id, candidate_config_path, publication_path):
        self._operation(operation_id)
        with self._lock():
            self._owner()
            candidate = _manifest(candidate_config_path)
            if (len(candidate['adapters']) != 1 or {key: candidate[key] for key in ('provider', 'authority_id', 'journal')}
                    != {key: self.identity[key] for key in ('provider', 'authority_id', 'journal')}):
                raise ToolLifecycleError('candidate_identity_invalid')
            installed_binding(candidate['adapters'][0])
            source = _owner_bytes(candidate['adapters'][0]['module'], 1024 * 1024, 'candidate_unavailable')
            if hashlib.sha256(source).hexdigest() != candidate['adapters'][0]['sha256']:
                raise ToolLifecycleError('candidate_integrity_failed')
            publication, report = _publication(publication_path)
            fingerprint = _digest([candidate, publication])
            return self._stage(operation_id, candidate, publication, source, report, fingerprint)

    def _hello(self):
        hello = self.client.request('/v1/mesh/hello')
        if (not isinstance(hello, dict) or hello.get('protocol') != 'mesh-a2a/1'
                or hello.get('node') != self.identity['authority_id']
                or hello.get('authority') != self.identity['authority_id']):
            raise ToolLifecycleError('publication_authority_mismatch')

    def _current(self, identity):
        path = '/v1/resource?id=' + urllib.parse.quote(identity, safe='') + '&include_unavailable=1'
        try:
            value = self.client.request(path)
        except urllib.error.HTTPError as error:
            # Client intentionally closes/discards private error bodies, and
            # the server redacts ValueError as invalid_request. A 400 alone is
            # NOT absence proof: require a complete authenticated directory.
            absent_candidate = error.code == 400
            try:
                error.close()
            except Exception:
                pass
            if not absent_candidate:
                raise ToolLifecycleError('publication_query_failed') from None
            listing = self.client.request('/v1/resources?include_unavailable=1&limit=1000')
            if (not isinstance(listing, dict) or not isinstance(listing.get('capabilities'), list)
                    or len(listing['capabilities']) > 1000
                    or not all(isinstance(item, dict) and isinstance(item.get('id'), str)
                               for item in listing['capabilities'])
                    or len({item['id'] for item in listing['capabilities']}) != len(listing['capabilities'])):
                raise ToolLifecycleError('publication_response_invalid')
            found = [item for item in listing['capabilities'] if item['id'] == identity]
            if not found:
                if len(listing['capabilities']) == 1000:
                    raise ToolLifecycleError('publication_directory_incomplete')
                return None
            value = found[0]
        if (not isinstance(value, dict) or value.get('id') != identity
                or type(value.get('epoch')) is not int or value['epoch'] < 1
                or not isinstance(value.get('spec'), dict) or not isinstance(value.get('principal'), str)
                or type(value.get('available')) is not bool or type(value.get('revoked')) is not bool):
            raise ToolLifecycleError('publication_response_invalid')
        return value

    def _matches(self, current, record):
        publication = record['publication']
        return (current is not None and current['principal'] == self.identity['provider']
                and current['epoch'] == record['entry']['capability_epoch']
                and current.get('kind') == publication['kind'] and current.get('description') == publication['description']
                and _json(current['spec'], 'spec') == _json(publication['spec'], 'spec')
                and current['available'] is True and current['revoked'] is False)

    def _confirm(self, operation_id, record):
        self._hello()
        current = self._current(record['entry']['capability_id'])
        if not self._matches(current, record):
            raise ToolLifecycleError('publication_not_confirmed')
        self._set(operation_id, 'published')

    @_guard
    def publish(self, operation_id):
        with self._lock():
            self._owner()
            row = self._get(operation_id)
            record = self._validate_record(row)
            if row['state'] == 'manifest_activated':
                return self._view(row)
            if row['state'] != 'staged':
                try:
                    self._confirm(operation_id, record)
                except Exception:
                    self._set(operation_id, 'unknown', 'publication_not_confirmed')
                return self._view(self._get(operation_id))
            # Failed preflight has no advertise intent and can safely be tried
            # again. Once intent exists even a crash before POST is query-only.
            self._hello()
            current = self._current(record['entry']['capability_id'])
            desired = record['entry']['capability_epoch']
            if ((desired == 1 and current is not None) or (desired > 1 and
                    (current is None or current['principal'] != self.identity['provider']
                     or current['epoch'] != desired - 1 or current['revoked']))):
                raise ToolLifecycleError('publication_predecessor_mismatch')
            self._set(operation_id, 'publication_intent')
            payload = dict(record['publication'], id=record['entry']['capability_id'], lease_seconds=86400,
                           expected_epoch=None if desired == 1 else desired - 1)
            try:
                value = self.client.request('/v1/resource/action', {'action': 'advertise', 'arguments': payload})
                if not self._matches(value, record):
                    raise ToolLifecycleError('publication_response_invalid')
                self._confirm(operation_id, record)
            except Exception:
                self._set(operation_id, 'unknown', 'publication_outcome_unknown')
            return self._view(self._get(operation_id))

    def _replace_manifest(self, desired, expected):
        temporary = Path(tempfile.mkdtemp(prefix='.activation-', dir=str(self.owner_path.parent)))
        temporary.chmod(0o700)
        draft = temporary / 'owner.json'
        _write_new(draft, desired)
        # This is immediately adjacent to replace, after staging/fsync. Legit-
        # imate non-cooperating edits are preserved when observable; hostile
        # same-UID racing processes are outside this trust boundary.
        self._owner()
        if _owner_bytes(self.owner_path, 65536, 'owner_config_unavailable') != expected:
            raise ToolLifecycleError('activation_owner_changed')
        os.replace(str(draft), str(self.owner_path))
        _sync_directory(self.owner_path.parent)
        # The temporary directory may remain after an interrupted replacement;
        # it is private and never treated as proof of activation or quiescence.
        temporary.rmdir()

    @_guard
    def activate(self, operation_id):
        with self._lock():
            owner = self._owner()
            row = self._get(operation_id)
            record = self._validate_record(row)
            if row['state'] not in ('published', 'activation_intent', 'manifest_activated'):
                raise ToolLifecycleError('activation_requires_publication')
            self._hello()
            if not self._matches(self._current(record['entry']['capability_id']), record):
                raise ToolLifecycleError('activation_publication_changed')
            current = _owner_bytes(self.owner_path, 65536, 'owner_config_unavailable')
            # HTTP read-back can be slow; rebuild from the CURRENT manifest,
            # not the pre-network snapshot, while proving this parse/read pair
            # saw the exact same bytes. Cooperating writers hold owner lock.
            owner = self._owner()
            if _owner_bytes(self.owner_path, 65536, 'owner_config_unavailable') != current:
                raise ToolLifecycleError('activation_owner_changed')
            if row['activation']:
                activation = json.loads(row['activation'])
                desired = _owner_bytes(activation['desired'], 65536, 'activation_manifest_unavailable')
                if hashlib.sha256(desired).hexdigest() != activation['desired_sha256']:
                    raise ToolLifecycleError('activation_manifest_invalid')
                if current == desired:
                    self._set(operation_id, 'manifest_activated', activation=activation)
                    return self._view(self._get(operation_id))
                if hashlib.sha256(current).hexdigest() != activation['previous_sha256']:
                    raise ToolLifecycleError('activation_owner_changed')
            else:
                release = Path(record['release'])
                previous_path, desired_path = release / 'owner-before.json', release / 'owner-after.json'
                adapters = [item for item in owner['adapters'] if item['capability_id'] != record['entry']['capability_id']]
                desired_value = dict(owner, adapters=adapters + [record['entry']])
                desired = _encoded(desired_value)
                if len(desired_value['adapters']) > 256 or len(desired) > 65536:
                    raise ToolLifecycleError('activation_manifest_invalid')
                # Backup publication is exclusive and checked on retry: never
                # overwrite a saved prior manifest after partial activation.
                for path, data in ((previous_path, current), (desired_path, desired)):
                    if path.exists():
                        if _owner_bytes(path, 65536, 'activation_manifest_unavailable') != data:
                            raise ToolLifecycleError('activation_backup_conflict')
                    else:
                        _write_new(path, data)
                activation = {'previous': str(previous_path), 'previous_sha256': hashlib.sha256(current).hexdigest(),
                              'desired': str(desired_path), 'desired_sha256': hashlib.sha256(desired).hexdigest()}
                self._set(operation_id, 'activation_intent', activation=activation)
            self._replace_manifest(desired, current)
            if _owner_bytes(self.owner_path, 65536, 'owner_config_unavailable') != desired:
                raise ToolLifecycleError('activation_not_confirmed')
            self._set(operation_id, 'manifest_activated', activation=activation)
            return self._view(self._get(operation_id))

    @_guard
    def prepare_rollback(self, new_operation_id, retained_operation_id, capability_epoch):
        self._operation(new_operation_id)
        _epoch(capability_epoch)
        with self._lock():
            self._owner()
            row = self._get(retained_operation_id)
            record = self._validate_record(row)
            if capability_epoch <= record['entry']['capability_epoch']:
                raise ToolLifecycleError('rollback_requires_new_epoch')
            source = _owner_bytes(record['entry']['module'], 1024 * 1024, 'retained_module_unavailable')
            report = _owner_bytes(Path(record['release']) / 'test-report', 1024 * 1024, 'retained_test_unavailable')
            candidate = dict(self.owner, adapters=[dict(record['entry'], capability_epoch=capability_epoch)])
            publication = dict(record['publication'])
            publication['spec'] = {key: value for key, value in publication['spec'].items()
                                   if key not in ('managed_adapter', 'tool_test')}
            publication['test_reference'] = {'path': str(Path(record['release']) / 'test-report'),
                                             'sha256': record['test_sha256']}
            return self._stage(new_operation_id, candidate, publication, source, report,
                               _digest(['rollback', retained_operation_id, capability_epoch, record]),
                               rollback_of=retained_operation_id)
