"""Opt-in Work MCP Events ledger and bounded webhook delivery.

Only owner code can publish original completion/attention receipt references.
No Store, native history, business retry, OAuth engine or background model call.
The installer MUST supply a current account/project access check (strict True).
"""
import contextlib
import hmac
import json
import secrets
import sqlite3
import threading
import time

from .cloud_jobs import _private_dir, _private_file
from .conversation import digest, encoded, original_time, utc
from .ingress import _identifier, validate_peer
from .mcp_event_transport import CallbackError, WebhookTransport, callback_url, signed_headers, signing_key
from .mcp_http_auth import NotConfigured, SOURCE
from .store import Conflict


FORMAT = 'mesh-work-mcp-events/1'
NAMES = ('task.completed', 'leader.attention_required')
MAX_ATTEMPTS = 5


def arguments(value):
    if not isinstance(value, dict) or not {'project_id'} <= set(value) <= {'project_id', 'request_id'}:
        raise ValueError('invalid_event_arguments')
    return {key: _identifier(item, 'event_' + key) for key, item in value.items()}


class MCPEvents:
    def __init__(self, directory, owner_binding, *, authorize, transport=None, clock=time.time):
        if not callable(authorize):
            raise NotConfigured('events_current_access_check_required')
        self.owner = _identifier(owner_binding, 'events_owner')
        self.authorize, self.transport, self.clock = authorize, transport or WebhookTransport(), clock
        if not callable(getattr(self.transport, 'post', None)):
            raise NotConfigured('events_transport_required')
        self.root = _private_dir(directory, create=False)
        self.database = self.root / 'work-events.sqlite'
        _private_file(self.database, create=True)
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS event_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS event_subscriptions(
                id TEXT PRIMARY KEY,principal TEXT NOT NULL,name TEXT NOT NULL,arguments TEXT NOT NULL,
                url TEXT NOT NULL,secret TEXT,old_secret TEXT,rotation_until REAL,
                expires REAL NOT NULL,state TEXT NOT NULL,generation INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS event_verifications(
                identity TEXT PRIMARY KEY,verified_until REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS event_records(
                id TEXT PRIMARY KEY,principal TEXT NOT NULL,project_id TEXT,
                request_id TEXT NOT NULL,name TEXT NOT NULL,body BLOB NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS event_outbox(
                subscription TEXT NOT NULL,event_id TEXT NOT NULL,state TEXT NOT NULL,
                attempts INTEGER NOT NULL,next_attempt REAL NOT NULL,last_code TEXT,
                PRIMARY KEY(subscription,event_id))''')
            expected = {'format': FORMAT, 'owner': self.owner, 'source': SOURCE}
            old = dict(db.execute("SELECT key,value FROM event_metadata WHERE key NOT IN ('receipt_cursor','private_feed_binding')"))
            if old != expected:
                if old or db.execute('SELECT 1 FROM event_subscriptions LIMIT 1').fetchone():
                    raise Conflict('events_namespace_conflict')
                db.executemany('INSERT INTO event_metadata VALUES(?,?)', expected.items())

    @contextlib.contextmanager
    def transaction(self):
        _private_dir(self.root); _private_file(self.database)
        db = sqlite3.connect(str(self.database), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE'); yield db; db.commit()
        except BaseException:
            db.rollback(); raise
        finally:
            db.close()

    def _principal(self, principal):
        value = validate_peer({'role': 'ingress', 'ingress': principal}, self.owner)
        if value['source'] != SOURCE or 'tasks:read' not in value['scopes']:
            raise PermissionError('events_not_authorized')
        return value

    def _allowed(self, principal, name, args):
        try:
            return self.authorize(dict(principal), name, dict(args)) is True
        except Exception:
            return False

    def _identity(self, principal, name, args, url):
        if name not in NAMES:
            raise ValueError('invalid_event_name')
        args = arguments(args); callback_url(url)
        identity = {key: principal[key] for key in ('owner_id', 'source', 'subject')}
        return 'sub_' + digest([FORMAT, identity, url, name, args]), args

    def list(self, principal, cursor=None):
        principal = self._principal(principal)
        if cursor is not None:
            raise ValueError('invalid_event_cursor')
        if not self._allowed(principal, None, {}):
            raise PermissionError('events_not_authorized')
        schema = {'type': 'object', 'properties': {'project_id': {'type': 'string'},
                  'request_id': {'type': 'string'}}, 'required': ['project_id'], 'additionalProperties': False}
        payload = {'type': 'object', 'properties': {key: {'type': 'string'} for key in
                   ('project_id', 'request_id', 'task_id', 'message_id')},
                   'required': ['project_id', 'request_id', 'task_id', 'message_id', 'task_epoch'],
                   'additionalProperties': False}
        payload['properties']['task_epoch'] = {'type': 'integer', 'minimum': 1}
        return {'events': [{'name': name, 'description': description, 'delivery': ['webhook'],
                            'inputSchema': schema, 'payloadSchema': payload} for name, description in zip(NAMES, (
            'An authorized original Mesh task finished (completed or failed); read its retained reply/status using the original request.',
            'The original Mesh Leader has a question or needs owner attention; read its retained message.'))]}

    def _verify(self, principal, url, secret, subscription):
        identity = digest([principal['owner_id'], principal['source'], principal['subject'], url, digest(secret)])
        with self.transaction() as db:
            cached = db.execute('SELECT verified_until FROM event_verifications WHERE identity=?', (identity,)).fetchone()
        if cached and cached[0] > self.clock():
            return
        challenge = secrets.token_urlsafe(32)
        verification_id = 'msg_verification_' + secrets.token_hex(16)
        body = encoded({'type': 'verification', 'challenge': challenge})
        started = self.clock()
        try:
            status, raw = self.transport.post(url, body, signed_headers(
                verification_id, int(started), body, subscription, [secret]))
            response = json.loads(raw)
            returned = response.get('challenge') if isinstance(response, dict) else None
            if (not 200 <= status < 300 or not isinstance(returned, str)
                    or not hmac.compare_digest(returned, challenge) or not 0 <= self.clock() - started <= 30):
                raise CallbackError('challenge_failed')
        except CallbackError:
            raise
        except (ValueError, UnicodeError):
            raise CallbackError('challenge_failed') from None
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO event_verifications VALUES(?,?)', (identity, self.clock() + 300))

    def subscribe(self, principal, *, name, arguments: dict, delivery, cursor=None, ttlMs=3600000):
        principal = self._principal(principal)
        if (not isinstance(delivery, dict) or set(delivery) != {'mode', 'url', 'secret'}
                or delivery.get('mode') != 'webhook' or cursor is not None):
            raise ValueError('invalid_event_delivery')
        secret = delivery['secret']; signing_key(secret)
        identifier, args = self._identity(principal, name, arguments, delivery['url'])
        if not self._allowed(principal, name, args):
            raise PermissionError('events_not_authorized')
        if ttlMs is not None and (type(ttlMs) is not int or ttlMs <= 0):
            raise ValueError('invalid_event_ttl')
        lifetime = 3600 if ttlMs is None else max(60, min(ttlMs, 86400000) / 1000)
        with self.transaction() as db:
            row = db.execute('SELECT * FROM event_subscriptions WHERE id=?', (identifier,)).fetchone()
            before = dict(row) if row else None
        self._verify(principal, delivery['url'], secret, identifier)
        if not self._allowed(principal, name, args):
            raise PermissionError('events_not_authorized')
        expires = self.clock() + lifetime
        with self.transaction() as db:
            row = db.execute('SELECT * FROM event_subscriptions WHERE id=?', (identifier,)).fetchone()
            if (dict(row) if row else None) != before:
                raise Conflict('events_subscription_changed')
            old_secret = before['secret'] if before and before['secret'] != secret else (before['old_secret'] if before else None)
            rotation = self.clock() + 300 if before and before['secret'] != secret else (before['rotation_until'] if before else None)
            generation = before['generation'] + 1 if before else 1
            db.execute('INSERT OR REPLACE INTO event_subscriptions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, encoded(principal).decode(), name, encoded(args).decode(), delivery['url'], secret,
                 old_secret, rotation, expires, 'active', generation))
        return {'id': identifier, 'refreshBefore': utc(expires), 'cursor': None, 'truncated': False}

    def unsubscribe(self, principal, *, name, arguments: dict, delivery):
        principal = self._principal(principal)
        if not isinstance(delivery, dict) or set(delivery) != {'mode', 'url'} or delivery.get('mode') != 'webhook':
            raise ValueError('invalid_event_delivery')
        identifier, args = self._identity(principal, name, arguments, delivery['url'])
        # Account binding authorizes cancelling its own subscription even after
        # project access is revoked. Other subjects cannot derive that identity.
        with self.transaction() as db:
            row = db.execute('SELECT generation FROM event_subscriptions WHERE id=?', (identifier,)).fetchone()
            generation = row[0] + 1 if row else 1
            db.execute('INSERT OR REPLACE INTO event_subscriptions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, encoded(principal).decode(), name, encoded(args).decode(), delivery['url'],
                 None, None, None, self.clock(), 'cancelled', generation))
            db.execute("UPDATE event_outbox SET state='cancelled' WHERE subscription=? AND state NOT IN ('accepted','failed')", (identifier,))
        return {}

    def record_task_event(self, *, event_id, name, owner_id, source, subject, project_id,
                          request_id, task_id, task_epoch, occurred_at, message_id):
        """Trusted receipt adapter only. No remote publisher or model fields.

        A stable original receipt ID permits post-commit synchronization retries,
        never another task/business invocation. Missing project is unroutable.
        """
        if owner_id != self.owner or source != SOURCE or name not in NAMES:
            raise PermissionError('events_original_receipt_not_authorized')
        principal = self._principal({'owner_id': owner_id, 'source': source, 'subject': subject, 'scopes': ['tasks:read']})
        args = (arguments({'project_id': project_id, 'request_id': request_id}) if project_id is not None
                else {'project_id': None, 'request_id': _identifier(request_id, 'event_request_id')})
        for value in (event_id, task_id, message_id):
            _identifier(value, 'event_reference')
        if type(task_epoch) is not int or task_epoch < 1 or original_time(occurred_at) is None:
            raise ValueError('invalid_event_original_time_or_epoch')
        body = encoded({'eventId': event_id, 'name': name, 'timestamp': original_time(occurred_at),
            'data': dict(args, task_id=task_id, task_epoch=task_epoch, message_id=message_id), 'cursor': None})
        audience = encoded({key: principal[key] for key in ('owner_id', 'source', 'subject')}).decode()
        event = (event_id, audience, project_id, request_id, name, body)
        with self.transaction() as db:
            old = db.execute('SELECT * FROM event_records WHERE id=?', (event_id,)).fetchone()
            if old:
                if tuple(old) != event:
                    raise Conflict('events_original_receipt_conflict')
                return {'recorded': True, 'new_event': False}
            db.execute('INSERT INTO event_records VALUES(?,?,?,?,?,?)', event)
            if project_id is None:
                # Retain the original unroutable receipt before advancing a
                # feed cursor. Never invent a project or block later receipts.
                return {'recorded': True, 'new_event': True, 'routable': False,
                        'reason': 'missing_authorized_project'}
            for sub in db.execute("SELECT * FROM event_subscriptions WHERE name=? AND state='active' AND expires>?", (name, self.clock())):
                peer = json.loads(sub['principal']); filters = json.loads(sub['arguments'])
                if (all(peer[key] == principal[key] for key in ('owner_id', 'source', 'subject'))
                        and filters['project_id'] == project_id
                        and ('request_id' not in filters or filters['request_id'] == request_id)):
                    db.execute('INSERT INTO event_outbox VALUES(?,?,?,0,?,NULL)', (sub['id'], event_id, 'pending', self.clock()))
        return {'recorded': True, 'new_event': True}

    def deliver_one(self):
        """One durable attempt; stable ID/body. Only webhooks, never work retries.

        The send transaction serializes unsubscribe against an actual send;
        cancellation acknowledges only after an already-inflight attempt ends.
        A process crash can cause a duplicate webhook, not a replacement task.
        """
        with self.transaction() as db:
            row = db.execute("SELECT subscription,event_id FROM event_outbox WHERE state IN ('pending','sending') AND next_attempt<=? ORDER BY next_attempt LIMIT 1", (self.clock(),)).fetchone()
            if not row:
                return {'attempted': False}
            sub_id, event_id = row
            item = db.execute('SELECT * FROM event_outbox WHERE subscription=? AND event_id=?', (sub_id, event_id)).fetchone()
            if item['attempts'] >= MAX_ATTEMPTS:
                db.execute("UPDATE event_outbox SET state='failed',last_code='attempts_exhausted' WHERE subscription=? AND event_id=?", (sub_id, event_id))
                return {'attempted': False, 'state': 'failed'}
            db.execute("UPDATE event_outbox SET state='sending',attempts=attempts+1,next_attempt=? WHERE subscription=? AND event_id=?",
                       (self.clock() + 60, sub_id, event_id))
        with self.transaction() as db:
            sub = db.execute('SELECT * FROM event_subscriptions WHERE id=?', (sub_id,)).fetchone()
            peer, filters = json.loads(sub['principal']), json.loads(sub['arguments'])
            if sub['state'] != 'active' or sub['expires'] <= self.clock() or not self._allowed(peer, sub['name'], filters):
                db.execute("UPDATE event_subscriptions SET state='inactive' WHERE id=? AND state='active'", (sub_id,))
                db.execute("UPDATE event_outbox SET state='cancelled',last_code='access_or_subscription_ended' WHERE subscription=? AND state IN ('pending','sending')", (sub_id,))
                return {'attempted': False, 'state': 'cancelled'}
            row = db.execute('SELECT * FROM event_outbox WHERE subscription=? AND event_id=?', (sub_id, event_id)).fetchone()
            if row['state'] != 'sending':
                return {'attempted': False, 'state': row['state']}
            body = db.execute('SELECT body FROM event_records WHERE id=?', (event_id,)).fetchone()[0]
            keys = [sub['secret']]
            if sub['old_secret'] is not None and sub['rotation_until'] > self.clock():
                keys.append(sub['old_secret'])
            retry, code = False, 'rejected'
            try:
                status, unused_body = self.transport.post(sub['url'], body, signed_headers(event_id, int(self.clock()), body, sub_id, keys))
                accepted = 200 <= status < 300
                retry = status in (408, 429) or 500 <= status <= 599
                code = 'accepted' if accepted else 'http_' + str(status)
                if status == 410:
                    db.execute("UPDATE event_subscriptions SET state='inactive' WHERE id=?", (sub_id,))
                    db.execute("UPDATE event_outbox SET state='cancelled' WHERE subscription=? AND event_id<>? AND state IN ('pending','sending')", (sub_id, event_id))
            except CallbackError as error:
                accepted, retry, code = False, error.transient, error.reason
            state = 'accepted' if accepted else ('pending' if retry and row['attempts'] < MAX_ATTEMPTS else 'failed')
            delay = min(300, 2 ** row['attempts'])
            db.execute('UPDATE event_outbox SET state=?,next_attempt=?,last_code=? WHERE subscription=? AND event_id=?',
                       (state, self.clock() + delay, code, sub_id, event_id))
            return {'attempted': True, 'state': state, 'webhook_receipt_only': accepted,
                    'chatgpt_resumed': False, 'event_id': event_id}


class EventDeliveryLoop:
    """Explicit owner lifecycle, default absent; close never wakes a model.

    receipt_source(after, limit) is an optional trusted read-only original-final
    outbox adapter. It returns {'receipts': [...], 'next_cursor': opaque}. Its
    cursor advances only after all original receipt IDs are durably recorded.
    """
    def __init__(self, service, *, receipt_source=None):
        self.service, self.source = service, receipt_source
        self.stop = threading.Event(); self.thread = None

    def step(self):
        feed_error = False
        if self.source is not None:
            try:
                with self.service.transaction() as db:
                    row = db.execute("SELECT value FROM event_metadata WHERE key='receipt_cursor'").fetchone()
                page = self.source(json.loads(row[0]) if row else None, 20)
                if not isinstance(page, dict) or set(page) != {'receipts', 'next_cursor'} or not isinstance(page['receipts'], list) or len(page['receipts']) > 20:
                    raise ValueError('invalid_event_receipt_page')
                for receipt in page['receipts']:
                    self.service.record_task_event(**receipt)
                with self.service.transaction() as db:
                    db.execute("INSERT OR REPLACE INTO event_metadata VALUES('receipt_cursor',?)", (encoded(page['next_cursor']).decode(),))
            except Exception:
                feed_error = True  # Original cursor/receipt IDs retained, no business replay.
        result = self.service.deliver_one()
        if feed_error:
            result['receipt_feed_unavailable'] = True
        return result

    def start(self):
        if self.thread is not None:
            raise Conflict('events_delivery_loop_already_started')
        def run():
            while not self.stop.is_set():
                try:
                    result = self.step()
                except Exception:
                    result = {'attempted': False}  # No content/secrets in logs.
                self.stop.wait(0.1 if result.get('attempted') else 1)
        self.thread = threading.Thread(target=run, name='mesh-work-event-delivery', daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(20)
            if self.thread.is_alive():
                raise Conflict('events_delivery_loop_close_unconfirmed')


def completion_receipt_source(finals):
    """Weak owner-code adapter; does not import or change the final producer."""
    if not callable(getattr(finals, 'completion_events', None)):
        raise NotConfigured('events_completion_receipt_source_required')
    def read(after, limit):
        start = 0 if after is None else after
        if type(start) is not int or start < 0:
            raise ValueError('invalid_event_completion_cursor')
        page = finals.completion_events(after=start, limit=limit)
        if (not isinstance(page, dict) or set(page) != {'events', 'cursor'}
                or type(page['cursor']) is not int or page['cursor'] < start):
            raise ValueError('invalid_event_completion_page')
        return {'receipts': page['events'], 'next_cursor': page['cursor']}
    return read
