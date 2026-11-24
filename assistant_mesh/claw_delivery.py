"""Direct iLink notification selection and original context renewal."""
import hashlib
import uuid
from types import MethodType

MAX_AUTO_RETRY_AGE = 15 * 60
MAX_RENEWAL_AGE = 5 * 60


def select_send(db, context, now):
    return db.execute("SELECT * FROM outbox WHERE delivery_route='channel' AND "
        "(status='pending' OR (status='rejected' AND retry_count<1 "
        "AND created>=? AND COALESCE(context_version,'')!=?)) ORDER BY created LIMIT 1",
        (now - MAX_AUTO_RETRY_AGE, context.get('version', ''))).fetchone()


def prepare_send(state, term):
    # Same original term, witness, Store and prepare/bind boundary.
    with state.store.transaction() as db:
        account, now = state.witness._current(db, term['holder'], term, admission=True)
        context = state.store._meta(db, 'owner_context', {})
        row = select_send(db, context, now)
        if row is None:
            return None
        result = dict(row)
        rejected = row['status'] == 'rejected'
        result['client_id'] = 'mesh-' + uuid.uuid4().hex if rejected else row['client_id']
        result['context_token'] = context.get('token')
        result['context_version'] = context.get('version', '')
        identity = {'authority': account['canonical_authority'], 'outbox_id': row['id'],
                    'client_id': result['client_id']}
        operation = state._prepare(db, term, 'send', identity, result, now)
        db.execute("UPDATE outbox SET status='submitting',client_id=?,context_version=?,"
                   'retry_count=retry_count+? WHERE id=?', (result['client_id'], result['context_version'],
                                                          int(rejected), row['id']))
        return {'operation_id': operation, 'row': result}


def claim_renewal(store, node):
    """Recent lost claim responses expose the same reserved operation."""
    with store.transaction() as db:
        now = store.clock()
        version = store._meta(db, 'owner_context', {}).get('version', '')
        rejected = db.execute(
            "SELECT id FROM outbox WHERE delivery_route='channel' AND status='rejected' "
            "AND retry_count<1 AND created>=? AND COALESCE(context_version,'')=? ORDER BY created LIMIT 1",
            (now - MAX_AUTO_RETRY_AGE, version)).fetchone()
        if not rejected:
            return None
        identity = hashlib.sha256((version or 'none').encode()).hexdigest()
        row = db.execute('SELECT * FROM renewals WHERE id=?', (identity,)).fetchone()
        if row is not None:
            if (row['status'] != 'reserved' or row['node'] != node
                    or row['created'] < now - MAX_RENEWAL_AGE):
                return None
            marker = row['marker']
        else:
            marker = 'ClawBot 自动刷新 mesh-' + identity[:24]
            db.execute('INSERT INTO renewals VALUES(?,?,?,?,?)',
                       (identity, node, marker, 'reserved', now))
        return {'id': identity, 'marker': marker, 'request_id': 'mesh-renew-' + identity[:40]}


def install_on_original_channel(api):
    """Bind the new policy to the already-running original authority objects."""
    authority = getattr(api, 'channel_authority', None)
    if authority is None:
        return
    state = authority.state
    if state.store is not api.store or state.witness.store is not api.store:
        raise ValueError('claw_delivery_original_store_required')
    state.channel_prepare_send = MethodType(prepare_send, state)
    api.store.claim_renewal = MethodType(claim_renewal, api.store)
