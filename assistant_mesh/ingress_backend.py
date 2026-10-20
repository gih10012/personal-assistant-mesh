"""Pure ingress backends shared by frozen receivers and modern MCP sidecars.

The existing backend classes are kept independent of SDK/Events imports. No
Store construction, listener, credentials, retries or work happens on import.
"""
from . import native_messaging as native
from .mcp_http_auth import NotConfigured, SOURCE
from .ingress import _identifier


class LocalIngressBackend:
    """Use existing owner-configured Ingress/Results; no new Store/recovery."""
    def __init__(self, ingress, results, *, source=SOURCE):
        if ingress is None or results is None or ingress.owner_id != results.owner_id:
            raise NotConfigured('mcp_http_ingress_not_configured')
        self.ingress, self.results = ingress, results
        self.source = _identifier(source, 'backend_source')

    def request(self, principal, action, payload):
        if principal['owner_id'] != self.ingress.owner_id or principal['source'] != self.source:
            raise PermissionError('mcp_http_ingress_owner_mismatch')
        peer = {'role': 'ingress', 'ingress': dict(principal)}
        if action == 'submit':
            return self.ingress.submit(peer, payload)
        if action == 'status':
            return self.ingress.status(peer, payload)
        if action == 'result':
            return self.results.read(peer, payload)
        raise PermissionError('mcp_http_action_rejected')


class RemoteIngressBackend:
    """Fixed owner-installed per-subject ingress clients, never operator RPC.

    Authoritative peer bindings must use role=ingress and this fixed source/
    subject. A Client path alone is not role proof: deployment checks the server
    configuration. OAuth access tokens are NEVER forwarded to the Mesh server.
    """
    def __init__(self, clients, owner_binding, *, source=SOURCE):
        if not isinstance(clients, dict) or not clients:
            raise NotConfigured('mcp_http_ingress_not_configured')
        self.clients, self.owner = dict(clients), owner_binding
        self.source = _identifier(source, 'backend_source')

    def request(self, principal, action, payload):
        if principal['owner_id'] != self.owner or principal['source'] != self.source:
            raise PermissionError('mcp_http_ingress_owner_mismatch')
        client = self.clients.get(principal['subject'])
        if client is None:
            raise PermissionError('mcp_http_ingress_subject_rejected')
        # Only these three exact routes are ever requested by this backend.
        if action not in native._ROUTES:
            raise PermissionError('mcp_http_action_rejected')
        return client.request(native._ROUTES[action], payload)
