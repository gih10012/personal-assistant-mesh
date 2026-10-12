"""Build the separate, unsigned collaborator XPI; never installs or connects."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import zipfile


MANIFEST = {
    'manifest_version': 2, 'name': 'Personal Assistant Mesh · Chat Collaborator',
    'version': '0.1.0',
    'description': 'Explicit owner-bound project Chat collaboration through a dedicated background tab; no authentication copying.',
    'browser_specific_settings': {'gecko': {'id': 'personal-assistant-mesh-chat@gih10012.local', 'strict_min_version': '109.0'}},
    'permissions': ['nativeMessaging', 'storage', 'tabs', 'sessions', 'https://chatgpt.com/*'],
    'background': {'scripts': ['chat_bridge_dom.js', 'chat_bridge_background.js'], 'persistent': True},
    'content_security_policy': "script-src 'self'; object-src 'none'; connect-src 'none'",
}


def build(output):
    root = Path(__file__).resolve().parent
    files = [('manifest.json', json.dumps(MANIFEST, ensure_ascii=False).encode())]
    for name, path in (('chat_bridge_dom.js', root / 'chat_bridge_dom.js'),
                       ('chat_bridge_background.js', root / 'chat_bridge_background.js'),
                       ('LICENSE', root.parents[1] / 'LICENSE')):
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('chat_package_source_invalid')
        raw = path.read_bytes()
        if len(raw) > 131072:
            raise ValueError('chat_package_source_too_large')
        files.append((name, raw))
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
    return {'format': 'mesh-chat-collaborator-package/1', 'files': [name for name, _ in files],
            'sha256': hashlib.sha256(Path(output).read_bytes()).hexdigest(),
            'signed': False, 'installed': False, 'account_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    args = parser.parse_args()
    print(json.dumps(build(args.output), sort_keys=True))


if __name__ == '__main__':
    main()
