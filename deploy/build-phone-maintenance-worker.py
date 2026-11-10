"""Build a private exact-device role candidate; never activate or call models."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MODERN = ('leader_governance', 'governance_worker', 'model_recovery', 'model_advice', 'model_frontier')


def sha(data): return hashlib.sha256(data).hexdigest()


def read(path, maximum=2 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('phone_role_regular_source_required')
    return path.read_bytes()


def assignment(data, name):
    rows = [n for n in ast.parse(data.decode()).body if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name]
    if len(rows) != 1: raise ValueError('phone_role_parent_pin_seam_changed')
    return ast.literal_eval(rows[0].value)


def write(path, data):
    path = Path(path); path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('xb') as handle:
        os.fchmod(handle.fileno(), 0o600); handle.write(data)


def encoded(value): return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def build(destination, parent_release, installed_target, first_start, worker_config, native_policy,
          governance_policy, governance_database, models_cache, previous_spec, source_root=ROOT):
    destination, parent = Path(destination), Path(parent_release)
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise ValueError('phone_role_new_private_destination_required')
    commit = subprocess.check_output(['git', '-C', str(source_root), 'rev-parse', 'HEAD']).decode().strip()
    start = json.loads(read(first_start).decode()); node = start['node_id']
    scope = 'phone-maintenance:' + hashlib.sha256(node.encode()).hexdigest()[:40]
    worker_raw, native_raw, policy_raw = read(worker_config), read(native_policy), read(governance_policy)
    worker, native, policy = json.loads(worker_raw.decode()), json.loads(native_raw.decode()), json.loads(policy_raw.decode())
    if worker.get('node_id') != 'laptop' or 'phone-maintenance' in worker.get('capabilities', []):
        raise ValueError('phone_role_original_laptop_configuration_required')
    if any(row['scope'] == scope for row in native['additional_profiles']):
        raise ValueError('phone_role_do_not_hard_bind_legacy_profile')
    # The owner's official auth-home cache is a managed symlink. Resolve only
    # this non-secret metadata cache; source/runtime pins remain no-symlink.
    cache = json.loads(read(Path(models_cache).resolve(), maximum=16 * 1024 * 1024).decode())
    model = next((m for m in cache['models'] if m.get('slug') == 'gpt-6-luna'), None)
    if model is None or 'max' not in [r['effort'] for r in model.get('supported_reasoning_levels', [])]:
        raise ValueError('phone_role_local_catalog_selection_missing')
    ordinary = next((r for r in policy['routes'] if r['id'] == policy['defaults']['D1']), None)
    if ordinary is None or (ordinary['model'], ordinary['effort'], ordinary['cost_basis']) != ('gpt-6-luna', 'max', 'included_subscription'):
        raise ValueError('phone_role_review_changed_owner_policy')
    parent_launcher, parent_init = read(parent / 'launch_worker.py'), read(parent / 'assistant_mesh/__init__.py')
    pins = dict(assignment(parent_launcher, '_pins'))
    pins[str(parent / 'launch_worker.py')], pins[str(parent / 'assistant_mesh/__init__.py')] = sha(parent_launcher), sha(parent_init)
    for relative, digest in assignment(parent_launcher, '_new_sources').items():
        path = str(parent / relative)
        if path in pins and pins[path] != digest: raise ValueError('phone_role_pin_conflict')
        pins[path] = digest
    for filename, digest in pins.items():
        if sha(read(filename)) != digest: raise ValueError('phone_role_actual_incumbent_pin_changed')
    config_dir, workspace = destination / 'candidate-config', destination / 'workspace'
    role = {'format': 'amita-phone-maintenance-role/1', 'scope': scope, 'workspace': str(workspace),
        'profile_path': str(config_dir / 'phone-maintenance-profile.json'),
        'governance_database': str(governance_database), 'governance_policy': str(governance_policy)}
    outputs = {'assistant_mesh/__init__.py':
        ("__path__.append(" + repr(str(parent / 'assistant_mesh')) + ")\n").encode() + parent_init,
        'assistant_mesh/phone_maintenance_runtime/__init__.py': b'"""Scoped existing governance algorithms; no startup on import."""\n'}
    for module in MODERN:
        outputs['assistant_mesh/phone_maintenance_runtime/' + module + '.py'] = subprocess.check_output(
            ['git', '-C', str(source_root), 'show', commit + ':assistant_mesh/' + module + '.py'])
    outputs['assistant_mesh/phone_maintenance_role.py'] = subprocess.check_output(
        ['git', '-C', str(source_root), 'show', commit + ':assistant_mesh/phone_maintenance_role.py'])
    stubs = {'config': 'from ..config import private_json\n',
        'reasoning': 'from ..native_worker_runtime.reasoning import *\n',
        'codex_provider': 'from ..native_worker_runtime.codex_provider import *\n',
        'model_provider_policy': 'from ..native_worker_runtime.model_provider_policy import *\n',
        'provider_runtime': 'from ..provider_runtime import _owner_bytes, _unique_object\n'}
    for module, text in stubs.items(): outputs['assistant_mesh/phone_maintenance_runtime/' + module + '.py'] = text.encode()
    new_hashes = {n: sha(data) for n, data in outputs.items()}
    launcher = ("import hashlib, importlib.util, sys\nfrom pathlib import Path\n_root=Path(__file__).resolve().parent\n"
        "sys.path[0]=str(_root)\n_pins=" + repr(pins) + "\n_new_sources=" + repr(new_hashes) + "\n"
        "for _name,_expected in list(_pins.items())+[(str(_root/n),h) for n,h in _new_sources.items()]:\n"
        "    _path=Path(_name)\n"
        "    if _path.is_symlink() or not _path.is_file() or hashlib.sha256(_path.read_bytes()).hexdigest()!=_expected:\n"
        "        raise RuntimeError('phone_role_runtime_pin_changed')\nimport assistant_mesh\n"
        "_spec=importlib.util.spec_from_file_location('_phone_role_incumbent'," + repr(str(parent / 'launch_worker.py')) + ")\n"
        "_parent=importlib.util.module_from_spec(_spec)\nsys.modules[_spec.name]=_parent\n_spec.loader.exec_module(_parent)\n"
        "from assistant_mesh.config import private_json\n"
        "from assistant_mesh.phone_maintenance_role import install\n"
        "from assistant_mesh.phone_maintenance_runtime.leader_governance import Governance\n"
        "from assistant_mesh.phone_maintenance_runtime.governance_worker import make_worker\n"
        "_role=private_json(" + repr(str(config_dir / 'role.json')) + ")\n"
        "_policy=private_json(" + repr(str(native_policy)) + ")\n"
        "if any(r['scope']==_role['scope'] for r in _policy['additional_profiles']):\n"
        "    raise RuntimeError('phone_role_legacy_profile_conflict')\n"
        "_parent.install(_parent._incumbent,_policy,_parent.Codex,_parent.WorkerStrategy,_parent.sessions)\n"
        "install(_parent._incumbent,_parent.Codex,_role,private_json,Governance,make_worker)\n"
        "if __name__=='__main__':\n    _parent._incumbent.main()\n").encode()
    outputs['launch_worker.py'] = launcher
    for filename, data in outputs.items(): ast.parse(data.decode(), filename=filename, feature_version=(3, 6))
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    for filename, data in outputs.items(): write(destination / 'candidate-release' / filename, data)
    candidate_worker = dict(worker, capabilities=list(worker['capabilities']) + ['phone-maintenance'])
    write(config_dir / 'laptop-worker.json', encoded(candidate_worker))
    write(config_dir / 'role.json', encoded(role))
    write(config_dir / 'phone-maintenance-profile.json', encoded({'model': 'gpt-6-luna', 'reasoning_effort': 'max', 'revision': 1}))
    write(config_dir / 'modern-worker-policy.unchanged.json', native_raw)
    write(config_dir / 'governance-policy.unchanged.json', policy_raw)
    write(workspace / 'AGENTS.md', read(Path(source_root) / 'deploy/PHONE-MAINTENANCE-ROLE-AGENTS.md'))
    write(destination / 'CHECKLIST.md', read(Path(source_root) / 'deploy/PHONE-MAINTENANCE-ROLE-CHECKLIST.md'))
    spec = json.loads(read(previous_spec).decode())
    if spec['component'] != 'laptop-worker' or spec['kind'] != 'worker': raise ValueError('phone_role_original_worker_spec_required')
    spec.update(version='20261109.phone-maintenance.1', argv=[spec['argv'][0], str(Path(installed_target) / 'launch_worker.py'),
                                                          '--config', str(config_dir / 'laptop-worker.json')])
    write(config_dir / 'stage-spec.json', encoded(spec))
    report = {'format': 'amita-phone-maintenance-candidate/1', 'source_commit': commit, 'scope': scope,
        'installed_target': str(installed_target), 'identity': {'node_id': 'laptop', 'hardware_created': False},
        'runtime_pins': pins, 'source_sha256': {n: sha(data) for n, data in outputs.items()},
        'owner_input_sha256': {str(worker_config): sha(worker_raw), str(native_policy): sha(native_raw),
                              str(governance_policy): sha(policy_raw)},
        'source_authorization_required': {'config': '/home/ai_agent/.local/state/personal-assistant-mesh/server.json',
            'peer_selector': {'role': 'worker', 'node': 'laptop'}, 'append_capability': 'phone-maintenance'},
        'local_worker_config_candidate': str(config_dir / 'laptop-worker.json'),
        'stage_spec': str(config_dir / 'stage-spec.json'), 'model_called': False,
        'production_installed': False, 'scope_only': True, 'old_scopes_changed': False,
        'candidate_ready_for_root_review': True, 'activation_requires_actual_idle': True,
        'model_catalog_support_only': True, 'inference_verified': False, 'ledger_opened': False}
    write(destination / 'candidate-release/phone-maintenance-worker.json', encoded(report))
    return {'destination': str(destination), 'stage_spec': str(config_dir / 'stage-spec.json'),
            'release_files': len(outputs) + 1, 'model_called': False, 'production_installed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('destination', 'parent-release', 'installed-target', 'first-start', 'worker-config', 'native-policy',
                 'governance-policy', 'governance-database', 'models-cache', 'previous-spec'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--source-root', default=str(ROOT))
    print(json.dumps(build(**vars(parser.parse_args())), sort_keys=True))


if __name__ == '__main__': main()
