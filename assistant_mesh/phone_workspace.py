"""Owner-installed file tools on the original phone allocation path."""
import base64
import hashlib
import json
from pathlib import Path

from .config import private_json
from .ingress import _identifier


FORMAT = 'amita-phone-workspace/1'
STATUS = 'mesh_phone_workspace_status'
SUFFIX = ':workspace-files-v1'
KIND = 'device.workspace'
READ = 'file.read.workspace'
WRITE = 'file.write.workspace'
MAX_BYTES = 65536
MAX_INLINE_WRITE_BYTES = 4096
OBSERVATION = '/v1/phone/workspace/observation'
VERIFIER = 'source:phone-workspace-presence'
METRIC = 'phone.workspace.reachable'
SPEC = {'contract': FORMAT, 'actions': [READ, WRITE], 'max_read_bytes': MAX_BYTES,
        'max_inline_write_bytes': MAX_INLINE_WRITE_BYTES,
        'workspace': 'app_owned', 'execution_verification': 'declared_only'}


def path_valid(value):
    if (not isinstance(value, str) or not value or len(value) > 255 or '\\' in value
            or len(json.dumps(value)) > 512
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        return False
    return all(part not in ('', '.', '..') for part in value.split('/'))


class PhoneWorkspace:
    def __init__(self, authority):
        self.authority = authority
        self.path = Path(authority.config_path).absolute().with_name('workspace-owner.json')
        self.policy = self._read()

    def _read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        value = private_json(self.path)
        if not isinstance(value, dict) or set(value) != {'format', 'node_id', 'session_id'} or value['format'] != FORMAT:
            raise ValueError('phone_workspace_owner_policy_invalid')
        for key in ('node_id', 'session_id'):
            _identifier(value[key], 'phone_workspace_' + key)
        return value

    def matches(self, node):
        return self.policy is not None and all(self.policy[key] == node[key] for key in ('node_id', 'session_id'))

    def available(self, node):
        if not self.matches(node):
            return False
        try:
            return self._read() == self.policy
        except (OSError, ValueError):
            return False

    @staticmethod
    def identity(node):
        return node['node_id'] + SUFFIX

    def capabilities(self, node, installed=False):
        enabled = self.matches(node) if installed else self.available(node)
        return node['capabilities'] + ([self.identity(node)] if enabled else [])

    def install(self, node):
        if not self.matches(node):
            return
        store, resources = self.authority.store, self.authority.api.resources
        identity, actor = self.identity(node), 'node:' + node['node_id']
        with store.transaction() as db:
            row = db.execute('SELECT * FROM capabilities WHERE id=?', (identity,)).fetchone()
        if row is None:
            resources.advertise(actor, identity, KIND, spec=SPEC, health='unavailable')
        else:
            self.validate_registry(row, node)
        self.presence(node, online=False)

    def validate_registry(self, row, node):
        if (row['principal'] != 'node:' + node['node_id'] or row['kind'] != KIND
                or row['epoch'] != 1 or json.loads(row['spec']) != SPEC):
            raise PermissionError('phone_workspace_original_capability_required')

    def presence(self, node, online=True):
        if not self.matches(node):
            return
        store = self.authority.store
        with store.transaction() as db:
            identity, now = self.identity(node), store.clock()
            row = self.authority.api.resources._find(db, identity)
            self.validate_registry(row, node)
            if row['revoked'] or row['health'] == 'failed':
                return
            expires = now
            if online and self.available(node):
                session = self.authority._presence(db, node)
                view = self.authority.sessions._session_view(session)
                if identity in view['managed_capabilities']:
                    expires = session['expires']
            db.execute('UPDATE capabilities SET health=?,deadline=?,updated=? WHERE id=? AND revoked=0',
                       ('ready' if expires > now else 'unavailable', expires, now, identity))

    def read_status(self, node, arguments):
        if arguments != {'session_id': node['session_id']}:
            raise PermissionError('phone_workspace_original_session_required')
        enabled = self.available(node)
        if enabled:
            with self.authority.store.transaction() as db:
                row = self.authority.api.resources._find(db, self.identity(node))
                self.validate_registry(row, node)
                enabled = not row['revoked'] and row['health'] != 'failed'
        return {'format': FORMAT, 'enabled': enabled, 'capability_id': self.identity(node), 'epoch': 1}

    def validate_dispatch(self, db, node, allocation, selection):
        if not self.available(node):
            raise PermissionError('phone_workspace_owner_binding_required')
        row = self.authority.api.resources._find(db, self.identity(node))
        self.validate_registry(row, node)
        if row['revoked'] or row['health'] == 'failed' or selection['epoch'] != 1:
            raise PermissionError('phone_workspace_original_capability_required')
        workload, action = selection['workload'], selection['action']
        path = workload.get('path')
        expected = {'task_id': allocation['task_id'], 'task_epoch': allocation['task_epoch'], 'path': path}
        if not path_valid(path) or selection['scope'] != expected:
            raise ValueError('phone_workspace_exact_path_scope_required')
        if action == READ and set(workload) == {'path', 'max_bytes'}:
            if type(workload['max_bytes']) is int and 0 <= workload['max_bytes'] <= MAX_BYTES:
                return
        if action == WRITE and set(workload) == {'path', 'input'}:
            blob = workload['input']
            if (isinstance(blob, dict) and set(blob) == {'bytes', 'sha256', 'base64'}
                    and type(blob['bytes']) is int and 0 <= blob['bytes'] <= MAX_INLINE_WRITE_BYTES
                    and isinstance(blob['base64'], str) and len(blob['base64']) <= 5464):
                try:
                    raw = base64.b64decode(blob['base64'], validate=True)
                except ValueError:
                    raw = None
                if (raw is not None and len(raw) == blob['bytes'] and hashlib.sha256(raw).hexdigest() == blob['sha256']
                        and base64.b64encode(raw).decode('ascii') == blob['base64']):
                    return  # Device verifies the same bytes again before writing.
        raise ValueError('phone_workspace_workload_invalid')

    def observe(self, actor, payload):
        from .store import Conflict
        if (not actor.startswith('node:') or not isinstance(payload, dict)
                or set(payload) != {'task_id', 'task_epoch', 'selection'}):
            raise PermissionError('phone_workspace_original_worker_required')
        task_id, epoch, selection = payload['task_id'], payload['task_epoch'], payload['selection']
        _identifier(task_id, 'phone_workspace_task')
        if (type(epoch) is not int or epoch < 1 or not isinstance(selection, dict)
                or set(selection) != {'capability_id', 'epoch', 'action', 'scope', 'workload', 'grant_id'}
                or type(selection['epoch']) is not int or not isinstance(selection['workload'], dict)):
            raise ValueError('phone_workspace_original_selection_required')
        _identifier(selection['grant_id'], 'phone_workspace_grant')
        authority = self.authority
        cap_id = selection['capability_id']
        _identifier(cap_id, 'phone_workspace_capability')
        with authority.lock:
            nodes = authority._config()['nodes']
            node = next((entry for entry in nodes if entry['enabled'] and self.identity(entry) == cap_id
                         and entry in authority.installed['nodes']), None)
            if node is None or not self.available(node):
                raise PermissionError('phone_workspace_owner_binding_required')
            with authority.store.transaction() as db:
                registry, allocations = authority.api.resources, authority.api.allocations
                now = authority.store.clock()
                allocation = allocations._task(db, actor, task_id, epoch, now)[0]
                row = registry._find(db, cap_id)
                if not registry._live(row, now):
                    raise Conflict('phone_workspace_not_live')
                self.validate_dispatch(db, node, {'task_id': allocation['id'], 'task_epoch': allocation['epoch']}, selection)
                allocations._authorize(db, actor, row, selection, now)
                presence = authority._presence(db, node, live=True, capability=cap_id)
                sampled = presence['expires'] - authority.sessions.ttl
            metric = registry.observe(VERIFIER, cap_id, METRIC, 1, 'observed',
                'authenticated phone workspace presence', sample_time=sampled, verification='verified', epoch=1,
                evidence={'scope': selection['scope'], 'workload': selection['workload']})
        return {'observation_id': metric['id'], 'metric': METRIC, 'unit': 'observed',
                'max_age_seconds': authority.sessions.ttl}
