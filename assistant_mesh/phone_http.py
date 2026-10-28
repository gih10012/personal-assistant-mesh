"""Exact phone routes on a loopback listener sharing the live receiver API."""
import ipaddress
import ssl
import threading
from urllib.parse import urlsplit

from .cloud_http import make_http_handler
from .phone_authority import PhoneAuthority
from .phone_updates import MANIFEST, ARTIFACT, _origin
from .server import ThreadingHTTPServer


class _Endpoint:
    def __init__(self, handle):
        self.handle = handle


def handler(service, origin, *, trusted_proxy_peers=()):
    origin = _origin(origin)
    authority = urlsplit(origin).netloc
    node = make_http_handler(_Endpoint(service.node), origin + '/phone/node',
                             trusted_proxy_peers=trusted_proxy_peers, max_request_bytes=128 * 1024)
    enrollment = make_http_handler(_Endpoint(service.enrollment), origin + '/enrollment/node',
                                   trusted_proxy_peers=trusted_proxy_peers, max_request_bytes=1024 * 1024)
    proxies = set(trusted_proxy_peers)  # validated by make_http_handler

    class Handler(node):
        server_version = 'MeshPhone'

        def do_POST(self):
            if self.path == '/enrollment/node':
                return enrollment.do_POST(self)
            return node.do_POST(self)

        def do_GET(self):
            if self.path != MANIFEST and not self.path.startswith(ARTIFACT):
                return self._reject(404, 'phone_route_rejected')
            if self.headers.get_all('Host') not in ([authority], [authority + ':443']):
                return self._reject(400, 'phone_host_rejected')
            tls = isinstance(self.connection, ssl.SSLSocket) and bool(self.connection.cipher())
            if not tls and (str(ipaddress.ip_address(self.client_address[0])) not in proxies
                            or self.headers.get_all('X-Forwarded-Proto') != ['https']):
                return self._reject(403, 'phone_tls_required')
            if any(self.headers.get_all(k) for k in ('Origin', 'Cookie', 'Transfer-Encoding', 'Content-Encoding', 'Expect')):
                return self._reject(400, 'phone_headers_rejected')
            if self.headers.get_all('Content-Length') not in (None, ['0']):
                return self._reject(400, 'phone_get_body_rejected')
            auth = self.headers.get_all('Authorization') or []
            if len(auth) != 1 or not auth[0].startswith('Bearer '):
                return self._reject(401, 'phone_auth_required')
            try:
                service.principal(auth[0][7:], 'phone:updates')
                if service.updates is None:
                    return self._reject(503, 'phone_updates_not_configured')
                raw = service.updates.manifest() if self.path == MANIFEST else service.updates.artifact(self.path[len(ARTIFACT):])
            except PermissionError:
                return self._reject(403, 'phone_request_rejected')
            except FileNotFoundError:
                return self._reject(404, 'phone_update_not_available')
            except ValueError:
                return self._reject(409, 'phone_update_invalid')
            except Exception:
                return self._reject(503, 'phone_update_unconfirmed')
            self.close_connection = True
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if self.path == MANIFEST else 'application/octet-stream')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)

        # The base aliases reference its original do_GET, preserving rejection
        # of all non-contract methods instead of dispatching artifact reads.

    return Handler


def start(api, config_path, *, origin, port, relays=(), updates=None,
          trusted_proxy_peers=('127.0.0.1',)):
    """Call last in the incumbent ready hook. The owner controls lifecycle.

    Port is explicit; binding is always loopback. Incumbent dispatch remains in
    place with one private full-result reader. No production API is proxied.
    """
    if hasattr(api, 'phone_authority') or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('invalid_phone_listener_installation')
    service = PhoneAuthority(api, config_path, relays=relays, updates=updates)
    previous = api.dispatch

    def dispatch(method, path, payload, peer):
        if path.split('?', 1)[0] == '/v1/phone/result':
            if method != 'POST' or path != '/v1/phone/result' or peer.get('role') not in ('operator', 'worker'):
                raise PermissionError('phone_result_route_not_authorized')
            actor = 'operator' if peer['role'] == 'operator' else 'node:' + peer.get('node', '')
            if not isinstance(payload, dict) or set(payload) != {'operation_id', 'capability_id'}:
                raise ValueError('invalid_phone_result_request')
            return service.read_result(actor, **payload)
        return previous(method, path, payload, peer)

    server = ThreadingHTTPServer(('127.0.0.1', port), handler(service, origin, trusted_proxy_peers=trusted_proxy_peers))
    api.phone_authority, api.dispatch = service, dispatch
    from .phone_owner import install_dispatch
    try:
        install_dispatch(api, service, origin)
    except Exception:
        api.dispatch = previous
        del api.phone_authority
        server.server_close()
        raise
    thread = threading.Thread(target=server.serve_forever, name='amita-phone-http', daemon=True)
    thread.start()
    return server, service
