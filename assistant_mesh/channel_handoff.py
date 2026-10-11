"""Owner-only, read-only carrier handoff inventory; NEVER a grant proof.

The audit checkpoint is intentionally not migratable. Real canonical receipts
and owned thread returns are useful facts, but do not independently prove that
an admitted remote request or delayed caller can no longer take effect. No
Store construction, service stop, transport IO, import or replay occurs here.
"""
import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from . import channel_checkpoint as checkpoint
from .channel_carrier import PROTOCOL as CARRIER_PROTOCOL, _TICKET_KEYS, _id, _sha
from .networking import canonical, digest


PROTOCOL = 'mesh-channel-handoff-readiness/1'
_CANONICAL_COLUMNS = ('operation_id', 'account_binding', 'holder', 'epoch', 'kind',
    'term', 'identity', 'snapshot', 'state', 'ticket', 'outcome', 'outcome_sha256',
    'persistence_sha256', 'accepted', 'uncertain', 'created', 'updated')
_SPOOL_COLUMNS = ('operation_id', 'ticket', 'outcome', 'outcome_sha256', 'post_attempted')
_IDENTITY_KEYS = ('protocol', 'witness_id', 'incarnation', 'account_binding',
                  'canonical_authority', 'owner_ref')


class HandoffError(ValueError):
    """Fixed diagnostics only, never a private path/row/SQLite exception."""


def _require(value, code):
    if not value:
        raise HandoffError(code)


@contextlib.contextmanager
def _readonly(path):
    database, inode = checkpoint._private_file(Path(path))
    db = None
    try:
        db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True,
                             isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        _require(checkpoint._private_file(database)[1] == inode, 'handoff_source_replaced')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db
        _require(checkpoint._private_file(database)[1] == inode, 'handoff_source_replaced')
    finally:
        if db is not None:
            db.close()


def _capture(db, authority, binding, max_rows, max_bytes):
    """Same read view for channel rows, full intents and dependency anchors."""
    budget = checkpoint._Budget(max_rows, max_bytes)
    catalog = [(row[0], row[1]) for row in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    names = {name for name, unused in catalog}
    _require(set(checkpoint._COLUMNS) - {'mesh_notify_receipts'} <= names,
             'handoff_canonical_schema_missing')
    selected = sorted(names & set(checkpoint._COLUMNS))
    schemas = {table: checkpoint._schema(db, table) for table in selected}
    tables = {table: checkpoint._rows(db, table, budget) for table in selected}
    dependencies = [checkpoint._dependency(db, table, sql, budget)
                    for table, sql in catalog if table not in tables]
    meta = {row['key']: json.loads(row['value']) for row in tables['meta']}
    payload = {'protocol': checkpoint.PROTOCOL, 'purpose': 'private_audit_checkpoint',
        'checkpoint_id': 'readiness-read-view', 'authority': authority,
        'account_binding': binding, 'route_id': checkpoint._digest([
            'mesh-notify/1', binding, meta.get('owner_notification_receiver_incarnation')]),
        'captured_at': time.time(), 'migration_ready': False,
        'safety': dict(checkpoint._SAFETY), 'schemas': schemas, 'tables': tables,
        'task_bindings': checkpoint._mapping(tables, authority),
        'inbox_task_bindings': checkpoint._inbox_bindings(tables, meta),
        'source_dependencies': dependencies}
    checkpoint._validate_payload(payload)
    canonical_rows, witnessed = [], []
    witness_row, account = None, None
    if 'channel_carrier_witness' in names:
        rows = list(db.execute('SELECT * FROM channel_carrier_witness'))
        _require(len(rows) == 1, 'handoff_witness_identity_invalid')
        witness_row = dict(rows[0])
    if 'channel_carrier_accounts' in names:
        row = db.execute('SELECT * FROM channel_carrier_accounts WHERE account_binding=?',
                         (binding,)).fetchone()
        account = dict(row) if row is not None else None
    if 'channel_carrier_intents' in names:
        witnessed = [dict(row) for row in db.execute(
            'SELECT * FROM channel_carrier_intents WHERE account_binding=? ORDER BY operation_id',
            (binding,))]
    if 'channel_canonical_intents' in names:
        columns = tuple(row[1] for row in db.execute('PRAGMA table_info(channel_canonical_intents)'))
        _require(columns == _CANONICAL_COLUMNS, 'handoff_canonical_intent_schema_unsupported')
        for row in db.execute('SELECT * FROM channel_canonical_intents '
                              'WHERE account_binding=? ORDER BY operation_id', (binding,)):
            result = dict(row)
            budget.add(len(checkpoint._canonical(result)))
            canonical_rows.append(result)
    return payload, witness_row, account, witnessed, canonical_rows, names


def _canonical_receipts(rows, witnessed, identity):
    """Recompute original receipt from FULL original ticket/outcome/identity."""
    indexed = {row['operation_id']: row for row in witnessed}
    canonical_ids = {row['operation_id'] for row in rows}
    pending, persisted = 0, 0
    for row in rows:
        original = indexed.get(row['operation_id'])
        _require(original is not None, 'handoff_original_witness_intent_missing')
        term, original_identity = json.loads(row['term']), json.loads(row['identity'])
        _require(all(term.get(key) == identity[key] for key in _IDENTITY_KEYS)
            and term.get('holder') == row['holder'] == original['holder']
            and term.get('epoch') == row['epoch'] == original['epoch']
            and row['kind'] == original['kind']
            and original_identity == json.loads(original['identity'])
            and digest(original_identity) == original['identity_sha256']
            and original_identity.get('authority') == identity['canonical_authority'],
            'handoff_original_identity_changed')
        if original['state'] == 'aborted' and row['ticket'] is None:
            _require(row['state'] == 'prepared' and row['outcome'] is None
                     and row['persistence_sha256'] is None,
                     'handoff_aborted_intent_changed')
            continue  # Never admitted, but retained; no rewrite of original row.
        if row['ticket'] is None or row['outcome'] is None or row['persistence_sha256'] is None:
            pending += 1
            continue
        ticket, outcome = json.loads(row['ticket']), json.loads(row['outcome'])
        expected_ticket = dict(identity)
        expected_ticket.pop('owner_ref')
        expected_ticket.update({key: original[key] for key in ('holder', 'epoch',
            'operation_id', 'kind', 'identity_sha256', 'request_sha256', 'dispatch_token')})
        expected_ticket['identity'] = original_identity
        _require(set(ticket) == set(_TICKET_KEYS) and ticket == expected_ticket
            and row['outcome_sha256'] == original['outcome_sha256'] == digest(outcome),
            'handoff_original_full_result_changed')
        expected = digest({'protocol': 'mesh-channel-canonical-receipt/1',
            'operation_id': row['operation_id'], 'ticket_sha256': digest(ticket),
            'outcome_sha256': row['outcome_sha256'], 'accepted': row['accepted'],
            'identity_sha256': digest(original_identity)})
        _require(row['state'] == 'persisted'
            and row['persistence_sha256'] == original['persistence_sha256'] == expected
            and bool(row['uncertain']) == (outcome.get('status') == 'unknown'),
            'handoff_original_persistence_receipt_changed')
        persisted += 1
        # An independently reconciled witness closes the original intent while
        # retaining its unknown canonical outcome. Do not silently rewrite that
        # evidence, or keep the inventory permanently unclosed after reconcile.
        if original['state'] not in ('committed', 'reconciled') or (
                row['uncertain'] and original['state'] != 'reconciled'):
            pending += 1
    _require(all(row['operation_id'] in canonical_ids
                 or row['state'] == 'aborted' for row in witnessed),
             'handoff_original_canonical_intent_missing')
    return {'full_original_intents': len(rows), 'durable_receipts_recomputed': persisted,
            'unclosed_canonical_intents': pending}


def _spools(paths, rows, identity, max_rows, max_bytes):
    budget = checkpoint._Budget(max_rows, max_bytes)
    indexed = {row['operation_id']: row for row in rows}
    covered, pending, matched = set(), 0, 0
    for path in paths:
        with _readonly(path) as db:
            identities = [row[0] for row in db.execute('SELECT identity FROM original_channel_authority')]
            _require(len(identities) == 1 and json.loads(identities[0]) == identity,
                     'handoff_spool_authority_changed')
            columns = tuple(row[1] for row in db.execute('PRAGMA table_info(original_channel_results)'))
            _require(columns == _SPOOL_COLUMNS, 'handoff_spool_schema_unsupported')
            for raw in db.execute('SELECT * FROM original_channel_results ORDER BY operation_id'):
                row = dict(raw)
                budget.add(len(checkpoint._canonical(row)))
                canonical_row = indexed.get(row['operation_id'])
                _require(canonical_row is not None and row['operation_id'] not in covered
                    and row['ticket'] == canonical_row['ticket'], 'handoff_spool_original_ticket_changed')
                covered.add(row['operation_id'])
                if row['outcome'] is None:
                    _require(row['outcome_sha256'] is None and row['post_attempted'] == 0,
                             'handoff_spool_result_invalid')
                    pending += 1
                    continue
                outcome = json.loads(row['outcome'])
                _require(row['post_attempted'] == 1
                    and row['outcome_sha256'] == digest(outcome), 'handoff_spool_result_invalid')
                if canonical_row['outcome'] is None:
                    pending += 1  # Full local return retained; no canonical ACK assumed.
                    continue
                _require(row['outcome'] == canonical_row['outcome']
                    and row['outcome_sha256'] == canonical_row['outcome_sha256'],
                    'handoff_spool_canonical_result_changed')
                matched += 1
    return {'private_spools_read': len(paths), 'original_tickets_matched': len(covered),
        'full_outcomes_matched': matched, 'unconfirmed_returns': pending,
        'canonical_dispatches_not_in_supplied_spools': sum(
            row['ticket'] is not None and row['operation_id'] not in covered for row in rows)}


def _caller(caller, identity, account):
    from .channel_supervisor import ChannelSupervisor
    _require(isinstance(caller, ChannelSupervisor), 'handoff_owned_caller_handle_missing')
    term = caller.fence.term
    _require(all(term.get(key) == identity[key] for key in _IDENTITY_KEYS)
        and account is not None and term.get('holder') == account['holder']
        and term.get('epoch') == account['epoch'], 'handoff_owned_caller_binding_changed')
    with caller._lock:
        threads = dict(caller._threads)
        started = caller._started
    _require(started and set(threads) == {'maintenance', 'poll', 'send'}
        and all(isinstance(thread, threading.Thread) and thread.ident is not None
                for thread in threads.values())
        and len({id(thread) for thread in threads.values()}) == 3
        and isinstance(caller.channel.stop, threading.Event),
        'handoff_owned_caller_handle_missing')
    alive = sum(thread.is_alive() for thread in threads.values())
    stopped = caller.channel.stop.is_set()
    return {'actual_owned_threads': 3, 'threads_still_alive': alive,
            'stop_requested': stopped, 'owned_thread_lifecycles_returned': stopped and alive == 0,
            'external_operation_quiescence_verified': False}


def collect_readiness(database, canonical_authority, account_binding, owner_ref,
                      witness_id, incarnation, checkpoint_path=None,
                      checkpoint_sha256=None, spool_paths=(), caller=None,
                      max_rows=100000, max_bytes=64 * 1024 * 1024):
    """Compare protected bindings with actual private state; no grant/stop/IO.

    Pins must come from authenticated owner deployment, independently of these
    artifacts. `caller` is an actual in-process owner-held ChannelSupervisor,
    not its snapshot/PID or a carrier-posted JSON. Even a fully checked report
    cannot prove external-call quiescence or authorize carrier/authority moves.
    """
    for value in (canonical_authority, owner_ref, witness_id):
        _id(value)
    _sha(account_binding)
    _sha(incarnation)
    checkpoint._Budget(max_rows, max_bytes)
    _require(isinstance(spool_paths, (tuple, list)) and len(spool_paths) <= 64,
             'handoff_spool_inventory_invalid')
    paths = [str(Path(path)) for path in spool_paths]
    _require(len(set(paths)) == len(paths), 'handoff_spool_inventory_invalid')
    identity = dict(protocol=CARRIER_PROTOCOL, witness_id=witness_id,
        incarnation=incarnation, account_binding=account_binding,
        canonical_authority=canonical_authority, owner_ref=owner_ref)
    report = {'protocol': PROTOCOL, 'purpose': 'private_readiness_inventory',
        'scope': 'communication_carrier_only_original_authority_retained',
        'migration_ready': False, 'grant_authorized': False,
        'native_features_restricted': False, 'checks': {}}

    def check(name, operation):
        try:
            detail = operation()
            report['checks'][name] = dict(detail, state='checked')
            return detail
        except (HandoffError, checkpoint.CheckpointError) as error:
            report['checks'][name] = {'state': 'missing', 'code': str(error)}
        except Exception:
            report['checks'][name] = {'state': 'missing', 'code': 'handoff_evidence_read_failed'}
        return None

    snapshot = []
    def capture():
        with _readonly(database) as db:
            snapshot.append(_capture(db, canonical_authority, account_binding, max_rows, max_bytes))
        payload = snapshot[0][0]
        return {'selected_rows': sum(len(rows) for rows in payload['tables'].values()),
                'dependency_tables': len(payload['source_dependencies']),
                'dependency_rows': sum(row['rows'] for row in payload['source_dependencies']),
                'snapshot_content_sha256': digest({key: payload[key] for key in
                    ('schemas', 'tables', 'source_dependencies')}),
                'native_history_bytes_copied': False}
    check('consistent_canonical_state', capture)
    account = None
    if snapshot:
        payload, witness, account, witnessed, rows, names = snapshot[0]
        def original_witness():
            _require(witness is not None and account is not None,
                     'handoff_original_witness_missing')
            _require(witness['witness_id'] == witness_id and witness['incarnation'] == incarnation
                and account['canonical_authority'] == canonical_authority
                and account['owner_ref'] == owner_ref,
                'handoff_original_witness_binding_changed')
            return {'epoch': account['epoch'], 'frozen': bool(account['frozen']),
                    'checkpoint_revision': account['checkpoint_revision']}
        matched = check('original_witness', original_witness)
        if matched is not None:
            def canonical_receipts():
                _require({'channel_canonical_intents', 'channel_carrier_intents'} <= names,
                         'handoff_original_intent_tables_missing')
                return _canonical_receipts(rows, witnessed, identity)
            receipts = check('full_canonical_intents_and_receipts', canonical_receipts)
            if receipts is not None:
                def managed_intents_closed():
                    _require(receipts['unclosed_canonical_intents'] == 0
                        and all(row['state'] in ('committed', 'aborted', 'reconciled')
                                for row in witnessed), 'handoff_original_intents_unclosed')
                    return {'canonical_and_witness_terminal': True,
                            'external_operations_quiescent': False}
                check('managed_original_intents_closed', managed_intents_closed)
            if paths:
                check('private_original_result_spools',
                      lambda: _spools(paths, rows, identity, max_rows, max_bytes))
        if checkpoint_path is not None and checkpoint_sha256 is not None:
            def pinned_checkpoint():
                path, unused = checkpoint._private_file(Path(checkpoint_path))
                with path.open('rb') as handle:
                    raw = handle.read(max_bytes + 1)
                _require(len(raw) <= max_bytes, 'handoff_checkpoint_limit_exceeded')
                artifact = json.loads(raw.decode('utf8'))
                checkpoint.validate_checkpoint(artifact, canonical_authority,
                    account_binding, checkpoint_sha256)
                _require(all(artifact['payload'][key] == payload[key] for key in
                    ('route_id', 'schemas', 'tables', 'task_bindings', 'inbox_task_bindings',
                     'source_dependencies')), 'handoff_checkpoint_source_changed')
                return {'externally_pinned_sha256': checkpoint_sha256,
                        'migration_ready': False, 'current_read_view_matches': True}
            check('externally_pinned_audit_checkpoint', pinned_checkpoint)
    if caller is not None:
        lifecycle = check('actual_owned_caller_lifecycle', lambda: _caller(caller, identity, account))
        if lifecycle is not None:
            def thread_return():
                _require(lifecycle['owned_thread_lifecycles_returned'],
                         'handoff_owned_caller_not_returned')
                return {'owned_threads_returned': True, 'external_operations_quiescent': False}
            check('owned_caller_thread_return', thread_return)
    for name in ('original_witness', 'full_canonical_intents_and_receipts',
                 'private_original_result_spools', 'externally_pinned_audit_checkpoint',
                 'actual_owned_caller_lifecycle', 'managed_original_intents_closed',
                 'owned_caller_thread_return'):
        report['checks'].setdefault(name, {'state': 'missing', 'code': 'handoff_evidence_not_supplied'})
    # Always missing: neither a SHA nor ledger terminality supplies these proofs.
    for name in ('independent_external_and_admitted_caller_quiescence',
                 'complete_original_history_execution_migration',
                 'independent_fault_domain_authority_and_arbitration'):
        report['checks'][name] = {'state': 'missing', 'code': 'handoff_independent_proof_required'}
    report['checked'] = sorted(name for name, detail in report['checks'].items()
                               if detail['state'] == 'checked')
    report['missing'] = sorted(name for name, detail in report['checks'].items()
                               if detail['state'] == 'missing')
    return report
