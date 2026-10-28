"""Assemble an additive phone overlay from pinned ACTUAL receiver snapshots.

Pure source assembly: never imports runtime modules, opens a Store, connects to
production, reads private owner config, starts a listener or creates identity.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


FORMAT = 'amita-phone-frozen-candidate/1'
PATCHED = ('session_nodes', 'session_node_authority', 'allocations')
ADDITIVE = ('phone_authority', 'phone_http', 'phone_updates', 'phone_owner')
DEPENDENCIES = ('cloud_http', 'enrollment', 'enrollment_bootstrap', 'enrollment_relay')
FORBIDDEN = frozenset(('store.py', 'worker.py', 'codex.py', 'server.py', 'entrance.py',
                       'entrance_runtime.py', 'entrance_media.py', 'native_history.py',
                       'sessions.py', 'amita_task_methods.py'))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('phone_source_regular_bounded_file_required')
    return path.read_bytes()


def once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('phone_frozen_exact_seam_changed')
    return text.replace(before, after, 1)


def method(text, class_name, name, change):
    cls = next(node for node in ast.parse(text).body if isinstance(node, ast.ClassDef) and node.name == class_name)
    node = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    lines = text.splitlines(keepends=True)
    start, end = sum(map(len, lines[:node.lineno - 1])), sum(map(len, lines[:node.end_lineno]))
    return text[:start] + change(text[start:end]) + text[end:]


def patch_session(raw):
    text = raw.decode('utf8')
    def constructor(body):
        body = once(body, 'work_lease_seconds=900):', 'work_lease_seconds=900, node_bindings=None):')
        return once(body, '        self.work_lease_seconds = work_lease_seconds\n',
            '        self.work_lease_seconds = work_lease_seconds\n'
            '        self.node_bindings = {} if node_bindings is None else dict(node_bindings)\n'
            '        for subject, binding in self.node_bindings.items():\n'
            "            if subject not in subjects or not isinstance(binding, dict) or set(binding) != {'session_id', 'node_id'}:\n"
            "                raise ValueError('invalid_session_node_binding')\n"
            '            for key, value in binding.items():\n'
            "                _identifier(value, 'bound_' + key)\n")
    text = method(text, 'LocalSessionNodes', '__init__', constructor)
    text = method(text, 'LocalSessionNodes', '_node_id', lambda body: once(body,
        '    def _node_id(self, subject, session_id):\n',
        '    def _node_id(self, subject, session_id):\n'
        '        binding = self.node_bindings.get(subject)\n'
        '        if binding is not None:\n'
        "            if binding['session_id'] != session_id:\n"
        "                raise PermissionError('session_node_binding_mismatch')\n"
        "            return binding['node_id']\n"))
    return text.encode('utf8')


def patch_allocations(raw):
    text = raw.decode('utf8')
    for name in ('accept', 'start'):
        def change(body, name=name):
            body = once(body, 'def ' + name + '(self, actor, operation_id, capability_id, epoch, receipt_id):',
                'def ' + name + '(self, actor, operation_id, capability_id, epoch, receipt_id, admission_check=None):')
            return once(body, '            self._admission(db, allocation, now)\n',
                '            self._admission(db, allocation, now)\n'
                '            if admission_check is not None:\n'
                '                admission_check(db)\n')
        text = method(text, 'Allocations', name, change)
    return text.encode('utf8')


def patch_authority(raw):
    return once(raw.decode('utf8'),
        "fields = {'subjects', 'mesh_id', 'ttl_seconds', 'work_lease_seconds'}",
        "fields = {'subjects', 'mesh_id', 'ttl_seconds', 'work_lease_seconds', 'node_bindings'}").encode('utf8')


TRANSFORMS = {'session_nodes': patch_session, 'session_node_authority': patch_authority,
              'allocations': patch_allocations}


def load_snapshot(path, expected_sha256, required=PATCHED):
    path = Path(path)
    raw = regular(path)
    if sha(raw) != expected_sha256:
        raise ValueError('phone_snapshot_manifest_pin_mismatch')
    value = json.loads(raw)
    if value.get('format') != 'amita-receiver-source-snapshot/1':
        raise ValueError('phone_actual_receiver_snapshot_required')
    files, pins = {}, {}
    for entry in value['files']:
        relative = PurePosixPath(entry['relative'])
        if relative.is_absolute() or str(relative) != entry['relative'] or '..' in relative.parts:
            raise ValueError('phone_snapshot_path_invalid')
        data = regular(path.parent / str(relative))
        if len(data) != entry['bytes'] or sha(data) != entry['sha256']:
            raise ValueError('phone_actual_source_pin_mismatch')
        source = entry['source']
        if not Path(source).is_absolute() or pins.get(source, entry['sha256']) != entry['sha256']:
            raise ValueError('phone_actual_source_mapping_invalid')
        files[str(relative)], pins[source] = data, entry['sha256']
    for name in required:
        resolution = value['resolution'][name]
        if resolution['state'] != 'found' or sha(files['resolved/' + name + '.py']) != resolution['sha256']:
            raise ValueError('phone_resolved_baseline_required')
    return value, files, pins


def launcher_source(launcher, runtime_config, origin, port, pins):
    # Readiness is owned by the original nested final receiver module. Merely
    # assigning incumbent.ready would not affect its previous.main().
    return ('''"""Add phone readiness after the unchanged actual receiver ready chain."""
import hashlib
import importlib.util
from pathlib import Path

_PINS = ''' + repr(pins) + '''
for _path, _expected in _PINS.items():
    _source = Path(_path)
    if _source.is_symlink() or hashlib.sha256(_source.read_bytes()).hexdigest() != _expected:
        raise RuntimeError('phone_incumbent_source_changed')

_spec = importlib.util.spec_from_file_location('amita_incumbent_phone_receiver', ''' + repr(launcher) + ''')
_incumbent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_incumbent)
_prior_ready = _incumbent.ready
if _incumbent.previous.ready is not _prior_ready:
    raise RuntimeError('phone_incumbent_ready_chain_changed')


def ready(http_server, api, channel):
    _prior_ready(http_server, api, channel)
    from assistant_mesh.config import private_json
    from assistant_mesh.phone_http import start
    from assistant_mesh.phone_updates import UpdateService
    from assistant_mesh.phone_authority import RegistryEnrollment
    from assistant_mesh.enrollment_bootstrap import EnrollmentBootstrap
    options = private_json(''' + repr(runtime_config) + ''')
    if (set(options) != {'format', 'owner_config', 'updates', 'enrollments'}
            or options['format'] != 'amita-phone-runtime/1'):
        raise ValueError('phone_runtime_owner_config_invalid')
    update = options['updates']
    if set(update) != {'directory', 'key_id', 'public_key_spki_base64'}:
        raise ValueError('phone_runtime_public_update_trust_required')
    updates = UpdateService(update['directory'], key_id=update['key_id'],
        public_key_spki_base64=update['public_key_spki_base64'], origin=''' + repr(origin) + ''')
    relays = []
    if not isinstance(options['enrollments'], list) or len(options['enrollments']) > 32:
        raise ValueError('phone_runtime_enrollments_invalid')
    for enrollment in options['enrollments']:
        if set(enrollment) != {'bootstrap_config', 'relay_config'}:
            raise ValueError('phone_runtime_enrollment_binding_invalid')
        bootstrap = EnrollmentBootstrap(enrollment['bootstrap_config'])
        registry = RegistryEnrollment(api.resources, bootstrap)
        bootstrap.registrar, bootstrap.registration_observer = registry.register, registry.observe
        relays.append((bootstrap, enrollment['relay_config']))
    server, service = start(api, options['owner_config'], origin=''' + repr(origin) + ''',
        port=''' + repr(port) + ''', relays=relays, updates=updates)
    api._phone_listener = server
    print('AMITA_PHONE_TRANSPORT_READY', flush=True)


_incumbent.previous.ready = ready

if __name__ == '__main__':
    _incumbent.previous.main()
''').encode('utf8')


def build(snapshot, destination, *, snapshot_sha256, runtime_config, origin, port=17684,
          support_root=None, extra_snapshot=None, extra_snapshot_sha256=None):
    from urllib.parse import urlsplit
    url = urlsplit(origin)
    if (url.scheme != 'https' or not url.hostname or url.path or url.query or url.fragment
            or url.username or url.password or url.port not in (None, 443)
            or type(port) is not int or not 1024 <= port <= 65535
            or not Path(runtime_config).is_absolute()):
        raise ValueError('phone_frozen_owner_parameters_invalid')
    support = Path(support_root or Path(__file__).resolve().parents[1])
    value, files, pins = load_snapshot(snapshot, snapshot_sha256)
    if not extra_snapshot or not extra_snapshot_sha256:
        raise ValueError('phone_provider_runtime_snapshot_pin_required')
    extra_value, extra_files, extra_pins = load_snapshot(extra_snapshot, extra_snapshot_sha256,
                                                       required=('provider_runtime',))
    for source, digest in extra_pins.items():
        if source in pins and pins[source] != digest:
            raise ValueError('phone_extra_source_pin_conflict')
        pins[source] = digest
    root = Path(destination)
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    package = root / 'assistant_mesh'; package.mkdir(mode=0o700)
    outputs, modified = {}, {}
    for name in PATCHED:
        before = files['resolved/' + name + '.py']
        after = TRANSFORMS[name](before)
        outputs['assistant_mesh/' + name + '.py'] = after
        modified[name] = {'before': sha(before), 'after': sha(after),
                          'source': value['resolution'][name]['source']}
    for name in ADDITIVE + DEPENDENCIES:
        if name in DEPENDENCIES and value['resolution'].get(name, {}).get('state') != 'missing':
            raise ValueError('phone_dependency_not_confirmed_absent')
        outputs['assistant_mesh/' + name + '.py'] = regular(support / 'assistant_mesh' / (name + '.py'))
    actual_package = str(Path(next(e['source'] for e in value['files']
                                   if e['relative'] == 'incumbent/assistant_mesh/__init__.py')).parent)
    # Keep every unknown incumbent module reachable, then execute its original
    # namespace bytes verbatim (including all original fallback paths).
    outputs['assistant_mesh/__init__.py'] = (
        "# Additive phone overlay; preserve the complete actual namespace.\n"
        '__path__.append(' + repr(actual_package) + ')\n').encode() + files['incumbent/assistant_mesh/__init__.py']
    launcher = next(e['source'] for e in value['files'] if e['relative'] == 'incumbent/launch_receiver.py')
    if files['incumbent/launch_receiver.py'].count(b'previous.ready = ready') != 1:
        raise ValueError('phone_actual_ready_seam_changed')
    outputs['launch_receiver.py'] = launcher_source(launcher, runtime_config, origin, port, pins)
    for relative, raw in outputs.items():
        if Path(relative).name in FORBIDDEN:
            raise ValueError('phone_protected_runtime_module_override')
        compile(raw, '<phone-frozen-source>', 'exec')
        with (root / relative).open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600); stream.write(raw)
    report = {'format': FORMAT, 'snapshot_sha256': snapshot_sha256,
        'extra_snapshot_sha256': extra_snapshot_sha256,
        'source_sha256': {name: sha(raw) for name, raw in outputs.items()},
        'runtime_input_pins': pins, 'patched_modules': modified,
        'additive_modules': list(ADDITIVE + DEPENDENCIES),
        'incumbent_package': actual_package, 'incumbent_launcher': launcher,
        'preserved_package_paths': value['package_paths'], 'origin': origin, 'port': port,
        'runtime_config': runtime_config, 'production_installed': False,
        'ledger_opened': False, 'phone_enrolled': False, 'device_challenge_created': False}
    with (root / 'phone-candidate.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, ensure_ascii=True, sort_keys=True, indent=2)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description='Build pinned actual frozen phone SOURCE overlay only')
    for flag in ('snapshot', 'snapshot-sha256', 'destination', 'runtime-config', 'origin'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--port', type=int, default=17684)
    parser.add_argument('--support-root')
    parser.add_argument('--extra-snapshot', required=True)
    parser.add_argument('--extra-snapshot-sha256', required=True)
    args = parser.parse_args()
    report = build(args.snapshot, args.destination, snapshot_sha256=args.snapshot_sha256,
        runtime_config=args.runtime_config, origin=args.origin, port=args.port, support_root=args.support_root,
        extra_snapshot=args.extra_snapshot, extra_snapshot_sha256=args.extra_snapshot_sha256)
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ('source_sha256', 'runtime_input_pins')}, sort_keys=True))


if __name__ == '__main__':
    main()
