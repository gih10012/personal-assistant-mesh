"""Optional MCP SDK 2.3 Streamable HTTP resource sidecar, Python >=3.10.

The legacy core/stdio entrance remains dependency-free and unchanged. Import
does not install packages, open Store, read auth, start a listener, or call an
API. An owner factory supplies existing ingress/backend + established verifier.
"""
import asyncio
import json
from urllib.parse import urlsplit

from . import native_messaging as native
from .mcp_http_auth import OwnerTokenVerifier, NotConfigured, SOURCE, validate_policy


SDK_PIN = '2.3.0'
INSTRUCTIONS = (
    'These are extra owner-bound Mesh capabilities; native tools remain available. '
    'Persist one original request_id before submitting. Use that same ID for status '
    'and approved result; never change it or replay automatically after unknown. '
    'Only this authenticated source can read its tasks. Project/agent labels do '
    'not choose authorization or native sessions. Completed status is not independent '
    'execution proof. Results contain only separately owner-approved publications.'
)


class LocalIngressBackend:
    """Use existing owner-configured Ingress/Results; no new Store/recovery."""
    def __init__(self, ingress, results):
        if ingress is None or results is None or ingress.owner_id != results.owner_id:
            raise NotConfigured('mcp_http_ingress_not_configured')
        self.ingress, self.results = ingress, results

    def request(self, principal, action, payload):
        if principal['owner_id'] != self.ingress.owner_id or principal['source'] != SOURCE:
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
    def __init__(self, clients, owner_binding):
        if not isinstance(clients, dict) or not clients:
            raise NotConfigured('mcp_http_ingress_not_configured')
        self.clients, self.owner = dict(clients), owner_binding

    def request(self, principal, action, payload):
        if principal['owner_id'] != self.owner or principal['source'] != SOURCE:
            raise PermissionError('mcp_http_ingress_owner_mismatch')
        client = self.clients.get(principal['subject'])
        if client is None:
            raise PermissionError('mcp_http_ingress_subject_rejected')
        # Only these three exact routes are ever requested by this backend.
        if action not in native._ROUTES:
            raise PermissionError('mcp_http_action_rejected')
        return client.request(native._ROUTES[action], payload)


def dispatch(backend, verifier, token, action, payload):
    """Closed approved projection and one call; no automatic retries."""
    try:
        principal = verifier.principal(token)
        if action not in ('submit', 'status', 'result'):
            raise PermissionError('mcp_http_action_rejected')
        required = 'tasks:submit' if action == 'submit' else 'tasks:read'
        if required not in principal['scopes']:
            return {'schema': 1, 'ok': False, 'error': 'insufficient_scope',
                    'outcome': 'not_attempted', 'retry_with_new_id': False}
        _, arguments = native.validate_message({'schema': 1, 'action': action, 'payload': payload})
        response = backend.request(principal, action, arguments)
        return native.project_response(action, arguments['request_id'], response)
    except PermissionError:
        return {'schema': 1, 'ok': False, 'error': 'not_authorized_or_not_visible',
                'outcome': 'rejected', 'retry_with_new_id': False}
    except native.NativeMessageError:
        # An invalid authority response may follow an accepted submission.
        return {'schema': 1, 'ok': False, 'error': 'request_or_response_unknown',
                'outcome': 'unknown', 'retry_with_new_id': False}
    except Exception:
        return {'schema': 1, 'ok': False, 'error': 'authority_response_unknown',
                'outcome': 'unknown', 'retry_with_new_id': False}


def create_server(config, *, token_verifier=None, backend=None,
                  allow_loopback_for_tests=False):
    """Build SDK server+ASGI app; does not bind a TCP/public listener.

    Root supplies only a trusted verifier and already owner-bound backend. There
    is deliberately no unauthenticated/static-token production fallback.
    """
    policy = validate_policy(config, allow_loopback_for_tests=allow_loopback_for_tests)
    if backend is None or not callable(getattr(backend, 'request', None)):
        raise NotConfigured('mcp_http_ingress_not_configured')
    verifier = OwnerTokenVerifier(token_verifier, policy)
    try:
        from importlib.metadata import version
        if version('mcp') != SDK_PIN:
            raise NotConfigured('mcp_http_sdk_pin_not_installed')
        from mcp.server import MCPServer
        from mcp.server.auth.middleware.auth_context import get_access_token
        from mcp.server.auth.settings import AuthSettings
        from mcp.server.transport_security import TransportSecuritySettings
        from mcp.shared.exceptions import MCPError
        from mcp_types import ToolAnnotations, CallToolResult, TextContent
    except ImportError:
        raise NotConfigured('mcp_http_sdk_not_installed') from None
    class NarrowArguments:
        async def __call__(self, context, call_next):
            if context.method == 'tools/call':
                params = context.params or {}
                actions = {'mesh_submit_task': 'submit', 'mesh_task_status': 'status',
                           'mesh_approved_result': 'result'}
                action = actions.get(params.get('name'))
                if action is not None:
                    try:
                        native.validate_message({'schema': 1, 'action': action,
                                                 'payload': params.get('arguments', {})})
                    except Exception:
                        # Run before SDK function argument models can ignore an
                        # unknown key. No caller-supplied owner/role/session.
                        raise MCPError(code=-32602, message='Invalid Mesh tool arguments') from None
            result = await call_next(context)
            if context.method == 'tools/list':
                # The core MCP Tool model does not declare OpenAI's additional
                # top-level securitySchemes. Use the SDK's public middleware
                # dict result seam, retaining the compatibility _meta mirror.
                result = result.model_dump(by_alias=True, exclude_none=True) if hasattr(result, 'model_dump') else result
                for descriptor in result.get('tools', []):
                    scopes = ['tasks:read', 'tasks:submit'] if descriptor['name'] == 'mesh_submit_task' else ['tasks:read']
                    descriptor['securitySchemes'] = [{'type': 'oauth2', 'scopes': scopes}]
                    descriptor.setdefault('_meta', {})['securitySchemes'] = descriptor['securitySchemes']
            return result

    server = MCPServer('personal-assistant-mesh-chat', version='0.1.0',
                       instructions=INSTRUCTIONS, token_verifier=verifier,
                       auth=AuthSettings(issuer_url=policy['issuer_url'],
                                         resource_server_url=policy['resource_url'],
                                         required_scopes=['tasks:read'],
                                         validate_token_resource=True),
                       log_level='WARNING', subscriptions=False, middleware=[NarrowArguments()])

    async def call(action, arguments):
        # Snapshot this HTTP request's SDK context, never a JSON-supplied peer.
        token = get_access_token()
        result = await asyncio.to_thread(dispatch, backend, verifier, token, action, arguments)
        meta = None
        if result.get('error') == 'insufficient_scope':
            allowed = next((entry for entry in policy['subjects'] if entry['issuer_subject'] == token.subject), None)
            required = 'tasks:submit' if action == 'submit' else 'tasks:read'
            if allowed and required in allowed['scopes']:
                from mcp.server.auth.routes import build_resource_metadata_url
                from pydantic import AnyHttpUrl
                metadata_url = build_resource_metadata_url(AnyHttpUrl(policy['resource_url']))
                meta = {'mcp/www_authenticate': [
                    'Bearer resource_metadata="{}", error="insufficient_scope", '
                    'error_description="Owner-approved scope requires OAuth consent", '
                    'scope="tasks:read {}"'.format(metadata_url, required)]}
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(result, ensure_ascii=False))],
                              structured_content=result, is_error=not result['ok'], _meta=meta)

    @server.tool(name='mesh_submit_task', description='Submit one owner-authorized Mesh task with a durable original request_id. Never auto-retry unknown.',
                 annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                             idempotent_hint=True, open_world_hint=True),
                 meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['tasks:read', 'tasks:submit']}]})
    async def submit(request_id: str, input: str, project_id: str = None, agent_id: str = None):
        arguments = {'request_id': request_id, 'input': input}
        if project_id is not None:
            arguments['project_id'] = project_id
        if agent_id is not None:
            arguments['agent_id'] = agent_id
        return await call('submit', arguments)

    @server.tool(name='mesh_task_status', description='Read only this authenticated entrance original request metadata; no task/global/history lookup.',
                 annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=False),
                 meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['tasks:read']}]})
    async def status(request_id: str):
        return await call('status', {'request_id': request_id})

    @server.tool(name='mesh_approved_result', description='Read only an original request owner-approved immutable summary/public references, never raw native output.',
                 annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=False),
                 meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['tasks:read']}]})
    async def result(request_id: str):
        return await call('result', {'request_id': request_id})

    host = urlsplit(policy['resource_url']).netloc
    app = server.streamable_http_app(
        streamable_http_path='/mcp', json_response=True, stateless_http=True,
        max_request_body_size=policy['max_request_bytes'],
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[host],
            allowed_origins=policy['allowed_origins']))
    return server, app
