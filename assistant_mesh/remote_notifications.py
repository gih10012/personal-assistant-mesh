"""Credential-bound text relay to the channel host's already configured owner.

This is a Mesh capability, not a native network/Shell permission gate. It never
accepts a recipient, channel credential, or claimed actor from a message body.
Receiving a notification and its channel outbox row is one writer transaction;
repeating either POST only inspects the original row, including unknown sends.
"""
import hashlib
import re
import secrets

from .networking import digest, identifier
from .store import Conflict


PROTOCOL = 'mesh-notify/1'
_REQUEST_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$')
_FINGERPRINT = re.compile(r'^[0-9a-f]{64}$')
_STATES = frozenset(('pending', 'submitting', 'accepted', 'rejected',
                     'unknown', 'waiting_auth'))
_MISSING = object()


def request_id(value):
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise ValueError('invalid_notification_request_id')
    return value


def fingerprint(value):
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise ValueError('invalid_notification_fingerprint')
    return value


def text_fingerprint(text):
    if not isinstance(text, str) or not text:
        raise ValueError('invalid_notification_text')
    try:
        encoded = text.encode('utf8')
    except UnicodeEncodeError:
        raise ValueError('invalid_notification_text') from None
    if len(encoded) > 16000:
        raise ValueError('invalid_notification_text')
    return hashlib.sha256(encoded).hexdigest()


def receipt_id(source_node, identity):
    """Same deterministic opaque outbox identity on the sender and receiver."""
    identifier(source_node)
    request_id(identity)
    # identifier permits deployment Unicode names, but invalid UTF-8 must never
    # reach the ledger or produce a different canonical identity on a peer.
    try:
        return 'mesh-notify-' + digest([PROTOCOL, source_node, identity])
    except UnicodeEncodeError:
        raise ValueError('invalid_notification_source_node') from None


class RemoteNotifications:
    def __init__(self, store, authority, route_id=None):
        self.store, self.authority = store, identifier(authority)
        # The optional constructor input is the verified account binding, not
        # the effective receiver route returned to clients. Include a durable
        # ledger incarnation so replacing a database with the SAME account is
        # also visible after a client lost its first submission acknowledgement.
        binding = fingerprint(route_id) if route_id is not None else None
        self.route_id = None
        # One static statement inside the Store transaction, never executescript
        # (which would implicitly commit and split schema migration/startup).
        with store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS mesh_notify_receipts(
                source_node TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, outbox_id TEXT NOT NULL,
                created REAL NOT NULL, PRIMARY KEY(source_node,request_id))''')
            if binding is not None:
                previous = self.store._meta(db, 'owner_notification_channel_binding', _MISSING)
                if previous is not _MISSING and previous != binding:
                    raise Conflict('owner_notification_channel_binding_conflict')
                try:
                    incarnation = self.store._meta(db, 'owner_notification_receiver_incarnation', _MISSING)
                    if incarnation is not _MISSING:
                        fingerprint(incarnation)
                except (ValueError, TypeError):
                    raise Conflict('owner_notification_incarnation_invalid') from None
                if incarnation is _MISSING:
                    incarnation = fingerprint(secrets.token_hex(32))
                    self.store._set(db, 'owner_notification_receiver_incarnation', incarnation)
                if previous is _MISSING:
                    self.store._set(db, 'owner_notification_channel_binding', binding)
                self.route_id = digest([PROTOCOL, binding, incarnation])
        # Restoring an old snapshot that preserves this incarnation but loses
        # receipts is an owner recovery operation, NOT automatically detectable
        # rollback or permission to re-send. No arbitrary rollback guarantee.

    @staticmethod
    def _payload(payload, submit):
        required = {'request_id', 'fingerprint', 'text', 'route_id'} if submit else {'request_id', 'fingerprint'}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError('invalid_notification_request')
        identity, expected = request_id(payload['request_id']), fingerprint(payload['fingerprint'])
        if submit:
            fingerprint(payload['route_id'])
            if text_fingerprint(payload['text']) != expected:
                raise ValueError('notification_text_fingerprint_mismatch')
        return identity, expected

    def _response(self, source_node, identity, expected, outbox_id, row=None):
        return {'protocol': PROTOCOL, 'authority': self.authority,
                'route_id': self.route_id,
                'source_node': source_node, 'request_id': identity,
                'fingerprint': expected, 'outbox_id': outbox_id,
                'found': row is not None,
                'status': row['status'] if row is not None else 'not_found',
                'idempotent_receive': True, 'delivery_verified': False}

    @staticmethod
    def _outbox(db, identity, expected):
        row = db.execute('SELECT * FROM outbox WHERE id=?', (identity,)).fetchone()
        if (row is None or row['id'] != identity or row['fingerprint'] != expected
                or row['media_items'] is not None or row['delivery_route'] != 'channel'
                or row['status'] not in _STATES):
            raise Conflict('notification_receipt_outbox_mismatch')
        # An unchanged stored fingerprint alone does not attest unchanged text.
        try:
            intact = text_fingerprint(row['body']) == expected
        except ValueError:
            intact = False
        if not intact:
            raise Conflict('notification_receipt_outbox_mismatch')
        return row

    def receive(self, source_node, payload):
        if self.route_id is None:
            raise PermissionError('owner_notification_channel_unavailable')
        identity, expected = self._payload(payload, True)
        # STATUS proof alone is not enough: a server/ledger could be replaced
        # between the query and this POST. Reject a changed owner route BEFORE
        # enqueue, rather than discover the retargeting from a late reply.
        if payload['route_id'] != self.route_id:
            raise Conflict('notification_channel_binding_mismatch')
        outbox_id = receipt_id(source_node, identity)
        with self.store.transaction() as db:
            receipt = db.execute('SELECT * FROM mesh_notify_receipts WHERE source_node=? AND request_id=?',
                                 (source_node, identity)).fetchone()
            if receipt:
                if receipt['fingerprint'] != expected or receipt['outbox_id'] != outbox_id:
                    raise Conflict('notification_request_content_conflict')
            else:
                # Even a matching unreceipted legacy row is not proof of this
                # authenticated submission. Never attach to it or alter it.
                if db.execute('SELECT 1 FROM outbox WHERE id=?', (outbox_id,)).fetchone():
                    raise Conflict('notification_outbox_collision')
                self.store._enqueue(db, outbox_id, payload['text'], delivery_route='channel')
                db.execute('INSERT INTO mesh_notify_receipts VALUES(?,?,?,?,?)',
                           (source_node, identity, expected, outbox_id, self.store.clock()))
            row = self._outbox(db, outbox_id, expected)
            return self._response(source_node, identity, expected, outbox_id, row)

    def status(self, source_node, payload):
        if self.route_id is None:
            raise PermissionError('owner_notification_channel_unavailable')
        identity, expected = self._payload(payload, False)
        outbox_id = receipt_id(source_node, identity)
        with self.store.transaction() as db:
            receipt = db.execute('SELECT * FROM mesh_notify_receipts WHERE source_node=? AND request_id=?',
                                 (source_node, identity)).fetchone()
            if receipt is None:
                return self._response(source_node, identity, expected, outbox_id)
            if receipt['fingerprint'] != expected or receipt['outbox_id'] != outbox_id:
                raise Conflict('notification_request_content_conflict')
            row = self._outbox(db, outbox_id, expected)
            return self._response(source_node, identity, expected, outbox_id, row)
