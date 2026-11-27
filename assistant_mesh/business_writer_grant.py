"""Confirm a writer transfer with a fresh majority-applied Raft barrier."""
from concurrent.futures import ThreadPoolExecutor
import secrets
import time

from .business_journal import digest
from .business_writer_seal import _integer
from .worker import Client


MANAGE = '/raft/manage'
MEMBERS = {1, 2, 3}


def _expected(proposal, receipt):
    head = dict(index=receipt['index'], sha256=digest(proposal))
    grant = dict(owner=proposal['next_owner'], epoch=proposal['next_epoch'], head=head)
    binding = {key: proposal[key] for key in ('group', 'role', 'scope', 'operation_id', 'checkpoint_sha256')}
    current = dict(grant, **binding, checkpoint=proposal['checkpoint'])
    expected_receipt = dict(**binding, format='amita-replica-writer-receipt/1', status='applied',
        request_sha256=head['sha256'], index=head['index'], term=receipt['term'], grant=grant,
        retained_effects_sha256=proposal['checkpoint']['retained_effects_sha256'])
    return current, expected_receipt


def _observation(proposal, barrier, member, status):
    if (not isinstance(status, dict) or status.get('format') != 'amita-raft-status/1'
            or type(status.get('member')) is not int or status['member'] != member
            or status.get('group') != proposal['group'] or status.get('scope') != proposal['scope']
            or status.get('owner_read_consistency') != 'local_durable_applied'
            or status.get('enabled') is not True or status.get('core_closed') is not False
            or status.get('hold') is not None or not isinstance(status.get('receipts'), dict)
            or not all(_integer(status.get(key)) for key in ('term', 'persisted_commit', 'applied'))):
        return None
    receipts = status['receipts']
    receipt, boundary = (receipts.get(key) for key in (proposal['operation_id'], barrier['operation_id']))
    if not all(isinstance(row, dict) and _integer(row.get('index'), 1) and _integer(row.get('term'), 1)
               for row in (receipt, boundary)):
        return None
    current, expected_receipt = _expected(proposal, receipt)
    if (receipt != expected_receipt or status.get('replica_writer') != current
            or boundary != {'digest': barrier['digest'], 'index': boundary['index'], 'term': boundary['term']}
            or not receipt['index'] < boundary['index'] <= status['applied'] <= status['persisted_commit']
            or not receipt['term'] <= boundary['term'] <= status['term']):
        return None
    return receipt, boundary


def _client_set(config):
    if (not isinstance(config, dict) or set(config) != {'voters'} or not isinstance(config['voters'], list)):
        raise ValueError('business_writer_voter_config_invalid')
    clients, addresses = {}, set()
    for voter in config['voters']:
        if (not isinstance(voter, dict) or not {'member', 'control_url', 'token_file'} <= set(voter)
                or set(voter) - {'member', 'control_url', 'token_file', 'unix_socket'}
                or type(voter['member']) is not int or voter['member'] not in MEMBERS or voter['member'] in clients):
            raise ValueError('business_writer_voter_config_invalid')
        client = Client(voter)
        address = client.parsed_url.hostname, client.parsed_url.port or (443 if client.parsed_url.scheme == 'https' else 80)
        if client.unix_socket:
            address = ('unix', client.unix_socket)
        if address in addresses:
            raise ValueError('business_writer_distinct_voters_required')
        addresses.add(address)
        clients[voter['member']] = client
    if len(clients) < 2:
        raise ValueError('business_writer_quorum_required')
    return clients


def _confirm(proposal, clients, timeout):
    # A new activation attempt needs a newly committed barrier. A transient
    # acknowledgement loss keeps the same barrier ID within the attempt.
    operation_id = 'writer-activate-' + secrets.token_hex(16)
    barrier = dict(format='amita-quorum-digest/1', operation_id=operation_id,
                   digest=digest([operation_id, digest(proposal)]))
    cutoff = time.monotonic() + timeout

    def request(member, action, **fields):
        body = dict(group=proposal['group'], scope=proposal['scope'], member=member, action=action, **fields)
        try:
            remaining = cutoff - time.monotonic()
            if remaining > 0:
                return clients[member].request(MANAGE, body, timeout=min(2, remaining))
        except (ValueError, OSError):
            return None

    def observe(member):
        return member, _observation(proposal, barrier, member, request(member, 'status'))

    with ThreadPoolExecutor(max_workers=len(clients)) as workers:
        statuses = dict(zip(clients, workers.map(lambda member: request(member, 'status'), clients)))
        # Prefer an observed leader to limit redundant entries; status hints
        # only choose an address and do not establish authority.
        hint = next((status.get('leader_hint') for status in statuses.values()
                     if isinstance(status, dict) and type(status.get('leader_hint')) is int
                     and status['leader_hint'] in clients), None)
        ordered = sorted(clients, key=lambda member: member != hint)
        attempt = 0
        while time.monotonic() < cutoff:
            # A submission ACK alone does not demonstrate commit.
            request(ordered[attempt % len(ordered)], 'propose', proposal=barrier)
            attempt += 1
            observations = dict(workers.map(observe, clients))
            for member, witness in observations.items():
                if witness is None:
                    continue
                seen = sorted(other for other, value in observations.items() if value == witness)
                if len(seen) >= 2 and time.monotonic() < cutoff:
                    receipt, applied = witness
                    return dict(receipt=receipt, barrier=dict(barrier, **{key: applied[key] for key in ('index', 'term')}),
                                members=seen)
            remaining = cutoff - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.35, remaining))
    raise ValueError('business_writer_fresh_applied_grant_unconfirmed')


def confirm_grant(proposal, config, timeout=15):
    """Read the exact still-current grant from two authenticated voters."""
    return _confirm(proposal, _client_set(config), timeout)
