"""Explicit owner-enrolled two-node topology; writes only private deployment data.

No network discovery, no purchases, no host-key changes, no credential printing.
The cloud apply reads CURRENT configs on that machine, not a stale local mirror.
"""
import argparse
import datetime
import json
import os
import secrets
import shutil
import tempfile
from pathlib import Path

from assistant_mesh.config import private_json


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('x', encoding='utf8') as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)


def secret(path):
    path = Path(path)
    with path.open('x', encoding='utf8') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(secrets.token_hex(32) + '\n')


def update(path, value):
    stamp = datetime.datetime.utcnow().strftime('.before-node-%Y%m%dT%H%M%S')
    shutil.copy2(str(path), str(path) + stamp)
    descriptor, temporary = tempfile.mkstemp(prefix='node-config-', dir=str(Path(path).parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, str(path))
    except BaseException:
        if Path(temporary).exists():
            Path(temporary).unlink()
        raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare-laptop', 'apply-cloud'])
    parser.add_argument('--state', required=True)
    parser.add_argument('--source-worker')
    parser.add_argument('--cloud-state')
    parser.add_argument('--server-config')
    parser.add_argument('--worker-config')
    args = parser.parse_args()
    root = Path(args.state).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    if args.command == 'prepare-laptop':
        if not args.source_worker or not args.cloud_state:
            parser.error('prepare-laptop requires source-worker and cloud-state')
        source = private_json(args.source_worker)
        cloud = Path(args.cloud_state)
        for name in ('local-worker', 'local-operator', 'cloud-to-laptop', 'laptop-to-cloud'):
            secret(root / (name + '.token'))
        caps = ['agent', 'mesh.node:laptop', 'codex.native', 'a2a.send']
        server = {'node_id': 'laptop', 'port': 17681, 'database': str(root / 'ledger.sqlite'),
                  'budget': {'monthly_minor': 0, 'automatic_minor': 0},
                  'network_peers': {'cloud': {'send_allowed': True}}, 'peers': [
                      {'role': 'operator', 'token_file': str(root / 'local-operator.token')},
                      {'role': 'worker', 'node': 'laptop', 'capabilities': caps, 'token_file': str(root / 'local-worker.token')},
                      {'role': 'agent_peer', 'node': 'cloud', 'capabilities': ['a2a.delegate', 'a2a.report'],
                       'token_file': str(root / 'cloud-to-laptop.token')} ]}
        worker = {'node_id': 'laptop', 'control_url': 'http://127.0.0.1:17681', 'capabilities': caps,
                  'token_file': str(root / 'local-worker.token'), 'codex': dict(source['codex'])}
        for option in ('codex_accounts', 'pi', 'turn_timeout', 'interaction_timeout'):
            if option in source:
                worker[option] = source[option]
        # Autonomy is an additional node-owned native conversation, not a
        # competing process attached to the continuous global Leader thread.
        worker['codex']['workspace'] = str(root / 'workspace')
        (root / 'workspace').mkdir(mode=0o700, exist_ok=True)
        save(root / 'server.json', server)
        save(root / 'worker.json', worker)
        save(root / 'operator.json', {'control_url': 'http://127.0.0.1:17681', 'token_file': str(root / 'local-operator.token')})
        save(root / 'cloud-client.json', {'control_url': 'http://127.0.0.1:17680', 'token_file': str(root / 'laptop-to-cloud.token')})
        save(root / 'node.json', {'node_id': 'laptop', 'local_server_config': str(root / 'server.json'),
             'local_worker_config': str(root / 'worker.json'), 'auto_maintenance': True, 'poll_interval': 1,
             'reconcile_remote_children': True, 'peers': [
                 {'node': 'cloud', 'authority': 'cloud', 'client_config': str(root / 'cloud-client.json'), 'report_results': True}]})
        save(root / 'cloud-client-for-laptop.json', {'control_url': 'http://127.0.0.1:17681',
                                                   'token_file': str(cloud / 'cloud-to-laptop.token')})
        save(root / 'node-cloud.json', {'node_id': 'cloud', 'local_server_config': str(cloud / 'server.json'),
             'local_worker_config': str(cloud / 'cloud-worker.json'), 'start_server': False, 'start_worker': False,
             'auto_maintenance': True, 'poll_interval': 1, 'reconcile_remote_children': True,
             'peers': [{'node': 'laptop', 'authority': 'laptop', 'client_config': str(cloud / 'laptop-client.json'), 'report_results': True}]})
    else:
        if not args.server_config or not args.worker_config:
            parser.error('apply-cloud requires server-config and worker-config')
        server = private_json(args.server_config)
        worker = private_json(args.worker_config)
        if worker.get('node_id') != 'cloud':
            raise ValueError('cloud_worker_identity_mismatch')
        server['node_id'] = 'cloud'
        server.setdefault('network_peers', {})['laptop'] = {'send_allowed': True}
        tokens = [root / 'cloud-to-laptop.token', root / 'laptop-to-cloud.token']
        for token in tokens:
            from assistant_mesh.config import read_secret
            read_secret(token)  # private regular owned file, never output
        role = {'role': 'agent_peer', 'node': 'laptop', 'capabilities': ['a2a.delegate', 'a2a.report'],
                'token_file': str(root / 'laptop-to-cloud.token')}
        bound = [p for p in server['peers'] if p.get('role') == 'agent_peer' and p.get('node') == 'laptop']
        if bound and bound != [role]:
            raise ValueError('existing_peer_binding_conflict')
        if not bound:
            server['peers'].append(role)
        for peer in server['peers']:
            if peer.get('role') == 'worker' and peer.get('node') == 'cloud':
                peer['capabilities'] = sorted(set(peer.get('capabilities', []) + ['agent', 'mesh.node:cloud', 'a2a.send']))
            elif peer.get('role') == 'worker' and peer.get('node') == 'laptop':
                peer['capabilities'] = sorted(set(peer.get('capabilities', []) + ['a2a.send']))
        worker['capabilities'] = sorted(set(worker.get('capabilities', []) + ['agent', 'mesh.node:cloud', 'a2a.send']))
        update(args.server_config, server)
        update(args.worker_config, worker)
    print(json.dumps({'prepared': True, 'node': 'laptop' if args.command == 'prepare-laptop' else 'cloud',
                      'private_state': str(root), 'additional_spend_enabled': False}))


if __name__ == '__main__':
    main()
