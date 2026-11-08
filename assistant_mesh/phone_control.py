"""Owner-installed GUI contract on the original Registry and Allocations.

Separate service presence is data in the incumbent Store, not a new physical
node or operation ledger. It never supplies device approvals or result bytes.
"""
import hashlib
import json
import base64

from .ingress import _identifier
from .session_nodes import arguments
from .store import Conflict

CONTRACT = 'amita-phone-ui-control/1'
KIND = 'device.gui'
ACTIONS = frozenset(('inspect', 'action'))
PREFIX = 'amita.phone.control.presence:'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _invalid_json_constant(unused):
    raise ValueError('phone_control_json_blob_required')


def validate_adapter(node):
    session, adapter = node.get('control_session_id'), node.get('installed_control_adapter')
    if session is None and adapter is None:
        return
    _identifier(session, 'phone_control_session')
    if (session == node['session_id'] or not isinstance(adapter, dict)
            or set(adapter) != {'enabled', 'capability_id', 'contract', 'actions', 'epoch'}
            or type(adapter['enabled']) is not bool
            or adapter['capability_id'] != node['node_id'] + ':ui-control-v1'
            or adapter['capability_id'] in node['capabilities'] or adapter['contract'] != CONTRACT
            or type(adapter['epoch']) is not int or adapter['epoch'] < 1
            or not isinstance(adapter['actions'], list) or not adapter['actions']
            or any(not isinstance(a, str) for a in adapter['actions'])
            or set(adapter['actions']) - ACTIONS or len(set(adapter['actions'])) != len(adapter['actions'])):
        raise ValueError('invalid_phone_installed_control_adapter')


def spec(adapter):
    return {'contract': CONTRACT, 'actions': sorted(adapter['actions']),
            'installation_authority': 'owner_installed_adapter',
            'execution_verification': 'declared_only', 'local_approval_required': True,
            'human_input_fence_required': True, 'max_json_bytes': 65536}


class PhoneControl:
    def __init__(self, service):
        self.service, self.store, self.registry = service, service.store, service.api.resources

    def install(self, node):
        adapter = node.get('installed_control_adapter')
        if adapter is None:
            return
        identity, actor = adapter['capability_id'], 'node:' + node['node_id']
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM capabilities WHERE id=?', (identity,)).fetchone()
        if row is None and adapter['enabled']:
            if adapter['epoch'] != 1:
                raise Conflict('phone_control_original_epoch_required')
            # Registration is a DECLARED installed adapter. The byte challenge
            # is deliberately absent from spec and supplies no GUI verification.
            self.registry.advertise(actor, identity, KIND, spec=spec(adapter), health='unavailable')
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM capabilities WHERE id=?', (identity,)).fetchone()
            if row is None:
                return
            if (row['principal'] != actor or row['kind'] != KIND
                    or json.loads(row['spec']) != spec(adapter) or row['epoch'] != adapter['epoch']):
                raise Conflict('phone_control_owner_installed_contract_changed')
            # A receiver reload never implies an accessibility service is live.
            db.execute("UPDATE capabilities SET health=CASE WHEN health='failed' THEN health ELSE 'unavailable' END,deadline=?,updated=? WHERE id=? AND revoked=0",
                       (self.store.clock(), self.store.clock(), identity))
            # Retain the latest bounded presence snapshot so a lost control ACK
            # can reconcile after reload. Only a new authenticated control
            # request below may renew availability; installation never does.

    def select(self, node, session_id):
        if session_id == node['session_id']:
            return node
        adapter = node.get('installed_control_adapter')
        if (session_id != node.get('control_session_id') or adapter is None or not adapter['enabled']):
            raise PermissionError('phone_session_binding_required')
        return dict(node, session_id=session_id, capabilities=[adapter['capability_id']], _control_service=True)

    def presence(self, db, node, *, live=False, capability=None):
        saved = db.execute('SELECT value FROM meta WHERE key=?', (PREFIX + node['node_id'],)).fetchone()
        row = json.loads(saved['value']) if saved else None
        if row is None or row['session_id'] != node['session_id']:
            raise PermissionError('phone_control_session_not_connected')
        adapter = node['installed_control_adapter']
        capable = capability is None or capability in row['capabilities'] and capability == adapter['capability_id']
        if live and (not adapter['enabled'] or not row['online'] or row['expires'] <= self.store.clock()
                     or row['agent_state'] == 'waiting_model' or row['model_state'] == 'unavailable' or not capable):
            raise Conflict('phone_control_service_presence_required')
        return row

    def view(self, node, row):
        online = row['online'] and row['expires'] > self.store.clock()
        return {'node_id': node['node_id'], 'session_id': node['session_id'],
                'lifecycle': 'accessibility-service', 'generation': row['generation'],
                'network': {'state': 'online' if online else 'offline', 'expires_at': row['expires'],
                            'presence_ttl_seconds': self.service.sessions.ttl},
                'agent': {'state': row['agent_state'], 'observation': 'service_reported'},
                'model': {'state': row['model_state'], 'observation': 'service_reported'},
                'managed_capabilities': row['capabilities'] if online else [],
                'capabilities': row['capabilities'], 'execution_verified': False,
                'capability_verification': 'declared', 'native_tools_restricted': False}

    def control(self, node, action, payload):
        value = arguments(action, payload)
        if set(value['capabilities']) - set(node['capabilities']):
            raise PermissionError('phone_control_capability_scope_required')
        fingerprint = hashlib.sha256(canonical([action, value]).encode()).hexdigest()
        receipt_id = 'phone-control:' + hashlib.sha256(canonical([node['session_id'], value['control_id']]).encode()).hexdigest()
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM interactive_controls WHERE node_id=? AND control_id=?',
                             (node['node_id'], receipt_id)).fetchone()
            if old and (old['kind'] != action or old['fingerprint'] != fingerprint):
                raise Conflict('phone_control_identity_changed')
            saved = db.execute('SELECT value FROM meta WHERE key=?', (PREFIX + node['node_id'],)).fetchone()
            row = json.loads(saved['value']) if saved else None
            if action == 'node_update' and row is None:
                raise PermissionError('phone_control_session_not_connected')
            now = self.store.clock()
            if old is None:
                row = {'session_id': node['session_id'], 'online': value['online'],
                       'capabilities': value['capabilities'], 'agent_state': value['agent_state'],
                       'model_state': value['model_state'], 'expires': now + self.service.sessions.ttl if value['online'] else now,
                       'generation': (row['generation'] if row else 0) + 1}
                db.execute('INSERT INTO interactive_controls VALUES(?,?,?,?)',
                           (node['node_id'], receipt_id, action, fingerprint))
            elif action == 'connect':
                # A connect retry renews the latest retained service observation,
                # preserving later local capability withdrawal after a lost ACK.
                if row is None:
                    raise Conflict('phone_control_fresh_connect_required')
                row['online'], row['expires'] = True, now + self.service.sessions.ttl
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (PREFIX + node['node_id'], canonical(row)))
            self._renew(db, node, row)
            return self.service.sessions._base(session=self.view(node, row), control_id=value['control_id'],
                                                business_task_created=False, control_retry_safe=True)

    def _renew(self, db, node, presence):
        adapter = node['installed_control_adapter']
        row = self.registry._find(db, adapter['capability_id'])
        if (row['revoked'] or row['epoch'] != adapter['epoch'] or row['principal'] != 'node:' + node['node_id']
                or row['kind'] != KIND or json.loads(row['spec']) != spec(adapter)):
            return
        live = (adapter['enabled'] and presence['online'] and presence['expires'] > self.store.clock()
                and adapter['capability_id'] in presence['capabilities'] and row['health'] != 'failed'
                and presence['agent_state'] != 'waiting_model' and presence['model_state'] != 'unavailable')
        now = self.store.clock()
        db.execute('UPDATE capabilities SET deadline=?,health=?,updated=? WHERE id=? AND epoch=? AND revoked=0',
            (presence['expires'] if live else now, 'unknown' if live else 'failed' if row['health'] == 'failed' else 'unavailable',
             now, adapter['capability_id'], adapter['epoch']))
        self.registry._audit(db, row['principal'], 'phone_control_presence', row['id'],
                             {'epoch': row['epoch'], 'available': live, 'session_id': node['session_id']})

    @staticmethod
    def validate_selection(node, selection):
        adapter = node['installed_control_adapter']
        if (selection.get('capability_id') != adapter['capability_id'] or selection.get('epoch') != adapter['epoch']
                or selection.get('action') not in adapter['actions']):
            raise PermissionError('phone_control_installed_action_required')
        workload = selection.get('workload')
        if not isinstance(workload, dict) or set(workload) != {'input'}:
            raise ValueError('phone_control_json_blob_required')
        blob = workload['input']
        if (not isinstance(blob, dict) or set(blob) != {'bytes', 'sha256', 'base64'}
                or type(blob['bytes']) is not int or not 1 <= blob['bytes'] <= 65536
                or not isinstance(blob['base64'], str) or len(blob['base64']) > 87384):
            raise ValueError('phone_control_json_blob_required')
        try:
            raw = base64.b64decode(blob['base64'], validate=True)
            body = json.loads(raw.decode('utf8'), parse_constant=_invalid_json_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError('phone_control_json_blob_required') from None
        if (len(raw) != blob['bytes'] or hashlib.sha256(raw).hexdigest() != blob['sha256']
                or not isinstance(body, dict)):
            raise ValueError('phone_control_json_blob_required')
