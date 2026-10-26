"""Private, exact-task independence grants; business and native scope stay distinct.

No work is created, settled, resumed or approved on import. A task's JSON is
never a grant. Installation/parent authority calls declare() on the SAME Store;
admission and native-session storage consume its durable private declaration.
"""
import hashlib
import json


FORMAT = 'mesh-operation-isolation/1'
TABLE = 'operation_isolation_grants'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def _name(value):
    if (not isinstance(value, str) or not value.strip() or len(value.encode('utf8')) > 256
            or any(ord(char) < 32 for char in value)):
        raise ValueError('invalid_operation_isolation_identity')
    return value


def _has(db, table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _raw(db, task_id):
    if not _has(db, TABLE):
        return None
    row = db.execute('SELECT body FROM ' + TABLE + ' WHERE task_id=?', (task_id,)).fetchone()
    return json.loads(row['body']) if row else None


def _native_key(db, row):
    grant = _raw(db, row['id'])
    return grant['native_scope'] if grant else row['scope']


def _goal_reserves(store, row):
    checkpoint = json.loads(row['checkpoint'])
    return bool(store._yielded_goal_reserves_scope(row['status'], checkpoint)
        or (row['status'] == 'paused' and checkpoint.get('thread_id')
            and (checkpoint.get('goal') or {}).get('status') == 'active'))


def _occupied(store, row):
    checkpoint = json.loads(row['checkpoint'])
    return bool(checkpoint.get('side_effect_started') or _goal_reserves(store, row)
        or row['status'] == 'running' and row['deadline'] is not None and row['deadline'] > store.clock())


def _footprint(db, row):
    grant, checkpoint, context = _raw(db, row['id']), json.loads(row['checkpoint']), json.loads(row['context'])
    operation = (checkpoint.get('operation_id') or context.get('operation_id')
                 or (grant or {}).get('operation_id') or row['id'])
    native = [dict(r) for r in db.execute('SELECT * FROM native_sessions WHERE scope=? ORDER BY harness', (_native_key(db, row),))]
    threads = {(checkpoint.get('harness', 'codex'), checkpoint.get('thread_id'))}
    for item in native:
        state = json.loads(item['state'])
        threads.add((item['harness'], state.get('thread_id')))
    resources = set((grant or {}).get('resources', []))
    resources.add('native:' + _native_key(db, row))
    resources.update('thread:' + harness + ':' + thread for harness, thread in threads if isinstance(thread, str) and thread)
    if operation:
        resources.add('operation:' + operation)
    managed = []
    if _has(db, 'managed_allocations'):
        for item in db.execute('SELECT * FROM managed_allocations WHERE task_id=? ORDER BY operation_id', (row['id'],)):
            allocation = dict(item)
            for table in ('managed_dispatch', 'managed_allocation_demands'):
                if _has(db, table):
                    allocation[table] = [dict(r) for r in db.execute(
                        'SELECT * FROM ' + table + ' WHERE operation_id=? ORDER BY rowid', (item['operation_id'],))]
            managed.append(allocation)
    return {'task_id': row['id'], 'operation_id': operation, 'resources': sorted(resources),
            'sha256': _hash([dict(row), native, managed, grant])}


def inspect_blockers(store, task_id):
    """Read private proof references; never infer or record settlement."""
    with store.transaction() as db:
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if task is None:
            raise ValueError('operation_isolation_task_missing')
        return [_footprint(db, row) for row in db.execute('SELECT * FROM tasks WHERE scope=? AND id<>? ORDER BY id',
            (task['scope'], task_id)) if _occupied(store, row)]


def declare(store, *, declaration_id, task_id, operation_id, resources, independent_of,
            reason, authorized=False, parent=None):
    """Owner CODE only, or a parent tuple bound by its authenticated transport.

    parent=(task_id,node,epoch) must name this child's actual live parent. No
    public endpoint is installed here and task/context approved=true is ignored.
    A new native resource is granted only before the first attempt, never as a
    way to rename an old execution. Identical declarations only read the receipt.
    """
    for value in (declaration_id, task_id, operation_id, reason):
        _name(value)
    if (not isinstance(resources, list) or len(resources) > 64
            or len(set(resources)) != len(resources) or not isinstance(independent_of, list)):
        raise ValueError('invalid_operation_isolation_declaration')
    resources = sorted(_name(value) for value in resources)
    if any(value.startswith(('native:', 'thread:', 'operation:')) for value in resources):
        raise ValueError('operation_isolation_reserved_resource')
    with store.transaction() as db:
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if task is None:
            raise ValueError('operation_isolation_task_missing')
        if authorized is not True:
            if not isinstance(parent, tuple) or len(parent) != 3 or task['parent_id'] != parent[0]:
                raise PermissionError('operation_isolation_private_authority_required')
            parent_row = db.execute('SELECT * FROM tasks WHERE id=?', (parent[0],)).fetchone()
            if not store._task_is_live(db, parent_row, parent[1], parent[2]):
                raise PermissionError('operation_isolation_live_parent_required')
        context, checkpoint = json.loads(task['context']), json.loads(task['checkpoint'])
        original_operation = checkpoint.get('operation_id') or context.get('operation_id') or task['id']
        if original_operation != operation_id:
            raise ValueError('operation_isolation_original_operation_changed')
        body = {'format': FORMAT, 'declaration_id': declaration_id, 'task_id': task_id,
            'operation_id': operation_id, 'resources': resources, 'independent_of': sorted(independent_of, key=lambda item: item['task_id']),
            'reason': reason, 'scope': task['scope'], 'created': task['created'], 'parent_id': task['parent_id'],
            'input_sha256': hashlib.sha256(task['input'].encode('utf8')).hexdigest(),
            'native_scope': 'execution:' + _hash([FORMAT, declaration_id, task_id])[:48]}
        old = _raw(db, task_id)
        if old is not None:
            if old != body:
                raise ValueError('operation_isolation_declaration_changed')
            return old
        if task['status'] != 'pending' or task['attempts'] or task['epoch'] or checkpoint:
            raise ValueError('operation_isolation_unstarted_task_required')
        if db.execute('SELECT 1 FROM native_sessions WHERE scope=?', (body['native_scope'],)).fetchone():
            raise ValueError('operation_isolation_new_native_resource_required')
        for proof in body['independent_of']:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (proof['task_id'],)).fetchone()
            if row is None or proof != _footprint(db, row):
                raise ValueError('operation_isolation_blocker_proof_changed')
        db.execute('CREATE TABLE IF NOT EXISTS ' + TABLE + '('
            'task_id TEXT PRIMARY KEY,declaration_id TEXT NOT NULL UNIQUE,body TEXT NOT NULL)')
        db.execute('INSERT INTO ' + TABLE + ' VALUES(?,?,?)', (task_id, declaration_id, _json(body)))
        return body


def plan(db, row, conflict):
    grant = _raw(db, row['id'])
    if grant is None:
        return None
    checkpoint, context = json.loads(row['checkpoint']), json.loads(row['context'])
    operation = checkpoint.get('operation_id') or context.get('operation_id') or grant['operation_id']
    if (grant['format'] != FORMAT or grant['scope'] != row['scope'] or grant['created'] != row['created']
            or grant['parent_id'] != row['parent_id'] or operation != grant['operation_id']
            or not row['attempts'] and grant['input_sha256'] != hashlib.sha256(row['input'].encode('utf8')).hexdigest()):
        raise conflict('operation_isolation_original_binding_changed')
    for proof in grant['independent_of']:
        other = db.execute('SELECT * FROM tasks WHERE id=?', (proof['task_id'],)).fetchone()
        if other is None or proof != _footprint(db, other):
            raise conflict('operation_isolation_blocker_proof_changed')
    return grant


def native_scope(db, row, conflict):
    grant = plan(db, row, conflict)
    return grant['native_scope'] if grant else row['scope']


def _unsafe_thread(store, db, row, state, harness):
    thread = state.get('thread_id')
    if not thread:
        return False
    resource = 'thread:' + harness + ':' + thread
    return any(resource in _footprint(db, other)['resources'] for other in
        db.execute('SELECT * FROM tasks WHERE id<>?', (row['id'],)) if _occupied(store, other))


def validate_native(store, db, row, state, harness, conflict):
    if plan(db, row, conflict) is not None and _unsafe_thread(store, db, row, state, harness):
        raise conflict('operation_isolation_unknown_native_resource')


def admit(store, db, row, conflict):
    """None keeps the old guard; False fences only this candidate; dict isolates."""
    try:
        grant = plan(db, row, conflict)
        if grant is None:
            return None
        own = set(grant['resources']) | {'native:' + grant['native_scope'], 'operation:' + grant['operation_id']}
        proofs = {proof['task_id']: proof for proof in grant['independent_of']}
        for other in db.execute('SELECT * FROM tasks WHERE id<>?', (row['id'],)):
            if not _occupied(store, other):
                continue
            footprint = _footprint(db, other)
            if own.intersection(footprint['resources']):
                return False
            if other['scope'] == row['scope']:
                if _goal_reserves(store, other):
                    return False
                if _raw(db, other['id']) is None and proofs.get(other['id']) != footprint:
                    return False
        checkpoint = json.loads(row['checkpoint'])
        if checkpoint.get('side_effect_started') or _unsafe_thread(store, db, row, checkpoint, checkpoint.get('harness', 'codex')):
            return False
        for native in db.execute('SELECT harness,state FROM native_sessions WHERE scope=?', (grant['native_scope'],)):
            if _unsafe_thread(store, db, row, json.loads(native['state']), native['harness']):
                return False
        return grant
    except (conflict, ValueError, TypeError, KeyError):
        return False


def related(db, row, conflict):
    """Conservative native/exclusive-resource family for existing continuation."""
    grant = plan(db, row, conflict)
    if grant is None:
        return list(db.execute('SELECT * FROM tasks WHERE scope=?', (row['scope'],)))
    resources = set(grant['resources']) | {'native:' + grant['native_scope']}
    return [other for other in db.execute('SELECT * FROM tasks')
            if other['id'] == row['id'] or resources.intersection(_footprint(db, other)['resources'])]


def make_store_class(base, methods_class):
    """Compose BEFORE entrance/native-Goal public wrappers; no new Store/DB.

    Frozen helpers are bound by code identity, with only Conflict remapped.
    Specialized outer admission checks must be composed afterwards, not replaced.
    """
    if getattr(base, '_operation_isolation_version', None) == 1:
        return base
    if getattr(base, '_native_goal_scope_version', None) is not None or getattr(base, 'entrance_policy', None) is not None:
        raise TypeError('operation_isolation_base_composition_order_required')
    if getattr(methods_class, '_operation_isolation_version', None) != 1:
        raise TypeError('operation_isolation_trusted_methods_required')
    from .task_transaction_adapter import _bind, _conflict_class
    from inspect import getattr_static
    conflict = _conflict_class(base)
    names = ('claim', '_claim_in_db', 'update_task', '_update_task_in_db', 'session_action', 'continue_task')
    namespace = {name: _bind(getattr_static(methods_class, name), conflict) for name in names}
    namespace.update(__module__=__name__, _operation_isolation_version=1,
        _yielded_active_goal_checkpoint=getattr_static(methods_class, '_yielded_active_goal_checkpoint'),
        _yielded_goal_reserves_scope=getattr_static(methods_class, '_yielded_goal_reserves_scope'),
        _conversation_final_hook_version=1)
    return type('OperationIsolation' + base.__name__, (base,), namespace)
