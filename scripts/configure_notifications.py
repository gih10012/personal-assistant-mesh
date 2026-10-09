"""Explicit owner enrollment for an optional node-to-owner text relay.

Only private Mesh configuration is changed. Native Shell, networking and
tools are not inspected or restricted. Existing tokens/recipients are not
copied, printed, reset, or discovered. Existing intents are never retargeted.
"""
import argparse
import json

from assistant_mesh.config import private_json
from assistant_mesh.networking import identifier
from scripts.configure_nodes import update


def _caps(peer):
    caps = peer.get('capabilities', [])
    if not isinstance(caps, list) or not all(isinstance(cap, str) and cap for cap in caps):
        raise ValueError('invalid_notification_capabilities')
    return sorted(set(caps + ['owner.notify']))


def grant_cloud_peer(server_path, peer, dry_run=False):
    """Grant only a specific already enrolled agent_peer, never operator."""
    peer = identifier(peer)
    server = private_json(server_path)
    policy = server.get('notification_policy', {'mode': 'channel'})
    if (not server.get('ilink_account') or not isinstance(policy, dict) or
            policy.get('mode') != 'channel'):
        raise ValueError('owner_notification_channel_not_configured')
    bound = [entry for entry in server.get('peers', [])
             if entry.get('role') == 'agent_peer' and entry.get('node') == peer]
    if len(bound) != 1:
        raise ValueError('notification_peer_not_uniquely_enrolled')
    caps = _caps(bound[0])
    changed = bound[0].get('capabilities') != caps
    bound[0]['capabilities'] = caps
    if changed and not dry_run:
        update(server_path, server)
    return {'operation': 'grant-cloud-peer', 'peer': peer, 'changed': changed,
            'applied': not dry_run, 'native_tools_intercepted': False,
            'additional_spend_enabled': False}


def enable_node_relay(node_path, peer, authority, dry_run=False):
    peer, authority = identifier(peer), identifier(authority)
    node = private_json(node_path)
    node_id = identifier(node['node_id'])
    if peer == node_id or node.get('start_server', True) is not True:
        raise ValueError('notification_relay_requires_owned_local_node')
    peers = [entry for entry in node.get('peers', []) if entry.get('node') == peer]
    if len(peers) != 1 or peers[0].get('authority', peer) != authority:
        raise ValueError('notification_relay_peer_not_enrolled')
    # Validate the existing client is private, without reading/copying tokens.
    private_json(peers[0]['client_config'])
    server_path, worker_path = node['local_server_config'], node['local_worker_config']
    server, worker = private_json(server_path), private_json(worker_path)
    if server.get('node_id') != node_id or worker.get('node_id') != node_id or server.get('ilink_account'):
        raise ValueError('notification_relay_local_identity_conflict')
    route = {'peer': peer, 'authority': authority}
    policy = server.get('notification_policy')
    if policy is not None and (not isinstance(policy, dict) or
            set(policy) - {'mode', 'owner_relay'} or
            policy.get('mode') not in ('private', 'channel') or
            (policy.get('owner_relay') is not None and policy['owner_relay'] != route)):
        raise ValueError('notification_relay_existing_route_conflict')
    bound = [entry for entry in server.get('peers', [])
             if entry.get('role') == 'worker' and entry.get('node') == node_id]
    if len(bound) != 1:
        raise ValueError('notification_worker_binding_conflict')
    if _caps(bound[0]) != _caps(worker):
        raise ValueError('notification_worker_binding_conflict')
    new_policy = {'mode': 'private', 'owner_relay': route}
    caps = _caps(bound[0])
    server_changed = policy != new_policy or bound[0].get('capabilities') != caps
    worker_changed = worker.get('capabilities') != caps
    server['notification_policy'] = new_policy
    bound[0]['capabilities'] = caps
    worker['capabilities'] = caps
    # Config reload is explicit. Partial interruption fails closed for this
    # managed notify capability, never changes a native tool or old intent.
    if not dry_run:
        if server_changed:
            update(server_path, server)
        if worker_changed:
            update(worker_path, worker)
    return {'operation': 'enable-node-relay', 'node': node_id, 'peer': peer,
            'authority': authority, 'changed': server_changed or worker_changed,
            'applied': not dry_run, 'historical_notifications_enrolled': False,
            'native_tools_intercepted': False, 'additional_spend_enabled': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    cloud = sub.add_parser('grant-cloud-peer')
    cloud.add_argument('--server-config', required=True)
    cloud.add_argument('--peer', required=True)
    cloud.add_argument('--dry-run', action='store_true')
    local = sub.add_parser('enable-node-relay')
    local.add_argument('--node-config', required=True)
    local.add_argument('--peer', required=True)
    local.add_argument('--authority', required=True)
    local.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.command is None:
        parser.error('a command is required')
    if args.command == 'grant-cloud-peer':
        result = grant_cloud_peer(args.server_config, args.peer, args.dry_run)
    else:
        result = enable_node_relay(args.node_config, args.peer, args.authority, args.dry_run)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
