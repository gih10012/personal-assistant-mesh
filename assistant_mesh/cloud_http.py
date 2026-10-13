"""Narrow JSON transport for an owner-created CloudMembership facade.

No Store/Worker construction, token minting, Cloud submission, public listener,
or retry happens here. A trusted installer supplies the existing facade and TLS
termination. This interface does not constrain any native Cloud tools.
"""
import ipaddress
import json
import ssl
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from .store import Conflict


DEFAULT_MAX_BYTES = 8 * 1024 * 1024
MAX_BYTES = 24 * 1024 * 1024


class CloudTransportError(RuntimeError):
    """Fixed category only; a failure does not authorize another submission."""


def _resource(url):
    try:
        parsed = urlsplit(url)
        valid = (isinstance(url, str) and len(url) <= 2048
                 and parsed.scheme == 'https' and parsed.hostname
                 and not parsed.username and not parsed.password
                 and parsed.port in (None, 443) and not parsed.query
                 and not parsed.fragment and parsed.path.startswith('/')
                 and parsed.path != '/' and '%' not in url and '\\' not in url
                 and '?' not in url and '#' not in url and '*' not in url
                 and not any(part in ('.', '..', '') for part in parsed.path.split('/')[1:])
                 and all(33 <= ord(char) < 127 for char in url))
        if not valid:
            raise ValueError('unsafe')
        host = parsed.hostname.lower()
        authority = '[' + host + ']' if ':' in host else host
        if parsed.netloc.lower() not in (authority, authority + ':443'):
            raise ValueError('unsafe')
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid_cloud_http_resource') from None
    return parsed.path, authority


def _limit(value):
    if type(value) is not int or not 65536 <= value <= MAX_BYTES:
        raise ValueError('invalid_cloud_http_limit')
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_cloud_http_key')
        result[key] = value
    return result


def _decode(raw):
    def reject_constant(_):
        raise ValueError('invalid_cloud_http_number')
    value = json.loads(raw.decode('utf8'), object_pairs_hook=_object,
                       parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError('invalid_cloud_http_object')
    # Reject lone surrogate code points, even when escaped in otherwise valid
    # JSON, before the facade can consume any operation intent.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf8')
    return value


def make_http_handler(membership, resource_url, *, trusted_proxy_peers=(),
                      max_request_bytes=DEFAULT_MAX_BYTES, timeout=15):
    """Build a handler without binding a socket or creating an authority.

    Direct connections must be TLS. Explicit loopback reverse-proxy peers may
    terminate TLS, but must preserve Host and replace X-Forwarded-Proto with one
    exact `https` value. Headers from any other peer cannot assert TLS. Do not
    expose the plain loopback listener or forward the general Mesh API.
    """
    if not callable(getattr(membership, 'handle', None)):
        raise ValueError('cloud_http_membership_required')
    path, authority = _resource(resource_url)
    maximum = _limit(max_request_bytes)
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise ValueError('invalid_cloud_http_timeout')
    if not isinstance(trusted_proxy_peers, (list, tuple, set, frozenset)):
        raise ValueError('invalid_cloud_http_proxy_peers')
    peers = set()
    for peer in trusted_proxy_peers:
        try:
            address = ipaddress.ip_address(peer)
            if not address.is_loopback:
                raise ValueError('unsafe')
            peers.add(str(address))
        except (TypeError, ValueError):
            raise ValueError('invalid_cloud_http_proxy_peers') from None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        server_version = 'MeshCloud'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(timeout)

        def log_message(self, *args):
            # Paths, raw parser errors, Authorization and response bodies must
            # not reach a generic public access/error log.
            pass

        def _reply(self, status, value):
            self.close_connection = True
            raw = json.dumps(value, ensure_ascii=True, allow_nan=False,
                             separators=(',', ':')).encode('utf8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)

        def _reject(self, status, code, *, dispatched=False):
            self._reply(status, {'error': code,
                                 'outcome': 'unconfirmed' if dispatched else 'not_attempted',
                                 'automatic_retry': False, 'retry_with_new_id': False})

        def send_error(self, code, message=None, explain=None):
            self._reject(code, 'cloud_http_parse_rejected')

        def do_POST(self):
            if self.path != path:
                return self._reject(404, 'cloud_http_route_rejected')
            if self.headers.get_all('Host') not in ([authority], [authority + ':443']):
                return self._reject(400, 'cloud_http_host_rejected')
            tls = isinstance(self.connection, ssl.SSLSocket) and bool(self.connection.cipher())
            if not tls:
                peer = str(ipaddress.ip_address(self.client_address[0]))
                forwarded = self.headers.get_all('X-Forwarded-Proto')
                if peer not in peers or forwarded != ['https']:
                    return self._reject(403, 'cloud_http_tls_required')
            if (self.headers.get_all('Origin') or self.headers.get_all('Cookie')
                    or self.headers.get_all('Transfer-Encoding')
                    or self.headers.get_all('Content-Encoding')
                    or self.headers.get_all('Expect')):
                return self._reject(400, 'cloud_http_headers_rejected')
            lengths = self.headers.get_all('Content-Length') or []
            if (len(lengths) != 1 or not 1 <= len(lengths[0]) <= 8
                    or not all('0' <= char <= '9' for char in lengths[0])):
                return self._reject(411, 'cloud_http_length_required')
            size = int(lengths[0])
            if not 1 <= size <= maximum:
                return self._reject(413, 'cloud_http_body_limit')
            if self.headers.get_all('Content-Type') not in (
                    ['application/json'], ['application/json; charset=utf-8']):
                return self._reject(415, 'cloud_http_json_required')
            authorization = self.headers.get_all('Authorization') or []
            if len(authorization) != 1 or not authorization[0].startswith('Bearer '):
                return self._reject(401, 'cloud_http_auth_required')
            token = authorization[0][7:]
            if (not 32 <= len(token) <= 4096
                    or any(ord(char) < 33 or ord(char) >= 127 for char in token)):
                return self._reject(401, 'cloud_http_auth_required')
            try:
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError('short_body')
                envelope = _decode(raw)
            except (ValueError, UnicodeError, RecursionError, TimeoutError, OSError):
                return self._reject(400, 'cloud_http_invalid_json')
            try:
                result = membership.handle(token, envelope)
            except PermissionError:
                return self._reject(403, 'cloud_http_request_rejected', dispatched=True)
            except (ValueError, Conflict):
                return self._reject(409, 'cloud_http_request_conflict', dispatched=True)
            except Exception:
                return self._reject(503, 'cloud_http_authority_unconfirmed', dispatched=True)
            try:
                self._reply(200, result)
            except (ValueError, UnicodeError, RecursionError):
                self._reject(503, 'cloud_http_response_unconfirmed', dispatched=True)

        def do_GET(self):
            self._reject(405, 'cloud_http_method_rejected')

        do_HEAD = do_GET
        do_PUT = do_GET
        do_PATCH = do_GET
        do_DELETE = do_GET
        do_OPTIONS = do_GET

    return Handler


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CloudHTTPClient:
    """One verified HTTPS request; respect the native Cloud proxy environment.

    The token is a separately delegated task-bound credential or Cloud network
    secret placeholder, never an operator bearer. An error is not proof that the
    operation did not happen. This client cannot retry or choose a different ID.
    """
    def __init__(self, resource_url, token, *, max_bytes=DEFAULT_MAX_BYTES, timeout=30):
        _resource(resource_url)
        # Cloud network-secret placeholders need not have the real bearer's
        # length. The trusted HTTPS proxy substitutes them at the destination;
        # the server still checks the separately issued strong credential.
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or any(ord(char) < 33 or ord(char) >= 127 for char in token)):
            raise ValueError('invalid_cloud_http_credential')
        self.url, self.token, self.max_bytes = resource_url, token, _limit(max_bytes)
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError('invalid_cloud_http_timeout')
        self.timeout = timeout
        # Default HTTPSHandler verifies both CA trust and hostname. Keep normal
        # ProxyHandler discovery: Codex Cloud VPN routes also use its proxy.
        self.opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler())

    def request(self, envelope):
        try:
            raw = json.dumps(envelope, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode('utf8')
            _decode(raw)
            if len(raw) > self.max_bytes:
                raise ValueError('large')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError('invalid_cloud_http_request') from None
        request = urllib.request.Request(self.url, data=raw, method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise CloudTransportError('cloud_http_outcome_unconfirmed')
                result = response.read(self.max_bytes + 1)
            if len(result) > self.max_bytes:
                raise ValueError('large')
            return _decode(result)
        except urllib.error.HTTPError as error:
            error.close()
            raise CloudTransportError('cloud_http_outcome_unconfirmed') from None
        except (OSError, ValueError, UnicodeError, RecursionError, urllib.error.URLError):
            raise CloudTransportError('cloud_http_outcome_unconfirmed') from None
