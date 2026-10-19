"""Owner-authorized Cloud ENTRANCE, independent of task-bound Cloud workers.

No Mesh claim, provider launch, model key, OAuth engine or execution authority
is created here. The fixed scoped ingress can queue work while Leader is idle.
Native execution/questions still use the original Mesh authority and leases.
"""
import contextlib
import hashlib
import hmac
import json
import sqlite3
import time

from .cloud_http import _resource
from .cloud_jobs import _private_dir, _private_file
from .cloud_membership import DISPLAY_ID, _checksum, _json, _sha
from .conversation import ConversationService, arguments as conversation_arguments
from .conversation_runtime import ConversationBridge
from .conversation_sync import sync_original_message
from .ingress import _identifier
from .mcp_http import RemoteIngressBackend
from .native_messaging import validate_message, project_response
from .store import Conflict


FORMAT = 'mesh-cloud-entrance/1'
GRANT_FORMAT = 'mesh-cloud-entrance-grant/1'
REQUEST_FORMAT = 'mesh-cloud-entrance-request/1'
_SCOPES = frozenset(('node:hello', 'tasks:read', 'tasks:submit'))
_ACTIONS = {'hello': 'node:hello', 'status': 'tasks:read', 'task_submit': 'tasks:submit',
    'task_status': 'tasks:read', 'task_result': 'tasks:read', 'conversation_open': 'tasks:submit',
    'conversation_receive': 'tasks:read', 'conversation_reply': 'tasks:submit',
    'conversation_receipt': 'tasks:read', 'conversation_sync': 'tasks:submit'}
_READS = frozenset(('status', 'task_status', 'task_result', 'conversation_receive', 'conversation_receipt'))


def validate_policy(document):
    fields = {'format', 'owner_binding', 'subject', 'project_id', 'callback_url'}
    if not isinstance(document, dict) or set(document) != fields or document['format'] != FORMAT:
        raise ValueError('cloud_entrance_policy_required')
    value = json.loads(_json(document))
    for key in ('owner_binding', 'subject', 'project_id'):
        _identifier(value[key], 'cloud_entrance_' + key)
    if _resource(value['callback_url'])[0] != '/cloud/node':
        raise ValueError('cloud_entrance_fixed_route_required')
    return value


def identity(document):
    """Pure stable entrance/node IDs; neither is provider attestation."""
    policy = validate_policy(document)
    fingerprint = _sha([FORMAT, policy['owner_binding'], DISPLAY_ID, policy['subject']])
    return {'entrance_id': 'cloud-entrance-' + fingerprint,
            'node_id': DISPLAY_ID + '-entrance-' + fingerprint[:32]}


class CloudEntrance:
    """One fixed owner/source/subject, private grants and original intents.

    Construction installs no credential and calls no Mesh backend. Grants are
    explicit owner operations; reconnect never renews them or replays work.
    """
    def __init__(self, directory, policy, backend, *, conversation_service=None, clock=time.time):
        self.policy = validate_policy(policy)
        self.ids, self.clock = identity(self.policy), clock
        if (not isinstance(backend, RemoteIngressBackend) or backend.source != DISPLAY_ID
                or backend.owner != self.policy['owner_binding']
                or set(backend.clients) != {self.policy['subject']}):
            raise ValueError('cloud_entrance_fixed_backend_required')
        self.backend, self.conversation = backend, conversation_service
        if conversation_service is not None:
            runtime = getattr(conversation_service, 'runtime', None)
            if (not isinstance(conversation_service, ConversationService)
                    or conversation_service.backend is not backend or conversation_service.source != DISPLAY_ID
                    or conversation_service.owner != self.policy['owner_binding']
                    or not isinstance(runtime, ConversationBridge)
                    or runtime.principal != self._principal_binding(['tasks:read', 'tasks:submit'])):
                raise ValueError('cloud_entrance_fixed_conversation_required')
        self.root = _private_dir(directory, create=True)
        self.database = self.root / 'cloud-entrances.sqlite'
        _private_file(self.database, create=True)
        with self._transaction(initialize=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cloud_entrance_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS cloud_entrance_grants(
                id TEXT PRIMARY KEY,body TEXT NOT NULL,fingerprint TEXT NOT NULL,
                credential_sha256 TEXT NOT NULL UNIQUE,state TEXT NOT NULL,hello_at REAL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS cloud_entrance_requests(
                id TEXT PRIMARY KEY,body TEXT NOT NULL,fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,response TEXT,created REAL NOT NULL)''')
            old = dict(db.execute('SELECT key,value FROM cloud_entrance_metadata'))
            if old != self._metadata():
                if (old or db.execute('SELECT 1 FROM cloud_entrance_grants LIMIT 1').fetchone()
                        or db.execute('SELECT 1 FROM cloud_entrance_requests LIMIT 1').fetchone()):
                    raise Conflict('cloud_entrance_namespace_changed')
                db.executemany('INSERT INTO cloud_entrance_metadata VALUES(?,?)', self._metadata().items())

    def _metadata(self):
        return {'format': FORMAT, 'policy': _json(self.policy), 'identity': _json(self.ids)}

    @contextlib.contextmanager
    def _transaction(self, initialize=False):
        _private_dir(self.root)
        _private_file(self.database)
        db = sqlite3.connect(self.database.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            if not initialize and dict(db.execute('SELECT key,value FROM cloud_entrance_metadata')) != self._metadata():
                raise Conflict('cloud_entrance_namespace_changed')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _grant(self, document):
        fields = {'format', 'grant_id', 'entrance_id', 'expires_at', 'scopes'}
        if (not isinstance(document, dict) or set(document) != fields or document['format'] != GRANT_FORMAT
                or document['entrance_id'] != self.ids['entrance_id']):
            raise ValueError('cloud_entrance_grant_required')
        value = json.loads(_json(document))
        _identifier(value['grant_id'], 'cloud_entrance_grant')
        if type(value['expires_at']) is not int:
            raise ValueError('cloud_entrance_expiry_invalid')
        scopes = value['scopes']
        if (not isinstance(scopes, list) or any(not isinstance(item, str) or item not in _SCOPES for item in scopes)
                or len(set(scopes)) != len(scopes) or not {'node:hello', 'tasks:read'} <= set(scopes)):
            raise ValueError('cloud_entrance_scopes_invalid')
        return value

    def _grant_row(self, db, grant_id):
        row = db.execute('SELECT * FROM cloud_entrance_grants WHERE id=?', (grant_id,)).fetchone()
        if row is None:
            raise PermissionError('cloud_entrance_grant_not_found')
        value = self._grant(json.loads(row['body']))
        if (row['id'] != value['grant_id'] or row['body'] != _json(value) or row['fingerprint'] != _sha(value)
                or row['state'] not in ('prepared', 'active', 'revoked')
                or (row['state'] == 'active' and row['hello_at'] is None)):
            raise Conflict('cloud_entrance_grant_invalid')
        _checksum(row['credential_sha256'])
        return row, value

    def _principal_binding(self, scopes):
        return {'owner_id': self.policy['owner_binding'], 'source': DISPLAY_ID,
                'subject': self.policy['subject'], 'scopes': [item for item in scopes if item != 'node:hello']}

    def _view(self, row, grant):
        return dict(self.ids, format=FORMAT, profile='owner_authorized_entrance', source=DISPLAY_ID,
            subject=self.policy['subject'], grant_id=grant['grant_id'], grant_state=row['state'],
            expires_at=grant['expires_at'], expired=grant['expires_at'] <= self.clock(),
            scopes=grant['scopes'], authenticated_hello_received=row['hello_at'] is not None,
            provider_identity_verified=False, provenance='owner_authorized_unattested_entrance',
            execution_verified=False, native_tools_intercepted=False, background_wake_supported=False)

    def install(self, document, *, token_sha256, authorized=False):
        """Owner-only delegation; no Mesh task/claim/provider launch prerequisite."""
        if authorized is not True:
            raise PermissionError('cloud_entrance_owner_required')
        grant = self._grant(document)
        if grant['expires_at'] <= self.clock():
            raise ValueError('cloud_entrance_expiry_invalid')
        token_sha256 = _checksum(token_sha256)
        with self._transaction() as db:
            old = db.execute('SELECT 1 FROM cloud_entrance_grants WHERE id=?', (grant['grant_id'],)).fetchone()
            if old:
                row, saved = self._grant_row(db, grant['grant_id'])
                if saved != grant or row['credential_sha256'] != token_sha256:
                    raise Conflict('cloud_entrance_original_grant_changed')
            else:
                if db.execute('SELECT 1 FROM cloud_entrance_grants WHERE credential_sha256=?', (token_sha256,)).fetchone():
                    raise Conflict('cloud_entrance_credential_already_bound')
                db.execute("INSERT INTO cloud_entrance_grants VALUES(?,?,?,?,'prepared',NULL)",
                    (grant['grant_id'], _json(grant), _sha(grant), token_sha256))
                row, grant = self._grant_row(db, grant['grant_id'])
            return self._view(row, grant)

    def revoke(self, grant_id, *, authorized=False):
        if authorized is not True:
            raise PermissionError('cloud_entrance_owner_required')
        with self._transaction() as db:
            self._grant_row(db, grant_id)
            db.execute("UPDATE cloud_entrance_grants SET state='revoked' WHERE id=?", (grant_id,))
            row, grant = self._grant_row(db, grant_id)
            return self._view(row, grant)

    def bootstrap(self, grant_id):
        """Private owner reads a NON-secret contract for the original Cloud task."""
        with self._transaction() as db:
            row, grant = self._grant_row(db, grant_id)
            return dict(self._view(row, grant), callback_url=self.policy['callback_url'],
                        request_format=REQUEST_FORMAT, credentials_included=False)

    def _authorized(self, db, grant_id, action):
        row, grant = self._grant_row(db, grant_id)
        if (row['state'] == 'revoked' or grant['expires_at'] <= self.clock()
                or _ACTIONS[action] not in grant['scopes']):
            raise PermissionError('cloud_entrance_not_authorized')
        if action not in ('hello', 'status') and row['hello_at'] is None:
            raise Conflict('cloud_entrance_hello_required')
        return row, grant

    def _authenticate(self, token, action):
        if action not in _ACTIONS:
            raise PermissionError('cloud_entrance_action_rejected')
        if (not isinstance(token, str) or not 32 <= len(token) <= 4096
                or any(ord(char) < 33 or ord(char) >= 127 for char in token)):
            raise PermissionError('cloud_entrance_auth_required')
        checksum = hashlib.sha256(token.encode('ascii')).hexdigest()
        with self._transaction() as db:
            candidate = db.execute('SELECT id FROM cloud_entrance_grants WHERE credential_sha256=?', (checksum,)).fetchone()
            if candidate is None:
                raise PermissionError('cloud_entrance_auth_required')
            row, grant = self._authorized(db, candidate['id'], action)
            if not hmac.compare_digest(checksum, row['credential_sha256']):
                raise PermissionError('cloud_entrance_auth_required')
            return grant

    def _arguments(self, action, arguments, request_id):
        if not isinstance(arguments, dict):
            raise ValueError('cloud_entrance_arguments_invalid')
        if action.startswith('conversation_'):
            if self.conversation is None:
                raise PermissionError('cloud_entrance_conversation_not_configured')
            operation = 'receipt' if action == 'conversation_sync' else action[len('conversation_'):]
            return conversation_arguments(operation, arguments)
        if action in ('hello', 'status'):
            if arguments:
                raise ValueError('cloud_entrance_arguments_invalid')
            return {}
        if action == 'task_submit':
            if set(arguments) != {'input'}:
                raise ValueError('cloud_entrance_arguments_invalid')
            return validate_message({'schema': 1, 'action': 'submit', 'payload': {
                'request_id': request_id, 'input': arguments['input'], 'project_id': self.policy['project_id']}})[1]
        if set(arguments) != {'entry_request_id'}:
            raise ValueError('cloud_entrance_arguments_invalid')
        original = _identifier(arguments['entry_request_id'], 'cloud_entrance_original_request')
        return {'request_id': original}

    def _response(self, action, request_id, data):
        return dict(self.ids, format=FORMAT, profile='owner_authorized_entrance', action=action,
            request_id=request_id, data=data, automatic_retry=False, retry_with_new_id=False,
            provider_identity_verified=False, execution_verified=False, background_wake_supported=False)

    def handle(self, token, envelope):
        fields = {'format', 'entrance_id', 'node_id', 'request_id', 'action', 'arguments'}
        if (not isinstance(envelope, dict) or set(envelope) != fields or envelope['format'] != REQUEST_FORMAT
                or any(envelope[key] != value for key, value in self.ids.items())):
            raise ValueError('cloud_entrance_request_invalid')
        action = envelope['action']
        if not isinstance(action, str):
            raise ValueError('cloud_entrance_request_invalid')
        grant = self._authenticate(token, action)
        request_id = _identifier(envelope['request_id'], 'cloud_entrance_request')
        args = self._arguments(action, envelope['arguments'], request_id)
        principal = self._principal_binding(grant['scopes'])
        if action in _READS:
            data = self._execute(action, args, principal, request_id, grant)
            self._authenticate(token, action)  # A bounded wait cannot disclose after expiry/revocation.
            return self._response(action, request_id, data)
        body, fingerprint = _json(envelope), _sha(envelope)
        with self._transaction() as db:
            self._authorized(db, grant['grant_id'], action)
            old = db.execute('SELECT * FROM cloud_entrance_requests WHERE id=?', (request_id,)).fetchone()
            if old and (old['body'] != body or old['fingerprint'] != fingerprint):
                raise Conflict('cloud_entrance_original_request_changed')
            if old is None and db.execute('SELECT COUNT(*) FROM cloud_entrance_requests').fetchone()[0] >= 4096:
                raise Conflict('cloud_entrance_request_capacity_exceeded')
            if action == 'hello':
                # Local state + ACK commit atomically; a new owner grant can
                # hello with the same stable identity without replaying Mesh work.
                db.execute("UPDATE cloud_entrance_grants SET state='active',hello_at=COALESCE(hello_at,?) WHERE id=?",
                           (self.clock(), grant['grant_id']))
                response = self._response(action, request_id, {'authenticated_hello_received': True})
                if old is None:
                    db.execute("INSERT INTO cloud_entrance_requests VALUES(?,?,?,'confirmed',?,?)",
                               (request_id, body, fingerprint, _json(response), self.clock()))
                return response
            if old:
                if old['state'] == 'confirmed':
                    return json.loads(old['response'])
                return self._response(action, request_id, {'state': 'unknown', 'automatic_retry': False})
            db.execute("INSERT INTO cloud_entrance_requests VALUES(?,?,?,'submitting',NULL,?)",
                       (request_id, body, fingerprint, self.clock()))
        response = self._response(action, request_id, self._execute(action, args, principal, request_id, grant))
        encoded = _json(response)
        if len(encoded.encode('utf8')) > 131072:
            raise Conflict('cloud_entrance_response_unknown')
        with self._transaction() as db:
            changed = db.execute("UPDATE cloud_entrance_requests SET state='confirmed',response=? "
                "WHERE id=? AND fingerprint=? AND state='submitting'", (encoded, request_id, fingerprint))
            if changed.rowcount != 1:
                raise Conflict('cloud_entrance_receipt_changed')
        self._authenticate(token, action)
        return response

    def _execute(self, action, args, principal, request_id, grant):
        if action == 'status':
            with self._transaction() as db:
                row, original = self._grant_row(db, grant['grant_id'])
                return self._view(row, original)
        if action in ('task_submit', 'task_status', 'task_result'):
            operation = {'task_submit': 'submit', 'task_status': 'status', 'task_result': 'result'}[action]
            raw = self.backend.request(principal, operation, args)
            return project_response(operation, args['request_id'], raw)['data']
        if action == 'conversation_sync':
            return sync_original_message(self.conversation, principal, args)
        operation = action[len('conversation_'):]
        runtime = self.conversation.runtime
        context = {key: args[key] for key in ('conversation_id', 'request_id')}
        report = None
        if operation == 'receive':
            try:
                report = runtime.before_receive(principal, context)
            except Exception:
                report = {'state': 'mesh_projection_unavailable', 'automatic_retry': False}
        result = self.conversation.request(principal, operation, args)
        if operation in ('reply', 'receipt'):
            callback = (runtime.after_reply if operation == 'reply' and result.get('new_message') is True
                        else runtime.original_receipt)
            try:
                report = callback(principal, context, args['message_id'])
            except Exception:
                report = {'state': 'native_delivery_unconfirmed', 'automatic_retry': False}
        return dict(result, **({'runtime': report} if report is not None else {}))


def assemble(directory, conversation_directory, policy, ingress_client, *, clock=time.time):
    """Private sidecar assembly, no Store/receiver/credential/provider creation.

    ingress_client is Root's existing dedicated scoped Cloud Client; server
    authentication must independently fix this exact owner/source/subject.
    """
    policy = validate_policy(policy)
    backend = RemoteIngressBackend({policy['subject']: ingress_client}, policy['owner_binding'], source=DISPLAY_ID)
    service = ConversationService(_private_dir(conversation_directory, create=True),
        policy['owner_binding'], backend, source=DISPLAY_ID, clock=clock)
    principal = {'owner_id': policy['owner_binding'], 'source': DISPLAY_ID, 'subject': policy['subject'],
                 'scopes': ['tasks:read', 'tasks:submit']}
    ConversationBridge(service, principal, ingress_client)
    return CloudEntrance(directory, policy, backend, conversation_service=service, clock=clock)
