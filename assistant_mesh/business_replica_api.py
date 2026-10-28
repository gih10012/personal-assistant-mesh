"""Authenticated additive dispatch wrapper; no server/core replacement."""
from urllib.parse import urlsplit

from .business_journal import Journal, MAX_BYTES


PREFIX = '/v1/business-replica/'


def attach(api, config):
    existing = getattr(api, '_business_journal', None)
    if existing is not None:
        if existing.config != Journal(api.store, config).config:
            raise ValueError('business_replication_attachment_changed')
        return existing
    journal = Journal(api.store, config)
    journal.install()
    previous = api.dispatch

    def dispatch(method, path, payload, peer):
        parsed = urlsplit(path)
        if not parsed.path.startswith(PREFIX):
            return previous(method, path, payload, peer)
        permitted = (peer.get('role') == 'operator' or
            (peer.get('role') in ('worker', 'agent_peer') and peer.get('node')
             and 'business.replica.read' in peer.get('capabilities', [])))
        if not permitted or method != 'POST' or parsed.query or parsed.fragment or not isinstance(payload, dict):
            raise PermissionError('business_replication_read_not_authorized')
        owner = (peer.get('role'), peer.get('node'))
        action = parsed.path[len(PREFIX):]
        if action == 'status' and not payload:
            return journal.status()
        if action in ('refresh', 'suspend') and not payload:
            if peer.get('role') != 'operator':
                raise PermissionError('business_replication_refresh_not_authorized')
            return journal.refresh() if action == 'refresh' else journal.suspend()
        if action == 'export' and {'after', 'incarnation', 'generation'} <= set(payload) <= {'after', 'incarnation', 'generation', 'limit', 'max_bytes'}:
            return journal.export(payload['after'], payload['incarnation'], payload['generation'],
                                  payload.get('limit', 100), payload.get('max_bytes', MAX_BYTES))
        if action == 'snapshot':
            verb = payload.get('action')
            if verb == 'begin' and set(payload) == {'action'}:
                return journal.snapshot_begin(owner)
            if verb == 'page' and {'action', 'snapshot_id', 'after'} <= set(payload) <= {'action', 'snapshot_id', 'after', 'limit', 'max_bytes'}:
                return journal.snapshot_page(owner, payload['snapshot_id'], payload['after'],
                    payload.get('limit', 100), payload.get('max_bytes', MAX_BYTES))
            if verb == 'end' and set(payload) == {'action', 'snapshot_id'}:
                return journal.snapshot_end(owner, payload['snapshot_id'])
        raise ValueError('business_replication_request_invalid')

    api.dispatch = dispatch
    api._business_journal = journal
    return journal
