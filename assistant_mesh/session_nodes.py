"""Owner-bound interactive nodes over the existing Store/Ingress (Python >=3.8).

Only membership and call receipts are added. Business tasks, operations, leases
and results stay in the original authority; neither discovery nor join needs a
business task. Client JSON never supplies an owner, node or execution grant.
"""
import hashlib
import json

from .ingress import _identifier, validate_peer
from .ingress_backend import LocalIngressBackend, RemoteIngressBackend
from .mcp_http_auth import NotConfigured, SOURCE
from .store import Conflict, Store

FORMAT = 'mesh-interactive-node/1'
TOOLS = {'mesh_connect': 'connect', 'mesh_discover': 'discover',
         'mesh_node_update': 'node_update', 'mesh_work_pull': 'work_pull',
         'mesh_work_report': 'work_report'}
ROUTES = {action: '/v1/ingress/mesh/' + action for action in TOOLS.values()}
WRITES = frozenset(('connect', 'node_update', 'work_pull', 'work_report'))
AGENT_STATES = frozenset(('ready', 'idle', 'waiting_model', 'unknown'))
MODEL_STATES = frozenset(('available', 'unavailable', 'unknown'))
REPORT_STATES = frozenset(('progress', 'effect_started', 'unknown', 'completed',
                          'failed', 'waiting_auth', 'waiting_backend'))


def _encoded(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(',', ':'),
                            ensure_ascii=False, allow_nan=False)
        if len(result.encode('utf8')) > 32768:
            raise ValueError()
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid_session_node_json') from None


def _digest(value):
    return hashlib.sha256(_encoded(value).encode('utf8')).hexdigest()


def _capabilities(value):
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError('invalid_session_capabilities')
    for cap in value:
        _identifier(cap, 'session_capability')
    if len(value) != len(set(value)):
        raise ValueError('duplicate_session_capability')
    return sorted(value)


def arguments(action, payload):
    fields, required = {'session_id'}, {'session_id'}
    if action in ('connect', 'node_update'):
        fields |= {'control_id', 'capabilities', 'online', 'agent_state', 'model_state'}
        required.add('control_id')
    elif action == 'discover':
        required = set()
    elif action == 'work_pull':
        fields.add('pull_id')
        required.add('pull_id')
    elif action == 'work_report':
        fields |= {'report_id', 'task_id', 'epoch', 'operation_id', 'request_id',
                   'conversation_id', 'status', 'checkpoint', 'result', 'effect_receipt'}
        required |= {'report_id', 'task_id', 'epoch', 'operation_id', 'request_id', 'status'}
    else:
        raise ValueError('invalid_session_node_action')
    if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - fields:
        raise ValueError('invalid_session_node_arguments')
    value = dict(payload)
    for key in ('session_id', 'control_id', 'pull_id', 'report_id', 'task_id',
                'operation_id', 'request_id', 'conversation_id'):
        if key in value and value[key] is not None:
            _identifier(value[key], 'session_' + key)
        elif key in required:
            raise ValueError('invalid_session_node_identifier')
    if action in ('connect', 'node_update'):
        value['capabilities'] = _capabilities(value.get('capabilities', []))
        value.setdefault('online', True)
        value.setdefault('agent_state', 'ready')
        value.setdefault('model_state', 'unknown')
        if (type(value['online']) is not bool or value['agent_state'] not in AGENT_STATES
                or value['model_state'] not in MODEL_STATES or action == 'connect' and not value['online']):
            raise ValueError('invalid_session_node_observation')
    if action == 'work_report':
        if type(value['epoch']) is not int or value['epoch'] < 1 or value['status'] not in REPORT_STATES:
            raise ValueError('invalid_session_work_report')
        value.setdefault('conversation_id', None)
        value.setdefault('checkpoint', {})
        value.setdefault('result', None)
        value.setdefault('effect_receipt', None)
        if (not isinstance(value['checkpoint'], dict) or
                value['result'] is not None and (not isinstance(value['result'], str)
                                                or len(value['result'].encode('utf8')) > 16384)):
            raise ValueError('invalid_session_work_report')
        receipt = value['effect_receipt']
        if receipt is not None:
            if (not isinstance(receipt, dict) or set(receipt) != {'operation_id', 'reference'}
                    or receipt['operation_id'] != value['operation_id']):
                raise ValueError('invalid_session_effect_receipt')
            _identifier(receipt['reference'], 'session_effect_reference', 2048)
    _encoded(value)
    return value


def dispatch(service, verifier, token, action, payload):
    """Current OAuth grant is checked before work and before disclosure."""
    try:
        principal = verifier.principal(token)
        if ('tasks:submit' if action in WRITES else 'tasks:read') not in principal['scopes']:
            return {'schema': 1, 'ok': False, 'error': 'insufficient_scope',
                    'outcome': 'not_attempted', 'retry_with_new_id': False}
        value = arguments(action, payload)
    except PermissionError:
        return {'schema': 1, 'ok': False, 'error': 'not_authorized_or_not_visible',
                'outcome': 'rejected', 'retry_with_new_id': False}
    except Exception:
        return {'schema': 1, 'ok': False, 'error': 'invalid_session_node_arguments',
                'outcome': 'not_attempted', 'retry_with_new_id': False}
    try:
        if service is None:
            raise NotConfigured('session_nodes_not_configured')
        result = service.request(principal, action, value)
        verifier.principal(token)
        return {'schema': 1, 'ok': True, 'data': result, 'retry_with_new_id': False}
    except PermissionError:
        category, outcome = 'not_authorized_or_not_visible', 'rejected'
    except NotConfigured:
        category, outcome = 'session_nodes_not_configured', 'not_attempted'
    except Conflict:
        category, outcome = 'original_binding_or_lease_unavailable', 'not_available'
    except Exception:
        category, outcome = 'session_authority_response_unknown', 'unknown'
    return {'schema': 1, 'ok': False, 'error': category, 'outcome': outcome,
            'retry_same_id': True, 'retry_with_new_id': False,
            'retry_semantics': 'reconcile_original_receipt' if action in ('work_pull', 'work_report')
                               else 'idempotent_control'}


class RemoteSessionNodes:
    """Fixed same-subject ingress routes, never worker/operator credentials."""
    def __init__(self, backend):
        if not isinstance(backend, RemoteIngressBackend) or backend.source != SOURCE:
            raise NotConfigured('session_nodes_scoped_ingress_required')
        self.backend, self.owner, self.source = backend, backend.owner, backend.source

    def request(self, principal, action, payload):
        binding = validate_peer({'role': 'ingress', 'ingress': principal}, self.owner)
        if binding['source'] != self.source:
            raise PermissionError('session_node_source_rejected')
        if ('tasks:submit' if action in WRITES else 'tasks:read') not in binding['scopes']:
            raise PermissionError('session_node_scope_required')
        value = arguments(action, payload)
        client = self.backend.clients.get(binding['subject'])
        if client is None:
            raise PermissionError('session_node_subject_rejected')
        response = client.request(ROUTES[action], value)
        if not isinstance(response, dict) or response.get('format') != FORMAT:
            raise Conflict('session_node_response_unrecognized')
        return response


class LocalSessionNodes:
    """Private installation over the original authority with real Store fencing.

    subjects maps approved Mesh subjects to capabilities and visible_nodes
    (private node ID -> display name). Advertisements are self-reported; only
    their intersection with installed capabilities qualifies for managed work.
    execute_own_requests is a separate private execution grant, default False.
    Otherwise authorize_work() binds an existing ingress task and operation.
    """
    def __init__(self, backend, subjects, *, mesh_id='amita', ttl_seconds=60,
                 work_lease_seconds=900):
        if (not isinstance(backend, LocalIngressBackend) or backend.source != SOURCE
                or not isinstance(backend.ingress.store, Store)):
            raise NotConfigured('session_nodes_local_ingress_required')
        if type(ttl_seconds) is not int or not 15 <= ttl_seconds <= 60:
            raise ValueError('invalid_interactive_ttl')
        if type(work_lease_seconds) is not int or not 60 <= work_lease_seconds <= 3600:
            raise ValueError('invalid_interactive_work_lease')
        if not isinstance(subjects, dict) or not subjects or len(subjects) > 32:
            raise ValueError('invalid_session_subject_policy')
        self.backend, self.owner, self.source = backend, backend.ingress.owner_id, backend.source
        self.ingress, self.store = backend.ingress, backend.ingress.store
        self.mesh_id, self.ttl = _identifier(mesh_id, 'mesh_id'), ttl_seconds
        self.work_lease_seconds = work_lease_seconds
        self.subjects = {}
        for subject, policy in subjects.items():
            _identifier(subject, 'session_subject')
            if (not isinstance(policy, dict) or not {'capabilities', 'visible_nodes'} <= set(policy)
                    or set(policy) - {'capabilities', 'visible_nodes', 'execute_own_requests'}):
                raise ValueError('invalid_session_subject_policy')
            caps = _capabilities(policy['capabilities'])
            if 'leader' in caps:
                raise ValueError('interactive_coordinator_capability_rejected')
            visible = policy['visible_nodes']
            if not isinstance(visible, dict) or len(visible) > 128:
                raise ValueError('invalid_session_visible_nodes')
            for node, label in visible.items():
                _identifier(node, 'session_visible_node')
                _identifier(label, 'session_node_label')
            execute = policy.get('execute_own_requests', False)
            if type(execute) is not bool:
                raise ValueError('invalid_session_execution_policy')
            self.subjects[subject] = {'capabilities': caps, 'visible_nodes': dict(visible),
                                      'execute_own_requests': execute}
        with self.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS interactive_nodes(
                node_id TEXT PRIMARY KEY,subject TEXT NOT NULL,session_id TEXT NOT NULL,
                capabilities TEXT NOT NULL,state TEXT NOT NULL,agent_state TEXT NOT NULL,
                model_state TEXT NOT NULL,seen REAL NOT NULL,expires REAL NOT NULL,
                generation INTEGER NOT NULL,UNIQUE(subject,session_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS interactive_controls(
                node_id TEXT NOT NULL,control_id TEXT NOT NULL,kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,PRIMARY KEY(node_id,control_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS interactive_calls(
                node_id TEXT NOT NULL,call_id TEXT NOT NULL,kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,receipt TEXT NOT NULL,created REAL NOT NULL,
                PRIMARY KEY(node_id,call_id))''')

    def _principal(self, principal, action):
        binding = validate_peer({'role': 'ingress', 'ingress': principal}, self.owner)
        if (binding['source'] != self.source or binding['subject'] not in self.subjects
                or ('tasks:submit' if action in WRITES else 'tasks:read') not in binding['scopes']):
            raise PermissionError('session_node_not_authorized')
        return binding

    def _node_id(self, subject, session_id):
        return 'chat-' + _digest([FORMAT, self.owner, self.source, subject, session_id])[:48]

    def _public_node_id(self, node_id):
        return 'node-' + _digest([self.owner, self.mesh_id, node_id])[:32]

    def _session(self, db, binding, session_id, live=False):
        node = self._node_id(binding['subject'], session_id)
        row = db.execute('SELECT * FROM interactive_nodes WHERE node_id=?', (node,)).fetchone()
        if row is None or row['subject'] != binding['subject'] or row['session_id'] != session_id:
            raise PermissionError('session_node_not_visible')
        if live and (row['state'] != 'online' or row['expires'] <= self.store.clock()):
            raise Conflict('interactive_session_expired')
        return row

    def _session_view(self, row):
        online = row['state'] == 'online' and row['expires'] > self.store.clock()
        presence = 'online' if online else 'inactive' if row['state'] == 'online' else 'offline'
        advertised = json.loads(row['capabilities'])
        granted = sorted(set(advertised) & set(self.subjects[row['subject']]['capabilities']))
        return {'node_id': row['node_id'], 'session_id': row['session_id'],
                'lifecycle': 'interactive', 'generation': row['generation'],
                'network': {'state': presence, 'expires_at': row['expires'], 'presence_ttl_seconds': self.ttl},
                'agent': {'state': row['agent_state'], 'observation': 'session_reported', 'observation_stale': not online},
                'model': {'state': row['model_state'], 'observation': 'session_reported', 'observation_stale': not online},
                'roles': ['entrance'] + (['execution'] if granted else []),
                'capabilities': advertised, 'managed_capabilities': granted,
                'capability_verification': 'session_reported', 'background_wake_supported': False,
                'provider_identity_verified': False, 'execution_verified': False,
                'native_tools_restricted': False}

    def _base(self, **values):
        return dict({'format': FORMAT, 'mesh_id': self.mesh_id,
                     'trusted_binding': 'owner_installed_ingress', 'execution_verified': False,
                     'native_tools_restricted': False, 'retry_with_new_id': False}, **values)

    def _control(self, binding, action, value):
        node = self._node_id(binding['subject'], value['session_id'])
        fingerprint = _digest([action, value])
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM interactive_controls WHERE node_id=? AND control_id=?',
                             (node, value['control_id'])).fetchone()
            if old and (old['kind'] != action or old['fingerprint'] != fingerprint):
                raise Conflict('session_control_identity_changed')
            row = db.execute('SELECT * FROM interactive_nodes WHERE node_id=?', (node,)).fetchone()
            now = self.store.clock()
            if old is None:
                if action == 'node_update' and row is None:
                    raise PermissionError('session_node_not_visible')
                db.execute('INSERT OR REPLACE INTO interactive_nodes VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (node, binding['subject'], value['session_id'], _encoded(value['capabilities']),
                            'online' if value['online'] else 'offline', value['agent_state'], value['model_state'],
                            now, now + self.ttl if value['online'] else now,
                            row['generation'] + 1 if row else 1))
                db.execute('INSERT INTO interactive_controls VALUES(?,?,?,?)',
                           (node, value['control_id'], action, fingerprint))
            elif action == 'connect':
                # Metadata/control retry can renew the same identity after a
                # lost ACK/TTL. Retain later capabilities instead of old JSON.
                db.execute("UPDATE interactive_nodes SET state='online',seen=?,expires=? WHERE node_id=?",
                           (now, now + self.ttl, node))
            row = self._session(db, binding, value['session_id'])
            view = self._session_view(row)
        # Existing repeatable heartbeat; a failed ACK never freezes membership.
        if view['network']['state'] == 'online':
            self.store.heartbeat(node, view['managed_capabilities'], details={
                'lifecycle': 'interactive', 'agent_state': row['agent_state'],
                'model_state': row['model_state'], 'execution_verified': False})
        else:
            with self.store.transaction() as db:
                db.execute('UPDATE nodes SET seen=? WHERE id=?', (self.store.clock() - 61, node))
        return self._base(session=view, control_id=value['control_id'],
                          control_retry_safe=True, business_task_created=False)

    def authorize_work(self, subject, request_id, operation_id, *, conversation_id=None,
                       task_id=None, expires_at=None, authorized=False):
        """Private owner installation, never an MCP/public action."""
        if authorized is not True or subject not in self.subjects:
            raise PermissionError('interactive_work_owner_authorization_required')
        for value in (request_id, operation_id):
            _identifier(value, 'interactive_original_identity')
        if conversation_id is not None:
            _identifier(conversation_id, 'interactive_original_conversation')
        if task_id is not None:
            _identifier(task_id, 'interactive_original_task')
        if expires_at is not None and (type(expires_at) not in (int, float)
                                        or not self.store.clock() < expires_at < float('inf')):
            raise ValueError('invalid_interactive_work_expiry')
        with self.store.transaction() as db:
            if task_id is None:
                entry = db.execute('SELECT * FROM ingress_requests WHERE source=? AND subject=? AND request_id=?',
                                   (self.source, subject, request_id)).fetchone()
                task_id = entry['task_id'] if entry else None
            else:
                entry = db.execute('SELECT * FROM ingress_requests WHERE task_id=?', (task_id,)).fetchone()
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone() if task_id else None
            if row is None:
                raise PermissionError('interactive_original_request_not_visible')
            context, checkpoint = json.loads(row['context']), json.loads(row['checkpoint'])
            original_request = (entry['request_id'] if entry else context.get('request_id')
                                or context.get('origin', {}).get('message_id'))
            if original_request != request_id:
                raise Conflict('interactive_original_request_changed')
            recorded_operation = checkpoint.get('operation_id') or context.get('operation_id')
            if recorded_operation is not None and recorded_operation != operation_id:
                raise Conflict('interactive_original_operation_changed')
            grant = {'format': FORMAT, 'task_id': row['id'], 'source': self.source,
                     'subject': subject, 'request_id': request_id, 'conversation_id': conversation_id,
                     'operation_id': operation_id, 'expires_at': expires_at}
            old = context.get('interactive_work')
            if old is not None and old != grant:
                raise Conflict('interactive_original_work_changed')
            context['interactive_work'] = grant
            db.execute('UPDATE tasks SET context=? WHERE id=?', (_encoded(context), row['id']))
            return dict(grant)

    def _entry(self, db, row):
        entry = db.execute('SELECT * FROM ingress_requests WHERE task_id=?', (row['id'],)).fetchone()
        if entry is not None:
            return entry
        context = json.loads(row['context'])
        request_id = context.get('request_id') or context.get('origin', {}).get('message_id')
        return {'request_id': request_id, 'source': None, 'subject': None} if request_id else None

    def _grant(self, row, entry, subject):
        context, checkpoint = json.loads(row['context']), json.loads(row['checkpoint'])
        grant = context.get('interactive_work')
        if (grant is None and self.subjects[subject]['execute_own_requests']
                and entry['source'] == self.source and entry['subject'] == subject):
            if row['attempts'] or checkpoint or context.get('operation_id'):
                return None
            grant = {'format': FORMAT, 'task_id': row['id'], 'source': self.source, 'subject': subject,
                     'request_id': entry['request_id'], 'conversation_id': None,
                     'operation_id': 'interactive-' + _digest([self.owner, row['id']])[:48], 'expires_at': None}
        if (not isinstance(grant, dict) or grant.get('format') != FORMAT
                or grant.get('task_id') != row['id'] or grant.get('source') != self.source
                or grant.get('subject') != subject or grant.get('request_id') != entry['request_id']
                or grant.get('expires_at') is not None and grant['expires_at'] <= self.store.clock()):
            return None
        _identifier(grant.get('operation_id'), 'interactive_original_operation')
        recorded = checkpoint.get('operation_id') or context.get('operation_id')
        if recorded is not None and recorded != grant['operation_id']:
            raise Conflict('interactive_original_operation_changed')
        return grant

    def _receipt(self, db, node, call_id, kind, value):
        old = db.execute('SELECT * FROM interactive_calls WHERE node_id=? AND call_id=?', (node, call_id)).fetchone()
        fingerprint = _digest([kind, value])
        if old and (old['kind'] != kind or old['fingerprint'] != fingerprint):
            raise Conflict('interactive_call_identity_changed')
        return json.loads(old['receipt']) if old else None, fingerprint

    def _save_receipt(self, db, node, call_id, kind, fingerprint, receipt):
        db.execute('INSERT INTO interactive_calls VALUES(?,?,?,?,?,?)',
                   (node, call_id, kind, fingerprint, _encoded(receipt), self.store.clock()))

    def _work_view(self, db, binding, session, receipt):
        if receipt['task_id'] is None:
            return self._base(work=None, pull_id=receipt['pull_id'], poll_after_seconds=15,
                              reason='no_eligible_authorized_work')
        row = db.execute('SELECT * FROM tasks WHERE id=?', (receipt['task_id'],)).fetchone()
        entry = self._entry(db, row) if row is not None else None
        if row is None or entry is None:
            raise PermissionError('interactive_work_not_visible')
        grant = self._grant(row, entry, binding['subject'])
        if grant is None or grant['operation_id'] != receipt['operation_id']:
            raise PermissionError('interactive_work_not_authorized')
        live = self.store._task_is_live(db, row, session['node_id'], receipt['epoch'])
        checkpoint = json.loads(row['checkpoint'])
        capable = set(json.loads(row['required'])) <= set(self._session_view(session)['managed_capabilities'])
        ready = session['agent_state'] != 'waiting_model' and session['model_state'] != 'unavailable'
        return self._base(work={'task_id': row['id'], 'epoch': receipt['epoch'],
            'operation_id': receipt['operation_id'], 'request_id': grant['request_id'],
            'conversation_id': grant['conversation_id'], 'input': row['input'],
            'required': json.loads(row['required']), 'status': row['status'], 'lease_live': bool(live),
            'lease_remaining_seconds': max(0, (row['deadline'] or 0) - self.store.clock()) if live else 0,
            'may_execute': bool(live and session['state'] == 'online' and capable and ready
                                and not checkpoint.get('side_effect_started')),
            'effect_state': 'requires_original_receipt' if checkpoint.get('side_effect_started') else 'not_started',
            'checkpoint': checkpoint.get('interactive_progress', {}), 'execution_verified': False},
            pull_id=receipt['pull_id'])

    def _pull(self, binding, value):
        if not callable(getattr(self.store, '_claim_in_db', None)):
            raise NotConfigured('interactive_atomic_claim_not_installed')
        self.store.elect()
        with self.store.transaction() as db:
            session = self._session(db, binding, value['session_id'])
            old, fingerprint = self._receipt(db, session['node_id'], value['pull_id'], 'work_pull', value)
            if old is not None:
                return self._work_view(db, binding, session, old)
            session = self._session(db, binding, value['session_id'], live=True)
            eligible, grants = [], {}
            if session['agent_state'] != 'waiting_model' and session['model_state'] != 'unavailable':
                rows = db.execute('''SELECT * FROM tasks WHERE id IN
                    (SELECT task_id FROM ingress_requests WHERE source=? AND subject=?)
                    OR context LIKE ?''', (self.source, binding['subject'], '%"interactive_work"%')).fetchall()
                for row in rows:
                    entry = self._entry(db, row)
                    grant = self._grant(row, entry, binding['subject']) if entry else None
                    if grant is not None:
                        eligible.append(row['id'])
                        grants[row['id']] = grant
            task = db.execute("SELECT * FROM tasks WHERE node=? AND status='running' AND deadline>? ORDER BY created LIMIT 1",
                              (session['node_id'], self.store.clock())).fetchone()
            if task is not None and task['id'] not in eligible:
                task, eligible = None, []
            fresh_claim = task is None
            if fresh_claim:
                task = self.store._claim_in_db(db, session['node_id'], task_ids=eligible)
            receipt = {'pull_id': value['pull_id'], 'task_id': task['id'] if task else None}
            if task:
                grant = grants[task['id']]
                context = json.loads(task['context']) if isinstance(task['context'], str) else task['context']
                checkpoint = json.loads(task['checkpoint']) if isinstance(task['checkpoint'], str) else task['checkpoint']
                context['interactive_work'], checkpoint['operation_id'] = grant, grant['operation_id']
                db.execute('UPDATE tasks SET context=?,checkpoint=? WHERE id=?',
                           (_encoded(context), _encoded(checkpoint), task['id']))
                if fresh_claim:
                    db.execute('UPDATE tasks SET deadline=? WHERE id=? AND node=? AND epoch=?',
                               (self.store.clock() + self.work_lease_seconds, task['id'], session['node_id'], task['epoch']))
                receipt.update(epoch=task['epoch'], operation_id=grant['operation_id'])
            self._save_receipt(db, session['node_id'], value['pull_id'], 'work_pull', fingerprint, receipt)
            return self._work_view(db, binding, session, receipt)

    def _report(self, binding, value):
        if not callable(getattr(self.store, '_update_task_in_db', None)):
            raise NotConfigured('interactive_atomic_report_not_installed')
        with self.store.transaction() as db:
            session = self._session(db, binding, value['session_id'])
            old, fingerprint = self._receipt(db, session['node_id'], value['report_id'], 'work_report', value)
            if old is not None:
                return self._base(receipt=old, replayed=False, original_receipt=True)
            row = db.execute('SELECT * FROM tasks WHERE id=?', (value['task_id'],)).fetchone()
            entry = self._entry(db, row) if row is not None else None
            grant = self._grant(row, entry, binding['subject']) if row is not None and entry is not None else None
            if grant is None or any(value[key] != grant[key] for key in ('operation_id', 'request_id', 'conversation_id')):
                raise PermissionError('interactive_original_work_not_authorized')
            claims = (json.loads(r['receipt']) for r in db.execute(
                "SELECT receipt FROM interactive_calls WHERE node_id=? AND kind='work_pull'", (session['node_id'],)))
            if not any(all(claim.get(key) == value[key] for key in ('task_id', 'epoch', 'operation_id')) for claim in claims):
                raise PermissionError('interactive_original_claim_required')
            checkpoint = json.loads(row['checkpoint'])
            if value['status'] == 'effect_started':
                capable = set(json.loads(row['required'])) <= set(self._session_view(session)['managed_capabilities'])
                if (session['state'] != 'online' or not capable or session['agent_state'] == 'waiting_model'
                        or session['model_state'] == 'unavailable' or checkpoint.get('side_effect_started')):
                    raise Conflict('interactive_new_effect_not_authorized')
            effect = bool(checkpoint.get('side_effect_started'))
            if effect and value['status'] in ('completed', 'failed') and value['effect_receipt'] is None:
                raise Conflict('interactive_original_effect_receipt_required')
            update = {'operation_id': grant['operation_id'], 'interactive_progress': value['checkpoint']}
            if value['status'] in ('effect_started', 'unknown'):
                update['side_effect_started'] = True
            elif value['effect_receipt'] is not None:
                update['side_effect_started'] = False
                update['interactive_effect_receipt'] = value['effect_receipt']
            status = {'progress': None, 'effect_started': None, 'unknown': 'needs_review'}.get(value['status'], value['status'])
            self.store._update_task_in_db(db, value['task_id'], session['node_id'], value['epoch'],
                                         checkpoint=update, result=value['result'], status=status)
            # Presence is an observation, not proof of an execution's death.
            # Extend only an already fenced interactive running epoch, without
            # fabricating a node heartbeat or changing normal Store timeouts.
            db.execute("UPDATE tasks SET deadline=? WHERE id=? AND node=? AND epoch=? AND status='running'",
                       (self.store.clock() + self.work_lease_seconds, value['task_id'], session['node_id'], value['epoch']))
            receipt = {key: value[key] for key in ('report_id', 'task_id', 'epoch', 'operation_id',
                                                  'request_id', 'conversation_id', 'status')}
            receipt.update(accepted=True, execution_verified=False)
            self._save_receipt(db, session['node_id'], value['report_id'], 'work_report', fingerprint, receipt)
            return self._base(receipt=receipt, replayed=False, original_receipt=False)

    def _discover(self, binding, value):
        with self.store.transaction() as db:
            now, nodes = self.store.clock(), []
            leader = db.execute('SELECT * FROM leader').fetchone()
            visible = self.subjects[binding['subject']]['visible_nodes']
            has_links = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mesh_links'").fetchone()
            for node_id, label in visible.items():
                row = db.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone()
                link = db.execute("SELECT * FROM mesh_links WHERE peer=? AND kind='a2a'", (node_id,)).fetchone() if has_links else None
                online = row['seen'] > now - 60 if row else bool(link and link['state'] == 'connected' and link['deadline'] > now)
                caps, roles = json.loads(row['capabilities']) if row else [], []
                if leader['node'] == node_id and leader['deadline'] > now:
                    roles.append('coordinator')
                if 'agent' in caps:
                    roles.append('execution')
                task_states = {r['status']: r['count'] for r in db.execute(
                    'SELECT status,COUNT(*) AS count FROM tasks WHERE node=? GROUP BY status', (node_id,))}
                nodes.append({'node_id': self._public_node_id(node_id), 'display_name': label,
                    'roles': roles, 'capabilities': caps,
                    'network': {'state': 'online' if online else 'offline', 'observation': 'heartbeat' if row else 'authenticated_a2a' if link else 'not_observed'},
                    'agent': {'state': 'unknown', 'observation': 'not_attested'},
                    'model': {'state': 'unknown', 'observation': 'not_attested'},
                    'tasks': {'states': task_states}, 'execution_verified': False})
            for row in db.execute('SELECT * FROM interactive_nodes WHERE subject=? ORDER BY node_id', (binding['subject'],)):
                nodes.append(self._session_view(row))
            session = self._session_view(self._session(db, binding, value['session_id'])) if value.get('session_id') is not None else None
            return self._base(network={'state': 'reachable', 'control_available': True,
                                      'coordinator_lease_live': bool(leader['node'] and leader['deadline'] > now)},
                              nodes=nodes, session=session, business_task_created=False)

    def request(self, principal, action, payload):
        value = arguments(action, payload)
        binding = self._principal(principal, action)
        if action in ('connect', 'node_update'):
            return self._control(binding, action, value)
        if action == 'discover':
            return self._discover(binding, value)
        if action == 'work_pull':
            return self._pull(binding, value)
        return self._report(binding, value)
