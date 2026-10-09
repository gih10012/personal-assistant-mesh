"""Optional read-only local uplink discovery, not native networking policy.

Linux's built-in collector reads OS link/default-route and NetworkManager device
status metadata. No connection, route, DNS, proxy or radio is changed; no SSID,
MAC, address, proxy value, account or credential is returned. A configured route
is not Internet reachability, bandwidth, cost, physical-host or execution proof.

``inspect_network`` accepts an explicit trusted adapter for another OS or richer
owner/model-authored discovery. This is not a universal OS script, remote source
loader, command allowlist for native agents, advertisement/pool allocator, or
reconnection controller. Returned capability candidates need explicit enrollment
and independent observations before managed allocation. Native agents can still
use their own Shell/network tools, including autonomous offline recovery.
"""
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import selectors
import subprocess
import time


FORMAT = 'mesh-network-inventory/1'
MAX_COMMAND_BYTES = 1024 * 1024
MAX_INTERFACES = 256
MAX_ROUTES = 256
COMMAND_TIMEOUT = 2
_PROXY_NAMES = frozenset(('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                          'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'))
_KINDS = frozenset(('wifi', 'ethernet', 'loopback', 'virtual', 'unknown'))
_STATES = frozenset(('up', 'down', 'unknown'))
_GATEWAY_CLASSES = frozenset(('absent', 'private', 'public', 'loopback', 'link_local',
                             'unspecified', 'unknown'))


class NetworkInventoryError(ValueError):
    """Fixed categories only; never raw commands, output, values or paths."""


def _require(condition, category):
    if not condition:
        raise NetworkInventoryError(category)


def _identifier(value, maximum=200):
    _require(isinstance(value, str) and 0 < len(value) <= maximum
             and all(ord(char) >= 32 and not 0xd800 <= ord(char) <= 0xdfff for char in value),
             'network_inventory_identifier_invalid')
    return value


def _interface(value):
    _require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:@-]{1,128}', value) is not None,
             'network_inventory_interface_invalid')
    return value


def run_readonly(argv):
    """Bound owned diagnostics by time and bytes; never pass a Shell string."""
    _require(isinstance(argv, (tuple, list)) and argv
             and all(isinstance(item, str) and item and '\0' not in item for item in argv),
             'network_inventory_command_invalid')
    environment = dict(os.environ, LC_ALL='C', LANG='C')
    process = None
    selector = selectors.DefaultSelector()
    data = bytearray()
    try:
        process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=environment, shell=False)
        selector.register(process.stdout, selectors.EVENT_READ)
        expires = time.monotonic() + COMMAND_TIMEOUT
        while selector.get_map():
            remaining = expires - time.monotonic()
            if remaining <= 0:
                return {'status': 'timeout'}
            ready = selector.select(remaining)
            if not ready:
                return {'status': 'timeout'}
            for key, mask in ready:
                chunk = os.read(key.fileobj.fileno(), min(65536, MAX_COMMAND_BYTES + 1 - len(data)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                data.extend(chunk)
                if len(data) > MAX_COMMAND_BYTES:
                    return {'status': 'too_large'}
        remaining = max(0.001, expires - time.monotonic())
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return {'status': 'timeout'}
        if status != 0:
            return {'status': 'failed'}
        return {'status': 'ok', 'stdout': bytes(data)}
    except FileNotFoundError:
        return {'status': 'unavailable'}
    except Exception:
        return {'status': 'failed'}
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            if process.stdout is not None:
                process.stdout.close()


def _command(runner, argv, category, diagnostics):
    try:
        value = runner(tuple(argv))
        _require(isinstance(value, dict) and value.get('status') in
                 ('ok', 'timeout', 'too_large', 'failed', 'unavailable'), 'network_inventory_runner_invalid')
        if value['status'] != 'ok':
            diagnostics.append(category + '_' + value['status'])
            return None
        raw = value.get('stdout')
        _require(isinstance(raw, bytes) and len(raw) <= MAX_COMMAND_BYTES, 'network_inventory_runner_invalid')
        return raw.decode('utf8')
    except Exception:
        diagnostics.append(category + '_invalid')
        return None


def _json_rows(value, category, diagnostics, limit):
    if value is None:
        return None
    try:
        rows = json.loads(value)
        _require(isinstance(rows, list) and len(rows) <= limit
                 and all(isinstance(row, dict) for row in rows), 'network_inventory_rows_invalid')
        return rows
    except Exception:
        diagnostics.append(category + '_invalid')
        return None


def _split_nmcli(line):
    parts, current, escape = [], [], False
    for char in line:
        if escape:
            current.append(char)
            escape = False
        elif char == '\\':
            escape = True
        elif char == ':':
            parts.append(''.join(current))
            current = []
        else:
            current.append(char)
    if escape:
        raise ValueError('escape')
    parts.append(''.join(current))
    _require(len(parts) == 3, 'network_inventory_nmcli_invalid')
    return parts


def _medium(kind):
    return {'wifi': 'wifi', 'wifi-p2p': 'wifi', 'ethernet': 'ethernet', 'loopback': 'loopback',
            'bridge': 'virtual', 'tun': 'virtual', 'vpn': 'virtual', 'veth': 'virtual',
            'bond': 'virtual', 'wireguard': 'virtual', 'dummy': 'virtual'}.get(kind, 'unknown')


def _gateway(value):
    if value is None:
        return 'absent'
    try:
        address = ipaddress.ip_address(value.split('%', 1)[0])
        for flag, category in ((address.is_unspecified, 'unspecified'), (address.is_loopback, 'loopback'),
                               (address.is_link_local, 'link_local'), (address.is_private, 'private')):
            if flag:
                return category
        return 'public' if address.is_global else 'unknown'
    except Exception:
        return 'unknown'


class LinuxNetworkAdapter:
    """Known Linux metadata only; presence of tools is detected by execution."""
    name = 'linux-os-metadata-v1'

    def collect(self, runner):
        diagnostics = []
        links = _json_rows(_command(runner, ('ip', '-j', 'link', 'show'), 'links', diagnostics),
                           'links', diagnostics, MAX_INTERFACES)
        interfaces = {}
        for row in links or []:
            try:
                identity = _interface(row.get('ifname'))
                flags = row.get('flags', [])
                _require(isinstance(flags, list) and all(isinstance(flag, str) for flag in flags),
                         'network_inventory_flags_invalid')
                link = 'up' if row.get('operstate') == 'UP' else 'down' if row.get('operstate') == 'DOWN' else 'unknown'
                kind = 'loopback' if row.get('link_type') == 'loopback' else 'unknown'
                interfaces[identity] = {'id': identity, 'kind': kind, 'admin_up': 'UP' in flags,
                    'link_state': link, 'carrier_present': 'LOWER_UP' in flags, 'manager_connected': None}
            except Exception:
                diagnostics.append('interface_metadata_invalid')
        raw_manager = _command(runner, ('nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device', 'status'),
                               'network_manager', diagnostics)
        if raw_manager is not None:
            try:
                lines = raw_manager.splitlines()
                _require(len(lines) <= MAX_INTERFACES, 'network_inventory_nmcli_invalid')
                for line in lines:
                    identity, kind, state = _split_nmcli(line)
                    identity = _interface(identity)
                    if identity not in interfaces:
                        interfaces[identity] = {'id': identity, 'kind': 'unknown', 'admin_up': None,
                            'link_state': 'unknown', 'carrier_present': None, 'manager_connected': None}
                    interfaces[identity]['kind'] = _medium(kind)
                    interfaces[identity]['manager_connected'] = (True if state in ('connected', 'connected (externally)')
                        else False if state in ('disconnected', 'unavailable', 'unmanaged') else None)
            except Exception:
                diagnostics.append('network_manager_invalid')
        routes, queried = [], []
        for family, option in (('ipv4', '-4'), ('ipv6', '-6')):
            rows = _json_rows(_command(runner, ('ip', '-j', option, 'route', 'show', 'default'),
                                      'default_' + family, diagnostics), 'default_' + family, diagnostics, MAX_ROUTES)
            if rows is None:
                continue
            queried.append(family)
            for row in rows:
                try:
                    identity = _interface(row.get('dev'))
                    _require(row.get('dst', 'default') == 'default', 'network_inventory_not_default')
                    if identity not in interfaces:
                        interfaces[identity] = {'id': identity, 'kind': 'unknown', 'admin_up': None,
                            'link_state': 'unknown', 'carrier_present': None, 'manager_connected': None}
                    routes.append({'interface': identity, 'family': family,
                                   'gateway_class': _gateway(row.get('gateway')),
                                   'table': 'main' if row.get('table', 'main') in ('main', 254) else 'other'})
                except Exception:
                    diagnostics.append('default_route_metadata_invalid')
        return {'interfaces': list(interfaces.values()), 'default_routes': routes,
                'route_families_observed': queried, 'diagnostics': sorted(set(diagnostics))}


def _normalized(value):
    """Enforce a small privacy contract even for explicit trusted OS adapters."""
    _require(isinstance(value, dict) and set(value) ==
             {'interfaces', 'default_routes', 'route_families_observed', 'diagnostics'},
             'network_inventory_adapter_contract_invalid')
    _require(isinstance(value['interfaces'], list) and len(value['interfaces']) <= MAX_INTERFACES
             and isinstance(value['default_routes'], list) and len(value['default_routes']) <= MAX_ROUTES,
             'network_inventory_adapter_contract_invalid')
    interfaces, seen = [], set()
    for row in value['interfaces']:
        _require(isinstance(row, dict) and set(row) ==
                 {'id', 'kind', 'admin_up', 'link_state', 'carrier_present', 'manager_connected'},
                 'network_inventory_adapter_contract_invalid')
        identity = _interface(row['id'])
        _require(identity not in seen and row['kind'] in _KINDS and row['link_state'] in _STATES
                 and all(row[key] is None or type(row[key]) is bool
                         for key in ('admin_up', 'carrier_present', 'manager_connected')),
                 'network_inventory_adapter_contract_invalid')
        seen.add(identity)
        interfaces.append(dict(row))
    routes = []
    for row in value['default_routes']:
        _require(isinstance(row, dict) and set(row) == {'interface', 'family', 'gateway_class', 'table'}
                 and row['interface'] in seen and row['family'] in ('ipv4', 'ipv6')
                 and row['gateway_class'] in _GATEWAY_CLASSES and row['table'] in ('main', 'other'),
                 'network_inventory_adapter_contract_invalid')
        if row not in routes:
            routes.append(dict(row))
    families = value['route_families_observed']
    _require(isinstance(families, list) and all(family in ('ipv4', 'ipv6') for family in families),
             'network_inventory_adapter_contract_invalid')
    diagnostics = value['diagnostics']
    _require(isinstance(diagnostics, list) and len(diagnostics) <= 64
             and all(isinstance(item, str) and re.fullmatch('[a-z][a-z0-9_]{0,79}', item) for item in diagnostics),
             'network_inventory_adapter_contract_invalid')
    return {'interfaces': sorted(interfaces, key=lambda row: row['id']),
            'default_routes': sorted(routes, key=lambda row: (row['interface'], row['family'], row['table'], row['gateway_class'])),
            'route_families_observed': sorted(set(families)), 'diagnostics': sorted(set(diagnostics))}


def inspect_network(node_id, adapter=None, system=None, runner=None, environ=None, clock=None,
                    freshness_seconds=30):
    """Discover candidates only. Caller explicitly publishes/manages them.

    Each observation is local/declared, not independent Registry verification.
    Freshness is bounded local observation time, not an interface/route lease.
    Existing network spend/billing is unknown; no new paid resource is provisioned.
    """
    node_id = _identifier(node_id)
    _require(type(freshness_seconds) in (int, float) and math.isfinite(freshness_seconds)
             and 0 < freshness_seconds <= 300, 'network_inventory_freshness_invalid')
    clock = clock or time.time
    started = clock()
    _require(type(started) in (int, float) and math.isfinite(started) and started >= 0,
             'network_inventory_clock_invalid')
    selected_system = system if system is not None else platform.system()
    _require(isinstance(selected_system, str) and bool(selected_system), 'network_inventory_system_invalid')
    if selected_system not in ('Linux', 'Windows', 'Darwin', 'FreeBSD', 'OpenBSD', 'NetBSD'):
        selected_system = 'other'
    if adapter is None and selected_system == 'Linux':
        adapter = LinuxNetworkAdapter()
    if adapter is None:
        raw = {'interfaces': [], 'default_routes': [], 'route_families_observed': [],
               'diagnostics': ['os_adapter_not_configured']}
        adapter_name, supported = None, False
    else:
        adapter_name = _identifier(getattr(adapter, 'name', None), 128)
        _require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}', adapter_name) is not None,
                 'network_inventory_adapter_invalid')
        _require(callable(getattr(adapter, 'collect', None)), 'network_inventory_adapter_invalid')
        try:
            raw = adapter.collect(runner or run_readonly)
        except Exception:
            raw = {'interfaces': [], 'default_routes': [], 'route_families_observed': [],
                   'diagnostics': ['adapter_failed']}
        supported = True
    normalized = _normalized(raw)
    observed = clock()
    _require(type(observed) in (int, float) and math.isfinite(observed) and observed >= started,
             'network_inventory_clock_invalid')
    environment = os.environ if environ is None else environ
    # Membership only: neither proxy URLs nor other environment values are read.
    proxy_names = sorted(name for name in _PROXY_NAMES if name in environment)
    candidates = []
    for interface in normalized['interfaces']:
        routes = [row for row in normalized['default_routes'] if row['interface'] == interface['id']]
        if not routes:
            continue
        digest = hashlib.sha256(json.dumps([FORMAT, node_id, interface['id']], ensure_ascii=False,
            separators=(',', ':')).encode('utf8')).hexdigest()
        families = sorted(set(row['family'] for row in routes))
        candidates.append({'id': 'network-egress-' + digest, 'kind': 'network.egress',
            'spec': {'node': node_id, 'interface': interface['id'], 'medium': interface['kind'],
                'observed_default_route_families': families, 'new_spend_minor': 0,
                'existing_network_billing_verified': False, 'internet_reachability_verified': False,
                'managed_use_requires_explicit_enrollment': True, 'native_features_restricted': False},
            'observations': [{'metric': 'default_route_present', 'value': True, 'unit': 'boolean',
                'source': 'local_os_metadata', 'verification': 'declared', 'sample_time': started,
                'deadline': started + freshness_seconds,
                'evidence': {'scope': {'node': node_id, 'interface': interface['id']},
                    'workload': {'route_families': families}, 'measurement_method': 'read_only_os_route_metadata',
                    'independent_verification': False, 'internet_probe_performed': False}}]})
    return dict(normalized, format=FORMAT, node=node_id, system=selected_system, adapter=adapter_name,
        adapter_configured=supported, sample_time=started, completed_time=observed,
        deadline=started + freshness_seconds, snapshot_fresh=observed < started + freshness_seconds,
        collection_seconds=observed - started, proxy_environment_names=proxy_names,
        proxy_endpoint_values_disclosed=False, proxy_application_verified=False,
        candidates=candidates, capabilities_advertised=False, allocation_authorized=False,
        internet_reachability_verified=False, independent_performance_verified=False,
        independent_physical_host_verified=False, independent_verification=False,
        new_paid_resource_provisioned=False, existing_network_billing_verified=False,
        native_features_restricted=False, network_configuration_changed=False,
        limitations=['main_default_routes_only', 'no_policy_routing_or_dns_verification',
                     'no_active_connectivity_or_performance_probe', 'local_metadata_is_not_physical_attestation'])
