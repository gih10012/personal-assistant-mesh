"""Environment-neutral enrollment documents and a small Linux reference journal.

The JSON contract does not require Python, a daemon, SSH, POSIX, or native Shell.
It adds managed Mesh capabilities; it never intercepts the host's native tools.
Owner authorization, executors, independent oracles and registry observers are
trusted integration inputs, NOT fields an untrusted node may approve for itself.
Import/validation is pure. Only explicit journal methods write private state.
"""
import base64
import contextlib
import functools
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
from pathlib import Path


ENROLLMENT_FORMAT = 'mesh-enrollment/1'
REPORT_FORMAT = 'mesh-adaptation-report/1'
CHALLENGE_FORMAT = 'mesh-enrollment-challenge/1'
RESULT_FORMAT = 'mesh-enrollment-result/1'
REGISTRATION_FORMAT = 'mesh-enrollment-registration/1'
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_INTEGER = 9007199254740991  # exact in JSON/JavaScript implementations
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_COMMIT = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?\Z')


class EnrollmentError(ValueError):
    """Fixed categories only; callbacks/private paths never appear in errors."""


def _require(condition, category):
    if not condition:
        raise EnrollmentError(category)


def _guard(method):
    @functools.wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except EnrollmentError:
            raise
        except Exception:
            raise EnrollmentError('enrollment_reference_failed') from None
    return guarded


def _text(value, maximum=512):
    try:
        valid = (isinstance(value, str) and bool(value.strip())
                 and len(value.encode('utf8')) <= maximum
                 and all(ord(char) >= 32 for char in value))
    except UnicodeError:
        valid = False
    _require(valid, 'enrollment_text_invalid')
    return value


def _integer(value, minimum=0, maximum=_MAX_INTEGER):
    _require(type(value) is int and minimum <= value <= maximum,
             'enrollment_integer_invalid')
    return value


def _object(value, fields):
    _require(isinstance(value, dict) and set(value) == set(fields),
             'enrollment_document_shape_invalid')
    return value


def _encoded(value, maximum=_MAX_JSON_BYTES):
    try:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('ascii')
    except (TypeError, ValueError, UnicodeError):
        raise EnrollmentError('enrollment_json_invalid') from None
    _require(len(raw) <= maximum, 'enrollment_document_too_large')
    return raw


def document_sha256(value):
    """Hash exact canonical ASCII JSON bytes; no fetch or source execution."""
    return hashlib.sha256(_encoded(value)).hexdigest()


def _detached(value, maximum=128 * 1024):
    return json.loads(_encoded(value, maximum).decode('ascii'))


def _hash(value):
    _require(isinstance(value, str) and _SHA.fullmatch(value) is not None,
             'enrollment_sha256_invalid')
    return value


def _continuation(value):
    _object(value, ('project_id', 'session_ref', 'state_ref'))
    for item in value.values():
        _text(item)
    return value


def validate_enrollment(document):
    """Return a detached document, not a trust grant or a live enrollment.

    References are opaque private deployment handles, not required file paths.
    Expiration is checked by the caller at actual admission/use, not at parsing.
    Temporary nodes can keep durable state elsewhere in Mesh. Interactive nodes
    need neither a background process nor their own files or execution harness.
    """
    document = _detached(document)
    _object(document, ('format', 'mesh_id', 'node_id', 'owner_binding',
                       'repo_pin', 'runtime', 'join', 'continuation'))
    _require(document['format'] == ENROLLMENT_FORMAT, 'enrollment_format_invalid')
    for name in ('mesh_id', 'node_id', 'owner_binding'):
        _text(document[name], 200)
    pin = _object(document['repo_pin'], ('repository', 'commit'))
    _text(pin['repository'], 1024)
    _require(isinstance(pin['commit'], str) and _COMMIT.fullmatch(pin['commit']) is not None,
             'enrollment_repo_pin_invalid')
    runtime = _object(document['runtime'], ('kind', 'expires_at', 'state_ref'))
    _require(runtime['kind'] in ('persistent', 'temporary', 'interactive'),
             'enrollment_runtime_kind_invalid')
    _text(runtime['state_ref'])
    if runtime['expires_at'] is not None:
        _integer(runtime['expires_at'], 1)
    _require(runtime['kind'] != 'temporary' or runtime['expires_at'] is not None,
             'enrollment_temporary_expiration_required')
    join = _object(document['join'], ('peer_id', 'authority_id', 'route_ref',
                                      'credential_ref', 'trust_ref'))
    for item in join.values():
        _text(item)
    _continuation(document['continuation'])
    return document


def validate_adaptation_report(document):
    """Environment/skills/candidates are declarations, never verification."""
    document = _detached(document)
    _object(document, ('format', 'mesh_id', 'node_id', 'system', 'harness',
                       'storage', 'capabilities', 'gaps', 'native_features_restricted'))
    _require(document['format'] == REPORT_FORMAT, 'enrollment_report_format_invalid')
    _text(document['mesh_id'], 200)
    _text(document['node_id'], 200)
    system = _object(document['system'], ('name', 'version'))
    _text(system['name'], 200)  # Windows/Harmony/mobile/custom names remain valid.
    _text(system['version'], 200)
    harness = _object(document['harness'], ('name', 'version', 'available'))
    _text(harness['name'], 200)
    _text(harness['version'], 200)
    _require(type(harness['available']) is bool, 'enrollment_harness_report_invalid')
    storage = _object(document['storage'], ('mode', 'state_ref'))
    _require(storage['mode'] in ('local', 'mesh', 'both', 'none'),
             'enrollment_storage_report_invalid')
    if storage['state_ref'] is not None:
        _text(storage['state_ref'])
    _require(storage['mode'] == 'none' or storage['state_ref'] is not None,
             'enrollment_storage_reference_required')
    capabilities = document['capabilities']
    _require(isinstance(capabilities, list) and len(capabilities) <= 256,
             'enrollment_capability_report_invalid')
    seen = set()
    for capability in capabilities:
        _object(capability, ('id', 'revision', 'actions'))
        _text(capability['id'], 200)
        _integer(capability['revision'], 1)
        _require(capability['id'] not in seen, 'enrollment_capability_duplicate')
        seen.add(capability['id'])
        _actions(capability['actions'])
    _require(isinstance(document['gaps'], list) and len(document['gaps']) <= 64,
             'enrollment_gap_report_invalid')
    for gap in document['gaps']:
        _text(gap, 1024)
    _require(document['native_features_restricted'] is False,
             'enrollment_must_not_restrict_native_features')
    return document


def _actions(value):
    _require(isinstance(value, list) and 0 < len(value) <= 64,
             'enrollment_actions_invalid')
    for action in value:
        _text(action, 200)
    _require(len(set(value)) == len(value), 'enrollment_actions_duplicate')


def _bytes(value, maximum=MAX_ARTIFACT_BYTES):
    _require(isinstance(value, bytes) and len(value) <= maximum,
             'enrollment_artifact_bytes_invalid')
    return value


def _blob(value):
    value = _bytes(value)
    return {'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest(),
            'base64': base64.b64encode(value).decode('ascii')}


def _unblob(value):
    _object(value, ('bytes', 'sha256', 'base64'))
    _integer(value['bytes'], 0, MAX_ARTIFACT_BYTES)
    _hash(value['sha256'])
    _require(isinstance(value['base64'], str)
             and len(value['base64']) <= (MAX_ARTIFACT_BYTES + 2) // 3 * 4,
             'enrollment_artifact_encoding_invalid')
    try:
        raw = base64.b64decode(value['base64'].encode('ascii'), validate=True)
    except (ValueError, UnicodeError):
        raise EnrollmentError('enrollment_artifact_encoding_invalid') from None
    _require(len(raw) == value['bytes']
             and hmac.compare_digest(hashlib.sha256(raw).hexdigest(), value['sha256'])
             and base64.b64encode(raw).decode('ascii') == value['base64'],
             'enrollment_artifact_integrity_failed')
    return raw


def result_envelope(challenge, output_bytes):
    """Convenience for any executor; returned JSON is still provider-reported."""
    challenge = _detached(challenge)
    _require(challenge.get('format') == CHALLENGE_FORMAT, 'enrollment_challenge_format_invalid')
    return {'format': RESULT_FORMAT, 'operation_id': challenge['operation_id'],
            'request_sha256': document_sha256(challenge), 'nonce': challenge['nonce'],
            'result': _blob(output_bytes), 'continuation': challenge['continuation'],
            'proxy': challenge['proxy']}


def _private_directory(value, create=True):
    """Linux/POSIX reference only; other hosts use equivalent private storage."""
    _require(os.name == 'posix', 'enrollment_private_storage_adapter_required')
    path = Path(value)
    _require(path.is_absolute() and '..' not in path.parts,
             'enrollment_private_directory_required')
    for parent in path.parents:
        item = parent.lstat()
        _require(stat.S_ISDIR(item.st_mode) and item.st_uid in (0, os.getuid())
                 and (not item.st_mode & 0o022 or item.st_mode & stat.S_ISVTX),
                 'enrollment_private_directory_required')
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    item = path.lstat()
    _require(stat.S_ISDIR(item.st_mode) and item.st_uid == os.getuid()
             and stat.S_IMODE(item.st_mode) == 0o700,
             'enrollment_private_directory_required')
    return path


def _private_file(path, create=False):
    if create:
        try:
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        item = os.fstat(descriptor)
        _require(stat.S_ISREG(item.st_mode) and item.st_uid == os.getuid()
                 and stat.S_IMODE(item.st_mode) == 0o600 and item.st_nlink == 1,
                 'enrollment_private_file_required')
    finally:
        os.close(descriptor)


class EnrollmentJournal:
    """Explicitly installed trusted local reference, not a public admission API.

    The installer supplies the pinned document, exact owner authorization and
    verifier identity. Callbacks cannot be selected by model/node JSON. A node
    never earns more authorization by migrating, obtaining a TTL or reporting
    an interface. Real Registry grants/pools and native permissions stay separate.
    """
    @_guard
    def __init__(self, state_dir, document, authorization, verifier_id, clock=time.time, *, initialize=True):
        _require(type(initialize) is bool, 'enrollment_initialize_flag_invalid')
        metadata = self._configure(document, authorization, verifier_id, clock)
        self._readonly = False
        self.root = _private_directory(state_dir, create=initialize)
        self.path = self.root / 'enrollment.sqlite'
        _private_file(self.path, create=initialize)
        with self._transaction() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            expected = {'enrollment_metadata', 'enrollment_reports', 'enrollment_operations'}
            _require((initialize and not tables) or tables == expected,
                     'enrollment_journal_schema_invalid')
            if initialize:
                db.execute('CREATE TABLE IF NOT EXISTS enrollment_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.execute('''CREATE TABLE IF NOT EXISTS enrollment_reports(
                    sha256 TEXT PRIMARY KEY,body TEXT NOT NULL)''')
                db.execute('''CREATE TABLE IF NOT EXISTS enrollment_operations(
                    id TEXT PRIMARY KEY,intent_sha256 TEXT NOT NULL,request_sha256 TEXT NOT NULL,request TEXT NOT NULL,
                    state TEXT NOT NULL,result TEXT,result_sha256 TEXT,verification TEXT,
                    registration_state TEXT NOT NULL,registration TEXT)''')
            saved = dict(db.execute('SELECT key,value FROM enrollment_metadata'))
            if not saved and initialize:
                _require(not db.execute('SELECT 1 FROM enrollment_operations LIMIT 1').fetchone()
                         and not db.execute('SELECT 1 FROM enrollment_reports LIMIT 1').fetchone(),
                         'enrollment_journal_identity_missing')
                db.executemany('INSERT INTO enrollment_metadata VALUES(?,?)', list(metadata.items()))
            else:
                _require(saved == metadata, 'enrollment_journal_identity_mismatch')

    def _configure(self, document, authorization, verifier_id, clock):
        """Shared binding validation; this helper performs no filesystem I/O."""
        self.document = validate_enrollment(document)
        self.verifier = _text(verifier_id, 200)
        authorization = _detached(authorization)
        _object(authorization, ('enrollment_sha256', 'valid_until', 'capabilities'))
        _require(_hash(authorization['enrollment_sha256']) == document_sha256(self.document),
                 'enrollment_owner_binding_mismatch')
        _integer(authorization['valid_until'], 1)
        capabilities = authorization['capabilities']
        _require(isinstance(capabilities, list) and len(capabilities) <= 256,
                 'enrollment_owner_capabilities_invalid')
        self.capabilities = {}
        for capability in capabilities:
            _object(capability, ('id', 'actions', 'source_device', 'proxy_binding'))
            _text(capability['id'], 200)
            _actions(capability['actions'])
            _require(capability['id'] not in self.capabilities, 'enrollment_capability_duplicate')
            _require((capability['source_device'] is None) == (capability['proxy_binding'] is None),
                     'enrollment_proxy_binding_invalid')
            if capability['source_device'] is not None:
                _text(capability['source_device'], 200)
                _text(capability['proxy_binding'])
            self.capabilities[capability['id']] = capability
        self.authorization, self.clock = authorization, clock
        return {'format': ENROLLMENT_FORMAT,
                'enrollment_sha256': document_sha256(self.document),
                'authorization_sha256': document_sha256(authorization), 'verifier': self.verifier}

    @classmethod
    @_guard
    def open_readonly(cls, state_dir, document, authorization, verifier_id, clock=time.time):
        """Observe an existing journal without initialization, recovery or writes.

        Expired/disabled installations can still read original challenges and
        full results. Missing state is an error, never a reason to create a new
        operation or authority. This is the POSIX reference storage adapter.
        """
        self = cls.__new__(cls)
        metadata = self._configure(document, authorization, verifier_id, clock)
        self._readonly = True
        self.root = _private_directory(state_dir, create=False)
        self.path = self.root / 'enrollment.sqlite'
        _private_file(self.path)
        with self._transaction() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            _require(tables == {'enrollment_metadata', 'enrollment_reports', 'enrollment_operations'},
                     'enrollment_journal_schema_invalid')
            saved = dict(db.execute('SELECT key,value FROM enrollment_metadata'))
            _require(saved == metadata, 'enrollment_journal_identity_mismatch')
        return self

    def _writable(self):
        _require(not self._readonly, 'enrollment_journal_readonly')

    @contextlib.contextmanager
    def _transaction(self):
        _private_directory(self.root, create=False)
        _private_file(self.path)
        # The constructor explicitly creates a new file when requested. A
        # vanished existing journal must never be recreated by connect().
        target = self.path.as_uri() + ('?mode=ro' if self._readonly else '?mode=rw')
        with contextlib.closing(sqlite3.connect(target, timeout=10, isolation_level=None,
                                               uri=True)) as db:
            db.row_factory = sqlite3.Row
            if self._readonly:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
            else:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def _now(self):
        value = self.clock()
        _require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= _MAX_INTEGER,
                 'enrollment_clock_invalid')
        return value

    def _live(self, request=None):
        now = self._now()
        until = self.document['runtime']['expires_at']
        _require(now < self.authorization['valid_until'] and (until is None or now < until),
                 'enrollment_authorization_expired')
        if request is not None:
            _require(request['created_at'] <= now < request['expires_at'],
                     'enrollment_challenge_expired')
        return now

    @staticmethod
    def _row(db, operation_id):
        _text(operation_id, 200)
        row = db.execute('SELECT * FROM enrollment_operations WHERE id=?', (operation_id,)).fetchone()
        _require(row is not None, 'enrollment_operation_unknown')
        _require(hashlib.sha256(row['request'].encode('ascii')).hexdigest() == row['request_sha256'],
                 'enrollment_original_request_integrity_failed')
        _require((row['result'] is None) == (row['result_sha256'] is None),
                 'enrollment_original_result_integrity_failed')
        if row['result'] is not None:
            _require(hashlib.sha256(row['result'].encode('ascii')).hexdigest() == row['result_sha256'],
                     'enrollment_original_result_integrity_failed')
        return row

    @_guard
    def record_adaptation(self, report):
        self._writable()
        report = validate_adaptation_report(report)
        _require(all(report[name] == self.document[name] for name in ('mesh_id', 'node_id')),
                 'enrollment_report_identity_mismatch')
        sha = document_sha256(report)
        with self._transaction() as db:
            db.execute('INSERT OR IGNORE INTO enrollment_reports VALUES(?,?)',
                       (sha, _encoded(report).decode('ascii')))
        return {'report_sha256': sha, 'verification': 'declared',
                'capabilities_registered': False, 'native_features_restricted': False}

    @_guard
    def intent(self, operation_id, capability_id, action, input_bytes, expected_bytes,
               revision=1, ttl_seconds=60, proxy=None):
        self._writable()
        _text(operation_id, 200)
        _text(capability_id, 200)
        _text(action, 200)
        _integer(revision, 1)
        _integer(ttl_seconds, 1, 3600)
        _bytes(input_bytes, 64 * 1024)
        _bytes(expected_bytes)
        capability = self.capabilities.get(capability_id)
        _require(capability is not None and action in capability['actions'],
                 'enrollment_capability_not_authorized')
        if proxy is not None:
            proxy = _detached(proxy)
            _object(proxy, ('source_device', 'proxy_binding', 'revision', 'ttl_seconds'))
            _integer(proxy['revision'], 1)
            _integer(proxy['ttl_seconds'], 1, 3600)
            _require(all(proxy[name] == capability[name] for name in ('source_device', 'proxy_binding'))
                     and proxy['source_device'] is not None,
                     'enrollment_proxy_not_authorized')
        else:
            _require(capability['source_device'] is None, 'enrollment_proxy_binding_required')
        intent = {'capability_id': capability_id, 'action': action, 'revision': revision,
                  'input': _blob(input_bytes),
                  'expected': {'bytes': len(expected_bytes), 'sha256': hashlib.sha256(expected_bytes).hexdigest()},
                  'ttl_seconds': ttl_seconds, 'proxy': proxy}
        intent_sha = document_sha256(intent)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM enrollment_operations WHERE id=?', (operation_id,)).fetchone()
            if row is not None:
                row = self._row(db, operation_id)
                _require(row['intent_sha256'] == intent_sha, 'enrollment_original_intent_conflict')
                return json.loads(row['request'])
            now = self._live()
            deadlines = [int(now) + ttl_seconds, self.authorization['valid_until']]
            if self.document['runtime']['expires_at'] is not None:
                deadlines.append(self.document['runtime']['expires_at'])
            if proxy is not None:
                deadlines.append(int(now) + proxy['ttl_seconds'])
            request = dict(intent, format=CHALLENGE_FORMAT, operation_id=operation_id,
                           mesh_id=self.document['mesh_id'], node_id=self.document['node_id'],
                           owner_binding=self.document['owner_binding'],
                           enrollment_sha256=document_sha256(self.document),
                           continuation=self.document['continuation'],
                           nonce=secrets.token_hex(32), created_at=int(now), expires_at=min(deadlines))
            db.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,\'intent\',NULL,NULL,NULL,\'not_registered\',NULL)',
                       (operation_id, intent_sha, document_sha256(request), _encoded(request, 128 * 1024).decode('ascii')))
        return request

    @_guard
    def admit_external(self, operation_id, expected_request_sha256):
        """One durable admission for an owner-authenticated external executor.

        No callback runs here. Commit BEFORE handing out the original challenge;
        a lost response cannot cause another executable admission. Repeated calls
        return the original challenge for non-executable observation only. The
        caller must check trusted task/credential authority, never a node boolean.
        This uses the same operation row/state as execute(), not a new database.
        """
        self._writable()
        _hash(expected_request_sha256)
        with self._transaction() as db:
            row = self._row(db, operation_id)
            _require(row['request_sha256'] == expected_request_sha256,
                     'enrollment_original_request_binding_mismatch')
            request = json.loads(row['request'])
            fresh = row['state'] == 'intent'
            if fresh:
                self._live(request)
                db.execute("UPDATE enrollment_operations SET state='attempted' WHERE id=? AND state='intent'",
                           (operation_id,))
            return {'fresh': fresh, 'challenge': request,
                    'status': self._view(self._row(db, operation_id)),
                    'automatic_replay': False, 'managed_use_authorized': False}

    @_guard
    def execute(self, operation_id, executor):
        """One owned callback attempt; crash/ACK loss is unknown, never retried."""
        self._writable()
        _require(callable(executor), 'enrollment_executor_required')
        with self._transaction() as db:
            row = self._row(db, operation_id)
            request = json.loads(row['request'])
            if row['state'] != 'intent':
                return self._view(row)
            self._live(request)
            db.execute("UPDATE enrollment_operations SET state='attempted' WHERE id=?", (operation_id,))
        try:
            envelope = executor(_detached(request))
            return self.record_result(operation_id, envelope)
        except Exception:
            with self._transaction() as db:
                db.execute("UPDATE enrollment_operations SET state='unknown' WHERE id=? AND state='attempted'",
                           (operation_id,))
            return self.inspect(operation_id)

    @_guard
    def record_result(self, operation_id, envelope, *, require_live=False):
        self._writable()
        _require(type(require_live) is bool, 'enrollment_live_requirement_invalid')
        envelope = _detached(envelope, _MAX_JSON_BYTES)
        _object(envelope, ('format', 'operation_id', 'request_sha256', 'nonce', 'result', 'continuation', 'proxy'))
        _unblob(envelope['result'])
        with self._transaction() as db:
            row = self._row(db, operation_id)
            request = json.loads(row['request'])
            _require(row['state'] != 'intent', 'enrollment_execution_intent_not_admitted')
            _require(envelope['format'] == RESULT_FORMAT and envelope['operation_id'] == operation_id
                     and envelope['request_sha256'] == document_sha256(request)
                     and envelope['nonce'] == request['nonce']
                     and envelope['continuation'] == request['continuation']
                     and envelope['proxy'] == request['proxy'],
                     'enrollment_result_binding_mismatch')
            raw = _encoded(envelope).decode('ascii')
            if row['result'] is not None:
                _require(row['result'] == raw, 'enrollment_original_result_conflict')
            else:
                # External managed transports may require a still-live original
                # authorization for first receipt. Native late observation keeps
                # its existing default; identical saved bytes never renew a grant.
                if require_live:
                    self._live(request)
                db.execute("UPDATE enrollment_operations SET state='result_reported',result=?,result_sha256=? WHERE id=?",
                           (raw, document_sha256(envelope), operation_id))
            return self._view(self._row(db, operation_id))

    @_guard
    def verify(self, operation_id, oracle_bytes, connection_observer=None):
        """Independent full-byte oracle; proxy also needs actual connection evidence.

        The trusted observer performs its own original-binding connection
        challenge. An observation/lease alone never gives it device control.
        This is not remote native-tool/process/model execution attestation.
        """
        self._writable()
        _bytes(oracle_bytes)
        with self._transaction() as db:
            row = self._row(db, operation_id)
            request = json.loads(row['request'])
            self._live(request)
            _require(row['result'] is not None, 'enrollment_result_missing')
            envelope = json.loads(row['result'])
        _require(request['expected'] == {'bytes': len(oracle_bytes),
                 'sha256': hashlib.sha256(oracle_bytes).hexdigest()}, 'enrollment_oracle_binding_mismatch')
        _require(hmac.compare_digest(_unblob(envelope['result']), oracle_bytes),
                 'enrollment_result_oracle_mismatch')
        connection = None
        if request['proxy'] is not None:
            _require(callable(connection_observer), 'enrollment_proxy_connection_observer_required')
            connection = _detached(connection_observer(_detached(request)))
            _object(connection, ('source_device', 'proxy_binding', 'revision', 'checked_at',
                                  'expires_at', 'method', 'evidence_sha256'))
            _integer(connection['checked_at'])
            _integer(connection['expires_at'], 1)
            _hash(connection['evidence_sha256'])
            _require(connection['method'] == 'actual_connection_challenge'
                     and all(connection[name] == request['proxy'][name]
                             for name in ('source_device', 'proxy_binding', 'revision')),
                     'enrollment_proxy_connection_binding_mismatch')
        with self._transaction() as db:
            current = self._row(db, operation_id)
            now = self._live(request)
            _require(current['request'] == row['request'] and current['result'] == row['result'],
                     'enrollment_verification_input_changed')
            if connection is not None:
                _require(request['created_at'] <= connection['checked_at'] <= now
                         < connection['expires_at'] <= request['expires_at'],
                         'enrollment_proxy_connection_expired')
            verification = {'verification': 'challenge_bytes_verified', 'verifier': self.verifier,
                            'result_sha256': document_sha256(envelope),
                            'artifact_sha256': envelope['result']['sha256'],
                            'artifact_bytes': envelope['result']['bytes'], 'connection': connection,
                            'native_execution_attested': False}
            if current['verification'] is None:
                db.execute("UPDATE enrollment_operations SET state='verified',verification=? WHERE id=?",
                           (_encoded(verification).decode('ascii'), operation_id))
            else:
                saved = json.loads(current['verification'])
                _require(saved['result_sha256'] == verification['result_sha256'],
                         'enrollment_verification_conflict')
            return self._view(self._row(db, operation_id))

    def _publication(self, row):
        _require(row['verification'] is not None, 'enrollment_registration_requires_verification')
        request, verification = json.loads(row['request']), json.loads(row['verification'])
        until = request['expires_at']
        if verification['connection'] is not None:
            until = min(until, verification['connection']['expires_at'])
        return {'format': 'mesh-enrollment-publication/1', 'operation_id': row['id'],
                'mesh_id': self.document['mesh_id'], 'node_id': self.document['node_id'],
                'owner_binding': self.document['owner_binding'],
                'authority_id': self.document['join']['authority_id'],
                'enrollment_sha256': document_sha256(self.document),
                'request_sha256': document_sha256(request),
                'capability_id': request['capability_id'], 'action': request['action'],
                'revision': request['revision'], 'expires_at': until,
                'result_sha256': verification['result_sha256'],
                'artifact_sha256': verification['artifact_sha256'],
                'artifact_bytes': verification['artifact_bytes'],
                'proxy': request['proxy'], 'native_execution_attested': False,
                'source_control_granted': False, 'managed_use_authorized': False}

    @staticmethod
    def _registration_receipt(publication, receipt):
        receipt = _detached(receipt)
        _object(receipt, ('format', 'operation_id', 'authority_id', 'publication_sha256', 'registry_ref'))
        _text(receipt['registry_ref'])
        _require(receipt['format'] == REGISTRATION_FORMAT
                 and receipt['operation_id'] == publication['operation_id']
                 and receipt['authority_id'] == publication['authority_id']
                 and receipt['publication_sha256'] == document_sha256(publication),
                 'enrollment_registration_binding_mismatch')
        return receipt

    @_guard
    def register(self, operation_id, registrar):
        """Explicit registry attempt, distinct from bytes/connection verification."""
        self._writable()
        _require(callable(registrar), 'enrollment_registrar_required')
        with self._transaction() as db:
            row = self._row(db, operation_id)
            publication = self._publication(row)
            if row['registration_state'] != 'not_registered':
                return self._view(row)
            self._live(json.loads(row['request']))
            connection = json.loads(row['verification'])['connection']
            _require(connection is None or self._now() < connection['expires_at'],
                     'enrollment_proxy_connection_expired')
            db.execute("UPDATE enrollment_operations SET registration_state='attempted' WHERE id=?", (operation_id,))
        try:
            receipt = self._registration_receipt(publication, registrar(_detached(publication)))
        except Exception:
            with self._transaction() as db:
                db.execute("UPDATE enrollment_operations SET registration_state='unknown' WHERE id=? AND registration_state='attempted'",
                           (operation_id,))
            return self.inspect(operation_id)
        with self._transaction() as db:
            current = self._row(db, operation_id)
            raw = _encoded(receipt).decode('ascii')
            _require(current['registration'] is None or current['registration'] == raw,
                     'enrollment_original_registration_conflict')
            db.execute("UPDATE enrollment_operations SET registration_state='registered',registration=? WHERE id=?",
                       (raw, operation_id))
            return self._view(self._row(db, operation_id))

    @_guard
    def reconcile_registration(self, operation_id, observer):
        """Only read original publication; missing receipt never permits replay."""
        self._writable()
        _require(callable(observer), 'enrollment_registration_observer_required')
        with self._transaction() as db:
            row = self._row(db, operation_id)
            publication = self._publication(row)
            _require(row['registration_state'] != 'not_registered',
                     'enrollment_registration_not_attempted')
        receipt = self._registration_receipt(publication, observer(_detached(publication)))
        with self._transaction() as db:
            current = self._row(db, operation_id)
            raw = _encoded(receipt).decode('ascii')
            _require(current['registration'] is None or current['registration'] == raw,
                     'enrollment_original_registration_conflict')
            db.execute("UPDATE enrollment_operations SET registration_state='registered',registration=? WHERE id=?",
                       (raw, operation_id))
            return self._view(self._row(db, operation_id))

    def _view(self, row):
        request = json.loads(row['request'])
        verification = json.loads(row['verification']) if row['verification'] else None
        now = self._now()
        runtime_until = self.document['runtime']['expires_at']
        fresh = (request['created_at'] <= now < request['expires_at']
                 and now < self.authorization['valid_until']
                 and (runtime_until is None or now < runtime_until))
        if verification and verification['connection']:
            fresh = fresh and now < verification['connection']['expires_at']
        return {'format': 'mesh-enrollment-status/1', 'mesh_id': self.document['mesh_id'],
                'node_id': self.document['node_id'], 'runtime_kind': self.document['runtime']['kind'],
                'operation_id': row['id'], 'capability_id': request['capability_id'],
                'revision': request['revision'], 'state': row['state'],
                'request_sha256': document_sha256(request), 'expires_at': request['expires_at'],
                'result_sha256': document_sha256(json.loads(row['result'])) if row['result'] else None,
                'verification': verification['verification'] if verification else 'provider_reported' if row['result'] else 'unverified',
                'artifact_sha256': verification['artifact_sha256'] if verification else None,
                'artifact_bytes': verification['artifact_bytes'] if verification else None,
                'source_connection_verified': bool(verification and verification['connection']),
                'registration_state': row['registration_state'],
                'registration_evidence': 'trusted_registrar_receipt' if row['registration'] else None,
                'registered_candidate_fresh': fresh and row['registration_state'] == 'registered',
                'native_features_restricted': False, 'native_execution_attested': False,
                'source_control_granted': False, 'managed_use_authorized': False,
                'global_consensus_verified': False, 'automatic_replay': False}

    @_guard
    def inspect(self, operation_id):
        with self._transaction() as db:
            return self._view(self._row(db, operation_id))

    @_guard
    def read_result(self, operation_id):
        """Exact immutable private bytes remain readable after node/lease expiry."""
        with self._transaction() as db:
            row = self._row(db, operation_id)
            _require(row['result'] is not None, 'enrollment_result_missing')
            return _unblob(json.loads(row['result'])['result'])

    @_guard
    def receipt(self, operation_id):
        """Full original private evidence, not an authorization or summary.

        Includes the exact challenge, complete result envelope and original
        verification/registration. It stays readable after node/lease expiry.
        The caller is responsible for keeping private input/artifact bytes out
        of public logs and for independent source/device execution validation.
        """
        with self._transaction() as db:
            row = self._row(db, operation_id)
            envelope = json.loads(row['result']) if row['result'] else None
            if envelope is not None:
                _unblob(envelope['result'])
            return {'format': 'mesh-enrollment-receipt/1',
                    'enrollment_sha256': document_sha256(self.document),
                    'status': self._view(row), 'challenge': json.loads(row['request']),
                    'result': envelope,
                    'verification': json.loads(row['verification']) if row['verification'] else None,
                    'registration': json.loads(row['registration']) if row['registration'] else None}
