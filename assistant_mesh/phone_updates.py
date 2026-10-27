"""Signed immutable data updates. Private RSA keys stay in the offline publisher."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

MANIFEST = '/v1/harmony/updates/manifest'
ARTIFACT = '/v1/harmony/updates/artifacts/'
MAX_FILE, MAX_TOTAL = 32 * 1024 * 1024, 64 * 1024 * 1024
BLOCKED = frozenset(('.hap', '.hsp', '.abc', '.so', '.ets', '.ts', '.js', '.mjs', '.cjs',
                     '.wasm', '.exe', '.dll', '.sh', '.py', '.bat', '.jar', '.dex', '.apk'))


def _openssl(*args, data=None):
    result = subprocess.run(['openssl', *args], input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=15, check=False)
    if result.returncode:
        raise ValueError('update_openssl_verification_failed')
    return result.stdout


def _origin(value):
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('update_https_origin_required')
    return 'https://' + url.netloc


def _path(value):
    if (not isinstance(value, str) or not value or len(value.encode()) > 512
            or '\\' in value or '%' in value or ':' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid_update_artifact_path')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in ('.', '..') for p in path.parts):
        raise ValueError('invalid_update_artifact_path')
    if any(PurePosixPath(p).suffix.lower() in BLOCKED for p in path.parts):
        raise ValueError('native_update_requires_signed_hap')
    return value


def validate_payload(payload, origin):
    if (not isinstance(payload, dict) or set(payload) != {'format', 'version', 'min_shell_version', 'artifacts'}
            or payload['format'] != 'amita-data-manifest/1'
            or type(payload['version']) is not int or not 1 <= payload['version'] <= 2147483647
            or type(payload['min_shell_version']) is not int or not 1 <= payload['min_shell_version'] <= 2147483647
            or not isinstance(payload['artifacts'], list) or len(payload['artifacts']) > 32):
        raise ValueError('invalid_update_manifest')
    total, paths = 0, set()
    for item in payload['artifacts']:
        if not isinstance(item, dict) or set(item) != {'path', 'kind', 'size', 'sha256', 'url'}:
            raise ValueError('invalid_update_artifact')
        name = _path(item['path'])
        digest = item['sha256']
        if (name in paths or item['kind'] not in ('config', 'skill', 'file')
                or type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE
                or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None
                or item['url'] != origin + ARTIFACT + digest):
            raise ValueError('invalid_update_artifact')
        paths.add(name); total += item['size']
    if total > MAX_TOTAL:
        raise ValueError('update_total_too_large')
    return payload


class UpdateService:
    def __init__(self, directory, *, key_id, public_key_spki_base64, origin):
        self.root, self.key_id, self.origin = Path(directory), key_id, _origin(origin)
        self.spki = base64.b64decode(public_key_spki_base64, validate=True)
        if not isinstance(key_id, str) or not key_id or len(key_id) > 200:
            raise ValueError('invalid_update_key_id')
        info = _openssl('pkey', '-pubin', '-inform', 'DER', '-text', '-noout', data=self.spki)
        if b'(2048 bit)' not in info or b'Modulus:' not in info:
            raise ValueError('update_rsa2048_key_required')

    def current(self):
        path = self.root / 'manifest.json'
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            raise ValueError('invalid_update_envelope')
        envelope = json.loads(path.read_bytes())
        if (not isinstance(envelope, dict)
                or set(envelope) != {'format', 'key_id', 'algorithm', 'payload', 'signature'}
                or envelope['format'] != 'amita-data-manifest-envelope/1'
                or envelope['key_id'] != self.key_id or envelope['algorithm'] != 'RSA-SHA256'):
            raise ValueError('invalid_update_envelope')
        raw = base64.b64decode(envelope['payload'], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
        if len(raw) > 128 * 1024 or len(signature) != 256:
            raise ValueError('invalid_update_signature')
        with tempfile.TemporaryDirectory(prefix='mesh-update-verify.') as tmp:
            key, sig = Path(tmp) / 'public.pem', Path(tmp) / 'signature.bin'
            key.write_bytes(_openssl('pkey', '-pubin', '-inform', 'DER', '-outform', 'PEM', data=self.spki))
            sig.write_bytes(signature)
            _openssl('dgst', '-sha256', '-verify', str(key), '-signature', str(sig), data=raw)
        payload = validate_payload(json.loads(raw.decode('utf8')), self.origin)
        return envelope, payload

    def manifest(self):
        envelope, unused = self.current()
        return json.dumps(envelope, separators=(',', ':'), ensure_ascii=True).encode()

    def artifact(self, digest):
        if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
            raise ValueError('invalid_update_artifact_hash')
        unused, payload = self.current()
        item = next((i for i in payload['artifacts'] if i['sha256'] == digest), None)
        if item is None:
            raise FileNotFoundError('update_artifact_not_in_current_manifest')
        path = self.root / 'artifacts' / digest
        if path.is_symlink() or path.stat().st_size != item['size']:
            raise ValueError('update_artifact_size_mismatch')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('update_artifact_hash_mismatch')
        return raw

    def trust(self, node_id):
        return {'format': 'amita-update-trust/1', 'node_id': node_id,
                'manifest_url': self.origin + MANIFEST, 'key_id': self.key_id,
                'public_key_spki_base64': base64.b64encode(self.spki).decode(), 'enabled': True}


def publish(directory, spec, *, private_key, key_id, origin):
    """Offline only. Publish blobs first, signed manifest last, no private key copy."""
    origin = _origin(origin)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    blobs = root / 'artifacts'
    blobs.mkdir(exist_ok=True, mode=0o700)
    if root.is_symlink() or blobs.is_symlink():
        raise ValueError('update_private_directory_required')
    spki = _openssl('pkey', '-in', str(private_key), '-passin', 'pass:', '-pubout', '-outform', 'DER')
    service = UpdateService(root, key_id=key_id, public_key_spki_base64=base64.b64encode(spki).decode(), origin=origin)
    if not isinstance(spec, dict) or set(spec) != {'version', 'min_shell_version', 'artifacts'}:
        raise ValueError('invalid_update_publish_spec')
    payload = {'format': 'amita-data-manifest/1', 'version': spec['version'],
               'min_shell_version': spec['min_shell_version'], 'artifacts': []}
    pending = {}
    if not isinstance(spec['artifacts'], list) or len(spec['artifacts']) > 32:
        raise ValueError('invalid_update_publish_spec')
    for item in spec['artifacts']:
        if not isinstance(item, dict) or set(item) != {'path', 'kind', 'source'}:
            raise ValueError('invalid_update_publish_spec')
        source = Path(item['source'])
        if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_FILE:
            raise ValueError('invalid_update_source')
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        payload['artifacts'].append({'path': item['path'], 'kind': item['kind'], 'size': len(raw),
                                    'sha256': digest, 'url': origin + ARTIFACT + digest})
        pending[digest] = raw
    validate_payload(payload, origin)
    if (root / 'manifest.json').exists():
        unused, previous = service.current()
        if payload['version'] <= previous['version']:
            raise ValueError('update_monotonic_version_required')
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf8')
    signature = _openssl('dgst', '-sha256', '-sign', str(private_key), '-passin', 'pass:', data=raw)
    envelope = {'format': 'amita-data-manifest-envelope/1', 'key_id': key_id,
                'algorithm': 'RSA-SHA256', 'payload': base64.b64encode(raw).decode(),
                'signature': base64.b64encode(signature).decode()}
    for digest, body in pending.items():
        path = blobs / digest
        if path.exists():
            if path.is_symlink() or path.read_bytes() != body:
                raise ValueError('update_content_address_conflict')
        else:
            with path.open('xb') as output:
                os.chmod(path, 0o600); output.write(body); output.flush(); os.fsync(output.fileno())
    with tempfile.NamedTemporaryFile(dir=root, prefix='.manifest-', delete=False) as output:
        temporary = output.name
        output.write(json.dumps(envelope, separators=(',', ':')).encode()); output.flush(); os.fsync(output.fileno())
    os.replace(temporary, root / 'manifest.json')
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return service.trust('replace-with-real-enrolled-node-id')


def main():
    parser = argparse.ArgumentParser(description='Offline signed phone DATA publisher; never publishes a HAP')
    for flag in ('directory', 'spec', 'private-key', 'key-id', 'origin'):
        parser.add_argument('--' + flag, required=True)
    args = parser.parse_args()
    trust = publish(args.directory, json.loads(Path(args.spec).read_text()),
                    private_key=args.private_key, key_id=args.key_id, origin=args.origin)
    print(json.dumps({'published': True, 'trust_template': trust}, separators=(',', ':')))


if __name__ == '__main__':
    main()
