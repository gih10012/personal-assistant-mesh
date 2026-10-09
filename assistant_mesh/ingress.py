"""Owner-bound submission/status policy, not an OAuth or MCP transport.

Callers must pass the peer returned by server authentication, NEVER a peer from
request JSON. This optional surface cannot impersonate a worker/operator or
choose a task lease, native session, credential, or execution capability. Open
task descriptions and requested project/agent labels remain model-readable.

One authority transaction creates the ordinary Store task and its immutable
source/subject/request binding. Lost replies use the same request ID; no status,
model/node change, or policy upgrade creates a replacement task. Native tools
remain untouched. Status is deliberately metadata-only: raw results, rollouts,
checkpoints, account state and other subjects' tasks are not exposed here.
"""
import hashlib
import json

from .store import Conflict


PROTOCOL = 'owner-ingress/1'
SCOPES = frozenset(('tasks:submit', 'tasks:read'))
_STATUSES = frozenset(('pending', 'running', 'completed', 'failed', 'waiting_auth',
                       'waiting_backend', 'waiting_children', 'continuing',
                       'needs_review', 'paused', 'waiting_remote'))
_REQUEST_COLUMNS = frozenset(('request_key', 'source', 'subject', 'request_id',
                              'fingerprint', 'body', 'task_id', 'created'))


def _identifier(value, label, maximum=200):
    try:
        valid = (isinstance(value, str) and bool(value) and value == value.strip()
                 and all(ord(char) >= 32 and ord(char) != 127 for char in value)
                 and len(value.encode('utf8')) <= maximum)
    except UnicodeError:
        valid = False
    if not valid:
        raise ValueError('invalid_ingress_' + label)
    return value


def _encoded(value):
    try:
        # Actual UTF-8 validation rejects lone surrogates even though canonical
        # ASCII escaping would otherwise hide them in a fingerprint.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf8')
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid_ingress_json') from None


def _digest(value):
    return hashlib.sha256(_encoded(value).encode('utf8')).hexdigest()


def validate_config(config):
    """Validate deployment-only policy before opening any runtime table.

    This does not verify a token or read a credential. A transport must resolve
    OAuth issuer/audience/expiry/subject/revocation before selecting a peer.
    """
    if (not isinstance(config, dict) or 'owner_id' not in config
            or set(config) - {'owner_id', 'required', 'session_scope'}):
        raise ValueError('invalid_ingress_config')
    owner = _identifier(config['owner_id'], 'owner_id')
    required = config.get('required', ['leader'])
    if (not isinstance(required, list) or not 1 <= len(required) <= 64
            or not all(isinstance(item, str) for item in required)):
        raise ValueError('invalid_ingress_required')
    required = [_identifier(item, 'required') for item in required]
    if len(set(required)) != len(required):
        raise ValueError('invalid_ingress_required')
    # Store uses a 256-character limit; the stricter byte limit also keeps
    # model-readable context and native session keys bounded on every host.
    scope = _identifier(config.get('session_scope', 'leader:owner'), 'session_scope', 256)
    return {'owner_id': owner, 'required': list(required), 'session_scope': scope}


def validate_peer(peer, owner_id):
    """Return detached, deployment-authenticated ingress identity and scopes.

    token_file is allowed as ordinary server configuration, but never opened,
    copied into the returned binding, or emitted in responses/model context.
    Empty scopes are a valid no-access/revoked configuration.
    """
    _identifier(owner_id, 'owner_id')
    if (not isinstance(peer, dict) or peer.get('role') != 'ingress'
            or set(peer) - {'role', 'token_file', 'ingress'}):
        raise PermissionError('ingress_principal_required')
    if 'token_file' in peer:
        _identifier(peer['token_file'], 'token_file', 4096)
    binding = peer.get('ingress')
    if (not isinstance(binding, dict)
            or set(binding) != {'owner_id', 'source', 'subject', 'scopes'}):
        raise ValueError('invalid_ingress_peer')
    owner = _identifier(binding['owner_id'], 'owner_id')
    if owner != owner_id:
        raise PermissionError('ingress_owner_mismatch')
    source = _identifier(binding['source'], 'source')
    subject = _identifier(binding['subject'], 'subject', 1024)
    scopes = binding['scopes']
    if (not isinstance(scopes, list) or len(scopes) > len(SCOPES)
            or not all(isinstance(scope, str) and scope in SCOPES for scope in scopes)
            or len(set(scopes)) != len(scopes)):
        raise ValueError('invalid_ingress_scopes')
    return {'owner_id': owner, 'source': source, 'subject': subject,
            'scopes': list(scopes)}


class Ingress:
    """Optional local business contract backed by an existing authority Store.

    Server integration must route ingress peers before generic discovery/status
    routes and must never accept request-supplied role/identity. This module has
    no public HTTP listener, credential discovery, OAuth/MCP dependency, worker
    enrollment, steering/control or outbound network operation.
    """
    def __init__(self, store, authority_id, config):
        policy = validate_config(config)
        self.authority = _identifier(authority_id, 'authority_id')
        self.owner_id = policy['owner_id']
        self.required = tuple(policy['required'])
        self.session_scope = policy['session_scope']
        self.store = store
        expected = {'schema': '1', 'authority': self.authority, 'owner': self.owner_id}
        with store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ingress_metadata(
                key TEXT PRIMARY KEY,value TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS ingress_requests(
                request_key TEXT PRIMARY KEY,source TEXT NOT NULL,subject TEXT NOT NULL,
                request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,body TEXT NOT NULL,
                task_id TEXT NOT NULL UNIQUE,created REAL NOT NULL,
                UNIQUE(source,subject,request_id))''')
            if ({row[1] for row in db.execute('PRAGMA table_info(ingress_metadata)')}
                    != {'key', 'value'} or
                    {row[1] for row in db.execute('PRAGMA table_info(ingress_requests)')}
                    != _REQUEST_COLUMNS):
                raise Conflict('ingress_schema_mismatch')
            recorded = dict(db.execute('SELECT key,value FROM ingress_metadata'))
            if not recorded:
                if db.execute('SELECT 1 FROM ingress_requests LIMIT 1').fetchone():
                    raise Conflict('ingress_namespace_missing')
                for key, value in expected.items():
                    db.execute('INSERT INTO ingress_metadata VALUES(?,?)', (key, value))
            elif recorded != expected:
                raise Conflict('ingress_namespace_mismatch')

    def _principal(self, peer, scope):
        binding = validate_peer(peer, self.owner_id)
        if scope not in binding['scopes']:
            raise PermissionError('ingress_scope_required')
        return binding

    def _request_key(self, binding, request_id):
        return _digest([PROTOCOL, self.authority, self.owner_id,
                        binding['source'], binding['subject'], request_id])

    @staticmethod
    def _submit_body(payload):
        if (not isinstance(payload, dict) or not {'request_id', 'input'} <= set(payload)
                or set(payload) - {'request_id', 'input', 'project_id', 'agent_id'}):
            raise ValueError('invalid_ingress_submit')
        request_id = _identifier(payload['request_id'], 'request_id')
        text = payload['input']
        try:
            valid = isinstance(text, str) and bool(text.strip()) and len(text.encode('utf8')) <= 65536
        except UnicodeError:
            valid = False
        if not valid:
            raise ValueError('invalid_ingress_input')
        body = {'input': text}
        for key in ('project_id', 'agent_id'):
            label = payload.get(key)
            # Null and absent labels have the same task meaning.
            if label is not None:
                body[key] = _identifier(label, key)
        return request_id, body

    @staticmethod
    def _binding(db, request_key, binding, request_id):
        row = db.execute('''SELECT request_key,source,subject,request_id,
            fingerprint,body,task_id FROM ingress_requests WHERE request_key=?''',
                         (request_key,)).fetchone()
        if row and (row['source'] != binding['source'] or row['subject'] != binding['subject']
                    or row['request_id'] != request_id):
            raise Conflict('ingress_request_identity_conflict')
        if row and (row['task_id'] != 'ingress-' + request_key
                    or row['fingerprint'] != hashlib.sha256(row['body'].encode('utf8')).hexdigest()):
            raise Conflict('ingress_request_binding_conflict')
        return row

    @staticmethod
    def _view(db, row):
        # Do not reuse Store.task_status: it intentionally exposes native
        # checkpoint metadata and raw results to the full owner/operator API.
        task = db.execute('''SELECT id,status,
            CASE WHEN result IS NOT NULL AND result<>'' THEN 1 ELSE 0 END AS result_available
            FROM tasks WHERE id=?''', (row['task_id'],)).fetchone()
        if not task:
            raise Conflict('ingress_task_binding_missing')
        known = task['status'] in _STATUSES
        return {'format': PROTOCOL, 'request_id': row['request_id'],
                'task_id': task['id'], 'accepted': True,
                'status': task['status'] if known else 'unrecognized',
                'status_recognized': known, 'result_available': bool(task['result_available']),
                'execution_verified': False, 'native_tools_intercepted': False}

    def submit(self, peer, payload):
        binding = self._principal(peer, 'tasks:submit')
        request_id, body = self._submit_body(payload)
        request_key = self._request_key(binding, request_id)
        encoded = _encoded(body)
        fingerprint = hashlib.sha256(encoded.encode('utf8')).hexdigest()
        with self.store.transaction() as db:
            old = self._binding(db, request_key, binding, request_id)
            if old:
                if old['fingerprint'] != fingerprint or old['body'] != encoded:
                    raise Conflict('ingress_request_content_conflict')
                return dict(self._view(db, old), task_created=False)
            task_id = 'ingress-' + request_key
            # A derived ID without its ingress record is NOT a retry or an
            # authorization to adopt a preexisting owner/worker task.
            if db.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
                raise Conflict('ingress_task_identity_conflict')
            context = {'session_scope': self.session_scope,
                       'origin': {'kind': 'owner-ingress', 'request_key': request_key},
                       'ingress': {'source': binding['source'],
                                   'subject_sha256': _digest([binding['source'], binding['subject']])}}
            if 'project_id' in body:
                context['project_id'] = body['project_id']
            if 'agent_id' in body:
                context['ingress']['requested_agent_id'] = body['agent_id']
            created = self.store._create_task_in_db(db, body['input'], required=list(self.required),
                                                   task_id=task_id, context=context)
            if created != task_id:
                raise Conflict('ingress_task_identity_conflict')
            db.execute('''INSERT INTO ingress_requests(request_key,source,subject,
                request_id,fingerprint,body,task_id,created) VALUES(?,?,?,?,?,?,?,?)''',
                       (request_key, binding['source'], binding['subject'], request_id,
                        fingerprint, encoded, task_id, self.store.clock()))
            row = self._binding(db, request_key, binding, request_id)
            return dict(self._view(db, row), task_created=True)

    def status(self, peer, payload):
        binding = self._principal(peer, 'tasks:read')
        if not isinstance(payload, dict) or set(payload) != {'request_id'}:
            raise ValueError('invalid_ingress_status')
        request_id = _identifier(payload['request_id'], 'request_id')
        request_key = self._request_key(binding, request_id)
        with self.store.transaction() as db:
            row = self._binding(db, request_key, binding, request_id)
            if not row:
                # No global task ID fallback; a foreign subject and an absent
                # request produce the same verdict and reveal no task metadata.
                raise PermissionError('ingress_request_not_visible')
            return self._view(db, row)
