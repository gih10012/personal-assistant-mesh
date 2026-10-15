"""Native project/schema and exact TS contract tests, NOT ArkTS/HAP compilation."""
import base64
import hashlib
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from assistant_mesh.enrollment import document_sha256, validate_enrollment, validate_adaptation_report

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'apps' / 'harmonyos'


class HarmonyNodeTests(unittest.TestCase):
    def test_stage_project_declares_no_phone_wide_or_system_agent_permissions(self):
        app = json.loads((APP / 'AppScope/app.json5').read_text())['app']
        module = json.loads((APP / 'entry/src/main/module.json5').read_text())['module']
        profile = json.loads((APP / 'build-profile.json5').read_text())
        self.assertEqual('org.personalassistant.mesh', app['bundleName'])
        self.assertEqual(['ohos.permission.INTERNET'], [item['name'] for item in module['requestPermissions']])
        self.assertEqual([], profile['app']['signingConfigs'])
        self.assertNotIn('extensionAbilities', module)
        self.assertEqual('singleton', module['abilities'][0]['launchType'])
        self.assertTrue((APP / 'entry/src/main/ets/entryability/EntryAbility.ets').is_file())

    def test_platform_adapter_has_no_retry_or_persisted_credentials_or_uri(self):
        source = (APP / 'entry/src/main/ets/platform/NativeServices.ets').read_text()
        self.assertIn('maxRedirects: 0', source)
        self.assertIn('deviceInfo.sdkApiVersion >= 23', source)
        self.assertIn('exactByteView(utf8.buffer, utf8.byteOffset, utf8.byteLength)', source)
        snapshot = source.split('export class NodeSnapshot {', 1)[1].split('\n}', 1)[0]
        self.assertNotIn('token', snapshot.lower())
        self.assertNotIn('uri', snapshot.lower())
        intent = source.index('record = beginOperation(')
        self.assertLess(source.index('await this.persist();', intent), source.index('output = this.readSelectedComplete();', intent))
        for method, effect in [('bindEnrollment', 'await this.digestText(encoded)'),
                               ('connect', 'await transport.request('),
                               ('chooseDocument', 'await view.select(options)')]:
            body = source.split('async ' + method + '(', 1)[1].split('\n  }', 1)[0]
            self.assertLess(body.index('this.busy = true;'), body.index(effect))
            self.assertIn('finally { this.busy = false;', body)

    def node(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node TypeScript loader unavailable; not an ArkTS compile result')
        version = subprocess.check_output([node, '--version'], timeout=5).decode().strip()
        match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', version)
        if not match or int(match.group(1)) < 23 and not (int(match.group(1)) == 22 and int(match.group(2)) >= 18):
            self.skipTest('Node23+/22.18+ type stripping required for exact TS protocol test')
        return node

    def test_same_protocol_typescript_behavior_tests_run_without_harmony_sdk(self):
        run = subprocess.run([self.node(), '--test', str(APP / 'test/protocol.test.mjs')],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(0, run.returncode, run.stderr.decode(errors='replace'))

    def test_native_ascii_codec_matches_original_python_enrollment_bytes_and_schema(self):
        enrollment = {'format': 'mesh-enrollment/1', 'mesh_id': 'synthetic-mesh', 'node_id': 'harmony-fixture',
            'owner_binding': 'synthetic-owner', 'repo_pin': {
                'repository': 'https://github.com/gih10012/personal-assistant-mesh',
                'commit': 'd5e3e1f3130cc66719ce2ac444b79fdbd9c0237c'},
            'runtime': {'kind': 'interactive', 'expires_at': None, 'state_ref': 'private:phone-state'},
            'join': {'peer_id': 'laptop', 'authority_id': 'laptop', 'route_ref': 'private:approved-route',
                     'credential_ref': 'private:scoped-reference', 'trust_ref': 'private:owner-trust'},
            'continuation': {'project_id': '鸿蒙🙂', 'session_ref': 'private:original-session', 'state_ref': 'private:原检查点'}}
        challenge = {'format': 'mesh-enrollment-challenge/1', 'operation_id': 'original-op',
            'mesh_id': enrollment['mesh_id'], 'node_id': enrollment['node_id'], 'owner_binding': enrollment['owner_binding'],
            'enrollment_sha256': document_sha256(enrollment), 'continuation': enrollment['continuation'],
            'nonce': 'a' * 64, 'created_at': 1000, 'expires_at': 1060, 'capability_id': 'harmony-fixture:bytes-v1',
            'action': 'challenge.bytes.repeat', 'revision': 1,
            'input': {'bytes': 3, 'sha256': hashlib.sha256(b'abc').hexdigest(), 'base64': base64.b64encode(b'abc').decode()},
            'expected': {'bytes': 9, 'sha256': hashlib.sha256(b'abcabcabc').hexdigest()}, 'ttl_seconds': 60, 'proxy': None}
        run = subprocess.run([self.node(), str(APP / 'tools/protocol-fixture.mjs')],
            input=json.dumps({'enrollment': enrollment, 'challenge': challenge, 'now': 1001}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(0, run.returncode, run.stderr.decode(errors='replace'))
        value = json.loads(run.stdout.decode())
        encode = lambda item: json.dumps(item, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        self.assertEqual(encode(enrollment), value['canonical_enrollment'])
        self.assertEqual(encode(challenge), value['canonical_challenge'])
        self.assertEqual(enrollment, validate_enrollment(enrollment))
        self.assertEqual(value['adaptation'], validate_adaptation_report(value['adaptation']))


if __name__ == '__main__':
    unittest.main()
