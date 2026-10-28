"""Build a small scoped-profile launcher over the actual unchanged PC Worker.

Source-only generation: no config/credential/history reads or runtime imports.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PHONE_SCOPE = 'leader:project:amita-harmony-onboarding'
FORMAT = 'amita-task-scoped-worker-launcher/1'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, digest=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ValueError('scoped_worker_regular_source_required')
    raw = path.read_bytes()
    if digest is not None and sha(raw) != digest:
        raise ValueError('scoped_worker_incumbent_source_changed')
    return raw


def once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('scoped_worker_exact_launcher_seam_changed')
    return text.replace(before, after, 1)


def build(launcher, namespace, destination, *, launcher_sha256, namespace_sha256,
          installed_root, profile_path, support_root=ROOT):
    paths = [Path(p) for p in (launcher, namespace, destination, installed_root, profile_path)]
    if any(not path.is_absolute() for path in paths) or paths[2].exists() or paths[2].is_symlink():
        raise ValueError('scoped_worker_new_absolute_paths_required')
    original = read(launcher, launcher_sha256)
    original_namespace = read(namespace, namespace_sha256)
    text = original.decode('utf8')
    text = once(text, 'sys.path.insert(0, ' + repr(str(Path(launcher).parent)) + ')',
                'sys.path.insert(0, ' + repr(str(installed_root)) + ')')
    text = once(text, 'from assistant_mesh.governance_worker import make_worker\n',
                'from assistant_mesh.governance_worker import make_worker\n'
                'from assistant_mesh.task_scoped_profile import TaskScopedProfile\n')
    text = once(text,
        '    cls = make_worker(Worker, Codex, governance, scopes=SCOPES, receipt_callback=receipt)',
        '    profile = TaskScopedProfile(lambda agent: private_json(' + repr(str(profile_path)) + '),\n'
        '                                scopes=(' + repr(PHONE_SCOPE) + ',))\n'
        '    cls = make_worker(Worker, Codex, governance, scopes=SCOPES,\n'
        '                      profile_loader=profile, receipt_callback=receipt)')
    text = once(text, '    cls = observe_worker(cls, diagnostics)\n',
        '    cls = observe_worker(cls, diagnostics)\n\n'
        '    def factory():\n'
        '        worker = cls(config, config_path=args.config)\n'
        '        profile.bind(worker)\n'
        '        return worker\n')
    text = once(text, 'return run_worker(lambda: cls(config, config_path=args.config))', 'return run_worker(factory)')
    text = once(text, '    worker = cls(config, config_path=args.config)\n    if args.once:',
                '    worker = factory()\n    if args.once:')
    pins = {str(launcher): launcher_sha256, str(namespace): namespace_sha256}
    prelude = ('# Preserve the pinned incumbent sources; changes live only in this release.\n'
        'import hashlib\nfrom pathlib import Path\n'
        'for _path, _digest in ' + repr(pins) + '.items():\n'
        '    if Path(_path).is_symlink() or hashlib.sha256(Path(_path).read_bytes()).hexdigest() != _digest:\n'
        "        raise RuntimeError('scoped_worker_incumbent_source_changed')\n\n")
    outputs = {'launch_worker.py': (prelude + text).encode('utf8'),
        'assistant_mesh/__init__.py': ('__path__.append(' + repr(str(Path(namespace).parent)) + ')\n').encode()
            + original_namespace,
        'assistant_mesh/task_scoped_profile.py': read(Path(support_root) / 'assistant_mesh/task_scoped_profile.py')}
    root = Path(destination)
    root.mkdir(mode=0o700)
    for relative, raw in outputs.items():
        ast.parse(raw.decode('utf8'), feature_version=(3, 6))
        path = root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
    report = dict(format=FORMAT, source_sha256={name: sha(raw) for name, raw in outputs.items()},
        incumbent_pins=pins, installed_root=str(installed_root), profile_path=str(profile_path),
        profile_scopes=[PHONE_SCOPE], governance_scopes_changed=False,
        initial_owner_profile={'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'revision': 1},
        model_catalog_validation='existing_reasoning_make_codex',
        effective_model_effort_verified=False, production_installed=False)
    with (root / 'scoped-worker-candidate.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('launcher', 'namespace', 'destination', 'launcher-sha256', 'namespace-sha256',
                 'installed-root', 'profile-path'):
        parser.add_argument('--' + name, required=True)
    print(json.dumps(build(**vars(parser.parse_args())), sort_keys=True))


if __name__ == '__main__':
    main()
