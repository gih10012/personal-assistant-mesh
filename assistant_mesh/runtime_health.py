"""Advisory, node-local package layout observations, never a native-tool gate.

Only the canonical codex-package.json is read. Executables, credentials,
configuration referenced by a wrapper, and session history are never read or
executed. A complete layout is not an integrity, inference or Shell proof.
"""
import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath


MAX_MANIFEST_BYTES = 64 * 1024
UNKNOWN_CODES = frozenset(('runtime_entrypoint_indirect', 'runtime_manifest_invalid',
                          'runtime_manifest_untrusted', 'runtime_manifest_changed',
                          'runtime_canonical_layout_not_discovered'))


class UnknownLayout(ValueError):
    """Internal fixed classifications; never include paths or file contents."""


def _report(harness, code='runtime_layout_unknown'):
    return {'harness': harness, 'layout': 'unknown', 'code': code,
            'package_provenance': 'not_verified', 'execution_verified': False,
            'model_availability': 'not_checked', 'network_availability': 'not_checked'}


def _direct(path):
    if not path.is_absolute() or '..' in path.parts:
        raise UnknownLayout('runtime_entrypoint_indirect')
    for candidate in (path,) + tuple(path.parents):
        if candidate.is_symlink():
            raise UnknownLayout('runtime_entrypoint_indirect')
    return path


def _relative(root, value):
    if (not isinstance(value, str) or not value or '\\' in value
            or any(ord(char) < 32 for char in value)):
        raise UnknownLayout('runtime_manifest_invalid')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or str(path) == '.':
        raise UnknownLayout('runtime_manifest_invalid')
    return _direct(root / str(path))


def _read_manifest(path):
    _direct(path)
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid not in (0, os.getuid()) or before.st_mode & 0o022
            or before.st_size > MAX_MANIFEST_BYTES):
        raise UnknownLayout('runtime_manifest_untrusted')
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        current = os.fstat(handle.fileno())
        if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
            raise UnknownLayout('runtime_manifest_changed')
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
        after = os.fstat(handle.fileno())
        if ((after.st_size, after.st_mtime_ns) != (current.st_size, current.st_mtime_ns)
                or len(raw) > MAX_MANIFEST_BYTES):
            raise UnknownLayout('runtime_manifest_changed')
    value = json.loads(raw.decode('utf8'))
    if not isinstance(value, dict):
        raise UnknownLayout('runtime_manifest_invalid')
    return value


def _component(path, directory=False):
    _direct(path)
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return 'missing'
    if directory:
        return 'present' if stat.S_ISDIR(observed.st_mode) else 'missing'
    if not stat.S_ISREG(observed.st_mode):
        return 'missing'
    return 'present' if observed.st_mode & 0o111 else 'not_executable'


def _codex(config):
    report = _report('codex')
    if not isinstance(config, dict):
        report['code'] = 'runtime_configuration_invalid'
        return report
    configured = config.get('executable', 'codex')
    if (not isinstance(configured, str) or not configured
            or any(ord(char) < 32 for char in configured)):
        report['code'] = 'runtime_configuration_invalid'
        return report
    selected = Path(configured)
    if not selected.is_absolute():
        # PATH lookup observes metadata only; wrapper code is never inspected.
        if len(selected.parts) != 1:
            report['code'] = 'runtime_entrypoint_indirect'
            return report
        located = shutil.which(configured)
        if not located:
            report['code'] = 'runtime_manifest_not_found'
            return report
        selected = Path(located)
    _direct(selected)
    if selected.parent.name != 'bin' or selected.name not in ('codex', 'codex.exe'):
        report['code'] = 'runtime_canonical_layout_not_discovered'
        return report
    root = selected.parent.parent
    manifest_path = root / 'codex-package.json'
    if not manifest_path.exists():
        # Standalone binaries and custom wrappers can be valid. Don't label
        # them broken just because this optional observer lacks their contract.
        report['code'] = 'runtime_manifest_not_found'
        return report
    manifest = _read_manifest(manifest_path)
    version, target = manifest.get('version'), manifest.get('target')
    if (manifest.get('layoutVersion') != 1 or manifest.get('variant') != 'codex'
            or not isinstance(version, str) or len(version) > 80
            or re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9._-]+)?', version) is None
            or not isinstance(target, str) or len(target) > 100
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', target) is None):
        raise UnknownLayout('runtime_manifest_invalid')
    if manifest.get('entrypoint') != 'bin/' + selected.name:
        raise UnknownLayout('runtime_canonical_layout_not_discovered')
    for field, expected in (('resourcesDir', 'codex-resources'), ('pathDir', 'codex-path')):
        if field in manifest and manifest[field] != expected:
            raise UnknownLayout('runtime_canonical_layout_not_discovered')
    entrypoint = _relative(root, manifest.get('entrypoint'))
    if entrypoint != selected:
        raise UnknownLayout('runtime_canonical_layout_not_discovered')
    helper = selected.with_name('codex-code-mode-host.exe' if selected.suffix == '.exe'
                                else 'codex-code-mode-host')
    components = {'entrypoint': _component(selected), 'code_mode_host': _component(helper)}
    for field, label in (('resourcesDir', 'resources'), ('pathDir', 'path')):
        components[label] = (_component(_relative(root, manifest[field]), directory=True)
                             if field in manifest else 'missing')
    missing = [label for label in ('entrypoint', 'code_mode_host', 'resources', 'path')
               if components[label] != 'present']
    report.update(layout='missing' if missing else 'complete',
                  code='runtime_required_components_missing' if missing else 'runtime_layout_present',
                  manifest={'version': version, 'target': target, 'layout_version': 1},
                  components=components, missing=missing)
    return report


def diagnose(worker_config):
    """Observe this trusted host configuration, not model-supplied paths.

    No subprocess, network, authentication, registry mutation or task mutation.
    Missing/unreadable/unknown packages never prevent native runtime use.
    """
    result = {'schema': 'runtime-health/1', 'read_only': True,
              'observation': 'configured_layout_only', 'active_process_verified': False,
              'native_tools_intercepted': False, 'inference_started': False,
              'network_accessed': False, 'automatic_repair': False,
              'task_replayed': False, 'session_replaced': False, 'runtimes': []}
    if not isinstance(worker_config, dict):
        result['error'] = 'runtime_configuration_invalid'
        return result
    if 'codex' in worker_config:
        try:
            codex = _codex(worker_config['codex'])
        except UnknownLayout as exc:
            code = exc.args[0] if exc.args else None
            codex = _report('codex', code if isinstance(code, str) and code in UNKNOWN_CODES
                            else 'runtime_diagnosis_unavailable')
        except (OSError, ValueError, TypeError):
            codex = _report('codex', 'runtime_layout_unreadable')
        except Exception:
            codex = _report('codex', 'runtime_diagnosis_unavailable')
        result['runtimes'].append(codex)
    if 'pi' in worker_config:
        result['runtimes'].append(_report('pi', 'runtime_layout_contract_not_available'))
    if not result['runtimes']:
        result['error'] = 'runtime_configuration_not_available'
    return result
