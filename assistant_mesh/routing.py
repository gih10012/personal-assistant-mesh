"""Task-fenced, immutable model routing proposals and read-only effect links.

These are coordination records, not execution permits, verified observations,
resource admissions or a host-wide native-tool policy. Deployment callers MUST
bind authority/node/task lease independently of model input. A model may name
an existing execution reference; this ledger verifies its task identity, not its
alignment with the model's plan or actual external effects. Nothing here starts,
replays, settles or releases an operation, or accesses a credential or network.
"""
import hashlib
import json
import sqlite3

from .resources import _epoch, _finite, _name, _safe_metadata
from .store import Conflict


MAX_BODY_BYTES = 65536
MAX_CANDIDATES = 64
MAX_EVIDENCE_REFS = 128
MAX_LIST = 100
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_FIELDS = frozenset(('decision_id', 'work', 'candidates', 'selection',
                     'rationale', 'evidence_refs'))
_FLAGS = {'proposal_only': True, 'managed_invocation_authorized': False,
          'capacity_reserved': False, 'execution_verified': False,
          'plan_alignment_verified': False, 'model_selection_verified': False,
          'native_tools_intercepted': False,
          'task_or_operation_replayed': False}


def _canonical(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError('invalid_routing_decision') from None
    if len(encoded.encode('utf8')) > MAX_BODY_BYTES:
        raise ValueError('routing_decision_too_large')
    return encoded


def _sha(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def _decision(value):
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError('invalid_routing_decision_fields')
    _name(value['decision_id'], 'routing_decision_id')
    for field in ('work', 'selection'):
        if not isinstance(value[field], dict) or not value[field]:
            raise ValueError('invalid_routing_' + field)
    for field, maximum in (('candidates', MAX_CANDIDATES), ('evidence_refs', MAX_EVIDENCE_REFS)):
        if (not isinstance(value[field], list) or len(value[field]) > maximum
                or not all(isinstance(item, dict) for item in value[field])):
            raise ValueError('invalid_routing_' + field)
    if not isinstance(value['rationale'], str):
        raise ValueError('invalid_routing_rationale')
    try:
        if len(value['rationale'].encode('utf8')) > 8000:
            raise ValueError('routing_rationale_too_large')
        encoded = _canonical(value)
        # ASCII escapes must not admit lone surrogates which the actual UTF-8
        # HTTP response cannot encode, including nested values/dictionary keys.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf8')
        safe, redacted = _safe_metadata(value)
    except (UnicodeError, RecursionError):
        raise ValueError('invalid_routing_decision') from None
    # Redacting an immutable work/evidence contract would change its meaning.
    # Free-form text is still declared data, not perfectly classified DLP.
    if redacted or safe != value:
        raise ValueError('private_routing_metadata_not_allowed')
    return json.loads(encoded), encoded


class Routing:
    """Use one initialized Store and its task/Leader clock and transactions."""

    def __init__(self, store, authority):
        self.store, self.authority = store, _name(authority, 'routing_authority')
        with store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS route_decisions(
                decision_id TEXT PRIMARY KEY, authority TEXT NOT NULL,
                task_id TEXT NOT NULL, original_node TEXT NOT NULL,
                original_epoch INTEGER NOT NULL, original_leader_epoch INTEGER,
                body TEXT NOT NULL, fingerprint TEXT NOT NULL, created REAL NOT NULL,
                link_kind TEXT, link_reference TEXT)''')
            db.execute('''CREATE INDEX IF NOT EXISTS route_decision_task
                ON route_decisions(task_id,created,decision_id)''')
            if db.execute('SELECT 1 FROM route_decisions WHERE authority<>? LIMIT 1',
                          (self.authority,)).fetchone():
                raise Conflict('routing_authority_mismatch')

    def _task(self, db, task_id, node, epoch):
        _name(task_id, 'routing_task_id')
        _name(node, 'routing_node')
        _epoch(epoch)
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not self.store._task_is_live(db, task, node=node, epoch=epoch):
            raise Conflict('stale_task_lease')
        return task

    def _find(self, db, task_id, decision_id):
        _name(decision_id, 'routing_decision_id')
        row = db.execute('SELECT * FROM route_decisions WHERE decision_id=?',
                         (decision_id,)).fetchone()
        if not row:
            raise ValueError('routing_decision_not_found')
        if row['authority'] != self.authority:
            raise Conflict('routing_authority_mismatch')
        if row['task_id'] != task_id:
            raise PermissionError('routing_decision_task_mismatch')
        return row

    @staticmethod
    def _table(db, table):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (table,)).fetchone():
            raise ValueError('routing_reference_ledger_unavailable')

    def _reference(self, db, decision, kind, reference):
        _name(reference, 'routing_reference_id')
        try:
            if kind == 'remote_delegation':
                self._table(db, 'remote_delegations')
                row = db.execute('''SELECT child_id,parent_id,caller_node,peer,message_id,
                    remote_task_id,remote_status,terminal,observed FROM remote_delegations
                    WHERE child_id=?''', (reference,)).fetchone()
                if not row:
                    raise ValueError('routing_reference_not_found')
                if row['parent_id'] != decision['task_id']:
                    raise PermissionError('routing_reference_task_mismatch')
                if row['caller_node'] != decision['original_node']:
                    raise PermissionError('routing_reference_principal_mismatch')
                child = db.execute('SELECT parent_id,status,result FROM tasks WHERE id=?',
                                   (row['child_id'],)).fetchone()
                if not child or child['parent_id'] != decision['task_id']:
                    raise Conflict('routing_reference_identity_mismatch')
                result = {'kind': kind, 'reference_id': reference, 'peer': row['peer'],
                          'message_id': row['message_id'], 'state': child['status'],
                          'remote_task_id': row['remote_task_id'], 'remote_state': row['remote_status'],
                          'terminal_recorded': bool(row['terminal']), 'observed_at': row['observed'],
                          'result_sha256': _sha(child['result']) if child['result'] is not None else None}
            elif kind == 'managed_allocation':
                self._table(db, 'managed_allocations')
                self._table(db, 'managed_dispatch')
                row = db.execute('''SELECT operation_id,actor,task_id,task_node,task_epoch,
                    state,epoch,updated FROM managed_allocations WHERE operation_id=?''',
                                 (reference,)).fetchone()
                if not row:
                    raise ValueError('routing_reference_not_found')
                if row['task_id'] != decision['task_id']:
                    raise PermissionError('routing_reference_task_mismatch')
                if (row['actor'] != 'node:' + decision['original_node']
                        or row['task_node'] != decision['original_node']):
                    raise PermissionError('routing_reference_principal_mismatch')
                dispatch = db.execute('''SELECT capability_id,state,settlement
                    FROM managed_dispatch WHERE operation_id=? ORDER BY capability_id LIMIT 33''',
                                      (reference,)).fetchall()
                result = {'kind': kind, 'reference_id': reference, 'state': row['state'],
                          'allocation_epoch': row['epoch'], 'original_task_epoch': row['task_epoch'],
                          'observed_at': row['updated'],
                          'dispatch_truncated': len(dispatch) > 32,
                          'dispatch': [{'capability_id': item['capability_id'], 'state': item['state'],
                                        'settlement_sha256': _sha(item['settlement'])
                                        if item['settlement'] is not None else None} for item in dispatch[:32]]}
            else:
                raise ValueError('invalid_routing_reference_kind')
        except sqlite3.Error:
            raise ValueError('routing_reference_ledger_unavailable') from None
        result.update({'association_verification': 'authority_ledger_identity',
                       'association_caller_selected': True,
                       'model_selection_verified': False,
                       'plan_alignment_verified': False, 'execution_verified': False,
                       'external_outcome_verified': False})
        return result

    def _view(self, db, row):
        linked = self._reference(db, row, row['link_kind'], row['link_reference']) if row['link_kind'] else None
        return dict(_FLAGS, decision_id=row['decision_id'], authority=row['authority'],
                    task_id=row['task_id'], original_node=row['original_node'],
                    original_epoch=row['original_epoch'],
                    original_leader_epoch=row['original_leader_epoch'],
                    decision=json.loads(row['body']), content_sha256=row['fingerprint'],
                    created=row['created'], proposal_verification='model_declared',
                    evidence_refs_verification='unchecked_model_references',
                    linked_execution=linked)

    def propose(self, task_id, node, epoch, decision):
        body, encoded = _decision(decision)
        fingerprint = _sha(encoded)
        with self.store.transaction() as db:
            task = self._task(db, task_id, node, epoch)
            previous = db.execute('SELECT * FROM route_decisions WHERE decision_id=?',
                                  (body['decision_id'],)).fetchone()
            if previous:
                if (previous['task_id'] != task_id or previous['authority'] != self.authority
                        or previous['fingerprint'] != fingerprint or previous['body'] != encoded):
                    raise Conflict('routing_decision_identity_content_conflict')
                return dict(self._view(db, previous), proposal_created=False)
            now = _finite(self.store.clock(), 'routing_created')
            if now < 0:
                raise ValueError('invalid_routing_created')
            db.execute('INSERT INTO route_decisions VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)',
                       (body['decision_id'], self.authority, task_id, node, epoch,
                        task['leader_epoch'], encoded, fingerprint, now))
            return dict(self._view(db, self._find(db, task_id, body['decision_id'])), proposal_created=True)

    def inspect(self, task_id, node, epoch, decision_id):
        with self.store.transaction() as db:
            self._task(db, task_id, node, epoch)
            return self._view(db, self._find(db, task_id, decision_id))

    def _page_view(self, db, rows, task_id, limit):
        page = dict(_FLAGS, authority=self.authority, task_id=task_id,
                    decisions=[], has_more=False)
        # Canonical ASCII serialization is conservative for the compact UTF-8
        # HTTP response. Reserve the longer False spelling before truncation.
        size = len(json.dumps(page, sort_keys=True, separators=(',', ':'),
                              ensure_ascii=True, allow_nan=False).encode('utf8'))
        for row in rows[:limit]:
            view = self._view(db, row)
            encoded = json.dumps(view, sort_keys=True, separators=(',', ':'),
                                 ensure_ascii=True, allow_nan=False).encode('utf8')
            addition = len(encoded) + bool(page['decisions'])
            if size + addition > MAX_OUTPUT_BYTES:
                if not page['decisions']:
                    raise ValueError('routing_view_too_large')
                break
            page['decisions'].append(view)
            size += addition
        page['has_more'] = len(rows) > len(page['decisions'])
        return page

    def list(self, task_id, node, epoch, limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST:
            raise ValueError('invalid_routing_limit')
        with self.store.transaction() as db:
            self._task(db, task_id, node, epoch)
            rows = db.execute('''SELECT * FROM route_decisions WHERE task_id=? AND authority=?
                ORDER BY created DESC,decision_id LIMIT ?''',
                              (task_id, self.authority, limit + 1)).fetchall()
            return self._page_view(db, rows, task_id, limit)

    def link(self, task_id, node, epoch, decision_id, kind, reference_id):
        """Bind ONE existing own-task reference; never invoke its operation."""
        if kind not in ('remote_delegation', 'managed_allocation'):
            raise ValueError('invalid_routing_reference_kind')
        _name(reference_id, 'routing_reference_id')
        with self.store.transaction() as db:
            self._task(db, task_id, node, epoch)
            decision = self._find(db, task_id, decision_id)
            if decision['link_kind'] and (decision['link_kind'] != kind
                                          or decision['link_reference'] != reference_id):
                raise Conflict('routing_execution_link_conflict')
            self._reference(db, decision, kind, reference_id)
            created = not decision['link_kind']
            if created:
                db.execute('UPDATE route_decisions SET link_kind=?,link_reference=? WHERE decision_id=?',
                           (kind, reference_id, decision_id))
            return dict(self._view(db, self._find(db, task_id, decision_id)), link_created=created)

    def owner_read(self, decision_id=None, task_id=None, limit=20):
        """Historical read for a separately authenticated operator/viewer caller.

        This method has no identity-authentication mechanism. The application
        MUST limit this route to its deployment's operator/viewer; a method
        name or a model argument cannot grant that role. Worker reads continue
        through task-fenced inspect/list, including after a valid continuation.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST:
            raise ValueError('invalid_routing_limit')
        where, values = ['authority=?'], [self.authority]
        for key, value in (('decision_id', decision_id), ('task_id', task_id)):
            if value is not None:
                _name(value, 'routing_' + key)
                where.append(key + '=?')
                values.append(value)
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM route_decisions WHERE ' + ' AND '.join(where)
                              + ' ORDER BY created DESC,decision_id LIMIT ?', values + [limit + 1]).fetchall()
            if decision_id is not None and not rows:
                raise ValueError('routing_decision_not_found')
            return self._page_view(db, rows, task_id, limit)
