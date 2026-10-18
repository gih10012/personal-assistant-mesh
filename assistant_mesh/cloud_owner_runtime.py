"""Opt-in owner hook on an already-open receiver, not a Cloud job launcher.

Install is an explicit owner operation. Starting the loopback listener only
reopens that grant; it cannot create/claim/renew a Mesh task or restore a Store.
The private config maps the original grant to the actual native claim holder.
No model/API credential or operator bearer is accepted here.
"""
import json
import sqlite3
import threading
from http.server import HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

from .cloud_adoption import CloudAdoptions
from .cloud_http import make_http_handler, _resource
from .cloud_jobs import _digest, _private_dir, _private_file
from .cloud_membership import CloudMembership, DISPLAY_ID, PROTOCOL, _checksum, _json, node_id
from .conversation import ConversationService
from .conversation_authority import ConversationAuthority, ROUTE
from .conversation_runtime import ConversationBridge
from .ingress import _identifier
from .mcp_http import LocalIngressBackend
from .native_messaging import _private_bytes, _unique, _nonfinite
from .store import Conflict


FORMAT = 'mesh-cloud-owner-runtime/1'
_TABLES = frozenset(('cloud_adoption_metadata', 'cloud_adoptions', 'cloud_memberships',
                     'cloud_node_requests', 'cloud_node_artifacts'))


def validate_config(document):
    """Private installation data, never fields supplied by a Cloud request."""
    fields = {'format', 'adoptions_directory', 'conversation_directory', 'loopback_port',
              'task_holder', 'grant', 'token_sha256'}
    if not isinstance(document, dict) or set(document) != fields or document['format'] != FORMAT:
        raise ValueError('cloud_owner_configuration_required')
    value = json.loads(_json(document))
    for key in ('adoptions_directory', 'conversation_directory'):
        path = value[key]
        if (not isinstance(path, str) or not Path(path).is_absolute() or '..' in Path(path).parts
                or str(Path(path)) != path):
            raise ValueError('cloud_owner_private_directory_required')
    if value['adoptions_directory'] == value['conversation_directory']:
        raise ValueError('cloud_owner_separate_conversation_directory_required')
    port = value['loopback_port']
    if type(port) is not int or not (port == 0 or 1024 <= port <= 65535):
        raise ValueError('cloud_owner_loopback_port_invalid')
    _identifier(value['task_holder'], 'cloud_claim_holder')
    _checksum(value['token_sha256'])
    grant = value['grant']
    if (not isinstance(grant, dict) or grant.get('format') != PROTOCOL
            or not {'mesh_task_id', 'mesh_epoch', 'request_id', 'lease_id', 'expires_at',
                    'enrollment', 'callback_url'} <= set(grant)):
        raise ValueError('cloud_owner_original_grant_required')
    CloudAdoptions._binding({key: grant[key] for key in ('mesh_task_id', 'mesh_epoch', 'request_id')})
    _identifier(grant['lease_id'], 'cloud_lease_id')
    if type(grant['expires_at']) is not int:
        raise ValueError('cloud_owner_original_grant_required')
    if _resource(grant['callback_url'])[0] != '/cloud/node':
        raise ValueError('cloud_owner_fixed_route_required')
    return value


def load_config(path):
    """Existing strict private-file reader; no env/credential discovery."""
    return validate_config(json.loads(_private_bytes(path).decode('utf8'),
                                      object_pairs_hook=_unique, parse_constant=_nonfinite))


class _OriginalLease:
    def __init__(self, api, config):
        ingress, results, store = (getattr(api, name, None) for name in ('ingress', 'ingress_results', 'store'))
        if (ingress is None or results is None or store is None
                or ingress.store is not store or results.store is not store
                or ingress.owner_id != results.owner_id or ingress.authority != results.authority
                or not callable(getattr(store, '_task_is_live', None))):
            raise ValueError('cloud_owner_existing_authority_required')
        self.store, self.grant = store, config['grant']
        self.holder, self.owner = config['task_holder'], ingress.owner_id
        enrollment = self.grant['enrollment']
        if (not isinstance(enrollment, dict) or enrollment.get('owner_binding') != self.owner
                or not isinstance(enrollment.get('join'), dict)
                or enrollment['join'].get('authority_id') != ingress.authority):
            raise PermissionError('cloud_owner_original_authority_mismatch')

    def __call__(self, binding):
        # lease_id/request_id come from the privately installed ORIGINAL grant.
        # A caller cannot turn a different live task/holder into its delegation.
        if any(binding.get(key) != value for key, value in self.grant.items()):
            return False
        return self.current()

    def current(self):
        if self.grant['expires_at'] <= self.store.clock():
            return False
        with self.store.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (self.grant['mesh_task_id'],)).fetchone()
            return self.store._task_is_live(db, task, node=self.holder, epoch=self.grant['mesh_epoch'])


def _unprepared(reason):
    return {'format': FORMAT, 'prepared': False, 'serving': False, 'state': 'not_prepared',
            'reason': reason, 'automatic_retry': False, 'cloud_execution_verified': False}


def install_owner(api, document, *, authorized=False):
    """Explicit owner install only: adopt/prepare one grant, never start work.

    A real live Store claim must already exist. This function never creates a
    task, claims one, sends a Cloud job, renews a lease or starts a listener.
    """
    if authorized is not True:
        raise PermissionError('cloud_owner_install_authorization_required')
    config = validate_config(document)
    lease = _OriginalLease(api, config)
    if not lease.current():
        return _unprepared('original_task_not_live')
    grant = config['grant']
    jobs = CloudAdoptions(config['adoptions_directory'], lease.owner, clock=api.store.clock)
    binding = {key: grant[key] for key in ('mesh_task_id', 'mesh_epoch', 'request_id')}
    job_id = jobs.adopt(binding, authorized=True)['job_id']
    membership = CloudMembership(jobs, ingress=api.ingress, lease_validator=lease)
    result = membership.prepare(job_id, grant, token_sha256=config['token_sha256'], authorized=True)
    if result['membership_state'] == 'closed':
        return _unprepared('owner_membership_revoked')
    _private_dir(config['conversation_directory'], create=True)
    return {'format': FORMAT, 'prepared': True, 'serving': False, 'state': 'owner_installed',
            'installation_created': result['prepared'], 'membership': membership.get(job_id),
            'bootstrap': membership.bootstrap(job_id), 'cloud_execution_verified': False}


class _FixedConversationClient:
    def __init__(self, authority, principal):
        self.authority = authority
        self.peer = {'role': 'ingress', 'ingress': principal}

    def request(self, path, body):
        if path != ROUTE:
            raise PermissionError('cloud_owner_private_route_rejected')
        return self.authority.dispatch(self.peer, body)


class _LoopbackServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class CloudOwnerRuntime:
    """One closable /cloud/node listener; construction does not bind a socket.

    Use in the original receiver's ready hook and close in its runner finally.
    Missing/stale/revoked installation returns not_prepared, without creating a
    replacement grant. close() stops transport, not the remote VM or task.
    """
    def __init__(self, api, document):
        self.config = validate_config(document)
        self.api, self.lease = api, _OriginalLease(api, self.config)
        self.membership = self.bridge = None
        self._server = self._thread = None
        self._closed = False
        self._lock = threading.RLock()

    def _reopen(self):
        root = Path(self.config['adoptions_directory'])
        database = root / 'cloud-adoptions.sqlite'
        if not root.exists() or not database.exists():
            return _unprepared('owner_install_missing')
        _private_dir(root)
        _private_file(database)
        # Read-only preflight prevents a missing installation from being
        # recreated by journal/facade constructors during ordinary startup.
        db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=10)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if _TABLES <= tables and [row[0] for row in db.execute(
                    'SELECT owner FROM cloud_adoption_metadata')] != [_digest(self.lease.owner)]:
                raise Conflict('cloud_adoption_owner_mismatch')
        finally:
            db.close()
        if not _TABLES <= tables:
            return _unprepared('owner_install_missing')
        _private_dir(root / 'node-artifacts')
        jobs = CloudAdoptions(root, self.lease.owner, clock=self.api.store.clock)
        grant = self.config['grant']
        job_id = jobs.planned_id(grant['mesh_task_id'], grant['request_id'])
        membership = CloudMembership(jobs, ingress=self.api.ingress, lease_validator=self.lease)
        try:
            with jobs._transaction() as db:
                row, original = membership._row(db, job_id)
                if original != grant or row['credential_sha256'] != self.config['token_sha256']:
                    raise Conflict('cloud_owner_original_grant_changed')
                if row['state'] == 'closed':
                    return _unprepared('owner_membership_revoked')
        except PermissionError:
            return _unprepared('owner_install_missing')
        _private_dir(self.config['conversation_directory'])
        backend = LocalIngressBackend(self.api.ingress, self.api.ingress_results, source=DISPLAY_ID)
        service = ConversationService(self.config['conversation_directory'], self.lease.owner,
                                      backend, source=DISPLAY_ID, clock=self.api.store.clock)
        principal = {'owner_id': self.lease.owner, 'source': DISPLAY_ID, 'subject': node_id(job_id),
                     'scopes': ['tasks:read', 'tasks:submit']}
        authority = ConversationAuthority(self.api.ingress, sources=(DISPLAY_ID,))
        self.bridge = ConversationBridge(service, principal, _FixedConversationClient(authority, principal))
        membership.conversation = service
        self.membership = membership
        return None

    def start(self):
        with self._lock:
            if self._closed:
                return _unprepared('owner_runtime_closed')
            if self._server is not None:
                return self.status()
            if not self.lease.current():
                return _unprepared('original_task_not_live')
            unavailable = self._reopen()
            if unavailable is not None:
                return unavailable
            handler = make_http_handler(self.membership, self.config['grant']['callback_url'],
                                        trusted_proxy_peers=('127.0.0.1',))
            server = _LoopbackServer(('127.0.0.1', self.config['loopback_port']), handler)
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.1),
                                      name='mesh-cloud-owner-loopback', daemon=True)
            try:
                thread.start()
            except BaseException:
                server.server_close()
                raise
            self._server, self._thread = server, thread
            return self.status()

    def status(self):
        with self._lock:
            if self._server is None:
                return _unprepared('owner_runtime_closed' if self._closed else 'owner_runtime_not_started')
            job_id = self.membership.jobs.planned_id(self.config['grant']['mesh_task_id'],
                                                    self.config['grant']['request_id'])
            membership = self.membership.get(job_id)
            live = (membership['membership_state'] != 'closed' and not membership['expired']
                    and self.lease.current())
            return {'format': FORMAT, 'prepared': live, 'serving': self._thread.is_alive(),
                    'state': 'loopback_serving' if live else 'loopback_authority_unavailable',
                    'loopback_port': self._server.server_port, 'job_id': job_id,
                    'cloud_execution_verified': False}

    def close(self):
        with self._lock:
            server, thread = self._server, self._thread
            self._server = self._thread = None
            self._closed = True
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join(2)
        return {'format': FORMAT, 'serving': False, 'state': 'loopback_closed',
                'remote_quiescence_verified': False, 'cloud_execution_verified': False}
