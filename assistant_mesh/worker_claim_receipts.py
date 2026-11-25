"""Optional worker claim receipts on the original authority transaction."""
from contextlib import contextmanager
import hashlib
import re
import secrets
import threading
import time

from .ingress import _identifier
from .store import Conflict


CLAIM = '/v1/worker-claim'
RECEIPT = '/v1/worker-claim/receipt'
FORMAT = 'amita-worker-claim/1'
NEW_ID_WINDOW = 120
EMPTY_RETENTION = 3600
CLAIM_ID = re.compile(r'(c1|c2)-([1-9][0-9]{9,13})-[0-9a-f]{32}(?:-([0-9a-f]{64}))?')


def _task_digest(task_id):
    return hashlib.sha256(_identifier(task_id, 'worker_claim_task').encode('utf8')).hexdigest()


def exact_claim_id(task_id):
    """Create a request ID for one task. Retain it before sending the claim."""
    return 'c2-{}-{}-{}'.format(int(time.time()), secrets.token_hex(16), _task_digest(task_id))


def parse_claim(value):
    match = CLAIM_ID.fullmatch(value) if isinstance(value, str) else None
    if match is None or (match[1] == 'c2') != (match[3] is not None):
        raise ValueError('worker_claim_id_invalid')
    return float(match[2]), match[3]


def confirm(response, identity):
    receipt = response.get('receipt') if isinstance(response, dict) else None
    if (not isinstance(receipt, dict) or receipt.get('claim_id') != identity
            or response.get('format') != FORMAT or 'task' not in response):
        raise ValueError('worker_claim_receipt_unconfirmed')
    task = response.get('task')
    if response.get('replayed') is not False:
        raise ValueError('worker_claim_already_recorded')
    expected = (task['id'], task['epoch']) if task is not None else (None, None)
    if (receipt.get('task_id'), receipt.get('task_epoch')) != expected:
        raise ValueError('worker_claim_receipt_binding_changed')
    return task


def install_dispatch(api):
    """Attach to the existing API, without replacing its claim algorithm."""
    if getattr(api, '_worker_claim_receipts_installed', False):
        raise ValueError('worker_claim_receipts_already_installed')
    store, previous = api.store, api.dispatch
    original_transaction = store.transaction
    original_claim_in_db = getattr(store, '_claim_in_db', None)
    active = threading.local()

    with original_transaction() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS worker_claim_receipts(
            node TEXT NOT NULL, claim_id TEXT NOT NULL, task_id TEXT, task_epoch INTEGER,
            recorded_at REAL NOT NULL, PRIMARY KEY(node, claim_id))''')
        db.execute('''CREATE INDEX IF NOT EXISTS worker_claim_receipts_empty_time
            ON worker_claim_receipts(recorded_at) WHERE task_id IS NULL''')

    @contextmanager
    def transaction():
        state = getattr(active, 'state', None)
        if state is None:
            with original_transaction() as db:
                yield db
            return
        try:
            # Existing dispatch invokes its own transaction helpers under
            # this same connection, including the legacy election and claim.
            yield state['db']
        except BaseException:
            state['failed'] = True
            raise

    def claim_in_db(db, node, task_ids=None):
        state = getattr(active, 'state', None)
        if state is not None and state['task_id'] is not None:
            if db is not state['db'] or node != state['node']:
                raise ValueError('worker_exact_claim_dispatch_binding_changed')
            selected = state['task_id']
            task_ids = (selected,) if task_ids is None or selected in task_ids else ()
            state['selector_used'] = True
        return original_claim_in_db(db, node, task_ids=task_ids)

    def dispatch(method, route, payload, peer):
        if route not in (CLAIM, RECEIPT):
            return previous(method, route, payload, peer)
        if method != 'POST' or not isinstance(peer, dict) or peer.get('role') != 'worker':
            raise PermissionError('worker_claim_authenticated_worker_required')
        node = peer.get('node')
        maintenance = getattr(api, '_worker_maintenance', None)
        if maintenance is None or maintenance.store is not store or node not in maintenance.workers:
            raise PermissionError('worker_claim_registered_worker_required')
        if not isinstance(payload, dict) or 'claim_id' not in payload:
            raise ValueError('worker_claim_arguments_invalid')
        identity = payload['claim_id']
        issued_at, task_digest = parse_claim(identity)
        exact = route == CLAIM and task_digest is not None
        if set(payload) != ({'claim_id', 'task_id'} if exact else {'claim_id'}):
            raise ValueError('worker_claim_arguments_invalid')
        task_id = payload.get('task_id')
        if exact and _task_digest(task_id) != task_digest:
            raise ValueError('worker_exact_claim_task_binding_changed')
        # Original maintenance acquires this per-node RLock before touching
        # SQLite. Preserve that order across the wrapped legacy dispatcher.
        with maintenance.locks[node], original_transaction() as db:
            old = db.execute(
                'SELECT claim_id,task_id,task_epoch,recorded_at FROM worker_claim_receipts WHERE node=? AND claim_id=?',
                (node, identity)).fetchone()
            if old is not None or route == RECEIPT:
                return {'format': FORMAT, 'receipt': dict(old) if old is not None else None,
                        'task': None, 'replayed': old is not None}
            now = store.clock()
            if abs(now - issued_at) > NEW_ID_WINDOW:
                raise Conflict('worker_claim_admission_expired')
            if exact and not callable(original_claim_in_db):
                raise Conflict('worker_exact_claim_selector_not_installed')
            state = {'db': db, 'failed': False, 'node': node, 'task_id': task_id,
                     'selector_used': False}
            active.state = state
            try:
                # Re-enter the actual current routing chain. Middleware
                # installed after this adapter still gates the legacy claim.
                result = api.dispatch('POST', '/v1/claim', {}, peer)
            finally:
                active.state = None
            if state['failed'] or not isinstance(result, dict) or 'task' not in result:
                raise ValueError('worker_claim_original_outcome_unconfirmed')
            task = result['task']
            if exact and task is not None and (not state['selector_used'] or task['id'] != task_id):
                # A legacy fallback must never commit a different assignment.
                raise Conflict('worker_exact_claim_selection_unconfirmed')
            task_id, epoch = (task['id'], task['epoch']) if task is not None else (None, None)
            if task is not None and (not isinstance(task_id, str) or type(epoch) is not int or epoch < 1):
                raise ValueError('worker_claim_original_identity_invalid')
            # Only empty poll metadata expires. Every task assignment receipt
            # remains durable even after its execution lease ends.
            db.execute('DELETE FROM worker_claim_receipts WHERE task_id IS NULL AND recorded_at<?',
                       (now - EMPTY_RETENTION,))
            db.execute('INSERT INTO worker_claim_receipts VALUES(?,?,?,?,?)',
                       (node, identity, task_id, epoch, now))
            receipt = dict(claim_id=identity, task_id=task_id, task_epoch=epoch, recorded_at=now)
            return {'format': FORMAT, 'receipt': receipt, 'task': task, 'replayed': False}

    store.transaction = transaction
    if callable(original_claim_in_db):
        store._claim_in_db = claim_in_db
    api.dispatch = dispatch
    api._worker_claim_receipts_installed = True
