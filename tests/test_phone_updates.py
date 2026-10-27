"""Offline RSA publisher and raw HTTP artifact integrity, synthetic keys only."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from assistant_mesh.phone_updates import publish, UpdateService


class PhoneUpdateTests(unittest.TestCase):
    def test_signed_utf8_manifest_content_address_and_failed_publish_retains_old(self):
        with tempfile.TemporaryDirectory(prefix='phone-data-update.') as tmp:
            root = Path(tmp)
            key = root / 'offline.pem'
            result = subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt',
                'rsa_keygen_bits:2048', '-out', str(key)], capture_output=True)
            self.assertEqual(0, result.returncode)
            source = root / 'skill.md'; source.write_text('# 手机技能\n完整 UTF-8\n')
            spec = {'version': 1, 'min_shell_version': 1,
                    'artifacts': [{'path': 'skills/phone.md', 'kind': 'skill', 'source': str(source)}]}
            directory = root / 'release'
            trust = publish(directory, spec, private_key=key, key_id='synthetic-v1', origin='https://mesh.example.test')
            service = UpdateService(directory, key_id=trust['key_id'],
                public_key_spki_base64=trust['public_key_spki_base64'], origin='https://mesh.example.test')
            envelope, payload = service.current()
            self.assertEqual('amita-data-manifest-envelope/1', envelope['format'])
            item = payload['artifacts'][0]
            self.assertEqual(source.read_bytes(), service.artifact(item['sha256']))
            saved = (directory / 'manifest.json').read_bytes()
            spec['version'] = 2; spec['artifacts'][0]['path'] = 'native/bypass.abc'
            with self.assertRaisesRegex(ValueError, 'signed_hap'):
                publish(directory, spec, private_key=key, key_id='synthetic-v1', origin='https://mesh.example.test')
            self.assertEqual(saved, (directory / 'manifest.json').read_bytes())
            modified = dict(envelope)
            altered = json.loads(base64.b64decode(modified['payload']))
            altered['version'] = 999
            modified['payload'] = base64.b64encode(json.dumps(altered).encode()).decode()
            (directory / 'manifest.json').write_text(json.dumps(modified))
            with self.assertRaisesRegex(ValueError, 'openssl_verification'):
                service.current()
            (directory / 'manifest.json').write_bytes(saved)
            blob = directory / 'artifacts' / item['sha256']; blob.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'size_mismatch'):
                service.artifact(item['sha256'])


if __name__ == '__main__':
    unittest.main()
