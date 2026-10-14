"""NEW source-only, exact old Claw remote module plus two opt-in hooks.

Does not install, change remote.json, touch journals or send anything. Root
installs an independently inspected result in its selected CLI release only.
"""
import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

BASE_SHA256 = '3c172a06f0d412ed930742cb7b56b114468368479329e2394b1414b956c8387e'
CLAW_LICENSE = '''MIT License

Copyright (c) 2026 Laurieta Temuies

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''


def build(source, destination):
    source, destination = Path(source), Path(destination)
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('claw_original_module_not_regular')
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != BASE_SHA256:
        raise ValueError('claw_original_module_changed')
    old = data.decode('utf8')
    request = 'def request(config, access, endpoint, body=None):\n'
    run = 'def run(args, access, root, config):\n'
    if old.count(request) != 1 or old.count(run) != 1:
        raise ValueError('claw_original_module_shape_changed')
    selected = old.replace(request, request +
        "    if 'mesh_communication' in config:\n"
        '        from assistant_mesh.communication_client import owner_request\n'
        '        return owner_request(config, access, endpoint, body)\n', 1)
    selected = selected.replace(run, run +
        "    if 'mesh_communication' in config:\n"
        '        from assistant_mesh.communication_client import legacy_preflight\n'
        '        result = legacy_preflight(args, access, root, config)\n'
        '        if result is not None:\n'
        '            return result\n', 1)
    compile(selected, '<claw-source-overlay>', 'exec')
    if (not destination.is_absolute() or any(parent.is_symlink() for parent in destination.parents)
            or destination.exists() or destination.is_symlink()):
        raise ValueError('claw_overlay_destination_must_be_new')
    destination.mkdir(mode=0o700)
    path = destination / 'ilink_remote.py'
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(selected.encode('utf8'))
    with (destination / 'LICENSE').open('x') as stream:
        stream.write(CLAW_LICENSE)
    report = {'protocol': 'mesh-claw-client-source-overlay/1', 'base_sha256': BASE_SHA256,
        'overlay_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'opt_in_hooks': 2,
        'old_logical_url_and_reservation_code_unchanged': True, 'installed': False,
        'remote_config_or_journals_changed': False, 'requests_submitted': 0}
    with (destination / 'overlay.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, sort_keys=True)
    return report


def main():
    parser = argparse.ArgumentParser(description='Prepare exact Claw client SOURCE, no install/send')
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.destination), sort_keys=True))


if __name__ == '__main__':
    main()
