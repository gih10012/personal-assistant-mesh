"""Complete private FileTree OAuth bytes -> verified, inactive target staging.

Only the dedicated Mesh OAuth App's configured state and three explicit service
key references are in scope. No account/browser/Codex login discovery, token
exchange, service installation, writer election or activation is implemented.
"""
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import time

from .oauth_state_boundary import FreezeLease, SDK_PINS, STAGING_MARKER, VERSIONS


FORMAT = 'amita-oauth-state-bundle/1'
COLLECTIONS = ('mcp-upstream-tokens', 'mcp-oauth-proxy-clients', 'mcp-oauth-transactions',
               'mcp-consent-csrf-tokens', 'mcp-authorization-codes', 'mcp-jti-mappings', 'mcp-refresh-tokens')
# This gateway constructs FileTreeStore without optional V1 sanitizers. In
# pinned 0.4.6 the actual default is PassthroughStrategy for keys/collections.
COLLECTION_DIRECTORIES = {name: name for name in COLLECTIONS}
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
KEY_ROLES = ('dedicated_github_oauth_app_secret', 'mesh_oauth_jwt_signing', 'mesh_oauth_state_fernet')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('oauth_bundle_duplicate_json_key')
        value[key] = item
    return value


def decode(raw):
    return json.loads(raw.decode('utf8'), object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('oauth_bundle_nonfinite_json')))


def safe_path(value):
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or path.is_symlink()
            or any(parent.is_symlink() for parent in path.parents)):
        raise ValueError('oauth_bundle_private_absolute_path_required')
    return path


def _read(path, maximum):
    path = safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('oauth_bundle_owned_private_file_required')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('oauth_bundle_file_exceeds_budget')
    return raw


def binding(document, allow_loopback_for_tests=False):
    from .chat_github_oauth import validate_config
    config = validate_config(document, allow_loopback_for_tests=allow_loopback_for_tests)
    return config, {'format': config['format'], 'policy': config['policy'],
        'github': {key: config['github'][key] for key in ('client_id', 'owner_id')},
        'redirect_uris': config['redirect_uris']}


def key_references(config):
    paths = (config['github']['client_secret_file'], config['state']['jwt_key_file'], config['state']['encryption_key_file'])
    result = {}
    for role, path in zip(KEY_ROLES, paths):
        raw = _read(path, 4096)
        if len(raw.strip()) < 32:
            raise ValueError('oauth_bundle_service_key_invalid')
        result[role] = {'source_reference': str(path), 'sha256': sha(raw), 'size': len(raw),
                        'scope': 'dedicated_mesh_oauth_service_only'}
    if len({value['sha256'] for value in result.values()}) != 3:
        raise ValueError('oauth_bundle_distinct_service_keys_required')
    return result


def _relative(value):
    path = PurePosixPath(value)
    if (not isinstance(value, str) or not value or str(path) != value or path.is_absolute()
            or '..' in path.parts or '\\' in value or ':' in value):
        raise ValueError('oauth_bundle_relative_path_invalid')
    return path


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, stat.S_IMODE(info.st_mode)


def _scan(root):
    root = safe_path(root)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('oauth_bundle_owned_private_state_required')
    files, directories, signatures, size = {}, [], {'': _signature(info)}, 0
    for parent, dirs, names in os.walk(str(root), followlinks=False):
        dirs.sort(); names.sort()
        for name in dirs:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            _relative(relative)
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or path.is_symlink() or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or PurePosixPath(relative).parts[0] not in COLLECTION_DIRECTORIES.values()):
                raise ValueError('oauth_bundle_unexpected_state_directory')
            directories.append(relative); signatures[relative] = _signature(info)
        for name in names:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            _relative(relative)
            before = _signature(path.lstat())
            raw = _read(path, MAX_FILE)
            if before != _signature(path.lstat()):
                raise ValueError('oauth_bundle_source_changed')
            size += len(raw)
            if size > MAX_TOTAL or len(files) >= 10000 or len(directories) > 1000:
                raise ValueError('oauth_bundle_state_exceeds_budget')
            signatures[relative] = before
            files[relative] = raw
    return files, sorted(directories), signatures


def _layout(files, directories, source_root):
    if (not isinstance(source_root, str) or not Path(source_root).is_absolute()
            or '..' in Path(source_root).parts):
        raise ValueError('oauth_bundle_source_root_invalid')
    artifacts = []
    for relative in directories:
        path = _relative(relative)
        if len(path.parts) != 1 or path.parts[0] not in COLLECTION_DIRECTORIES.values():
            raise ValueError('oauth_bundle_collection_not_supported')
    for relative, raw in files.items():
        path = _relative(relative)
        if path.suffix == '.tmp':
            if len(path.parts) > 1 and path.parts[0] not in COLLECTION_DIRECTORIES.values():
                raise ValueError('oauth_bundle_non_sdk_artifact')
            artifacts.append(relative)  # Preserve orphan/unknown SDK writes, never silently discard.
            continue
        if len(path.parts) == 1:
            value = decode(raw)
            if (not relative.endswith('-info.json') or not isinstance(value, dict)
                    or set(value) != {'version', 'collection', 'directory', 'created_at'}
                    or value['version'] != 1 or value['collection'] not in COLLECTIONS
                    or relative != COLLECTION_DIRECTORIES[value['collection']] + '-info.json'
                    or value['directory'] != str(Path(source_root) / COLLECTION_DIRECTORIES[value['collection']])
                    or COLLECTION_DIRECTORIES[value['collection']] not in directories):
                raise ValueError('oauth_bundle_filetree_metadata_invalid')
        else:
            if len(path.parts) != 2 or path.parts[0] not in COLLECTION_DIRECTORIES.values() or path.suffix != '.json':
                raise ValueError('oauth_bundle_non_sdk_state_refused')
            value = decode(raw)
            encrypted = value.get('value') if isinstance(value, dict) else None
            if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
                    or not isinstance(encrypted, dict)
                    or set(encrypted) != {'__encrypted_data__', '__encryption_version__'}
                    or encrypted['__encryption_version__'] != 1
                    or not isinstance(encrypted['__encrypted_data__'], str)):
                raise ValueError('oauth_bundle_encrypted_managed_entry_required')
            for field in ('created_at', 'expires_at'):
                if field in value and (not isinstance(value[field], str)
                        or datetime.fromisoformat(value[field]).tzinfo is None):
                    raise ValueError('oauth_bundle_absolute_ttl_required')
    # A populated collection without its index/absolute-path metadata cannot be restored safely.
    for collection in {path.split('/')[0] for path in files if '/' in path}:
        if collection + '-info.json' not in files:
            raise ValueError('oauth_bundle_collection_metadata_missing')
    return sorted(artifacts)


def _write(path, raw):
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def _sync_tree(root):
    for parent, _, _ in os.walk(str(root), topdown=False):
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(str(root.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def export_bundle(document, destination, *, lease=None, allow_loopback_for_tests=False):
    config, bound = binding(document, allow_loopback_for_tests)
    root = safe_path(config['state']['directory'])
    target = safe_path(destination)
    if target.exists() or root == target or root in target.parents:
        raise ValueError('oauth_bundle_new_external_destination_required')
    if lease is not None and not isinstance(lease, FreezeLease):
        raise ValueError('oauth_bundle_observed_freeze_lease_required')
    references = key_references(config)
    evidence = lease.evidence(root, bound, references) if lease is not None else {'kind': 'unfenced_stable_file_copy',
        'asgi_active': None, 'sdk_active': None, 'cross_process_writer_fenced': False, 'activation_authorized': False}
    files, directories, signatures = _scan(root)
    artifacts = _layout(files, directories, str(root))
    again, again_dirs, again_signatures = _scan(root)
    if files != again or directories != again_dirs or signatures != again_signatures:
        raise ValueError('oauth_bundle_source_changed')
    if lease is not None:
        lease.evidence(root, bound, key_references(config))  # Real counters/bindings/keys still sealed after I/O.
    value = {'format': FORMAT, 'created_at': time.time(), 'binding': bound,
        'binding_sha256': sha(canonical(bound).encode()), 'source_root': str(root),
        'required_versions': VERSIONS, 'reviewed_sdk_sha256': SDK_PINS, 'key_references': references,
        'collections': list(COLLECTIONS), 'directories': directories, 'unknown_sdk_artifacts': artifacts,
        'files': {name: {'size': len(raw), 'sha256': sha(raw), 'base64': base64.b64encode(raw).decode('ascii')}
                  for name, raw in sorted(files.items())},
        'source_stability': {'full_passes': 2, 'signature_fields': ['dev', 'ino', 'size', 'mtime_ns', 'ctime_ns', 'mode'],
                             'tree_sha256': sha(canonical({key: sha(raw) for key, raw in sorted(files.items())}).encode())},
        'writer_observation': evidence, 'activation_authorized': False,
        'external_restore_dependencies': ['original_owner_bound_ingress_and_conversation_session_services',
            'unchanged_issuer_resource_client_scope_redirect_bindings_and_tls_routes',
            'three_dedicated_service_key_references', 'trusted_exclusion_of_all_writers_and_restart_paths'],
        'remaining_fence_dependency': 'trusted_exclusion_of_every_original_writer_and_restart_path'}
    value['sha256'] = sha(canonical(value).encode())
    target.mkdir(mode=0o700)
    _write(target / 'oauth-state-bundle.json', (canonical(value) + '\n').encode('utf8'))
    _sync_tree(target)
    return value


def verify_bundle(bundle_path, expected_document, *, key_paths=None, expected_sha256=None, allow_loopback_for_tests=False):
    value = decode(_read(bundle_path, 2 * MAX_TOTAL))
    if not isinstance(value, dict):
        raise ValueError('oauth_bundle_envelope_invalid')
    expected_hash = value.get('sha256')
    unsigned = {key: item for key, item in value.items() if key != 'sha256'}
    _, expected = binding(expected_document, allow_loopback_for_tests)
    if (value.get('format') != FORMAT or sha(canonical(unsigned).encode()) != expected_hash
            or value.get('binding') != expected or value.get('binding_sha256') != sha(canonical(expected).encode())
            or value.get('required_versions') != VERSIONS or value.get('reviewed_sdk_sha256') != SDK_PINS
            or value.get('collections') != list(COLLECTIONS) or value.get('activation_authorized') is not False
            or set(value.get('key_references', {})) != set(KEY_ROLES)
            or (expected_sha256 is not None and value['sha256'] != expected_sha256)):
        raise ValueError('oauth_bundle_integrity_or_binding_conflict')
    files, total = {}, 0
    if not isinstance(value['directories'], list) or len(set(value['directories'])) != len(value['directories']):
        raise ValueError('oauth_bundle_directories_invalid')
    if len(value['files']) > 10000 or len(value['directories']) > 1000:
        raise ValueError('oauth_bundle_state_exceeds_budget')
    for name, entry in value['files'].items():
        _relative(name)
        if (not isinstance(entry, dict) or set(entry) != {'size', 'sha256', 'base64'}
                or type(entry['size']) is not int or not 0 <= entry['size'] <= MAX_FILE
                or not isinstance(entry['base64'], str) or len(entry['base64']) > 4 * ((MAX_FILE + 2) // 3)):
            raise ValueError('oauth_bundle_file_entry_invalid')
        raw = base64.b64decode(entry['base64'], validate=True)
        total += len(raw)
        if (len(raw) != entry['size'] or sha(raw) != entry['sha256'] or total > MAX_TOTAL
                or base64.b64encode(raw).decode('ascii') != entry['base64']):
            raise ValueError('oauth_bundle_file_content_conflict')
        files[name] = raw
    artifacts = _layout(files, value['directories'], value['source_root'])
    if artifacts != value['unknown_sdk_artifacts']:
        raise ValueError('oauth_bundle_artifacts_changed')
    tree_hash = sha(canonical({key: sha(raw) for key, raw in sorted(files.items())}).encode())
    if value['source_stability'].get('tree_sha256') != tree_hash:
        raise ValueError('oauth_bundle_tree_inventory_conflict')
    for reference in value['key_references'].values():
        if (not isinstance(reference, dict)
                or set(reference) != {'source_reference', 'sha256', 'size', 'scope'}
                or reference['scope'] != 'dedicated_mesh_oauth_service_only'
                or not isinstance(reference['sha256'], str) or len(reference['sha256']) != 64
                or any(character not in '0123456789abcdef' for character in reference['sha256'])
                or type(reference['size']) is not int or not 32 <= reference['size'] <= 4096):
            raise ValueError('oauth_bundle_service_key_reference_invalid')
        source_reference = reference['source_reference']
        if (not isinstance(source_reference, str) or not Path(source_reference).is_absolute()
                or '..' in Path(source_reference).parts):
            raise ValueError('oauth_bundle_service_key_reference_invalid')
    verified_keys = None
    if key_paths is not None:
        if set(key_paths) != set(KEY_ROLES):
            raise ValueError('oauth_bundle_all_service_key_roles_required')
        verified_keys = {}
        for role, filename in key_paths.items():
            raw = _read(filename, 4096)
            reference = value['key_references'][role]
            if (reference.get('scope') != 'dedicated_mesh_oauth_service_only'
                    or sha(raw) != reference['sha256'] or len(raw) != reference['size']):
                raise ValueError('oauth_bundle_service_key_mismatch')
            verified_keys[role] = raw
        from cryptography.fernet import Fernet
        fernet = Fernet(verified_keys['mesh_oauth_state_fernet'].strip())
        for name, raw in files.items():
            if '/' in name and name.endswith('.json'):
                encrypted = decode(raw)['value']['__encrypted_data__']
                if not isinstance(decode(fernet.decrypt(base64.b64decode(encrypted, validate=True))), dict):
                    raise ValueError('oauth_bundle_decrypted_record_invalid')
    return value, files, verified_keys is not None


def stage_bundle(bundle_path, destination, expected_document, *, key_paths=None, expected_sha256=None, allow_loopback_for_tests=False):
    target = safe_path(destination)
    if target.exists():
        raise ValueError('oauth_bundle_new_private_staging_required')
    value, files, keys_verified = verify_bundle(bundle_path, expected_document, key_paths=key_paths,
                                               expected_sha256=expected_sha256,
                                               allow_loopback_for_tests=allow_loopback_for_tests)
    target.mkdir(mode=0o700)
    state = target / 'state'; state.mkdir(mode=0o700)
    _write(state / STAGING_MARKER, canonical({'format': FORMAT, 'bundle_sha256': value['sha256'],
                                            'activation_authorized': False}).encode())
    _sync_tree(target)  # A failed/interrupted staging copy must remain startup-blocked too.
    for relative in sorted(value['directories'], key=lambda item: (len(PurePosixPath(item).parts), item)):
        (state / relative).mkdir(mode=0o700)
    relocations, derived = {}, {}
    for name, raw in files.items():
        if '/' not in name and name.endswith('-info.json'):
            metadata = decode(raw)
            metadata['directory'] = str(state / COLLECTION_DIRECTORIES[metadata['collection']])
            raw = canonical(metadata).encode('utf8')
            relocations[name] = {'field': 'directory', 'source_sha256': value['files'][name]['sha256'],
                                 'target_sha256': sha(raw)}
        _write(state / name, raw)
        derived[name] = sha(raw)
    # Preserve every original source byte inside the verified self-contained
    # bundle, alongside the explicitly derived SDK path-relocated view.
    _write(target / 'source-bundle.json', (canonical(value) + '\n').encode('utf8'))
    report = {'format': FORMAT, 'bundle_sha256': value['sha256'], 'state_directory': str(state),
        'key_dependencies_verified': keys_verified, 'metadata_relocations': relocations,
        'source_bundle_pinned': expected_sha256 is not None,
        'state_sha256': derived, 'activation_authorized': False, 'writer_fence_verified': False,
        'scopes_or_issuer_rewritten': False, 'ttl_rebased': False}
    _write(target / 'staging.json', (canonical(report) + '\n').encode('utf8'))
    _sync_tree(target)
    return report


def activate_staging(*args, **kwargs):
    # An in-process drain or a caller's boolean/JSON receipt is not fencing of
    # other processes, machines, old code or its restart path. S2 owns that.
    raise ValueError('oauth_bundle_trusted_original_writer_fence_required_staging_only')
