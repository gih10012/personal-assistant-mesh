#!/usr/bin/env python3
"""Build an unsigned development XPI from an exact, credential-free allowlist.

Packaging only: no browser installation, signing, host registration or network.
The output must not already exist. No private client/launcher/template/test file
is included. The existing repository license is preserved in the archive.
"""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import zipfile


FILES = ('manifest.json', 'background.js', 'popup.html', 'popup.js', 'popup.css')


def build(output):
    source = Path(__file__).resolve().parent
    selected = [(name, source / name) for name in FILES]
    selected.append(('LICENSE', source.parents[1] / 'LICENSE'))
    contents = []
    for name, path in selected:
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('package_source_invalid')
        value = path.read_bytes()
        if len(value) > 131072:
            raise ValueError('package_source_too_large')
        contents.append((name, value))
    output = Path(output)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in contents:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    return {'format': 'mesh-firefox-package/1', 'path': str(output.resolve()),
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'files': [name for name, _ in contents], 'signed': False,
            'installed': False, 'account_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', help='New output XPI path; never overwrites an existing file')
    args = parser.parse_args()
    try:
        value = build(args.output)
    except (OSError, ValueError, zipfile.BadZipFile):
        parser.exit(1, 'package_unavailable_or_output_exists\n')
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
