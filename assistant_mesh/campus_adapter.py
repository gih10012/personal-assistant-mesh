"""Selective unauthenticated campus HTTP on an owner-connected local node.

This is an installed callback, not a general proxy or native-network gate.
Private owner configuration bounds networks/protocols/port ranges. An admitted
scope chooses a URL/address within that contract, never credentials/configuration paths.
The original operation intent and ProviderRuntime journal survive unknowns.
"""
import base64
import functools
import hashlib
import ipaddress
import json
import math
import os
import re
import time
from urllib.parse import urlencode, urlsplit

from .egress import _bounded_capture
from .egress_adapter import (_owner_json, _root, _sha, _sync_directory,
                             _unique_object, _write_artifact, EgressAdapterError)
from .provider_runtime import _owner_bytes
from .resources import _json, _name, _safe_metadata


FORMAT = 'mesh-campus-http-artifact/1'


class CampusAdapterError(ValueError):
    """Fixed safe diagnostics; never include a private path, URL or HTTP data."""


def _safe(function):
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CampusAdapterError:
            raise
        except Exception:
            raise CampusAdapterError('campus_callback_outcome_unknown') from None
    return guarded


def _require(condition, category):
    if not condition:
        raise CampusAdapterError(category)


def _digest(value):
    return hashlib.sha256(_json(value, 'campus_contract').encode('utf8')).hexdigest()


def _host(value):
    _require(isinstance(value, str) and value == value.lower() and not value.endswith('.'),
             'campus_destination_host_invalid')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        # Require an unambiguous ASCII DNS name, not legacy curl numeric IP
        # spellings (127.1/0x7f000001) which might ignore a --resolve pin.
        _require(len(value) <= 253 and re.fullmatch(
            r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}', value),
            'campus_destination_host_invalid')
    else:
        _require(value == str(address), 'campus_destination_host_invalid')
    return value


def _address(value, networks):
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        raise CampusAdapterError('campus_destination_address_invalid') from None
    _require(value == str(address) and not (address.is_loopback or address.is_link_local
             or address.is_multicast or address.is_unspecified) and
             any(address.version == network.version and address in network for network in networks),
             'campus_destination_address_outside_owner_network')
    return address


def _configuration(path, expected_sha):
    try:
        config = _owner_json(path, expected_sha)
    except EgressAdapterError:
        raise CampusAdapterError('campus_private_config_unavailable_or_changed') from None
    fields = {'schema', 'provider', 'capability_id', 'capability_epoch', 'action',
              'egress_node', 'artifact_root', 'allowed_networks', 'allowed_ports', 'allowed_schemes',
              'expires_at', 'max_bytes', 'timeout_seconds'}
    _require(set(config) == fields and type(config['schema']) is int and config['schema'] == 1,
             'campus_private_config_invalid')
    try:
        for key in ('provider', 'capability_id', 'action', 'egress_node'):
            _name(config[key], key)
    except ValueError:
        raise CampusAdapterError('campus_private_config_invalid') from None
    _require(type(config['capability_epoch']) is int and config['capability_epoch'] >= 1 and
             type(config['max_bytes']) is int and 1 <= config['max_bytes'] <= 16777216 and
             isinstance(config['expires_at'], (int, float)) and not isinstance(config['expires_at'], bool) and
             math.isfinite(config['expires_at']) and config['expires_at'] > 0 and
             isinstance(config['timeout_seconds'], (int, float)) and not isinstance(config['timeout_seconds'], bool) and
             math.isfinite(config['timeout_seconds']) and 0 < config['timeout_seconds'] <= 60,
             'campus_private_config_invalid')
    networks = config['allowed_networks']
    _require(isinstance(networks, list) and 1 <= len(networks) <= 32,
             'campus_owner_networks_invalid')
    try:
        parsed_networks = [ipaddress.ip_network(value, strict=True) for value in networks]
        _require(all(value == str(network) and network.prefixlen > 0
                     for value, network in zip(networks, parsed_networks)), 'campus_owner_networks_invalid')
    except (ValueError, TypeError):
        raise CampusAdapterError('campus_owner_networks_invalid') from None
    ranges = config['allowed_ports']
    _require(isinstance(ranges, list) and 1 <= len(ranges) <= 16 and
             all(isinstance(pair, list) and len(pair) == 2 and all(type(port) is int for port in pair)
                 and 1 <= pair[0] <= pair[1] <= 65535 for pair in ranges),
             'campus_owner_port_ranges_invalid')
    schemes = config['allowed_schemes']
    _require(isinstance(schemes, list) and 1 <= len(schemes) <= 2 and
             len(set(schemes)) == len(schemes) and all(scheme in ('http', 'https') for scheme in schemes),
             'campus_owner_schemes_invalid')
    config['parsed_networks'] = parsed_networks
    try:
        config['artifact_root'] = _root(config['artifact_root'])
    except EgressAdapterError:
        raise CampusAdapterError('campus_private_artifact_root_invalid') from None
    return config


def _request(context, config):
    _require(isinstance(context, dict), 'campus_admitted_context_required')
    for key in ('provider', 'capability_id', 'capability_epoch', 'action'):
        _require(context.get(key) == config[key] and type(context.get(key)) is type(config[key]),
                 'campus_admitted_identity_mismatch')
    try:
        for key in ('operation_id', 'task_id', 'receipt_id', 'adapter_handle'):
            _name(context.get(key), key, 256)
    except ValueError:
        raise CampusAdapterError('campus_admitted_context_required') from None
    _require(type(context.get('task_epoch')) is int and context['task_epoch'] >= 1 and
             isinstance(context.get('invocation_nonce'), str) and
             re.fullmatch(r'[0-9a-f]{32}', context['invocation_nonce']), 'campus_admitted_context_required')
    scope, workload = context.get('scope'), context.get('workload')
    _require(isinstance(scope, dict) and {'operation', 'url'} <= set(scope) and
             not set(scope) - {'operation', 'url', 'address', 'query'} and
             scope['operation'] in ('get', 'probe'), 'campus_scope_invalid')
    url = scope['url']
    _require(isinstance(url, str) and 0 < len(url) <= 4096 and '\\' not in url and
             all(32 < ord(char) < 127 for char in url), 'campus_http_target_invalid')
    try:
        target = urlsplit(url)
        _require(target.scheme in config['allowed_schemes'] and target.hostname and not target.username and
                 not target.password and not target.query and not target.fragment and '?' not in url and '#' not in url,
                 'campus_http_target_invalid')
        host = _host(target.hostname)
        # Canonical netloc prevents curl and urllib disagreeing about numeric
        # spelling, empty ports, brackets, case or authority parsing.
        port = target.port if target.port is not None else (443 if target.scheme == 'https' else 80)
        authority = '[' + host + ']' if ':' in host else host
        if target.port is not None:
            authority += ':' + str(port)
        _require(target.netloc == authority, 'campus_http_target_invalid')
    except ValueError:
        raise CampusAdapterError('campus_http_target_invalid') from None
    _require(any(low <= port <= high for low, high in config['allowed_ports']),
             'campus_target_outside_owner_ports')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        _require('address' in scope, 'campus_dns_target_requires_scoped_address')
        address = _address(scope['address'], config['parsed_networks'])
    else:
        address = _address(str(literal), config['parsed_networks'])
        _require('address' not in scope or scope['address'] == str(address), 'campus_ip_literal_pin_mismatch')
    query = scope.get('query', {})
    _require(isinstance(query, dict) and len(query) <= 64, 'campus_query_invalid')
    safe, redacted = _safe_metadata(query)
    _require(not redacted, 'campus_query_private_metadata_not_allowed')
    for key, value in safe.items():
        _require(isinstance(key, str) and 0 < len(key) <= 128 and all(ord(char) >= 32 for char in key) and
                 isinstance(value, str) and len(value) <= 2048 and all(ord(char) >= 32 for char in value),
                 'campus_query_invalid')
    _require(len(_json(query, 'campus_query', 8192)) <= 8192, 'campus_query_invalid')
    if query:
        url += '?' + urlencode(sorted(query.items()))
    _require(isinstance(workload, dict) and set(workload) == {'kind', 'max_bytes', 'timeout_seconds'} and
             workload['kind'] == 'campus_http' and type(workload['max_bytes']) is int and
             1 <= workload['max_bytes'] <= config['max_bytes'] and
             isinstance(workload['timeout_seconds'], (int, float)) and not isinstance(workload['timeout_seconds'], bool) and
             math.isfinite(workload['timeout_seconds']) and 0 < workload['timeout_seconds'] <= config['timeout_seconds'],
             'campus_workload_outside_owner_limits')
    _require(config['expires_at'] > time.time(), 'campus_owner_contract_expired')
    return url, host, port, str(address), target.scheme, 'HEAD' if scope['operation'] == 'probe' else 'GET'


def _fetch(url, host, port, address, scheme, method, maximum, timeout):
    marker = b'\nMESH_CAMPUS_METADATA:'
    pinned = '[' + address + ']' if ':' in address else address
    command = ['curl', '--disable', '--silent', '--show-error', '--proxy', '', '--noproxy', '*',
               '--proto', '=' + scheme, '--proto-redir', '=' + scheme, '--max-redirs', '0',
               '--connect-timeout', str(min(10, timeout)), '--max-time', str(timeout),
               '--max-filesize', str(maximum), '--output', '-',
               '--write-out', marker.decode('ascii') + '%{http_code} %{remote_ip} %{ssl_verify_result}']
    try:
        ipaddress.ip_address(host)
    except ValueError:
        command += ['--resolve', host + ':' + str(port) + ':' + pinned]
    if method == 'HEAD':
        command.append('--head')
    command += ['--url', url]
    try:
        exit_code, captured = _bounded_capture(command, maximum + 80, timeout + 1)
        pieces = captured.rsplit(marker, 1)
        _require(len(pieces) == 2 and len(pieces[0]) <= maximum, 'campus_request_outcome_unknown')
        fields = pieces[1].split()
        _require(len(fields) == 3 and fields[0].isdigit() and fields[2].isdigit(), 'campus_request_outcome_unknown')
        status = int(fields[0])
        actual = str(ipaddress.ip_address(fields[1].decode('ascii')))
        tls = int(fields[2])
        _require(exit_code == 0 and actual == address and 100 <= status <= 599 and tls == 0,
                 'campus_request_outcome_unknown')
    except Exception:
        raise CampusAdapterError('campus_request_outcome_unknown') from None
    return pieces[0], {'http_status': status, 'http_success': 200 <= status < 300,
                       'http_reachable': True, 'curl_exit_code': exit_code,
                       'connected_address_matches_owner_pin': True, 'tls_used': scheme == 'https',
                       'tls_verify_result': tls if scheme == 'https' else None,
                       'tls_verified': scheme == 'https', 'transport_authentication_verified': scheme == 'https'}


@_safe
def execute(context, owner_config_path, owner_config_sha256):
    """Install behind ProviderRuntime; never expose as an admission bypass."""
    config = _configuration(owner_config_path, owner_config_sha256)
    url, host, port, address, scheme, method = _request(context, config)
    root = config['artifact_root']
    intent = root / ('campus-' + _digest([config['provider'], context['operation_id'], context['capability_id']]) + '.intent')
    try:
        descriptor = os.open(str(intent), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise CampusAdapterError('campus_original_invocation_no_replay') from None
    with os.fdopen(descriptor, 'wb') as output:
        output.write(b'owned campus invocation; no automatic replay\n')
        output.flush()
        os.fsync(output.fileno())
    _sync_directory(root)
    remaining = config['expires_at'] - time.time()
    _require(remaining > 0, 'campus_owner_contract_expired')
    began = time.monotonic()
    data, report = _fetch(url, host, port, address, scheme, method, context['workload']['max_bytes'],
                          min(remaining, context['workload']['timeout_seconds']))
    evidence = dict(report, method=method, scheme=scheme, transport='direct_scoped_address',
        egress_node=config['egress_node'], egress_node_verification='owner_contract',
        destination_contract_sha256=_digest([host, port, address]),
        response_bytes=len(data), response_sha256=hashlib.sha256(data).hexdigest(),
        response_kind='headers' if method == 'HEAD' else 'body',
        duration_seconds=round(time.monotonic() - began, 3),
        authentication_verified=False, model_inference_verified=False,
        target_content_independently_verified=False, redirects_followed=False,
        automatic_replay=False, native_tools_intercepted=False, owned_curl_reaped=True)
    artifact_id = 'campus-' + _digest([context['operation_id'], context['capability_id'],
                                     context['invocation_nonce']]) + '.json'
    value = {'format': FORMAT, 'operation_id': context['operation_id'],
        'capability_id': context['capability_id'], 'provider': context['provider'],
        'task_id': context['task_id'], 'task_epoch': context['task_epoch'],
        'receipt_id': context['receipt_id'], 'invocation_nonce': context['invocation_nonce'],
        'scope_sha256': _digest(context['scope']), 'workload_sha256': _digest(context['workload']),
        'observed_at': time.time(), 'observation': evidence,
        'response_base64': base64.b64encode(data).decode('ascii')}
    try:
        digest = _write_artifact(root, artifact_id, value)
    except Exception:
        raise CampusAdapterError('campus_artifact_write_outcome_unknown') from None
    return {'outcome': 'completed', 'resource_quiescent': True,
            'result_reference': 'mesh-campus-http-artifact:sha256:' + digest,
            'evidence': dict(evidence, artifact_id=artifact_id, artifact_sha256=digest)}


@_safe
def read_artifact(owner_config_path, owner_config_sha256, artifact_id, artifact_sha256):
    """Read fixed private artifact and verify its bytes/body, never retry HTTP."""
    config = _configuration(owner_config_path, owner_config_sha256)
    _require(isinstance(artifact_id, str) and re.fullmatch(r'campus-[0-9a-f]{64}\.json', artifact_id),
             'campus_artifact_identity_invalid')
    try:
        raw = _owner_bytes(config['artifact_root'] / artifact_id, 24 * 1024 * 1024, 'campus_artifact_unavailable')
        _require(hashlib.sha256(raw).hexdigest() == _sha(artifact_sha256), 'campus_artifact_hash_mismatch')
        value = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object)
        _require(value.get('format') == FORMAT, 'campus_artifact_invalid')
        data = base64.b64decode(value['response_base64'], validate=True)
        _require(len(data) == value['observation']['response_bytes'] and
                 hashlib.sha256(data).hexdigest() == value['observation']['response_sha256'],
                 'campus_artifact_body_hash_mismatch')
    except CampusAdapterError:
        raise
    except Exception:
        raise CampusAdapterError('campus_artifact_unavailable_or_invalid') from None
    return value
