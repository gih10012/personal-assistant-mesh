"""Owner-installed, read-only transfer of an ORIGINAL private provider artifact.

No Store/Provider constructor, RPC, callback, SSH, grant, settlement or task
mutation. Transport/authentication is the caller's existing native authority.
The consumer requires an externally trusted manifest, not a self-attesting body.
"""
import base64
import contextlib
import functools
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path

from . import campus_adapter, egress_adapter
from .provider_runtime import _owner_bytes
from .resources import _json


FORMAT = 'mesh-private-artifact-transfer/1'
MAX_ARTIFACT_BYTES = 24 * 1024 * 1024
CHUNK_BYTES = 65536
_CONTEXT_KEYS = {'operation_id', 'capability_id', 'provider', 'receipt_id',
                 'adapter_handle', 'capability_epoch', 'action', 'scope',
                 'workload', 'task_id', 'task_epoch'}
_SCHEMAS = {
    'mesh_messages': ('sender message_id fingerprint task_id created').split(),
    'tasks': ('id parent_id input required status node epoch deadline checkpoint result created attempts '
              'context scope paused_status paused_deadline leader_epoch').split(),
    'managed_allocations': ('operation_id fingerprint actor task_id task_epoch task_node leader_epoch '
                           'plan resolved state epoch deadline created updated').split(),
    'managed_dispatch': ('operation_id capability_id provider state epoch receipt_id settlement created updated').split(),
    'provider_metadata': ['key', 'value'],
    'provider_executions': ('operation_id capability_id receipt_id fingerprint context state '
                            'authority_epoch invocation_nonce result error created updated').split(),
}
_MANIFEST_KEYS = {'format', 'authority', 'sender', 'message_id', 'message_fingerprint',
    'task_id', 'task_epoch', 'operation_id', 'capability_id', 'capability_epoch',
    'provider', 'receipt_id', 'invocation_nonce', 'scope_sha256', 'workload_sha256',
    'provider_context_sha256', 'settlement_sha256', 'adapter', 'artifact_format',
    'artifact_id', 'artifact_bytes', 'artifact_sha256', 'body_bytes', 'body_sha256',
    'evidence_level', 'native_owner_file_authority', 'managed_artifact_grant_verified',
    'execution_verified', 'target_content_independently_verified',
    'authentication_verified', 'model_inference_verified', 'original_operation_replayed'}


class ArtifactTransportError(ValueError):
    """Fixed codes only; no DB messages, paths, credentials or response bytes."""


def _safe(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ArtifactTransportError:
            raise
        except Exception:
            raise ArtifactTransportError('artifact_transport_unavailable_or_invalid') from None
    return call


def _require(condition, code):
    if not condition:
        raise ArtifactTransportError(code)


def _name(value):
    _require(isinstance(value, str) and 0 < len(value) <= 256 and
             all(ord(char) >= 32 and not 0xd800 <= ord(char) <= 0xdfff for char in value),
             'artifact_identity_invalid')
    return value


def _sha(value):
    _require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None,
             'artifact_digest_invalid')
    return value


def _positive(value, maximum=MAX_ARTIFACT_BYTES):
    _require(type(value) is int and 0 < value <= maximum, 'artifact_limit_invalid')
    return value


def _digest(value):
    # Same encoding as provider/allocations and their original context hashes.
    return hashlib.sha256(_json(value, 'artifact_binding', 131072).encode('utf8')).hexdigest()


def _mesh_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def _unique(items):
    result = {}
    for key, value in items:
        _require(key not in result, 'artifact_duplicate_json_key')
        result[key] = value
    return result


def _decode(raw, maximum=131072):
    if isinstance(raw, str):
        raw = raw.encode('utf8')
    _require(isinstance(raw, bytes) and len(raw) <= maximum, 'artifact_json_limit_invalid')
    value = json.loads(raw.decode('utf8'), object_pairs_hook=_unique,
                       parse_constant=lambda ignored: (_ for _ in ()).throw(ValueError()))
    _require(isinstance(value, dict), 'artifact_json_object_required')
    return value


def _private_path(path):
    path = Path(path)
    _require(path.is_absolute() and '..' not in path.parts, 'artifact_private_path_required')
    # Existing owner's native file authority, not an additional managed grant.
    egress_adapter._root(path.parent)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and
             stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1,
             'artifact_private_path_required')
    return path, (before.st_dev, before.st_ino)


@contextlib.contextmanager
def _readonly(path, tables):
    path, identity = _private_path(path)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        for table in tables:
            columns = [row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')]
            _require(columns == _SCHEMAS[table], 'artifact_source_schema_unsupported')
        yield db
        _require(_private_path(path)[1] == identity, 'artifact_source_database_changed')
    finally:
        if db.in_transaction:
            db.execute('ROLLBACK')
        db.close()


def _one(db, sql, parameters):
    rows = db.execute(sql, parameters).fetchmany(2)
    _require(len(rows) == 1, 'artifact_original_record_not_found_or_ambiguous')
    return dict(rows[0])


def _selection(plan, capability_id):
    _require(isinstance(plan, dict) and isinstance(plan.get('selections'), list),
             'artifact_original_plan_invalid')
    selections = [item for item in plan['selections'] if isinstance(item, dict)
                  and item.get('capability_id') == capability_id]
    _require(len(selections) == 1, 'artifact_original_selection_ambiguous')
    return selections[0]


def _check_value(raw, manifest):
    _require(len(raw) == manifest['artifact_bytes'] and
             hashlib.sha256(raw).hexdigest() == manifest['artifact_sha256'],
             'artifact_complete_file_hash_mismatch')
    value = _decode(raw, MAX_ARTIFACT_BYTES)
    for key in ('format', 'operation_id', 'capability_id', 'provider', 'task_id',
                'task_epoch', 'receipt_id', 'invocation_nonce', 'scope_sha256', 'workload_sha256'):
        expected = manifest['artifact_format'] if key == 'format' else manifest[key]
        _require(value.get(key) == expected and type(value.get(key)) is type(expected),
                 'artifact_original_content_binding_mismatch')
    data = base64.b64decode(value['response_base64'], validate=True)
    evidence = value['observation']
    _require(isinstance(evidence, dict) and len(data) == manifest['body_bytes'] and
             len(data) == evidence.get('response_bytes') and
             hashlib.sha256(data).hexdigest() == manifest['body_sha256'] == evidence.get('response_sha256'),
             'artifact_complete_body_hash_mismatch')
    return value


def _manifest(value, maximum):
    _positive(maximum)
    _require(isinstance(value, dict) and set(value) == _MANIFEST_KEYS and value['format'] == FORMAT,
             'artifact_manifest_invalid')
    detached = _decode(_json(value, 'artifact_manifest', 16384).encode('utf8'), 16384)
    for key in ('authority', 'sender', 'message_id', 'task_id', 'operation_id', 'capability_id', 'provider', 'receipt_id'):
        _name(detached[key])
    for key in ('message_fingerprint', 'scope_sha256', 'workload_sha256', 'provider_context_sha256',
                'settlement_sha256', 'artifact_sha256', 'body_sha256'):
        _sha(detached[key])
    for key in ('task_epoch', 'capability_epoch'):
        _positive(detached[key], (1 << 63) - 1)
    _positive(detached['artifact_bytes'], maximum)
    _require(type(detached['body_bytes']) is int and 0 <= detached['body_bytes'] <= maximum,
             'artifact_manifest_invalid')
    _require(detached['adapter'] in ('campus', 'https') and
             detached['artifact_format'] == ('mesh-campus-http-artifact/1' if detached['adapter'] == 'campus'
                                           else 'mesh-https-artifact/1') and
             isinstance(detached['artifact_id'], str) and re.fullmatch(
                 ('campus' if detached['adapter'] == 'campus' else 'https') + r'-[0-9a-f]{64}\.json',
                 detached['artifact_id']) is not None and
             isinstance(detached['invocation_nonce'], str) and
             re.fullmatch(r'[0-9a-f]{32}', detached['invocation_nonce']) is not None,
             'artifact_manifest_invalid')
    _require(detached['evidence_level'] == 'provider_reported' and
             detached['native_owner_file_authority'] is True and
             all(detached[key] is False for key in ('managed_artifact_grant_verified', 'execution_verified',
                'target_content_independently_verified', 'authentication_verified', 'model_inference_verified',
                'original_operation_replayed')), 'artifact_manifest_evidence_invalid')
    return detached


class ArtifactProducer:
    """Fixed owner config; request IDs select original records, never file paths.

`sender` is the owner-installed original A2A source binding, NOT an authenticated
HTTP principal supplied by a model. Native SSH owner file authority authorizes
this producer. This library does not create a managed artifact-read grant.
"""
    @_safe
    def __init__(self, owner_config_path, owner_config_sha256):
        self.path, self.sha = str(owner_config_path), _sha(owner_config_sha256)
        self._config()

    def _config(self):
        raw = _owner_bytes(self.path, 65536, 'artifact_owner_config_unavailable')
        _require(hashlib.sha256(raw).hexdigest() == self.sha, 'artifact_owner_config_changed')
        config = _decode(raw, 65536)
        _require(set(config) == {'schema', 'authority', 'sender', 'authority_db',
                                'max_artifact_bytes', 'bindings'} and config['schema'] == 1
                 and type(config['schema']) is int, 'artifact_owner_config_invalid')
        _name(config['authority'])
        _name(config['sender'])
        _private_path(config['authority_db'])
        _positive(config['max_artifact_bytes'])
        _require(isinstance(config['bindings'], dict) and 1 <= len(config['bindings']) <= 64,
                 'artifact_owner_config_invalid')
        for capability, binding in config['bindings'].items():
            _name(capability)
            _require(isinstance(binding, dict) and set(binding) == {'adapter', 'provider',
                'provider_journal', 'owner_config_path', 'owner_config_sha256'} and
                binding['adapter'] in ('campus', 'https'), 'artifact_owner_binding_invalid')
            _name(binding['provider'])
            _private_path(binding['provider_journal'])
            _sha(binding['owner_config_sha256'])
        return config

    def _prepare(self, message_id, operation_id, capability_id):
        for identity in (message_id, operation_id, capability_id):
            _name(identity)
        config = self._config()
        binding = config['bindings'].get(capability_id)
        _require(binding is not None, 'artifact_capability_not_owner_bound')
        with _readonly(config['authority_db'], ('mesh_messages', 'tasks', 'managed_allocations', 'managed_dispatch')) as db:
            message = _one(db, 'SELECT * FROM mesh_messages WHERE sender=? AND message_id=?',
                           (config['sender'], message_id))
            _sha(message['fingerprint'])
            _require(message['task_id'] == _mesh_digest([config['authority'], config['sender'], message_id]),
                     'artifact_original_message_task_mismatch')
            task = _one(db, 'SELECT id,epoch,context,scope FROM tasks WHERE id=? AND length(context)<=65536',
                        (message['task_id'],))
            context = _decode(task['context'], 65536)
            origin = context.get('origin', {})
            _require(context.get('authority') == 'local:' + config['authority'] and
                     origin.get('kind') == 'a2a' and origin.get('peer') == config['sender'] and
                     origin.get('message_id') == message_id, 'artifact_original_task_origin_mismatch')
            allocation = _one(db, 'SELECT operation_id,task_id,task_epoch,task_node,actor,state,plan '
                'FROM managed_allocations WHERE operation_id=? AND length(plan)<=131072', (operation_id,))
            _require(allocation['task_id'] == task['id'] and allocation['state'] == 'completed' and
                     allocation['actor'] == 'node:' + allocation['task_node'] and
                     type(allocation['task_epoch']) is int and 1 <= allocation['task_epoch'] <= task['epoch'],
                     'artifact_original_allocation_mismatch_or_unsettled')
            selection = _selection(_decode(allocation['plan']), capability_id)
            dispatch = _one(db, 'SELECT * FROM managed_dispatch WHERE operation_id=? AND capability_id=? '
                'AND length(settlement)<=8192', (operation_id, capability_id))
            _require(dispatch['state'] == 'completed' and dispatch['provider'] == binding['provider'],
                     'artifact_original_dispatch_mismatch_or_unsettled')
            settlement = _decode(dispatch['settlement'], 8192)
        with _readonly(binding['provider_journal'], ('provider_metadata', 'provider_executions')) as db:
            metadata = dict((row['key'], row['value']) for row in db.execute('SELECT * FROM provider_metadata LIMIT 4'))
            _require(metadata == {'schema': '1', 'provider': binding['provider'], 'authority': config['authority']},
                     'artifact_original_provider_authority_mismatch')
            execution = _one(db, 'SELECT * FROM provider_executions WHERE operation_id=? AND capability_id=? '
                'AND length(context)<=65536 AND length(result)<=8192', (operation_id, capability_id))
        provider_context = _decode(execution['context'], 65536)
        _require(set(provider_context) == _CONTEXT_KEYS and execution['state'] == 'settled' and
                 execution['error'] is None and _digest(provider_context) == execution['fingerprint'] and
                 type(execution['authority_epoch']) is int and execution['authority_epoch'] == dispatch['epoch'],
                 'artifact_original_provider_unsettled_or_changed')
        expected = {'operation_id': operation_id, 'capability_id': capability_id,
            'provider': binding['provider'], 'task_id': task['id'], 'task_epoch': allocation['task_epoch'],
            'capability_epoch': selection.get('epoch'), 'action': selection.get('action'),
            'scope': selection.get('scope'), 'workload': selection.get('workload'), 'receipt_id': dispatch['receipt_id']}
        _require(all(provider_context.get(key) == value and type(provider_context.get(key)) is type(value)
                     for key, value in expected.items()) and execution['receipt_id'] == dispatch['receipt_id'] ==
                 'provider-' + _digest([config['authority'], binding['provider'], operation_id, capability_id]),
                 'artifact_original_provider_context_mismatch')
        actual = _decode(execution['result'], 8192)
        _require(actual.get('outcome') == 'completed' and actual.get('resource_quiescent') is True and
                 isinstance(actual.get('evidence'), dict), 'artifact_original_result_invalid')
        evidence = dict(actual['evidence'], resource_quiescent=True, result_reference=actual.get('result_reference'),
                        adapter_handle=provider_context['adapter_handle'], invocation_nonce=execution['invocation_nonce'])
        _require(settlement == {'outcome': 'completed', 'evidence': evidence},
                 'artifact_original_settlement_mismatch')
        adapter = campus_adapter if binding['adapter'] == 'campus' else egress_adapter
        adapter_config = adapter._configuration(binding['owner_config_path'], binding['owner_config_sha256'])
        _require(all(adapter_config.get(key) == provider_context[key] for key in
                     ('provider', 'capability_id', 'capability_epoch', 'action')),
                 'artifact_original_adapter_binding_mismatch')
        artifact_id, artifact_sha = actual['evidence'].get('artifact_id'), actual['evidence'].get('artifact_sha256')
        _sha(artifact_sha)
        prefix = 'campus' if binding['adapter'] == 'campus' else 'https'
        _require(artifact_id == prefix + '-' + _digest([operation_id, capability_id, execution['invocation_nonce']]) + '.json'
                 and actual.get('result_reference') == 'mesh-' + ('campus-http' if prefix == 'campus' else 'https')
                 + '-artifact:sha256:' + artifact_sha, 'artifact_original_reference_mismatch')
        # Existing adapter's pure local full-file/body verifier, NOT execute().
        value = adapter.read_artifact(binding['owner_config_path'], binding['owner_config_sha256'], artifact_id, artifact_sha)
        raw = _owner_bytes(adapter_config['artifact_root'] / artifact_id, config['max_artifact_bytes'],
                           'artifact_original_file_unavailable')
        manifest = dict(expected)
        for key in ('action', 'scope', 'workload'):
            manifest.pop(key)
        manifest.update(format=FORMAT, authority=config['authority'], sender=config['sender'],
            message_id=message_id, message_fingerprint=message['fingerprint'],
            invocation_nonce=execution['invocation_nonce'], scope_sha256=_digest(provider_context['scope']),
            workload_sha256=_digest(provider_context['workload']), provider_context_sha256=execution['fingerprint'],
            settlement_sha256=_digest(settlement), adapter=binding['adapter'], artifact_format=adapter.FORMAT,
            artifact_id=artifact_id, artifact_bytes=len(raw), artifact_sha256=artifact_sha,
            body_bytes=value['observation']['response_bytes'], body_sha256=value['observation']['response_sha256'],
            evidence_level='provider_reported', native_owner_file_authority=True,
            managed_artifact_grant_verified=False, execution_verified=False,
            target_content_independently_verified=False, authentication_verified=False,
            model_inference_verified=False, original_operation_replayed=False)
        manifest = _manifest(manifest, config['max_artifact_bytes'])
        verified = _check_value(raw, manifest)
        _require(verified == value and all(actual['evidence'].get(key) == value['observation'].get(key)
                 for key in ('response_bytes', 'response_sha256')), 'artifact_original_file_changed')
        return manifest, raw

    @_safe
    def manifest(self, message_id, operation_id, capability_id):
        return self._prepare(message_id, operation_id, capability_id)[0]

    @_safe
    def write_to(self, message_id, operation_id, capability_id, binary_stream):
        """Write exact raw artifact bytes to the caller's PRIVATE transport stream.

        The caller must bound stream blocking/transport lifetime and must not log
        stdout if it intentionally uses stdout as this private binary channel.
        """
        manifest, raw = self._prepare(message_id, operation_id, capability_id)
        for offset in range(0, len(raw), CHUNK_BYTES):
            chunk = raw[offset:offset + CHUNK_BYTES]
            _require(binary_stream.write(chunk) == len(chunk), 'artifact_private_stream_short_write')
        return manifest


def _existing(parent, name, manifest):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and
                 stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1 and
                 before.st_size == manifest['artifact_bytes'], 'artifact_existing_output_conflict')
        raw = source.read(manifest['artifact_bytes'] + 1)
        after = os.fstat(source.fileno())
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                 (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) ==
                 (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns, named.st_ctime_ns),
                 'artifact_existing_output_changed')
        _check_value(raw, manifest)
    return True


def _directory_unchanged(root, parent):
    current = egress_adapter._root(root).lstat()
    opened = os.fstat(parent)
    _require((current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino),
             'artifact_output_directory_changed')


@_safe
def fetch_artifact(binary_stream, externally_pinned_manifest, output_path, max_artifact_bytes=MAX_ARTIFACT_BYTES):
    """Verify complete private stream then exclusively publish an owned 0600 file.

    This function does NOT authenticate its manifest. The caller obtains/pins it
    through an existing authorized source and original receipt mapping. A model
    supplied manifest or a stream's own manifest is not independent authority.
    Stream reads must be bounded by the caller's transport. No network is opened.
    """
    manifest = _manifest(externally_pinned_manifest, max_artifact_bytes)
    output = Path(output_path)
    _require(output.is_absolute() and '..' not in output.parts, 'artifact_private_output_required')
    root = egress_adapter._root(output.parent)
    parent = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = '.artifact-transfer-' + uuid.uuid4().hex
    created = False
    try:
        info = os.fstat(parent)
        named = root.lstat()
        _require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino), 'artifact_output_directory_changed')
        if _existing(parent, output.name, manifest):
            _directory_unchanged(root, parent)
            return dict(manifest, artifact_content_verified=True, already_present=True)
        descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        created = True
        with os.fdopen(descriptor, 'w+b') as destination:
            remaining, digest = manifest['artifact_bytes'], hashlib.sha256()
            while remaining:
                chunk = binary_stream.read(min(CHUNK_BYTES, remaining))
                _require(isinstance(chunk, bytes) and 0 < len(chunk) <= min(CHUNK_BYTES, remaining),
                         'artifact_private_stream_truncated_or_invalid')
                destination.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            _require(binary_stream.read(1) == b'', 'artifact_private_stream_extra_bytes')
            _require(digest.hexdigest() == manifest['artifact_sha256'], 'artifact_complete_file_hash_mismatch')
            destination.flush()
            os.fsync(destination.fileno())
            destination.seek(0)
            _check_value(destination.read(manifest['artifact_bytes'] + 1), manifest)
            try:
                os.link(temporary, output.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                existing = False
                os.fsync(parent)
            except FileExistsError:
                existing = _existing(parent, output.name, manifest)
                _require(existing, 'artifact_output_changed')
        # Drop only our private staging link, then verify the actual named
        # single-link output and the still-attached owner directory before ACK.
        os.unlink(temporary, dir_fd=parent)
        created = False
        _require(_existing(parent, output.name, manifest), 'artifact_output_changed')
        _directory_unchanged(root, parent)
        return dict(manifest, artifact_content_verified=True, already_present=existing)
    finally:
        if created:
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)
