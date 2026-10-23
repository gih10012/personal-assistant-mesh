"""Source-only scoped Goal candidate; never install, import runtime or open DB.

Reuse pinned candidate algorithms in a separate pure namespace. Do not copy the
modern Store/Worker into incumbent module names. The only incumbent behavior
change is an exact Worker claimed-task seam; its existing pin references are
updated explicitly. Root supplies the exact full base manifest.
"""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FORMAT = 'mesh-native-goal-source-candidate/1'
PINS = {
    'codex.py': '0dcf663c7014932586737e46a44915067a3000aa17bf844ee01c1d21586781cd',
    'worker.py': 'ad7fc4cc5aa59f51d4aadd293cd31c34a3b8d7424a8d588a0b56f555f1712faf',
    'store.py': 'b59b5ca35ce1941dfff749173c0100c89277db87364cf1710492b9fd8bec7b4d',
    'sessions.py': '61382191eff2f3979b817db0e07b9fc143c2544632362d3de23166304a3e5304',
    'native_goal_observer.py': '0dec24469800087382dd58c550535302e6a4c43ec7c36db64ee6bee450d796c1',
    'native_history.py': '5e06abac895a169a7738b52f8818b2cec64bf3d7e112d92f285a1bf076b7dd6c',
    'native_ws.py': '4d2bc715cc2676dd4b7793550872f300a7f3dbe1ee789b7d140a96eb5ad9246d',
    'codex_provider.py': '3b00058a29fb24dbbafb19f3195df134f61106bb3619a32fce756072554d6e7b',
    'model_provider_policy.py': '8c64c465cb227eb800100600480a824b22f14d13cd92eb7b4613f59ae5ca961b',
    'reasoning.py': '720d82d51ab6093149bf86f66e5e271c3ebad3f08290280559dc8e497d888084',
}
WORKER_PIN = '816a8eb2f6bc5a24a22ae345b1b7b94b0dd5d0ce52f99a6658688e683ce9cbf0'
STORE_PIN = 'be2df48e03021f48c382efc4690fa7a8ee5e67da8bd93497e725566532e30c0b'
SCOPE_SHA256 = '9f93cd76fdd8a426e6f4b277f507a97c74a86c0b9a194d311ad511efe5863130'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('native_goal_candidate_exact_source_changed')
    return text.replace(before, after, 1)


def _class(text, name):
    return next(node for node in ast.parse(text).body if isinstance(node, ast.ClassDef) and node.name == name)


def _function(text, cls, name):
    return copy.deepcopy(next(node for node in _class(text, cls).body
                              if isinstance(node, ast.FunctionDef) and node.name == name))


def _module(imports, name, methods, constants=()):
    text = imports + '\n\n' + '\n\n'.join(constants) + '\n\nclass ' + name + ':\n'
    for node in methods:
        node = ast.fix_missing_locations(node)
        text += '\n' + '\n'.join('    ' + line if line else '' for line in ast.unparse(node).splitlines()) + '\n'
    return text


def scoped_worker_source(text):
    prefix = text[:text.index('\nclass NoRedirect')]
    constants = prefix[prefix.index('_RESTORE_FAILURES'):]
    # Only standard-library imports and the selected native/save implementation.
    imports = ('import copy, hashlib, json, os, time, urllib.error\nfrom pathlib import Path\n'
               'from .codex import CodexError\nfrom . import sessions\n')
    methods = [_function(text, 'Worker', name) for name in (
        'on_activity', 'tick', '_native_yield_tick', '_checked_native_yield_seal')]
    run = _function(text, 'Worker', 'run_once')
    cut = next(i for i, item in enumerate(run.body) if isinstance(item, ast.Assign)
               and any(isinstance(target, ast.Attribute) and target.attr == 'current' for target in item.targets))
    run.name, run.args.args = 'run_claimed', [ast.arg(arg='self'), ast.arg(arg='task')]
    run.body = run.body[cut:]
    rendered = ast.unparse(ast.fix_missing_locations(run))
    # Cross-node/history import is a separate project, never implicit Goal resume.
    rendered = replace_once(rendered,
        'imported = sessions.restore(self.client, task, folder, self.tick, self.harness)',
        "raise CodexError('native_session_migration_unavailable')")
    # Scoped plain turns also need real closure; not every task creates a Goal.
    rendered = replace_once(rendered,
        "if isinstance(lifecycle, dict) and lifecycle.get('goal_managed'):",
        "if isinstance(lifecycle, dict) and lifecycle.get('controller_enabled') is True:")
    rendered = replace_once(rendered, 'seal()', 'seal(tick=self._native_yield_tick)')
    # Both terminal and yielded native history remain guarded until their ACK.
    before = """if yielded_seal is None:
                sessions.save(self.client, task, self.config.get('node_id'), self.harness, state, rollout, self.tick)"""
    after = """if yielded_seal is None:
                guarded = dict(state, side_effect_started=True)
                saved = sessions.save(self.client, task, self.config.get('node_id'), self.harness, guarded, rollout, self._native_yield_tick,
                                      expected_fingerprint=agent.native_lifecycle['history_fingerprint'])
                if not isinstance(saved, dict) or saved.get('ok') is not True or saved.get('saved') is not True or saved.get('artifact_saved') is not True or saved.get('scope') != task.get('scope'):
                    raise CodexError('codex_native_yield_commit_unconfirmed')"""
    rendered = replace_once(rendered, before, after)
    rendered = replace_once(rendered,
        "if yielded_seal is not None and (not isinstance(finalized, dict) or finalized.get('ok') is not True):",
        "if not isinstance(finalized, dict) or finalized.get('ok') is not True:")
    # ast.unparse adds grouping predictably; parse validates the exact edit below.
    methods.append(ast.parse(rendered).body[0])
    return _module(imports, 'WorkerStrategy', methods, (constants,))


def authority_source(text):
    names = ('_yielded_active_goal_checkpoint', '_yielded_goal_reserves_scope',
             'claim', 'update_task', 'agent_action')
    methods = [_function(text, 'Store', name) for name in names]
    for index, method in enumerate(methods):
        if method.name == 'claim':
            loop = next(node for node in ast.walk(method) if isinstance(node, ast.For)
                        and isinstance(node.target, ast.Name) and node.target.id == 'row')
            loop.body[0:0] = ast.parse('if not self._native_goal_row_allowed(db, row, node):\n    continue').body
            for position, node in enumerate(loop.body):
                if ast.unparse(node).startswith("result['checkpoint'] = json.loads"):
                    loop.body[position + 1:position + 1] = ast.parse(
                        "binding = self._native_goal_binding(db, row)\nif binding is not None:\n    result['native_goal_profile'] = binding").body
                    break
            else:
                raise ValueError('native_goal_candidate_claim_shape_changed')
        elif method.name == 'update_task':
            block = next(node for node in ast.walk(method) if isinstance(node, ast.With)).body
            lifecycle = next(i for i, node in enumerate(block) if isinstance(node, ast.Assign)
                             and any(isinstance(target, ast.Name) and target.id == 'lifecycle' for target in node.targets))
            block[lifecycle:lifecycle] = ast.parse('self._native_goal_validate_checkpoint(db, row, next_checkpoint, status)').body
            update = next(i for i, node in enumerate(block) if isinstance(node, ast.Expr)
                          and ast.unparse(node).startswith("db.execute('UPDATE tasks SET checkpoint=?,deadline=?,result=?,status=? WHERE id=?',"))
            block[update:update] = ast.parse("hook = getattr(self, '_conversation_final_in_db', None)\nif hook is not None:\n    hook(db, row, node, epoch, checkpoint, result, status)").body
        methods[index] = ast.fix_missing_locations(method)
    return _module('import hashlib, json\nfrom ..store import Conflict\n', 'AuthorityMethods', methods)


def backend_source(text):
    # The existing full transport/controller is isolated; no incumbent Codex is
    # replaced. Small additions cover all natural completion statuses, accounting
    # continuity, plain-turn closure and immutable upload fingerprint.
    text = replace_once(text, "        self._validate_goal_snapshot(goal)\n        expected =", """        self._validate_goal_snapshot(goal)
        if goal is not None:
            from .native_goal_observer import validate_goal_continuity
            baseline = self.native_lifecycle.get('goal_baseline')
            previous = self.native_lifecycle.get('goal')
            validate_goal_continuity(previous or baseline or goal, goal, self.thread_id)
            if baseline is None:
                self.native_lifecycle['goal_baseline'] = copy.deepcopy(goal)
        expected =""")
    text = replace_once(text,
        "                if status != 'interrupted' or not self.native_lifecycle.get('goal_managed'):\n                    raise CodexError('codex_turn_failed')",
        "                raise CodexError('codex_turn_failed')")
    text = replace_once(text,
        "                return '\\n\\n'.join(replies) or '任务已结束，但没有生成文字回答。'",
        "                if lifecycle.get('controller_enabled') and not self._goal_quiescence_barrier(replies):\n                    continue\n                return '\\n\\n'.join(replies) or '任务已结束，但没有生成文字回答。'")
    text = replace_once(text, "        if not lifecycle.get('goal_managed') or not lifecycle.get('quiescent'):",
                        "        if not lifecycle.get('controller_enabled') or not lifecycle.get('quiescent'):")
    text = replace_once(text, '    def seal_native_lifecycle(self):',
                        '    def seal_native_lifecycle(self, tick=None):')
    text = replace_once(text, "                lifecycle['goal'] = copy.deepcopy(goal)\n            elif method == 'thread/goal/cleared':",
                        "                self._store_goal_snapshot(goal)\n            elif method == 'thread/goal/cleared':")
    text = replace_once(text, "        lifecycle['settled'] = False\n        self.close()", """        if (not self._native_started_turns or set(self._native_completion_statuses) != self._native_started_turns
                or any(status != 'completed' for status in self._native_completion_statuses.values())):
            raise CodexError('codex_native_drain_turn_unsettled')
        lifecycle['settled'] = False
        self.close()""")
    text = replace_once(text, "        lifecycle['settled'] = True\n        return copy.deepcopy(lifecycle)", """        from .native_history import capture_prefix, verify_history
        prefix = capture_prefix(self.rollout_path, self.thread_id, tick=tick)
        frozen = verify_history(self.rollout_path, self.thread_id, prefix, self._native_started_turns, tick=tick)
        lifecycle['history_fingerprint'] = frozen['fingerprint']
        lifecycle['completion_statuses'] = copy.deepcopy(self._native_completion_statuses)
        lifecycle['settled'] = True
        return copy.deepcopy(lifecycle)""")
    return text


def import_closure(pure):
    graph, boundaries = {}, set()
    for filename, source in pure.items():
        name = Path(filename).stem
        if name == '__init__':
            continue
        imports = set()
        for node in ast.walk(ast.parse(source, feature_version=(3, 6))):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.level == 1:
                    imports.update([node.module.split('.')[0]] if node.module else
                                   [item.name.split('.')[0] for item in node.names])
                elif (node.level == 2 and ((name == 'config' and node.module == 'config')
                                          or (name == 'authority' and node.module == 'store'))):
                    boundaries.add(node.module)
                else:
                    raise ValueError('native_goal_candidate_relative_import_changed')
            elif isinstance(node, ast.Import):
                if any(item.name.split('.')[0] not in __import__('sys').stdlib_module_names for item in node.names):
                    raise ValueError('native_goal_candidate_nonstdlib_import')
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split('.')[0] not in __import__('sys').stdlib_module_names:
                    raise ValueError('native_goal_candidate_nonstdlib_import')
        if any(item + '.py' not in pure for item in imports):
            raise ValueError('native_goal_candidate_import_closure_changed')
        graph[name] = sorted(imports)
    pending, reached = ['codex', 'worker_strategy', 'authority', 'native_goal_observer'], set()
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name); pending.extend(graph[name])
    if reached != set(graph) or boundaries != {'config', 'store'}:
        raise ValueError('native_goal_candidate_import_closure_changed')
    return graph


def _load_final_builder(root):
    spec = importlib.util.spec_from_file_location('_native_goal_frozen_base', str(root / 'deploy/build-conversation-final-candidate.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def _base_source(base_root, root):
    base_builder = _load_final_builder(root)
    # Its input Store pin predates final; this builder requires the exact final
    # output Store instead, while retaining every other original core pin.
    old_store = base_builder.CORE_SHA256['store.py']
    base_builder.CORE_SHA256['store.py'] = STORE_PIN
    try:
        original = base_builder._source(base_root)
    finally:
        base_builder.CORE_SHA256['store.py'] = old_store
    return base_builder, original


def inspect_base(base_root, support_root=ROOT):
    """Read only .py source and exact core pins, never runtime configuration."""
    base_builder, original = _base_source(base_root, Path(support_root))
    return {'format': FORMAT, 'base_manifest_sha256': base_builder._manifest(original),
            'source_sha256': {name: sha(data) for name, data in sorted(original.items())}}


def build(base_root, destination, expected_base_manifest_sha256, support_root=ROOT):
    root = Path(support_root)
    base_builder, original = _base_source(base_root, root)
    if base_builder._manifest(original) != expected_base_manifest_sha256:
        raise ValueError('native_goal_candidate_base_manifest_changed')
    sources = {}
    for name, expected in PINS.items():
        data = base_builder._bytes(root / 'assistant_mesh' / name)
        if sha(data) != expected:
            raise ValueError('native_goal_candidate_algorithm_pin_changed')
        sources[name] = data.decode('utf8')
    scope_bytes = base_builder._bytes(root / 'assistant_mesh/native_goal_scoped.py')
    if sha(scope_bytes) != SCOPE_SHA256:
        raise ValueError('native_goal_candidate_scope_pin_changed')
    files = dict(original)
    worker = original['assistant_mesh/worker.py'].decode('utf8')
    worker = replace_once(worker, 'class Worker:\n', 'class Worker:\n    _native_goal_claim_seam_version = 1\n')
    worker = replace_once(worker, '        self.current = task\n        self.wait_children = False\n',
                          '        return self.run_claimed(task)\n\n    def run_claimed(self, task):\n        self.current = task\n        self.wait_children = False\n')
    files['assistant_mesh/worker.py'] = worker.encode('utf8')
    new_worker_pin = sha(files['assistant_mesh/worker.py'])
    for name in ('entrance_runtime.py', 'legacy_fenced_receiver.py'):
        key = 'assistant_mesh/' + name
        files[key] = replace_once(original[key].decode('utf8'), WORKER_PIN, new_worker_pin).encode('utf8')
    files['assistant_mesh/native_goal_scoped.py'] = scope_bytes
    pure = {
        '__init__.py': '"""Pinned optional native lifecycle components; no startup on import."""\n',
        'config.py': 'from ..config import discover_codex_auth, private_json\n',
        'codex.py': backend_source(sources['codex.py']),
        'worker_strategy.py': scoped_worker_source(sources['worker.py']),
        'authority.py': authority_source(sources['store.py']),
    }
    for name in ('native_goal_observer.py', 'native_history.py', 'native_ws.py',
                 'codex_provider.py', 'model_provider_policy.py', 'reasoning.py'):
        pure[name] = sources[name]
    tree = ast.parse(sources['sessions.py'])
    save_names = {'request', 'save', '_expected_fingerprint', '_fingerprint_metadata',
        '_open_fingerprint_source', '_check_fingerprint_identity', '_check_fingerprint_bytes',
        '_verify_fingerprint', '_save_fingerprinted'}
    nodes = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
             or isinstance(node, ast.FunctionDef) and node.name in save_names]
    pure['sessions.py'] = ast.unparse(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))) + '\n'
    for name, text in pure.items():
        ast.parse(text, feature_version=(3, 6))
        files['assistant_mesh/native_goal_runtime/' + name] = text.encode('utf8')
    graph = import_closure(pure)
    ast.parse(scope_bytes.decode('utf8'), feature_version=(3, 6))
    target = Path(destination)
    if (not target.is_absolute() or target.exists() or target.is_symlink()
            or any(parent.is_symlink() for parent in target.parents)):
        raise ValueError('native_goal_candidate_new_destination_required')
    target.mkdir(mode=0o700)
    for name, data in sorted(files.items()):
        path = target / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600); stream.write(data)
    report = {'format': FORMAT, 'base_manifest_sha256': expected_base_manifest_sha256,
        'candidate_manifest_sha256': base_builder._manifest(files),
        'algorithm_source_sha256': PINS, 'scope_source_sha256': sha(scope_bytes),
        'changed_original_files': sorted(name for name in original if original[name] != files[name]),
        'source_sha256': {name: sha(data) for name, data in sorted(files.items())},
        'optional_runtime_modules': sorted(pure), 'optional_import_graph': graph,
        'preserved_import_boundaries': ['config', 'store'], 'worker_pin': new_worker_pin,
        'old_scope_body_preserved': True, 'production_installed': False, 'ledger_opened': False,
        'model_requested': False, 'original_cases_replayed': False}
    with (target / 'native-goal-candidate.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600); json.dump(report, stream, sort_keys=True)
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-root', required=True)
    parser.add_argument('--destination')
    parser.add_argument('--base-manifest-sha256')
    parser.add_argument('--support-root', default=str(ROOT))
    arguments = parser.parse_args()
    if arguments.destination is None:
        result = inspect_base(arguments.base_root, arguments.support_root)
    else:
        if arguments.base_manifest_sha256 is None:
            parser.error('--base-manifest-sha256 is required when building')
        result = build(arguments.base_root, arguments.destination,
                       arguments.base_manifest_sha256, arguments.support_root)
    print(json.dumps({key: value for key, value in result.items() if key != 'source_sha256'}, sort_keys=True))
