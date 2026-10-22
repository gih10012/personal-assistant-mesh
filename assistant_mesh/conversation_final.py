"""Default-off authenticated completion -> original entrance final, in ONE txn.

No Worker replacement, human approval, read-time export, model, network or loop.
Only new Chat requests explicitly admitted by the owner's fixed reply policy.
"""
import json
import threading
from urllib.parse import urlsplit

from .conversation import MAX_TEXT, digest, encoded, utc
from .conversation_emit import _conversation_message_id
from .ingress import _identifier
from .mcp_http_auth import SOURCE
from .store import Conflict


FORMAT = 'mesh-conversation-final/1'
CAPABILITY = 'owner.conversation.final'
CALL_ID = 'automatic-task-final-v1'
RECEIPT_ROUTE = '/v1/conversation/final/receipt'
TERMINAL = frozenset(('completed', 'failed', 'needs_review'))
# A control character cannot be a legacy _identifier node. Keep the original
# node TEXT column/digest byte-for-byte for single-node policies.
_NODES_PREFIX = '\x1ffinal-nodes/1:'


def _node_set(nodes):
    if not isinstance(nodes, (list, tuple)) or not 1 <= len(nodes) <= 20:
        raise ValueError('conversation_final_explicit_nodes_required')
    nodes = tuple(_identifier(node, 'final_node') for node in nodes)
    if len(set(nodes)) != len(nodes) or '*' in nodes:
        raise ValueError('conversation_final_explicit_nodes_required')
    return tuple(sorted(nodes))


def _policy_nodes(value):
    if not value.startswith(_NODES_PREFIX):
        return (value,)
    try:
        nodes = _node_set(json.loads(value[len(_NODES_PREFIX):]))
        if value == _NODES_PREFIX + encoded(nodes).decode('utf8'):
            return nodes
    except (TypeError, ValueError):
        pass
    raise Conflict('conversation_final_original_reply_policy_changed')


def _policies(policies):
    if not isinstance(policies, (list, tuple)) or not 1 <= len(policies) <= 20:
        raise ValueError('conversation_final_explicit_policy_required')
    result = {}
    for policy in policies:
        if (not isinstance(policy, dict) or set(policy) not in (
                {'source', 'subject', 'node'}, {'source', 'subject', 'nodes'})
                or policy['source'] != SOURCE):
            raise ValueError('conversation_final_new_chat_policy_required')
        value = {key: _identifier(policy[key], 'final_' + key, 1024 if key == 'subject' else 200)
                 for key in ('source', 'subject')}
        key = (value['source'], value['subject'])
        if key in result:
            raise ValueError('conversation_final_duplicate_policy')
        result[key] = (_identifier(policy['node'], 'final_node') if 'node' in policy else
                       _NODES_PREFIX + encoded(_node_set(policy['nodes'])).decode('utf8'))
    return result


def _answer(checkpoint, result):
    # Read ONLY the CURRENT authenticated finalization payload, not a saved
    # checkpoint, result column, native history, stdout, or approval record.
    value = checkpoint.get('answer') if isinstance(checkpoint, dict) else None
    if value is None:
        value = result
    if not isinstance(value, str) or not value.strip():
        raise ValueError('conversation_final_task_agent_text_required')
    data = value.encode('utf8')
    if len(data) > MAX_TEXT:
        suffix = '\n[Agent final reply truncated at the conversation text limit.]'
        value = data[:MAX_TEXT - len(suffix.encode('utf8'))].decode('utf8', errors='ignore') + suffix
    return value


class ConversationFinals:
    def __init__(self, api, policies):
        self.api, self.emits, self.policies = api, api._conversation_emits, policies
        self.store, self.ingress = api.store, api.ingress
        self.local = threading.local()
        with self.store.transaction() as db:
            self.emits._namespace(db)
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_reply_policies(
                request_key TEXT PRIMARY KEY,task_id TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,subject TEXT NOT NULL,node TEXT NOT NULL,
                policy_id TEXT NOT NULL,created REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_task_finals(
                operation_id TEXT PRIMARY KEY,request_key TEXT NOT NULL,
                task_id TEXT NOT NULL,epoch INTEGER NOT NULL,node TEXT NOT NULL,
                status TEXT NOT NULL,fingerprint TEXT NOT NULL,project_id TEXT,
                created REAL NOT NULL,UNIQUE(task_id,epoch))''')

    def admission(self, db, binding):
        node = self.policies.get((binding['source'], binding['subject']))
        if node is None:
            return
        self.emits._namespace(db)
        policy_id = digest([FORMAT, self.ingress.authority, self.ingress.owner_id,
                            binding['request_key'], binding['task_id'], node])
        db.execute('INSERT INTO conversation_reply_policies VALUES(?,?,?,?,?,?,?)',
            (binding['request_key'], binding['task_id'], binding['source'], binding['subject'],
             node, policy_id, self.store.clock()))

    def _policy(self, db, task_id):
        row = db.execute('SELECT * FROM conversation_reply_policies WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            return None, None
        binding = self.emits._binding(db, task_id)
        expected = digest([FORMAT, self.ingress.authority, self.ingress.owner_id,
                           binding['request_key'], task_id, row['node']])
        current = self.policies.get((row['source'], row['subject']))
        if (row['policy_id'] != expected or row['request_key'] != binding['request_key']
                or any(row[key] != binding[key] for key in ('source', 'subject'))
                or current is None
                or not set(_policy_nodes(row['node'])) <= set(_policy_nodes(current))):
            raise Conflict('conversation_final_original_reply_policy_changed')
        # A wider owner policy applies only to NEW admissions. Existing rows
        # retain their original finite membership, including legacy singletons.
        return row, binding

    @staticmethod
    def _worker(peer):
        if (not isinstance(peer, dict) or peer.get('role') != 'worker'
                or not isinstance(peer.get('capabilities'), list) or CAPABILITY not in peer['capabilities']):
            raise PermissionError('conversation_final_worker_grant_required')
        return _identifier(peer.get('node'), 'final_worker')

    def complete(self, db, task, node, epoch, checkpoint, result, status):
        if status not in TERMINAL:
            return
        self.emits._namespace(db)
        policy, binding = self._policy(db, task['id'])
        if policy is None:
            return  # NEVER adopt/backfill a pre-policy or non-Chat task.
        context = getattr(self.local, 'completion', None)
        if context is None or self._worker(context['peer']) != node or node not in _policy_nodes(policy['node']):
            raise PermissionError('conversation_final_authenticated_completion_required')
        if context['task_id'] != task['id'] or context['epoch'] != epoch:
            raise Conflict('conversation_final_original_completion_changed')
        # The patched frozen Store has ALREADY checked this live lease/Leader
        # generation, and both its UPDATE and this INSERT share the same db.
        if not self.store._task_is_live(db, task, node=node, epoch=epoch):
            raise Conflict('conversation_final_original_lease_not_live')
        value = {'task_id': task['id'], 'epoch': epoch, 'call_id': CALL_ID,
                 'message_id': 'mesh-auto-final-' + digest([FORMAT, binding['request_key'], task['id'], epoch]),
                 'kind': 'final', 'text': _answer(checkpoint, result)}
        receipt = self.emits._append_in_db(db, binding, node, value)
        operation = receipt['message']['operation_id']
        fingerprint = digest([FORMAT, status, value])
        project = json.loads(binding['body']).get('project_id')
        if project is not None:
            _identifier(project, 'final_project_id')
        db.execute('INSERT INTO conversation_task_finals VALUES(?,?,?,?,?,?,?,?,?)',
            (operation, binding['request_key'], task['id'], epoch, node, status, fingerprint,
             project, self.store.clock()))

    def _record(self, db, row):
        policy, binding = self._policy(db, row['task_id'])
        message = db.execute('SELECT * FROM conversation_emits WHERE operation_id=?', (row['operation_id'],)).fetchone()
        if policy is None or message is None:
            raise Conflict('conversation_final_original_receipt_missing')
        value = self.emits._message(message)
        if (row['request_key'] != binding['request_key'] or message['request_key'] != row['request_key']
                or any(row[key] != message[key] for key in ('task_id', 'epoch', 'node'))
                or row['node'] not in _policy_nodes(policy['node']) or value['call_id'] != CALL_ID or value['kind'] != 'final'
                or row['status'] not in TERMINAL
                or row['fingerprint'] != digest([FORMAT, row['status'],
                    {key: value[key] for key in ('task_id', 'epoch', 'call_id', 'message_id', 'kind', 'text')}])
                or row['project_id'] != json.loads(binding['body']).get('project_id')):
            raise Conflict('conversation_final_original_receipt_changed')
        refs = {'event_id': 'mesh-task-event-' + digest([FORMAT, row['operation_id'], row['status']]),
                'name': 'leader.attention_required' if row['status'] == 'needs_review' else 'task.completed',
                'owner_id': self.ingress.owner_id, 'source': binding['source'], 'subject': binding['subject'],
                'project_id': row['project_id'], 'request_id': binding['request_id'],
                'task_id': row['task_id'], 'task_epoch': row['epoch'], 'occurred_at': utc(row['created']),
                'message_id': _conversation_message_id(row['operation_id'])}
        return dict(self.emits._receipt(value), task_status=row['status'], event=refs)

    def receipt(self, peer, payload):
        node = self._worker(peer)
        if (not isinstance(payload, dict) or set(payload) != {'task_id', 'epoch'}
                or type(payload['epoch']) is not int or not 1 <= payload['epoch'] < (1 << 63)):
            raise ValueError('conversation_final_original_receipt_identity_required')
        _identifier(payload['task_id'], 'final_task_id')
        with self.store.transaction() as db:
            self.emits._namespace(db)
            policy, binding = self._policy(db, payload['task_id'])
            if policy is None or node not in _policy_nodes(policy['node']):
                raise PermissionError('conversation_final_original_worker_required')
            row = db.execute('SELECT * FROM conversation_task_finals WHERE task_id=? AND epoch=? AND node=?',
                (payload['task_id'], payload['epoch'], node)).fetchone()
            if row is None:
                return {'format': FORMAT, 'found': False, 'outcome': 'unconfirmed',
                        'retry_authorized': False, 'automatic_retry': False}
            return self._record(db, row)

    def completion_events(self, *, after=0, limit=20):
        """Trusted owner-code feed, NOT HTTP/tool/read-hook or a publication grant.

        Post-commit consumers may durably outbox these fixed refs, never rerun
        task work. A missing project remains None/unroutable, not a default.
        """
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('conversation_final_event_feed_bounds')
        with self.store.transaction() as db:
            self.emits._namespace(db)
            rows = db.execute('SELECT rowid AS cursor,* FROM conversation_task_finals '
                              'WHERE rowid>? ORDER BY rowid LIMIT ?', (after, limit)).fetchall()
            return {'events': [self._record(db, row)['event'] for row in rows],
                    'cursor': rows[-1]['cursor'] if rows else after}


def attach(api, *, enabled=False, policies=()):
    """Owner ready hook AFTER emit.attach; preserve original passive constructor."""
    if type(enabled) is not bool:
        raise ValueError('conversation_final_owner_enable_required')
    if not enabled:
        return None
    policies = _policies(policies)
    if (getattr(api, '_conversation_emits', None) is None
            or api._conversation_emits.store is not api.store
            or SOURCE not in api._conversation_emits.sources
            or getattr(api.store, '_conversation_final_hook_version', None) != 1):
        raise ValueError('conversation_final_patched_original_authority_required')
    old = getattr(api, '_conversation_finals', None)
    if old is not None:
        if old.policies != policies:
            raise Conflict('conversation_final_installed_policy_changed')
        return old
    if (getattr(api.store, '_conversation_final_in_db', None) is not None
            or getattr(api.ingress, '_conversation_reply_admission', None) is not None):
        raise Conflict('conversation_final_existing_hook')
    authority = ConversationFinals(api, policies)
    previous = api.dispatch
    def dispatch(method, path, payload, peer):
        parsed = urlsplit(path)
        if parsed.path == RECEIPT_ROUTE:
            if method != 'POST' or path != RECEIPT_ROUTE:
                raise PermissionError('conversation_final_private_receipt_required')
            return authority.receipt(peer, payload)
        if method == 'POST' and path == '/v1/task/update':
            # Only the authenticated peer passed by the server. Never accept a
            # role/node/grant or recipient from request JSON. Thread-local scope
            # cannot leak authorization between concurrent HTTP handlers.
            before = getattr(authority.local, 'completion', None)
            authority.local.completion = {'peer': peer, 'task_id': payload.get('id'), 'epoch': payload.get('epoch')}
            try:
                return previous(method, path, payload, peer)
            finally:
                authority.local.completion = before
        return previous(method, path, payload, peer)
    api.dispatch = dispatch
    api.store._conversation_final_in_db = authority.complete
    api.ingress._conversation_reply_admission = authority.admission
    api._conversation_finals = authority
    return authority
