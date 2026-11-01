"""Build a pinned modern execution namespace around an incumbent launcher.

Source only: no incumbent imports, account/config/DB reads, processes or models.
The original launcher still owns Governance, profiles, receipts and rolling.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FORMAT = 'amita-native-worker-release-source/1'
PINS = {
    'codex': 'cde90cbb40039e2f1ecac52c864afe0b1a247573d1bf9d13d316f0bc50e4db41',
    'worker': '082fe07f42659b256e7447d41586da16d5e096b31b11370fa5189f66974ee943',
    'codex_provider': '5ff5c97c66e4090f4ef6bacefec037fdde39eab829610a96c8ddea639670034f',
    'model_provider_policy': '8c64c465cb227eb800100600480a824b22f14d13cd92eb7b4613f59ae5ca961b',
    'native_ws': '4d2bc715cc2676dd4b7793550872f300a7f3dbe1ee789b7d140a96eb5ad9246d',
    'native_goal_observer': '0dec24469800087382dd58c550535302e6a4c43ec7c36db64ee6bee450d796c1',
    'native_history': '5e06abac895a169a7738b52f8818b2cec64bf3d7e112d92f285a1bf076b7dd6c',
    'sessions': '61382191eff2f3979b817db0e07b9fc143c2544632362d3de23166304a3e5304',
    'reasoning': 'a9eae2d719be4288f1c4a8e71e6bd232e88fcf4c31177b7e5c6092f478099945',
    'native_partial_continuation': 'caa6bb6f4a3db9fac595cc9a8433ff2750b13e7545aa1ac487dcdd1bb6827f0c',
    'native_worker_release': 'd8e0c50a4013eec22735742e85682a310ee4179b05ce3ef0a9c9e505c0382c63',
}
CONVERTER_PIN = '992139b27fdb191bef9a98c681dbd55e8e37a4c8b7ad1f914625d675e7ceae62'
INCUMBENT_MODULES = ('worker', 'codex', 'config', 'governance_worker', 'leader_governance',
                     'metadata_diagnostics', 'task_scoped_profile', 'rolling_worker', 'rolling_runtime', 'reasoning', 'sessions')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('native_worker_release_absolute_path_required')
    return str(path)


def digest(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('native_worker_release_sha256_required')
    return value


def read(path, expected=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('native_worker_release_bounded_regular_source_required')
    raw = path.read_bytes()
    if expected is not None and sha(raw) != digest(expected):
        raise ValueError('native_worker_release_source_pin_changed')
    return raw


def functions(text, names):
    nodes = [node for node in ast.parse(text).body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in nodes} != set(names):
        raise ValueError('native_worker_release_exact_guard_functions_required')
    return '\n\n'.join(''.join(text.splitlines(keepends=True)[node.lineno - 1:node.end_lineno]) for node in nodes)


def pure_closure(files):
    names = {Path(name).stem for name in files if name.endswith('.py')} - {'__init__'}
    graph = {}
    for filename, text in files.items():
        if not filename.endswith('.py') or filename == '__init__.py':
            continue
        imported = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.level == 1:
                    imported.update([node.module.split('.')[0]] if node.module else [alias.name for alias in node.names])
                elif not (Path(filename).stem == 'config' and node.level == 2 and node.module == 'config'):
                    raise ValueError('native_worker_release_import_boundary_changed')
        if not imported <= names:
            raise ValueError('native_worker_release_incomplete_pure_namespace')
        graph[Path(filename).stem] = sorted(imported)
    return graph


def build(destination, *, launcher, launcher_sha256, installed_launcher, package_init,
          init_sha256, installed_init, incumbent_manifest, policy_file, support_root=ROOT):
    root, support = Path(destination), Path(support_root)
    if (not root.is_absolute() or '..' in root.parts or root.exists() or root.is_symlink()
            or any(parent.is_symlink() for parent in root.parents)):
        raise ValueError('native_worker_release_new_destination_required')
    old_launcher, init = read(launcher, launcher_sha256), read(package_init, init_sha256)
    # The shim depends on the original construction contract, never rewrites it.
    for required in ('TaskScopedProfile', 'Governance', 'MetadataDiagnostics', 'install_save_observer',
                     'observe_worker', 'run_worker', 'def factory()', 'def main()'):
        if required not in old_launcher.decode('utf8'):
            raise ValueError('native_worker_release_incumbent_launcher_contract_changed')
    pins = {absolute(installed_launcher): digest(launcher_sha256), absolute(installed_init): digest(init_sha256)}
    manifest = json.loads(read(incumbent_manifest))
    if not isinstance(manifest, dict) or set(manifest) != set(INCUMBENT_MODULES):
        raise ValueError('native_worker_release_explicit_incumbent_module_manifest_required')
    for name, item in manifest.items():
        if not isinstance(item, dict) or set(item) != {'snapshot', 'installed', 'sha256'}:
            raise ValueError('native_worker_release_module_binding_invalid')
        raw = read(item['snapshot'], item['sha256'])
        if Path(item['snapshot']).suffix != '.py' or Path(item['installed']).name != name + '.py':
            raise ValueError('native_worker_release_python_source_only')
        installed, expected = absolute(item['installed']), digest(item['sha256'])
        if installed in pins and pins[installed] != expected:
            raise ValueError('native_worker_release_conflicting_incumbent_pin')
        pins[installed] = expected
        if name == 'worker':
            owner = next(node for node in ast.parse(raw).body if isinstance(node, ast.ClassDef) and node.name == 'Worker')
            methods = {node.name for node in owner.body if isinstance(node, ast.FunctionDef)}
            if not {'resource_reference', 'heartbeat', 'on_tool', 'on_interaction', 'open_backend'} <= methods:
                raise ValueError('native_worker_release_incumbent_worker_seams_required')
    sources = {name: read(support / 'assistant_mesh' / (name + '.py'), expected).decode('utf8') for name, expected in PINS.items()}
    converter_path = support / 'deploy/build-native-goal-scoped-candidate.py'
    read(converter_path, CONVERTER_PIN)
    spec = importlib.util.spec_from_file_location('_native_worker_qualified_converter', str(converter_path))
    converter = importlib.util.module_from_spec(spec); spec.loader.exec_module(converter)
    codex = converter.backend_source(sources['codex'])
    expected = 'from .native_partial_continuation import validate_resume_goal'
    if codex.count(expected) != 1:
        raise ValueError('native_worker_release_resume_guard_seam_changed')
    codex = codex.replace(expected, 'from .native_partial_resume_guard import validate_resume_goal', 1)
    guard = ('import hashlib, json\nfrom .codex import CodexError\n\n'
             'def require(value, code):\n    if not value:\n        raise CodexError("native_partial_" + code)\n\n'
             'PROTOCOL = "owner-native-partial-continuation/1"\n\n' + functions(
                 sources['native_partial_continuation'], ('encoded', 'digest', 'validate_resume_goal')) + '\n')
    pure = {'__init__.py': '"""Pinned modern native execution; imports have no startup."""\n',
            'config.py': 'from ..config import discover_codex_auth, private_json\n',
            'codex.py': codex, 'worker_strategy.py': converter.scoped_worker_source(sources['worker']),
            'native_partial_resume_guard.py': guard}
    for name in ('codex_provider', 'model_provider_policy', 'native_ws', 'native_goal_observer', 'native_history', 'sessions', 'reasoning'):
        pure[name + '.py'] = sources[name]
    graph = pure_closure(pure)
    files = {'assistant_mesh/native_worker_runtime/' + name: text.encode('utf8') for name, text in pure.items()}
    files['assistant_mesh/native_worker_release.py'] = sources['native_worker_release'].encode('utf8')
    files['assistant_mesh/__init__.py'] = ('__path__.append(' + repr(str(Path(installed_init).parent)) + ')\n').encode() + init
    # Existing Governance still calls its original make_codex interface. Only
    # this release opts into catalog-checked/returned native thread settings.
    files['assistant_mesh/reasoning.py'] = (
        'from .native_worker_runtime.reasoning import *\n'
        'from .native_worker_runtime.reasoning import make_codex as _modern_make_codex\n'
        'def make_codex(base_class, profile_loader, receipt_callback=None):\n'
        '    return _modern_make_codex(base_class, profile_loader, receipt_callback, thread_profile=True)\n').encode()
    hashes = {name: sha(raw) for name, raw in files.items()}
    launch = ("import hashlib, importlib.util, sys\nfrom pathlib import Path\n"
              "_root = Path(__file__).resolve().parent\nsys.path[0] = str(_root)\n"
              "_pins = " + repr(pins) + "\n_new_sources = " + repr(hashes) + "\n"
              "for _name, _expected in list(_pins.items()) + [(str(_root / n), h) for n,h in _new_sources.items()]:\n"
              "    _path=Path(_name)\n"
              "    if _path.is_symlink() or not _path.is_file() or hashlib.sha256(_path.read_bytes()).hexdigest()!=_expected:\n"
              "        raise RuntimeError('native_worker_release_runtime_pin_changed')\n"
              "import assistant_mesh\n"
              "_spec=importlib.util.spec_from_file_location('_native_worker_incumbent_launcher', " + repr(absolute(installed_launcher)) + ")\n"
              "_incumbent=importlib.util.module_from_spec(_spec)\nsys.modules[_spec.name]=_incumbent\n_spec.loader.exec_module(_incumbent)\n"
              "from assistant_mesh.native_worker_release import install\n"
              "from assistant_mesh.native_worker_runtime.codex import Codex\n"
              "from assistant_mesh.native_worker_runtime.worker_strategy import WorkerStrategy\n"
              "from assistant_mesh.native_worker_runtime import sessions\n"
              "from assistant_mesh.config import private_json\n"
              "if __name__=='__main__':\n"
              "    install(_incumbent, private_json(" + repr(absolute(policy_file)) + "), Codex, WorkerStrategy, sessions)\n"
              "    _incumbent.main()\n")
    files['launch_worker.py'] = launch.encode()
    for raw in files.values():
        ast.parse(raw.decode('utf8'), feature_version=(3, 6))
    report = {'format': FORMAT, 'algorithm_pins': PINS, 'converter_sha256': CONVERTER_PIN,
              'incumbent_pins': pins, 'source_sha256': {name: sha(raw) for name, raw in files.items()},
              'native_import_graph': graph, 'policy_file': absolute(policy_file),
              'original_launcher_main_used': True, 'original_governance_preserved': True,
              'original_config_auth_home_preserved': True, 'original_rolling_protocol_preserved': True,
              'authority_store_api_deployed': False, 'model_called': False, 'production_installed': False,
              'effective_model_effort_verified': False,
              'unsupported': ['Pi-configured release', 'implicit cross-account/native history migration']}
    root.mkdir(mode=0o700)
    for relative, raw in files.items():
        path = root / relative; path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600); stream.write(raw)
    with (root / 'native-worker-release.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600); json.dump(report, stream, sort_keys=True, indent=2); stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('destination', 'launcher', 'launcher-sha256', 'installed-launcher', 'package-init',
                 'init-sha256', 'installed-init', 'incumbent-manifest', 'policy-file'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--support-root', default=str(ROOT))
    print(json.dumps(build(**vars(parser.parse_args())), sort_keys=True))


if __name__ == '__main__':
    main()
