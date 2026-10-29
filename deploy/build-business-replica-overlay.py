"""Assemble additive SOURCE overlays for explicit incumbent receiver/node entrypoints.

No runtime imports, DB/config reads, services, network requests or installation.
Local source snapshots and installed runtime paths are separate explicit inputs.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FORMAT = 'amita-business-replica-overlay/1'
MODULES = ('business_journal', 'business_replica_api', 'business_replica')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError('business_overlay_sha256_required')
    return value


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('business_overlay_absolute_runtime_path_required')
    return str(path)


def read_source(path, expected=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('business_overlay_bounded_regular_source_required')
    raw = path.read_bytes()
    if expected is not None and sha(raw) != digest(expected):
        raise ValueError('business_overlay_source_pin_changed')
    return raw


def add_pin(pins, path, expected):
    path, expected = absolute(path), digest(expected)
    if path in pins and pins[path] != expected:
        raise ValueError('business_overlay_conflicting_runtime_pin')
    pins[path] = expected


def runtime_header(pins, module_hashes):
    # Resolve the final script location after transfer; never embed build paths.
    return ("import hashlib\nimport importlib.util\nimport sys\nfrom pathlib import Path\n"
        "_root = Path(__file__).resolve().parent\n"
        "sys.path[0] = str(_root)\n"
        "_runtime_pins = " + repr(pins) + "\n"
        "_module_hashes = " + repr(module_hashes) + "\n"
        "for _filename, _expected in list(_runtime_pins.items()) + [\n"
        "        (str(_root / name), value) for name, value in _module_hashes.items()]:\n"
        "    _path = Path(_filename)\n"
        "    if (_path.is_symlink() or not _path.is_file()\n"
        "            or hashlib.sha256(_path.read_bytes()).hexdigest() != _expected):\n"
        "        raise RuntimeError('business_overlay_runtime_pin_changed')\n"
        "from assistant_mesh.store import Store\n"
        "from assistant_mesh.business_journal import hook_store_type\n"
        "hook_store_type(Store)\n")


def build(component, destination, package_init, init_sha256, installed_init,
          support_root=ROOT, runtime_pins=None, launcher=None, launcher_sha256=None,
          installed_launcher=None, policy_file=None, snapshot_ttl=600):
    if component not in ('receiver', 'node'):
        raise ValueError('business_overlay_explicit_component_required')
    root = Path(destination)
    if (not root.is_absolute() or '..' in root.parts or root.exists() or root.is_symlink()
            or any(parent.is_symlink() for parent in root.parents)):
        raise ValueError('business_overlay_new_absolute_destination_required')
    pins = {}
    if runtime_pins is not None:
        extra = json.loads(read_source(runtime_pins).decode('utf8'))
        if not isinstance(extra, dict):
            raise ValueError('business_overlay_runtime_pins_object_required')
        for path, expected in extra.items():
            add_pin(pins, path, expected)
    init = read_source(package_init, init_sha256)
    add_pin(pins, installed_init, init_sha256)
    if Path(installed_init).name != '__init__.py':
        raise ValueError('business_overlay_actual_namespace_init_required')
    outputs = {}
    for name in MODULES:
        outputs['assistant_mesh/' + name + '.py'] = read_source(
            Path(support_root) / 'assistant_mesh' / (name + '.py'))
    # The new package directory remains first; original namespace chains follow.
    outputs['assistant_mesh/__init__.py'] = (
        '__path__.append(' + repr(str(Path(installed_init).parent)) + ')\n').encode('utf8') + init
    if component == 'receiver':
        read_source(launcher, launcher_sha256)
        installed_launcher = absolute(installed_launcher)
        add_pin(pins, installed_launcher, launcher_sha256)
        policy_file = absolute(policy_file)
        if type(snapshot_ttl) is not int or not 10 <= snapshot_ttl <= 600:
            raise ValueError('business_overlay_snapshot_ttl_invalid')
    elif any(value is not None for value in (launcher, launcher_sha256, installed_launcher, policy_file)):
        raise ValueError('business_overlay_node_has_no_receiver_launcher')
    module_hashes = {name: sha(raw) for name, raw in outputs.items()}
    source = runtime_header(pins, module_hashes)
    if component == 'receiver':
        source += (
            "_spec = importlib.util.spec_from_file_location(\n"
            "    '_amita_business_replica_incumbent_receiver', " + repr(installed_launcher) + ")\n"
            "_incumbent = importlib.util.module_from_spec(_spec)\n"
            "sys.modules[_spec.name] = _incumbent\n"
            "_spec.loader.exec_module(_incumbent)\n"
            "_entry = _incumbent._recovery_incumbent._incumbent.previous\n"
            "_previous_ready = _entry.ready\n"
            "from assistant_mesh.business_journal import hook_store\n"
            "from assistant_mesh.business_replica_api import attach\n"
            "from assistant_mesh.config import private_json\n"
            "def _ready(http_server, api, channel):\n"
            "    hook_store(api.store)\n"
            "    journal = attach(api, private_json(" + repr(policy_file) + "))\n"
            "    journal.snapshot_ttl = " + repr(snapshot_ttl) + "\n"
            "    return _previous_ready(http_server, api, channel)\n"
            "_entry.ready = _ready\n"
            "if __name__ == '__main__':\n"
            "    _entry.main()\n")
    else:
        source += ("from assistant_mesh.cli import main\n"
                   "if __name__ == '__main__':\n    raise SystemExit(main())\n")
    launcher_name = 'launch_' + component + '.py'
    outputs[launcher_name] = source.encode('utf8')
    # Parse as source only. In particular, never import actual incumbent code.
    for raw in outputs.values():
        ast.parse(raw.decode('utf8'))
    report = dict(format=FORMAT, component=component, runtime_pins=pins,
                  module_sha256={name: sha(raw) for name, raw in outputs.items()},
                  launcher=launcher_name, production_installed=False, ledger_opened=False,
                  incumbent_files_modified=False, constructor_business_logic_preserved=True,
                  redundancy='degraded', execution_authorized=False)
    if component == 'receiver':
        report.update(policy_file=policy_file, snapshot_ttl=snapshot_ttl,
                      incumbent_entry='_incumbent._recovery_incumbent._incumbent.previous')
    else:
        report.update(argv_preserved=True, required_node_config={
            'start_server': False, 'start_worker': False}, node_config_verified=False)
    root.mkdir(mode=0o700)
    for name, raw in outputs.items():
        path = root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
    with (root / 'business-replica-overlay.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, ensure_ascii=True, sort_keys=True, indent=2)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='component')
    for component in ('receiver', 'node'):
        command = commands.add_parser(component)
        for name in ('destination', 'package-init', 'init-sha256', 'installed-init'):
            command.add_argument('--' + name, required=True)
        command.add_argument('--support-root', default=str(ROOT))
        command.add_argument('--runtime-pins', help='JSON object: installed absolute source path -> SHA256')
        if component == 'receiver':
            for name in ('launcher', 'launcher-sha256', 'installed-launcher', 'policy-file'):
                command.add_argument('--' + name, required=True)
            command.add_argument('--snapshot-ttl', type=int, default=600)
    args = vars(parser.parse_args())
    if args['component'] is None:
        parser.error('receiver or node required')
    report = build(**args)
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
