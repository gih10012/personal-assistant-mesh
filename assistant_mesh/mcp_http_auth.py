"""Owner-bound authorization for an optional modern MCP resource sidecar.

No authorization server, login, token minting, account lookup, API key, or
operator credential exists here. TokenVerifier delegates signature/introspection
validation to an established IdP adapter, then restricts it to the configured
issuer/resource/owner subject and scopes. Pure policy parsing is SDK-independent.
"""
import json
import time
from urllib.parse import urlsplit

from .ingress import _identifier


FORMAT = 'mesh-mcp-http/1'
SOURCE = 'chatgpt-chat'
SCOPES = frozenset(('tasks:read', 'tasks:submit'))


class NotConfigured(ValueError):
    pass


def _url(value, *, loopback=False):
    _identifier(value, 'mcp_http_url', 2048)
    try:
        parsed = urlsplit(value)
        valid = (parsed.hostname and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment and '?' not in value
                 and '#' not in value and not any(c.isspace() for c in value)
                 and '\\' not in value and '*' not in value)
        secure = parsed.scheme == 'https'
        local = (loopback and parsed.scheme == 'http'
                 and parsed.hostname in ('127.0.0.1', 'localhost', '::1'))
        if not valid or not (secure or local):
            raise ValueError('unsafe')
        parsed.port
    except ValueError:
        raise ValueError('invalid_mcp_http_url') from None
    return value


def validate_policy(document, *, allow_loopback_for_tests=False):
    """Private deployment config, not data supplied by a tool/client/model.

    metadata is the existing IdP's published document, independently obtained by
    the owner. Validation checks shape/PKCE/issuer, not live login or consent.
    The explicit loopback switch is for isolated tests, never public config.
    """
    required = {'format', 'resource_url', 'issuer_url', 'issuer_metadata',
                'owner_binding', 'subjects'}
    optional = {'allowed_origins', 'max_request_bytes'}
    if document is None:
        raise NotConfigured('mcp_http_not_configured')
    if not isinstance(document, dict) or not required <= set(document) or set(document) - required - optional:
        raise ValueError('invalid_mcp_http_policy')
    try:
        detached = json.loads(json.dumps(document, ensure_ascii=False, allow_nan=False))
        json.dumps(detached, ensure_ascii=False, allow_nan=False).encode('utf8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('invalid_mcp_http_policy') from None
    if detached['format'] != FORMAT:
        raise ValueError('invalid_mcp_http_policy')
    resource = _url(detached['resource_url'], loopback=allow_loopback_for_tests)
    issuer = _url(detached['issuer_url'], loopback=allow_loopback_for_tests)
    if urlsplit(resource).path != '/mcp':
        raise ValueError('mcp_http_resource_path_must_be_mcp')
    _identifier(detached['owner_binding'], 'mcp_http_owner')
    metadata = detached['issuer_metadata']
    if (not isinstance(metadata, dict) or metadata.get('issuer') != issuer
            or not isinstance(metadata.get('code_challenge_methods_supported'), list)
            or 'S256' not in metadata['code_challenge_methods_supported']):
        raise NotConfigured('mcp_http_idp_pkce_not_configured')
    for field in ('authorization_endpoint', 'token_endpoint'):
        _url(metadata.get(field), loopback=allow_loopback_for_tests)
    methods = metadata.get('token_endpoint_auth_methods_supported')
    if not isinstance(methods, list) or not methods or not all(isinstance(x, str) for x in methods):
        raise NotConfigured('mcp_http_idp_client_auth_not_configured')
    # Leave client registration and callbacks to the existing IdP. This sidecar
    # accepts only owner-approved actual client IDs, regardless of DCR or CIMD.
    subjects = detached['subjects']
    if not isinstance(subjects, list) or not 1 <= len(subjects) <= 32:
        raise NotConfigured('mcp_http_owner_subject_not_configured')
    seen_subjects, seen_bindings = set(), set()
    for entry in subjects:
        if not isinstance(entry, dict) or set(entry) != {'issuer_subject', 'mesh_subject', 'client_ids', 'scopes'}:
            raise ValueError('invalid_mcp_http_subject')
        for field in ('issuer_subject', 'mesh_subject'):
            _identifier(entry[field], 'mcp_http_subject', 1024)
        if entry['issuer_subject'] in seen_subjects or entry['mesh_subject'] in seen_bindings:
            raise ValueError('duplicate_mcp_http_subject')
        seen_subjects.add(entry['issuer_subject'])
        seen_bindings.add(entry['mesh_subject'])
        clients, scopes = entry['client_ids'], entry['scopes']
        if not isinstance(clients, list) or not 1 <= len(clients) <= 32:
            raise NotConfigured('mcp_http_owner_client_not_configured')
        for client in clients:
            _identifier(client, 'mcp_http_client', 1024)
            if '*' in client:
                raise ValueError('invalid_mcp_http_client')
        if len(set(clients)) != len(clients):
            raise ValueError('duplicate_mcp_http_client')
        if (not isinstance(scopes, list) or not scopes
                or any(s not in SCOPES for s in scopes) or len(set(scopes)) != len(scopes)
                or 'tasks:read' not in scopes):
            raise ValueError('invalid_mcp_http_scopes')
    origins = detached.get('allowed_origins', [])
    if not isinstance(origins, list) or len(origins) > 16:
        raise ValueError('invalid_mcp_http_origins')
    for origin in origins:
        _url(origin, loopback=allow_loopback_for_tests)
        if urlsplit(origin).path not in ('', '/'):
            raise ValueError('invalid_mcp_http_origins')
    maximum = detached.get('max_request_bytes', 65536)
    if type(maximum) is not int or not 32768 <= maximum <= 262144:
        raise ValueError('invalid_mcp_http_request_limit')
    detached['allowed_origins'], detached['max_request_bytes'] = origins, maximum
    return detached


class OwnerTokenVerifier:
    """SDK TokenVerifier protocol; the delegate is trusted IdP integration.

    The delegate MUST verify signature/introspection and the configured expiry/
    revocation policy. A claims decoder is not a verifier. No delegate means not_configured, never
    anonymous mode or a production static-token fallback.
    """
    def __init__(self, delegate, policy, *, clock=time.time):
        if delegate is None or not callable(getattr(delegate, 'verify_token', None)):
            raise NotConfigured('mcp_http_token_verifier_not_configured')
        self.delegate, self.policy, self.clock = delegate, policy, clock

    def principal(self, token):
        claims = getattr(token, 'claims', None)
        if (not isinstance(claims, dict) or claims.get('iss') != self.policy['issuer_url']
                or getattr(token, 'resource', None) != self.policy['resource_url']
                or type(getattr(token, 'expires_at', None)) is not int
                or token.expires_at <= self.clock()):
            raise PermissionError('mcp_http_principal_rejected')
        entry = next((entry for entry in self.policy['subjects']
                      if entry['issuer_subject'] == getattr(token, 'subject', None)), None)
        scopes = getattr(token, 'scopes', None)
        if (entry is None or getattr(token, 'client_id', None) not in entry['client_ids']
                or not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes)):
            raise PermissionError('mcp_http_principal_rejected')
        approved = sorted(set(scopes) & set(entry['scopes']) & SCOPES)
        if 'tasks:read' not in approved:
            raise PermissionError('mcp_http_principal_rejected')
        return {'owner_id': self.policy['owner_binding'], 'source': SOURCE,
                'subject': entry['mesh_subject'], 'scopes': approved}

    async def verify_token(self, token):
        if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                or any(ord(char) < 33 or ord(char) == 127 for char in token)):
            return None
        try:
            verified = await self.delegate.verify_token(token)
            principal = self.principal(verified)
            from mcp.server.auth.provider import AccessToken
            # Keep only SDK session identity data, not private IdP claim bodies.
            return AccessToken(token=token, client_id=verified.client_id,
                               subject=verified.subject, scopes=principal['scopes'],
                               expires_at=verified.expires_at, resource=self.policy['resource_url'],
                               claims={'iss': self.policy['issuer_url']})
        except Exception:
            # No issuer URLs, JWTs, user identities, or raw network errors in logs.
            return None


class IdPJWTVerifier:
    """Optional PyJWT adapter for an existing issuer/JWKS, never an OAuth AS.

    Asymmetric algorithms only; issuer/resource/exp/sub/client identity are
    validated before returning SDK AccessToken. Online revocation/introspection
    depends on the issuer; JWT acceptance lasts until exp unless owner mapping
    is revoked. JWKS is owner-pinned config, not a token-selected URL.
    """
    def __init__(self, *, issuer_url, resource_url, jwks_url,
                 algorithms=('RS256',), client_id_claim='client_id'):
        self.issuer = _url(issuer_url)
        self.resource = _url(resource_url)
        self.jwks_url = _url(jwks_url)
        if (not isinstance(algorithms, (list, tuple)) or not algorithms
                or any(a not in ('RS256', 'RS384', 'RS512', 'ES256', 'ES384', 'ES512', 'EdDSA') for a in algorithms)
                or client_id_claim not in ('client_id', 'azp')):
            raise ValueError('invalid_mcp_http_jwt_configuration')
        import jwt
        self.jwt, self.algorithms, self.client_claim = jwt, list(algorithms), client_id_claim
        self.keys = jwt.PyJWKClient(self.jwks_url, timeout=10, cache_jwk_set=True, lifespan=300)

    def _verify(self, token):
        signing_key = self.keys.get_signing_key_from_jwt(token)
        claims = self.jwt.decode(token, signing_key.key, algorithms=self.algorithms,
                                 issuer=self.issuer, audience=self.resource,
                                 options={'require': ['exp', 'iss', 'sub', 'aud', self.client_claim]})
        if type(claims.get('exp')) is not int:
            return None
        subject = _identifier(claims['sub'], 'mcp_http_token_subject', 1024)
        client_id = _identifier(claims[self.client_claim], 'mcp_http_token_client', 1024)
        raw_scopes = claims.get('scope', '')
        if not isinstance(raw_scopes, str):
            return None
        from mcp.server.auth.provider import AccessToken
        return AccessToken(token=token, client_id=client_id, subject=subject,
                           scopes=raw_scopes.split(), expires_at=claims['exp'],
                           resource=self.resource, claims={'iss': self.issuer})

    async def verify_token(self, token):
        import asyncio
        try:
            return await asyncio.to_thread(self._verify, token)
        except Exception:
            return None
