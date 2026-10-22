"""Explicit adaptation of three existing owner skills, not a new-node installer.

This optional CLI builds declarations and exact private source bundles. It never
imports a skill, starts an agent, touches authentication state or calls a Mesh
server. Registry and remote-delegation payloads use the existing APIs. Code
presence is not readiness, an advertisement is not a grant, and a copied skill
does not acquire its source device's sessions or permissions.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .provider_runtime import _owner_bytes, _unique_object
from .resources import _safe_metadata


FORMAT = 'mesh-existing-capabilities/1'
BUNDLE_FORMAT = 'mesh-private-skill-bundle/1'
MAX_FILES = 160
MAX_FILE = 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_SOURCES = ('ncut-web-api', 'xiumi-native-clipboard', 'niri-computer-use')

# An action, not a skill name, is the unit of portability. Runtime requirements
# are explicit; in particular the VPS host's Python 3.6 cannot run these skills.
_ACTIONS = (
    ('ncut.knowledge', 'ncut-web-api', 'knowledge.lookup', 'portable', ('python>=3.9',)),
    ('ncut.business', 'ncut-web-api', 'campus.business', 'provider',
     ('python>=3.9', 'ncut.account', 'ncut.campus-route', 'owner.business-scope')),
    ('xiumi.compose', 'xiumi-native-clipboard', 'document.compose', 'portable',
     ('python>=3.9',)),
    ('xiumi.validate', 'xiumi-native-clipboard', 'document.validate', 'portable',
     ('python>=3.9',)),
    ('xiumi.localize', 'xiumi-native-clipboard', 'images.localize-and-paste', 'provider',
     ('xiumi.browser', 'xiumi.clipboard', 'xiumi.authorized-assets', 'gui.single-operator')),
    ('niri.observe', 'niri-computer-use', 'desktop.windows.read', 'provider',
     ('niri.backend', 'niri.wayland-dbus-session', 'gui.single-operator')),
    ('niri.control', 'niri-computer-use', 'desktop.operate', 'provider',
     ('niri.backend', 'niri.wayland-dbus-session', 'gui.single-operator', 'owner.business-scope')),
)
_DEPENDENCIES = frozenset(item for action in _ACTIONS for item in action[4]
                          if not item.startswith('python'))


class CapabilityAdaptationError(ValueError):
    """Fixed diagnostics only; do not leak source paths or private contents."""


def _check(condition, category):
    if not condition:
        raise CapabilityAdaptationError(category)


def _encoded(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf8')
    except (ValueError, TypeError):
        raise CapabilityAdaptationError('capability_json_invalid') from None


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _name(value):
    _check(isinstance(value, str) and _NAME.fullmatch(value) is not None,
           'capability_identifier_invalid')
    return value


def _hash(value):
    _check(isinstance(value, str) and _HASH.fullmatch(value) is not None,
           'capability_pin_invalid')
    return value


def _decode(raw):
    try:
        value = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        _encoded(value)
        return value
    except Exception:
        raise CapabilityAdaptationError('capability_json_invalid') from None


def _outside_repository(path):
    path = Path(path)
    _check(path.is_absolute() and '..' not in path.parts,
           'capability_private_path_required')
    _check(all(not (parent / '.git').exists() for parent in (path,) + tuple(path.parents)),
           'capability_private_path_required')
    return path


def _private_json(path, maximum=MAX_TOTAL * 2, expected_sha=None):
    path = _outside_repository(path)
    try:
        raw = _owner_bytes(path, maximum, 'capability_private_file_invalid')
    except Exception:
        raise CapabilityAdaptationError('capability_private_file_invalid') from None
    if expected_sha is not None:
        _check(_sha(raw) == _hash(expected_sha), 'capability_private_file_changed')
    return _decode(raw), _sha(raw)


def _private_parent(path):
    path = _outside_repository(path)
    for parent in path.parents:
        entry = parent.lstat()
        _check(stat.S_ISDIR(entry.st_mode) and entry.st_uid in (0, os.getuid())
               and (not entry.st_mode & 0o022 or entry.st_mode & stat.S_ISVTX),
               'capability_private_parent_invalid')
    entry = path.parent.lstat()
    _check(entry.st_uid == os.getuid() and stat.S_IMODE(entry.st_mode) == 0o700,
           'capability_private_parent_invalid')
    return path


def _write_new(path, raw):
    path = _private_parent(path)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
    directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _relative(value):
    _check(isinstance(value, str) and 0 < len(value) <= 512
           and '\\' not in value and '\0' not in value,
           'capability_source_path_invalid')
    path = PurePosixPath(value)
    _check(not path.is_absolute() and all(part not in ('.', '..') for part in value.split('/'))
           and str(path) == value, 'capability_source_path_invalid')
    _check(value == 'SKILL.md' or (path.parts[0] in ('scripts', 'references', 'agents')
           and path.suffix in ('.py', '.mjs', '.js', '.md', '.json', '.yaml')),
           'capability_source_path_not_reviewable_code')
    _check(not any(part.startswith('.') or part.lower() in
                   ('state', 'accounts', 'auth', 'sessions', 'results', 'images', 'node_modules', '__pycache__')
                   for part in path.parts), 'capability_private_state_not_portable')
    _check(not re.search(r'(?i)(?:^|[-_.])(cookie|token|secret|credential|password)(?:[-_.]|$)', path.name),
           'capability_private_state_not_portable')
    return value


def _source_bytes(root, relative):
    """Read exact reviewed regular source, rejecting links and concurrent drift."""
    root = Path(root)
    _check(root.is_absolute() and '..' not in root.parts and
           os.path.realpath(str(root)) == str(root), 'capability_canonical_source_root_required')
    path = root.joinpath(*PurePosixPath(_relative(relative)).parts)
    for parent in path.parents:
        item = parent.lstat()
        _check(stat.S_ISDIR(item.st_mode) and item.st_uid in (0, os.getuid())
               and (not item.st_mode & 0o022 or item.st_mode & stat.S_ISVTX),
               'capability_source_parent_unsafe')
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        _check(stat.S_ISREG(before.st_mode) and before.st_uid in (0, os.getuid())
               and not before.st_mode & 0o022 and before.st_nlink == 1
               and before.st_size <= MAX_FILE, 'capability_source_file_unsafe')
        raw = source.read(MAX_FILE + 1)
        after = os.fstat(source.fileno())
    fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    current = path.lstat()
    _check(len(raw) <= MAX_FILE and len(raw) == before.st_size and
           all(getattr(before, field) == getattr(after, field) == getattr(current, field)
               for field in fields), 'capability_source_changed')
    return raw


def validate_inventory(value, pinned=True):
    """Pure shape check. Owner-declared references never become auth/evidence."""
    _check(isinstance(value, dict) and set(value) ==
           {'format', 'provider', 'revision', 'sources', 'dependencies'} and value['format'] == FORMAT,
           'capability_inventory_invalid')
    _name(value['provider'])
    _check(type(value['revision']) is int and 1 <= value['revision'] <= 9007199254740991,
           'capability_revision_invalid')
    _check(isinstance(value['sources'], list) and 1 <= len(value['sources']) <= 3,
           'capability_sources_invalid')
    seen, total = set(), 0
    for source in value['sources']:
        _check(isinstance(source, dict) and set(source) == {'id', 'root', 'files'}
               and source['id'] in _SOURCES and source['id'] not in seen,
               'capability_source_invalid')
        seen.add(source['id'])
        _check(isinstance(source['root'], str) and Path(source['root']).is_absolute()
               and '..' not in Path(source['root']).parts, 'capability_source_root_invalid')
        _check(isinstance(source['files'], list) and bool(source['files']), 'capability_source_files_invalid')
        paths = set()
        for item in source['files']:
            keys = {'path', 'sha256'} if pinned else {'path'}
            _check(isinstance(item, dict) and set(item) == keys, 'capability_source_file_invalid')
            relative = _relative(item['path'])
            _check(relative not in paths, 'capability_source_file_duplicate')
            paths.add(relative)
            if pinned:
                _hash(item['sha256'])
        _check('SKILL.md' in paths, 'capability_skill_instructions_required')
        # A partial copied skill must not advertise missing script entry points.
        if source['id'] == 'ncut-web-api':
            _check({'scripts/ncut.py', 'scripts/knowledge.py', 'references/services.json'} <= paths
                   and any(path.startswith('references/capabilities/') for path in paths),
                   'capability_ncut_lookup_closure_required')
        if source['id'] == 'xiumi-native-clipboard':
            _check({'scripts/xiumi_components.py', 'scripts/xiumi_clipboard.py', 'references/format.md'} <= paths,
                   'capability_xiumi_code_closure_required')
        if source['id'] == 'niri-computer-use':
            _check(paths == {'SKILL.md'}, 'capability_niri_backend_not_portable')
        total += len(paths)
    _check(total <= MAX_FILES, 'capability_too_many_files')
    dependencies = value['dependencies']
    _check(isinstance(dependencies, dict) and not set(dependencies) - _DEPENDENCIES,
           'capability_dependencies_invalid')
    for name, binding in dependencies.items():
        _check(isinstance(binding, dict) and set(binding) == {'ref', 'state'}
               and isinstance(binding['ref'], str) and binding['ref'].startswith('private:')
               and _NAME.fullmatch(binding['ref']) is not None
               and binding['state'] in ('missing', 'configured_not_verified'),
               'capability_dependency_reference_invalid')
    return _decode(_encoded(value))


def pin_selection(value):
    """Explicit bounded file selection, not recursive auto-export or execution."""
    value = validate_inventory(value, pinned=False)
    total = 0
    for source in value['sources']:
        for item in source['files']:
            raw = _source_bytes(source['root'], item['path'])
            total += len(raw)
            _check(total <= MAX_TOTAL, 'capability_sources_too_large')
            item['sha256'] = _sha(raw)
    return validate_inventory(value)


def inspect_inventory(value):
    value = validate_inventory(value)
    sources, total = {}, 0
    for source in value['sources']:
        for item in source['files']:
            raw = _source_bytes(source['root'], item['path'])
            _check(_sha(raw) == item['sha256'], 'capability_source_pin_changed')
            total += len(raw)
            _check(total <= MAX_TOTAL, 'capability_sources_too_large')
        sources[source['id']] = source
    actions = []
    for identity, source, action, portability, requirements in _ACTIONS:
        if source not in sources:
            continue
        actions.append({'id': identity, 'class': action, 'action': action,
            'instance': value['provider'] + ':' + identity, 'provider': value['provider'],
            'source': source, 'revision': value['revision'], 'portability': portability,
            'requirements': list(requirements), 'source_sha256': _sha(_encoded(sources[source]['files'])),
            'dependency_states': {name: value['dependencies'].get(name, {}).get('state', 'missing')
                                  for name in requirements if name in _DEPENDENCIES},
            'evidence_level': 'source_pinned_not_executed', 'runtime_verified': False,
            'managed_use_authorized': False})
    return {'format': 'mesh-existing-capability-inventory/1', 'provider': value['provider'],
            'revision': value['revision'], 'actions': actions, 'source_file_count': sum(
                len(source['files']) for source in sources.values()), 'source_bytes': total,
            'native_features_restricted': False, 'authentication_exported': False}


def advertisements(value, execution_provider=None, lease_seconds=90):
    """Ready existing /v1/resource/action advertise bodies; do not POST them."""
    view = inspect_inventory(value)
    provider = view['provider'] if execution_provider is None else _name(execution_provider)
    _check(type(lease_seconds) is int and 1 <= lease_seconds <= 86400, 'capability_lease_invalid')
    output = []
    for action in view['actions']:
        if provider != view['provider'] and action['portability'] != 'portable':
            continue
        descriptor = dict(action)
        descriptor.update(provider=provider, instance=provider + ':' + action['id'],
                          source_provider=view['provider'],
                          source_device=view['provider'] if action['portability'] == 'provider' else None,
                          proxy_binding='a2a:' + view['provider'] if action['portability'] == 'provider' else None,
                          dispatch='native-installed-code' if action['portability'] == 'portable'
                          else 'authenticated-a2a-local-helper',
                          target_dependencies_verified=False,
                          original_operation_required=True)
        # Do not federate source directories, auth handles or personal references.
        _check(not _safe_metadata(descriptor)[1], 'capability_descriptor_contains_private_metadata')
        output.append({'action': 'advertise', 'arguments': {
            'id': descriptor['instance'], 'kind': descriptor['class'],
            'description': 'Existing owner capability; source pinned, runtime unverified.',
            'spec': descriptor, 'lease_seconds': lease_seconds}})
    return {'format': 'mesh-existing-capability-advertisements/1', 'provider': provider,
            'requests': output, 'written': False, 'grants_created': False,
            'runtime_verified': False}


def build_bundle(value):
    """Only exact source files, no auth refs, source paths, results or MCP config."""
    inspect_inventory(value)
    files, total, sources = [], 0, []
    for source in value['sources']:
        if source['id'] == 'niri-computer-use':
            continue
        sources.append(source['id'])
        for pin in source['files']:
            raw = _source_bytes(source['root'], pin['path'])
            _check(_sha(raw) == pin['sha256'], 'capability_source_pin_changed')
            total += len(raw)
            _check(total <= MAX_TOTAL, 'capability_sources_too_large')
            files.append({'path': source['id'] + '/' + pin['path'], 'sha256': pin['sha256'],
                          'base64': base64.b64encode(raw).decode('ascii')})
    _check(bool(files), 'capability_no_portable_sources')
    return {'format': BUNDLE_FORMAT, 'source_provider': value['provider'],
            'revision': value['revision'], 'sources': sources, 'files': files,
            'contains_authentication': False, 'enables_plugins_or_mcp': False,
            'runtime_verified': False}


def _directory_private(path):
    entry = path.lstat()
    _check(stat.S_ISDIR(entry.st_mode) and entry.st_uid == os.getuid()
           and stat.S_IMODE(entry.st_mode) == 0o700, 'capability_install_directory_not_private')


def _make_private_parents(destination, relative_parent):
    """mkdir parents one at a time: Path.mkdir(parents=True) ignores parent mode."""
    current = destination
    _directory_private(current)
    for component in relative_parent.parts:
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _directory_private(current)


def _existing_members(destination, decoded, receipt):
    """Read-only full-tree preflight before any explicit resume write.

    Existing permissions are never repaired here. An owner may tighten only
    its independently identified partial-install directories first. Extra
    members, links, mismatching bytes or a partial completed receipt refuse.
    """
    _directory_private(destination)
    expected_files = dict(decoded)
    expected_dirs = {str(parent) for name in expected_files
                     for parent in PurePosixPath(name).parents if str(parent) != '.'}
    _check(not set(expected_files) & expected_dirs, 'capability_bundle_path_collision')
    present = set()
    pending = [destination]
    while pending:
        directory = pending.pop()
        _directory_private(directory)
        for path in directory.iterdir():
            relative = path.relative_to(destination).as_posix()
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                _check(relative in expected_dirs, 'capability_install_extra_member')
                _directory_private(path)
                pending.append(path)
            else:
                _check(relative in expected_files or relative == 'installation.json',
                       'capability_install_extra_member')
                expected = _encoded(receipt) if relative == 'installation.json' else expected_files[relative]
                actual = _owner_bytes(path, MAX_TOTAL, 'capability_install_member_unsafe')
                _check(actual == expected, 'capability_install_member_mismatch')
                present.add(relative)
    if 'installation.json' in present:
        _check(set(expected_files) <= present, 'capability_install_receipt_incomplete')
    return present


def install_bundle(value, destination, resume_exact=False):
    """New private materialization, or explicit verified same-target completion.

    No overwrite, profile activation, automatic retry or permission repair.
    """
    _check(type(resume_exact) is bool, 'capability_resume_mode_invalid')
    _check(isinstance(value, dict) and set(value) == {'format', 'source_provider', 'revision',
           'sources', 'files', 'contains_authentication', 'enables_plugins_or_mcp', 'runtime_verified'}
           and value['format'] == BUNDLE_FORMAT and value['contains_authentication'] is False
           and value['enables_plugins_or_mcp'] is False and value['runtime_verified'] is False,
           'capability_bundle_invalid')
    _name(value['source_provider'])
    _check(type(value['revision']) is int and value['revision'] >= 1
           and isinstance(value['sources'], list) and bool(value['sources'])
           and len(value['sources']) == len(set(value['sources']))
           and set(value['sources']) <= set(_SOURCES[:2]), 'capability_bundle_invalid')
    _check(isinstance(value['files'], list) and 1 <= len(value['files']) <= MAX_FILES,
           'capability_bundle_invalid')
    decoded, paths, total = [], set(), 0
    for item in value['files']:
        _check(isinstance(item, dict) and set(item) == {'path', 'sha256', 'base64'}
               and isinstance(item['path'], str) and isinstance(item['base64'], str),
               'capability_bundle_file_invalid')
        source, separator, relative = item['path'].partition('/')
        _check(separator and source in value['sources'] and item['path'] not in paths,
               'capability_bundle_file_invalid')
        _relative(relative)
        paths.add(item['path'])
        _check(len(item['base64']) <= 4 * ((MAX_FILE + 2) // 3), 'capability_bundle_file_too_large')
        try:
            raw = base64.b64decode(item['base64'].encode('ascii'), validate=True)
        except (ValueError, UnicodeError):
            raise CapabilityAdaptationError('capability_bundle_file_invalid') from None
        _check(len(raw) <= MAX_FILE and _sha(raw) == _hash(item['sha256']),
               'capability_bundle_pin_changed')
        total += len(raw)
        _check(total <= MAX_TOTAL, 'capability_bundle_too_large')
        decoded.append((item['path'], raw))
    _check(all(source + '/SKILL.md' in paths for source in value['sources']),
           'capability_bundle_instructions_missing')
    validate_inventory({'format': FORMAT, 'provider': value['source_provider'],
        'revision': value['revision'], 'dependencies': {}, 'sources': [
            {'id': source, 'root': '/not-used-during-shape-check', 'files': [
                {'path': name.partition('/')[2], 'sha256': _sha(raw)}
                for name, raw in decoded if name.partition('/')[0] == source]}
            for source in value['sources']]})
    receipt = {'format': 'mesh-private-skill-installation/1',
               'bundle_sha256': _sha(_encoded(value)), 'source_provider': value['source_provider'],
               'revision': value['revision'], 'sources': value['sources'],
               'files': [{'path': name, 'sha256': _sha(raw)} for name, raw in decoded],
               'authentication_copied': False, 'profile_changed': False,
               'runtime_verified': False}
    destination = _private_parent(destination)
    if resume_exact:
        # Require the original target to exist; do not silently turn a typo into
        # a new installation. Check every existing member before adding any.
        present = _existing_members(destination, decoded, receipt)
        if 'installation.json' in present:
            return receipt
    else:
        # Default remains create-only, including for an existing empty target.
        destination.mkdir(mode=0o700)
        present = set()
    for name, raw in decoded:
        if name in present:
            continue
        relative = PurePosixPath(name)
        _make_private_parents(destination, relative.parent)
        _write_new(destination.joinpath(*relative.parts), raw)
    # A failed partial write remains a mismatching member on the next explicit
    # preflight. Do not overwrite it, delete it or issue a success receipt.
    _write_new(destination / 'installation.json', _encoded(receipt))
    return receipt


def delegate_request(value, task_id, epoch, operation_id, action_id, arguments,
                     project_id='existing-device-capabilities'):
    """Pure original-identity payload for existing Remote.delegate/HTTP route.

    No alternate task scope, task submission, native execution or retry journal
    is created. The existing task and A2A receipt authority provide fencing.
    """
    view = inspect_inventory(value)
    for identity in (task_id, operation_id, action_id, project_id):
        _name(identity)
    _check(type(epoch) is int and epoch >= 1, 'capability_task_epoch_invalid')
    actions = [item for item in view['actions'] if item['id'] == action_id]
    _check(len(actions) == 1, 'capability_action_not_installed')
    action = actions[0]
    _check(isinstance(arguments, dict) and len(_encoded(arguments)) <= 16384,
           'capability_arguments_invalid')
    request = {'format': 'mesh-existing-capability-call/1', 'task_id': task_id,
               'task_epoch': epoch, 'operation_id': operation_id,
               'capability_instance': action['instance'], 'action': action['action'],
               'source': action['source'], 'source_files': next(source['files']
                   for source in value['sources'] if source['id'] == action['source']),
               'revision': view['revision'], 'source_sha256': action['source_sha256'],
               'arguments': arguments}
    call_id = 'capability-' + _sha(_encoded([task_id, operation_id]))
    prompt = ('Use your existing native tools and the locally installed original skill for this '
        'owner-authorized request. Read the skill and verify its pinned source/dependencies; '
        'configuration/presence is not readiness. Preserve the original operation/task identity '
        'and receipts. Do not retry unknown external effects or do paused unrelated work. '
        'Authentication, campus route, browser session and assets stay on their provider; '
        'never copy cookies, auth state, native history or personal files to another node. '
        'For GUI, first obtain the single-operator desktop lease/coordination; if another '
        'operator is active, report busy without input. Follow the installed niri skill: '
        'begin before input, wake only if needed at minimum brightness, end on all outcomes, '
        'preserve entry power/brightness and user windows. Read-only window enumeration '
        'does not need GUI input or wake. Return the actual result and evidence, not an '
        'accepted receipt as success. This Mesh path does not restrict native Shell/files/network/MCP. '
        'Immutable request JSON:\n' + _encoded(request).decode('utf8'))
    _check(len(prompt.encode('utf8')) <= 65536, 'capability_delegate_input_too_large')
    return {'task_id': task_id, 'epoch': epoch, 'call_id': call_id, 'peer': view['provider'],
            'arguments': {'input': prompt, 'project_id': project_id,
                          'agent_id': 'existing-device-helper', 'role': 'existing-device-helper'}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command')
    pin = commands.add_parser('pin')
    pin.add_argument('--selection', required=True)
    pin.add_argument('--output', required=True)
    for name in ('inspect', 'advertisements', 'bundle', 'delegate-request'):
        command = commands.add_parser(name)
        command.add_argument('--owner', required=True)
        command.add_argument('--owner-sha256', required=True)
        if name == 'advertisements':
            command.add_argument('--execution-provider')
            command.add_argument('--lease-seconds', type=int, default=90)
        if name == 'bundle':
            command.add_argument('--output', required=True)
        if name == 'delegate-request':
            command.add_argument('--task-id', required=True)
            command.add_argument('--epoch', type=int, required=True)
            command.add_argument('--operation-id', required=True)
            command.add_argument('--action-id', required=True)
            command.add_argument('--arguments-file', required=True)
    install = commands.add_parser('install')
    install.add_argument('--bundle', required=True)
    install.add_argument('--bundle-sha256', required=True)
    install.add_argument('--destination', required=True)
    install.add_argument('--resume-exact', action='store_true',
                         help='explicitly complete only the original exact partial target; no overwrite')
    args = parser.parse_args(argv)
    try:
        if args.command == 'pin':
            selection, _ = _private_json(args.selection, 262144)
            raw = _encoded(pin_selection(selection))
            _write_new(args.output, raw)
            result = {'pinned': True, 'sha256': _sha(raw), 'executed': False}
        elif args.command == 'install':
            bundle, _ = _private_json(args.bundle, expected_sha=args.bundle_sha256)
            result = install_bundle(bundle, args.destination, resume_exact=args.resume_exact)
        else:
            _check(args.command is not None, 'capability_command_required')
            value, _ = _private_json(args.owner, 262144, args.owner_sha256)
            if args.command == 'inspect':
                result = inspect_inventory(value)
            elif args.command == 'advertisements':
                result = advertisements(value, args.execution_provider, args.lease_seconds)
            elif args.command == 'bundle':
                raw = _encoded(build_bundle(value))
                _write_new(args.output, raw)
                result = {'bundled': True, 'sha256': _sha(raw), 'executed': False}
            else:
                arguments, _ = _private_json(args.arguments_file, 16384)
                result = delegate_request(value, args.task_id, args.epoch,
                                          args.operation_id, args.action_id, arguments)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (CapabilityAdaptationError, OSError, ValueError, TypeError):
        print(json.dumps({'error': 'existing_capability_adaptation_failed',
                          'automatic_retry': False, 'native_features_restricted': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
