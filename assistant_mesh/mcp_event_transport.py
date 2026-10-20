"""Small outbound Standard Webhooks transport; no proxy, redirect or retries.

DNS is checked at each connection and the socket dials that numeric address,
while TLS/SNI and Host retain the callback hostname. Imports never use network.
"""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit


MAX_BODY = 262144


class CallbackError(ValueError):
    def __init__(self, reason, transient=False):
        super().__init__(reason)
        self.reason, self.transient = reason, transient


def signing_key(secret):
    if not isinstance(secret, str) or not secret.startswith('whsec_'):
        raise ValueError('invalid_webhook_secret')
    encoded = secret[6:]
    if not re.fullmatch('[A-Za-z0-9+/]+={0,2}', encoded):
        raise ValueError('invalid_webhook_secret')
    try:
        key = base64.b64decode(encoded + '=' * (-len(encoded) % 4), validate=True)
    except ValueError:
        raise ValueError('invalid_webhook_secret') from None
    if not 24 <= len(key) <= 64 or base64.b64encode(key).decode().rstrip('=') != encoded.rstrip('='):
        raise ValueError('invalid_webhook_secret')
    return key


def signed_headers(identifier, timestamp, body, subscription, secrets):
    if (not isinstance(body, bytes) or len(body) > MAX_BODY or type(timestamp) is not int
            or not isinstance(identifier, str) or not identifier or len(identifier) > 200
            or any(ord(c) < 33 or ord(c) > 126 for c in identifier)):
        raise ValueError('invalid_webhook_body')
    signed = identifier.encode() + b'.' + str(timestamp).encode() + b'.' + body
    signatures = ['v1,' + base64.b64encode(hmac.new(signing_key(key), signed, hashlib.sha256).digest()).decode()
                  for key in secrets]
    return {'Content-Type': 'application/json', 'webhook-id': identifier,
            'webhook-timestamp': str(timestamp), 'webhook-signature': ' '.join(signatures),
            'X-MCP-Subscription-Id': subscription}


def callback_url(url):
    try:
        if (not isinstance(url, str) or not 1 <= len(url) <= 2048
                or any(ord(c) < 33 or ord(c) > 126 for c in url) or '\\' in url):
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment or '#' in url or '%' in parsed.netloc):
            raise ValueError()
        host = parsed.hostname.lower()
        authority = '[' + host + ']' if ':' in host else host
        if parsed.netloc.lower() not in (authority, authority + ':443'):
            raise ValueError()
        if ':' not in host and not re.fullmatch('[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
            raise ValueError()
        target = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
        return host, authority, target
    except (ValueError, AttributeError):
        raise CallbackError('invalid_callback_url') from None


def _public(address):
    ip = ipaddress.ip_address(address)
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    if ip.version == 6:
        # Do not let translated/tunnel addresses hide non-public IPv4 targets.
        if ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None:
            return False
        if ip in ipaddress.ip_network('64:ff9b::/96') or ip in ipaddress.ip_network('64:ff9b:1::/48'):
            return False
    return True


def public_addresses(host):
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        raise CallbackError('dns_failed', transient=True) from None
    if not answers or any(family not in (socket.AF_INET, socket.AF_INET6) or not _public(address[0])
                          for family, unused_type, unused_protocol, unused_name, address in answers):
        raise CallbackError('non_public_callback')
    return answers


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        family, kind, protocol, unused_name, address = public_addresses(self.host)[0]
        remaining = getattr(self, '_deadline', time.monotonic() + 10) - time.monotonic()
        if remaining <= 0:
            raise CallbackError('timeout', transient=True)
        sock = socket.socket(family, kind, protocol)
        self._io_socket = sock
        try:
            sock.settimeout(min(self.timeout, remaining))
            sock.connect(address)  # Numeric validated sockaddr: no second DNS resolution.
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            self._io_socket = self.sock
            self.sock.do_handshake()
        except BaseException:
            sock.close()
            raise

    def abort(self):
        sock = getattr(self, '_io_socket', None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # Interrupt even an active makefile/slow read.
            except OSError:
                pass
        self.close()


class WebhookTransport:
    def post(self, url, body, headers):
        host, authority, target = callback_url(url)
        if not isinstance(body, bytes) or len(body) > MAX_BODY:
            raise CallbackError('payload_too_large')
        connection = _PinnedHTTPSConnection(host, 443, timeout=10, context=ssl.create_default_context())
        connection._deadline = time.monotonic() + 10
        expired = threading.Event()
        def deadline():
            expired.set(); connection.abort()
        timer = threading.Timer(10, deadline); timer.daemon = True; timer.start()
        try:
            connection.request('POST', target, body=body, headers=dict(headers, Host=authority))
            response = connection.getresponse()
            if response.status in (410, 413) or 300 <= response.status < 400:
                return response.status, b''
            data = response.read(65537)
            if expired.is_set():
                raise CallbackError('timeout', transient=True)
            if len(data) > 65536:
                raise CallbackError('callback_response_too_large')
            # http.client never follows Location, including for verification.
            return response.status, data
        except CallbackError:
            raise
        except TimeoutError:
            raise CallbackError('timeout', transient=True) from None
        except (OSError, http.client.HTTPException):
            raise CallbackError('connection_failed', transient=True) from None
        finally:
            timer.cancel()
            connection.close()
