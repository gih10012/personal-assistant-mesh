"""Scoped interactive routes shared by the current and frozen API factories.

Owner code attaches this to its existing API and Store; no receiver, client,
business ledger or credential is constructed by the adapter.
"""
from urllib.parse import urlsplit


ACTIONS = {
    '/v1/ingress/mesh/connect': 'connect',
    '/v1/ingress/mesh/discover': 'discover',
    '/v1/ingress/mesh/node_update': 'node_update',
    '/v1/ingress/mesh/work_pull': 'work_pull',
    '/v1/ingress/mesh/work_report': 'work_report',
}
AUTHORIZE = '/v1/session-nodes/authorize'


def install(api, config):
    fields = {'subjects', 'mesh_id', 'ttl_seconds', 'work_lease_seconds', 'node_bindings'}
    if (not isinstance(config, dict) or 'subjects' not in config
            or set(config) - fields):
        raise ValueError('invalid_session_nodes_config')
    if api.ingress is None or api.ingress_results is None:
        raise ValueError('session_nodes_require_original_ingress')
    from .ingress_backend import LocalIngressBackend
    from .session_nodes import LocalSessionNodes
    service = LocalSessionNodes(LocalIngressBackend(api.ingress, api.ingress_results), **config)
    api.session_nodes = service
    return service


def dispatch(api, method, path, payload, peer):
    """Return NotImplemented for the unchanged original API dispatch chain."""
    parsed = urlsplit(path)
    service = getattr(api, 'session_nodes', None)
    if parsed.path in ACTIONS:
        if (peer.get('role') != 'ingress' or method != 'POST'
                or parsed.query or parsed.fragment or service is None):
            raise PermissionError('session_nodes_route_not_authorized')
        return service.request(peer['ingress'], ACTIONS[parsed.path], payload)
    if parsed.path == AUTHORIZE:
        if (peer.get('role') != 'operator' or method != 'POST'
                or parsed.query or parsed.fragment or service is None):
            raise PermissionError('session_work_authorization_rejected')
        required = {'subject', 'request_id', 'operation_id'}
        optional = {'conversation_id', 'task_id', 'expires_at'}
        if (not isinstance(payload, dict) or not required <= set(payload)
                or set(payload) - required - optional):
            raise ValueError('invalid_session_work_authorization')
        return service.authorize_work(authorized=True, **payload)
    return NotImplemented


def attach(api, config):
    """Wrap a frozen API's ready hook, preserving its original dispatch chain."""
    if getattr(api, '_session_node_routes_attached', False):
        raise ValueError('session_node_routes_already_attached')
    service = install(api, config)
    previous = api.dispatch

    def composed(method, path, payload, peer):
        result = dispatch(api, method, path, payload, peer)
        return previous(method, path, payload, peer) if result is NotImplemented else result

    api.dispatch = composed
    api._session_node_routes_attached = True
    return service
