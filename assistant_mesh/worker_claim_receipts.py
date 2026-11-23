"""Optional worker claim receipts on the original authority transaction."""
from contextlib import contextmanager
import re
import threading

from .store import Conflict


CLAIM = '/v1/worker-claim'
RECEIPT = '/v1/worker-claim/receipt'
FORMAT = 'amita-worker-claim/1'
NEW_ID_WINDOW = 120
EMPTY_RETENTION = 3600


def parse_claim(value):
    if (not isinstance(value, str)
            or re.fullmatch(r'c1-[1-9][0-9]{9,13}-[0-9a-f]{32}', value) is None):
        raise ValueError('worker_claim_id_invalid')
    return float(value.split('-')[1])


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

    def dispatch(method, route, payload, peer):
        if route not in (CLAIM, RECEIPT):
            return previous(method, route, payload, peer)
        if method != 'POST' or not isinstance(peer, dict) or peer.get('role') != 'worker':
            raise PermissionError('worker_claim_authenticated_worker_required')
        node = peer.get('node')
        maintenance = getattr(api, '_worker_maintenance', None)
        if maintenance is None or maintenance.store is not store or node not in maintenance.workers:
            raise PermissionError('worker_claim_registered_worker_required')
        if not isinstance(payload, dict) or set(payload) != {'claim_id'}:
            raise ValueError('worker_claim_arguments_invalid')
        identity = payload['claim_id']
        issued_at = parse_claim(identity)
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
            state = {'db': db, 'failed': False}
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
    api.dispatch = dispatch
    api._worker_claim_receipts_installed = True
