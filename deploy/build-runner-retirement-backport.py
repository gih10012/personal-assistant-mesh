"""Pinned SOURCE-only cooperative retirement for the inspected old parent.

No production DB/config/token reads, incumbent imports, signals, process,
services or model calls. Installed paths are explicit, independent of snapshots.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FORMAT = 'amita-runner-retirement-backport/1'
PINS = {
    'runtime': 'c72ecc52a11649cc2968e2da5c5561efa6cf9ddfe8e7d70c60ac6e4e6ae30d17',
    'base_runtime': '1d6feb38369d62d09f8bdf4f05a998fd8fb614892d0a43766459ca25be347271',
    'package_init': '13984e72db4ee8493e47b78b2c1475623616cd74c54f68f49928a264d1fca1cb',
    'launcher': '965ee94c883ce5faf0a2f13e153dcc60dd140259b95fb69721a1e766d72bd073',
}
RETIRE_METHOD_PIN = '7c9c5131c4266cbdf01b2602b974deb22853dca055ef387a88a2cad674dd021e'
CLIENT_ADAPTER_PIN = '05a83f8461715b9d2c3c9fa88eb416b092720a12bbed511fefc98f7fe3f7c3de'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, expected=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('retirement_bounded_regular_source_required')
    raw = path.read_bytes()
    if expected is not None and sha(raw) != expected:
        raise ValueError('retirement_actual_source_pin_changed')
    return raw


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('retirement_absolute_installed_path_required')
    return str(path)


def method(raw, cls, name):
    text = raw.decode('utf8')
    owner = next(item for item in ast.parse(text).body if isinstance(item, ast.ClassDef) and item.name == cls)
    item = next(item for item in owner.body if isinstance(item, ast.FunctionDef) and item.name == name)
    return ''.join(text.splitlines(keepends=True)[item.lineno - 1:item.end_lineno])


def build(destination, runtime, installed_runtime, base_runtime, installed_base_runtime,
          package_init, installed_init, launcher, installed_launcher, maintenance_client_config,
          support_root=ROOT, runtime_pins=None):
    root = Path(destination)
    if (not root.is_absolute() or root.exists() or root.is_symlink() or '..' in root.parts
            or any(parent.is_symlink() for parent in root.parents)):
        raise ValueError('retirement_new_absolute_destination_required')
    sources = {'runtime': runtime, 'base_runtime': base_runtime, 'package_init': package_init, 'launcher': launcher}
    raw = {key: read(path, PINS[key]) for key, path in sources.items()}
    installed = {'runtime': absolute(installed_runtime), 'base_runtime': absolute(installed_base_runtime),
                 'package_init': absolute(installed_init), 'launcher': absolute(installed_launcher)}
    fixed_config = absolute(maintenance_client_config)
    pins = {installed[key]: PINS[key] for key in sources}
    if runtime_pins is not None:
        extra = json.loads(read(runtime_pins).decode('utf8'))
        if not isinstance(extra, dict):
            raise ValueError('retirement_additional_pins_object_required')
        for path, expected in extra.items():
            path = absolute(path)
            if (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)
                    or (path in pins and pins[path] != expected)):
                raise ValueError('retirement_additional_pin_invalid')
            pins[path] = expected
    # This recovery wrapper itself pins a specific old base path. Never guess
    # a fallback, silently use modern core or import a supplied source at build.
    tree = ast.parse(raw['runtime'].decode('utf8'))
    actual_base = next(item.value.args[0].value for item in tree.body
        if isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '_source' for target in item.targets))
    if actual_base != installed['base_runtime']:
        raise ValueError('retirement_explicit_fallback_binding_changed')
    retire = method(read(Path(support_root) / 'assistant_mesh/rolling_runtime.py'), 'RuntimeStore', 'retire_closed_runner')
    if sha(retire.encode('utf8')) != RETIRE_METHOD_PIN:
        raise ValueError('retirement_reviewed_method_pin_changed')
    runner = method(raw['base_runtime'], 'RuntimeStore', 'runner_request')
    seam = "                work = json.loads(row['work']) if row['work'] else None\n"
    if runner.count(seam) != 1:
        raise ValueError('retirement_original_authenticated_heartbeat_seam_changed')
    runner = runner.replace(seam, seam +
        "                if (action == 'heartbeat' and db.execute(\"SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_closed_runner_retirements'\").fetchone()\n"
        "                        and db.execute('SELECT 1 FROM native_closed_runner_retirements WHERE runner=?', (runner,)).fetchone()):\n"
        "                    return {'state': 'stopped', 'busy': work is not None, 'protocol': PROTOCOL}\n", 1)
    outputs = {'assistant_mesh/__init__.py': (
        '__path__.append(' + repr(str(Path(installed['package_init']).parent)) + ')\n').encode('utf8') + raw['package_init'],
        'assistant_mesh/runner_retirement.py': read(Path(support_root) / 'assistant_mesh/runner_retirement.py', CLIENT_ADAPTER_PIN)}
    header = ("import hashlib\nimport importlib.util\nimport sys\nfrom pathlib import Path\n"
        "_retirement_pins = " + repr(pins) + "\n"
        "for _filename, _expected in _retirement_pins.items():\n"
        "    _path = Path(_filename)\n"
        "    if _path.is_symlink() or hashlib.sha256(_path.read_bytes()).hexdigest() != _expected:\n"
        "        raise RuntimeError('retirement_runtime_source_pin_changed')\n"
        "_retirement_spec = importlib.util.spec_from_file_location(__package__ + '._retirement_incumbent', " + repr(installed['runtime']) + ")\n"
        "_retirement_incumbent = importlib.util.module_from_spec(_retirement_spec)\n"
        "sys.modules[_retirement_spec.name] = _retirement_incumbent\n"
        "_retirement_spec.loader.exec_module(_retirement_incumbent)\n"
        "for _export_name, _export_value in vars(_retirement_incumbent).items():\n"
        "    if not _export_name.startswith('__'):\n        globals()[_export_name] = _export_value\n")
    body = (header + '\nclass _RetirementMethods:\n' + runner + '\n' + retire + '\n'
        'RuntimeStore.runner_request = _RetirementMethods.runner_request\n'
        'RuntimeStore.retire_closed_runner = _RetirementMethods.retire_closed_runner\n'
        'RuntimeStore.retirement_resolver = None\n'
        '_retirement_original_dispatch = Supervisor.dispatch\n'
        'def _retirement_dispatch(self, request, peer_pid):\n'
        "    if request.get('runner') is not None or request.get('action') != 'retire_closed':\n"
        '        return _retirement_original_dispatch(self, request, peer_pid)\n'
        "    if request.get('protocol') != PROTOCOL:\n        raise ValueError('runtime_protocol_incompatible')\n"
        "    body = request.get('body')\n"
        "    if (set(request) != {'protocol', 'action', 'body'} or not isinstance(body, dict)\n"
        "            or set(body) != {'operation', 'runner', 'acceptance_id'}):\n"
        "        raise ValueError('runtime_retirement_control_shape_invalid')\n"
        "    return self.store.retire_closed_runner(body['operation'], body['runner'], body['acceptance_id'])\n"
        'Supervisor.dispatch = _retirement_dispatch\n'
        "if __name__ == '__main__':\n    sys.exit(main())\n")
    outputs['assistant_mesh/rolling_runtime.py'] = body.encode('utf8')
    hashes = {key: sha(value) for key, value in outputs.items()}
    outputs['launch_control.py'] = ("import hashlib\nimport sys\nfrom pathlib import Path\n"
        "_root = Path(__file__).resolve().parent\nsys.path[0] = str(_root)\n"
        "for _name, _expected in " + repr(hashes) + ".items():\n"
        "    _path = _root / _name\n"
        "    if _path.is_symlink() or hashlib.sha256(_path.read_bytes()).hexdigest() != _expected:\n"
        "        raise RuntimeError('retirement_overlay_source_changed')\n"
        'from assistant_mesh import rolling_runtime\n'
        'from assistant_mesh.runner_retirement import install\n'
        'install(rolling_runtime, ' + repr(fixed_config) + ')\n'
        'raise SystemExit(rolling_runtime.main())\n').encode('utf8')
    for value in outputs.values():
        ast.parse(value.decode('utf8'), feature_version=(3, 6))
    report = dict(format=FORMAT, original_runtime_pins=pins,
        module_sha256={key: sha(value) for key, value in outputs.items()}, maintenance_client_config=fixed_config,
        overridden_methods=['RuntimeStore.runner_request', 'RuntimeStore.retire_closed_runner', 'Supervisor.dispatch'],
        original_constructor_preserved=True, original_transaction_preserved=True, original_recovery_preserved=True,
        production_installed=False, ledger_opened=False, credentials_read=False, processes_signaled=False)
    root.mkdir(mode=0o700)
    for name, value in outputs.items():
        target = root / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
    with (root / 'runner-retirement-overlay.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('destination', 'runtime', 'installed-runtime', 'base-runtime', 'installed-base-runtime',
                 'package-init', 'installed-init', 'launcher', 'installed-launcher', 'maintenance-client-config'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--support-root', default=str(ROOT))
    parser.add_argument('--runtime-pins')
    print(json.dumps(build(**vars(parser.parse_args())), sort_keys=True))


if __name__ == '__main__':
    main()
