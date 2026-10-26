"""Private original-authority declarations for independent, unstarted work."""
from urllib.parse import urlsplit

from . import operation_isolation as isolation
from .store import Conflict


ROUTE = '/v1/operation-isolation/action'


class IsolationAuthority:
    def __init__(self, api):
        self.store = api.store

    def request(self, peer, payload):
        if not isinstance(payload, dict) or not isinstance(peer, dict):
            raise ValueError('operation_isolation_arguments_invalid')
        action = payload.get('action')
        if action not in ('inspect', 'declare'):
            raise ValueError('operation_isolation_action_invalid')
        fields = {'action', 'task_id', 'parent_task_id', 'epoch'}
        required = {'declaration_id', 'operation_id', 'resources', 'independent_of', 'reason'}
        if action == 'declare':
            fields |= required
        if set(payload) - fields or 'task_id' not in payload:
            raise ValueError('operation_isolation_arguments_invalid')
        task_id = isolation._name(payload['task_id'])
        parent = None
        operator = peer.get('role') == 'operator'
        if not operator:
            if peer.get('role') != 'worker' or not peer.get('node'):
                raise PermissionError('operation_isolation_original_parent_required')
            with self.store.transaction() as db:
                child = db.execute('SELECT parent_id FROM tasks WHERE id=?', (task_id,)).fetchone()
                parent_id = payload.get('parent_task_id')
                row = db.execute('SELECT * FROM tasks WHERE id=?', (parent_id,)).fetchone()
                if (child is None or child['parent_id'] != parent_id
                        or not self.store._task_is_live(db, row, peer['node'], payload.get('epoch'))):
                    raise PermissionError('operation_isolation_original_parent_required')
                parent = (parent_id, peer['node'], payload['epoch'])
        elif 'parent_task_id' in payload or 'epoch' in payload:
            raise ValueError('operation_isolation_operator_parent_not_required')
        if action == 'inspect':
            return {'format': isolation.FORMAT, 'task_id': task_id,
                    'blockers': isolation.inspect_blockers(self.store, task_id),
                    'task_created': False, 'unknown_settled': False}
        if not required <= set(payload):
            raise ValueError('operation_isolation_arguments_invalid')
        resources, proofs = payload['resources'], payload['independent_of']
        if (not isinstance(resources, list) or any(not isinstance(value, str) for value in resources)
                or not isinstance(proofs, list) or len(proofs) > 256):
            raise ValueError('operation_isolation_arguments_invalid')
        for proof in proofs:
            if (not isinstance(proof, dict) or set(proof) != {'task_id', 'operation_id', 'resources', 'sha256'}
                    or not isinstance(proof['task_id'], str) or not isinstance(proof['sha256'], str)
                    or len(proof['sha256']) != 64 or not isinstance(proof['resources'], list)):
                raise ValueError('operation_isolation_arguments_invalid')
        declaration = isolation.declare(self.store, task_id=task_id, authorized=operator,
            parent=parent, **{key: payload[key] for key in required})
        return {'format': isolation.FORMAT, 'declaration': declaration,
                'task_created': False, 'unknown_settled': False}


def attach(api):
    installed = getattr(api, '_operation_isolation_authority', None)
    if installed is not None:
        if installed.store is not api.store:
            raise Conflict('operation_isolation_authority_changed')
        return installed
    authority, previous = IsolationAuthority(api), api.dispatch

    def dispatch(method, path, payload, peer):
        if urlsplit(path).path == ROUTE:
            if method != 'POST' or path != ROUTE:
                raise PermissionError('operation_isolation_private_route_required')
            return authority.request(peer, payload)
        return previous(method, path, payload, peer)

    api.dispatch = dispatch
    api._operation_isolation_authority = authority
    return authority
