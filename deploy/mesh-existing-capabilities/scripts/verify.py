"""Check a fixed private installation; never import/execute its skill code."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys


def file_pins(files):
    if not isinstance(files, list) or not 1 <= len(files) <= 160:
        raise ValueError('complete_file_pins_required')
    pins = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
            raise ValueError('file_pin_shape')
        name, digest = item['path'], item['sha256']
        if (not isinstance(name, str) or not name or '\\' in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or PurePosixPath(name).is_absolute() or str(PurePosixPath(name)) != name
                or any(part in ('.', '..') for part in name.split('/')) or name in pins
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in '0123456789abcdef' for char in digest)):
            raise ValueError('file_pin_identity')
        pins[name] = digest
    return pins


def checked_bytes(path, maximum=8 * 1024 * 1024):
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise ValueError('unsafe_parent')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size > maximum):
        raise ValueError('unsafe_member')
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    current = path.lstat()
    if (len(raw) > maximum or any(getattr(info, key) != getattr(before, key)
            or getattr(info, key) != getattr(after, key) or getattr(info, key) != getattr(current, key)
            for key in fields)):
        raise ValueError('changed_member')
    return raw


def verify():
    entry = Path(__file__).resolve().parents[1]
    entry_receipt = json.loads(checked_bytes(entry / 'installation.json').decode('utf8'))
    entry_pins = file_pins(entry_receipt['files'])
    binding_raw = checked_bytes(entry / 'references/binding.json')
    if (entry_receipt.get('format') != 'mesh-existing-skill-installation/1'
            or set(entry_pins) != {'SKILL.md', 'scripts/verify.py', 'references/binding.json'}
            or hashlib.sha256(binding_raw).hexdigest() != entry_pins['references/binding.json']):
        raise ValueError('entry_binding_changed')
    binding = json.loads(binding_raw.decode('utf8'))
    if binding.get('format') != 'mesh-existing-skill-binding/1':
        raise ValueError('binding_format')
    if (entry_receipt['provider'] != binding['provider']
            or entry_receipt['bundle_sha256'] != binding['bundle_sha256']):
        raise ValueError('entry_binding_identity')
    if sys.version_info < (3, 9):
        raise ValueError('modern_python_required')
    root = Path(binding['installation'])
    if not root.is_absolute() or '..' in root.parts or str(root.resolve()) != str(root):
        raise ValueError('installation_path')
    if stat.S_IMODE(root.lstat().st_mode) != 0o700:
        raise ValueError('installation_not_private')
    receipt_raw = checked_bytes(root / 'installation.json')
    if hashlib.sha256(receipt_raw).hexdigest() != binding['installation_receipt_sha256']:
        raise ValueError('original_receipt_changed')
    receipt = json.loads(receipt_raw.decode('utf8'))
    pins = file_pins(receipt['files'])
    if (receipt.get('format') != 'mesh-private-skill-installation/1'
            or type(receipt['revision']) is not int or receipt['revision'] < 1
            or any(type(receipt[key]) is not type(binding[key]) or receipt[key] != binding[key]
                   for key in ('source_provider', 'revision', 'bundle_sha256'))
            or receipt['files'] != binding['files']):
        raise ValueError('original_binding_changed')
    sources = receipt['sources']
    if (not isinstance(sources, list) or not sources or len(set(sources)) != len(sources)
            or not set(sources) <= {'ncut-web-api', 'xiumi-native-clipboard'}
            or any(name.partition('/')[0] not in sources for name in pins)):
        raise ValueError('original_sources_changed')
    expected = {}
    for identity, source, field, relative in (
            ('ncut.knowledge', 'ncut-web-api', 'script', 'scripts/ncut.py'),
            ('xiumi.compose', 'xiumi-native-clipboard', 'module', 'scripts/xiumi_components.py'),
            ('xiumi.validate', 'xiumi-native-clipboard', 'script', 'scripts/xiumi_clipboard.py')):
        if source in sources:
            expected[identity] = {'skill': str(root / source / 'SKILL.md'),
                field: str(root / source / relative), 'references': [str(root / name)
                    for name in pins if name.startswith(source + '/references/')]}
    checked_paths = {str(root / name) for name in pins}
    if (binding['actions'] != expected or any(path not in checked_paths
            for action in expected.values() for key, value in action.items()
            for path in (value if key == 'references' else [value]))):
        raise ValueError('original_action_paths_changed')
    for name, digest in pins.items():
        path = PurePosixPath(name)
        raw = checked_bytes(root.joinpath(*path.parts))
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('source_pin_changed')
    return {'source_integrity_checked': True, 'python_minimum_met': True,
            'runtime_verified': False, 'authentication_checked': False,
            'native_features_restricted': False}


if __name__ == '__main__':
    try:
        print(json.dumps(verify(), sort_keys=True))
    except Exception:
        print(json.dumps({'error': 'existing_skill_binding_unavailable',
                          'automatic_retry': False, 'runtime_verified': False}))
        raise SystemExit(1)
