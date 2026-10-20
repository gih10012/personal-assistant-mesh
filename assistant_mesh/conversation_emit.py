"""Opt-in task-agent messages to the task's ORIGINAL authenticated entrance.

No Store/Worker construction, model, task, approval or permanent loop. The
operator installs the private hook and grants one finite worker capability.
Original approved IngressResults publications are not read or modified here.
"""
import argparse
import json
import re
import sys
from urllib.parse import urlsplit

from .conversation import MAX_TEXT, digest, encoded, original_time, utc
from .ingress import _identifier
from .store import Conflict


FORMAT = 'mesh-conversation-emit/1'
CAPABILITY = 'owner.conversation.emit'
WRITE_ROUTE = '/v1/conversation/emit'
RECEIPT_ROUTE = '/v1/conversation/emit/receipt'
READ_ROUTE = '/v1/ingress/conversation/messages'
KINDS = frozenset(('progress', 'message', 'final'))


def _identity(payload, write=False):
    fields = {'task_id', 'epoch', 'call_id'}
    if write:
        fields |= {'message_id', 'kind', 'text'}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError('conversation_emit_arguments_invalid')
    value = dict(payload)
    for key in ('task_id', 'call_id') + (('message_id',) if write else ()):
        _identifier(value[key], 'emit_' + key)
    if type(value['epoch']) is not int or not 1 <= value['epoch'] < (1 << 63):
        raise ValueError('conversation_emit_epoch_invalid')
    if write:
        if (not isinstance(value['kind'], str) or value['kind'] not in KINDS or not isinstance(value['text'], str)
                or not value['text'].strip() or len(value['text'].encode('utf8')) > MAX_TEXT):
            raise ValueError('conversation_emit_message_invalid')
    encoded(value)
    return value


def _conversation_message_id(operation):
    return 'mesh-emit-' + operation


class ConversationEmits:
    """One additive message ledger on the already-open ingress authority."""
    def __init__(self, ingress, sources):
        if (ingress is None or not callable(getattr(ingress.store, '_task_is_live', None))
                or not isinstance(sources, (list, tuple)) or not sources):
            raise ValueError('conversation_emit_original_authority_required')
        self.ingress, self.store = ingress, ingress.store
        self.sources = frozenset(_identifier(source, 'emit_source') for source in sources)
        with self.store.transaction() as db:
            self._namespace(db)
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_emits(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,
                request_key TEXT NOT NULL,task_id TEXT NOT NULL,epoch INTEGER NOT NULL,
                node TEXT NOT NULL,call_id TEXT NOT NULL,message_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,
                UNIQUE(request_key,message_id))''')

    def _namespace(self, db):
        if dict(db.execute('SELECT key,value FROM ingress_metadata')) != {
                'schema': '1', 'authority': self.ingress.authority, 'owner': self.ingress.owner_id}:
            raise Conflict('conversation_emit_owner_namespace_changed')

    def _worker(self, peer):
        if (not isinstance(peer, dict) or peer.get('role') != 'worker'
                or not isinstance(peer.get('capabilities'), list) or CAPABILITY not in peer['capabilities']):
            raise PermissionError('conversation_emit_worker_grant_required')
        return _identifier(peer.get('node'), 'emit_worker')

    def _binding(self, db, task_id):
        rows = db.execute('SELECT request_key,source,subject,request_id FROM ingress_requests WHERE task_id=?',
                          (task_id,)).fetchall()
        if len(rows) != 1 or rows[0]['source'] not in self.sources:
            raise PermissionError('conversation_emit_original_ingress_required')
        row = rows[0]
        original = self.ingress._binding(db, row['request_key'], row, row['request_id'])
        if original['task_id'] != task_id:
            raise Conflict('conversation_emit_original_task_changed')
        return original

    def _operation(self, node, value):
        return digest([FORMAT, self.ingress.authority, self.ingress.owner_id, node,
                       value['task_id'], value['epoch'], value['call_id']])

    def _message(self, row):
        value = _identity(json.loads(row['body']), write=True)
        if (row['body'] != encoded(value).decode('utf8') or row['fingerprint'] != digest(value)
                or row['operation_id'] != self._operation(row['node'], value)
                or any(row[key] != value[key] for key in ('task_id', 'epoch', 'call_id', 'message_id'))):
            raise Conflict('conversation_emit_original_message_changed')
        return dict(value, operation_id=row['operation_id'], sequence=row['sequence'],
                    created_at=utc(row['created']), producer='authenticated_task_agent',
                    human_reviewed=False, execution_verified=False)

    def emit(self, peer, payload):
        node, value = self._worker(peer), _identity(payload, write=True)
        if value['call_id'] == 'automatic-task-final-v1' or value['message_id'].startswith('mesh-auto-final-'):
            raise PermissionError('conversation_emit_automatic_final_identity_reserved')
        with self.store.transaction() as db:
            self._namespace(db)
            task = db.execute('SELECT * FROM tasks WHERE id=?', (value['task_id'],)).fetchone()
            if not self.store._task_is_live(db, task, node=node, epoch=value['epoch']):
                raise Conflict('conversation_emit_original_lease_not_live')
            binding = self._binding(db, value['task_id'])
            return self._append_in_db(db, binding, node, value)

    def _append_in_db(self, db, binding, node, value):
        """Internal only: caller has fenced the authenticated producer in THIS transaction."""
        value = _identity(value, write=True)
        operation = self._operation(node, value)
        old = db.execute('SELECT * FROM conversation_emits WHERE operation_id=?', (operation,)).fetchone()
        if old is not None:
            message = self._message(old)
            if old['request_key'] != binding['request_key'] or old['body'] != encoded(value).decode('utf8'):
                raise Conflict('conversation_emit_call_content_conflict')
            return self._receipt(message, created=False)
        if db.execute('SELECT 1 FROM conversation_emits WHERE request_key=? AND message_id=?',
                      (binding['request_key'], value['message_id'])).fetchone():
            raise Conflict('conversation_emit_message_identity_conflict')
        db.execute('''INSERT INTO conversation_emits(operation_id,request_key,task_id,epoch,node,
            call_id,message_id,fingerprint,body,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (operation, binding['request_key'], value['task_id'], value['epoch'], node,
             value['call_id'], value['message_id'], digest(value), encoded(value).decode('utf8'), self.store.clock()))
        row = db.execute('SELECT * FROM conversation_emits WHERE operation_id=?', (operation,)).fetchone()
        return self._receipt(self._message(row), created=True)

    @staticmethod
    def _receipt(message, created=False):
        return {'format': FORMAT, 'found': True, 'created': created, 'message': message,
                'conversation_message_id': _conversation_message_id(message['operation_id']),
                'automatic_retry': False, 'client_display_verified': False, 'human_reviewed': False}

    def receipt(self, peer, payload):
        # Original-node receipts remain readable after lease closure; this does
        # not renew the lease, emit a message or permit another write attempt.
        node, value = self._worker(peer), _identity(payload)
        with self.store.transaction() as db:
            self._namespace(db)
            binding = self._binding(db, value['task_id'])
            row = db.execute('SELECT * FROM conversation_emits WHERE operation_id=?',
                             (self._operation(node, value),)).fetchone()
            if row is None:
                return {'format': FORMAT, 'found': False, 'automatic_retry': False,
                        'outcome': 'unconfirmed', 'retry_authorized': False}
            if row['request_key'] != binding['request_key']:
                raise Conflict('conversation_emit_original_binding_changed')
            return self._receipt(self._message(row))

    def read(self, peer, payload):
        principal = self.ingress._principal(peer, 'tasks:read')
        if (principal['source'] not in self.sources or not isinstance(payload, dict)
                or set(payload) != {'request_id', 'after', 'limit'}):
            raise PermissionError('conversation_emit_read_not_authorized')
        request_id = _identifier(payload['request_id'], 'emit_request_id')
        if (type(payload['after']) is not int or not 0 <= payload['after'] <= 2**53 - 1
                or type(payload['limit']) is not int or not 1 <= payload['limit'] <= 20):
            raise ValueError('conversation_emit_read_bounds_invalid')
        with self.store.transaction() as db:
            self._namespace(db)
            key = self.ingress._request_key(principal, request_id)
            binding = self.ingress._binding(db, key, principal, request_id)
            if binding is None:
                raise PermissionError('conversation_emit_request_not_visible')
            last = db.execute('SELECT COALESCE(MAX(sequence),0) FROM conversation_emits WHERE request_key=?',
                              (key,)).fetchone()[0]
            if payload['after'] > last:
                raise Conflict('conversation_emit_cursor_ahead')
            page = []
            for row in db.execute('SELECT * FROM conversation_emits WHERE request_key=? AND sequence>? ORDER BY sequence LIMIT ?',
                                  (key, payload['after'], payload['limit'])):
                if row['task_id'] != binding['task_id']:
                    raise Conflict('conversation_emit_original_binding_changed')
                message = self._message(row)
                if len(encoded(page + [message])) > 60000:
                    break
                page.append(message)
            cursor = page[-1]['sequence'] if page else payload['after']
            return {'format': FORMAT, 'request_id': request_id, 'task_id': binding['task_id'],
                    'messages': page, 'cursor': cursor, 'has_more': cursor < last}


def attach(api, *, enabled=False, sources=()):
    """Explicit owner ready hook. Disabled leaves the original API untouched."""
    if type(enabled) is not bool:
        raise ValueError('conversation_emit_owner_enable_required')
    if not enabled:
        return None
    if getattr(api, 'ingress', None) is None or api.ingress.store is not getattr(api, 'store', None):
        raise ValueError('conversation_emit_existing_api_required')
    previous_emit = getattr(api, '_conversation_emits', None)
    if previous_emit is not None:
        if previous_emit.sources != frozenset(sources):
            raise Conflict('conversation_emit_installed_sources_changed')
        return previous_emit
    authority = ConversationEmits(api.ingress, sources)
    previous = api.dispatch
    routes = {WRITE_ROUTE: authority.emit, RECEIPT_ROUTE: authority.receipt, READ_ROUTE: authority.read}
    def dispatch(method, path, payload, peer):
        if urlsplit(path).path in routes:
            if method != 'POST' or path not in routes:
                raise PermissionError('conversation_emit_private_route_required')
            return routes[path](peer, payload)
        return previous(method, path, payload, peer)
    api.dispatch = dispatch
    api._conversation_emits = authority
    return authority


def enable_projection(bridge, *, enabled=False):
    """Owner sidecar hook; copies already-authored messages, never emits one."""
    from .conversation_runtime import ConversationBridge
    if type(enabled) is not bool or not isinstance(bridge, ConversationBridge):
        raise ValueError('conversation_emit_original_bridge_required')
    if not enabled:
        return bridge
    with bridge.lock, bridge.service.transaction() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS conversation_emit_projections(
            identity TEXT NOT NULL,operation_id TEXT NOT NULL,source_message_id TEXT NOT NULL,
            source_created_at TEXT NOT NULL,projected_message_id TEXT NOT NULL,projected_at TEXT NOT NULL,
            sequence INTEGER NOT NULL,PRIMARY KEY(identity,operation_id))''')
    bridge._conversation_emit_projection = lambda identity, binding: _project(bridge, identity, binding)
    return bridge


def _project(bridge, identity, binding):
    service = bridge.service
    with service.transaction() as db:
        after = db.execute('SELECT COALESCE(MAX(sequence),0) FROM conversation_emit_projections WHERE identity=?',
                           (identity,)).fetchone()[0]
    page = bridge.client.request(READ_ROUTE, {'request_id': binding['request_id'], 'after': after, 'limit': 20})
    if (not isinstance(page, dict) or page.get('format') != FORMAT or page.get('request_id') != binding['request_id']
            or page.get('task_id') not in bridge.requests
            or bridge.requests[page['task_id']] != binding['request_id']
            or not isinstance(page.get('messages'), list) or len(page['messages']) > 20
            or type(page.get('cursor')) is not int or type(page.get('has_more')) is not bool):
        raise Conflict('conversation_emit_projection_response_unknown')
    cursor = after
    for message in page['messages']:
        fields = {'task_id', 'epoch', 'call_id', 'message_id', 'kind', 'text', 'operation_id',
                  'sequence', 'created_at', 'producer', 'human_reviewed', 'execution_verified'}
        if (not isinstance(message, dict) or set(message) != fields or message['task_id'] != page['task_id']
                or not isinstance(message['operation_id'], str) or not re.fullmatch('[0-9a-f]{64}', message['operation_id'])
                or type(message['sequence']) is not int or not cursor < message['sequence'] <= page['cursor']
                or message['producer'] != 'authenticated_task_agent'
                or message['human_reviewed'] is not False or message['execution_verified'] is not False):
            raise Conflict('conversation_emit_projection_message_unknown')
        _identity({key: message[key] for key in ('task_id', 'epoch', 'call_id', 'message_id', 'kind', 'text')}, write=True)
        created = original_time(message['created_at'])
        if created is None:
            raise Conflict('conversation_emit_source_time_missing')
        target = _conversation_message_id(message['operation_id'])
        payload = dict(binding, message_id=target, text=message['text'], kind=message['kind'], original_time=created)
        provenance = {'producer': 'authenticated_task_agent', 'human_reviewed': False,
                      'source_message_id': message['message_id'], 'source_call_id': message['call_id'],
                      'source_task_epoch': message['epoch'], 'source_created_at': created}
        # Fence replies BEFORE making this message visible, so a crash between
        # publication and projection bookkeeping cannot adopt a newer epoch.
        with service.transaction() as db:
            db.execute('INSERT OR IGNORE INTO conversation_bridge_publications VALUES(?,?,?)',
                       (identity, target, message['epoch']))
            publication = db.execute('SELECT task_epoch FROM conversation_bridge_publications '
                                     'WHERE identity=? AND message_id=?', (identity, target)).fetchone()
            if publication['task_epoch'] != message['epoch']:
                raise Conflict('conversation_emit_original_publication_epoch_changed')
        old = service.request(bridge.principal, 'receipt', dict(binding, message_id=target))
        if not old['found']:
            saved = service.publish(bridge.principal, payload, provenance=provenance)
        elif (old['message']['direction'] != 'to_client'
                or any(old['message'][key] != payload[key] for key in ('text', 'kind', 'original_time'))
                or old['message'].get('provenance') != provenance):
            raise Conflict('conversation_emit_original_projection_changed')
        else:
            saved = old
        with service.transaction() as db:
            db.execute('INSERT OR IGNORE INTO conversation_emit_projections VALUES(?,?,?,?,?,?,?)',
                (identity, message['operation_id'], message['message_id'], created, target,
                 saved['message']['received_at'], message['sequence']))
        cursor = message['sequence']
    if cursor != page['cursor']:
        raise Conflict('conversation_emit_projection_cursor_unknown')
    return {'agent_messages_projected': len(page['messages']), 'agent_messages_have_more': page['has_more']}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit current-task agent emit or original receipt; never retries')
    parser.add_argument('--config', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--epoch', type=int, required=True)
    parser.add_argument('--call-id', required=True)
    parser.add_argument('command', choices=('emit', 'receipt'))
    parser.add_argument('--payload-file')
    args = parser.parse_args(argv)
    if (args.command == 'emit') != bool(args.payload_file):
        parser.error('emit requires a private payload; receipt takes no payload')
    try:
        from .config import private_json
        from .worker import Client
        payload = {'task_id': args.task_id, 'epoch': args.epoch, 'call_id': args.call_id}
        if args.command == 'emit':
            body = private_json(args.payload_file)
            if not isinstance(body, dict) or set(body) != {'message_id', 'kind', 'text'}:
                raise ValueError('conversation_emit_private_message_required')
            payload.update(body)
        _identity(payload, write=args.command == 'emit')
        result = Client(private_json(args.config)).request(WRITE_ROUTE if args.command == 'emit' else RECEIPT_ROUTE, payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({'ok': False, 'error': 'conversation_emit_unconfirmed', 'automatic_retry': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
