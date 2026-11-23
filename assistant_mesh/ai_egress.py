"""Mesh-owned inference egress, applied only to managed model processes."""
from urllib.parse import urlsplit

from .config import private_json


PROXY_NAMES = frozenset(name for root in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')
                        for name in (root, root.upper()))
EGRESS_ID = 'amita-ai-vps-v1'


def model_network_env(config, inherited):
    """Return effective proxy variables without editing the parent environment."""
    path = config.get('network_env_file')
    mesh = config.get('egress_id') == EGRESS_ID
    if mesh and not path:
        raise ValueError('mesh_ai_egress_binding_required')

    configured = private_json(path) if path else {}
    if (not isinstance(configured, dict) or set(configured) - PROXY_NAMES
            or not all(isinstance(value, str) for value in configured.values())):
        raise ValueError('invalid_network_environment')

    if mesh:
        url = configured.get('HTTPS_PROXY')
        try:
            proxy = urlsplit(url or '')
            valid = (proxy.scheme == 'http' and proxy.hostname == '127.0.0.1'
                     and proxy.port is not None and proxy.port > 1023
                     and proxy.netloc == '127.0.0.1:' + str(proxy.port)
                     and not (proxy.path or proxy.query or proxy.fragment))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('mesh_ai_egress_local_transport_required')
        network = {name: url for name in PROXY_NAMES if name.lower() != 'no_proxy'}
        network.update(NO_PROXY='localhost,127.0.0.1,::1', no_proxy='localhost,127.0.0.1,::1')
        return network

    network = {name: inherited[name] for name in PROXY_NAMES if name in inherited}
    network.update(configured)
    return network
