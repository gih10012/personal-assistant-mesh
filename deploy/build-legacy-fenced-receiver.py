"""Build a NEW isolated source candidate; never install or open a ledger.

Input is the exact 40-module frozen receiver source, not a production state
directory. The only changed incumbent modules are Store ingestion extraction
and the authenticated carrier HTTP outcome budget. No HEAD Store is copied.
"""
import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path

BASE_MANIFEST_SHA256 = 'f68f548cf160bb2083ad67ab7204d382c990fcd6ee0af8fb2ef999d5a1b2e29a'
BASE_STORE_SHA256 = '7d8f750e8dfe10045ca97b52ac0ff853d645ea66b4b01f9a9aaf60fb48df5440'
BASE_SERVER_SHA256 = '23ae66d85d860ebcce2dfbe3d61482a1bc6c89a0b068ec75799aade3d99c526b'
PATCHED_STORE_SHA256 = '081633994459ffd2fb253e5514ea398d011b0eb8adcf059b251bbde10a705f08'
PATCHED_SERVER_SHA256 = 'cc5bb8b492d7e03f68a5f8720eb1399d9ab4ac8e303f46919713c0a6592fd19c'
SUPPORT_SHA256 = {
    'channel_authority.py': 'a9978f8dff13b4b4ae6409fab3a12d43b7e7a53087dda423e2f48abb09a0934c',
    'channel_state.py': 'e1b3642f1c9c8a736474e888e70f845e6b750c98413da2f49d7602eddf857568',
}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def apply_exact_patch(source, patch, filename):
    """Single-file unified patch with exact offsets/counts/context; no fuzz."""
    old, lines = source.decode('utf8').splitlines(True), patch.decode('utf8').splitlines(True)
    prefix = ['diff --git a/assistant_mesh/{0} b/assistant_mesh/{0}\n'.format(filename),
              '--- a/assistant_mesh/' + filename + '\n',
              '+++ b/assistant_mesh/' + filename + '\n']
    if lines[:3] != prefix:
        raise ValueError('candidate_patch_target_changed')
    result, cursor, index, count = [], 0, 3, 0
    while index < len(lines):
        header = re.fullmatch(r'@@ -(\d+),(\d+) \+(\d+),(\d+) @@\n', lines[index])
        if header is None:
            raise ValueError('candidate_patch_invalid')
        before, removed_count, after, added_count = map(int, header.groups())
        if before - 1 < cursor:
            raise ValueError('candidate_patch_overlap')
        result.extend(old[cursor:before - 1])
        cursor, index, removed, added = before - 1, index + 1, 0, 0
        if len(result) != after - 1:
            raise ValueError('candidate_patch_offset_changed')
        while index < len(lines) and not lines[index].startswith('@@ '):
            line = lines[index]
            if line[:1] not in (' ', '+', '-'):
                raise ValueError('candidate_patch_invalid')
            if line[0] in (' ', '-'):
                if cursor >= len(old) or old[cursor] != line[1:]:
                    raise ValueError('candidate_patch_context_changed')
                cursor, removed = cursor + 1, removed + 1
            if line[0] in (' ', '+'):
                result.append(line[1:])
                added += 1
            index += 1
        if (removed, added) != (removed_count, added_count):
            raise ValueError('candidate_patch_count_changed')
        count += 1
    if count != 1:
        raise ValueError('candidate_patch_hunk_changed')
    result.extend(old[cursor:])
    return ''.join(result).encode('utf8')


def _regular_bytes(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('candidate_source_not_regular')
    return path.read_bytes()


def build(base_root, destination, support_root=None):
    base_root, destination = Path(base_root), Path(destination)
    support_root = Path(support_root or Path(__file__).resolve().parents[1])
    original = {path.name: _regular_bytes(path)
                for path in sorted((base_root / 'assistant_mesh').glob('*.py'))}
    manifest = ''.join(_sha(data) + '  ' + name + '\n' for name, data in sorted(original.items()))
    if len(original) != 40 or _sha(manifest.encode('ascii')) != BASE_MANIFEST_SHA256:
        raise ValueError('candidate_frozen_base_changed')
    if (_sha(original['store.py']) != BASE_STORE_SHA256
            or _sha(original['server.py']) != BASE_SERVER_SHA256):
        raise ValueError('candidate_original_core_changed')
    files = dict(original)
    for name, patch, expected in (
            ('store.py', 'legacy-store-carrier.patch', PATCHED_STORE_SHA256),
            ('server.py', 'legacy-fenced-http-budget.patch', PATCHED_SERVER_SHA256)):
        files[name] = apply_exact_patch(original[name],
            _regular_bytes(support_root / 'deploy' / patch), name)
        if _sha(files[name]) != expected:
            raise ValueError('candidate_patched_core_changed')
    for name, expected in SUPPORT_SHA256.items():
        data = _regular_bytes(support_root / 'assistant_mesh' / name)
        if _sha(data) != expected:
            raise ValueError('candidate_facade_changed')
        files[name] = data
    # This explicit bridge is the only new implementation. No broad HEAD copy.
    files['legacy_fenced_receiver.py'] = _regular_bytes(
        support_root / 'assistant_mesh' / 'legacy_fenced_receiver.py')
    for data in files.values():
        compile(data, '<isolated-candidate-source>', 'exec')
    if (not destination.is_absolute() or any(parent.is_symlink() for parent in destination.parents)
            or destination.exists() or destination.is_symlink()):
        raise ValueError('candidate_destination_must_be_new')
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    package = destination / 'assistant_mesh'
    package.mkdir(mode=0o700)
    for name, data in files.items():
        with (package / name).open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
    report = {'format': 'mesh-legacy-fenced-source-candidate/1',
        'base_manifest_sha256': BASE_MANIFEST_SHA256,
        'original_modules': 40, 'unchanged_original_modules': 38,
        'changed_original_modules': ['server.py', 'store.py'],
        'new_modules': sorted(set(files) - set(original)),
        'source_sha256': {name: _sha(data) for name, data in sorted(files.items())},
        'production_installed': False, 'ledger_opened': False,
        'carrier_started': False, 'grant_authorized': False}
    with (destination / 'candidate.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, ensure_ascii=False, sort_keys=True)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description='Build isolated frozen-source candidate, no deployment')
    parser.add_argument('--base-root', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--support-root')
    args = parser.parse_args()
    report = build(args.base_root, args.destination, args.support_root)
    print(json.dumps({key: value for key, value in report.items() if key != 'source_sha256'}, sort_keys=True))


if __name__ == '__main__':
    main()
