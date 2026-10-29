"""Deployment-only receipt resolver; never accepts a receipt/config over RPC.

The old control namespace has no Worker Client. This narrow stdlib adapter
reuses its control_url/token_file private config contract without importing a
Worker, model, Store, business ledger or account integration.
"""
import json
import os
from pathlib import Path
import stat
import urllib.error
import urllib.request
from urllib.parse import urlsplit


PROTOCOL = 'owner-native-partial-continuation/1'
RECEIPT_PATH = '/v1/maintenance/native-partial/receipt'
MAX_BYTES = 256 * 1024


def _private(path, maximum):
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts or path.is_symlink()
            or any(parent.is_symlink() for parent in path.parents)):
        raise ValueError('runtime_retirement_private_file_required')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > maximum):
        raise ValueError('runtime_retirement_private_file_required')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('runtime_retirement_private_file_required')
    return raw


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('runtime_retirement_redirect_rejected')


class MaintenanceReceiptClient:
    def __init__(self, fixed_config):
        config = json.loads(_private(fixed_config, 65536).decode('utf8'))
        if not isinstance(config, dict) or set(config) != {'control_url', 'token_file'}:
            raise ValueError('runtime_retirement_client_config_invalid')
        parsed = urlsplit(config['control_url'])
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or (parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'))):
            raise ValueError('runtime_retirement_fixed_secure_authority_required')
        self.url = config['control_url'].rstrip('/')
        self.token_file = config['token_file']
        # Fail startup if deployment's dedicated credential is unavailable.
        self._token()

    def _token(self):
        token = _private(self.token_file, 4096).decode('utf8').strip()
        if not token or any(ord(character) < 33 or ord(character) == 127 for character in token):
            raise ValueError('runtime_retirement_private_token_invalid')
        return token

    def request(self, path, payload):
        if path != RECEIPT_PATH or not isinstance(payload, dict) or set(payload) != {'acceptance_id'}:
            raise ValueError('runtime_retirement_receipt_only_client')
        raw = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode('utf8')
        request = urllib.request.Request(self.url + RECEIPT_PATH, data=raw,
            headers={'Authorization': 'Bearer ' + self._token(), 'Content-Type': 'application/json'})
        # Preserve established direct Client semantics; no redirect, ambient
        # proxy, credential-bearing URL or alternate management route.
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=5) as response:
                value = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as error:
            error.close()
            raise ValueError('runtime_retirement_original_acceptance_unavailable') from None
        if len(value) > MAX_BYTES:
            raise ValueError('runtime_retirement_receipt_oversized')
        return json.loads(value.decode('utf8'))


def receipt_resolver(client):
    if not callable(getattr(client, 'request', None)):
        raise TypeError('runtime_retirement_trusted_client_required')

    def resolve(acceptance_id):
        if (not isinstance(acceptance_id, str) or not acceptance_id.strip()
                or len(acceptance_id.encode('utf8')) > 256
                or any(ord(character) < 32 for character in acceptance_id)):
            raise ValueError('runtime_retirement_acceptance_id_required')
        value = client.request(RECEIPT_PATH, {'acceptance_id': acceptance_id})
        if (not isinstance(value, dict) or value.get('protocol') != PROTOCOL
                or value.get('acceptance_id') != acceptance_id or value.get('accepted') is not True):
            raise ValueError('runtime_retirement_original_acceptance_not_confirmed')
        return value
    return resolve


def install(runtime, fixed_config):
    """Only the owner-installed launcher calls this with its embedded path."""
    runtime.RuntimeStore.retirement_resolver = staticmethod(receipt_resolver(MaintenanceReceiptClient(fixed_config)))
