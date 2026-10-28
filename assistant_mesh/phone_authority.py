"""Phone transport over the running authority, never a second business ledger.

The owner attaches this to an already running API. No Store constructor,
recovery, task creation, enrollment challenge or network listener runs on import.
"""
import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import threading
from pathlib import Path

from .enrollment import document_sha256, REGISTRATION_FORMAT
from .enrollment_bootstrap import _private_json, _write_new_private
from .enrollment_relay import EnrollmentRelay
from .ingress import _identifier
from .session_nodes import FORMAT, TOOLS
from .store import Conflict


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


class LiveTaskGate:
    """Original Store writer lock spans validation AND journal commit.

    The journal is a separate enrollment database; holding BEGIN IMMEDIATE on
    the business Store prevents a concurrent cancel/revoke/reclaim from passing
    between the live task check and admission. Validators outside this context
    fail closed. No network or registrar hook runs while this lock is held.
    """
    def __init__(self, store):
        self.store, self.local = store, threading.local()

    @contextlib.contextmanager
    def admission(self):
        with self.store.transaction() as db:
            self.local.db = db
            try:
                yield
            finally:
                self.local.db = None

    def __call__(self, binding):
        db = getattr(self.local, 'db', None)
        if db is None:
            return False
        row = db.execute('SELECT * FROM tasks WHERE id=?', (binding['task_id'],)).fetchone()
        if row is None or not self.store._task_is_live(db, row, row['node'], binding['task_epoch']):
            return False
        entry = db.execute('SELECT request_id FROM ingress_requests WHERE task_id=?', (row['id'],)).fetchone()
        context = json.loads(row['context'])
        request = (entry['request_id'] if entry else context.get('request_id')
                   or context.get('origin', {}).get('message_id'))
        return request == binding['entry_request_id']


class RegistryEnrollment:
    """Actual Registry registrar and independent original-registration query.

    Only the separate Bootstrap oracle may verify execution. This hook checks
    that stored verification and publishes its digest as declared metadata; it
    neither fabricates a verification metric nor grants business use.
    """
    def __init__(self, registry, bootstrap):
        self.registry, self.bootstrap = registry, bootstrap

    def _receipt(self, publication, cap):
        digest = document_sha256(publication)
        if (cap['principal'] != 'node:' + publication['node_id']
                or cap['spec'].get('enrollment_publication_sha256') != digest):
            raise PermissionError('original_registry_publication_required')
        return {'format': REGISTRATION_FORMAT, 'operation_id': publication['operation_id'],
                'authority_id': publication['authority_id'], 'publication_sha256': digest,
                'registry_ref': 'capability:' + cap['id'] + ':' + str(cap['epoch'])}

    def register(self, publication):
        receipt = self.bootstrap.receipt(publication['operation_id'])
        if (receipt['status']['verification'] != 'challenge_bytes_verified'
                or receipt['verification']['result_sha256'] != publication['result_sha256']
                or receipt['challenge']['node_id'] != publication['node_id']
                or receipt['status']['request_sha256'] != publication['request_sha256']):
            raise PermissionError('independent_enrollment_verification_required')
        cap = self.registry.advertise('node:' + publication['node_id'],
            publication['capability_id'], 'compute.bytes',
            spec={'actions': [publication['action']], 'revision': publication['revision'],
                  'enrollment_publication_sha256': document_sha256(publication)},
            lease_seconds=max(1, min(86400, publication['expires_at'] - self.registry.store.clock())))
        return self._receipt(publication, cap)

    def observe(self, publication):
        return self._receipt(publication, self.registry.describe(publication['capability_id'], include_unavailable=False))


class PhoneAuthority:
    def __init__(self, api, config_path, *, relays=(), updates=None):
        self.api, self.store, self.sessions = api, api.store, api.session_nodes
        if self.sessions is None or self.sessions.store is not api.store:
            raise ValueError('phone_requires_original_session_authority')
        self.config_path, self.updates = str(config_path), updates
        self.lock = threading.RLock()
        self.relays = []
        for bootstrap, path in relays:
            gate = LiveTaskGate(self.store)
            self.relays.append((EnrollmentRelay(bootstrap, path, live_task_validator=gate,
                                                 clock=self.store.clock), gate))
        self.installed = self._config()
        self._install_bindings(self.installed)
        previous_request = self.sessions.request
        def session_request(*args, **kwargs):
            with self.lock:
                return previous_request(*args, **kwargs)
        self.sessions.request = session_request
        with self.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS phone_result_blobs(
                operation_id TEXT NOT NULL,capability_id TEXT NOT NULL,provider TEXT NOT NULL,
                receipt_id TEXT NOT NULL,sha256 TEXT NOT NULL,body BLOB NOT NULL,
                PRIMARY KEY(operation_id,capability_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS phone_report_ids(
                node_id TEXT NOT NULL,report_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                PRIMARY KEY(node_id,report_id))''')

    def _config(self, value=None):
        value = _private_json(self.config_path) if value is None else value
        if (set(value) != {'format', 'enabled', 'nodes'} or value['format'] != 'mesh-phone-authority/1'
                or type(value['enabled']) is not bool or not isinstance(value['nodes'], list)
                or len(value['nodes']) > 32):
            raise ValueError('invalid_phone_authority_config')
        seen, subjects, tokens = set(), set(), set()
        fields = {'node_id', 'subject', 'session_id', 'token_sha256', 'enabled',
                  'capabilities', 'scope', 'enrollment'}
        for node in value['nodes']:
            if (not isinstance(node, dict) or not fields <= set(node)
                    or set(node) - fields - {'visible_nodes'} or type(node['enabled']) is not bool):
                raise ValueError('invalid_phone_node_config')
            for key in ('node_id', 'subject', 'session_id'):
                _identifier(node[key], 'phone_' + key)
            digest = node['token_sha256']
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(c not in '0123456789abcdef' for c in digest)
                    or node['node_id'] in seen or node['subject'] in subjects or digest in tokens
                    or not isinstance(node['capabilities'], list) or 'leader' in node['capabilities']
                    or not isinstance(node['scope'], list)
                    or set(node['scope']) - {'phone:node', 'phone:updates'}):
                raise ValueError('invalid_phone_node_config')
            for cap in node['capabilities']:
                _identifier(cap, 'phone_capability')
            seen.add(node['node_id']); subjects.add(node['subject']); tokens.add(digest)
        return value

    def validate_policy(self, subject, session_id, capabilities, visible_nodes, *, observe=False):
        for key, value in (('subject', subject), ('session_id', session_id)):
            _identifier(value, 'phone_' + key)
        if (not isinstance(capabilities, list) or len(capabilities) > 32
                or len(set(capabilities)) != len(capabilities) or 'leader' in capabilities
                or not isinstance(visible_nodes, dict) or len(visible_nodes) > 128):
            raise ValueError('invalid_phone_owner_policy')
        for cap in capabilities:
            _identifier(cap, 'phone_capability')
        with self.store.transaction() as db:
            for node_id, metadata in visible_nodes.items():
                _identifier(node_id, 'phone_visible_node')
                if not isinstance(metadata, dict) or set(metadata) != {'display_name', 'capabilities'}:
                    raise ValueError('invalid_phone_visible_metadata')
                _identifier(metadata['display_name'], 'phone_visible_label')
                caps = metadata['capabilities']
                if not isinstance(caps, list) or len(caps) > 32 or len(set(caps)) != len(caps):
                    raise ValueError('invalid_phone_visible_capabilities')
                for cap in caps:
                    _identifier(cap, 'phone_visible_capability')
                row = db.execute('SELECT capabilities FROM nodes WHERE id=?', (node_id,)).fetchone() if observe else None
                if observe and (row is None or set(caps) - set(json.loads(row['capabilities']))):
                    raise ValueError('phone_visible_metadata_not_observed')

    def prepare_bindings(self, config):
        if not hasattr(self.sessions, 'node_bindings'):
            raise ValueError('phone_session_node_binding_seam_required')
        self._config(config)
        subjects, bindings = dict(self.sessions.subjects), dict(self.sessions.node_bindings)
        old_nodes = {n['subject']: n for n in getattr(self, 'installed', {}).get('nodes', [])}
        # Initial installation cannot claim an existing non-phone subject.
        installed_before = getattr(self, '_bindings_installed', False)
        for node in config['nodes']:
            subject = node['subject']
            self.validate_policy(subject, node['session_id'], node['capabilities'], node.get('visible_nodes', {}),
                                 observe=installed_before and subject not in old_nodes)
            binding = {'session_id': node['session_id'], 'node_id': node['node_id']}
            policy = {'capabilities': sorted(set(node['capabilities'])),
                      'visible_nodes': {k: v['display_name'] for k, v in node.get('visible_nodes', {}).items()},
                      'execute_own_requests': False}
            if subject in subjects and (not installed_before or subject not in old_nodes or
                    subjects[subject] != policy or bindings.get(subject) != binding):
                raise ValueError('phone_subject_already_bound')
            for other, bound in bindings.items():
                if other != subject and (bound['node_id'] == node['node_id'] or bound['session_id'] == node['session_id']):
                    raise ValueError('phone_identity_already_bound')
            subjects[subject], bindings[subject] = policy, binding
        return subjects, bindings

    def _install_bindings(self, config):
        with self.lock:
            subjects, bindings = self.prepare_bindings(config)
            self.sessions.subjects, self.sessions.node_bindings = subjects, bindings
            self._bindings_installed = True

    def principal(self, token, scope):
        with self.lock:
            return self._principal(token, scope)

    def _principal(self, token, scope):
        if not isinstance(token, str) or not 32 <= len(token) <= 4096:
            raise PermissionError('phone_authentication_required')
        current = self._config()
        # Owner may revoke/disable immediately; identity/capability widening
        # needs an explicit ready-hook reload, never a client request.
        for node in current['nodes']:
            installed = next((n for n in self.installed['nodes'] if n['node_id'] == node['node_id']), None)
            if installed is None or any(node[k] != installed[k] for k in node if k != 'enabled'):
                raise PermissionError('phone_owner_binding_changed')
            if hmac.compare_digest(node['token_sha256'], hashlib.sha256(token.encode()).hexdigest()):
                if not current['enabled'] or not node['enabled'] or scope not in node['scope']:
                    break
                return node
        raise PermissionError('phone_authentication_required')

    def enrollment(self, token, envelope):
        with self.lock:
            return self._enrollment(token, envelope)

    def _enrollment(self, token, envelope):
        for relay, gate in self.relays:
            try:
                relay._principal(token)
            except PermissionError:
                continue
            with gate.admission():
                return relay.handle(token, envelope)
        raise PermissionError('enrollment_operation_credential_required')

    def _binding(self, node):
        return {'owner_id': self.sessions.owner, 'source': self.sessions.source,
                'subject': node['subject'], 'scopes': ['tasks:read', 'tasks:submit']}

    def _presence(self, db, node, *, live=False, capability=None):
        row = self.sessions._session(db, self._binding(node), node['session_id'], live=live)
        view = self.sessions._session_view(row)
        if live and (row['agent_state'] == 'waiting_model' or row['model_state'] == 'unavailable'
                     or capability is not None and capability not in view['managed_capabilities']):
            raise Conflict('phone_capability_withdrawn')
        return row

    def node(self, token, envelope):
        with self.lock:
            return self._node(token, envelope)

    def _node(self, token, envelope):
        node = self.principal(token, 'phone:node')
        if not isinstance(envelope, dict) or set(envelope) != {'action', 'arguments'}:
            raise ValueError('invalid_phone_request')
        action, value = TOOLS.get(envelope['action']), envelope['arguments']
        if action is None or not isinstance(value, dict) or value.get('session_id') != node['session_id']:
            raise PermissionError('phone_session_binding_required')
        with self.lock:
            if action in ('connect', 'node_update', 'discover'):
                result = self.sessions.request(self._binding(node), action, value)
                if action == 'discover':
                    visible = {self.sessions._public_node_id(k): v for k, v in node.get('visible_nodes', {}).items()}
                    for item in result['nodes']:
                        metadata = visible.get(item['node_id'])
                        if metadata is not None:
                            item['capabilities'] = sorted(set(item['capabilities']) & set(metadata['capabilities']))
                            item['roles'] = []
                            item.pop('tasks', None)  # never leak other projects' task counts
                return result
            if action == 'work_pull':
                if set(value) != {'session_id', 'pull_id'}:
                    raise ValueError('invalid_phone_pull')
                _identifier(value['pull_id'], 'phone_pull')
                with self.store.transaction() as db:
                    self._presence(db, node, live=True)
                pending = self.api.allocations.pending('node:' + node['node_id'])
                with self.store.transaction() as db:
                    session = self._presence(db, node, live=True)
                    caps = self.sessions._session_view(session)['managed_capabilities']
                return {'format': FORMAT, 'mesh_id': self.sessions.mesh_id, 'work': None,
                        'pull_id': value['pull_id'], 'dispatch': [d for d in pending['dispatch']
                        if d['capability_id'] in caps], 'execution_authorized': False}
            if set(value) != {'session_id', 'report_id', 'provider'}:
                raise ValueError('invalid_phone_report')
            _identifier(value['report_id'], 'phone_report')
            with self.store.transaction() as db:
                self._presence(db, node)
                fingerprint = hashlib.sha256(_json(value).encode()).hexdigest()
                old = db.execute('SELECT fingerprint FROM phone_report_ids WHERE node_id=? AND report_id=?',
                                 (node['node_id'], value['report_id'])).fetchone()
                if old and old[0] != fingerprint:
                    raise Conflict('phone_report_identity_changed')
                db.execute('INSERT OR IGNORE INTO phone_report_ids VALUES(?,?,?)',
                           (node['node_id'], value['report_id'], fingerprint))
            result = self._provider(node, value['provider'])
            return {'format': FORMAT, 'mesh_id': self.sessions.mesh_id, 'provider': result}

    def _provider(self, node, provider):
        if not isinstance(provider, dict) or set(provider) != {'action', 'arguments'}:
            raise ValueError('invalid_phone_provider')
        action, args = provider['action'], provider['arguments']
        fields = {'inspect': {'operation_id', 'capability_id'},
                  'accept': {'operation_id', 'capability_id', 'epoch', 'receipt_id'},
                  'start': {'operation_id', 'capability_id', 'epoch', 'receipt_id'},
                  'unknown': {'operation_id', 'capability_id', 'epoch'},
                  'result': {'operation_id', 'capability_id', 'receipt_id', 'result'},
                  'settle': {'operation_id', 'capability_id', 'epoch', 'receipt_id', 'outcome', 'evidence'}}
        if action not in fields or not isinstance(args, dict) or set(args) != fields[action]:
            raise ValueError('invalid_phone_provider_action')
        actor, allocations = 'node:' + node['node_id'], self.api.allocations
        # An allocation may have multiple providers: never expose another
        # provider's operation through an arbitrary capability ID.
        with self.store.transaction() as db:
            dispatch = allocations._dispatch(db, args['operation_id'], args['capability_id'], actor)
            if action in ('accept', 'start'):
                self._presence(db, node, live=True, capability=args['capability_id'])
        if action == 'inspect':
            return allocations.inspect(actor, args['operation_id'])
        if action == 'result':
            return self._record_result(actor, args)
        if action == 'settle':
            with self.store.transaction() as db:
                blob = db.execute('SELECT * FROM phone_result_blobs WHERE operation_id=? AND capability_id=?',
                                  (args['operation_id'], args['capability_id'])).fetchone()
                evidence = args['evidence']
                if (blob is None or blob['provider'] != actor or blob['receipt_id'] != args['receipt_id']
                        or not isinstance(evidence, dict)
                        or evidence.get('result_reference') != 'phone-result:' + blob['sha256']
                        or evidence.get('sha256') != blob['sha256']
                        or evidence.get('bytes') != len(blob['body'])):
                    raise Conflict('phone_full_result_required_before_settlement')
        if action in ('accept', 'start'):
            def admission(db):
                self._presence(db, node, live=True, capability=args['capability_id'])
            return getattr(allocations, action)(actor, admission_check=admission, **args)
        return getattr(allocations, action)(actor, **args)

    def _record_result(self, actor, args):
        result = args['result']
        if not isinstance(result, dict) or set(result) != {'bytes', 'sha256', 'base64'}:
            raise ValueError('invalid_phone_result_blob')
        if type(result['bytes']) is not int or not 0 <= result['bytes'] <= 65536:
            raise ValueError('phone_result_too_large')
        raw = base64.b64decode(result['base64'], validate=True)
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != result['bytes'] or digest != result['sha256']:
            raise ValueError('phone_result_hash_mismatch')
        with self.store.transaction() as db:
            dispatch = self.api.allocations._dispatch(db, args['operation_id'], args['capability_id'], actor)
            if dispatch['receipt_id'] != args['receipt_id'] or dispatch['state'] not in ('running', 'unknown', 'completed', 'stopped'):
                raise Conflict('phone_original_started_receipt_required')
            old = db.execute('SELECT * FROM phone_result_blobs WHERE operation_id=? AND capability_id=?',
                             (args['operation_id'], args['capability_id'])).fetchone()
            if old and (old['sha256'] != digest or bytes(old['body']) != raw or old['receipt_id'] != args['receipt_id']):
                raise Conflict('phone_original_result_conflict')
            db.execute('INSERT OR IGNORE INTO phone_result_blobs VALUES(?,?,?,?,?,?)',
                (args['operation_id'], args['capability_id'], actor, args['receipt_id'], digest, raw))
        return {'result_reference': 'phone-result:' + digest, 'sha256': digest, 'bytes': len(raw)}

    def read_result(self, actor, operation_id, capability_id):
        """Private caller-side read using the original allocation authority."""
        self.api.allocations.inspect(actor, operation_id)
        with self.store.transaction() as db:
            allocation = self.api.allocations._allocation(db, operation_id)
            if actor not in (allocation['actor'], self.api.allocations.owner_principal):
                raise PermissionError('phone_result_caller_required')
            row = db.execute('SELECT * FROM phone_result_blobs WHERE operation_id=? AND capability_id=?',
                             (operation_id, capability_id)).fetchone()
            if row is None:
                raise Conflict('phone_result_not_recorded')
            return {'bytes': len(row['body']), 'sha256': row['sha256'],
                    'base64': base64.b64encode(row['body']).decode('ascii')}


def issue_node(bootstrap, registry, *, subject, session_id, capabilities, resource_url,
               scope=('phone:node', 'phone:updates')):
    """Private owner call only, after real enrollment and separate registry query.

    Returns a private node config + one-time export. Caller writes node config
    atomically and installs bindings under the owner lock; no bearer is retained by this service.
    """
    from urllib.parse import urlsplit
    url = urlsplit(resource_url)
    if (url.scheme != 'https' or not url.netloc or url.path != '/phone/node'
            or url.query or url.fragment or url.username or url.password):
        raise ValueError('phone_exact_https_resource_required')
    enrollment = verified_enrollment(bootstrap, registry)
    token = secrets.token_urlsafe(48)
    config = {'node_id': enrollment['node_id'], 'subject': subject, 'session_id': session_id,
              'capabilities': list(capabilities), 'scope': list(scope), 'enabled': True,
              'token_sha256': hashlib.sha256(token.encode()).hexdigest(), 'enrollment': enrollment}
    export = {'format': 'mesh-phone-bootstrap/1',
              'config': {'format': 'mesh-phone-client/1', 'resource_url': resource_url,
                         'node_id': enrollment['node_id'], 'session_id': session_id,
                         'mesh_id': enrollment['mesh_id'], 'authority_id': enrollment['authority_id']},
              'credential': {'kind': 'bearer', 'token': token, 'scope': list(scope)},
              'enrollment': enrollment}
    return config, export


def verified_enrollment(bootstrap, registry):
    """Original oracle receipt plus independent current Registry read; no token."""
    operation = bootstrap.config['challenge']['operation_id']
    receipt = bootstrap.receipt(operation)
    status = receipt['status']
    if (not receipt['owner_enabled'] or not status['registered_candidate_fresh']
            or status['verification'] != 'challenge_bytes_verified'
            or status['registration_state'] != 'registered'):
        raise PermissionError('phone_real_registered_enrollment_required')
    journal = bootstrap._journal(readonly=True)
    with journal._transaction() as db:
        publication = journal._publication(journal._row(db, operation))
    observed = RegistryEnrollment(registry, bootstrap).observe(publication)
    if observed != receipt['registration']:
        raise PermissionError('phone_original_registration_query_mismatch')
    enrollment = {'operation_id': operation, 'request_sha256': status['request_sha256'],
                  'node_id': status['node_id'], 'mesh_id': status['mesh_id'],
                  'authority_id': publication['authority_id'], 'registration_receipt': observed}
    return enrollment
