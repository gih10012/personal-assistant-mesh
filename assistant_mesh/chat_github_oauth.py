"""Optional owner-only GitHub identity bridge for the existing MCP 2.3 server.

Code/token/refresh/PKCE/consent/state machinery belongs to pinned FastMCP's
OAuthProxy, not this module. GitHub only proves one stable public user ID; its
permissions NEVER become Mesh grants. No import reads keys, initializes state,
starts a listener, authenticates, or constructs Store. Production is opt-in.
"""
import contextlib
import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import native_messaging as native
from .mcp_http import create_server
from .mcp_http_auth import NotConfigured, SCOPES, validate_policy


FORMAT = 'mesh-chat-github-oauth/1'
FASTMCP_PIN = '4.1.0'
CALLBACK_PATH = '/auth/callback'


def _require(ok, code):
    if not ok:
        raise NotConfigured(code)


def validate_config(document, *, allow_loopback_for_tests=False):
    """Trusted private deployment configuration, never tool/model arguments."""
    _require(isinstance(document, dict) and set(document) ==
             {'format', 'policy', 'github', 'state', 'redirect_uris'}, 'chat_oauth_config_invalid')
    try:
        value = json.loads(json.dumps(document, ensure_ascii=False, allow_nan=False))
    except Exception:
        raise NotConfigured('chat_oauth_config_invalid') from None
    _require(value['format'] == FORMAT, 'chat_oauth_config_invalid')
    policy = validate_policy(value['policy'], allow_loopback_for_tests=allow_loopback_for_tests)
    value['policy'] = policy
    url = urlsplit(policy['resource_url'])
    origin = '{}://{}'.format(url.scheme, url.netloc)
    _require(url.port in (None, 443) or allow_loopback_for_tests,
             'chat_oauth_public_origin_invalid')
    # The pinned provider canonicalizes a bare origin to a trailing slash.
    # Require that SAME issuer in metadata/JWT/policy; never weaken exact iss.
    _require(policy['issuer_url'] == origin + '/'
             and policy['issuer_metadata']['authorization_endpoint'] == origin + '/authorize'
             and policy['issuer_metadata']['token_endpoint'] == origin + '/token',
             'chat_oauth_existing_resource_issuer_mismatch')
    github, storage = value['github'], value['state']
    _require(isinstance(github, dict) and set(github) == {'client_id', 'client_secret_file', 'owner_id'}
             and isinstance(github['owner_id'], str) and re.fullmatch(r'[1-9][0-9]{0,19}', github['owner_id'])
             and isinstance(github['client_id'], str) and re.fullmatch(r'[A-Za-z0-9_.-]{8,200}', github['client_id']),
             'chat_oauth_owner_identity_required')
    subjects = policy['subjects']
    _require(len(subjects) == 1 and subjects[0]['issuer_subject'] == github['owner_id'],
             'chat_oauth_fixed_owner_subject_required')
    _require(isinstance(storage, dict) and set(storage) ==
             {'directory', 'jwt_key_file', 'encryption_key_file'}, 'chat_oauth_persistent_state_required')
    for path in (github['client_secret_file'], storage['directory'], storage['jwt_key_file'], storage['encryption_key_file']):
        _require(isinstance(path, str) and Path(path).is_absolute() and '..' not in Path(path).parts,
                 'chat_oauth_private_reference_invalid')
    redirects = value['redirect_uris']
    _require(isinstance(redirects, list) and 1 <= len(redirects) <= 8 and len(set(redirects)) == len(redirects),
             'chat_oauth_exact_redirects_required')
    for redirect in redirects:
        parsed = urlsplit(redirect)
        real = (parsed.scheme == 'https' and parsed.hostname == 'chatgpt.com' and parsed.port in (None, 443)
                and (parsed.path == '/connector_platform_oauth_redirect'
                     or re.fullmatch(r'/connector/oauth/[A-Za-z0-9_-]+', parsed.path)))
        synthetic = (allow_loopback_for_tests and parsed.scheme == 'http'
                     and parsed.hostname in ('127.0.0.1', 'localhost'))
        _require((real or synthetic) and not parsed.username and not parsed.password and not parsed.query
                 and not parsed.fragment and not any(c.isspace() for c in redirect) and '*' not in redirect,
                 'chat_oauth_exact_redirects_required')
    return value


def _private_state(path):
    try:
        from .enrollment import _private_directory
        _private_directory(path, create=False)  # Existing owned 0700, safe ancestors.
    except NotConfigured:
        raise
    except Exception:
        raise NotConfigured('chat_oauth_private_state_unavailable') from None


def _secret(path, *, minimum=32):
    try:
        raw = native._private_bytes(path).strip()
        _require(minimum <= len(raw) <= 4096, 'chat_oauth_private_secret_unavailable')
        return raw
    except Exception:
        raise NotConfigured('chat_oauth_private_secret_unavailable') from None


class _PrivateOAuthLog(logging.Filter):
    """Keep severity, never library-format a code/token/profile/exception."""
    def filter(self, record):
        record.msg = 'OAuth component event; private details suppressed'
        record.args, record.exc_info, record.exc_text, record.stack_info = (), None, None, None
        return True


def _private_component_logs():
    for name in tuple(logging.Logger.manager.loggerDict):
        if name.startswith(('fastmcp.server.auth', 'mcp.server.auth')):
            logger = logging.getLogger(name)
            if not any(isinstance(value, _PrivateOAuthLog) for value in logger.filters):
                logger.addFilter(_PrivateOAuthLog())


def create_app(document, *, backend, allow_loopback_for_tests=False, http_client=None):
    """Owner-installed factory; not a public daemon or GitHub App creator.

    http_client is only an owner-code seam for a synthetic IdP transport in
    tests; no endpoint, claims or verifier is selected by client JSON.
    """
    config = validate_config(document, allow_loopback_for_tests=allow_loopback_for_tests)
    policy = config['policy']
    try:
        from contextvars import ContextVar
        from importlib.metadata import version
        _require(version('fastmcp-slim') == FASTMCP_PIN, 'chat_oauth_component_pin_missing')
        from cryptography.fernet import Fernet
        from fastmcp.server.auth import OAuthProxy, TokenVerifier
        from key_value.aio.stores.filetree import FileTreeStore
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
        from mcp.server.auth.provider import AccessToken, AuthorizeError, TokenError
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route
        import httpx2
    except ImportError:
        raise NotConfigured('chat_oauth_component_not_installed') from None
    state = config['state']
    _private_state(state['directory'])
    references = [config['github']['client_secret_file'], state['jwt_key_file'], state['encryption_key_file']]
    _require(len(set(references)) == 3, 'chat_oauth_distinct_private_keys_required')
    try:
        github_secret = _secret(config['github']['client_secret_file']).decode('ascii')
    except UnicodeError:
        raise NotConfigured('chat_oauth_private_secret_unavailable') from None
    jwt_key = _secret(state['jwt_key_file'])
    storage_key = _secret(state['encryption_key_file'])
    _require(len({github_secret.encode('ascii'), jwt_key, storage_key}) == 3,
             'chat_oauth_distinct_private_keys_required')
    owner_id = config['github']['owner_id']
    approved_clients = frozenset(policy['subjects'][0]['client_ids'])
    approved_scopes = frozenset(policy['subjects'][0]['scopes']) & SCOPES
    origin = policy['resource_url'][:-len('/mcp')]

    class PublicOwnerIdentity(TokenVerifier):
        # These are RESOURCE scopes for the proxy's client consent/metadata,
        # not GitHub permissions. The upstream authorize scope is always empty.
        def __init__(self):
            super().__init__(required_scopes=['tasks:read'])

        async def verify_token(self, token):
            try:
                async with (contextlib.nullcontext(http_client) if http_client is not None
                            else httpx2.AsyncClient(timeout=10, follow_redirects=False)) as client:
                    response = await client.get('https://api.github.com/user', headers={
                        'Authorization': 'Bearer ' + token,
                        'Accept': 'application/vnd.github+json', 'User-Agent': 'Personal-Assistant-Mesh-Identity'})
                # A dedicated no-scope OAuth App only. Never query repos/email,
                # infer 'user', or accept a missing/broader scope header.
                if (response.status_code != 200 or len(response.content) > 65536
                        or response.headers.get('x-oauth-scopes') != ''):
                    return None
                value = response.json()
                if type(value.get('id')) is not int or str(value['id']) != owner_id:
                    return None
                return AccessToken(token=token, client_id='upstream-public-identity-only',
                    subject=owner_id, scopes=[], claims={'sub': owner_id})
            except Exception:
                return None

    class OwnerIdentityProxy(OAuthProxy):
        """Only identity/resource-scope translation; all OAuth machinery is super."""
        def __init__(self, **kwargs):
            self.requested_mesh_scopes = ContextVar('mesh_oauth_scopes', default=None)
            super().__init__(**kwargs)

        def granted(self, client_id, requested, previously=None):
            if (client_id not in approved_clients or not isinstance(requested, list)
                    or not set(requested) <= approved_scopes or 'tasks:read' not in requested
                    or (previously is not None and not set(requested) <= set(previously))):
                raise TokenError('invalid_scope', 'Owner-approved resource scopes required')
            return list(requested)

        async def get_client(self, client_id):
            # Pin CIMD before its URL is fetched. A DCR-generated ID must first
            # be independently owner-approved; registration is not a Mesh grant.
            if client_id not in approved_clients:
                return None
            return await super().get_client(client_id)

        async def authorize(self, client, params):
            if params.resource != policy['resource_url']:
                raise AuthorizeError('invalid_request', 'Exact Mesh resource required')
            return await super().authorize(client, params)

        def _translate_scopes_from_idp(self, scopes):
            requested = self.requested_mesh_scopes.get()
            if requested is None or (scopes and not set(scopes) <= set(requested)):
                raise TokenError('invalid_scope', 'Identity and resource scopes are separate')
            return list(requested)

        def _prepare_scopes_for_upstream_refresh(self, scopes):
            return []  # Do not forward Mesh scopes, offline_access or GitHub permissions.

        async def _extract_upstream_claims(self, tokens):
            identity = await self._token_validator.verify_token(tokens.get('access_token', ''))
            if identity is None:
                raise TokenError('invalid_grant', 'Owner identity unavailable')
            return {'owner_subject': identity.subject}  # No profile/email/full user_data.

        async def exchange_authorization_code(self, client, authorization_code):
            scope_token = self.requested_mesh_scopes.set(self.granted(client.client_id, authorization_code.scopes))
            try:
                return await super().exchange_authorization_code(client, authorization_code)
            finally:
                self.requested_mesh_scopes.reset(scope_token)

        async def exchange_refresh_token(self, client, refresh_token, scopes):
            granted = self.granted(client.client_id, scopes, refresh_token.scopes)
            scope_token = self.requested_mesh_scopes.set(granted)
            try:
                return await super().exchange_refresh_token(client, refresh_token, granted)
            finally:
                self.requested_mesh_scopes.reset(scope_token)

        async def _try_transparent_refresh(self, upstream):
            scopes = upstream.scope.split() if upstream.scope else []
            scope_token = self.requested_mesh_scopes.set(self.granted(upstream.client_id, scopes))
            try:
                return await super()._try_transparent_refresh(upstream)
            finally:
                self.requested_mesh_scopes.reset(scope_token)

    try:
        fernet = Fernet(storage_key)
    except Exception:
        raise NotConfigured('chat_oauth_private_secret_unavailable') from None
    _private_component_logs()  # Only this optional owner-installed auth integration.
    encrypted = FernetEncryptionWrapper(key_value=FileTreeStore(data_directory=Path(state['directory'])),
                                         fernet=fernet)
    provider = OwnerIdentityProxy(
        upstream_authorization_endpoint='https://github.com/login/oauth/authorize',
        upstream_token_endpoint='https://github.com/login/oauth/access_token',
        upstream_client_id=config['github']['client_id'], upstream_client_secret=github_secret,
        token_verifier=PublicOwnerIdentity(), base_url=origin, issuer_url=policy['issuer_url'],
        redirect_path=CALLBACK_PATH, valid_scopes=sorted(approved_scopes),
        extra_authorize_params={'scope': ''}, forward_pkce=True, forward_resource=False,
        allowed_client_redirect_uris=config['redirect_uris'], client_storage=encrypted,
        jwt_signing_key=jwt_key, require_authorization_consent=True,
        fastmcp_access_token_expiry_seconds=900, enable_cimd=True)
    auth_routes = provider.get_routes(mcp_path='/mcp')
    # FileTree's async get/delete is not an atomic authorization-code consume.
    # Serialize the EXISTING component's full token handler, including its SDK
    # load/PKCE/exchange/refresh sequence. This reference is ONE active process,
    # ONE worker. It is not a distributed lock or a replacement OAuth engine.
    token_gate = asyncio.Lock()
    def serialized_token_route(route):
        endpoint = route.app  # May be CORS ASGI middleware, not a Request callable.
        class GuardedTokenEndpoint:
            async def __call__(self, scope, receive, send):
                if scope['method'] != 'POST':  # Preserve component CORS OPTIONS.
                    return await endpoint(scope, receive, send)
                messages, size = [], 0
                while True:
                    message = await receive()
                    if message['type'] != 'http.request':
                        return
                    size += len(message.get('body', b''))
                    if size > policy['max_request_bytes']:
                        return await JSONResponse({'error': 'invalid_request'}, status_code=413)(scope, receive, send)
                    messages.append(message)
                    if not message.get('more_body', False):
                        break
                def replay():
                    index = 0
                    async def next_message():
                        nonlocal index
                        if index < len(messages):
                            value = messages[index]
                            index += 1
                            return value
                        return await receive()
                    return next_message
                try:
                    form = await Request(scope, receive=replay()).form()
                except Exception:
                    return await JSONResponse({'error': 'invalid_request'}, status_code=400)(scope, receive, send)
                # SDK 2.3 ignores token-request resource in code/refresh exchange.
                # Reject before consuming code; replay exact bounded original
                # messages into the UNCHANGED component's ASGI/CORS handler.
                if form.getlist('resource') != [policy['resource_url']]:
                    return await JSONResponse({'error': 'invalid_target'}, status_code=400)(scope, receive, send)
                async with token_gate:
                    return await endpoint(scope, replay(), send)
        return Route(route.path, endpoint=GuardedTokenEndpoint(), methods=route.methods,
                     name=route.name, include_in_schema=route.include_in_schema)
    auth_routes = [serialized_token_route(route) if route.path == '/token' else route
                   for route in auth_routes]

    class ProxyResourceVerifier:
        async def verify_token(self, token):
            try:
                # These are established component signature/use/issuer/audience
                # checks, not a JWT decode or fabricated claims fixture.
                claims = provider.jwt_issuer.verify_token(token)
                identity = await provider.load_access_token(token)
                if (identity is None or identity.subject != owner_id
                        or claims.get('client_id') not in approved_clients
                        or claims.get('iss') != policy['issuer_url']
                        or claims.get('aud') != policy['resource_url']
                        or not isinstance(claims.get('scope'), str)
                        or type(claims.get('exp')) is not int):
                    return None
                scopes = claims['scope'].split()
                if not set(scopes) <= approved_scopes or 'tasks:read' not in scopes:
                    return None
                return AccessToken(token=token, client_id=claims['client_id'], subject=owner_id,
                    scopes=scopes, expires_at=claims['exp'], resource=policy['resource_url'],
                    claims={'iss': policy['issuer_url']})
            except Exception:
                return None

    server, resource_app = create_server(policy, token_verifier=ProxyResourceVerifier(), backend=backend,
                                         allow_loopback_for_tests=allow_loopback_for_tests)
    # Keep the existing resource's metadata/challenge and three tools. Only the
    # established AS routes are mounted; no generic Mesh/Store API appears here.
    auth_routes = [route for route in auth_routes if not route.path.startswith('/.well-known/oauth-protected-resource')]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with resource_app.router.lifespan_context(resource_app):
            yield

    app = Starlette(routes=auth_routes + [Mount('/', app=resource_app)], lifespan=lifespan)
    return provider, server, app
