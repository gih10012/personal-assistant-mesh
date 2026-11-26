"""Huawei system notifications for the already enrolled owner phone."""
import base64
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import private_json
from .ingress import _identifier
from .remote_notifications import request_id, text_fingerprint
from .store import Conflict


FORMAT = 'amita-harmony-owner-push/1'
STATUS = 'mesh_phone_push_status'
BIND = 'mesh_phone_push_bind'
ROUTE = '/v1/phone/push'
DELIVERY = 'phone_push'
AUDIENCE = 'https://oauth-login.cloud.huawei.com/oauth2/v3/token'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=')


def service_jwt(account, now):
    kid, issuer, key = (account.get(name) for name in ('key_id', 'sub_account', 'private_key'))
    if (any(not isinstance(value, str) or not 1 <= len(value) <= 160 for value in (kid, issuer))
            or not isinstance(key, str) or len(key) > 12000 or 'PRIVATE KEY' not in key):
        raise ValueError('phone_push_service_key_invalid')
    header = {'typ': 'JWT', 'alg': 'PS256', 'kid': kid}
    claims = {'iss': issuer, 'aud': AUDIENCE, 'iat': now, 'exp': now + 3600}
    unsigned = _encode(_json(header)) + b'.' + _encode(_json(claims))
    with tempfile.TemporaryFile() as secret:
        secret.write(key.replace('\\n', '\n').encode('utf-8'))
        secret.seek(0)
        fd = secret.fileno()
        signed = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', '/proc/self/fd/' + str(fd),
            '-sigopt', 'rsa_padding_mode:pss', '-sigopt', 'rsa_pss_saltlen:32', '-sigopt', 'rsa_mgf1_md:sha256'],
            input=unsigned, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(fd,), timeout=5)
        if signed.returncode != 0 or not signed.stdout:
            raise ValueError('phone_push_service_signing_failed')
    return (unsigned + b'.' + _encode(signed.stdout)).decode('ascii')


def send_alert(project, auth, token, text, test_message):
    body = {'payload': {'notification': {'title': 'AMITA', 'body': text, 'clickAction': {'actionType': 0}}},
            'target': {'token': [token]}, 'pushOptions': {'ttl': 900, 'testMessage': test_message}}
    req = Request('https://push-api.cloud.huawei.com/v3/' + project + '/messages:send', data=_json(body),
                  headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + auth, 'push-type': '0'})
    # System proxy variables and redirect targets must not receive this bearer.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        status = response.code
        raw = response.read(16385)
    try:
        if len(raw) > 16384:
            raise ValueError
        result = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    code, vendor = result.get('code'), result.get('requestId')
    detail = {'http_status': status}
    if isinstance(code, str) and re.fullmatch(r'[0-9]{8}', code):
        detail['provider_code'] = code
    if isinstance(vendor, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', vendor):
        detail['provider_request_id'] = vendor
    if status == 200 and code == '80000000' and 'provider_request_id' in detail:
        return 'accepted', detail
    if status in (400, 401, 403, 404) and code != '80000000':
        return 'rejected', detail
    return 'unknown', detail


class OwnerPush:
    def __init__(self, authority):
        self.authority = authority
        self.store = authority.store
        root = Path(authority.config_path).absolute().parent
        self.settings = root / 'owner-push.json'
        self.binding = root / 'owner-push-binding.json'

    def _config(self):
        if not self.settings.exists() and not self.settings.is_symlink():
            return None
        value = private_json(self.settings)
        if (not isinstance(value, dict) or set(value) != {'format', 'node_id', 'project_id', 'service_key_file', 'test_message'}
                or value['format'] != FORMAT or type(value['test_message']) is not bool
                or not isinstance(value['project_id'], str) or not re.fullmatch(r'[0-9]{5,32}', value['project_id'])
                or not isinstance(value['service_key_file'], str) or not Path(value['service_key_file']).is_absolute()):
            raise ValueError('phone_push_configuration_invalid')
        _identifier(value['node_id'], 'phone_push_node')
        return value

    def _target(self, config):
        current = self.authority._config()
        target = next((node for node in current['nodes'] if node['node_id'] == config['node_id']), None)
        if (not current['enabled'] or target is None or not target['enabled'] or 'phone:chat' not in target['scope']
                or not any(node == target for node in self.authority.installed['nodes'])):
            raise PermissionError('phone_push_original_owner_required')
        return target

    def _binding(self, config, node):
        if not self.binding.exists() and not self.binding.is_symlink():
            return None
        row = private_json(self.binding)
        if (row.get('node_id'), row.get('session_id'), row.get('project_id')) != (
                node['node_id'], node['session_id'], config['project_id']):
            return None
        return row

    def phone_request(self, node, action, arguments):
        from .phone_owner import atomic_private
        if not isinstance(arguments, dict) or arguments.get('session_id') != node['session_id']:
            raise PermissionError('phone_push_original_session_required')
        fields = {'session_id'} if action == STATUS else {'session_id', 'token'}
        if set(arguments) != fields:
            raise ValueError('phone_push_arguments_required')
        config = self._config()
        enabled = config is not None and config['node_id'] == node['node_id']
        if not enabled:
            return {'format': FORMAT, 'enabled': False, 'bound': False}
        target = self._target(config)
        if target['session_id'] != node['session_id']:
            raise PermissionError('phone_push_original_owner_required')
        row = self._binding(config, node)
        if action == BIND:
            token = arguments['token']
            if not isinstance(token, str) or (token and not re.fullmatch(r'[!-~]{16,4096}', token)):
                raise ValueError('phone_push_token_invalid')
            new_row = {'node_id': node['node_id'], 'session_id': node['session_id'],
                       'project_id': config['project_id'], 'token': token}
            if row != new_row:
                atomic_private(self.binding, new_row)
            row = new_row
        return {'format': FORMAT, 'enabled': True, 'bound': bool(row and row.get('token'))}

    def dispatch(self, method, path, arguments, peer):
        # The recipient is defined by the original owner binding, never by a sender.
        grants = peer.get('capabilities', [])
        worker = peer.get('role') in ('worker', 'agent_peer') and isinstance(grants, list) and 'owner.notify' in grants
        actor = 'operator' if peer.get('role') == 'operator' else peer.get('node') if worker else None
        if method != 'POST' or not actor or path not in (ROUTE, ROUTE + '/status'):
            raise PermissionError('phone_push_owner_notification_required')
        submit = path == ROUTE
        if not isinstance(arguments, dict) or set(arguments) != ({'request_id', 'text'} if submit else {'request_id'}):
            raise ValueError('phone_push_arguments_required')
        original = request_id(arguments['request_id'])
        identity = 'phone-push-' + hashlib.sha256(_json([peer.get('role'), actor, original])).hexdigest()
        fingerprint = None
        if submit:
            text = arguments['text']
            fingerprint = text_fingerprint(text)
            if len(text.encode('utf-8')) > 512:
                raise ValueError('phone_push_text_too_long')
        with self.authority.lock:
            with self.store.transaction() as db:
                row = db.execute('SELECT * FROM outbox WHERE id=?', (identity,)).fetchone()
                if row is not None:
                    if row['delivery_route'] != DELIVERY or (submit and row['fingerprint'] != fingerprint):
                        raise Conflict('phone_push_original_request_changed')
                    return self._receipt(original, row['status'], json.loads(row['detail']))
                if not submit:
                    return self._receipt(original, 'not_found', {})
            config = self._config()
            if config is None:
                raise Conflict('phone_push_service_not_configured')
            target = self._target(config)
            bound = self._binding(config, target)
            if bound is None or not bound.get('token'):
                raise Conflict('phone_push_owner_device_not_ready')
            auth = service_jwt(private_json(config['service_key_file']), int(self.store.clock()))
            with self.store.transaction() as db:
                db.execute('INSERT INTO outbox(id,fingerprint,body,status,client_id,created,delivery_route) VALUES(?,?,?,?,?,?,?)',
                           (identity, fingerprint, text, 'submitting', identity, self.store.clock(), DELIVERY))
        try:
            status, detail = send_alert(config['project_id'], auth, bound['token'], text, config['test_message'])
        except (OSError, ValueError):
            status, detail = 'unknown', {}
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET status=?,detail=? WHERE id=? AND status='submitting'",
                       (status, _json(detail).decode('utf-8'), identity))
            row = db.execute('SELECT status,detail FROM outbox WHERE id=?', (identity,)).fetchone()
        return self._receipt(original, row['status'], json.loads(row['detail']))

    @staticmethod
    def _receipt(original, status, detail):
        return {'format': FORMAT, 'request_id': original, 'status': status, 'provider': detail,
                'delivery_verified': False}
