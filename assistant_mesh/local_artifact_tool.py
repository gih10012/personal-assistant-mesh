"""Fixed owner-only original-artifact reader and independent private CLI bundle.

Stage writes only a new candidate. Inspect reads complete original bytes; consume
writes only the fixed candidate cache. No credential copy, RPC, Store/provider
constructor, original business request, service change or journal migration.
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import sys
import uuid
import zipfile
from contextlib import closing
from pathlib import Path

from .artifact_transport import (ArtifactProducer, LocalArtifactProducer, MAX_ARTIFACT_BYTES,
    _decode, _digest, _directory_unchanged, _private_path, _readonly)
from .egress_adapter import _root
from .provider_runtime import _owner_bytes
from .workspace import MeshWorkspace, _existing_journal
from .workspace_cli import _private_json


SOURCE_FORMAT = 'mesh-local-artifact-source/1'
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class LocalArtifactToolError(ValueError):
    """Fixed, redacted diagnostics only."""


def _check(value):
    if not value:
        raise LocalArtifactToolError('local_artifact_unavailable_or_invalid')


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                      allow_nan=False).encode('utf8')


def _reference(value):
    from .resources import _name
    _check(isinstance(value, dict) and set(value) == {'task_id', 'operation_id', 'capability_id'})
    return {key: _name(item, key) for key, item in value.items()}


class _NoRPC:
    def request(self, *args, **kwargs):
        raise LocalArtifactToolError('local_artifact_rpc_not_available')


class LocalArtifactSource(LocalArtifactProducer):
    """Owner-pinned source recipe, with explicit local allocation provenance.

    An old A2A producer config remains untouched. Its already-pinned source DB,
    provider bindings and adapter files are reused, while the ORIGINAL thin-call
    journal supplies the actual local node. No message ID is invented/reused.
    """

    def _spec(self):
        value = _private_json(self.path, 65536, self.sha)
        _check(set(value) == {'format', 'owner_config_path', 'owner_config_sha256', 'journal_path'}
               and value['format'] == SOURCE_FORMAT)
        _existing_journal(value['journal_path'])
        return value

    def _config(self):
        spec = self._spec()
        # Reuse the original producer's private-file/pin contract. A dotfiles
        # Git root above an existing private source is not permission to copy
        # it, nor a reason to move/rewrite the owner's production config. The
        # NEW recipe/candidate still use the outside-worktree policy.
        raw = _decode(_owner_bytes(spec['owner_config_path'], 65536,
                                  'local_artifact_owner_source_unavailable'), 65536)
        producer_type = ArtifactProducer if 'sender' in raw else LocalArtifactProducer
        producer = producer_type(spec['owner_config_path'], spec['owner_config_sha256'])
        config = producer._config()
        with closing(sqlite3.connect(Path(spec['journal_path']).as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            identities = db.execute('SELECT authority,node FROM call_identity').fetchmany(2)
        _check(len(identities) == 1 and identities[0][0] == config['authority'])
        node = identities[0][1]
        if 'node' in config:
            _check(config['node'] == node)
        else:
            config = dict(config)
            config.pop('sender')
            config['node'] = node
        return config

    def workspace(self, task_id, artifact_dir=None):
        config, spec = self._config(), self._spec()
        with _readonly(config['authority_db'], ('tasks',)) as db:
            rows = db.execute('SELECT epoch FROM tasks WHERE id=?', (task_id,)).fetchmany(2)
        _check(len(rows) == 1)
        return MeshWorkspace(_NoRPC(), spec['journal_path'], config['authority'], config['node'],
            task_id, rows[0]['epoch'], artifact_dir, {config['authority']: self}, read_only=True)

    def _prepare(self, task_id, operation_id, capability_id):
        original, request, unused_stages = self.workspace(task_id)._existing_local_call(operation_id)
        selection = request['plan']['selections'][0]
        _check(selection['capability_id'] == capability_id)
        manifest, raw = super()._prepare(task_id, operation_id, capability_id)
        _check(manifest['task_epoch'] == original['task_epoch']
               and manifest['allocation_plan_sha256'] == _digest(request['plan'])
               and manifest['capability_epoch'] == selection['epoch']
               and manifest['scope_sha256'] == _digest(selection['scope'])
               and manifest['workload_sha256'] == _digest(selection['workload']))
        return manifest, raw

    def read(self, task_id, operation_id, capability_id):
        return self._prepare(task_id, operation_id, capability_id)[1]


def _journal_snapshot(path):
    """One RO transaction: entire schema and all table rows, not only counts."""
    path, identity = _private_path(path)
    total, tables = 0, {}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        for kind, name, unused_table, unused_sql in schema:
            if kind != 'table':
                continue
            escaped = '"' + name.replace('"', '""') + '"'
            columns = db.execute('PRAGMA table_info(' + escaped + ')').fetchall()
            order = ','.join('"' + item[1].replace('"', '""') + '"' for item in columns)
            digest, count = hashlib.sha256(), 0
            for row in db.execute('SELECT * FROM ' + escaped + (' ORDER BY ' + order if order else '')):
                encoded = _encoded(list(row))
                total += len(encoded)
                _check(total <= MAX_SNAPSHOT_BYTES)
                digest.update(str(len(encoded)).encode('ascii') + b':' + encoded)
                count += 1
            tables[name] = {'rows': count, 'rows_sha256': digest.hexdigest(),
                            'columns_sha256': hashlib.sha256(_encoded(columns)).hexdigest()}
        db.execute('ROLLBACK')
    _check(_private_path(path)[1] == identity)
    return {'file_identity': list(identity), 'schema_sha256': hashlib.sha256(_encoded(schema)).hexdigest(),
            'tables': tables}


def _snapshots(source):
    config, spec = source._config(), source._spec()
    paths = sorted({binding['provider_journal'] for binding in config['bindings'].values()})
    return {'call_journal': _journal_snapshot(spec['journal_path']),
        'provider_journals': {hashlib.sha256(path.encode('utf8')).hexdigest(): _journal_snapshot(path)
                              for path in paths}}


def inspect_source(source, reference):
    reference = _reference(reference)
    before = _snapshots(source)
    manifest, raw = source._prepare(**reference)
    original, unused_request, unused_stages = source.workspace(reference['task_id'])._existing_local_call(
        reference['operation_id'])
    after = _snapshots(source)
    return {'format': 'mesh-local-artifact-inspection/1', 'manifest': manifest,
        'source_complete_bytes_verified': len(raw) == manifest['artifact_bytes'],
        'original_call_sha256': original['fingerprint'],
        'original_call_row_sha256': hashlib.sha256(_encoded(original)).hexdigest(),
        'source_snapshots_before': before, 'source_snapshots_after': after,
        'source_journals_unchanged_observed': before == after,
        'source_snapshot_scope': 'complete call/provider journal schema and rows; not unrelated live authority/history',
        'destination_content_verified': False, 'native_leader_consumption_verified': False,
        'original_operation_replayed': False, 'execution_verified': False}


def consume(source, reference, artifact_dir):
    reference = _reference(reference)
    # Validate capability as well as task+operation before the Workspace's
    # original read. Output path/cache are fixed by the installed CLI.
    before = _snapshots(source)
    source._prepare(**reference)
    report = source.workspace(reference['task_id'], str(_root(artifact_dir))).artifact(reference['operation_id'])
    _check(report.get('phase') == 'artifact_verified')
    after = _snapshots(source)
    return dict(report, source_snapshots_before=before, source_snapshots_after=after,
                source_journals_unchanged_observed=before == after,
                native_leader_consumption_verified=False)


def _publish(root, name, raw):
    """Publish a new private regular file atomically, never overwrite a target."""
    parent = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary, created = '.local-artifact-' + uuid.uuid4().hex, False
    try:
        try:
            existing = _owner_bytes(root / name, len(raw) + 1, 'local_artifact_candidate_invalid')
        except FileNotFoundError:
            existing = None
        except Exception:
            # _owner_bytes redacts a missing path; distinguish with safe lstat.
            try:
                (root / name).lstat()
            except FileNotFoundError:
                existing = None
            else:
                raise
        if existing is not None:
            _check(existing == raw)
            return
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        created = True
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent)
        created = False
        os.fsync(parent)
        _directory_unchanged(root, parent)
        _check(_owner_bytes(root / name, len(raw) + 1, 'local_artifact_candidate_invalid') == raw)
    finally:
        if created:
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


def stage(spec_path, spec_sha256, candidate_dir, python_executable):
    """Generate only a separately reviewed candidate; no system install/activation."""
    LocalArtifactSource(spec_path, spec_sha256)
    root = _root(candidate_dir)
    _check(Path(python_executable).is_absolute() and os.access(python_executable, os.X_OK))
    package = Path(__file__).parent
    files = {}
    for path in sorted(package.glob('*.py')):
        info = path.lstat()
        _check(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid())
               and not info.st_mode & 0o022 and info.st_nlink == 1 and info.st_size <= 1048576)
        raw, after = path.read_bytes(), path.lstat()
        _check((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
               (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
               and len(raw) == info.st_size)
        files['assistant_mesh/' + path.name] = raw
    _check('assistant_mesh/__init__.py' in files and 'assistant_mesh/local_artifact_tool.py' in files)
    license_path = package.parent / 'LICENSE'
    license_info = license_path.lstat()
    _check(stat.S_ISREG(license_info.st_mode) and not license_info.st_mode & 0o022
           and license_info.st_nlink == 1 and license_info.st_size <= 65536)
    files['LICENSE'] = license_path.read_bytes()
    files['__main__.py'] = ('from assistant_mesh.local_artifact_tool import tool_main\n'
        'raise SystemExit(tool_main(' + repr(str(Path(spec_path))) + ',' + repr(spec_sha256) + ','
        + repr(str(root / 'artifacts')) + '))\n').encode('utf8')
    source_hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(files.items())}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, raw)
    body = buffer.getvalue()
    artifact_root = root / 'artifacts'
    try:
        artifact_root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _root(artifact_root)
    command = [str(python_executable), '-I', str(root / 'local-artifact.pyz')]
    binding = {'reference_kind': 'local_task', 'manifest_argv': command + ['manifest', '{request_base64url}'],
               'read_argv': command + ['read', '{request_base64url}'], 'timeout_seconds': 30}
    marker = {'format': 'mesh-local-artifact-candidate/1', 'spec_sha256': spec_sha256,
        'package_sha256': hashlib.sha256(body).hexdigest(), 'source_sha256': source_hashes,
        'source_tree_sha256': hashlib.sha256(_encoded(source_hashes)).hexdigest(),
        'reader_binding_sha256': hashlib.sha256(_encoded(binding)).hexdigest(),
        'production_installed': False, 'native_leader_consumption_verified': False,
        'source_journal_write_requested': False, 'native_features_restricted': False}
    _publish(root, 'local-artifact.pyz', body)
    _publish(root, 'reader-binding.json', _encoded(binding))
    _publish(root, 'READY.json', _encoded(marker))
    return dict(marker, package_path=str(root / 'local-artifact.pyz'),
                reader_binding_path=str(root / 'reader-binding.json'))


def _error():
    return {'status': 'error', 'diagnostics': ['local_artifact_unavailable_or_invalid'],
            'original_operation_replayed': False, 'execution_verified': False}


def _token(token):
    _check(isinstance(token, str) and len(token) <= 8192 and re.fullmatch(r'[A-Za-z0-9_-]+', token) is not None)
    return _reference(_decode(base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True), 6144))


def tool_main(spec_path, spec_sha256, artifact_dir, argv=None):
    """Fixed package entrypoint: only original IDs, never source/config/paths."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        _check(bool(args) and args[0] in ('manifest', 'read', 'inspect', 'consume'))
        if args[0] in ('manifest', 'read'):
            _check(len(args) == 2)
            reference = _token(args[1])
        else:
            _check(len(args) == 7 and args[1::2] == ['--task-id', '--operation-id', '--capability-id'])
            reference = _reference(dict(zip(('task_id', 'operation_id', 'capability_id'), args[2::2])))
        source = LocalArtifactSource(spec_path, spec_sha256)
        if args[0] == 'read':
            source.write_to(**reference, binary_stream=sys.stdout.buffer)
            return 0
        if args[0] == 'manifest':
            value = source.manifest(**reference)
        elif args[0] == 'inspect':
            value = inspect_source(source, reference)
        else:
            value = consume(source, reference, artifact_dir)
    except Exception:
        if args and args[0] == 'read':
            return 2  # Binary stdout must never contain an error JSON/log.
        value = _error()
    print(json.dumps(value, sort_keys=True, ensure_ascii=False), flush=True)
    return 2 if value.get('status') == 'error' else 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps(_error()), flush=True)
        self.exit(2)


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('action', choices=('stage',))
    parser.add_argument('--spec', required=True)
    parser.add_argument('--spec-sha256', required=True)
    parser.add_argument('--candidate-dir', required=True)
    parser.add_argument('--python-executable', required=True)
    args = parser.parse_args(argv)
    try:
        report = stage(args.spec, args.spec_sha256, args.candidate_dir, args.python_executable)
    except Exception:
        report = _error()
    print(json.dumps({key: value for key, value in report.items() if key != 'source_sha256'},
                     sort_keys=True, ensure_ascii=False), flush=True)
    return 2 if report.get('status') == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
