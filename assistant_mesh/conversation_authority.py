"""Opt-in PRIVATE ingress conversation adapter over an already-open Store.

No Store/Worker/server replacement, listener, history edit or OAuth machinery.
The ready hook adds one exact internal route; public TLS still exposes /mcp only.
"""
import argparse
import hashlib
import json
from urllib.parse import urlsplit

from .ingress import _identifier
from .store import Conflict


ROUTE = '/v1/ingress/conversation'
FORMAT = 'mesh-conversation-authority/1'
SOURCE = 'chatgpt-chat'


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                     separators=(',', ':')).encode('utf8')
    return hashlib.sha256(raw).hexdigest()


class ConversationAuthority:
    """Original ingress binding + native lease/interaction/steering transaction."""
    def __init__(self, ingress, *, sources=(SOURCE,)):
        if ingress is None or not callable(getattr(ingress.store, '_task_is_live', None)):
            raise ValueError('conversation_original_ingress_required')
        if not isinstance(sources, (tuple, list)) or not sources:
            raise ValueError('conversation_sources_required')
        self.sources = frozenset(_identifier(source, 'conversation_source') for source in sources)
        self.ingress, self.store = ingress, ingress.store
        with self.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_deliveries(
                operation_id TEXT PRIMARY KEY,request_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,receipt TEXT NOT NULL)''')

    def _binding(self, db, peer, request_id, write=False):
        principal = self.ingress._principal(peer, 'tasks:submit' if write else 'tasks:read')
        if principal['source'] not in self.sources:
            raise PermissionError('conversation_source_not_authorized')
        request_id = _identifier(request_id, 'request_id')
        key = self.ingress._request_key(principal, request_id)
        original = self.ingress._binding(db, key, principal, request_id)
        if original is None:
            raise PermissionError('conversation_request_not_visible')
        task = db.execute('SELECT * FROM tasks WHERE id=?', (original['task_id'],)).fetchone()
        if task is None:
            raise Conflict('conversation_task_binding_missing')
        return key, task

    @staticmethod
    def _operation(key, payload):
        _identifier(payload.get('conversation_id'), 'conversation_id')
        _identifier(payload.get('message_id'), 'message_id')
        return digest([FORMAT, key, payload['conversation_id'], payload['message_id']])

    @staticmethod
    def _view(task, request_id):
        return {'format': FORMAT, 'request_id': request_id, 'task_id': task['id'],
                'task_epoch': task['epoch'], 'status': task['status'],
                'execution_verified': False, 'human_consent_verified': False}

    def _observe(self, db, key, task, request_id):
        live = self.store._task_is_live(db, task)
        questions, private_count = [], 0
        if live:
            rows = db.execute('SELECT * FROM interactions WHERE task_id=? AND epoch=? AND answer IS NULL',
                              (task['id'], task['epoch'])).fetchall()
            for row in rows:
                params = json.loads(row['params'])
                if row['kind'] != 'question' or any(item.get('isSecret') for item in params.get('questions', [])):
                    private_count += 1
                    continue
                # Only public question fields, never command/cwd/private params.
                visible = []
                for item in params.get('questions', []):
                    visible.append({name: item[name] for name in ('id', 'question', 'header', 'options') if name in item})
                questions.append({'interaction_id': row['id'], 'task_id': task['id'],
                    'task_epoch': task['epoch'], 'params_sha256': digest(params),
                    'questions': visible, 'created_at': row['created']})
        return dict(self._view(task, request_id), live=live, questions=questions,
                    private_interactions=private_count)

    def _receipt(self, db, key, payload):
        operation = self._operation(key, payload)
        row = db.execute('SELECT request_key,receipt FROM conversation_deliveries WHERE operation_id=?',
                         (operation,)).fetchone()
        if row is None:
            return {'format': FORMAT, 'operation_id': operation, 'found': False,
                    'outcome': 'unknown', 'automatic_retry': False}
        if row['request_key'] != key:
            raise Conflict('conversation_original_operation_changed')
        receipt = json.loads(row['receipt'])
        if receipt.get('steering_id'):
            steering = db.execute('SELECT task_id,epoch,state FROM steering WHERE id=?',
                                  (receipt['steering_id'],)).fetchone()
            if steering is None or steering['task_id'] != receipt['task_id'] or steering['epoch'] != receipt['task_epoch']:
                raise Conflict('conversation_original_steering_changed')
            receipt['steering_state'] = steering['state']
            receipt['native_submission_acknowledged'] = steering['state'] == 'submitted'
        # An authority queue/answer row is not proof the model read/understood it.
        return dict(receipt, found=True, native_consumption_verified=False)

    def dispatch(self, peer, payload):
        if not isinstance(payload, dict) or set(payload) != {'action', 'arguments'} or not isinstance(payload['arguments'], dict):
            raise ValueError('conversation_private_request_invalid')
        action, args = payload['action'], payload['arguments']
        fields = {'request_id'}
        if action in ('receipt', 'deliver'):
            fields |= {'conversation_id', 'message_id'}
        if action == 'deliver':
            fields |= {'task_epoch', 'text', 'interaction'}
        if action not in ('observe', 'receipt', 'deliver') or set(args) != fields:
            raise ValueError('conversation_private_request_invalid')
        with self.store.transaction() as db:
            key, task = self._binding(db, peer, args['request_id'], write=action == 'deliver')
            if action == 'observe':
                return self._observe(db, key, task, args['request_id'])
            if action == 'receipt':
                return self._receipt(db, key, args)
            operation, fingerprint = self._operation(key, args), digest(args)
            old = db.execute('SELECT fingerprint FROM conversation_deliveries WHERE operation_id=?', (operation,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('conversation_original_delivery_changed')
                return self._receipt(db, key, args)
            if (type(args['task_epoch']) is not int or args['task_epoch'] < 1
                    or not isinstance(args['text'], str) or not args['text'].strip()
                    or len(args['text'].encode('utf8')) > 8192):
                raise ValueError('conversation_private_delivery_invalid')
            receipt = dict(self._view(task, args['request_id']), operation_id=operation,
                task_epoch=args['task_epoch'], recorded_at=self.store.clock(),
                native_submission_acknowledged=False, native_consumption_verified=False,
                native_interaction_effective=False, automatic_retry=False)
            if args['task_epoch'] != task['epoch'] or not self.store._task_is_live(db, task):
                receipt['state'] = 'stale_original_task_epoch'
            elif args['interaction'] is not None:
                target = args['interaction']
                if (not isinstance(target, dict) or set(target) != {'task_id', 'task_epoch', 'interaction_id', 'params_sha256'}
                        or target['task_id'] != task['id'] or target['task_epoch'] != args['task_epoch']):
                    raise ValueError('conversation_original_question_required')
                row = db.execute('SELECT * FROM interactions WHERE id=?', (target['interaction_id'],)).fetchone()
                if (row is None or row['task_id'] != task['id'] or row['epoch'] != args['task_epoch']
                        or digest(json.loads(row['params'])) != target['params_sha256']):
                    raise Conflict('conversation_original_question_changed')
                params = json.loads(row['params'])
                if row['kind'] != 'question' or any(item.get('isSecret') for item in params.get('questions', [])):
                    receipt['state'] = 'requires_private_owner_interaction'
                elif row['answer'] is not None or not self.store._interaction_is_live(db, task, row):
                    receipt['state'] = 'original_question_no_longer_open'
                else:
                    questions = params.get('questions', [])
                    answer = ({'answers': {questions[0]['id']: {'answers': [args['text']]}}}
                              if len(questions) == 1 else json.loads(args['text']))
                    self.store._validate_interaction_answer(row, answer)
                    # Exact original resolve_interaction validation/row contract,
                    # plus source/request/epoch binding and same-transaction ACK.
                    db.execute('UPDATE interactions SET answer=? WHERE id=?', (json.dumps(answer), row['id']))
                    receipt.update(state='native_answer_recorded', interaction_id=row['id'],
                                   native_interaction_effective=True)
            else:
                identity = 'chat-reply-' + operation
                old_steer = db.execute('SELECT task_id,epoch,text FROM steering WHERE id=?', (identity,)).fetchone()
                if old_steer is not None and (old_steer['task_id'] != task['id'] or old_steer['epoch'] != args['task_epoch']
                                              or old_steer['text'] != args['text']):
                    raise Conflict('conversation_original_steering_changed')
                # Exact original Store.steer queue contract; do not nest Store
                # transactions or resolve a newly elected task generation.
                db.execute('INSERT OR IGNORE INTO steering(id,task_id,epoch,text) VALUES(?,?,?,?)',
                           (identity, task['id'], args['task_epoch'], args['text']))
                receipt.update(state='queued_for_native', steering_id=identity, steering_state='pending')
            db.execute('INSERT INTO conversation_deliveries VALUES(?,?,?,?)',
                       (operation, key, fingerprint, json.dumps(receipt, sort_keys=True)))
            return dict(receipt, found=True)


def attach(api, *, sources=(SOURCE,)):
    """Owner ready hook; one exact route on the existing authenticated API."""
    if getattr(api, '_conversation_authority', None) is not None:
        if api._conversation_authority.sources != frozenset(sources):
            raise Conflict('conversation_installed_sources_changed')
        return api._conversation_authority
    authority = ConversationAuthority(api.ingress, sources=sources)
    previous = api.dispatch
    def dispatch(method, path, payload, peer):
        parsed = urlsplit(path)
        if parsed.path == ROUTE:
            if method != 'POST' or path != ROUTE:
                raise PermissionError('conversation_private_route_not_authorized')
            return authority.dispatch(peer, payload)
        return previous(method, path, payload, peer)
    api.dispatch = dispatch
    api._conversation_authority = authority
    return authority


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit optional hook on the sole entrance receiver')
    parser.add_argument('--config', required=True)
    parser.add_argument('--socket', required=True)
    args = parser.parse_args(argv)
    from .config import private_json
    from .entrance_runtime import serve_entrance
    return serve_entrance(private_json(args.config), args.socket,
                          ready=lambda http, api, channel: attach(api))


if __name__ == '__main__':
    main()
