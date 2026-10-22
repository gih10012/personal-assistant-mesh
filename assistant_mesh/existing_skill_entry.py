"""Existing-device native skill entry, source checks and private apply payloads.

No RPC, original skill execution, profile config/auth edits or new-node scan.
Only explicit install_entry writes a new private entry and its exact skill link.
"""
import base64
import os
from pathlib import Path, PurePosixPath
import stat
import sys

from . import existing_capabilities as source


NAME = 'mesh-existing-capabilities'
FORMAT = 'mesh-existing-skill-entry-plan/1'
_FILES = frozenset(('SKILL.md', 'scripts/verify.py', 'references/binding.json'))


def _absolute(value):
    path = Path(value)
    source._check(path.is_absolute() and '..' not in path.parts
                  and all(ord(char) >= 32 and ord(char) != 127 for char in str(path)),
                  'capability_entry_absolute_path_required')
    return path


def _inventory(bundle, installation):
    return {'format': source.FORMAT, 'provider': bundle['source_provider'],
            'revision': bundle['revision'], 'dependencies': {}, 'sources': [
                {'id': name, 'root': str(installation / name), 'files': [
                    {'path': item['path'].partition('/')[2], 'sha256': item['sha256']}
                    for item in bundle['files'] if item['path'].partition('/')[0] == name]}
                for name in bundle['sources']]}


def installed_discovery(bundle, installation, execution_provider):
    """Read every exact installed member/receipt; compile without importing code."""
    source._name(execution_provider)
    decoded, receipt = source._bundle_members(bundle)
    installation = source._private_parent(_absolute(installation))
    present = source._existing_members(installation, decoded, receipt)
    source._check('installation.json' in present, 'capability_install_receipt_required')
    view = source.inspect_inventory(_inventory(bundle, installation))
    parseable = True
    for name, raw in decoded:
        if name.endswith('.py'):
            try:
                compile(raw, '<pinned-existing-capability>', 'exec', dont_inherit=True)
            except (SyntaxError, ValueError):
                parseable = False
    actions = []
    for item in view['actions']:
        if item['portability'] != 'portable':
            continue
        actions.append(dict(item, provider=execution_provider,
            instance=execution_provider + ':' + item['id'], source_provider=view['provider'],
            target_dependencies_verified=False, native_discovery_verified=False))
    return {'format': 'mesh-existing-installed-discovery/1', 'provider': execution_provider,
            'bundle_sha256': receipt['bundle_sha256'], 'revision': receipt['revision'],
            'source_provider': receipt['source_provider'], 'source_file_count': len(decoded),
            'installation_checked': True, 'inspector_python_minimum_met': sys.version_info >= (3, 9),
            'code_parseable_by_inspector': parseable, 'actions': actions,
            'runtime_verified': False, 'authentication_checked': False,
            'native_discovery_verified': False, 'native_features_restricted': False}


def _profile_link(profile_skills, destination):
    root = _absolute(profile_skills)
    source._check(str(root.resolve()) == str(root), 'capability_entry_canonical_profile_root_required')
    for parent in (root,) + tuple(root.parents):
        info = parent.lstat()
        source._check(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.getuid())
                      and (not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX),
                      'capability_entry_profile_root_unsafe')
    source._check(root.stat().st_uid == os.getuid(), 'capability_entry_profile_root_unsafe')
    link = root / NAME
    if os.path.lexists(str(link)):
        source._check(link.is_symlink() and os.readlink(str(link)) == str(destination),
                      'capability_entry_existing_profile_link')
    return link


def entry_plan(bundle, installation, destination, execution_provider, python_executable,
               profile_skills, template_root=None):
    """Private, reviewable create-only payload for Root's selected EXISTING profile."""
    installation, destination = _absolute(installation), source._private_parent(_absolute(destination))
    source._check(installation != destination and installation not in destination.parents
                  and destination not in installation.parents, 'capability_entry_distinct_destination_required')
    report = installed_discovery(bundle, installation, execution_provider)
    python = _absolute(python_executable)
    info = python.stat()  # Owner-selected existing executable, never run here.
    source._check(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid())
                  and not info.st_mode & 0o022 and info.st_mode & 0o111,
                  'capability_entry_python_executable_required')
    link = _profile_link(profile_skills, destination)
    decoded, receipt = source._bundle_members(bundle)
    actions = {}
    for item in report['actions']:
        root = installation / item['source']
        action = {'skill': str(root / 'SKILL.md'), 'references': [str(installation / name)
            for name, unused in decoded if name.startswith(item['source'] + '/references/')]}
        if item['id'] == 'ncut.knowledge':
            action['script'] = str(root / 'scripts/ncut.py')
        elif item['id'] == 'xiumi.compose':
            action['module'] = str(root / 'scripts/xiumi_components.py')
        else:
            action['script'] = str(root / 'scripts/xiumi_clipboard.py')
        actions[item['id']] = action
    binding = {'format': 'mesh-existing-skill-binding/1', 'provider': execution_provider,
        'source_provider': receipt['source_provider'], 'installation': str(installation),
        'bundle_sha256': receipt['bundle_sha256'], 'revision': receipt['revision'],
        'installation_receipt_sha256': source._sha(source._encoded(receipt)),
        'files': receipt['files'], 'python_executable': str(python), 'actions': actions,
        'runtime_verified': False, 'native_discovery_verified': False}
    template = Path(template_root or Path(__file__).resolve().parents[1] / 'deploy' / NAME)
    files = [('references/binding.json', source._encoded(binding)),
             ('scripts/verify.py', source._source_bytes(template, 'scripts/verify.py')),
             ('SKILL.md', source._source_bytes(template, 'SKILL.md'))]
    advertisements = source.advertisements(_inventory(bundle, installation), execution_provider)
    requests = [item for item in advertisements['requests']
                if item['arguments']['spec']['portability'] == 'portable']
    for item in requests:
        item['arguments']['spec']['native_entry'] = NAME
        item['arguments']['spec']['installation_bundle_sha256'] = receipt['bundle_sha256']
    return {'format': FORMAT, 'destination': str(destination), 'profile_skill_link': str(link),
        'binding': binding, 'files': [{'path': name, 'sha256': source._sha(raw),
                                    'base64': base64.b64encode(raw).decode('ascii')} for name, raw in files],
        'registration_requests': requests, 'runtime_verified': False, 'rpc_attempted': False,
        'authentication_copied': False, 'native_features_restricted': False}


def install_entry(plan, resume_exact=False):
    """Explicit owner apply, or exact same-target resume; never overwrite/repair."""
    source._check(type(resume_exact) is bool and isinstance(plan, dict)
                  and set(plan) == {'format', 'destination', 'profile_skill_link', 'binding', 'files',
                      'registration_requests', 'runtime_verified', 'rpc_attempted',
                      'authentication_copied', 'native_features_restricted'}
                  and plan.get('format') == FORMAT and plan.get('rpc_attempted') is False
                  and plan.get('runtime_verified') is False and plan.get('authentication_copied') is False,
                  'capability_entry_plan_invalid')
    source._check(isinstance(plan['registration_requests'], list) and
                  all(isinstance(item, dict) and item.get('action') == 'advertise'
                      and isinstance(item.get('arguments'), dict)
                      and item['arguments'].get('health') == 'unavailable'
                      and isinstance(item['arguments'].get('spec'), dict)
                      and item['arguments']['spec'].get('runtime_verified') is False
                      and item['arguments']['spec'].get('managed_use_authorized') is False
                      for item in plan['registration_requests']), 'capability_entry_plan_invalid')
    destination = source._private_parent(_absolute(plan['destination']))
    binding = plan['binding']
    installation = _absolute(binding['installation'])
    source._check(destination != installation and installation not in destination.parents
                  and destination not in installation.parents, 'capability_entry_distinct_destination_required')
    actual_receipt = source._owner_bytes(installation / 'installation.json', source.MAX_TOTAL,
                                        'capability_entry_original_receipt_unavailable')
    source._check(source._sha(actual_receipt) == binding['installation_receipt_sha256'],
                  'capability_entry_original_receipt_changed')
    original = []
    for item in binding['files']:
        name = item['path']
        first, separator, relative = name.partition('/')
        source._check(separator and first in source._SOURCES[:2], 'capability_entry_plan_invalid')
        source._relative(relative)
        raw = source._source_bytes(installation / first, relative)
        source._check(source._sha(raw) == item['sha256'], 'capability_entry_original_source_changed')
        original.append((name, raw))
    source._existing_members(installation, original, source._decode(actual_receipt))
    link = _absolute(plan['profile_skill_link'])
    source._check(link.name == NAME and _profile_link(link.parent, destination) == link,
                  'capability_entry_profile_link_invalid')
    decoded = []
    source._check(isinstance(plan['files'], list) and len(plan['files']) == len(_FILES),
                  'capability_entry_plan_invalid')
    for item in plan['files']:
        source._check(isinstance(item, dict) and set(item) == {'path', 'sha256', 'base64'}
                      and isinstance(item['path'], str) and isinstance(item['base64'], str)
                      and item['path'] in _FILES, 'capability_entry_plan_invalid')
        raw = base64.b64decode(item['base64'].encode('ascii'), validate=True)
        source._check(len(raw) <= source.MAX_FILE and source._sha(raw) == source._hash(item['sha256']),
                      'capability_entry_plan_invalid')
        decoded.append((item['path'], raw))
    source._check({name for name, unused in decoded} == _FILES
                  and dict(decoded)['references/binding.json'] == source._encoded(binding),
                  'capability_entry_plan_invalid')
    receipt = {'format': 'mesh-existing-skill-installation/1',
        'bundle_sha256': binding['bundle_sha256'], 'provider': binding['provider'],
        'files': [{'path': name, 'sha256': source._sha(raw)} for name, raw in decoded],
        'authentication_copied': False, 'runtime_verified': False}
    if resume_exact:
        present = source._existing_members(destination, decoded, receipt)
    else:
        destination.mkdir(mode=0o700)
        present = set()
    if 'installation.json' not in present:
        for name, raw in decoded:
            if name not in present:
                source._make_private_parents(destination, PurePosixPath(name).parent)
                source._write_new(destination / name, raw)
        source._write_new(destination / 'installation.json', source._encoded(receipt))
    linked = not os.path.lexists(str(link))
    if linked:
        link.symlink_to(destination, target_is_directory=True)
        directory = os.open(str(link.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    else:
        _profile_link(link.parent, destination)
    return dict(receipt, profile_link_created=linked, native_discovery_verified=False,
                registration_requests=plan['registration_requests'], rpc_attempted=False)
