"""New source-only candidate: exact frozen Store hook + fixed reply libraries.

Never install, open a ledger, read config/credentials, start a service or replace
Worker. Keep Root's current entrance/Claw/passive constructor/launch .py bytes.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys


FORMAT = 'mesh-conversation-final-source-candidate/1'
CORE_SHA256 = {
    'store.py': '081633994459ffd2fb253e5514ea398d011b0eb8adcf059b251bbde10a705f08',
    'server.py': 'cc5bb8b492d7e03f68a5f8720eb1399d9ab4ac8e303f46919713c0a6592fd19c',
    'worker.py': '816a8eb2f6bc5a24a22ae345b1b7b94b0dd5d0ce52f99a6658688e683ce9cbf0',
    'codex.py': 'b32e2ba28c2c06ce1b1208a023de01ddd56081c012d9b5723e1158449bff1f98',
    'sessions.py': '37f5b647c5ddb11018907311bef1e60e512b0d5259d78ca188b3f93e3ecb33ec',
    'channel.py': 'ed95ff4cfb33f385edc3f319085532adf543f581cdf8303864f6ac1bfc8c5261',
}
FINAL_STORE_SHA256 = 'be2df48e03021f48c382efc4690fa7a8ee5e67da8bd93497e725566532e30c0b'
PATCH_SHA256 = '1c9285211430e15b3c753082ae54db35cf1cd220fc884cebe17712a0fd6dd3ec'
SUPPORT_SHA256 = {
    'conversation_event_feed.py': '61ea42be1543137bb6afbe3ca40d8ab32bf354f72938c1309f5b6107ef03132b',
    'conversation_final.py': 'cf725c549d353f0c496c1c2d945efb1463becc8624c7b85e8654b9995981aae0',
    'conversation_emit.py': '62cdbd46cec437e69f365f298852e1b05b036a15291356235952e0900ac66547',
    'conversation.py': 'ba39fd3f96f11743aab24eb2c992810fdaa4cbe629c18b17f286862d0949644d',
    'conversation_runtime.py': 'f479802b5bfe50c48b765ca131f720065f2cb6b4a18a6f95eac0080eb238927c',
    'conversation_authority.py': '9dbb791a432ddecd06222ffd9b6d2977dfb46f547683ae4a5b1800be482b8deb',
    'conversation_sync.py': '55c51dcb64bdb1fd252287b96da55fc9c63877cc24aec10cbe16f30da4c690ca',
    'ingress.py': '7f5f52199e2ddf3634229389c75faca58b1c11e21b8766327844b7a2e3bf6938',
    'ingress_backend.py': '56238409d7626eaa0fbddbe261959e8ff06215b29e7b26c9fc764dc2265b4c41',
    'mcp_http_auth.py': 'b90835502cb9e0a8216500241aeb2e74d4ff21ec669ebf0ad19b2e8f413d4ad7',
}
BOUNDARIES = frozenset(('cloud_jobs', 'config', 'entrance_runtime', 'native_messaging', 'store', 'worker'))
PRESERVED = frozenset(('entrance.py', 'entrance_runtime.py', 'entrance_failure.py',
    'entrance_media.py', 'claw_cli.py', 'claw_upload.py', 'legacy_channel_owner.py', 'config.py'))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _bytes(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('conversation_candidate_regular_source_required')
    return path.read_bytes()


def _source(root):
    root = Path(root).absolute()
    package = root / 'assistant_mesh'
    if root.is_symlink() or package.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise ValueError('conversation_candidate_source_directory_required')
    files = {'assistant_mesh/' + path.name: _bytes(path) for path in sorted(package.glob('*.py'))}
    files.update({path.name: _bytes(path) for path in sorted(root.glob('*.py'))})
    if not PRESERVED <= {Path(name).name for name in files}:
        raise ValueError('conversation_candidate_existing_entrance_required')
    for name, expected in CORE_SHA256.items():
        if _sha(files.get('assistant_mesh/' + name, b'')) != expected:
            raise ValueError('conversation_candidate_frozen_core_changed')
    return files


def _manifest(files):
    return _sha(''.join(_sha(data) + '  ' + name + '\n' for name, data in sorted(files.items())).encode('utf8'))


def inspect_base(root):
    files = _source(root)
    return {'format': FORMAT, 'base_manifest_sha256': _manifest(files),
            'source_sha256': {name: _sha(data) for name, data in sorted(files.items())}}


def _parse(data):
    tree = ast.parse(data.decode('utf8'), feature_version=(3, 6)) if sys.version_info >= (3, 8) else ast.parse(data.decode('utf8'))
    compile(tree, '<conversation-final-candidate-source>', 'exec')
    return tree


def _closure(overlay):
    pending, reached, boundaries, graph = ['conversation_final', 'conversation_emit', 'conversation_sync', 'conversation_event_feed'], set(), set(), {}
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        dependencies = set()
        for node in ast.walk(_parse(overlay[name + '.py'])):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.level != 1:
                    raise ValueError('conversation_candidate_relative_import_changed')
                dependencies.update([node.module.split('.')[0]] if node.module else
                                    [item.name.split('.')[0] for item in node.names])
        graph[name] = sorted(dependencies)
        for dependency in dependencies:
            if dependency in BOUNDARIES:
                boundaries.add(dependency)
            elif dependency + '.py' in overlay:
                pending.append(dependency)
            else:
                raise ValueError('conversation_candidate_import_closure_changed')
    if reached != {Path(name).stem for name in SUPPORT_SHA256} or boundaries != BOUNDARIES:
        raise ValueError('conversation_candidate_import_closure_changed')
    return graph


def build(base_root, destination, expected_base_manifest_sha256, support_root=None):
    original = _source(base_root)
    if (not isinstance(expected_base_manifest_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_base_manifest_sha256)
            or _manifest(original) != expected_base_manifest_sha256):
        raise ValueError('conversation_candidate_base_manifest_changed')
    support = Path(support_root or Path(__file__).resolve().parents[1])
    overlay = {name: _bytes(support / 'assistant_mesh' / name) for name in SUPPORT_SHA256}
    if any(_sha(data) != SUPPORT_SHA256[name] for name, data in overlay.items()):
        raise ValueError('conversation_candidate_support_pin_changed')
    graph = _closure(overlay)
    patch = _bytes(support / 'deploy/legacy-store-conversation-final.patch')
    if _sha(patch) != PATCH_SHA256:
        raise ValueError('conversation_candidate_patch_changed')
    spec = importlib.util.spec_from_file_location('_conversation_exact_patch',
        str(Path(__file__).resolve().parent / 'build-legacy-fenced-receiver.py'))
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    files = dict(original)
    files.update({'assistant_mesh/' + name: data for name, data in overlay.items()})
    files['assistant_mesh/store.py'] = legacy.apply_exact_patch(original['assistant_mesh/store.py'], patch, 'store.py')
    if _sha(files['assistant_mesh/store.py']) != FINAL_STORE_SHA256:
        raise ValueError('conversation_candidate_final_store_pin_changed')
    # One explicit new Store pin, never accepting a general modern Store.
    fence = original['assistant_mesh/legacy_fenced_receiver.py']
    before = ("STORE_SHA256 = '" + CORE_SHA256['store.py'] + "'").encode('ascii')
    after = ("STORE_SHA256 = '" + FINAL_STORE_SHA256 + "'").encode('ascii')
    if fence.count(before) != 1:
        raise ValueError('conversation_candidate_original_fence_pin_changed')
    files['assistant_mesh/legacy_fenced_receiver.py'] = fence.replace(before, after)
    for data in files.values():
        _parse(data)
    target, base = Path(destination), Path(base_root).absolute()
    if (not target.is_absolute() or target.exists() or target.is_symlink() or base in target.parents
            or any(parent.is_symlink() for parent in target.parents)):
        raise ValueError('conversation_candidate_destination_must_be_new')
    target.mkdir(mode=0o700)
    (target / 'assistant_mesh').mkdir(mode=0o700)
    for name, data in sorted(files.items()):
        with (target / name).open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
    changed = sorted(name for name in original if original[name] != files[name])
    report = {'format': FORMAT, 'base_manifest_sha256': _manifest(original),
        'candidate_manifest_sha256': _manifest(files), 'changed_original_files': changed,
        'overlay_modules': sorted(overlay), 'import_graph': graph, 'preserved_boundaries': sorted(BOUNDARIES),
        'source_sha256': {name: _sha(data) for name, data in sorted(files.items())},
        'production_installed': False, 'ledger_opened': False, 'worker_replaced': False,
        'grant_authorized': False, 'model_requested': False, 'old_tasks_backfilled': False}
    with (target / 'conversation-final-candidate.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, sort_keys=True)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description='Build frozen final-reply source candidate only, never deploy')
    parser.add_argument('--base-root', required=True)
    parser.add_argument('--destination')
    parser.add_argument('--base-manifest-sha256')
    parser.add_argument('--support-root')
    args = parser.parse_args()
    if args.destination is None:
        print(json.dumps(inspect_base(args.base_root), sort_keys=True))
    else:
        report = build(args.base_root, args.destination, args.base_manifest_sha256, args.support_root)
        print(json.dumps({key: value for key, value in report.items() if key not in ('source_sha256', 'import_graph')}, sort_keys=True))


if __name__ == '__main__':
    main()
