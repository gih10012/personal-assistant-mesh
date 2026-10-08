"""Explicit deployment migration after owner's Codex-first/full-host request.

Backs up private configuration once, never prints its contents or tokens.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path
from pathlib import PurePosixPath

from assistant_mesh.config import private_json


def _digest(handle):
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()


def _checked_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path.is_symlink():
        raise ValueError('runtime_requires_absolute_non_symlink_path')
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('runtime_requires_absolute_non_symlink_path')
    return path


def _member_path(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name or '\0' in name:
        raise ValueError('runtime_archive_unsafe_path')
    return path


def _members(archive):
    members = {}
    for member in archive.getmembers():
        path = _member_path(member.name)
        if str(path) == '.' and member.isdir():
            continue
        if str(path) in members or not (member.isfile() or member.isdir()) or member.mode & 0o022:
            raise ValueError('runtime_archive_unsafe_member')
        members[str(path)] = member
    if not members:
        raise ValueError('runtime_archive_empty')
    return members


def _package(payload, version=None, target=None):
    manifests = list(payload.rglob('codex-package.json'))
    if len(manifests) != 1:
        raise ValueError('runtime_complete_package_required')
    manifest_path = manifests[0]
    with manifest_path.open(encoding='utf8') as handle:
        manifest = json.load(handle)
    if (not isinstance(manifest, dict) or manifest.get('layoutVersion') != 1 or manifest.get('variant') != 'codex'
            or not isinstance(manifest.get('version'), str) or not isinstance(manifest.get('target'), str)):
        raise ValueError('runtime_package_manifest_invalid')
    if version is not None and manifest['version'] != version:
        raise ValueError('runtime_package_version_mismatch')
    if target is not None and manifest['target'] != target:
        raise ValueError('runtime_package_target_mismatch')
    root = manifest_path.parent
    # Native discovery resolves these canonical siblings from bin/codex. A
    # manifest pointing at an arbitrary complete-looking tree is not a package
    # that the official executable can discover by itself.
    if (manifest.get('entrypoint') not in ('bin/codex', 'bin/codex.exe')
            or manifest.get('resourcesDir') != 'codex-resources'
            or manifest.get('pathDir') != 'codex-path'):
        raise ValueError('runtime_package_canonical_layout_required')
    paths = {}
    for key in ('entrypoint', 'resourcesDir', 'pathDir'):
        if not isinstance(manifest.get(key), str) or str(_member_path(manifest[key])) == '.':
            raise ValueError('runtime_package_manifest_invalid')
        paths[key] = root / str(_member_path(manifest[key]))
    executable = paths['entrypoint']
    helper = executable.parent / ('codex-code-mode-host.exe' if executable.suffix == '.exe' else 'codex-code-mode-host')
    if (not executable.is_file() or not helper.is_file()
            or not executable.stat().st_mode & 0o111 or not helper.stat().st_mode & 0o111
            or not paths['resourcesDir'].is_dir() or not paths['pathDir'].is_dir()):
        raise ValueError('runtime_complete_package_required')
    return {'version': manifest['version'], 'target': manifest['target'], 'executable': str(executable),
            'complete_package_verified': True}


def verify_installation(package_dir, sha256, version=None, target=None):
    """Maintenance-only complete-artifact verification; never a turn/tool gate.

    No receipt is trusted as an integrity claim: verify both the retained whole
    artifact and every extracted file against that artifact on every switch.
    """
    root = _checked_path(package_dir)
    if not isinstance(sha256, str) or re.fullmatch(r'[0-9a-f]{64}', sha256) is None:
        raise ValueError('runtime_artifact_sha256_required')
    artifact, payload = root / 'artifact.tar.gz', root / 'payload'
    if not root.is_dir() or artifact.is_symlink() or not artifact.is_file() or payload.is_symlink() or not payload.is_dir():
        raise ValueError('runtime_complete_package_required')
    with artifact.open('rb') as handle:
        if _digest(handle) != sha256:
            raise ValueError('runtime_integrity_mismatch')
        handle.seek(0)
        with tarfile.open(fileobj=handle, mode='r:gz') as archive:
            members = _members(archive)
            files = {name for name, member in members.items() if member.isfile()}
            actual_files = set()
            for path in payload.rglob('*'):
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise ValueError('runtime_package_tree_mismatch')
                if path.is_dir() and path.stat().st_mode & 0o022:
                    raise ValueError('runtime_package_tree_mismatch')
                if path.is_file():
                    actual_files.add(path.relative_to(payload).as_posix())
            if actual_files != files:
                raise ValueError('runtime_package_tree_mismatch')
            for name, member in members.items():
                path = payload / name
                if member.isdir():
                    if (not path.is_dir()
                            or stat.S_IMODE(path.stat().st_mode) != member.mode & 0o777):
                        raise ValueError('runtime_package_tree_mismatch')
                    continue
                if path.stat().st_size != member.size or stat.S_IMODE(path.stat().st_mode) != member.mode & 0o777:
                    raise ValueError('runtime_package_tree_mismatch')
                with archive.extractfile(member) as expected, path.open('rb') as actual:
                    if _digest(expected) != _digest(actual):
                        raise ValueError('runtime_package_tree_mismatch')
    result = _package(payload, version, target)
    result.update(package_dir=str(root), artifact_sha256=sha256)
    return result


def install_package(artifact, sha256, version, target, install_dir):
    """Install one owner-verified official package, keeping its full layout.

    The caller supplies the complete artifact SHA256 from its official release,
    not the standalone codex binary hash. No latest/model selection occurs.
    """
    if (not isinstance(version, str) or re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9._-]+)?', version) is None
            or not isinstance(target, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', target) is None
            or not isinstance(sha256, str) or re.fullmatch(r'[0-9a-f]{64}', sha256) is None):
        raise ValueError('runtime_explicit_release_identity_required')
    artifact, root = _checked_path(artifact), _checked_path(install_dir)
    if not artifact.is_file():
        raise ValueError('runtime_artifact_must_be_regular_file')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o022:
        raise ValueError('runtime_install_directory_must_be_owned')
    destination = root / ('codex-' + version + '-' + target + '-' + sha256[:12])
    if destination.exists() or destination.is_symlink():
        result = verify_installation(destination, sha256, version, target)
        result['already_installed'] = True
        return result
    with tempfile.TemporaryDirectory(prefix='.codex-install-', dir=str(root)) as temporary:
        stage = Path(temporary)
        # Hash the retained copy, then extract it, never a second read of a
        # potentially changed source or a mixture of separately fetched files.
        shutil.copyfile(str(artifact), str(stage / 'artifact.tar.gz'))
        (stage / 'artifact.tar.gz').chmod(0o600)
        with (stage / 'artifact.tar.gz').open('rb') as handle:
            if _digest(handle) != sha256:
                raise ValueError('runtime_integrity_mismatch')
            handle.seek(0)
            with tarfile.open(fileobj=handle, mode='r:gz') as archive:
                members = _members(archive)
                payload = stage / 'payload'
                payload.mkdir(mode=0o700)
                for name, member in members.items():
                    path = payload / name
                    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    if member.isdir():
                        path.mkdir(mode=0o755, exist_ok=True)
                    else:
                        with archive.extractfile(member) as source, path.open('xb') as output:
                            shutil.copyfileobj(source, output)
                        path.chmod(member.mode & 0o777)
                # Apply archived directory modes only after writing children;
                # preserve the release tree instead of silently normalizing it.
                for name, member in sorted(members.items(), key=lambda item: len(PurePosixPath(item[0]).parts), reverse=True):
                    if member.isdir():
                        (payload / name).chmod(member.mode & 0o777)
        verify_installation(stage, sha256, version, target)
        # Exclusive reservation never overwrites a previous release. A partial
        # failed publication is unused until a later explicit admin verification.
        destination.mkdir(mode=0o700)
        os.rename(str(stage / 'payload'), str(destination / 'payload'))
        os.rename(str(stage / 'artifact.tar.gz'), str(destination / 'artifact.tar.gz'))
    result = verify_installation(destination, sha256, version, target)
    result['already_installed'] = False
    return result


def update(path, transform):
    path = Path(path)
    value = private_json(path)
    backup = path.with_suffix(path.suffix + '.before-model-led')
    if not backup.exists():
        shutil.copy2(str(path), str(backup))
    transform(value)
    # Administrative config migration, not public source generation.
    with path.open('w', encoding='utf8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    path.chmod(0o600)


def worker(value):
    value['capabilities'] = sorted(set(value.get('capabilities', [])) | {'agent', 'leader', 'codex.native'})
    value['codex'].update(sandbox='danger-full-access', approval_policy='never', model_policy='catalog-first')
    value['turn_timeout'] = 3600


def server(value):
    for peer in value['peers']:
        if peer.get('node'):
            peer['capabilities'] = sorted(set(peer.get('capabilities', [])) | {'agent', 'codex.native'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state')
    parser.add_argument('--artifact', help='local complete official codex-package tar.gz, not standalone codex')
    parser.add_argument('--sha256', help='SHA256 of the complete official artifact')
    parser.add_argument('--version')
    parser.add_argument('--target')
    parser.add_argument('--install-dir')
    args = parser.parse_args()
    if args.artifact:
        if args.state or not all((args.sha256, args.version, args.target, args.install_dir)):
            parser.error('package install requires artifact, sha256, version, target, install-dir; no state migration')
        print(json.dumps(install_package(args.artifact, args.sha256, args.version, args.target, args.install_dir)))
        return
    if not args.state or any((args.sha256, args.version, args.target, args.install_dir)):
        parser.error('configuration migration requires only --state')
    root = Path(args.state)
    for name in ('laptop-worker.json', 'cloud-worker.json'):
        if (root / name).exists():
            update(root / name, worker)
    if (root / 'server.json').exists():
        update(root / 'server.json', server)
    print(json.dumps({'configuration_upgraded': True, 'private_backup_created': True}))


if __name__ == '__main__':
    main()
