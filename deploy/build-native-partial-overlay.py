"""Pinned additive S3 source assembly, never a production deployment.

Explicit captured sources and installed paths are separate. No incumbent import,
DB/account/config read, process, model, listener or live action occurs at build.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FORMAT = 'amita-native-partial-overlay/1'
SUPPORT_PINS = {
    'native_partial_continuation.py': '73f378520bbd1e14669ab1ddf1fee428a7d47f7e0f89d1916e47723081c9b386',
    'native_partial_adapter.py': 'fe82c7fc1202d9413820271c8ebfce7fbd41e016814500396089fe6d043097d2',
    'store.py': 'a12292767c356cbbbb887c6cd91d17ceac2f80bfc936259e5d4307660b1231e0',
}
HELPER_PIN = '49a61b719593b64505cd5dd902ca784b97a6626a705673039d6da7a0fa111a73'
TABLES = ['native_partial_acceptances', 'native_partial_before_chunks', 'native_partial_history_parts']


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('native_partial_overlay_sha256_required')
    return value


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('native_partial_overlay_absolute_path_required')
    return str(path)


def read(path, expected=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('native_partial_overlay_bounded_regular_source_required')
    raw = path.read_bytes()
    if expected is not None and sha(raw) != digest(expected):
        raise ValueError('native_partial_overlay_source_pin_changed')
    return raw


def add_pin(pins, installed, expected):
    installed, expected = absolute(installed), digest(expected)
    if installed in pins and pins[installed] != expected:
        raise ValueError('native_partial_overlay_conflicting_pin')
    pins[installed] = expected


def method(raw, cls, name):
    text = raw.decode('utf8')
    owner = next(item for item in ast.parse(text).body if isinstance(item, ast.ClassDef) and item.name == cls)
    item = next(item for item in owner.body if isinstance(item, ast.FunctionDef) and item.name == name)
    return ''.join(text.splitlines(keepends=True)[item.lineno - 1:item.end_lineno])


def attributes(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*', value):
        raise ValueError('native_partial_overlay_explicit_entry_attribute_required')
    return value.split('.')


def header(pins, outputs, installed_launcher):
    hashes = {name: sha(raw) for name, raw in outputs.items()}
    return ("import hashlib\nimport importlib.util\nimport sys\nfrom pathlib import Path\n"
            "_root = Path(__file__).resolve().parent\n"
            "sys.path[0] = str(_root)\n"
            "_pins = " + repr(pins) + "\n"
            "_new_sources = " + repr(hashes) + "\n"
            "for _filename, _expected in list(_pins.items()) + [(str(_root / n), h) for n, h in _new_sources.items()]:\n"
            "    _path = Path(_filename)\n"
            "    if _path.is_symlink() or not _path.is_file() or hashlib.sha256(_path.read_bytes()).hexdigest() != _expected:\n"
            "        raise RuntimeError('native_partial_overlay_runtime_pin_changed')\n"
            "# Anchor the new package before an old launcher adjusts sys.path.\n"
            "import assistant_mesh\n"
            "_spec = importlib.util.spec_from_file_location('_amita_native_partial_incumbent', " + repr(installed_launcher) + ")\n"
            "_incumbent = importlib.util.module_from_spec(_spec)\n"
            "sys.modules[_spec.name] = _incumbent\n"
            "_spec.loader.exec_module(_incumbent)\n")


def patch_codex(raw):
    body = method(raw, 'Codex', '_read_goal_snapshot')
    before = "        self._validate_goal_snapshot(response['goal'])\n        goal = response['goal']\n"
    guard = "        from .native_partial_continuation import validate_resume_goal\n        validate_resume_goal(checkpoint, goal)\n"
    if body.count(before) != 1 or body.count('self._consume_prior_notifications()') != 1:
        raise ValueError('native_partial_overlay_actual_goal_read_seam_required')
    if guard in body:
        if body.count(guard) != 1 or body.count(before + guard) != 1:
            raise ValueError('native_partial_overlay_goal_guard_ambiguous')
        return raw
    if 'validate_resume_goal' in raw.decode('utf8'):
        raise ValueError('native_partial_overlay_existing_goal_guard_changed')
    text = raw.decode('utf8')
    if text.count(body) != 1:
        raise ValueError('native_partial_overlay_exact_codex_method_required')
    return text.replace(body, body.replace(before, before + guard, 1), 1).encode('utf8')


def build(component, destination, *, package_init, init_sha256, installed_init,
          launcher, launcher_sha256, installed_launcher, support_root=ROOT,
          runtime_pins=None, incumbent_store=None, store_sha256=None, installed_store=None,
          entry_attribute=None, maintenance_config=None, codex=None, codex_sha256=None, installed_codex=None):
    if component not in ('receiver', 'worker'):
        raise ValueError('native_partial_overlay_explicit_component_required')
    root = Path(destination)
    if (not root.is_absolute() or '..' in root.parts or root.exists() or root.is_symlink()
            or any(parent.is_symlink() for parent in root.parents)):
        raise ValueError('native_partial_overlay_new_destination_required')
    pins = {}
    if runtime_pins is not None:
        extra = json.loads(read(runtime_pins).decode('utf8'))
        if not isinstance(extra, dict):
            raise ValueError('native_partial_overlay_runtime_pins_object_required')
        for installed, expected in extra.items():
            add_pin(pins, installed, expected)
    init = read(package_init, init_sha256)
    read(launcher, launcher_sha256)
    add_pin(pins, installed_init, init_sha256)
    add_pin(pins, installed_launcher, launcher_sha256)
    if Path(installed_init).name != '__init__.py':
        raise ValueError('native_partial_overlay_actual_namespace_init_required')
    support = Path(support_root) / 'assistant_mesh'
    full_methods = read(support / 'store.py', SUPPORT_PINS['store.py'])
    helper = method(full_methods, 'Store', '_assert_continuation_quiescent')
    if sha(helper.encode('utf8')) != HELPER_PIN:
        raise ValueError('native_partial_overlay_shared_helper_changed')
    outputs = {'assistant_mesh/__init__.py':
               ('__path__.append(' + repr(str(Path(installed_init).parent)) + ')\n').encode('utf8') + init,
               'assistant_mesh/native_partial_continuation.py': read(support / 'native_partial_continuation.py', SUPPORT_PINS['native_partial_continuation.py'])}
    if component == 'receiver':
        if any(value is not None for value in (codex, codex_sha256, installed_codex)):
            raise ValueError('native_partial_overlay_receiver_never_replaces_codex')
        read(incumbent_store, store_sha256)
        add_pin(pins, installed_store, store_sha256)
        entry = attributes(entry_attribute)
        maintenance_config = absolute(maintenance_config)
        outputs['assistant_mesh/native_partial_adapter.py'] = read(support / 'native_partial_adapter.py', SUPPORT_PINS['native_partial_adapter.py'])
        outputs['assistant_mesh/native_partial_methods.py'] = (
            'import json\nfrom . import operation_isolation as isolation\nfrom .store import Conflict\n\n'
            'class PartialContinuationMethods:\n' + helper + '\n').encode('utf8')
    else:
        if any(value is not None for value in (incumbent_store, store_sha256, installed_store, entry_attribute, maintenance_config)):
            raise ValueError('native_partial_overlay_worker_has_no_receiver_authority')
        original = read(codex, codex_sha256)
        add_pin(pins, installed_codex, codex_sha256)
        try:
            outputs['assistant_mesh/codex.py'] = patch_codex(original)
        except (StopIteration, ValueError) as exc:
            raise ValueError('native_partial_overlay_selected_modern_goal_namespace_required') from exc
    source = header(pins, outputs, absolute(installed_launcher))
    if component == 'receiver':
        source += ("_entry = _incumbent\n"
                   "for _attribute in " + repr(entry) + ":\n    _entry = getattr(_entry, _attribute)\n"
                   "_previous_ready = _entry.ready\n"
                   "from assistant_mesh.native_partial_adapter import install_schema, attach\n"
                   "from assistant_mesh.native_partial_methods import PartialContinuationMethods\n"
                   "from assistant_mesh.config import private_json\n"
                   "def _ready(http_server, api, channel):\n"
                   "    # Create EMPTY named tables before original journal ready; do not refresh.\n"
                   "    install_schema(api.store, PartialContinuationMethods)\n"
                   "    result = _previous_ready(http_server, api, channel)\n"
                   "    attach(api, private_json(" + repr(maintenance_config) + "), PartialContinuationMethods)\n"
                   "    return result\n"
                   "_entry.ready = _ready\n"
                   "if __name__ == '__main__':\n    _entry.main()\n")
    else:
        source += "if __name__ == '__main__':\n    _incumbent.main()\n"
    outputs['launch_' + component + '.py'] = source.encode('utf8')
    for raw in outputs.values():
        ast.parse(raw.decode('utf8'), feature_version=(3, 6))
    report = {'format': FORMAT, 'component': component, 'runtime_pins': pins,
              'source_sha256': {name: sha(raw) for name, raw in outputs.items()},
              'support_pins': dict(SUPPORT_PINS), 'quiescent_helper_sha256': HELPER_PIN,
              'protected_core_modules_preserved': ['store', 'server', 'worker', 'entrance', 'native_history', 'sessions'],
              'production_installed': False, 'ledger_opened': False, 'incumbent_files_modified': False,
              'effective_model_effort_verified': False, 'tasks_reconciled': False,
              'replication_refresh_performed': False, 'required_global_tables': TABLES}
    if component == 'receiver':
        report.update(entry_attribute=entry_attribute, maintenance_config=maintenance_config,
                      original_ready_preserved=True, ordinary_operator_maintenance_authorized=False)
    else:
        report.update(actual_codex_source=absolute(installed_codex), codex_guard='actual_goal_read_before_prior_notifications',
                      incumbent_codex_sha256=codex_sha256, model_profile_modified=False)
    root.mkdir(mode=0o700)
    for relative, raw in outputs.items():
        path = root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
    with (root / 'native-partial-overlay.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='component')
    for component in ('receiver', 'worker'):
        command = commands.add_parser(component)
        for name in ('destination', 'package-init', 'init-sha256', 'installed-init',
                     'launcher', 'launcher-sha256', 'installed-launcher'):
            command.add_argument('--' + name, required=True)
        command.add_argument('--support-root', default=str(ROOT))
        command.add_argument('--runtime-pins')
        required = ('incumbent-store', 'store-sha256', 'installed-store', 'entry-attribute', 'maintenance-config') if component == 'receiver' else ('codex', 'codex-sha256', 'installed-codex')
        for name in required:
            command.add_argument('--' + name, required=True)
    args = vars(parser.parse_args())
    if args['component'] is None:
        parser.error('receiver or worker required')
    print(json.dumps(build(**args), sort_keys=True))


if __name__ == '__main__':
    main()
