"""Add the pinned Cloud pure-library closure to a NEW frozen source copy.

Input is Root's read-only copy of the CURRENT sole receiver, not modern HEAD.
No original core, entrance/Claw module, config, ledger or service is replaced.
This builder only reads Python source and creates an isolated source candidate.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


FORMAT = 'mesh-cloud-owner-source-candidate/1'
CORE_SHA256 = {
    'store.py': '081633994459ffd2fb253e5514ea398d011b0eb8adcf059b251bbde10a705f08',
    'server.py': 'cc5bb8b492d7e03f68a5f8720eb1399d9ab4ac8e303f46919713c0a6592fd19c',
    'worker.py': '816a8eb2f6bc5a24a22ae345b1b7b94b0dd5d0ce52f99a6658688e683ce9cbf0',
    'codex.py': 'b32e2ba28c2c06ce1b1208a023de01ddd56081c012d9b5723e1158449bff1f98',
    'sessions.py': '37f5b647c5ddb11018907311bef1e60e512b0d5259d78ca188b3f93e3ecb33ec',
    'channel.py': 'ed95ff4cfb33f385edc3f319085532adf543f581cdf8303864f6ac1bfc8c5261',
}
PRESERVED_FEATURES = frozenset((
    '__init__.py', '__main__.py', 'config.py', 'entrance.py', 'entrance_runtime.py',
    'entrance_failure.py', 'entrance_media.py', 'claw_cli.py', 'claw_upload.py',
    'legacy_fenced_receiver.py', 'legacy_channel_owner.py'))
# Fixed c46ef1e/b348010 pure-library closure, not a recursive modern repo copy.
SUPPORT_SHA256 = {
    'cloud_owner_runtime.py': '23038f279a1de9e62cc6c3011968c42a23f85b33c1ae202d5131eb81fd38d1ff',
    'cloud_adoption.py': '9bc85aefb22bf531a405b30c6ddec2a5d22a23aa72186364eba2598757d2c8fb',
    'cloud_http.py': 'a85de1e694481d9f95d295e9340e60a7f0a93e290a13277dbccf31505c974b41',
    'cloud_jobs.py': '245d829176c5177c50466205fb680f43a2403fc45c1707c89e96888e1b4bcf9a',
    'cloud_membership.py': '8b795774cb080b795ae8e551400a2af718166c6c7dab73db094ec3ffdc9cc5a6',
    'cloud_ui.py': 'd89e8e30c5e742dbbf29edf82a6f08a4d4dd77d8b74aa7e7ddf88ee08b300aaf',
    'conversation.py': '8814f090f7b32227f65d9b8df7d08b2a3dd9be1d9c4df9eac130b3ec996bb716',
    'conversation_authority.py': 'de203526488557842da7ee921017866054f39e47af2b83f169704b4ed7cab004',
    'conversation_runtime.py': '76291b0a68cee69265882e7c4c623511acaba0232902f6bb6493c994a62330c7',
    'enrollment.py': '56759340cf05f3f1b68d995eb2d4c5b1004f68a62d580937cc54b272136c3a0b',
    'ingress.py': 'a19b620023964d373b9a3cedff63983981ba835263905249e66667ce6232f53b',
    'ingress_results.py': '1215920e643d18467410564fddd29b6a0356b3484b49e97ca1931817a4f7b498',
    'mcp_http.py': '6f8ed6907a291adfe64aa32cc2eeaf1619c0e2d26e9be6bb0dd349b0b483000b',
    'mcp_http_auth.py': 'b90835502cb9e0a8216500241aeb2e74d4ff21ec669ebf0ad19b2e8f413d4ad7',
    'native_messaging.py': '34dcf15b3194e488193bfc0e8e78e86f93b4064fde61785424d0e93418da5fba',
}
BOUNDARIES = frozenset(('config', 'store', 'worker', 'entrance_runtime'))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _regular_bytes(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('cloud_candidate_source_not_regular')
    return path.read_bytes()


def _source(root):
    root = Path(root)
    package = root / 'assistant_mesh'
    if (root.is_symlink() or package.is_symlink() or not package.is_dir()
            or any(parent.is_symlink() for parent in root.absolute().parents)):
        raise ValueError('cloud_candidate_source_directory_required')
    files = {'assistant_mesh/' + path.name: _regular_bytes(path)
             for path in sorted(package.glob('*.py'))}
    # Keep current private Python launch hooks byte-for-byte, not state/config.
    files.update({path.name: _regular_bytes(path) for path in sorted(root.glob('*.py'))})
    if not PRESERVED_FEATURES <= {Path(name).name for name in files if name.startswith('assistant_mesh/')}:
        raise ValueError('cloud_candidate_existing_entrance_required')
    for name, expected in CORE_SHA256.items():
        if _sha(files.get('assistant_mesh/' + name, b'')) != expected:
            raise ValueError('cloud_candidate_frozen_core_changed')
    return files


def _manifest(files):
    return _sha(''.join(_sha(data) + '  ' + name + '\n'
                        for name, data in sorted(files.items())).encode('utf8'))


def inspect_base(base_root):
    files = _source(base_root)
    return {'format': FORMAT, 'base_manifest_sha256': _manifest(files),
            'source_sha256': {name: _sha(data) for name, data in sorted(files.items())},
            'source_only': True, 'production_installed': False, 'ledger_opened': False}


def _parse(data):
    text = data.decode('utf8')
    # Native 3.6 parses its own grammar. Modern builders also reject syntax
    # unavailable on the actual legacy host; this is not host execution proof.
    if sys.version_info >= (3, 8):
        tree = ast.parse(text, feature_version=(3, 6))
    else:
        tree = ast.parse(text)
    compile(tree, '<cloud-candidate-source>', 'exec')
    return tree


def _closure(files):
    graph, boundaries, reached, pending = {}, set(), set(), ['cloud_owner_runtime']
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        dependencies = set()
        for node in ast.walk(_parse(files[name + '.py'])):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.level != 1:
                    raise ValueError('cloud_candidate_relative_import_changed')
                dependencies.update([node.module.split('.')[0]] if node.module else
                                    [item.name.split('.')[0] for item in node.names])
        graph[name] = sorted(dependencies)
        for dependency in dependencies:
            if dependency in BOUNDARIES:
                boundaries.add(dependency)
            elif dependency + '.py' in files:
                pending.append(dependency)
            else:
                raise ValueError('cloud_candidate_pure_library_closure_changed')
    if reached != {Path(name).stem for name in SUPPORT_SHA256} or boundaries != BOUNDARIES:
        raise ValueError('cloud_candidate_pure_library_closure_changed')
    return graph


def build(base_root, destination, expected_base_manifest_sha256, support_root=None):
    if (not isinstance(expected_base_manifest_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_base_manifest_sha256)):
        raise ValueError('cloud_candidate_confirmed_base_manifest_required')
    original = _source(base_root)
    if _manifest(original) != expected_base_manifest_sha256:
        raise ValueError('cloud_candidate_base_manifest_changed')
    support = Path(support_root or Path(__file__).resolve().parents[1])
    overlay = {}
    for name, expected in SUPPORT_SHA256.items():
        data = _regular_bytes(support / 'assistant_mesh' / name)
        if _sha(data) != expected:
            raise ValueError('cloud_candidate_support_pin_changed')
        overlay[name] = data
    graph = _closure(overlay)
    files = dict(original)
    files.update({'assistant_mesh/' + name: data for name, data in overlay.items()})
    preserved = set(original) - {'assistant_mesh/' + name for name in overlay}
    for name in preserved:
        if files[name] != original[name]:
            raise ValueError('cloud_candidate_original_source_changed')
    for data in files.values():
        _parse(data)
    target = Path(destination)
    base = Path(base_root).absolute()
    if (not target.is_absolute() or target.exists() or target.is_symlink()
            or base in target.parents or any(parent.is_symlink() for parent in target.parents)):
        raise ValueError('cloud_candidate_destination_must_be_new')
    target.mkdir(mode=0o700)
    (target / 'assistant_mesh').mkdir(mode=0o700)
    for name, data in sorted(files.items()):
        with (target / name).open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
    report = {'format': FORMAT, 'base_manifest_sha256': expected_base_manifest_sha256,
        'base_source_sha256': {name: _sha(data) for name, data in sorted(original.items())},
        'source_sha256': {name: _sha(data) for name, data in sorted(files.items())},
        'candidate_manifest_sha256': _manifest(files),
        'overlay_modules': sorted(overlay), 'relative_imports': graph,
        'preserved_import_boundaries': sorted(BOUNDARIES),
        'unchanged_original_sources': sorted(name for name in original if files[name] == original[name]),
        'changed_original_sources': sorted(name for name in original if files[name] != original[name]),
        'added_sources': sorted(set(files) - set(original)),
        'protected_core_sha256': dict(CORE_SHA256),
        'production_installed': False, 'ledger_opened': False, 'carrier_started': False,
        'grant_authorized': False, 'model_called': False, 'cloud_job_submitted': False}
    with (target / 'cloud-owner-candidate.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, sort_keys=True)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description='Prepare NEW Cloud frozen source only, never install')
    parser.add_argument('--base-root', required=True)
    parser.add_argument('--inspect', action='store_true')
    parser.add_argument('--destination')
    parser.add_argument('--expected-base-manifest-sha256')
    parser.add_argument('--support-root')
    args = parser.parse_args()
    if args.inspect:
        if args.destination or args.expected_base_manifest_sha256 or args.support_root:
            parser.error('--inspect only reads --base-root')
        report = inspect_base(args.base_root)
    else:
        if not args.destination or not args.expected_base_manifest_sha256:
            parser.error('build requires destination and owner-confirmed base manifest SHA256')
        report = build(args.base_root, args.destination, args.expected_base_manifest_sha256, args.support_root)
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ('base_source_sha256', 'source_sha256', 'relative_imports')}, sort_keys=True))


if __name__ == '__main__':
    main()
