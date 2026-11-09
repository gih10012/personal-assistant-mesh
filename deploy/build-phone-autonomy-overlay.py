"""Assemble the phone-only receiver overlay from an immutable public snapshot.

No runtime imports, Store/config/account access, listener or production action.
The parent launcher is retained byte-for-byte and run by a pinned thin wrapper.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MODULES = ('phone_authority', 'phone_owner', 'phone_control', 'phone_maintenance')
PARENT_MANIFEST = 'native-partial-overlay.json'
PROTECTED = ('store', 'server', 'worker', 'entrance', 'native_history', 'sessions')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('phone_overlay_bounded_regular_source_required')
    return path.read_bytes()


def parse36(raw):
    text = raw.decode('utf8')
    return ast.parse(text, feature_version=(3, 6)) if sys.version_info >= (3, 8) else ast.parse(text)


def literal(tree, name):
    items = [n for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1
             and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name]
    if len(items) != 1:
        raise ValueError('phone_overlay_parent_assignment_changed')
    return ast.literal_eval(items[0].value)


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('phone_overlay_absolute_path_required')
    return str(path)


def build(parent_snapshot, parent_manifest_sha256, source_commit, destination,
          installed_parent, installed_target, source_root=ROOT):
    destination = Path(absolute(destination))
    if destination.exists() or destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        raise ValueError('phone_overlay_new_destination_required')
    if not re.fullmatch(r'[0-9a-f]{40}', source_commit):
        raise ValueError('phone_overlay_full_source_commit_required')
    parent = Path(absolute(parent_snapshot))
    installed_parent, installed_target = absolute(installed_parent), absolute(installed_target)
    original_manifest = read(parent / PARENT_MANIFEST)
    if sha(original_manifest) != parent_manifest_sha256:
        raise ValueError('phone_overlay_parent_manifest_pin_changed')
    manifest = json.loads(original_manifest.decode('utf8'))
    if manifest.get('format') != 'amita-native-partial-overlay/1' or manifest.get('component') != 'receiver':
        raise ValueError('phone_overlay_actual_receiver_parent_required')
    parent_hashes = manifest['source_sha256']
    expected = {'launch_receiver.py', 'assistant_mesh/__init__.py', 'assistant_mesh/native_partial_adapter.py',
                'assistant_mesh/native_partial_continuation.py', 'assistant_mesh/native_partial_methods.py'}
    if set(parent_hashes) != expected:
        raise ValueError('phone_overlay_parent_source_set_changed')
    originals = {}
    for relative in sorted(expected):
        data = read(parent / relative)
        if sha(data) != parent_hashes[relative]:
            raise ValueError('phone_overlay_parent_source_pin_changed')
        parse36(data); originals[relative] = data
    tree = parse36(originals['launch_receiver.py'])
    old_pins, old_sources = literal(tree, '_pins'), literal(tree, '_new_sources')
    if (old_pins != manifest['runtime_pins']
            or old_sources != {n: h for n, h in parent_hashes.items() if n != 'launch_receiver.py'}):
        raise ValueError('phone_overlay_parent_pins_changed')
    pins = dict(old_pins)
    # Keep all inherited runtime pins, and verify the actual parent release
    # still matches the captured public bytes before any runtime import.
    for relative, digest in dict(parent_hashes, **{PARENT_MANIFEST: sha(original_manifest)}).items():
        installed = str(Path(installed_parent) / relative)
        if installed in pins and pins[installed] != digest:
            raise ValueError('phone_overlay_conflicting_parent_pin')
        pins[installed] = digest
    outputs = {n: data for n, data in originals.items() if n != 'launch_receiver.py'}
    outputs['parent_launch_receiver.py'] = originals['launch_receiver.py']
    outputs[PARENT_MANIFEST] = original_manifest
    for module in MODULES:
        relative = 'assistant_mesh/' + module + '.py'
        data = subprocess.check_output(['git', '-C', str(source_root), 'show', source_commit + ':' + relative])
        parse36(data); outputs[relative] = data
    hashes = {n: sha(data) for n, data in outputs.items()}
    launcher = ("import hashlib\nimport importlib.util\nimport sys\nfrom pathlib import Path\n"
        "_root = Path(__file__).resolve().parent\nsys.path[0] = str(_root)\n"
        "_pins = " + repr(pins) + "\n_new_sources = " + repr(hashes) + "\n"
        "for _filename, _expected in list(_pins.items()) + [(str(_root / n), h) for n, h in _new_sources.items()]:\n"
        "    _path = Path(_filename)\n"
        "    if _path.is_symlink() or not _path.is_file() or hashlib.sha256(_path.read_bytes()).hexdigest() != _expected:\n"
        "        raise RuntimeError('phone_autonomy_overlay_runtime_pin_changed')\n"
        "# Preserve the namespace before the unchanged nested launchers run.\nimport assistant_mesh\n"
        "_spec = importlib.util.spec_from_file_location('_amita_phone_autonomy_parent', str(_root / 'parent_launch_receiver.py'))\n"
        "_parent = importlib.util.module_from_spec(_spec)\nsys.modules[_spec.name] = _parent\n"
        "_spec.loader.exec_module(_parent)\n"
        "if __name__ == '__main__':\n    _parent._entry.main()\n").encode('utf8')
    parse36(launcher); outputs['launch_receiver.py'] = launcher
    report = {'format': 'amita-phone-autonomy-overlay/1', 'component': 'receiver',
        'source_commit': source_commit, 'installed_target': installed_target, 'python_minimum': '3.6',
        'parent_installed_root': installed_parent, 'parent_manifest_sha256': sha(original_manifest),
        'parent_manifest': manifest, 'inherited_runtime_pins': old_pins, 'runtime_pins': pins,
        'source_sha256': {n: sha(data) for n, data in outputs.items()}, 'added_modules': list(MODULES),
        'parent_launcher_copy': 'parent_launch_receiver.py', 'original_ready_preserved': True,
        'namespace_init_preserved': True, 'protected_core_modules_preserved': list(PROTECTED),
        'adjacent_protocols': ['mesh-phone-authority/1', 'mesh-interactive-node/1', 'mesh-phone-maintenance/1',
                               'amita-phone-ui-control/1', 'amita-phone-owner-result/1'],
        'production_installed': False, 'end_to_end_verified': False, 'ledger_opened': False,
        'incumbent_files_modified': False, 'models_called': False}
    outputs['phone-autonomy-overlay.json'] = (json.dumps(report, sort_keys=True, indent=2) + '\n').encode('utf8')
    destination.mkdir(mode=0o700)
    for relative, data in outputs.items():
        path = destination / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as handle:
            os.fchmod(handle.fileno(), 0o600); handle.write(data)
    return {'destination': str(destination), 'manifest_sha256': sha(outputs['phone-autonomy-overlay.json']),
            'source_commit': source_commit, 'files': len(outputs), 'production_installed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('parent-snapshot', 'parent-manifest-sha256', 'source-commit', 'destination', 'installed-parent', 'installed-target'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--source-root', default=str(ROOT))
    print(json.dumps(build(**vars(parser.parse_args())), sort_keys=True))


if __name__ == '__main__':
    main()
