"""Explicit write-scope advancement of ONE recorded original client message.

No tool/route is registered here. Future entrances may expose this helper as a
write operation; ordinary receive/receipt must never call it as a read hook.
The existing bridge remains the sole native intent/receipt and lease boundary.
"""
from .conversation import ConversationService, arguments, digest
from .conversation_runtime import ConversationBridge
from .store import Conflict


def sync_original_message(service, principal, payload):
    """Advance an unadmitted original message, or ONLY read its native receipt.

    A known pending/no-claim result permits a later explicit sync of the SAME
    message. An attempted/unknown native operation can never get another POST.
    """
    value = arguments('receipt', payload)
    bridge = getattr(service, 'runtime', None)
    if not isinstance(service, ConversationService) or not isinstance(bridge, ConversationBridge):
        raise ValueError('conversation_sync_original_bridge_required')
    bridge._principal(principal, write=True)
    binding = {key: value[key] for key in ('conversation_id', 'request_id')}
    with bridge.lock:
        original = service.request(principal, 'receipt', value)
        if not original['found'] or original['message']['direction'] != 'to_mesh':
            raise Conflict('conversation_sync_original_client_message_required')
        previous = bridge.original_receipt(principal, binding, value['message_id'])
        if (previous.get('state') == 'not_admitted_to_native'
                and previous.get('post_attempted') is False):
            identity, task = service._binding(principal, value, write=True)
            fingerprint = digest([value, original['message']])
            with service.transaction() as db:
                # The bridge persists BEFORE its native POST. This extra small
                # marker covers death BEFORE entering the bridge, so absence
                # of a native receipt is never a permit to retry an unknown sync.
                db.execute('''CREATE TABLE IF NOT EXISTS conversation_sync_intents(
                    identity TEXT NOT NULL,message_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,PRIMARY KEY(identity,message_id))''')
                old = db.execute('SELECT * FROM conversation_sync_intents WHERE identity=? AND message_id=?',
                                 (identity, value['message_id'])).fetchone()
                if old is not None and old['fingerprint'] != fingerprint:
                    raise Conflict('conversation_sync_original_message_changed')
                admitted = old is None or old['state'] == 'waiting'
                if old is None:
                    db.execute("INSERT INTO conversation_sync_intents VALUES(?,?,?,'attempted')",
                               (identity, value['message_id'], fingerprint))
                elif admitted:
                    db.execute("UPDATE conversation_sync_intents SET state='attempted' WHERE identity=? AND message_id=?",
                               (identity, value['message_id']))
            if not admitted:
                report = {'found': False, 'state': 'sync_delivery_unknown',
                          'automatic_retry': False, 'original_native_receipt': previous}
            else:
                report = bridge.after_reply(principal, binding, value['message_id'])
                if (report.get('state') == 'awaiting_original_live_task'
                        and report.get('post_attempted') is False):
                    with service.transaction() as db:
                        db.execute("UPDATE conversation_sync_intents SET state='waiting' "
                                   "WHERE identity=? AND message_id=? AND fingerprint=? AND state='attempted'",
                                   (identity, value['message_id'], fingerprint))
        else:
            # Includes a missing/unknown authority ACK. No observation timeout,
            # reconnect or new sync envelope is permission to send it again.
            report = previous
        return dict(original, runtime=report, sync_of_original_message=True)
