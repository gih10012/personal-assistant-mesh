"""Add small, pinned recovery overlays without replacing incumbent core files.

Source-only assembly. No DB/config/history reads, imports of runtime code,
listeners, SSH, claims or process management occur while building.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORMAT = 'amita-runtime-recovery-backport/1'
METHODS_PIN = '2ee2b5db192426d2c5882684d76c8f3c8a2b7960b3fb0a679a6500d9836c42c6'
ISOLATION_PIN = '5f58ffaf130a6e86090eec9f7d78267dd1388b0f5a31387f516bcdec105c0c84'
METHODS = ('_claim_in_db', '_update_task_in_db', '_control_task', 'retry_waiting')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, expected=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('recovery_bounded_regular_source_required')
    raw = path.read_bytes()
    if expected is not None and sha(raw) != expected:
        raise ValueError('recovery_source_pin_changed')
    return raw


def once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('recovery_exact_source_seam_changed')
    return text.replace(before, after, 1)


def method(text, cls, name):
    owner = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == cls)
    item = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ''.join(text.splitlines(keepends=True)[item.lineno - 1:item.end_lineno])


def patch_methods(raw):
    text = raw.decode('utf8')
    extracted = {name: method(text, 'Store', name) for name in METHODS}
    claim = extracted['_claim_in_db']
    guard = """            # Native effects are not blindly replayed after losing a worker.
            if json.loads(row['checkpoint']).get('side_effect_started'):
                db.execute("UPDATE tasks SET status='needs_review',node=NULL,deadline=NULL WHERE id=?", (row['id'],))
                self._enqueue(db, 'review-' + row['id'], '任务 ' + row['id'][:8] + ' 执行中断，可能已产生外部效果，等待核对后续接。')
                continue
"""
    claim = once(claim, guard, '')
    guard = guard.replace('            # Native effects are not blindly replayed after losing a worker.\n', '')
    guard = guard.replace("json.loads(row['checkpoint']).get('side_effect_started')",
                          "isolation.native_work_unsettled(json.loads(row['checkpoint']))")
    claim = once(claim, '            admission = isolation.admit(self, db, row, Conflict)\n',
                 guard + '            admission = isolation.admit(self, db, row, Conflict)\n')
    extracted['_claim_in_db'] = once(claim, "json.loads(other['checkpoint']).get('side_effect_started')",
                                    "isolation.native_work_unsettled(json.loads(other['checkpoint']))")
    extracted['_update_task_in_db'] = once(extracted['_update_task_in_db'],
        "        isolation.validate_native(self, db, row, next_checkpoint, next_checkpoint.get('harness', 'codex'), Conflict)\n",
        "        if status in ('waiting_backend', 'waiting_auth') and isolation.native_work_unsettled(next_checkpoint):\n"
        "            status = 'needs_review'\n"
        "        isolation.validate_native(self, db, row, next_checkpoint, next_checkpoint.get('harness', 'codex'), Conflict)\n")
    extracted['_control_task'] = once(extracted['_control_task'], "checkpoint.get('side_effect_started')",
                                      'isolation.native_work_unsettled(checkpoint)')
    extracted['retry_waiting'] = once(extracted['retry_waiting'],
        "json.loads(row['checkpoint']).get('side_effect_started')",
        "isolation.native_work_unsettled(json.loads(row['checkpoint']))")
    return '\n\n'.join(extracted[name] for name in METHODS)


def patch_isolation(raw):
    text = once(raw.decode('utf8'), 'def _occupied(store, row):\n',
        "def native_work_unsettled(checkpoint):\n"
        "    failure = checkpoint.get('runtime_failure') or {}\n"
        "    return bool(checkpoint.get('side_effect_started')\n"
        "        or (failure.get('native_start_attempted') is True and failure.get('outcome') == 'unknown'))\n\n\n"
        'def _occupied(store, row):\n')
    text = once(text, "return bool(checkpoint.get('side_effect_started') or _goal_reserves(store, row)",
                'return bool(native_work_unsettled(checkpoint) or _goal_reserves(store, row)')
    text = once(text, "if checkpoint.get('side_effect_started') or _unsafe_thread(",
                'if native_work_unsettled(checkpoint) or _unsafe_thread(')
    return once(text,
        "names = ('claim', '_claim_in_db', 'update_task', '_update_task_in_db', 'session_action', 'continue_task')",
        "names = ('claim', '_claim_in_db', 'update_task', '_update_task_in_db', 'session_action', 'continue_task', '_control_task', 'retry_waiting')").encode('utf8')


def pinned_loader(source, digest, name):
    return ("import hashlib\nimport importlib.util\nimport sys\nfrom pathlib import Path\n"
        "_source = Path(" + repr(source) + ")\n"
        "if _source.is_symlink() or hashlib.sha256(_source.read_bytes()).hexdigest() != " + repr(digest) + ":\n"
        "    raise RuntimeError('recovery_incumbent_source_changed')\n"
        "_spec = importlib.util.spec_from_file_location(" + name + ", str(_source))\n"
        "_recovery_incumbent = importlib.util.module_from_spec(_spec)\n"
        "sys.modules[_spec.name] = _recovery_incumbent\n"
        "_spec.loader.exec_module(_recovery_incumbent)\n")


def write_candidate(destination, outputs, report):
    root = Path(destination)
    if not root.is_absolute() or root.exists() or root.is_symlink():
        raise ValueError('recovery_new_absolute_destination_required')
    root.mkdir(mode=0o700)
    for relative, raw in outputs.items():
        ast.parse(raw.decode('utf8'), feature_version=(3, 6))
        path = root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
    report = dict(report, format=FORMAT, source_sha256={name: sha(raw) for name, raw in outputs.items()},
                  source_bytes=sum(map(len, outputs.values())), production_installed=False,
                  ledger_opened=False, incumbent_files_modified=False)
    with (root / 'recovery-candidate.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, ensure_ascii=True, sort_keys=True, indent=2)
        stream.write('\n')
    return report


def build_receiver(snapshot, destination, *, snapshot_sha256, phone_candidate, installed_phone_root,
                   installed_methods_source=None):
    spec = importlib.util.spec_from_file_location('_recovery_phone_builder',
                                                 str(ROOT / 'deploy/build-phone-frozen-candidate.py'))
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    value, files, pins = builder.load_snapshot(snapshot, snapshot_sha256,
                                              required=('amita_task_methods', 'operation_isolation'))
    raw = files['resolved/amita_task_methods.py']
    isolation = files['resolved/operation_isolation.py']
    if sha(raw) != METHODS_PIN or sha(isolation) != ISOLATION_PIN:
        raise ValueError('recovery_actual_algorithm_pin_changed')
    phone = Path(phone_candidate)
    installed = Path(installed_phone_root)
    if not installed.is_absolute():
        raise ValueError('recovery_absolute_phone_root_required')
    manifest_raw = read(phone / 'phone-candidate.json')
    manifest = json.loads(manifest_raw)
    if manifest['format'] != 'amita-phone-frozen-candidate/1' or manifest['snapshot_sha256'] != snapshot_sha256:
        raise ValueError('recovery_phone_snapshot_binding_changed')
    for relative, digest in manifest['source_sha256'].items():
        read(phone / relative, digest)
    actual_methods = installed_methods_source or value['resolution']['amita_task_methods']['source']
    if not Path(actual_methods).is_absolute():
        raise ValueError('recovery_absolute_methods_source_required')
    helper = (pinned_loader(actual_methods, METHODS_PIN, "__package__ + '._recovery_original_task_methods'")
        + '\nimport json\nfrom . import operation_isolation as isolation\n'
        + 'Conflict = _recovery_incumbent.Conflict\n\nclass Store(_recovery_incumbent.Store):\n'
        + patch_methods(raw) + '\n').encode('utf8')
    outputs = {
        'assistant_mesh/amita_task_methods.py': helper,
        'assistant_mesh/operation_isolation.py': patch_isolation(isolation),
        'assistant_mesh/__init__.py': ("# Recovery overlay retains the complete phone namespace.\n"
            '__path__.append(' + repr(str(installed / 'assistant_mesh')) + ')\n').encode()
            + read(phone / 'assistant_mesh/__init__.py'),
    }
    launcher = (pinned_loader(str(installed / 'launch_receiver.py'),
        manifest['source_sha256']['launch_receiver.py'], "'_amita_phone_recovery_receiver'")
        + "\nif __name__ == '__main__':\n    _recovery_incumbent._incumbent.previous.main()\n").encode('utf8')
    outputs['launch_receiver.py'] = launcher
    return write_candidate(destination, outputs, dict(component='receiver', snapshot_sha256=snapshot_sha256,
        original_runtime_pins=pins, methods_source=actual_methods,
        phone_launcher=str(installed / 'launch_receiver.py'),
        phone_candidate_manifest_sha256=sha(manifest_raw), overridden_methods=list(METHODS),
        protected_core_modules_preserved=['store', 'worker', 'codex', 'entrance', 'native_history', 'sessions'],
        current_constructor_preserved=True, original_unknown_reconciled=False))


def build_control(source, destination, *, source_sha256, package_init, init_sha256,
                  installed_source=None, installed_init=None):
    raw = read(source, source_sha256)
    init = read(package_init, init_sha256)
    body = method(raw.decode('utf8'), 'Supervisor', '_recover')
    body = once(body, "                    db.execute('UPDATE recovery SET activated_at=? WHERE component=?', (now, recovery['component']))",
        "                    db.execute('UPDATE recovery SET source=?,activated_at=? WHERE component=?', (pending['id'], now, recovery['component']))")
    seam = "            # Every non-retired process must be CONFIRMED dead."
    body = once(body, seam,
        "            source = self.store._runner(db, recovery['source'])\n"
        "            if (source['state'] == 'standby' and source['note'] == 'rollback_standby'\n"
        "                    and source['ready'] and source['work'] is None\n"
        "                    and LinuxProcess.alive(source['pid'], source['birth'])\n"
        "                    and source['heartbeat'] is not None and source['heartbeat'] > now - 10\n"
        "                    and all(row['id'] == source['id'] or row['state'] == 'stopped'\n"
        "                        or (row['state'] != 'queued' and (row['death_confirmed']\n"
        "                            or LinuxProcess.observe(row['pid'], row['birth']) == 'dead'))\n"
        "                        for row in rows)):\n"
        "                db.execute(\"UPDATE runners SET state='active',note='automatic_readmission' WHERE id=?\", (source['id'],))\n"
        "                continue\n" + seam)
    # Keep all original fallback/backoff code; the repeated source lookup is safe.
    installed_source = str(installed_source or Path(source).absolute())
    installed_init = str(installed_init or Path(package_init).absolute())
    if not Path(installed_source).is_absolute() or not Path(installed_init).is_absolute():
        raise ValueError('recovery_absolute_control_source_required')
    loader = pinned_loader(installed_source, source_sha256, "__package__ + '._recovery_original_runtime'")
    runtime = (loader + "\nfor _recovery_export_name, _recovery_export_value in vars(_recovery_incumbent).items():\n"
        "    if not _recovery_export_name.startswith('__'):\n        globals()[_recovery_export_name] = _recovery_export_value\n"
        '\nclass Supervisor(_recovery_incumbent.Supervisor):\n' + body
        + '\n\n_recovery_incumbent.Supervisor = Supervisor\n'
        + "if __name__ == '__main__':\n    sys.exit(main())\n").encode('utf8')
    namespace = ('__path__.append(' + repr(str(Path(installed_init).parent)) + ')\n').encode() + init
    return write_candidate(destination, {'assistant_mesh/__init__.py': namespace,
        'assistant_mesh/rolling_runtime.py': runtime,
        'launch_control.py': b'from assistant_mesh.rolling_runtime import main\nraise SystemExit(main())\n'},
        dict(component='control', original_runtime_pins={installed_source: source_sha256, installed_init: init_sha256},
             overridden_methods=['Supervisor._recover'], original_state_directory_required=True,
             runner_processes_modified=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='component')
    receiver = commands.add_parser('receiver')
    for name in ('snapshot', 'snapshot-sha256', 'phone-candidate', 'installed-phone-root', 'destination'):
        receiver.add_argument('--' + name, required=True)
    receiver.add_argument('--installed-methods-source')
    control = commands.add_parser('control')
    for name in ('source', 'source-sha256', 'package-init', 'init-sha256', 'destination'):
        control.add_argument('--' + name, required=True)
    control.add_argument('--installed-source')
    control.add_argument('--installed-init')
    args = vars(parser.parse_args())
    component = args.pop('component')
    if component is None:
        parser.error('component required')
    report = (build_receiver if component == 'receiver' else build_control)(**args)
    print(json.dumps({key: value for key, value in report.items() if key != 'source_sha256'}, sort_keys=True))


if __name__ == '__main__':
    main()
