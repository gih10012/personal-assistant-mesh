"""Read-only device commands on the authenticated target, without a task."""
import os
from pathlib import Path
import subprocess
import time


ROUTE = '/v1/mesh/device-query'
FORMAT = 'amita-device-query/1'
QUERIES = frozenset(('disk', 'battery'))


def disk_usage():
    output = subprocess.check_output(['/usr/bin/df', '-B1', '--output=size,used,avail', '--', str(Path.home())],
        env=dict(os.environ, LC_ALL='C'), timeout=3, stderr=subprocess.DEVNULL, text=True)
    if len(output) > 4096:
        raise ValueError('device_disk_response_invalid')
    rows = output.splitlines()
    if len(rows) != 2:
        raise ValueError('device_disk_response_invalid')
    columns = rows[-1].split()
    if len(columns) != 3:
        raise ValueError('device_disk_response_invalid')
    total, used, available = map(int, columns)
    if min(total, used, available) < 0 or used > total or available > total:
        raise ValueError('device_disk_response_invalid')
    return {'filesystem': 'home', 'total_bytes': total, 'used_bytes': used, 'available_bytes': available}


def batteries():
    result = []
    for path in sorted(Path('/sys/class/power_supply').glob('*')):
        try:
            if (path / 'type').read_text().strip() != 'Battery':
                continue
            present = path / 'present'
            if present.exists() and present.read_text().strip() == '0':
                continue
        except OSError:
            continue
        try:
            capacity = int((path / 'capacity').read_text())
        except (OSError, ValueError):
            capacity = -1
        try:
            status = (path / 'status').read_text().strip()
        except OSError:
            status = 'Unknown'
        result.append({'percent': capacity if 0 <= capacity <= 100 else None,
                       'state': status if status in ('Full', 'Charging', 'Discharging', 'Not charging') else 'Unknown'})
    return {'batteries': result}


def inspect_device(node_id, payload, peer):
    caps = peer.get('capabilities', [])
    authorized = (peer.get('role') == 'operator' or peer.get('role') in ('worker', 'agent_peer')
        and peer.get('node') and isinstance(caps, list) and 'device.query' in caps)
    if not authorized or not isinstance(node_id, str) or not node_id:
        raise PermissionError('device_query_owner_capability_required')
    if (not isinstance(payload, dict) or set(payload) != {'query'}
            or not isinstance(payload['query'], str) or payload['query'] not in QUERIES):
        raise ValueError('device_query_expected_disk_or_battery')
    query = payload['query']
    try:
        value = disk_usage() if query == 'disk' else batteries()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError('device_query_unavailable') from None
    return {'format': FORMAT, 'node_id': node_id, 'query': query, 'sampled_at': time.time(), 'value': value}
