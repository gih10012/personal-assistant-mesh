"""Opt-in, single-witness channel fencing; not two-node distributed consensus.

Carriers on different nodes must call ONE authenticated witness. Admission is
once-only and durable before external IO. Expiry does not cancel admitted iLink
requests: unsettled intents prevent a new term, even after a process restart.
There is no default receiver, credential loader, import, or native-tool gate.
"""
import math
import json
import re
import secrets

from .networking import canonical, digest
from .store import Conflict


PROTOCOL = 'mesh-channel-carrier/1'
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_TERMINAL = frozenset(('committed', 'aborted', 'reconciled'))
_TERM_KEYS = frozenset(('protocol', 'witness_id', 'incarnation', 'account_binding',
    'owner_ref', 'canonical_authority', 'holder', 'epoch', 'deadline', 'frozen',
    'checkpoint_revision', 'checkpoint_sha256'))
_TICKET_KEYS = frozenset(('protocol', 'witness_id', 'incarnation', 'account_binding',
    'canonical_authority', 'holder', 'epoch', 'operation_id', 'kind',
    'identity', 'identity_sha256', 'request_sha256', 'dispatch_token'))
_RECEIPT_KEYS = _TICKET_KEYS | frozenset(('state', 'fresh', 'outcome_sha256',
                                        'persistence_sha256'))


class CarrierError(Conflict):
    """Fixed diagnostics only; no transport exceptions/private responses."""


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CarrierError('carrier_identifier_invalid')
    return value


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise CarrierError('carrier_digest_invalid')
    return value


def _integer(value, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CarrierError('carrier_revision_invalid')
    return value


def _duration(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 1 <= value <= 300):
        raise CarrierError('carrier_duration_invalid')
    return value


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise CarrierError('carrier_protocol_invalid')


def _term(term):
    _shape(term, _TERM_KEYS)
    if term['protocol'] != PROTOCOL or not isinstance(term['frozen'], bool):
        raise CarrierError('carrier_protocol_invalid')
    for key in ('witness_id', 'owner_ref', 'canonical_authority', 'holder'):
        _id(term[key])
    for key in ('incarnation', 'account_binding', 'checkpoint_sha256'):
        _sha(term[key])
    _integer(term['epoch'])
    _integer(term['checkpoint_revision'])
    value = term['deadline']
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CarrierError('carrier_protocol_invalid')
    return term


class CarrierWitness:
    """Designated witness ledger, instantiated explicitly by its server only.

    `store.transaction()` supplies one durable SQLite writer boundary. Do not
    construct this on both carriers' independent Stores and call it HA. The
    verifier hooks are trusted deployment code, never client-posted booleans.
    They must verify actual quiescence AND canonical history/effect closure.
    With no verifier installed there is deliberately no grant/reconciliation.
    """
    def __init__(self, store, witness_id, handoff_verifier=None, operation_verifier=None):
        self.store, self.witness_id = store, _id(witness_id)
        self.handoff_verifier = handoff_verifier
        self.operation_verifier = operation_verifier
        with store.transaction() as db:
            statements = (
                '''CREATE TABLE IF NOT EXISTS channel_carrier_witness(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    witness_id TEXT NOT NULL, incarnation TEXT NOT NULL,
                    last_clock REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS channel_carrier_accounts(
                    account_binding TEXT PRIMARY KEY, owner_ref TEXT NOT NULL,
                    canonical_authority TEXT NOT NULL, epoch INTEGER NOT NULL,
                    holder TEXT, deadline REAL NOT NULL, frozen INTEGER NOT NULL,
                    checkpoint_revision INTEGER NOT NULL, checkpoint_sha256 TEXT,
                    proof_sha256 TEXT)''',
                '''CREATE TABLE IF NOT EXISTS channel_carrier_candidates(
                    account_binding TEXT NOT NULL, node TEXT NOT NULL,
                    revision INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    contract TEXT NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(account_binding,node))''',
                '''CREATE TABLE IF NOT EXISTS channel_carrier_intents(
                    account_binding TEXT NOT NULL, operation_id TEXT NOT NULL,
                    canonical_authority TEXT NOT NULL, holder TEXT NOT NULL,
                    epoch INTEGER NOT NULL, kind TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    identity_sha256 TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL, dispatch_token TEXT,
                    outcome_sha256 TEXT, persistence_sha256 TEXT,
                    reconciliation_sha256 TEXT, created REAL NOT NULL,
                    updated REAL NOT NULL, PRIMARY KEY(account_binding,operation_id))''',
                '''CREATE UNIQUE INDEX IF NOT EXISTS channel_carrier_send_identity
                    ON channel_carrier_intents(account_binding,identity_sha256)
                    WHERE kind='send' AND state!='aborted' ''',
            )
            for statement in statements:
                db.execute(statement)
            row = db.execute('SELECT * FROM channel_carrier_witness').fetchone()
            if row is None:
                now = self._clock()
                self.incarnation = secrets.token_hex(32)
                db.execute('INSERT INTO channel_carrier_witness VALUES(1,?,?,?)',
                           (self.witness_id, self.incarnation, now))
            else:
                if row['witness_id'] != self.witness_id:
                    raise CarrierError('carrier_witness_identity_changed')
                self.incarnation = _sha(row['incarnation'])
                self._now(db)

    def _clock(self):
        now = self.store.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise CarrierError('carrier_clock_invalid')
        return now

    def _now(self, db):
        now = self._clock()
        row = db.execute('SELECT * FROM channel_carrier_witness').fetchone()
        if (row is None or row['witness_id'] != self.witness_id
                or row['incarnation'] != self.incarnation):
            raise CarrierError('carrier_witness_identity_changed')
        if now < row['last_clock']:
            raise CarrierError('carrier_clock_regressed')
        db.execute('UPDATE channel_carrier_witness SET last_clock=?', (now,))
        return now

    @staticmethod
    def _account(db, binding):
        _sha(binding)
        row = db.execute('SELECT * FROM channel_carrier_accounts WHERE account_binding=?',
                         (binding,)).fetchone()
        if row is None:
            raise CarrierError('carrier_account_not_enrolled')
        return row

    def _term_receipt(self, row):
        return dict(protocol=PROTOCOL, witness_id=self.witness_id, incarnation=self.incarnation,
            account_binding=row['account_binding'], owner_ref=row['owner_ref'],
            canonical_authority=row['canonical_authority'], holder=row['holder'],
            epoch=row['epoch'], deadline=row['deadline'], frozen=bool(row['frozen']),
            checkpoint_revision=row['checkpoint_revision'], checkpoint_sha256=row['checkpoint_sha256'])

    def enroll(self, account_binding, owner_ref, canonical_authority):
        """Trusted private config binding only; initial account is FROZEN/unowned."""
        _sha(account_binding)
        _id(owner_ref)
        _id(canonical_authority)
        with self.store.transaction() as db:
            self._now(db)
            old = db.execute('SELECT * FROM channel_carrier_accounts WHERE account_binding=?',
                             (account_binding,)).fetchone()
            if old is not None:
                if old['owner_ref'] != owner_ref or old['canonical_authority'] != canonical_authority:
                    raise CarrierError('carrier_account_binding_changed')
                return self._term_receipt(old)
            db.execute('INSERT INTO channel_carrier_accounts VALUES(?,?,?,0,NULL,0,1,0,NULL,NULL)',
                       (account_binding, owner_ref, canonical_authority))
            return self._term_receipt(self._account(db, account_binding))

    def candidate(self, actor, contract, ttl=60):
        """Authenticated node's REPORT, not credential attestation or a grant."""
        _id(actor)
        _duration(ttl)
        _shape(contract, ('protocol', 'node', 'owner_ref', 'account_binding',
            'canonical_authority', 'private_credential_ref', 'revision', 'runtime'))
        runtime = contract['runtime']
        _shape(runtime, ('adapter', 'transport_available', 'private_state', 'fence_protocol'))
        if (contract['protocol'] != PROTOCOL or contract['node'] != actor
                or runtime['adapter'] != 'ilink/0.3.0' or runtime['fence_protocol'] != PROTOCOL
                or type(runtime['transport_available']) is not bool
                or type(runtime['private_state']) is not bool):
            raise CarrierError('carrier_candidate_invalid')
        _id(contract['private_credential_ref'])
        _integer(contract['revision'])
        with self.store.transaction() as db:
            now = self._now(db)
            account = self._account(db, contract['account_binding'])
            if (contract['owner_ref'] != account['owner_ref']
                    or contract['canonical_authority'] != account['canonical_authority']):
                raise CarrierError('carrier_candidate_binding_changed')
            old = db.execute('SELECT * FROM channel_carrier_candidates WHERE account_binding=? AND node=?',
                             (contract['account_binding'], actor)).fetchone()
            fingerprint = digest(contract)
            if old is not None and (contract['revision'] < old['revision']
                    or (contract['revision'] == old['revision'] and fingerprint != old['fingerprint'])):
                raise CarrierError('carrier_candidate_revision_conflict')
            db.execute('INSERT OR REPLACE INTO channel_carrier_candidates VALUES(?,?,?,?,?,?)',
                       (contract['account_binding'], actor, contract['revision'], fingerprint,
                        canonical(contract), now + ttl))
            return {'protocol': PROTOCOL, 'node': actor, 'candidate': True, 'holder_granted': False,
                    'account_binding': contract['account_binding'], 'revision': contract['revision']}

    def inspect(self, account_binding):
        with self.store.transaction() as db:
            self._now(db)
            return self._term_receipt(self._account(db, account_binding))

    def freeze(self, account_binding, expected_epoch):
        """Trusted operator barrier; no service stop and no automatic takeover."""
        _integer(expected_epoch, 0)
        with self.store.transaction() as db:
            now = self._now(db)
            row = self._account(db, account_binding)
            if row['epoch'] != expected_epoch:
                raise CarrierError('carrier_term_conflict')
            db.execute('UPDATE channel_carrier_accounts SET frozen=1 WHERE account_binding=?', (account_binding,))
            # A prepared intent has NEVER received a dispatch permission. A
            # delayed begin ACK cannot dispatch after this barrier. Dispatched
            # intents, including uncertain ACKs, are never erased/auto-aborted.
            db.execute("UPDATE channel_carrier_intents SET state='aborted',updated=? "
                       "WHERE account_binding=? AND state='prepared'", (now, account_binding))
            return self._term_receipt(self._account(db, account_binding))

    @staticmethod
    def _outstanding(db, binding):
        return db.execute("SELECT 1 FROM channel_carrier_intents WHERE account_binding=? "
                          "AND state NOT IN ('committed','aborted','reconciled') LIMIT 1", (binding,)).fetchone()

    def grant(self, account_binding, expected_epoch, holder, seconds, proof_ref):
        """Explicit CAS after trusted external proof; expiry is NOT such proof."""
        _integer(expected_epoch, 0)
        _id(holder)
        _id(proof_ref)
        _duration(seconds)
        if not callable(self.handoff_verifier):
            raise CarrierError('carrier_handoff_verifier_missing')
        # Slow verification runs outside the writer lock. The frozen barrier,
        # CAS and second unsettled-intent check below protect this gap.
        with self.store.transaction() as db:
            self._now(db)
            before = self._term_receipt(self._account(db, account_binding))
            if before['epoch'] != expected_epoch or not before['frozen']:
                raise CarrierError('carrier_handoff_not_frozen')
            if self._outstanding(db, account_binding):
                raise CarrierError('carrier_unsettled_intent')
        try:
            proof = self.handoff_verifier(dict(before), holder, proof_ref)
        except Exception:
            raise CarrierError('carrier_handoff_proof_invalid') from None
        _shape(proof, ('protocol', 'account_binding', 'canonical_authority',
            'from_holder', 'from_epoch', 'to_holder', 'proof_ref', 'checkpoint_revision',
            'checkpoint_sha256', 'migration_ready', 'original_history_closed',
            'original_execution_closed', 'previous_carrier_stopped', 'external_operations_quiescent'))
        if (proof['protocol'] != 'mesh-channel-handoff-proof/1'
                or proof['account_binding'] != account_binding
                or proof['canonical_authority'] != before['canonical_authority']
                or proof['from_holder'] != before['holder'] or proof['from_epoch'] != expected_epoch
                or proof['to_holder'] != holder or proof['proof_ref'] != proof_ref
                or any(proof[key] is not True for key in ('migration_ready', 'original_history_closed',
                    'original_execution_closed', 'previous_carrier_stopped', 'external_operations_quiescent'))):
            raise CarrierError('carrier_handoff_proof_invalid')
        _sha(proof['checkpoint_sha256'])
        _integer(proof['from_epoch'], 0)
        _integer(proof['checkpoint_revision'])
        if proof['checkpoint_revision'] <= before['checkpoint_revision']:
            raise CarrierError('carrier_checkpoint_revision_conflict')
        with self.store.transaction() as db:
            now = self._now(db)
            row = self._account(db, account_binding)
            if self._term_receipt(row) != before:
                raise CarrierError('carrier_term_conflict')
            if self._outstanding(db, account_binding):
                raise CarrierError('carrier_unsettled_intent')
            candidate = db.execute('SELECT * FROM channel_carrier_candidates WHERE account_binding=? AND node=?',
                                   (account_binding, holder)).fetchone()
            if candidate is None or candidate['expires'] <= now:
                raise CarrierError('carrier_candidate_unavailable')
            runtime = json.loads(candidate['contract'])['runtime']
            if not runtime['transport_available'] or not runtime['private_state']:
                raise CarrierError('carrier_candidate_unavailable')
            db.execute('UPDATE channel_carrier_accounts SET epoch=?,holder=?,deadline=?,frozen=0,'
                       'checkpoint_revision=?,checkpoint_sha256=?,proof_sha256=? WHERE account_binding=?',
                       (expected_epoch + 1, holder, now + seconds, proof['checkpoint_revision'],
                        proof['checkpoint_sha256'], digest(proof), account_binding))
            return self._term_receipt(self._account(db, account_binding))

    def _current(self, db, actor, term, admission=False):
        _id(actor)
        _term(term)
        now = self._now(db)
        row = self._account(db, term['account_binding'])
        for key in ('account_binding', 'owner_ref', 'canonical_authority', 'holder', 'epoch',
                    'checkpoint_revision', 'checkpoint_sha256'):
            if term[key] != row[key]:
                raise CarrierError('carrier_term_conflict')
        if (term['witness_id'] != self.witness_id or term['incarnation'] != self.incarnation
                or actor != row['holder']):
            raise CarrierError('carrier_term_conflict')
        if admission and (row['frozen'] or row['deadline'] <= now):
            raise CarrierError('carrier_admission_closed')
        return row, now

    def renew(self, actor, term, seconds=60):
        _duration(seconds)
        with self.store.transaction() as db:
            row, now = self._current(db, actor, term, admission=True)
            candidate = db.execute('SELECT expires,contract FROM channel_carrier_candidates WHERE account_binding=? AND node=?',
                                   (row['account_binding'], actor)).fetchone()
            if candidate is None or candidate['expires'] <= now:
                raise CarrierError('carrier_candidate_unavailable')
            runtime = json.loads(candidate['contract'])['runtime']
            if not runtime['transport_available'] or not runtime['private_state']:
                raise CarrierError('carrier_candidate_unavailable')
            db.execute('UPDATE channel_carrier_accounts SET deadline=? WHERE account_binding=?',
                       (now + seconds, row['account_binding']))
            return self._term_receipt(self._account(db, row['account_binding']))

    def _receipt(self, row, fresh):
        return dict(protocol=PROTOCOL, witness_id=self.witness_id, incarnation=self.incarnation,
            **{key: row[key] for key in _TICKET_KEYS - {'protocol', 'witness_id', 'incarnation', 'identity'}},
            identity=json.loads(row['identity']),
            state=row['state'], fresh=fresh, outcome_sha256=row['outcome_sha256'],
            persistence_sha256=row['persistence_sha256'])

    def begin(self, actor, term, operation_id, kind, identity, request_sha256):
        _id(operation_id)
        _sha(request_sha256)
        if kind == 'send':
            _shape(identity, ('authority', 'outbox_id', 'client_id'))
            _id(identity['outbox_id'])
            _id(identity['client_id'])
        elif kind == 'poll':
            _shape(identity, ('authority', 'cursor_sha256'))
            _sha(identity['cursor_sha256'])
        else:
            raise CarrierError('carrier_operation_kind_invalid')
        _id(identity['authority'])
        with self.store.transaction() as db:
            account, now = self._current(db, actor, term, admission=True)
            binding = account['account_binding']
            if identity['authority'] != account['canonical_authority']:
                raise CarrierError('carrier_canonical_authority_changed')
            identity_sha = digest(identity)
            old = db.execute('SELECT * FROM channel_carrier_intents WHERE account_binding=? AND operation_id=?',
                             (binding, operation_id)).fetchone()
            if old is not None:
                if (old['holder'] != actor or old['epoch'] != account['epoch'] or old['kind'] != kind
                        or old['identity_sha256'] != identity_sha or old['request_sha256'] != request_sha256):
                    raise CarrierError('carrier_operation_identity_changed')
                return self._receipt(old, False)
            if kind == 'send' and db.execute("SELECT 1 FROM channel_carrier_intents WHERE account_binding=? "
                    "AND kind='send' AND identity_sha256=? AND state!='aborted'", (binding, identity_sha)).fetchone():
                raise CarrierError('carrier_original_send_already_recorded')
            # A returned/committing operation still owns the lane. Unknown
            # sends may coexist with DIFFERENT new sends under this SAME term;
            # they remain handoff blockers and their original IDs cannot replay.
            states = ('prepared', 'dispatched', 'returned', 'committing')
            if kind == 'poll':
                states += ('unknown',)
            placeholders = ','.join('?' for _ in states)
            if db.execute('SELECT 1 FROM channel_carrier_intents WHERE account_binding=? AND kind=? '
                          'AND state IN (' + placeholders + ') LIMIT 1', (binding, kind) + states).fetchone():
                raise CarrierError('carrier_lane_busy')
            db.execute('INSERT INTO channel_carrier_intents VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?)',
                       (binding, operation_id, account['canonical_authority'], actor, account['epoch'],
                        kind, canonical(identity), identity_sha, request_sha256, 'prepared', now, now))
            row = db.execute('SELECT * FROM channel_carrier_intents WHERE account_binding=? AND operation_id=?',
                             (binding, operation_id)).fetchone()
            return self._receipt(row, True)

    def _intent(self, db, actor, ticket):
        _shape(ticket, _TICKET_KEYS)
        _id(actor)
        _sha(ticket['account_binding'])
        _id(ticket['operation_id'])
        row = db.execute('SELECT * FROM channel_carrier_intents WHERE account_binding=? AND operation_id=?',
                         (ticket['account_binding'], ticket['operation_id'])).fetchone()
        if row is None or actor != row['holder']:
            raise CarrierError('carrier_intent_not_owned')
        expected = {key: value for key, value in self._receipt(row, False).items() if key in _TICKET_KEYS}
        if ticket != expected:
            raise CarrierError('carrier_ticket_changed')
        return row

    def dispatch(self, actor, ticket):
        with self.store.transaction() as db:
            now = self._now(db)
            row = self._intent(db, actor, ticket)
            account = self._account(db, row['account_binding'])
            if (account['holder'] != actor or account['epoch'] != row['epoch']
                    or account['frozen'] or account['deadline'] <= now):
                raise CarrierError('carrier_admission_closed')
            if row['state'] != 'prepared':
                return self._receipt(row, False)
            db.execute("UPDATE channel_carrier_intents SET state='dispatched',dispatch_token=?,updated=? "
                       'WHERE account_binding=? AND operation_id=?',
                       (secrets.token_hex(32), now, row['account_binding'], row['operation_id']))
            row = db.execute('SELECT * FROM channel_carrier_intents WHERE account_binding=? AND operation_id=?',
                             (row['account_binding'], row['operation_id'])).fetchone()
            return self._receipt(row, True)

    def transition(self, actor, ticket, action, sha256=None):
        """Original holder drains admitted work; never grants another IO call.

        Commit authorization is itself once-only. Expiry/freeze closes NEW IO,
        not this original operation's durable outcome. Its unsettled intent
        excludes a new holder until canonical persistence is acknowledged.
        """
        transitions = {'returned': ('dispatched', 'returned', 'outcome_sha256'),
                       'commit_authorize': ('returned', 'committing', None),
                       'committed': ('committing', 'committed', 'persistence_sha256')}
        if action != 'unknown' and action not in transitions:
            raise CarrierError('carrier_transition_invalid')
        if action in ('returned', 'committed'):
            _sha(sha256)
        elif sha256 is not None:
            raise CarrierError('carrier_protocol_invalid')
        with self.store.transaction() as db:
            now = self._now(db)
            row = self._intent(db, actor, ticket)
            account = self._account(db, row['account_binding'])
            if account['holder'] != actor or account['epoch'] != row['epoch']:
                raise CarrierError('carrier_term_conflict')
            if action == 'unknown':
                if row['state'] in _TERMINAL or row['state'] == 'prepared':
                    raise CarrierError('carrier_transition_invalid')
                target, column = 'unknown', None
                fresh = row['state'] != 'unknown'
            else:
                source, target, column = transitions[action]
                if row['state'] == target:
                    if column is not None and row[column] != sha256:
                        raise CarrierError('carrier_outcome_changed')
                    return self._receipt(row, False)
                if row['state'] != source:
                    raise CarrierError('carrier_transition_invalid')
                fresh = True
            assignment, values = ('', ()) if column is None else (',' + column + '=?', (sha256,))
            db.execute('UPDATE channel_carrier_intents SET state=?,updated=?' + assignment +
                       ' WHERE account_binding=? AND operation_id=?',
                       (target, now) + values + (row['account_binding'], row['operation_id']))
            changed = db.execute('SELECT * FROM channel_carrier_intents WHERE account_binding=? AND operation_id=?',
                                 (row['account_binding'], row['operation_id'])).fetchone()
            return self._receipt(changed, fresh)

    def reconcile(self, account_binding, operation_id, proof_ref):
        """Trusted frozen-only, verified original effect/result; NO external IO."""
        _id(operation_id)
        _id(proof_ref)
        if not callable(self.operation_verifier):
            raise CarrierError('carrier_operation_verifier_missing')
        with self.store.transaction() as db:
            self._now(db)
            account = self._account(db, account_binding)
            if not account['frozen']:
                raise CarrierError('carrier_handoff_not_frozen')
            row = db.execute('SELECT * FROM channel_carrier_intents WHERE account_binding=? AND operation_id=?',
                             (account_binding, operation_id)).fetchone()
            if row is None or row['state'] in _TERMINAL or row['state'] == 'prepared':
                raise CarrierError('carrier_transition_invalid')
            before = dict(row)
            receipt = self._receipt(row, False)
        try:
            proof = self.operation_verifier(dict(receipt), proof_ref)
        except Exception:
            raise CarrierError('carrier_operation_proof_invalid') from None
        _shape(proof, ('protocol', 'ticket_sha256', 'proof_ref', 'external_operation_quiescent',
                      'admitted_caller_quiescent',
                      'original_identity_preserved', 'canonical_persistence_complete', 'persistence_sha256'))
        if (proof['protocol'] != 'mesh-channel-operation-proof/1'
                or proof['ticket_sha256'] != digest({k: receipt[k] for k in _TICKET_KEYS})
                or proof['proof_ref'] != proof_ref
                or any(proof[key] is not True for key in ('external_operation_quiescent',
                    'admitted_caller_quiescent',
                    'original_identity_preserved', 'canonical_persistence_complete'))):
            raise CarrierError('carrier_operation_proof_invalid')
        _sha(proof['persistence_sha256'])
        with self.store.transaction() as db:
            now = self._now(db)
            account = self._account(db, account_binding)
            row = db.execute('SELECT * FROM channel_carrier_intents WHERE account_binding=? AND operation_id=?',
                             (account_binding, operation_id)).fetchone()
            if not account['frozen'] or row is None or dict(row) != before:
                raise CarrierError('carrier_reconciliation_conflict')
            db.execute("UPDATE channel_carrier_intents SET state='reconciled',persistence_sha256=?,"
                       'reconciliation_sha256=?,updated=? WHERE account_binding=? AND operation_id=?',
                       (proof['persistence_sha256'], digest(proof), now, account_binding, operation_id))
            changed = db.execute('SELECT * FROM channel_carrier_intents WHERE account_binding=? AND operation_id=?',
                                 (account_binding, operation_id)).fetchone()
            return self._receipt(changed, True)

    def request(self, actor, action, payload):
        """Thin authenticated API integration point; actor comes from server auth.

        Enrollment/freeze/grant/reconciliation are deliberately not remotely
        available here. Root supplies separate authorized operator workflows.
        """
        _id(actor)
        if action == 'candidate':
            _shape(payload, ('contract', 'ttl'))
            return self.candidate(actor, **payload)
        if action == 'renew':
            _shape(payload, ('term', 'seconds'))
            return self.renew(actor, **payload)
        if action == 'begin':
            _shape(payload, ('term', 'operation_id', 'kind', 'identity', 'request_sha256'))
            return self.begin(actor, **payload)
        if action == 'dispatch':
            _shape(payload, ('ticket',))
            return self.dispatch(actor, **payload)
        if action in ('returned', 'commit_authorize', 'committed', 'unknown'):
            keys = ('ticket', 'sha256') if action in ('returned', 'committed') else ('ticket',)
            _shape(payload, keys)
            return self.transition(actor, action=action, **payload)
        raise CarrierError('carrier_route_not_authorized')


class CarrierFence:
    """Actual callback gate usable by Channel through a node-bound RPC client.

    Client.request(path, body) must reach the configured witness, authenticated
    as the term's holder. No cached permission, auto-grant, or IO retry. `call`
    returns a JSON outcome; `persist` atomically records the original canonical
    inbox/outbox/identity and returns its durable receipt SHA. Known business
    rejections should be outcomes, not exceptions. An exception is unknown.
    """
    def __init__(self, client, term):
        self.term = dict(_term(term))
        if not callable(getattr(client, 'request', None)):
            raise CarrierError('carrier_client_invalid')
        self.client = client

    def _rpc(self, action, payload):
        try:
            return self.client.request('/channel-carrier/' + action, payload)
        except Exception:
            raise CarrierError('carrier_witness_unavailable') from None

    @staticmethod
    def _ticket(receipt):
        return {key: receipt[key] for key in _TICKET_KEYS}

    def _check(self, receipt, expected, state, token_change=False):
        _shape(receipt, _RECEIPT_KEYS)
        if type(receipt['fresh']) is not bool or receipt['state'] != state:
            raise CarrierError('carrier_receipt_invalid')
        for key, value in expected.items():
            if key != 'dispatch_token' or not token_change:
                if receipt[key] != value:
                    raise CarrierError('carrier_receipt_invalid')
        if receipt['dispatch_token'] is not None:
            _sha(receipt['dispatch_token'])
        if token_change and receipt['dispatch_token'] is None:
            raise CarrierError('carrier_receipt_invalid')
        if digest(receipt['identity']) != receipt['identity_sha256']:
            raise CarrierError('carrier_receipt_invalid')
        for key in ('outcome_sha256', 'persistence_sha256'):
            if receipt[key] is not None:
                _sha(receipt[key])
        if not receipt['fresh']:
            raise CarrierError('carrier_operation_already_recorded')
        return self._ticket(receipt)

    def _unknown(self, ticket):
        try:
            self._rpc('unknown', {'ticket': ticket})
        except CarrierError:
            pass  # The original dispatched/returned/committing intent remains.

    def run(self, operation_id, kind, identity, request_sha256, call, persist):
        if not callable(call) or not callable(persist):
            raise CarrierError('carrier_callback_invalid')
        expected = {key: self.term[key] for key in ('protocol', 'witness_id', 'incarnation',
                    'account_binding', 'canonical_authority', 'holder', 'epoch')}
        expected.update(operation_id=_id(operation_id), kind=kind, identity=json.loads(canonical(identity)),
                        identity_sha256=digest(identity),
                        request_sha256=_sha(request_sha256), dispatch_token=None)
        receipt = self._rpc('begin', {'term': self.term, 'operation_id': operation_id, 'kind': kind,
                                    'identity': identity, 'request_sha256': request_sha256})
        ticket = self._check(receipt, expected, 'prepared')
        receipt = self._rpc('dispatch', {'ticket': ticket})
        ticket = self._check(receipt, ticket, 'dispatched', token_change=True)
        # A pause/ACK loss after admission cannot cause a second carrier to
        # dispatch: this exact unresolved intent continues to block grant.
        try:
            result = call()
            encoded = canonical(result).encode('utf8')
            if len(encoded) > 8 * 1024 * 1024:
                raise CarrierError('carrier_result_invalid')
            outcome = digest(result)
        except Exception:
            self._unknown(ticket)
            raise CarrierError('carrier_transport_unknown') from None
        try:
            receipt = self._rpc('returned', {'ticket': ticket, 'sha256': outcome})
            self._check(receipt, ticket, 'returned')
            if receipt['outcome_sha256'] != outcome:
                raise CarrierError('carrier_receipt_invalid')
            receipt = self._rpc('commit_authorize', {'ticket': ticket})
            self._check(receipt, ticket, 'committing')
            if receipt['outcome_sha256'] != outcome:
                raise CarrierError('carrier_receipt_invalid')
            persistence = _sha(persist(result, json.loads(canonical(ticket))))
            receipt = self._rpc('committed', {'ticket': ticket, 'sha256': persistence})
            self._check(receipt, ticket, 'committed')
            if receipt['persistence_sha256'] != persistence or receipt['outcome_sha256'] != outcome:
                raise CarrierError('carrier_receipt_invalid')
        except Exception:
            self._unknown(ticket)
            raise CarrierError('carrier_persistence_unconfirmed') from None
        return result
