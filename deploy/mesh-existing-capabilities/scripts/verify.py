"""Check a fixed private installation; never import/execute its skill code."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys


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
    binding = json.loads(checked_bytes(entry / 'references/binding.json').decode('utf8'))
    if binding.get('format') != 'mesh-existing-skill-binding/1':
        raise ValueError('binding_format')
    if sys.version_info < (3, 9):
        raise ValueError('modern_python_required')
    root = Path(binding['installation'])
    if not root.is_absolute() or '..' in root.parts or str(root.resolve()) != str(root):
        raise ValueError('installation_path')
    if stat.S_IMODE(root.lstat().st_mode) != 0o700:
        raise ValueError('installation_not_private')
    receipt = checked_bytes(root / 'installation.json')
    if hashlib.sha256(receipt).hexdigest() != binding['installation_receipt_sha256']:
        raise ValueError('original_receipt_changed')
    for item in binding['files']:
        path = PurePosixPath(item['path'])
        if path.is_absolute() or any(part in ('.', '..') for part in path.parts):
            raise ValueError('member_path')
        raw = checked_bytes(root.joinpath(*path.parts))
        if hashlib.sha256(raw).hexdigest() != item['sha256']:
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
