"""Owner-installed generic HTTPS callback for the existing provider pipeline.

Only an admitted scope chooses the destination/query. Deployment configuration,
SSH credentials, executable client and artifact directory are fixed owner
references, not model arguments. No host network or native tool is intercepted.
The ManagedProvider journal, not this adapter, controls admission/no replay.
"""
import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .egress import EgressConfig, SshSocksEgress, _bounded_capture
from .provider_runtime import _owner_bytes
from .resources import _json, _name, _safe_metadata


FORMAT = 'mesh-https-artifact/1'


class EgressAdapterError(ValueError):
    """Safe fixed diagnostic categories, never private file/raw client errors."""


def _require(condition, category):
    if not condition:
        raise EgressAdapterError(category)


def _sha(value):
    _require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None,
             'egress_expected_sha_required')
    return value


def _unique_object(items):
    result = {}
    for key, value in items:
        _require(key not in result, 'egress_duplicate_private_config_key')
        result[key] = value
    return result


def _owner_json(path, expected_sha):
    try:
        raw = _owner_bytes(path, 65536, 'egress_private_config_unavailable')
    except Exception:
        raise EgressAdapterError('egress_private_config_unavailable') from None
    _require(hashlib.sha256(raw).hexdigest() == _sha(expected_sha), 'egress_private_config_changed')
    try:
        value = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object)
        _require(isinstance(value, dict), 'egress_private_config_invalid')
        return value
    except (ValueError, UnicodeError):
        raise EgressAdapterError('egress_private_config_invalid') from None


def _root(path):
    try:
        path = Path(path)
        _require(path.is_absolute() and '..' not in path.parts, 'egress_artifact_root_invalid')
        for ancestor in (path,) + tuple(path.parents):
            info = ancestor.lstat()
            _require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.getuid()) and
                     (not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX), 'egress_artifact_root_invalid')
        info = path.lstat()
        _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                 'egress_artifact_root_invalid')
        return path
    except (OSError, TypeError):
        raise EgressAdapterError('egress_artifact_root_invalid') from None


def _target(url):
    _require(isinstance(url, str) and 0 < len(url) <= 4096 and
             all(ord(char) > 32 and ord(char) < 127 for char in url), 'egress_https_target_invalid')
    try:
        parsed = urlsplit(url)
        _require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and
                 not parsed.password and not parsed.query and not parsed.fragment and '\\' not in url,
                 'egress_https_target_invalid')
        host = parsed.hostname.lower()
        _require(not host.endswith('.') and host != 'localhost' and not host.endswith(('.localhost', '.local')),
                 'egress_public_target_required')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            _require(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) is not None,
                     'egress_https_target_invalid')
        else:
            _require(address.is_global, 'egress_public_target_required')
        port = parsed.port
        _require(port is None or port == 443, 'egress_https_default_port_required')
        origin = 'https://' + ('[' + host + ']' if ':' in host else host)
        return url, origin
    except ValueError:
        raise EgressAdapterError('egress_https_target_invalid') from None


def _configuration(path, expected_sha):
    config = _owner_json(path, expected_sha)
    fields = {'schema', 'provider', 'capability_id', 'capability_epoch', 'action',
              'transport_mode', 'egress_node', 'artifact_root',
              'allowed_origins', 'expires_at', 'max_bytes', 'timeout_seconds'}
    _require(config.get('transport_mode') in ('direct', 'ssh_socks5h'), 'egress_transport_mode_invalid')
    if config['transport_mode'] == 'ssh_socks5h':
        fields |= {'transport_config', 'transport_config_sha256'}
    _require(set(config) == fields and type(config['schema']) is int and config['schema'] == 1,
             'egress_private_config_invalid')
    try:
        for key in ('provider', 'capability_id', 'action', 'egress_node'):
            _name(config[key], key, 200)
    except ValueError:
        raise EgressAdapterError('egress_private_config_invalid') from None
    _require(type(config['capability_epoch']) is int and config['capability_epoch'] >= 1 and
             isinstance(config['expires_at'], (int, float)) and not isinstance(config['expires_at'], bool) and
             math.isfinite(config['expires_at']) and config['expires_at'] > 0 and
             type(config['max_bytes']) is int and 1 <= config['max_bytes'] <= 16777216 and
             isinstance(config['timeout_seconds'], (int, float)) and not isinstance(config['timeout_seconds'], bool) and
             math.isfinite(config['timeout_seconds']) and 0 < config['timeout_seconds'] <= 60,
             'egress_private_config_invalid')
    origins = config['allowed_origins']
    _require(isinstance(origins, list) and 1 <= len(origins) <= 256 and
             all(isinstance(item, str) for item in origins), 'egress_origin_contract_invalid')
    if origins != ['*']:
        for origin in origins:
            _, canonical = _target(origin)
            _require(origin == canonical, 'egress_origin_contract_invalid')
    config['artifact_root'] = _root(config['artifact_root'])
    if config['transport_mode'] == 'ssh_socks5h':
        transport = _owner_json(config['transport_config'], config['transport_config_sha256'])
        _require({'ssh_destination', 'ssh_config'} <= set(transport) and not set(transport) -
                 {'ssh_destination', 'ssh_config', 'port', 'readiness_seconds', 'max_seconds', 'ssh_binary'},
                 'egress_transport_config_invalid')
        try:
            config['ssh_transport'] = EgressConfig(**transport)
        except Exception:
            raise EgressAdapterError('egress_transport_config_invalid') from None
    return config


def _request(context, config):
    _require(isinstance(context, dict), 'egress_admitted_context_required')
    for key in ('provider', 'capability_id', 'capability_epoch', 'action'):
        _require(context.get(key) == config[key] and type(context.get(key)) is type(config[key]),
                 'egress_admitted_identity_mismatch')
    for key in ('operation_id', 'task_id', 'receipt_id', 'adapter_handle'):
        try:
            _name(context.get(key), key, 256)
        except ValueError:
            raise EgressAdapterError('egress_admitted_context_required') from None
    _require(type(context.get('task_epoch')) is int and context['task_epoch'] >= 1 and
             isinstance(context.get('invocation_nonce'), str) and
             re.fullmatch(r'[0-9a-f]{32}', context['invocation_nonce']) is not None,
             'egress_admitted_context_required')
    scope, workload = context.get('scope'), context.get('workload')
    _require(isinstance(scope, dict) and {'operation', 'url'} <= set(scope) and
             not set(scope) - {'operation', 'url', 'query'} and scope['operation'] in ('get', 'probe'),
             'egress_scope_invalid')
    url, origin = _target(scope['url'])
    _require(config['allowed_origins'] == ['*'] or origin in config['allowed_origins'],
             'egress_target_outside_owner_scope')
    query = scope.get('query', {})
    _require(isinstance(query, dict) and len(query) <= 64, 'egress_query_invalid')
    safe, redacted = _safe_metadata(query)
    _require(not redacted, 'egress_query_private_metadata_not_allowed')
    for key, value in safe.items():
        _require(isinstance(key, str) and 0 < len(key) <= 128 and all(ord(char) >= 32 for char in key) and
                 isinstance(value, str) and len(value) <= 2048 and all(ord(char) >= 32 for char in value),
                 'egress_query_invalid')
    _require(len(_json(query, 'egress_query', 8192)) <= 8192, 'egress_query_invalid')
    if query:
        url += '?' + urlencode(sorted(query.items()))
    _require(isinstance(workload, dict) and set(workload) == {'kind', 'max_bytes', 'timeout_seconds'} and
             workload['kind'] == 'https' and type(workload['max_bytes']) is int and
             1 <= workload['max_bytes'] <= config['max_bytes'] and
             isinstance(workload['timeout_seconds'], (int, float)) and not isinstance(workload['timeout_seconds'], bool) and
             math.isfinite(workload['timeout_seconds']) and 0 < workload['timeout_seconds'] <= config['timeout_seconds'],
             'egress_workload_outside_owner_limits')
    _require(config['expires_at'] > time.time(), 'egress_owner_contract_expired')
    return url, origin, 'HEAD' if scope['operation'] == 'probe' else 'GET'


class _DirectPath:
    """No listener/session: network proof comes only from the real curl result."""
    process = None
    proxy_url = ''

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def _remaining(self):
        return 60

    def status(self):
        return {'transport': 'direct', 'connection_verified': False}


def _fetch(path, url, method, maximum, timeout):
    marker = b'\nMESH_EGRESS_METADATA:'
    proxy = path.proxy_url
    # An explicit empty curl proxy plus '*' bypasses ALL_PROXY/HTTPS_PROXY/etc
    # for this invocation, irrespective of inherited process configuration.
    command = ['curl', '--disable', '--silent', '--show-error', '--proxy', proxy,
               '--noproxy', '*' if not proxy else '', '--proto', '=https', '--connect-timeout', str(min(10, timeout)),
               '--max-time', str(timeout), '--max-filesize', str(maximum), '--output', '-',
               '--write-out', marker.decode('ascii') + '%{http_code} %{ssl_verify_result}']
    if method == 'HEAD':
        command.append('--head')
    command += ['--url', url]
    try:
        exit_code, captured = _bounded_capture(command, maximum + 64, timeout + 1)
    except Exception:
        raise EgressAdapterError('egress_request_outcome_unknown') from None
    pieces = captured.rsplit(marker, 1)
    _require(len(pieces) == 2 and len(pieces[0]) <= maximum, 'egress_request_outcome_unknown')
    fields = pieces[1].split()
    _require(len(fields) == 2 and all(field.isdigit() for field in fields), 'egress_request_outcome_unknown')
    status, tls = map(int, fields)
    _require(exit_code == 0 and tls == 0 and 100 <= status <= 599, 'egress_request_outcome_unknown')
    path.status()  # SSH path liveness; direct has no fictional connection/readiness proof
    return pieces[0], {'http_status': status, 'tls_verify_result': tls, 'curl_exit_code': exit_code,
                       'tls_http_reachable': True, 'http_success': 200 <= status < 300}


def _sync_directory(root):
    descriptor = os.open(str(root), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_artifact(root, identity, value):
    root = _root(root)
    raw = _json(value, 'egress_artifact', 24 * 1024 * 1024).encode('utf8')
    target = root / identity
    descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    _sync_directory(root)
    return hashlib.sha256(raw).hexdigest()


def execute(context, owner_config_path, owner_config_sha256):
    """Callable from a private version/hash-pinned ProviderRuntime wrapper.

    Do not expose this as a public bypass for ``allocation.start``. The runtime
    supplies its admitted context and invokes it once under the original IDs.
    Callback failure/timeout remains unknown; this function never retries HTTP.
    """
    config = _configuration(owner_config_path, owner_config_sha256)
    url, origin, method = _request(context, config)
    artifact_id = 'https-' + hashlib.sha256(_json(
        [context['operation_id'], context['capability_id'], context['invocation_nonce']],
        'egress_artifact_identity').encode('utf8')).hexdigest() + '.json'
    # Reserve an immutable intent before a request, even if directly miscalled.
    intent_id = hashlib.sha256(_json([config['provider'], context['operation_id'], context['capability_id']],
                                    'egress_intent_identity').encode('utf8')).hexdigest()
    intent = config['artifact_root'] / ('https-' + intent_id + '.intent')
    try:
        descriptor = os.open(str(intent), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise EgressAdapterError('egress_original_invocation_no_replay') from None
    with os.fdopen(descriptor, 'wb') as output:
        output.write(b'owned invocation; no automatic replay\n')
        output.flush()
        os.fsync(output.fileno())
    _sync_directory(config['artifact_root'])
    began = time.monotonic()
    carrier = SshSocksEgress(config['ssh_transport']) if config['transport_mode'] == 'ssh_socks5h' else _DirectPath()
    with carrier as path:
        remaining = config['expires_at'] - time.time()
        _require(remaining > 0, 'egress_owner_contract_expired')
        timeout = min(context['workload']['timeout_seconds'], remaining, path._remaining())
        data, report = _fetch(path, url, method, context['workload']['max_bytes'], timeout)
        owned = path.process
    _require(owned is None or owned.poll() is not None, 'egress_resource_quiescence_unproven')
    evidence = dict(report, method=method, target_origin=origin, transport=config['transport_mode'],
                    egress_node=config['egress_node'], egress_node_verification='owner_contract',
                    response_bytes=len(data), response_sha256=hashlib.sha256(data).hexdigest(),
                    response_kind='headers' if method == 'HEAD' else 'body',
                    duration_seconds=round(time.monotonic() - began, 3),
                    authentication_verified=False, model_inference_verified=False,
                    redirects_followed=False, automatic_replay=False, native_tools_intercepted=False,
                    owned_curl_reaped=True, owned_ssh_reaped=owned is not None,
                    owned_ssh_exit_code=owned.returncode if owned is not None else None)
    value = {'format': FORMAT, 'operation_id': context['operation_id'],
             'capability_id': context['capability_id'], 'provider': context['provider'],
             'task_id': context['task_id'], 'task_epoch': context['task_epoch'],
             'receipt_id': context['receipt_id'], 'invocation_nonce': context['invocation_nonce'],
             'scope_sha256': hashlib.sha256(_json(context['scope'], 'scope').encode('utf8')).hexdigest(),
             'workload_sha256': hashlib.sha256(_json(context['workload'], 'workload').encode('utf8')).hexdigest(),
             'observed_at': time.time(), 'observation': evidence,
             'response_base64': base64.b64encode(data).decode('ascii')}
    digest = _write_artifact(config['artifact_root'], artifact_id, value)
    return {'outcome': 'completed', 'resource_quiescent': True,
            'result_reference': 'mesh-https-artifact:sha256:' + digest,
            'evidence': dict(evidence, artifact_id=artifact_id, artifact_sha256=digest)}


def read_artifact(owner_config_path, owner_config_sha256, artifact_id, artifact_sha256):
    """Read/hash-check exact private bytes under the fixed installation root.

    This pure local read never opens a tunnel, retries a request or reconciles an
    unknown journal. Its caller must have the owner's native file authority.
    """
    config = _configuration(owner_config_path, owner_config_sha256)
    _require(isinstance(artifact_id, str) and re.fullmatch(r'https-[0-9a-f]{64}\.json', artifact_id),
             'egress_artifact_identity_invalid')
    raw = _owner_bytes(config['artifact_root'] / artifact_id, 24 * 1024 * 1024,
                       'egress_artifact_unavailable')
    _require(hashlib.sha256(raw).hexdigest() == _sha(artifact_sha256), 'egress_artifact_hash_mismatch')
    try:
        value = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object)
        _require(value.get('format') == FORMAT, 'egress_artifact_invalid')
        response = base64.b64decode(value['response_base64'], validate=True)
        _require(len(response) == value['observation']['response_bytes'] and
                 hashlib.sha256(response).hexdigest() == value['observation']['response_sha256'],
                 'egress_artifact_body_hash_mismatch')
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise EgressAdapterError('egress_artifact_invalid') from None
    return value
