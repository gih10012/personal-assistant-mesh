"""Atomic original-message admission into an explicitly chosen task authority.

No poll/send or native history is moved. Reuse the original Store ingestion
body (minimal extraction patch), so its control/effect/goal rules still apply.
An unknown ACK is reconciled through this SAME receipt, never another POST.
"""
import json

from .communication_store import PROTOCOL, RECEIPT_PROTOCOL, message_identity
from .networking import canonical, digest, identifier
from .store import Conflict


class CommunicationInbox:
    def __init__(self, store, execution_authority, communication_id):
        if not callable(getattr(store, '_ingest_in_db', None)):
            raise ValueError('communication_original_ingest_extraction_required')
        identifier(execution_authority)
        if not isinstance(communication_id, str) or len(communication_id) != 64:
            raise ValueError('communication_identity_invalid')
        self.store, self.authority, self.communication_id = store, execution_authority, communication_id
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS communication_inbox_receipts('
                'communication_id TEXT NOT NULL,message_id TEXT NOT NULL,body_sha256 TEXT NOT NULL,'
                'receipt TEXT NOT NULL,PRIMARY KEY(communication_id,message_id))')

    def receipt(self, message_id):
        with self.store.transaction() as db:
            row = db.execute('SELECT receipt FROM communication_inbox_receipts '
                'WHERE communication_id=? AND message_id=?', (self.communication_id, message_id)).fetchone()
            return json.loads(row[0]) if row else None

    def deliver(self, event):
        """Event must come from the owner's bound broker, not a posted claim."""
        if (not isinstance(event, dict) or event.get('protocol') != PROTOCOL
                or event.get('communication_id') != self.communication_id
                or event.get('target_authority') != self.authority
                or event.get('state') not in ('dispatching', 'unknown')
                or event.get('post_attempted') != 1
                or event.get('required_authority') not in (None, self.authority)
                or not isinstance(event.get('message'), dict)):
            raise PermissionError('communication_original_event_not_assigned')
        message, message_id = event['message'], event['message_id']
        if message_identity(message) != message_id or digest(message) != event['body_sha256']:
            raise ValueError('communication_original_message_changed')
        with self.store.transaction() as db:
            previous = db.execute('SELECT body_sha256,receipt FROM communication_inbox_receipts '
                'WHERE communication_id=? AND message_id=?', (self.communication_id, message_id)).fetchone()
            if previous:
                if previous[0] != event['body_sha256']:
                    raise Conflict('communication_original_message_changed')
                return json.loads(previous[1])  # readonly original durable ACK
            original = db.execute('SELECT body FROM inbox WHERE id=?', (message_id,)).fetchone()
            if original is not None and digest(json.loads(original[0])) != event['body_sha256']:
                raise Conflict('communication_original_message_changed')
            # This cursor is a TASK-authority projection, not a new channel
            # cursor. This task Store must not run another channel receiver.
            accepted = self.store._ingest_in_db(db, [message],
                'communication-projection-' + message_id,
                message.get('from_user_id'), message.get('to_user_id'))
            task = db.execute('SELECT id FROM tasks WHERE id=?', (message_id,)).fetchone()
            receipt = {'protocol': RECEIPT_PROTOCOL, 'communication_id': self.communication_id,
                'message_id': message_id, 'body_sha256': event['body_sha256'],
                'execution_authority': self.authority, 'task_id': task[0] if task else None,
                'accepted': accepted}
            receipt['receipt_sha256'] = digest(receipt)
            db.execute('INSERT INTO communication_inbox_receipts VALUES(?,?,?,?)',
                (self.communication_id, message_id, event['body_sha256'], canonical(receipt)))
            return receipt


def deliver_once(broker_client, inbox, message_id):
    """Only a fresh broker intent may POST; uncertain attempts only read back.

    This owner-side adapter is also usable via native SSH on a task node; no
    new broad HTTP/operator credential or remote task DB clone is required.
    """
    prepared = broker_client.request('/communication/prepare',
        {'message_id': message_id, 'authority': inbox.authority})
    event = broker_client.request('/communication/event', {'message_id': message_id})
    if prepared['fresh']:
        receipt = inbox.deliver(event)
    else:
        receipt = inbox.receipt(message_id)
        if receipt is None:
            return {'state': 'unknown', 'post_replayed': False, 'message_id': message_id}
    broker_client.request('/communication/receipt', {'message_id': message_id, 'receipt': receipt})
    return {'state': 'confirmed', 'post_replayed': False, 'message_id': message_id,
            'task_id': receipt['task_id'], 'execution_verified': False}
