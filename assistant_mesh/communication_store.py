"""Independent owner communication ledger; never a runnable Leader Store.

ChannelState, CarrierWitness and RemoteNotifications use this writer boundary.
An original task is a reference, not a copied executable task. New input waits
in a durable delivery queue; attempted input is never silently re-routed.
"""
import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from pathlib import Path

from .networking import canonical, digest, identifier
from .store import Conflict

PROTOCOL = 'mesh-communication-state/1'
RECEIPT_PROTOCOL = 'mesh-communication-inbox-receipt/1'
META_KEYS = frozenset(('cursor', 'owner_context', 'last_poll', 'activate_after_ms',
    'channel_error', 'owner_notification_channel_binding', 'owner_notification_receiver_incarnation'))
CORE_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS communication_inbox_order(
        message_id TEXT PRIMARY KEY,sequence INTEGER NOT NULL UNIQUE CHECK(sequence>0))''',
    '''CREATE TABLE IF NOT EXISTS outbox(
        id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL,
        status TEXT NOT NULL, client_id TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
        created REAL NOT NULL, context_version TEXT, media_items TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0, delivery_route TEXT NOT NULL DEFAULT 'channel')''',
    '''CREATE TABLE IF NOT EXISTS mesh_notify_receipts(
        source_node TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        outbox_id TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(source_node,request_id))''',
    '''CREATE TABLE IF NOT EXISTS communication_identity(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL,
        import_sha256 TEXT NOT NULL, activation TEXT, prepared INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS communication_task_refs(
        authority TEXT NOT NULL, task_id TEXT NOT NULL, reference TEXT NOT NULL,
        PRIMARY KEY(authority,task_id))''',
    '''CREATE TABLE IF NOT EXISTS communication_control_refs(
        authority TEXT NOT NULL, control_id TEXT NOT NULL, task_id TEXT NOT NULL,
        reference TEXT NOT NULL, PRIMARY KEY(authority,control_id))''',
    '''CREATE TABLE IF NOT EXISTS communication_events(
        message_id TEXT PRIMARY KEY, body_sha256 TEXT NOT NULL, kind TEXT NOT NULL,
        required_authority TEXT, target_authority TEXT,
        state TEXT NOT NULL, post_attempted INTEGER NOT NULL DEFAULT 0,
        receipt TEXT, created REAL NOT NULL, updated REAL NOT NULL)''',
)


def message_identity(message):
    raw = message.get('message_id')
    # EXACT original Store.ingest algorithm, including default ASCII escaping.
    return hashlib.sha256(json.dumps([message.get('to_user_id'), raw,
        message if raw is None else None], sort_keys=True).encode()).hexdigest()


def _private_path(path, existing=True):
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts
            or any(parent.is_symlink() for parent in (path,) + tuple(path.parents))):
        raise ValueError('communication_private_path_required')
    parent = path.parent.stat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError('communication_private_directory_required')
    if existing:
        value = path.lstat()
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1):
            raise ValueError('communication_private_file_required')
    elif path.exists():
        raise ValueError('communication_destination_exists')
    return path


class CommunicationStore:
    """Only communication and references, no tasks/claim/session/history API.

    Opening an existing prepared candidate does not activate it, recover an
    unknown or instantiate a business Store. Activation is owner-code only.
    """
    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(_private_path(path)), clock
        with self.transaction() as db:
            row = db.execute('SELECT identity,import_sha256,activation FROM communication_identity').fetchone()
            if row is None:
                raise ValueError('communication_identity_missing')
            self.identity = json.loads(row['identity'])
            if self.identity.get('protocol') != PROTOCOL:
                raise ValueError('communication_identity_invalid')
            self.import_sha256 = row['import_sha256']
        self.notification_policy = {'mode': 'channel'}
        self.notification_node = self.identity['origin_authority']

    @contextlib.contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _meta(db, key, default=None):
        row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(db, key, value):
        if key not in META_KEYS:
            raise ValueError('communication_meta_not_authorized')
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))

    def get(self, key, default=None):
        if key not in META_KEYS:
            raise ValueError('communication_meta_not_authorized')
        with self.transaction() as db:
            return self._meta(db, key, default)

    def set(self, key, value):
        with self.transaction() as db:
            self._set(db, key, value)

    def require_active(self, db=None):
        if db is None:
            with self.transaction() as current:
                return self.require_active(current)
        row = db.execute('SELECT activation,prepared FROM communication_identity WHERE singleton=1').fetchone()
        if row is None or row[0] is None or row[1] != 1:
            raise PermissionError('communication_candidate_not_activated')
        return json.loads(row[0])

    def _control_authority(self, db, text):
        if text.startswith(('/answer ', '/approve ', '/deny ')):
            parts = text.split(' ', 2)
            row = db.execute('SELECT authority FROM communication_control_refs WHERE control_id=?',
                             (parts[1],)).fetchone()
            return row[0] if row else self.identity['origin_authority']
        if text.startswith(('/steer ', '/pause ', '/resume ')):
            target = text.split(' ', 2)[1]
            row = db.execute('SELECT authority FROM communication_task_refs WHERE task_id=?',
                             (target,)).fetchone()
            return row[0] if row else self.identity['origin_authority']
        return None

    def _ingest_in_db(self, db, messages, cursor, owner, bot):
        self.require_active(db)
        if (not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages)
                or not isinstance(cursor, str)):
            raise ValueError('invalid_updates')
        accepted = 0
        for message in messages:
            if (message.get('from_user_id') != owner or message.get('to_user_id') != bot
                    or message.get('group_id') or message.get('message_type') != 1):
                continue
            identity = message_identity(message)
            if db.execute('SELECT 1 FROM inbox WHERE id=?', (identity,)).fetchone():
                continue  # migrated old inbox is dedup, NEVER re-executed
            items = message.get('item_list', [])
            if not isinstance(items, list):
                raise ValueError('invalid_items')
            created = message.get('create_time_ms', 0)
            if type(created) not in (int, float) or not math.isfinite(created):
                raise ValueError('communication_message_time_invalid')
            db.execute('INSERT INTO inbox VALUES(?,?,?)', (identity, json.dumps(message), self.clock()))
            sequence = db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM communication_inbox_order').fetchone()[0]
            db.execute('INSERT INTO communication_inbox_order VALUES(?,?)', (identity, sequence))
            context = message.get('context_token')
            if context:
                previous = self._meta(db, 'owner_context', {})
                if created >= previous.get('created', 0):
                    self._set(db, 'owner_context', {'token': context, 'version': identity, 'created': created})
            if created < self._meta(db, 'activate_after_ms', 0):
                continue
            text = '\n'.join(str(item.get('text_item', {}).get('text', '')) for item in items
                             if isinstance(item, dict) and item.get('type') == 1).strip()
            if text.startswith('ClawBot 自动刷新'):
                continue  # context refresh is not a new executable task
            authority = self._control_authority(db, text)
            kind = 'control' if authority else 'message' if text else 'media'
            now = self.clock()
            db.execute('INSERT INTO communication_events(message_id,body_sha256,kind,required_authority,'
                "state,created,updated) VALUES(?,?,?,?, 'queued',?,?)",
                (identity, digest(message), kind, authority, now, now))
            accepted += 1
        self._set(db, 'cursor', cursor)
        self._set(db, 'last_poll', self.clock())
        return accepted

    def _enqueue(self, db, request_id, text, delivery_route='channel', explicit=False):
        self.require_active(db)
        if (not isinstance(request_id, str) or not 0 < len(request_id) <= 200
                or delivery_route != 'channel' or not isinstance(text, str)
                or not 0 < len(text.encode('utf8')) <= 16000):
            raise ValueError('communication_notification_invalid')
        fingerprint = hashlib.sha256(text.encode('utf8')).hexdigest()
        old = db.execute('SELECT fingerprint FROM outbox WHERE id=?', (request_id,)).fetchone()
        if old is not None:
            if old[0] != fingerprint:
                raise Conflict('request_id_content_conflict')
            return request_id
        db.execute('INSERT INTO outbox(id,fingerprint,body,status,client_id,created,delivery_route) '
            "VALUES(?,?,?,'pending',?,?,'channel')",
            (request_id, fingerprint, text, 'mesh-' + uuid.uuid4().hex, self.clock()))
        return request_id

    def enqueue(self, request_id, text):
        with self.transaction() as db:
            return self._enqueue(db, request_id, text)

    def send_status(self, request_id):
        """Owner-only original outbox lookup, including migrated media/unknown.

        No new relay identity or client ID is attached to a historical row.
        A missing record never authorizes another POST.
        """
        from .remote_notifications import request_id as checked_id
        checked_id(request_id)
        with self.transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise ValueError('send_not_found')
            if (row['delivery_route'] != 'channel'
                    or hashlib.sha256(row['body'].encode('utf8')).hexdigest() != row['fingerprint']):
                raise Conflict('communication_original_send_changed')
            return {'id': row['id'], 'status': row['status'], 'detail': json.loads(row['detail']),
                    'delivery_route': row['delivery_route'], 'delivery_verified': False}

    def inbox(self, after=0, limit=20):
        """Legacy archive cursor is the ORIGINAL ordinal, not this DB's rowid."""
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_page')
        with self.transaction() as db:
            rows = db.execute('SELECT o.sequence,i.body FROM communication_inbox_order o '
                'JOIN inbox i ON i.id=o.message_id WHERE o.sequence>? '
                'ORDER BY o.sequence LIMIT ?', (after, limit)).fetchall()
            output = []
            for row in rows:
                raw = json.loads(row['body'])
                message = {key: raw.get(key) for key in (
                    'message_id', 'from_user_id', 'to_user_id', 'create_time_ms', 'message_type')}
                message['items'] = []
                for item in raw.get('item_list', []):
                    if not isinstance(item, dict):
                        continue
                    message['items'].append({'type': 1, 'text': item.get('text_item', {}).get('text', '')}
                        if item.get('type') == 1 else {'type': item.get('type'), 'content_downloaded': False})
                output.append(message)
            return {'items': output, 'next_cursor': rows[-1]['sequence'] if rows else after,
                    'source': 'mesh_archive', 'desktop_required': False}

    def _event(self, db, message_id):
        row = db.execute('SELECT e.*,i.body FROM communication_events e JOIN inbox i '
                         'ON i.id=e.message_id WHERE e.message_id=?', (message_id,)).fetchone()
        if row is None or digest(json.loads(row['body'])) != row['body_sha256']:
            raise ValueError('communication_original_event_missing_or_changed')
        return dict(row)

    def event(self, message_id, authority=None):
        with self.transaction() as db:
            row = self._event(db, message_id)
            if authority is not None and row['target_authority'] != authority:
                raise PermissionError('communication_event_not_assigned')
            return dict(row, protocol=PROTOCOL, communication_id=self.identity['communication_id'],
                        origin_authority=self.identity['origin_authority'], message=json.loads(row.pop('body')))

    def pending(self, limit=20):
        if type(limit) is not int or not 0 < limit <= 100:
            raise ValueError('communication_limit_invalid')
        with self.transaction() as db:
            return [dict(row) for row in db.execute('SELECT message_id,kind,required_authority,state,'
                'target_authority,post_attempted FROM communication_events '
                "WHERE state!='confirmed' ORDER BY post_attempted,created,message_id LIMIT ?", (limit,))]

    def register_reference(self, authority, task_id, task_row_sha256, control_id=None):
        """Authenticated executor's pointer only; never migrate execution rights."""
        identifier(authority)
        identifier(task_id)
        if not isinstance(task_row_sha256, str) or re.fullmatch('[0-9a-f]{64}', task_row_sha256) is None:
            raise ValueError('communication_reference_digest_invalid')
        if control_id is not None:
            identifier(control_id)
        with self.transaction() as db:
            self.require_active(db)
            conflicts = db.execute('SELECT authority FROM communication_task_refs WHERE task_id=?', (task_id,)).fetchall()
            if any(row[0] != authority for row in conflicts):
                raise Conflict('communication_original_reference_authority_changed')
            reference = {'authority': authority, 'task_id': task_id,
                'task_row_sha256': task_row_sha256, 'runnable_copy': False,
                'evidence': 'authenticated_executor_report_not_execution_attestation'}
            db.execute('INSERT OR IGNORE INTO communication_task_refs VALUES(?,?,?)',
                       (authority, task_id, canonical(reference)))
            if control_id is not None:
                old = db.execute('SELECT authority,task_id FROM communication_control_refs WHERE control_id=?',
                                 (control_id,)).fetchall()
                if any(row[0] != authority or row[1] != task_id for row in old):
                    raise Conflict('communication_original_control_reference_changed')
                db.execute('INSERT OR IGNORE INTO communication_control_refs VALUES(?,?,?,?)',
                           (authority, control_id, task_id, canonical(reference)))
        return {'reference_registered': True, 'runnable_copy': False, 'execution_verified': False}

    def prepare_delivery(self, message_id, authority):
        identifier(authority)
        with self.transaction() as db:
            self.require_active(db)
            row = self._event(db, message_id)
            if row['required_authority'] is not None and row['required_authority'] != authority:
                raise PermissionError('communication_control_stays_original_authority')
            if row['target_authority'] is not None and row['target_authority'] != authority:
                raise Conflict('communication_original_target_changed')
            fresh = row['state'] == 'queued' and not row['post_attempted']
            if fresh:
                db.execute("UPDATE communication_events SET target_authority=?,state='dispatching',"
                    'post_attempted=1,updated=? WHERE message_id=?', (authority, self.clock(), message_id))
            return {'protocol': PROTOCOL, 'message_id': message_id, 'authority': authority,
                    'fresh': fresh, 'replay_authorized': False}

    def record_delivery(self, message_id, authority, receipt):
        required = {'protocol', 'communication_id', 'message_id', 'body_sha256',
                    'execution_authority', 'task_id', 'accepted', 'receipt_sha256'}
        if not isinstance(receipt, dict) or set(receipt) != required:
            raise ValueError('communication_delivery_receipt_invalid')
        expected = dict(receipt)
        sha = expected.pop('receipt_sha256')
        if digest(expected) != sha:
            raise ValueError('communication_delivery_receipt_invalid')
        with self.transaction() as db:
            self.require_active(db)
            row = self._event(db, message_id)
            if (row['target_authority'] != authority or not row['post_attempted']
                    or receipt['protocol'] != RECEIPT_PROTOCOL
                    or receipt['communication_id'] != self.identity['communication_id']
                    or receipt['message_id'] != message_id or receipt['body_sha256'] != row['body_sha256']
                    or receipt['execution_authority'] != authority
                    or receipt['task_id'] not in (None, message_id)
                    or type(receipt['accepted']) is not int or receipt['accepted'] not in (0, 1)):
                raise Conflict('communication_original_receipt_changed')
            if row['receipt'] is not None and row['receipt'] != canonical(receipt):
                raise Conflict('communication_original_receipt_changed')
            db.execute("UPDATE communication_events SET state='confirmed',receipt=?,updated=? "
                       'WHERE message_id=?', (canonical(receipt), self.clock(), message_id))
            if receipt['task_id'] is not None:
                reference = {'authority': authority, 'task_id': receipt['task_id'],
                    'state_at_ack': 'admitted_not_execution_verified',
                    'receipt_sha256': receipt['receipt_sha256'], 'runnable_copy': False}
                old = db.execute('SELECT reference FROM communication_task_refs WHERE authority=? AND task_id=?',
                                 (authority, receipt['task_id'])).fetchone()
                if old is None:
                    db.execute('INSERT INTO communication_task_refs VALUES(?,?,?)',
                               (authority, receipt['task_id'], canonical(reference)))

    def mark_delivery_unknown(self, message_id, authority):
        with self.transaction() as db:
            self.require_active(db)
            row = self._event(db, message_id)
            if row['target_authority'] != authority or not row['post_attempted']:
                raise Conflict('communication_original_target_changed')
            if row['receipt'] is None:
                db.execute("UPDATE communication_events SET state='unknown',updated=? WHERE message_id=?",
                           (self.clock(), message_id))

    def status(self):
        with self.transaction() as db:
            active, prepared = db.execute('SELECT activation IS NOT NULL,prepared FROM communication_identity').fetchone()
            return {'protocol': PROTOCOL, 'communication_id': self.identity['communication_id'],
                'origin_execution_authority': self.identity['origin_authority'], 'activated': bool(active),
                'prepared_complete': prepared == 1,
                'outbox': {row[0]: row[1] for row in db.execute('SELECT status,COUNT(*) FROM outbox GROUP BY status')},
                'events': {row[0]: row[1] for row in db.execute('SELECT state,COUNT(*) FROM communication_events GROUP BY state')},
                'runnable_tasks_or_native_history_copied': False,
                'automatic_partition_promotion': False, 'phone_delivery_verified': False}
