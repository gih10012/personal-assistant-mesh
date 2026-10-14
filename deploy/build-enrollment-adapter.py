#!/usr/bin/env python3
"""Build a small no-secret, no-Python-required node-side recipe zip.

This build script is a reference packager, not a device installer or enrollment.
It never downloads dependencies, changes accounts, creates node tokens or sends
traffic. The output is new/exclusive; an existing package is never overwritten.
"""
import argparse
import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path


def build(output):
    output = Path(output)
    if not output.is_absolute() or '..' in output.parts:
        raise ValueError('adapter_package_absolute_output_required')
    parent = output.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError('adapter_package_private_parent_required')
    root = Path(__file__).resolve().parent.parent
    sources = {'enrollment-stdio-adapter.mjs': root / 'deploy/enrollment-stdio-adapter.mjs',
               'README.md': root / 'docs/ENROLLMENT-BOOTSTRAP.md',
               'enrollment.json.example': root / 'deploy/enrollment-node.json.example',
               'LICENSE': root / 'LICENSE'}
    files = {}
    for name, source in sources.items():
        item = source.lstat()
        if not stat.S_ISREG(item.st_mode) or item.st_mode & 0o022 or item.st_size > 1024 * 1024:
            raise ValueError('adapter_package_source_invalid')
        files[name] = source.read_bytes()
    manifest = {'format': 'mesh-enrollment-adapter-package/1',
                'recipe': 'node-builtins-stdio-byte-challenge',
                'requires_python_on_node': False, 'requires_systemd_or_ssh': False,
                'production_node_enrolled': False, 'contains_credentials': False,
                'files': {name: {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                          for name, raw in sorted(files.items())}}
    files['MANIFEST.json'] = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('ascii')
    fd = os.open(str(output), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as target:
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as package:
            for name, raw in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                package.writestr(info, raw)
        target.flush()
        os.fsync(target.fileno())
    raw = output.read_bytes()
    return {'format': manifest['format'], 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'production_node_enrolled': False, 'contains_credentials': False,
            'requires_python_on_node': False}


def main():
    parser = argparse.ArgumentParser(description='Package the small independent enrollment adapter recipe')
    parser.add_argument('output')
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.output), sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({'status': 'error', 'category': 'adapter_package_failed',
                          'production_node_enrolled': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
