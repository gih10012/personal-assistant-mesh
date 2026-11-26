import base64
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from assistant_mesh.phone_push import AUDIENCE, BIND, FORMAT, ROUTE, STATUS, send_alert, service_jwt
from assistant_mesh.store import Conflict
from tests import test_phone_authority as fixture
from tests.test_enrollment_relay import owned


class OwnerPhonePushTest(unittest.TestCase):
    def test_existing_phone_authenticator_and_at_most_one_push(self):
        original = fixture.PhoneAuthorityTests()
        original.setUp()
        self.addCleanup(original.tearDown)
        original.original.store.entrance_policy = {'owner_ref': 'owner-fixture'}
        node = original.config['nodes'][0]
        node['scope'].append('phone:chat')
        node['chat_owner_ref'] = 'owner-fixture'
        owned(original.config_path, original.config)
        original.service.installed = original.service._config()
        push = original.service.owner_push
        operator = {'role': 'operator'}
        worker = {'role': 'worker', 'node': 'existing-worker', 'capabilities': ['owner.notify']}
        payload = {'request_id': 'owner-reminder', 'text': 'AMITA 提醒'}
        status = {'request_id': payload['request_id']}
        self.assertEqual({'format': FORMAT, 'enabled': False, 'bound': False}, original.call(STATUS))
        with self.assertRaises(Conflict):
            push.dispatch('POST', ROUTE, payload, operator)
        root = original.config_path.parent
        keyfile = root / 'original-service.json'
        owned(keyfile, {})
        owned(push.settings, {'format': FORMAT, 'node_id': node['node_id'], 'project_id': '123456',
            'service_key_file': str(keyfile), 'test_message': True})
        handset_token = 'SYNTHETIC_HUAWEI_TOKEN_ONLY'
        with self.assertRaises(PermissionError):
            original.service.node(fixture.NODE_TOKEN, {'action': BIND,
                'arguments': {'session_id': 'wrong', 'token': handset_token}})
        self.assertTrue(original.call(BIND, token=handset_token)['bound'])
        self.assertNotIn(handset_token, str(original.call(STATUS)))
        self.assertEqual(0o600, push.binding.stat().st_mode & 0o777)

        with self.assertRaises(PermissionError):
            push.dispatch('POST', ROUTE, payload, dict(worker, capabilities=[]))
        with patch('assistant_mesh.phone_push.service_jwt', return_value='signed-authority'), \
                patch('assistant_mesh.phone_push.send_alert', side_effect=OSError) as send:
            first = push.dispatch('POST', ROUTE, payload, worker)
            self.assertEqual('unknown', first['status'])
            self.assertEqual(first, push.dispatch('POST', ROUTE, payload, worker))
            self.assertEqual(first, push.dispatch('POST', ROUTE + '/status', status, worker))
            self.assertEqual('not_found', push.dispatch('POST', ROUTE + '/status', status, operator)['status'])
            send.assert_called_once_with('123456', 'signed-authority', handset_token, payload['text'], True)
            with self.assertRaises(Conflict):
                push.dispatch('POST', ROUTE, dict(payload, text='different'), worker)
        self.assertFalse(original.call(BIND, token='')['bound'])
        with self.assertRaises(Conflict):
            push.dispatch('POST', ROUTE, dict(payload, request_id='after-revocation'), worker)

    def test_provider_alert_and_real_ps256_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / 'verify.pem'
            pem = subprocess.check_output(['openssl', 'genpkey', '-algorithm', 'RSA',
                '-pkeyopt', 'rsa_keygen_bits:2048'], stderr=subprocess.DEVNULL)
            public.write_bytes(subprocess.check_output(['openssl', 'pkey', '-pubout'], input=pem))
            token = service_jwt({'key_id': 'fixture-kid', 'sub_account': 'fixture-sub',
                                 'private_key': pem.decode().replace('\n', '\\n')}, 1000)
            head, claims, sig = token.split('.')
            decode = lambda part: base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))
            self.assertEqual('PS256', json.loads(decode(head))['alg'])
            self.assertEqual({'aud': AUDIENCE, 'iss': 'fixture-sub', 'iat': 1000, 'exp': 4600}, json.loads(decode(claims)))
            signed = Path(directory) / 'signature'
            signed.write_bytes(decode(sig))
            subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(public), '-signature', str(signed),
                '-sigopt', 'rsa_padding_mode:pss', '-sigopt', 'rsa_pss_saltlen:32', '-sigopt', 'rsa_mgf1_md:sha256'],
                input=(head + '.' + claims).encode(), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        response = io.BytesIO(b'{"code":"80000000","requestId":"provider-receipt"}')
        response.code = 200
        with patch('assistant_mesh.phone_push.build_opener') as server:
            server.return_value.open.return_value = response
            self.assertEqual(('accepted', {'http_status': 200, 'provider_code': '80000000',
                'provider_request_id': 'provider-receipt'}), send_alert('123456', 'jwt', 'only-phone', 'notice', True))
            req = server.return_value.open.call_args.args[0]
            self.assertEqual('https://push-api.cloud.huawei.com/v3/123456/messages:send', req.full_url)
            self.assertEqual('0', req.get_header('Push-type'))
            self.assertEqual({'token': ['only-phone']}, json.loads(req.data)['target'])
