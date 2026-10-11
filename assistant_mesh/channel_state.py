"""Original-authority channel state, never a carrier's independent inbox DB.

Opt-in facade: receipts, private returned batches and original send identities
share the authority's SQLite transaction. No credential loading or transport IO.
An unresolved attempt is a guard, not permission to create a replacement ID.
"""
import json
import uuid

from .channel_carrier import CarrierError, _TICKET_KEYS, _id
from .networking import canonical, digest


class ChannelState:
    def __init__(self, store, witness, account=None):
        if witness.store.path != store.path:
            raise CarrierError('channel_canonical_witness_mismatch')
        self.store, self.witness = store, witness
        # Production authority supplies the already validated account snapshot.
        # Carrier-posted user/bot values cannot retarget canonical ingestion.
        self.account = dict(account) if account is not None else None
        with store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS channel_canonical_intents(
                operation_id TEXT PRIMARY KEY, account_binding TEXT NOT NULL,
                holder TEXT NOT NULL, epoch INTEGER NOT NULL, kind TEXT NOT NULL,
                term TEXT NOT NULL, identity TEXT NOT NULL, snapshot TEXT NOT NULL,
                state TEXT NOT NULL, ticket TEXT, outcome TEXT, outcome_sha256 TEXT,
                persistence_sha256 TEXT, accepted INTEGER, uncertain INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL)''')

    def get(self, key, default=None):
        if key not in ('cursor', 'owner_context', 'owner_notification_channel_binding', 'channel_error'):
            raise CarrierError('channel_state_key_not_authorized')
        return self.store.get(key, default)

    def set(self, key, value):
        if key != 'channel_error':
            raise CarrierError('channel_state_key_not_authorized')
        if value is not None and (not isinstance(value, dict) or set(value) != {'code', 'at'}
                or value['code'] not in ('channel_fence_blocked', 'channel_fence_unconfirmed',
                    'network_timeout', 'network_error', 'poll_failed', 'auth_required',
                    'transport_failed', 'response_too_large', 'invalid_response',
                    'redirect_refused', 'business_-2', 'channel_fence_pending')):
            raise CarrierError('channel_error_invalid')
        self.store.set(key, value)

    @staticmethod
    def _row(db, operation_id):
        _id(operation_id)
        row = db.execute('SELECT * FROM channel_canonical_intents WHERE operation_id=?',
                         (operation_id,)).fetchone()
        if row is None:
            raise CarrierError('channel_original_intent_missing')
        return row

    def _prepare(self, db, term, kind, identity, snapshot, now):
        binding = term['account_binding']
        # Unknown sends may coexist with DIFFERENT new sends in the same term,
        # but an unresolved pull retains the cursor lane across process death.
        lane = db.execute("SELECT 1 FROM channel_canonical_intents WHERE account_binding=? "
                          "AND kind=? AND persistence_sha256 IS NULL LIMIT 1", (binding, kind)).fetchone()
        if lane is not None:
            raise CarrierError('channel_original_intent_unsettled')
        if kind == 'poll' and db.execute("SELECT 1 FROM channel_canonical_intents c "
                "WHERE c.account_binding=? AND c.kind='poll' AND c.uncertain=1 "
                "AND NOT EXISTS (SELECT 1 FROM channel_carrier_intents w "
                "WHERE w.account_binding=c.account_binding AND w.operation_id=c.operation_id "
                "AND w.state='reconciled' AND w.persistence_sha256=c.persistence_sha256) LIMIT 1",
                (binding,)).fetchone():
            raise CarrierError('channel_original_intent_unsettled')
        operation_id = 'channel-' + uuid.uuid4().hex
        db.execute('INSERT INTO channel_canonical_intents '
            '(operation_id,account_binding,holder,epoch,kind,term,identity,snapshot,state,created,updated) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (operation_id, binding, term['holder'], term['epoch'], kind,
                canonical(term), canonical(identity), canonical(snapshot), 'prepared', now, now))
        return operation_id

    def channel_prepare_poll(self, term, cursor):
        with self.store.transaction() as db:
            account, now = self.witness._current(db, term['holder'], term, admission=True)
            if not isinstance(cursor, str) or cursor != self.store._meta(db, 'cursor'):
                raise CarrierError('channel_original_cursor_changed')
            operation = self._prepare(db, term, 'poll',
                {'authority': account['canonical_authority'], 'cursor_sha256': digest(cursor)},
                {'cursor': cursor}, now)
            return {'operation_id': operation}

    def channel_prepare_send(self, term):
        with self.store.transaction() as db:
            account, now = self.witness._current(db, term['holder'], term, admission=True)
            context = self.store._meta(db, 'owner_context', {})
            row = db.execute("SELECT * FROM outbox WHERE delivery_route='channel' AND "
                "(status='pending' OR (status='rejected' AND retry_count<1 AND COALESCE(context_version,'')!=?)) "
                'ORDER BY created LIMIT 1', (context.get('version', ''),)).fetchone()
            if row is None:
                return None
            result = dict(row)
            # An explicit server rejection under a newer owner context is the
            # existing bounded retry policy, not an unknown outcome replay.
            result['client_id'] = 'mesh-' + uuid.uuid4().hex if row['status'] == 'rejected' else row['client_id']
            result['context_token'] = context.get('token')
            result['context_version'] = context.get('version', '')
            identity = {'authority': account['canonical_authority'], 'outbox_id': row['id'],
                        'client_id': result['client_id']}
            operation = self._prepare(db, term, 'send', identity, result, now)
            db.execute("UPDATE outbox SET status='submitting',client_id=?,context_version=?,"
                'retry_count=retry_count+? WHERE id=?', (result['client_id'], result['context_version'],
                    1 if row['status'] == 'rejected' else 0, row['id']))
            return {'operation_id': operation, 'row': result}

    def _ticket(self, db, row, ticket, state):
        witness = self.witness._intent(db, row['holder'], ticket)
        account = self.witness._account(db, row['account_binding'])
        if (set(ticket) != set(_TICKET_KEYS) or witness['state'] != state
                or row['operation_id'] != ticket['operation_id']
                or row['account_binding'] != ticket['account_binding']
                or row['epoch'] != ticket['epoch'] or row['holder'] != ticket['holder']
                or row['kind'] != ticket['kind'] or json.loads(row['identity']) != ticket['identity']
                or account['epoch'] != row['epoch'] or account['holder'] != row['holder']):
            raise CarrierError('channel_original_ticket_changed')
        return witness

    def channel_mark_dispatched(self, operation_id, ticket):
        with self.store.transaction() as db:
            row = self._row(db, operation_id)
            self._ticket(db, row, ticket, 'dispatched')
            if row['state'] != 'prepared' or row['ticket'] is not None:
                raise CarrierError('channel_original_dispatch_already_recorded')
            db.execute("UPDATE channel_canonical_intents SET state='dispatched',ticket=?,updated=? "
                       'WHERE operation_id=?', (canonical(ticket), self.store.clock(), operation_id))

    def channel_record_result(self, operation_id, outcome):
        encoded = canonical(outcome)
        if len(encoded.encode('utf8')) > 8 * 1024 * 1024:
            raise CarrierError('channel_original_result_too_large')
        self._outcome(outcome)
        with self.store.transaction() as db:
            row = self._row(db, operation_id)
            if row['state'] != 'dispatched' or row['outcome'] is not None:
                raise CarrierError('channel_original_result_already_recorded')
            db.execute("UPDATE channel_canonical_intents SET state='returned',outcome=?,outcome_sha256=?,"
                       'updated=? WHERE operation_id=?',
                       (encoded, digest(outcome), self.store.clock(), operation_id))

    @staticmethod
    def _outcome(outcome):
        if not isinstance(outcome, dict) or outcome.get('status') not in (
                'updates', 'accepted', 'rejected', 'waiting_auth', 'unknown'):
            raise CarrierError('channel_original_result_invalid')
        if outcome['status'] == 'updates':
            if set(outcome) != {'status', 'batch'} or not isinstance(outcome['batch'], dict):
                raise CarrierError('channel_original_result_invalid')
        else:
            expected = {'status', 'detail', 'response'} if outcome['status'] == 'accepted' else {'status', 'detail'}
            if set(outcome) != expected or not isinstance(outcome['detail'], dict):
                raise CarrierError('channel_original_result_invalid')

    def _commit(self, db, operation_id, ticket, kind, outcome):
        row = self._row(db, operation_id)
        if row['kind'] != kind or row['ticket'] != canonical(ticket):
            raise CarrierError('channel_original_ticket_changed')
        self._outcome(outcome)
        if row['outcome_sha256'] != digest(outcome) or row['outcome'] != canonical(outcome):
            raise CarrierError('channel_original_outcome_changed')
        witness = self.witness._intent(db, row['holder'], ticket)
        # Original durable receipt is readable after a lost write ACK; this
        # branch never ingests, sends, or mutates canonical state again.
        if row['persistence_sha256'] is not None:
            if witness['outcome_sha256'] != row['outcome_sha256']:
                raise CarrierError('channel_original_outcome_changed')
            return row, True
        witness = self._ticket(db, row, ticket, 'committing')
        if witness['outcome_sha256'] != row['outcome_sha256'] or row['state'] != 'returned':
            raise CarrierError('channel_original_commit_not_authorized')
        return row, False

    def _receipt(self, db, row, ticket, accepted):
        receipt = digest({'protocol': 'mesh-channel-canonical-receipt/1',
            'operation_id': row['operation_id'], 'ticket_sha256': digest(ticket),
            'outcome_sha256': row['outcome_sha256'], 'accepted': accepted,
            'identity_sha256': digest(json.loads(row['identity']))})
        uncertain = json.loads(row['outcome'])['status'] == 'unknown'
        db.execute("UPDATE channel_canonical_intents SET state='persisted',persistence_sha256=?,"
            'accepted=?,uncertain=?,updated=? WHERE operation_id=?',
            (receipt, accepted, int(uncertain), self.store.clock(), row['operation_id']))
        return receipt

    def channel_commit_poll(self, operation_id, ticket, outcome, owner, bot):
        if self.account is not None and (owner != self.account['ilink_user_id'] or bot != self.account['ilink_bot_id']):
            raise CarrierError('channel_original_owner_changed')
        with self.store.transaction() as db:
            row, persisted = self._commit(db, operation_id, ticket, 'poll', outcome)
            if persisted:
                return {'accepted': row['accepted'], 'sha256': row['persistence_sha256']}
            if self.store._meta(db, 'cursor') != json.loads(row['snapshot'])['cursor']:
                raise CarrierError('channel_original_cursor_changed')
            accepted = 0
            if outcome['status'] == 'updates':
                batch = outcome['batch']
                accepted = self.store._ingest_in_db(db, batch.get('msgs'), batch.get('get_updates_buf'), owner, bot)
                self.store._set(db, 'channel_error', None)
            else:
                self.store._set(db, 'channel_error', {'code': outcome['detail'].get('code', 'poll_failed'),
                                                     'at': self.store.clock()})
            receipt = self._receipt(db, row, ticket, accepted)
            return {'accepted': accepted, 'sha256': receipt}

    def channel_commit_send(self, operation_id, ticket, status, detail):
        if status not in ('accepted', 'rejected', 'unknown', 'waiting_auth'):
            raise CarrierError('channel_original_send_status_invalid')
        with self.store.transaction() as db:
            original = self._row(db, operation_id)
            outcome = json.loads(original['outcome']) if original['outcome'] else None
            if outcome is None or outcome['status'] != status or outcome['detail'] != detail:
                raise CarrierError('channel_original_outcome_changed')
            row, persisted = self._commit(db, operation_id, ticket, 'send', outcome)
            if persisted:
                return row['persistence_sha256']
            snapshot = json.loads(row['snapshot'])
            current = db.execute('SELECT * FROM outbox WHERE id=?', (snapshot['id'],)).fetchone()
            if (current is None or current['client_id'] != snapshot['client_id']
                    or current['fingerprint'] != snapshot['fingerprint'] or current['body'] != snapshot['body']
                    or current['media_items'] != snapshot['media_items']
                    or current['delivery_route'] != snapshot['delivery_route']
                    or current['context_version'] != snapshot['context_version']
                    or current['status'] not in ('submitting', 'unknown')):
                raise CarrierError('channel_original_outbox_changed')
            db.execute('UPDATE outbox SET status=?,detail=? WHERE id=?',
                       (status, canonical(detail), snapshot['id']))
            return self._receipt(db, row, ticket, 0)

    def channel_mark_uncertain(self, operation_id):
        with self.store.transaction() as db:
            row = self._row(db, operation_id)
            # Do not downgrade accepted/persisted results on a lost final ACK.
            if row['persistence_sha256'] is None:
                db.execute('UPDATE channel_canonical_intents SET uncertain=1,updated=? WHERE operation_id=?',
                           (self.store.clock(), operation_id))
                if row['kind'] == 'send':
                    snapshot = json.loads(row['snapshot'])
                    db.execute("UPDATE outbox SET status='unknown',detail=? WHERE id=? AND client_id=? "
                        "AND status='submitting'", (canonical({'code': 'channel_fence_unconfirmed',
                            'delivery_verified': False}), snapshot['id'], snapshot['client_id']))

    def receipt(self, operation_id):
        with self.store.transaction() as db:
            row = self._row(db, operation_id)
            return {key: row[key] for key in ('operation_id', 'account_binding', 'holder', 'epoch',
                'kind', 'state', 'outcome_sha256', 'persistence_sha256', 'accepted', 'uncertain')}
