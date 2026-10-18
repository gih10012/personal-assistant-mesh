"""Task-bound temporary membership for official OpenAI Codex Cloud.

This is a transport-independent authority facade, not a public listener or an
OpenAI API. A deployment authenticates a *separate* scoped node credential and
calls handle(). Its journal and full artifacts live outside the task VM, in a
private CloudJobs or owner-initiated CloudAdoptions directory. No local Codex
auth/home is copied. Owner-authorized enrollment is not provider attestation.

prepare() is an owner-side operation. A valid credential proves the delegated
identity, not independently verified Cloud execution or remote quiescence.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .cloud_jobs import _private_dir, _private_file, _text
from .store import Conflict


PROTOCOL = 'mesh-cloud-membership/1'
REQUEST_PROTOCOL = 'mesh-cloud-request/1'
DISPLAY_ID = 'openai-codex-cloud'
SCOPES = frozenset(('node:hello', 'task:read', 'task:report', 'artifact:write',
                    'tasks:submit', 'tasks:continue', 'capabilities:read', 'capabilities:call'))
_ACTION_SCOPE = {'hello': 'node:hello', 'status': 'task:read', 'report': 'task:report',
                 'artifact': 'artifact:write', 'task_submit': 'tasks:submit',
                 'task_status': 'task:read', 'task_continue': 'tasks:continue',
                 'discover': 'capabilities:read', 'call': 'capabilities:call',
                 'conversation_open': 'tasks:submit', 'conversation_receive': 'task:read',
                 'conversation_reply': 'tasks:submit', 'conversation_receipt': 'task:read'}
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


def _json(value):
    try:
        text = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                          allow_nan=False)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf8')
        return text
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid_cloud_membership_json') from None


def _sha(value):
    return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def node_id(job_id):
    _text(job_id, 'job_id', 200)
    return DISPLAY_ID + '-' + _sha(job_id)[:32]


def _checksum(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError('invalid_cloud_artifact_sha256')
    return value


def _callback(value):
    _text(value, 'callback_url', 2048)
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not parsed.path.startswith('/')
            or parsed.port not in (None, 443)):
        raise ValueError('invalid_cloud_callback_url')
    return value


class CloudMembership:
    """Owner facade with exact task/epoch/lease and no operator credential.

    ingress must be the existing owner-configured Ingress instance, when entry
    submission is enabled. capability_gateway(binding, action, arguments) is a
    deployment-owned thin managed gateway; it must independently validate live
    task authority and capability grants. No gateway means an explicit gate.
    lease_validator(binding) optionally checks the current Mesh authority epoch;
    True is required before every new write when configured. The persisted node
    lease is always checked regardless of the callback.
    """
    def __init__(self, jobs, *, ingress=None, capability_gateway=None,
                 lease_validator=None, clock=None, conversation_service=None):
        self.jobs = jobs
        self.ingress = ingress
        self.gateway = capability_gateway
        self.lease_validator = lease_validator
        self.conversation = conversation_service
        if conversation_service is not None:
            from .conversation import ConversationService
            if not isinstance(conversation_service, ConversationService) or conversation_service.source != DISPLAY_ID:
                raise ValueError('cloud_conversation_source_required')
        self.clock = clock or jobs.clock or time.time
        self.artifacts = _private_dir(jobs.path / 'node-artifacts', create=True)
        with jobs._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS cloud_memberships(
                job_id TEXT PRIMARY KEY,node_id TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL,fingerprint TEXT NOT NULL,credential_sha256 TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,hello_at REAL,terminal_report TEXT,created REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS cloud_node_requests(
                job_id TEXT NOT NULL,request_id TEXT NOT NULL,body TEXT NOT NULL,
                fingerprint TEXT NOT NULL,state TEXT NOT NULL,response TEXT,
                created REAL NOT NULL,PRIMARY KEY(job_id,request_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS cloud_node_artifacts(
                job_id TEXT NOT NULL,name TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,
                media_type TEXT NOT NULL,filename TEXT NOT NULL,created REAL NOT NULL,
                PRIMARY KEY(job_id,name))''')

    def _document(self, job_id, document):
        from .enrollment import validate_enrollment
        fields = {'format', 'mesh_task_id', 'mesh_epoch', 'lease_id', 'expires_at', 'request_id',
                  'enrollment', 'scopes', 'capabilities', 'callback_url'}
        if not isinstance(document, dict) or set(document) != fields or document['format'] != PROTOCOL:
            raise ValueError('invalid_cloud_membership_grant')
        value = json.loads(_json(document))
        for field in ('mesh_task_id', 'lease_id', 'request_id'):
            _text(value[field], field, 200)
        if type(value['mesh_epoch']) is not int or not 1 <= value['mesh_epoch'] < (1 << 63):
            raise ValueError('invalid_cloud_mesh_epoch')
        if type(value['expires_at']) is not int or value['expires_at'] <= self.clock():
            raise ValueError('invalid_cloud_membership_expiry')
        value['callback_url'] = _callback(value['callback_url'])
        scopes = value['scopes']
        capabilities = value['capabilities']
        if (not isinstance(scopes, list) or not scopes or len(scopes) > len(SCOPES)
                or any(not isinstance(item, str) or item not in SCOPES for item in scopes)
                or len(set(scopes)) != len(scopes) or 'node:hello' not in scopes):
            raise ValueError('invalid_cloud_membership_scopes')
        if (not isinstance(capabilities, list) or len(capabilities) > 64
                or len(set(_text(item, 'capability_id', 200) for item in capabilities)) != len(capabilities)):
            raise ValueError('invalid_cloud_membership_capabilities')
        enrollment = validate_enrollment(value['enrollment'])
        if (enrollment['node_id'] != node_id(job_id)
                or enrollment['runtime']['kind'] != 'temporary'
                or enrollment['runtime']['expires_at'] != value['expires_at']):
            raise ValueError('cloud_enrollment_binding_mismatch')
        value['enrollment'] = enrollment
        job = self.jobs.get(job_id)
        if (job['mesh_task_id'] != value['mesh_task_id'] or job['mesh_epoch'] != value['mesh_epoch']
                or job['request_id'] != value['request_id']):
            raise Conflict('cloud_membership_job_identity_mismatch')
        if self.ingress and enrollment['owner_binding'] != self.ingress.owner_id:
            raise PermissionError('cloud_membership_owner_mismatch')
        from .cloud_adoption import CloudAdoptions
        if isinstance(self.jobs, CloudAdoptions) and enrollment['owner_binding'] != self.jobs.owner:
            raise PermissionError('cloud_membership_owner_mismatch')
        return value

    def _row(self, db, job_id):
        self.jobs._row(db, job_id)
        row = db.execute('SELECT * FROM cloud_memberships WHERE job_id=?', (job_id,)).fetchone()
        if not row:
            raise PermissionError('cloud_membership_not_found')
        try:
            value = json.loads(row['body'])
            valid = (row['body'] == _json(value) and row['fingerprint'] == _sha(value)
                     and row['node_id'] == node_id(job_id) and _HASH.fullmatch(row['credential_sha256'])
                     and row['state'] in ('prepared', 'active', 'reported', 'closed'))
            job, body = self.jobs._row(db, job_id)
            valid = valid and all(value[key] == body[key] for key in
                                  ('mesh_task_id', 'mesh_epoch', 'request_id'))
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise Conflict('cloud_membership_journal_invalid')
        return row, value

    @staticmethod
    def _view(row, value):
        return {'format': PROTOCOL, 'job_id': row['job_id'], 'node_id': row['node_id'],
                'display_id': DISPLAY_ID, 'mesh_task_id': value['mesh_task_id'],
                'mesh_epoch': value['mesh_epoch'], 'lease_id': value['lease_id'],
                'expires_at': value['expires_at'], 'request_id': value['request_id'],
                'membership_state': row['state'], 'cloud_node_enrolled': row['hello_at'] is not None,
                'provider_reported': row['terminal_report'] is not None,
                'execution_verified': False, 'native_tools_intercepted': False}

    def prepare(self, job_id, document, *, token_sha256, authorized=False):
        """Bind an already privately supplied scoped credential, never mint one.

        For Mesh-launched CloudJobs this precedes its single Cloud launch. For
        CloudAdoptions it may follow the owner's already-running Cloud task;
        that journal never submits anything. This only prepares Mesh enrollment,
        not a live VM, provider proof, secret installation or network operation.
        """
        if authorized is not True:
            raise PermissionError('cloud_membership_owner_required')
        token_sha256 = _checksum(token_sha256)
        value = self._document(job_id, document)
        binding = dict(value, job_id=job_id, node_id=node_id(job_id))
        if self.lease_validator and self.lease_validator(binding) is not True:
            raise Conflict('stale_cloud_task_lease')
        body, fingerprint = _json(value), _sha(value)
        with self.jobs._transaction() as db:
            row = db.execute('SELECT * FROM cloud_memberships WHERE job_id=?', (job_id,)).fetchone()
            if row:
                row, prior = self._row(db, job_id)
                if row['fingerprint'] != fingerprint or row['credential_sha256'] != token_sha256:
                    raise Conflict('cloud_membership_grant_conflict')
                return dict(self._view(row, prior), prepared=False)
            job, _ = self.jobs._row(db, job_id)
            if job['attempted']:
                raise Conflict('cloud_membership_requires_prelaunch_binding')
            if db.execute('SELECT 1 FROM cloud_memberships WHERE credential_sha256=?',
                          (token_sha256,)).fetchone():
                raise Conflict('cloud_membership_credential_already_bound')
            db.execute('''INSERT INTO cloud_memberships(
                job_id,node_id,body,fingerprint,credential_sha256,state,created)
                VALUES(?,?,?,?,?,'prepared',?)''',
                       (job_id, node_id(job_id), body, fingerprint, token_sha256, self.clock()))
            row, value = self._row(db, job_id)
            return dict(self._view(row, value), prepared=True)

    def get(self, job_id):
        with self.jobs._transaction() as db:
            row, value = self._row(db, job_id)
            view = dict(self._view(row, value), expired=value['expires_at'] <= self.clock())
        from .cloud_adoption import CloudAdoptions
        if isinstance(self.jobs, CloudAdoptions):
            job = self.jobs.get(job_id)
            view.update({key: job[key] for key in (
                'enrollment_origin', 'provider_identity_verified', 'provenance')})
        return view

    def bootstrap(self, job_id):
        """Trusted owner reads the non-secret bootstrap, for the task prompt.

        credential_ref points to deployment-controlled injection, not a value.
        The referenced credential must be installed separately for the task.
        """
        with self.jobs._transaction() as db:
            row, value = self._row(db, job_id)
            return {'format': PROTOCOL, 'job_id': job_id, 'node_id': row['node_id'],
                    'callback_url': value['callback_url'], 'task_id': value['mesh_task_id'],
                    'epoch': value['mesh_epoch'], 'lease_id': value['lease_id'],
                    'expires_at': value['expires_at'], 'enrollment': value['enrollment'],
                    'scopes': value['scopes'], 'capabilities': value['capabilities'],
                    'credentials_included': False, 'native_tools_intercepted': False}

    def _principal(self, token, action):
        if not isinstance(action, str) or action not in _ACTION_SCOPE:
            raise PermissionError('cloud_node_action_not_authorized')
        try:
            valid = (isinstance(token, str) and 32 <= len(token.encode('utf8')) <= 4096
                     and not any(ord(char) < 33 or ord(char) == 127 for char in token))
        except UnicodeError:
            valid = False
        if not valid:
            raise PermissionError('cloud_node_auth_required')
        checksum = hashlib.sha256(token.encode('utf8')).hexdigest()
        with self.jobs._transaction() as db:
            candidate = db.execute('SELECT job_id FROM cloud_memberships WHERE credential_sha256=?',
                                   (checksum,)).fetchone()
            if not candidate:
                raise PermissionError('cloud_node_auth_required')
            row, value = self._row(db, candidate['job_id'])
            if (not hmac.compare_digest(checksum, row['credential_sha256'])
                    or row['state'] == 'closed' or value['expires_at'] <= self.clock()
                    or _ACTION_SCOPE[action] not in value['scopes']):
                raise PermissionError('cloud_node_action_not_authorized')
            job, _ = self.jobs._row(db, row['job_id'])
            from .cloud_adoption import CloudAdoptions
            adopted = isinstance(self.jobs, CloudAdoptions) and job['state'] == 'adopted'
            if job['state'] != 'submitted' and not adopted:
                raise Conflict('cloud_provider_identity_unconfirmed')
            if action != 'hello' and row['hello_at'] is None:
                raise Conflict('cloud_node_hello_required')
            return row['job_id'], value

    def _entry_peer(self, value, job_id):
        if self.ingress is None:
            raise PermissionError('cloud_entrypoint_not_configured')
        return {'role': 'ingress', 'ingress': {
            'owner_id': self.ingress.owner_id, 'source': DISPLAY_ID, 'subject': node_id(job_id),
            'scopes': ['tasks:submit', 'tasks:read']}}

    def _arguments(self, action, arguments, value):
        if not isinstance(arguments, dict):
            raise ValueError('invalid_cloud_node_arguments')
        args = json.loads(_json(arguments))
        if action.startswith('conversation_'):
            if self.conversation is None or self.conversation.owner != value['enrollment']['owner_binding']:
                raise PermissionError('cloud_conversation_not_configured')
            from .conversation import arguments as conversation_arguments
            args = conversation_arguments(action[len('conversation_'):], args)
        elif action in ('hello', 'status', 'discover'):
            if args:
                raise ValueError('invalid_cloud_node_arguments')
        elif action == 'artifact':
            if (set(args) != {'name', 'encoding', 'content_b64', 'sha256', 'bytes', 'media_type'}
                    or not isinstance(args['name'], str) or not _NAME.fullmatch(args['name'])
                    or args['encoding'] != 'base64' or type(args['bytes']) is not int
                    or not 0 <= args['bytes'] <= self.jobs.max_bytes):
                raise ValueError('invalid_cloud_artifact')
            _checksum(args['sha256'])
            _text(args['media_type'], 'media_type', 200)
            if (not isinstance(args['content_b64'], str)
                    or len(args['content_b64']) > (self.jobs.max_bytes + 2) // 3 * 4):
                raise ValueError('invalid_cloud_artifact')
            try:
                raw = base64.b64decode(args['content_b64'].encode('ascii'), validate=True)
            except (ValueError, UnicodeError):
                raise ValueError('invalid_cloud_artifact') from None
            if len(raw) != args['bytes'] or hashlib.sha256(raw).hexdigest() != args['sha256']:
                raise ValueError('cloud_artifact_content_mismatch')
        elif action == 'report':
            if set(args) != {'status', 'result', 'artifacts'} or args['status'] not in ('completed', 'failed'):
                raise ValueError('invalid_cloud_node_report')
            if (not isinstance(args['result'], str)
                    or len(args['result'].encode('utf8')) > self.jobs.max_bytes
                    or not isinstance(args['artifacts'], list) or len(args['artifacts']) > 64
                    or len(set(_checksum(item) for item in args['artifacts'])) != len(args['artifacts'])):
                raise ValueError('invalid_cloud_node_report')
        elif action == 'task_submit':
            if set(args) != {'input'}:
                raise ValueError('invalid_cloud_entry_request')
            if not isinstance(args['input'], str) or not args['input'].strip() or len(args['input'].encode('utf8')) > 65536:
                raise ValueError('invalid_cloud_entry_request')
        elif action == 'task_status':
            if set(args) != {'entry_request_id'}:
                raise ValueError('invalid_cloud_entry_status')
            _text(args['entry_request_id'], 'entry_request_id', 200)
        elif action == 'task_continue':
            if set(args) != {'entry_request_id', 'expected_epoch', 'task_result_sha256', 'instruction'}:
                raise ValueError('invalid_cloud_entry_continuation')
            _text(args['entry_request_id'], 'entry_request_id', 200)
            _checksum(args['task_result_sha256'])
            if (type(args['expected_epoch']) is not int or not 1 <= args['expected_epoch'] < (1 << 63)
                    or not isinstance(args['instruction'], str) or not args['instruction'].strip()
                    or len(args['instruction'].encode('utf8')) > 65536):
                raise ValueError('invalid_cloud_entry_continuation')
        elif action == 'call':
            if set(args) != {'capability_id', 'input'} or args['capability_id'] not in value['capabilities']:
                raise PermissionError('cloud_capability_not_authorized')
            _json(args['input'])
            if len(_json(args['input']).encode('utf8')) > 65536:
                raise ValueError('invalid_cloud_capability_request')
        if action in ('discover', 'call') and self.gateway is None:
            raise PermissionError('cloud_capability_gateway_not_configured')
        if action in ('task_submit', 'task_status', 'task_continue') and self.ingress is None:
            raise PermissionError('cloud_entrypoint_not_configured')
        if action in ('call', 'task_submit', 'task_continue') and self.lease_validator is None:
            raise PermissionError('cloud_task_authority_not_configured')
        return args

    def handle(self, token, envelope):
        """Authenticated transport call; JSON cannot choose its own authority.

        Write requests consume an immutable intent before dispatch. A crash or
        gateway error leaves unknown, and replay only reads that original state.
        Status/read calls do not create a new external operation.
        """
        fields = {'format', 'job_id', 'task_id', 'epoch', 'lease_id', 'request_id', 'action', 'arguments'}
        if not isinstance(envelope, dict) or set(envelope) != fields or envelope['format'] != REQUEST_PROTOCOL:
            raise ValueError('invalid_cloud_node_request')
        action = envelope['action']
        job_id, value = self._principal(token, action)
        if (envelope['job_id'] != job_id or envelope['task_id'] != value['mesh_task_id']
                or type(envelope['epoch']) is not int or envelope['epoch'] != value['mesh_epoch']
                or envelope['lease_id'] != value['lease_id']):
            raise PermissionError('cloud_node_task_binding_mismatch')
        request_id = _text(envelope['request_id'], 'node_request_id', 160)
        args = self._arguments(action, envelope['arguments'], value)
        binding = dict(value, job_id=job_id, node_id=node_id(job_id))
        if action in ('status', 'task_status', 'discover', 'conversation_receive', 'conversation_receipt'):
            response = self._execute(job_id, action, args, binding, request_id)
            if action.startswith('conversation_'):
                self._principal(token, action)  # A bounded wait cannot disclose after revocation/expiry.
            return response
        body, fingerprint = _json(envelope), _sha(envelope)
        with self.jobs._transaction() as db:
            row, current = self._row(db, job_id)
            # A revocation can race the earlier token lookup. Re-check at the
            # immutable intent boundary, not only before opening this journal.
            if row['state'] == 'closed' or current['expires_at'] <= self.clock():
                raise PermissionError('cloud_node_action_not_authorized')
            old = db.execute('SELECT * FROM cloud_node_requests WHERE job_id=? AND request_id=?',
                             (job_id, request_id)).fetchone()
            if old:
                if old['body'] != body or old['fingerprint'] != fingerprint:
                    raise Conflict('cloud_node_request_content_conflict')
                if old['state'] == 'confirmed':
                    return json.loads(old['response'])
                return {'request_id': request_id, 'job_id': job_id, 'state': 'unknown',
                        'automatic_retry': False, 'execution_verified': False}
            if row['state'] == 'reported' and action not in (
                    'task_submit', 'task_continue', 'conversation_open', 'conversation_reply'):
                raise Conflict('cloud_node_already_reported')
            if self.lease_validator and self.lease_validator(binding) is not True:
                raise Conflict('stale_cloud_task_lease')
            if db.execute('SELECT COUNT(*) FROM cloud_node_requests WHERE job_id=?',
                          (job_id,)).fetchone()[0] >= 256:
                raise Conflict('cloud_node_request_capacity_exceeded')
            # Validate the immutable artifacts before consuming terminal intent.
            if action == 'report':
                available = {item[0] for item in db.execute(
                    'SELECT sha256 FROM cloud_node_artifacts WHERE job_id=?', (job_id,))}
                if not set(args['artifacts']) <= available:
                    raise Conflict('cloud_report_artifact_missing')
            db.execute('INSERT INTO cloud_node_requests VALUES(?,?,?,?,\'submitting\',NULL,?)',
                       (job_id, request_id, body, fingerprint, self.clock()))
        response = self._execute(job_id, action, args, binding, request_id)
        encoded = _json(response)
        if len(encoded.encode('utf8')) > self.jobs.max_bytes:
            raise Conflict('cloud_node_response_oversize_unknown')
        with self.jobs._transaction() as db:
            self._row(db, job_id)
            changed = db.execute("UPDATE cloud_node_requests SET state='confirmed',response=? "
                                 "WHERE job_id=? AND request_id=? AND fingerprint=? AND state='submitting'",
                                 (encoded, job_id, request_id, fingerprint))
            if changed.rowcount != 1:
                raise Conflict('cloud_node_receipt_conflict')
        return response

    def _execute(self, job_id, action, args, binding, request_id):
        if action.startswith('conversation_'):
            operation = action[len('conversation_'):]
            principal = {'owner_id': binding['enrollment']['owner_binding'],
                'source': DISPLAY_ID, 'subject': node_id(job_id),
                'scopes': ['tasks:read'] + (['tasks:submit'] if 'tasks:submit' in binding['scopes'] else [])}
            runtime = getattr(self.conversation, 'runtime', None)
            context = {key: args[key] for key in ('conversation_id', 'request_id')}
            report = None
            if runtime is not None and operation == 'receive':
                report = runtime.before_receive(principal, context)
            result = self.conversation.request(principal, operation, args)
            if runtime is not None and operation in ('reply', 'receipt'):
                callback = (runtime.after_reply if operation == 'reply' and result.get('new_message') is True
                            else runtime.original_receipt)
                try:
                    report = callback(principal, context, args['message_id'])
                except Exception:
                    report = {'state': 'native_delivery_unconfirmed', 'automatic_retry': False}
            if report is not None:
                result = dict(result, runtime=report)
            return result
        if action == 'hello':
            with self.jobs._transaction() as db:
                self._row(db, job_id)
                db.execute("UPDATE cloud_memberships SET state='active',hello_at=? "
                           "WHERE job_id=? AND hello_at IS NULL AND state!='closed'",
                           (self.clock(), job_id))
            return dict(self.get(job_id), authenticated_hello=True, request_id=request_id)
        if action == 'status':
            return {'membership': self.get(job_id), 'job': self.jobs.get(job_id),
                    'artifacts': self.saved_artifacts(job_id)}
        if action == 'artifact':
            raw = base64.b64decode(args['content_b64'].encode('ascii'), validate=True)
            return dict(self._save_artifact(job_id, args['name'], raw, args['media_type']),
                        request_id=request_id)
        if action == 'report':
            artifact = self._save_artifact(job_id, '__terminal_result', args['result'].encode('utf8'),
                                           'text/plain; charset=utf-8')
            report = {'status': args['status'], 'result_sha256': artifact['sha256'],
                      'result_bytes': artifact['bytes'], 'artifacts': args['artifacts'],
                      'request_id': request_id, 'provider_reported': True, 'execution_verified': False}
            with self.jobs._transaction() as db:
                row, _ = self._row(db, job_id)
                if row['terminal_report'] and row['terminal_report'] != _json(report):
                    raise Conflict('cloud_terminal_report_conflict')
                # Persist the admitted callback for recovery, but never revive
                # a credential revoked while that callback was in flight.
                db.execute("UPDATE cloud_memberships SET state=CASE WHEN state='closed' THEN 'closed' "
                           "ELSE 'reported' END,terminal_report=? WHERE job_id=?",
                           (_json(report), job_id))
            return dict(self.get(job_id), report=report)
        if action == 'task_submit':
            return self.ingress.submit(self._entry_peer(binding, job_id), {
                'request_id': request_id, 'input': args['input'],
                'project_id': binding['enrollment']['continuation']['project_id']})
        if action in ('task_status', 'task_continue'):
            original = self.ingress.status(self._entry_peer(binding, job_id),
                                           {'request_id': args['entry_request_id']})
            if action == 'task_status':
                return original
            return self.ingress.store.continue_task({
                'continuation_id': 'cloud-' + _sha([job_id, request_id])[:64],
                'task_id': original['task_id'], 'expected_epoch': args['expected_epoch'],
                'task_result_sha256': args['task_result_sha256'], 'instruction': args['instruction']})
        if self.gateway is None:
            raise PermissionError('cloud_capability_gateway_not_configured')
        # The gateway must filter discovery to this declared capability set and
        # re-check current Mesh task/epoch authority. No raw operator RPC route.
        return self.gateway(binding, action, args)

    def _save_artifact(self, job_id, name, raw, media_type):
        checksum = hashlib.sha256(raw).hexdigest()
        filename = _sha([job_id, name, checksum]) + '.bin'
        path = self.artifacts / filename
        with self.jobs._transaction() as db:
            self._row(db, job_id)
            old = db.execute('SELECT * FROM cloud_node_artifacts WHERE job_id=? AND name=?',
                             (job_id, name)).fetchone()
            if old:
                if (old['sha256'] != checksum or old['bytes'] != len(raw)
                        or old['media_type'] != media_type or old['filename'] != filename):
                    raise Conflict('cloud_artifact_name_conflict')
            else:
                count, size, ordinary = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(bytes),0),"
                    "COALESCE(SUM(CASE WHEN name!='__terminal_result' THEN 1 ELSE 0 END),0) "
                    "FROM cloud_node_artifacts WHERE job_id=?", (job_id,)).fetchone()
                if (count >= 65 or (name != '__terminal_result' and ordinary >= 64)
                        or size + len(raw) > self.jobs.max_bytes * 8):
                    raise Conflict('cloud_artifact_capacity_exceeded')
            _private_dir(self.artifacts)
            fd, temporary = tempfile.mkstemp(prefix='.cloud-', dir=str(self.artifacts))
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary, str(path))
                except FileExistsError:
                    _private_file(path)
                    if path.read_bytes() != raw:
                        raise Conflict('cloud_artifact_saved_content_changed')
            finally:
                os.unlink(temporary)
            _private_file(path)
            directory = os.open(str(self.artifacts), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if not old:
                db.execute('INSERT INTO cloud_node_artifacts VALUES(?,?,?,?,?,?,?)',
                           (job_id, name, checksum, len(raw), media_type, filename, self.clock()))
        return {'name': name, 'sha256': checksum, 'bytes': len(raw), 'media_type': media_type,
                'full_artifact_saved': True, 'artifact_content_verified': False}

    def saved_artifacts(self, job_id):
        """Owner reads persistent metadata and validates full bytes, without CLI."""
        with self.jobs._transaction() as db:
            self._row(db, job_id)
            rows = db.execute('SELECT * FROM cloud_node_artifacts WHERE job_id=? ORDER BY name',
                              (job_id,)).fetchall()
        items = []
        for row in rows:
            expected = _sha([job_id, row['name'], _checksum(row['sha256'])]) + '.bin'
            if (row['filename'] != expected or type(row['bytes']) is not int
                    or not 0 <= row['bytes'] <= self.jobs.max_bytes):
                raise Conflict('cloud_artifact_journal_invalid')
            path = self.artifacts / row['filename']
            _private_dir(self.artifacts)
            _private_file(path)
            raw = path.read_bytes()
            if len(raw) != row['bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise Conflict('cloud_artifact_saved_content_changed')
            items.append({'name': row['name'], 'sha256': row['sha256'], 'bytes': row['bytes'],
                          'media_type': row['media_type'], 'full_artifact_saved': True,
                          'artifact_content_verified': False})
        return items

    def read_artifact(self, job_id, name):
        for item in self.saved_artifacts(job_id):
            if item['name'] == name:
                path = self.artifacts / (_sha([job_id, name, item['sha256']]) + '.bin')
                return path.read_bytes()
        raise PermissionError('cloud_artifact_not_found')

    def saved_result(self, job_id):
        """Read the full terminal callback even after lease/VM expiry/close."""
        with self.jobs._transaction() as db:
            row, _ = self._row(db, job_id)
            if not row['terminal_report']:
                raise Conflict('cloud_result_not_reported')
            report = json.loads(row['terminal_report'])
        raw = self.read_artifact(job_id, '__terminal_result')
        if len(raw) != report['result_bytes'] or hashlib.sha256(raw).hexdigest() != report['result_sha256']:
            raise Conflict('cloud_result_saved_content_changed')
        return {'membership': self.get(job_id), 'report': report, 'result': raw.decode('utf8'),
                'artifacts': self.saved_artifacts(job_id)}

    def close(self, job_id, *, authorized=False):
        """Revoke only this shared node credential; native Cloud tools untouched."""
        if authorized is not True:
            raise PermissionError('cloud_membership_owner_required')
        with self.jobs._transaction() as db:
            self._row(db, job_id)
            db.execute("UPDATE cloud_memberships SET state='closed' WHERE job_id=?", (job_id,))
        return self.get(job_id)
