"""Independent iLink transport. No personal-WeChat desktop dependency.

Wire shape verified against wechatbot-sdk 0.3.0 and Tencent/openclaw-weixin.
Secrets/raw attachment references remain in private runtime state.
"""
import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import private_json


class ChannelError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ChannelError('redirect_refused')


class ILink:
    def __init__(self, account_path, request=None):
        self.account = private_json(account_path)
        origin = urllib.parse.urlsplit(self.account['baseurl'])
        if (origin.scheme != 'https' or not origin.hostname or not origin.hostname.endswith('.weixin.qq.com')
                or origin.username or origin.password or origin.path not in ('', '/') or origin.query or origin.fragment
                or origin.port not in (None, 443)):
            raise ValueError('untrusted_ilink_origin')
        self.base = 'https://' + origin.hostname
        self.request_override = request
        for key in ('bot_token', 'ilink_user_id', 'ilink_bot_id'):
            if not self.account.get(key):
                raise ValueError('ilink_credentials_missing')

    def request(self, endpoint, body, timeout):
        if self.request_override:
            return self.request_override(endpoint, body, timeout)
        body = dict(body, base_info={'channel_version': '0.3.0', 'bot_agent': 'PersonalAssistantMesh/0.1.0'})
        headers = {'Content-Type': 'application/json', 'AuthorizationType': 'ilink_bot_token',
                   'Authorization': 'Bearer ' + self.account['bot_token'],
                   'X-WECHAT-UIN': base64.b64encode(str(struct.unpack('>I', os.urandom(4))[0]).encode()).decode(),
                   'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': '768'}
        req = urllib.request.Request(self.base + '/ilink/bot/' + endpoint, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ChannelError('response_too_large')
                value = json.loads(raw)
        except (socket.timeout, TimeoutError):
            raise ChannelError('network_timeout') from None
        except urllib.error.HTTPError as exc:
            raise ChannelError('http_' + str(exc.code)) from None
        except (urllib.error.URLError, OSError):
            raise ChannelError('network_error') from None
        except (ValueError, TypeError):
            raise ChannelError('invalid_response') from None
        if not isinstance(value, dict):
            raise ChannelError('invalid_response')
        codes = [value.get('ret'), value.get('errcode')]
        if -14 in codes:
            raise ChannelError('auth_required')
        for code in codes:
            if code not in (None, 0):
                raise ChannelError('business_' + str(code))
        return value

    def poll(self, cursor):
        return self.request('getupdates', {'get_updates_buf': cursor}, 40)

    def send(self, row):
        message = {'to_user_id': self.account['ilink_user_id'], 'client_id': row['client_id'],
                   'message_type': 2, 'message_state': 2,
                   'item_list': [{'type': 1, 'text_item': {'text': row['body']}}]}
        if row.get('context_token'):
            message['context_token'] = row['context_token']
        return self.request('sendmessage', {'msg': message}, 15)


class Channel:
    def __init__(self, store, transport):
        self.store, self.transport = store, transport
        self.stop = threading.Event()
        if self.store.get('cursor') is None:
            account = transport.account
            self.store.ingest(account.get('pending', []), account.get('cursor', ''), account['ilink_user_id'], account['ilink_bot_id'])
            context = account.get('owner_context', {})
            if context.get('user_id') == account['ilink_user_id'] and context.get('token') and not self.store.get('owner_context'):
                self.store.set('owner_context', {'token': context['token'],
                                               'version': context.get('message_fingerprint', 'imported'),
                                               'created': context.get('message_created_at_ms', 0)})

    def poll_once(self):
        account = self.transport.account
        batch = self.transport.poll(self.store.get('cursor', account.get('cursor', '')))
        result = self.store.ingest(batch.get('msgs'), batch.get('get_updates_buf'), account['ilink_user_id'], account['ilink_bot_id'])
        self.store.set('channel_error', None)
        return result

    def send_once(self):
        row = self.store.next_send()
        if not row:
            return False
        try:
            value = self.transport.send(row)
            self.store.finish_send(row['id'], 'accepted', {'delivery_verified': False, 'message_id': value.get('message_id')})
        except ChannelError as exc:
            status = 'rejected' if exc.code == 'business_-2' else 'waiting_auth' if exc.code == 'auth_required' else 'unknown'
            self.store.finish_send(row['id'], status, {'code': exc.code, 'delivery_verified': False})
        except Exception:
            self.store.finish_send(row['id'], 'unknown', {'code': 'transport_failed', 'delivery_verified': False})
        return True

    def run_poll(self):
        backoff = 1
        while not self.stop.is_set():
            try:
                self.poll_once()
                backoff = 1
            except ChannelError as exc:
                self.store.set('channel_error', {'code': exc.code, 'at': time.time()})
                backoff = min(60, backoff * 2)
            except Exception:
                self.store.set('channel_error', {'code': 'poll_failed', 'at': time.time()})
                backoff = min(60, backoff * 2)
            self.stop.wait(backoff)

    def run_send(self):
        while not self.stop.is_set():
            self.send_once()
            self.stop.wait(0.5)
