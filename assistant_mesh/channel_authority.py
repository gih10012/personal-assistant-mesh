"""Authenticated carrier-only RPC over the one original message authority.

This is transport placement, NOT Leader migration or partition availability.
No public grant/freeze/reconcile endpoint; operator proof remains independent.
"""
import contextlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from .channel_carrier import CarrierError, CarrierWitness, _shape
from .channel_state import ChannelState
from .networking import canonical, digest


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError('channel_carrier_config_invalid')
    base = {key: item for key, item in value.items() if key != 'supervisor'}
    _shape(base, ('witness_id', 'owner_ref', 'transport_mode'))
    from .channel_carrier import _id
    _id(value['witness_id'])
    _id(value['owner_ref'])
    if value['transport_mode'] not in ('disabled', 'fenced'):
        raise ValueError('channel_carrier_transport_mode_invalid')
    result = dict(base)
    if 'supervisor' in value:
        if value['transport_mode'] != 'fenced':
            raise ValueError('channel_supervisor_requires_fenced_transport')
        from .channel_supervisor import validate_config as validate_supervisor
        result['supervisor'] = validate_supervisor(value['supervisor'])
    return result


def validate_peer(peer, owner_ref, authority):
    from .channel_carrier import _id, _sha
    _id(peer.get('node'))
    _shape(peer.get('channel'), ('owner_ref', 'canonical_authority', 'account_binding'))
    binding = peer['channel']
    _sha(binding['account_binding'])
    if binding['owner_ref'] != owner_ref or binding['canonical_authority'] != authority:
        raise ValueError('channel_carrier_peer_binding_invalid')


class ChannelAuthority:
    def __init__(self, store, config, authority, transport, account_binding,
                 handoff_verifier=None, operation_verifier=None):
        # Trusted owner code injection only, not JSON/module loading or a
        # carrier RPC. Missing hooks deliberately keep grant/reconcile closed.
        if any(hook is not None and not callable(hook)
               for hook in (handoff_verifier, operation_verifier)):
            raise CarrierError('channel_owner_verifier_invalid')
        self.config, self.authority, self.transport = config, authority, transport
        self.binding, self.owner_ref = account_binding, config['owner_ref']
        self.witness = CarrierWitness(store, config['witness_id'],
            handoff_verifier=handoff_verifier, operation_verifier=operation_verifier)
        self.witness.enroll(account_binding, self.owner_ref, authority)
        self.state = ChannelState(store, self.witness, account=transport.account)

    def _owned(self, actor, operation_id):
        with self.state.store.transaction() as db:
            row = self.state._row(db, operation_id)
            if row['holder'] != actor or row['account_binding'] != self.binding:
                raise PermissionError('channel_original_intent_not_owned')
            return dict(row)

    def _scope(self, actor, payload):
        if 'term' in payload:
            term = payload['term']
            if (not isinstance(term, dict) or term.get('holder') != actor
                    or term.get('account_binding') != self.binding
                    or term.get('owner_ref') != self.owner_ref
                    or term.get('canonical_authority') != self.authority):
                raise PermissionError('channel_carrier_binding_changed')
        if 'ticket' in payload:
            ticket = payload['ticket']
            if (not isinstance(ticket, dict) or ticket.get('holder') != actor
                    or ticket.get('account_binding') != self.binding
                    or ticket.get('canonical_authority') != self.authority):
                raise PermissionError('channel_carrier_binding_changed')
            self._owned(actor, ticket.get('operation_id'))

    def dispatch(self, actor, action, payload):
        if not isinstance(payload, dict):
            raise CarrierError('carrier_protocol_invalid')
        self._scope(actor, payload)
        if action == 'inspect':
            _shape(payload, ())
            return self.witness.inspect(self.binding)
        if action == 'candidate':
            contract = payload.get('contract')
            if (not isinstance(contract, dict) or contract.get('account_binding') != self.binding
                    or contract.get('canonical_authority') != self.authority
                    or contract.get('owner_ref') != self.owner_ref):
                raise PermissionError('channel_carrier_binding_changed')
            return self.witness.request(actor, action, payload)
        if action == 'begin':
            _shape(payload, ('term', 'operation_id', 'kind', 'identity', 'request_sha256'))
            row = self._owned(actor, payload['operation_id'])
            identity, snapshot = json.loads(row['identity']), json.loads(row['snapshot'])
            endpoint = 'getupdates' if row['kind'] == 'poll' else 'sendmessage'
            body = {'get_updates_buf': snapshot['cursor']} if row['kind'] == 'poll' else self.transport.send_body(snapshot)
            if (row['epoch'] != payload['term']['epoch'] or row['kind'] != payload['kind']
                    or identity != payload['identity']
                    or digest({'endpoint': endpoint, 'body': body}) != payload['request_sha256']):
                raise CarrierError('channel_original_request_changed')
        if action in ('returned', 'commit_authorize', 'committed', 'committed_unknown'):
            ticket = payload.get('ticket', {})
            row = self._owned(actor, ticket.get('operation_id'))
            # Remote digests are never accepted as canonical persistence proof.
            # They must match the authority's durable full outcome/actual write.
            if action in ('returned', 'commit_authorize'):
                if row['outcome'] is None or row['outcome_sha256'] is None:
                    raise CarrierError('channel_original_result_missing')
                if action == 'returned' and payload.get('sha256') != row['outcome_sha256']:
                    raise CarrierError('channel_original_outcome_changed')
            else:
                if (row['persistence_sha256'] is None or row['persistence_sha256'] != payload.get('sha256')
                        or (action == 'committed_unknown') != bool(row['uncertain'])):
                    raise CarrierError('channel_original_receipt_unverified')
        if action in ('begin', 'dispatch', 'returned', 'commit_authorize', 'committed',
                      'committed_unknown', 'unknown', 'renew'):
            return self.witness.request(actor, action, payload)
        if action.startswith('state/'):
            return self.state_dispatch(actor, action[6:], payload)
        raise PermissionError('channel_carrier_route_not_authorized')

    def state_dispatch(self, actor, action, payload):
        if action == 'get':
            _shape(payload, ('key', 'default'))
            return {'value': self.state.get(**payload)}
        if action == 'set':
            _shape(payload, ('key', 'value'))
            self.state.set(**payload)
            return {'ok': True}
        if action == 'prepare_poll':
            _shape(payload, ('term', 'cursor'))
            return self.state.channel_prepare_poll(**payload)
        if action == 'prepare_send':
            _shape(payload, ('term',))
            return self.state.channel_prepare_send(**payload)
        keys = {'mark_dispatched': ('operation_id', 'ticket'),
                'record_result': ('operation_id', 'outcome'),
                'commit_poll': ('operation_id', 'ticket', 'outcome', 'owner', 'bot'),
                'commit_send': ('operation_id', 'ticket', 'status', 'detail'),
                'mark_uncertain': ('operation_id',), 'receipt': ('operation_id',)}
        if action not in keys:
            raise PermissionError('channel_carrier_route_not_authorized')
        _shape(payload, keys[action])
        self._owned(actor, payload['operation_id'])
        if action == 'receipt':
            return self.state.receipt(**payload)
        result = getattr(self.state, 'channel_' + action)(**payload)
        return {'value': result}

    def bound_client(self, actor):
        authority = self
        class BoundClient:
            def request(self, path, payload):
                if not path.startswith('/channel-carrier/'):
                    raise PermissionError('channel_carrier_route_not_authorized')
                return authority.dispatch(actor, path[len('/channel-carrier/'):], payload)
        return BoundClient()


class RemoteChannelState:
    """No local Store/import; every operation reaches the original authority."""
    def __init__(self, client, journal_path):
        self.client = client
        path = Path(journal_path).expanduser().absolute()
        if any(parent.is_symlink() for parent in path.parents) or path.is_symlink():
            raise CarrierError('channel_spool_path_unsafe')
        parent = path.parent.stat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700):
            raise CarrierError('channel_spool_directory_unsafe')
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        current = path.lstat()
        if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                or stat.S_IMODE(current.st_mode) != 0o600 or current.st_nlink != 1):
            raise CarrierError('channel_spool_path_unsafe')
        self.journal_path = str(path)
        term = client.request('/channel-carrier/inspect', {})
        identity = {key: term[key] for key in ('protocol', 'witness_id', 'incarnation',
                                             'account_binding', 'canonical_authority', 'owner_ref')}
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS original_channel_authority(identity TEXT NOT NULL)')
            rows = db.execute('SELECT identity FROM original_channel_authority').fetchall()
            if not rows:
                db.execute('INSERT INTO original_channel_authority VALUES(?)', (canonical(identity),))
            elif len(rows) != 1 or rows[0][0] != canonical(identity):
                raise CarrierError('channel_spool_authority_changed')
            db.execute('''CREATE TABLE IF NOT EXISTS original_channel_results(
                operation_id TEXT PRIMARY KEY, ticket TEXT NOT NULL,
                outcome TEXT, outcome_sha256 TEXT, post_attempted INTEGER NOT NULL DEFAULT 0)''')

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(self.journal_path, timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def spool(self, operation_id):
        # Private original-outcome recovery only. No callback/network replay.
        with self._db() as db:
            row = db.execute('SELECT ticket,outcome,outcome_sha256,post_attempted '
                             'FROM original_channel_results WHERE operation_id=?', (operation_id,)).fetchone()
        if row is None:
            raise CarrierError('channel_original_spool_missing')
        return {'ticket': json.loads(row[0]), 'outcome': json.loads(row[1]) if row[1] else None,
                'outcome_sha256': row[2], 'post_attempted': bool(row[3])}

    def _request(self, action, **payload):
        return self.client.request('/channel-carrier/state/' + action, payload)

    def get(self, key, default=None):
        return self._request('get', key=key, default=default)['value']

    def set(self, key, value):
        return self._request('set', key=key, value=value)

    def channel_prepare_poll(self, term, cursor):
        return self._request('prepare_poll', term=term, cursor=cursor)

    def channel_prepare_send(self, term):
        return self._request('prepare_send', term=term)

    def channel_mark_dispatched(self, operation_id, ticket):
        with self._db() as db:
            identity = json.loads(db.execute('SELECT identity FROM original_channel_authority').fetchone()[0])
            if any(ticket.get(key) != identity[key] for key in identity if key != 'owner_ref'):
                raise CarrierError('channel_spool_authority_changed')
            if db.execute('SELECT 1 FROM original_channel_results WHERE operation_id=?', (operation_id,)).fetchone():
                raise CarrierError('channel_original_dispatch_already_recorded')
            db.execute('INSERT INTO original_channel_results(operation_id,ticket) VALUES(?,?)',
                       (operation_id, canonical(ticket)))
        return self._request('mark_dispatched', operation_id=operation_id, ticket=ticket)['value']

    def channel_record_result(self, operation_id, outcome):
        encoded = canonical(outcome)
        if len(encoded.encode('utf8')) > 8 * 1024 * 1024:
            raise CarrierError('channel_original_result_too_large')
        with self._db() as db:
            row = db.execute('SELECT outcome,post_attempted FROM original_channel_results WHERE operation_id=?',
                             (operation_id,)).fetchone()
            if row is None or row[0] is not None or row[1]:
                raise CarrierError('channel_original_result_already_recorded')
            # Durably save full raw return BEFORE a write ACK can disappear.
            # Mark this RPC attempted too: reconciliation never silently posts
            # another operation or reloads a provider callback after restart.
            db.execute('UPDATE original_channel_results SET outcome=?,outcome_sha256=?,post_attempted=1 '
                       'WHERE operation_id=?', (encoded, digest(outcome), operation_id))
        return self._request('record_result', operation_id=operation_id, outcome=outcome)['value']

    def channel_commit_poll(self, operation_id, ticket, outcome, owner, bot):
        return self._request('commit_poll', operation_id=operation_id, ticket=ticket,
                             outcome=outcome, owner=owner, bot=bot)['value']

    def channel_commit_send(self, operation_id, ticket, status, detail):
        return self._request('commit_send', operation_id=operation_id, ticket=ticket,
                             status=status, detail=detail)['value']

    def channel_mark_uncertain(self, operation_id):
        return self._request('mark_uncertain', operation_id=operation_id)['value']
