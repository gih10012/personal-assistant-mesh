"""Private, owner-bound conversation messages for the remote MCP entrance.

This adds no public listener, native approval, task replay or autonomous wake.
An existing authenticated ingress remains the request/task authority. Owner
code may publish selected messages; remote clients cannot call that publisher.
"""
import contextlib
import datetime
import hashlib
import json
import re
import sqlite3
import threading
import time

from .cloud_jobs import _private_dir, _private_file
from .ingress import _identifier, validate_peer
from .mcp_http_auth import SOURCE, NotConfigured
from .native_messaging import project_response
from .store import Conflict


FORMAT = 'mesh-conversation/1'
MAX_TEXT = 8192
TOOLS = {'mesh_conversation_open': 'open', 'mesh_conversation_receive': 'receive',
         'mesh_conversation_reply': 'reply', 'mesh_conversation_receipt': 'receipt'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def utc(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def original_time(value):
    if value is None:
        return None  # Never substitute processing time for a missing platform time.
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)', value):
        raise ValueError('conversation_original_time_invalid')
    # strptime keeps this business contract importable by the legacy core.
    normalized = value[:-1] + '+0000' if value.endswith('Z') else value[:-3] + value[-2:]
    pattern = '%Y-%m-%dT%H:%M:%S.%f%z' if '.' in normalized else '%Y-%m-%dT%H:%M:%S%z'
    return datetime.datetime.strptime(normalized, pattern).astimezone(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def arguments(action, payload):
    fields = {'conversation_id', 'request_id'}
    required = set(fields)
    if action == 'receive':
        fields |= {'after', 'limit', 'wait_ms'}
    elif action == 'reply':
        required |= {'message_id', 'text'}
        fields |= {'message_id', 'text', 'reply_to', 'original_time'}
    elif action == 'receipt':
        required.add('message_id')
        fields.add('message_id')
    elif action != 'open':
        raise ValueError('conversation_action_invalid')
    if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - fields:
        raise ValueError('conversation_arguments_invalid')
    value = dict(payload)
    for key in ('conversation_id', 'request_id', 'message_id', 'reply_to'):
        if key in required or value.get(key) is not None:
            _identifier(value.get(key), 'conversation_' + key)
    if action == 'receive':
        for key, default, maximum in (('after', 0, 2**53 - 1), ('limit', 20, 20), ('wait_ms', 0, 20000)):
            number = value.setdefault(key, default)
            if type(number) is not int or not (1 if key == 'limit' else 0) <= number <= maximum:
                raise ValueError('conversation_receive_bound_invalid')
    if action == 'reply':
        if (not isinstance(value['text'], str) or not value['text'].strip()
                or len(value['text'].encode('utf8')) > MAX_TEXT):
            raise ValueError('conversation_text_invalid')
        value['original_time'] = original_time(value.get('original_time'))
        value.setdefault('reply_to', None)
    encoded(value)
    return value


def dispatch(service, verifier, token, action, payload):
    """Validated HTTP principal, fixed actions, no retry or token forwarding."""
    try:
        principal = verifier.principal(token)
        write = action in ('open', 'reply')
        if ('tasks:submit' if write else 'tasks:read') not in principal['scopes']:
            return {'schema': 1, 'ok': False, 'error': 'insufficient_scope',
                    'outcome': 'not_attempted', 'retry_with_new_id': False}
        value = arguments(action, payload)
    except PermissionError:
        return {'schema': 1, 'ok': False, 'error': 'not_authorized_or_not_visible',
                'outcome': 'rejected', 'retry_with_new_id': False}
    except Exception:
        return {'schema': 1, 'ok': False, 'error': 'invalid_conversation_arguments',
                'outcome': 'not_attempted', 'retry_with_new_id': False}
    try:
        result = service.request(principal, action, value)
        verifier.principal(token)  # A bounded wait must not disclose after expiry.
        return {'schema': 1, 'ok': True, 'data': result, 'retry_with_new_id': False}
    except PermissionError:
        category = 'not_authorized_or_not_visible'
    except (Conflict, NotConfigured):
        category = 'original_binding_or_live_interaction_unavailable'
    except Exception:
        category = 'conversation_outcome_unknown'
    return {'schema': 1, 'ok': False, 'error': category,
            'outcome': 'unknown' if write else 'not_available', 'retry_with_new_id': False}


class ConversationService:
    """One private journal; no Store construction or ingress scope changes.

    `backend` is the existing owner-bound Local/RemoteIngressBackend. A trusted
    live_interaction_validator may check the real task/epoch/question snapshot;
    absence denies question publication and answers, not ordinary messages.
    The validator is CODE installed by the owner, never a JSON-approved boolean.
    """
    def __init__(self, directory, owner_id, backend, *, live_interaction_validator=None, clock=time.time):
        self.owner = _identifier(owner_id, 'conversation_owner')
        if not callable(getattr(backend, 'request', None)):
            raise NotConfigured('conversation_ingress_required')
        if live_interaction_validator is not None and not callable(live_interaction_validator):
            raise ValueError('conversation_live_validator_invalid')
        self.backend, self.validator, self.clock = backend, live_interaction_validator, clock
        self.root = _private_dir(directory, create=False)
        self.database = self.root / 'conversation.sqlite'
        _private_file(self.database, create=True)
        self.changed = threading.Condition()
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS conversation_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS conversations(
                identity TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,request_id TEXT NOT NULL,
                task_id TEXT NOT NULL,created TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_messages(
                conversation TEXT NOT NULL,sequence INTEGER NOT NULL,message_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,body TEXT NOT NULL,
                PRIMARY KEY(conversation,sequence),UNIQUE(conversation,message_id))''')
            expected = {'format': FORMAT, 'owner': self.owner, 'source': SOURCE}
            old = dict(db.execute('SELECT key,value FROM conversation_metadata'))
            if old != expected:
                if (old or db.execute('SELECT 1 FROM conversations LIMIT 1').fetchone()
                        or db.execute('SELECT 1 FROM conversation_messages LIMIT 1').fetchone()):
                    raise Conflict('conversation_namespace_conflict')
                db.executemany('INSERT INTO conversation_metadata VALUES(?,?)', expected.items())

    @contextlib.contextmanager
    def transaction(self):
        _private_dir(self.root)
        _private_file(self.database)
        db = sqlite3.connect(str(self.database), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _binding(self, principal, payload, write=False):
        binding = validate_peer({'role': 'ingress', 'ingress': principal}, self.owner)
        if binding['source'] != SOURCE or ('tasks:submit' if write else 'tasks:read') not in binding['scopes']:
            raise PermissionError('conversation_not_authorized')
        status = self.backend.request(binding, 'status', {'request_id': payload['request_id']})
        view = project_response('status', payload['request_id'], status)['data']
        if not view['accepted']:
            raise PermissionError('conversation_original_request_not_visible')
        identity = digest([FORMAT, self.owner, SOURCE, binding['subject'], payload['conversation_id']])
        return identity, view['task_id']

    @staticmethod
    def _row(db, identity, payload, task):
        row = db.execute('SELECT * FROM conversations WHERE identity=?', (identity,)).fetchone()
        if row is None:
            raise PermissionError('conversation_not_visible')
        if row['request_id'] != payload['request_id'] or row['task_id'] != task:
            raise Conflict('conversation_original_binding_conflict')
        return row

    @staticmethod
    def _base(row):
        return {'format': FORMAT, 'conversation_id': row['conversation_id'], 'request_id': row['request_id'],
                'task_id': row['task_id'], 'native_interaction_effective': False,
                'execution_verified': False, 'retry_with_new_id': False}

    def _live(self, row, interaction):
        if self.validator is None:
            raise NotConfigured('conversation_live_interaction_not_configured')
        fields = {'interaction_id', 'task_id', 'task_epoch', 'params_sha256'}
        if (not isinstance(interaction, dict) or set(interaction) != fields
                or interaction['task_id'] != row['task_id'] or type(interaction['task_epoch']) is not int
                or interaction['task_epoch'] < 1 or not isinstance(interaction['params_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', interaction['params_sha256'])):
            raise ValueError('conversation_interaction_binding_invalid')
        _identifier(interaction['interaction_id'], 'conversation_interaction')
        if self.validator(dict(interaction)) is not True:
            raise Conflict('conversation_original_interaction_not_live')

    def request(self, principal, action, payload):
        value = arguments(action, payload)
        identity, task = self._binding(principal, value, write=action in ('open', 'reply'))
        if action == 'open':
            with self.transaction() as db:
                db.execute('INSERT OR IGNORE INTO conversations VALUES(?,?,?,?,?)',
                           (identity, value['conversation_id'], value['request_id'], task, utc(self.clock())))
                return self._base(self._row(db, identity, value, task))
        if action == 'reply':
            return self._append(identity, value, task, 'to_mesh', 'message', None)
        deadline = time.monotonic() + value.get('wait_ms', 0) / 1000
        while True:
            with self.transaction() as db:
                row = self._row(db, identity, value, task)
                if action == 'receipt':
                    message = db.execute('SELECT body FROM conversation_messages WHERE conversation=? AND message_id=?',
                                         (identity, value['message_id'])).fetchone()
                    return dict(self._base(row), found=message is not None,
                                message=json.loads(message[0]) if message else None)
                last = db.execute('SELECT COALESCE(MAX(sequence),0) FROM conversation_messages WHERE conversation=?',
                                  (identity,)).fetchone()[0]
                if value['after'] > last:
                    raise Conflict('conversation_cursor_ahead')
                messages = db.execute('SELECT body FROM conversation_messages WHERE conversation=? AND sequence>? '
                                      'ORDER BY sequence LIMIT ?', (identity, value['after'], value['limit'])).fetchall()
                page = []
                for message in messages:
                    candidate = json.loads(message[0])
                    if len(encoded(page + [candidate])) > 60000:
                        break
                    page.append(candidate)
                cursor = page[-1]['sequence'] if page else value['after']
                result = dict(self._base(row), messages=page, cursor=cursor, has_more=cursor < last,
                              client_display_verified=False, background_wake_supported=False)
            remaining = deadline - time.monotonic()
            if page or remaining <= 0:
                # Recheck the SAME authority before releasing data after a wait.
                self._binding(principal, value)
                return result
            # A bounded tool wait, not MCP Events polling/streaming. Local owner
            # publishers notify immediately; cross-process writers are noticed
            # within 250ms. No permanent loop, task submit or transport replay.
            with self.changed:
                self.changed.wait(min(0.25, remaining))

    def _append(self, identity, value, task, direction, kind, interaction):
        fingerprint = digest([direction, kind, value, interaction])
        with self.transaction() as db:
            row = self._row(db, identity, value, task)
            old = db.execute('SELECT fingerprint,body FROM conversation_messages WHERE conversation=? AND message_id=?',
                             (identity, value['message_id'])).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('conversation_original_message_changed')
                return dict(self._base(row), message=json.loads(old['body']), recorded=True,
                            replayed_effect=False, native_interaction_effective=False)
            if value.get('reply_to'):
                target = db.execute('SELECT body FROM conversation_messages WHERE conversation=? AND message_id=?',
                                    (identity, value['reply_to'])).fetchone()
                if target is None:
                    raise Conflict('conversation_reply_target_missing')
                target = json.loads(target[0])
                if direction == target['direction']:
                    raise Conflict('conversation_reply_direction_invalid')
                if target['kind'] == 'question':
                    interaction = target['interaction']
                    self._live(row, interaction)
            if kind == 'question':
                self._live(row, interaction)
            now = utc(self.clock())
            sequence = db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_messages WHERE conversation=?',
                                  (identity,)).fetchone()[0]
            message = {'message_id': value['message_id'], 'sequence': sequence, 'direction': direction,
                'kind': kind, 'text': value['text'], 'text_sha256': hashlib.sha256(value['text'].encode('utf8')).hexdigest(),
                'reply_to': value.get('reply_to'), 'original_time': value.get('original_time'),
                'received_at': now, 'processed_at': now, 'sent_at': None, 'interaction': interaction,
                'request_id': row['request_id'], 'task_id': row['task_id'],
                'client_display_verified': False, 'native_interaction_effective': False}
            db.execute('INSERT INTO conversation_messages VALUES(?,?,?,?,?)',
                       (identity, sequence, value['message_id'], fingerprint, encoded(message).decode('utf8')))
        with self.changed:
            self.changed.notify_all()
        return dict(self._base(row), recorded=True, message=message, replayed_effect=False)

    def publish(self, principal, payload):
        """PRIVATE owner-code helper, never an MCP/public endpoint.

        The owner selects exactly what is published. Do not copy raw rollouts,
        credentials or arbitrary Store results. Publishing a `result` message
        is communication, NOT the old three-tool approved-result publication.
        """
        if not isinstance(payload, dict) or set(payload) - {
                'conversation_id', 'request_id', 'message_id', 'text', 'reply_to', 'original_time', 'kind', 'interaction'}:
            raise ValueError('conversation_publication_invalid')
        kind, interaction = payload.get('kind', 'message'), payload.get('interaction')
        if kind not in ('message', 'progress', 'question', 'result') or (kind != 'question' and interaction is not None):
            raise ValueError('conversation_publication_invalid')
        value = arguments('reply', {key: item for key, item in payload.items() if key not in ('kind', 'interaction')})
        identity, task = self._binding(principal, value, write=True)
        return self._append(identity, value, task, 'to_client', kind, interaction)

    def question(self, principal, payload):
        """Read/live-check the original question before OPTIONAL elicitation."""
        result = self.request(principal, 'receipt', payload)
        message = result['message']
        if not message or message['kind'] != 'question' or message['direction'] != 'to_client':
            raise Conflict('conversation_original_question_missing')
        self._live(result, message['interaction'])
        return result


def store_interaction_validator(store):
    """Reuse an ALREADY owner-installed Store's live predicate; never resolve it.

    Only non-secret questions, exact params hash, original task/epoch/id and
    unanswered/live lease qualify. The conversation reply remains a message:
    this helper does not call resolve_interaction or grant native permission.
    """
    def validate(binding):
        with store.transaction() as db:
            question = db.execute('SELECT * FROM interactions WHERE id=?', (binding['interaction_id'],)).fetchone()
            task = db.execute('SELECT * FROM tasks WHERE id=?', (binding['task_id'],)).fetchone()
            if (not question or question['task_id'] != binding['task_id'] or question['epoch'] != binding['task_epoch']
                    or question['kind'] != 'question' or question['answer'] is not None
                    or not store._interaction_is_live(db, task, question)):
                return False
            params = json.loads(question['params'])
            return (digest(params) == binding['params_sha256']
                    and not any(item.get('isSecret') for item in params.get('questions', [])))
    return validate
