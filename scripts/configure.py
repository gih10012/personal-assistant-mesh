#!/usr/bin/env python3
"""Prepare private deployment configuration. Never prints secret material."""
import argparse
import json
import os
import secrets
import shutil
from pathlib import Path


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        raise ValueError('Refusing to overwrite existing configuration: ' + str(path))
    with path.open('x', encoding='utf8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    path.chmod(0o600)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', required=True)
    parser.add_argument('--remote-state', required=True)
    parser.add_argument('--remote-code', required=True)
    parser.add_argument('--remote-codex', required=True)
    parser.add_argument('--remote-auth-home', required=True)
    parser.add_argument('--local-auth-home', required=True)
    args = parser.parse_args()
    root, remote = Path(args.state), Path(args.remote_state)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    for name in ('operator', 'cloud', 'laptop'):
        path = root / (name + '.token')
        if not path.exists():
            path.write_text(secrets.token_hex(32) + '\n')
            path.chmod(0o600)
    common = {'control_url': 'http://127.0.0.1:17680'}
    save(root / 'server.json', {'port': 17680, 'database': str(remote / 'ledger.sqlite'),
        'ilink_account': str(remote / 'ilink-account.json'), 'budget': {'monthly_minor': 0, 'automatic_minor': 0},
        'peers': [
            {'role': 'operator', 'token_file': str(remote / 'operator.token')},
            {'role': 'worker', 'node': 'cloud', 'token_file': str(remote / 'cloud.token'), 'score': 5,
             'capabilities': ['leader', 'codex.readonly']},
            {'role': 'worker', 'node': 'laptop', 'token_file': str(remote / 'laptop.token'), 'score': 10,
             'capabilities': ['leader', 'codex.readonly']}]})
    save(root / 'cloud-worker.json', dict(common, token_file=str(remote / 'cloud.token'), node_id='cloud',
        capabilities=['leader', 'codex.readonly'], codex={'executable': args.remote_codex,
            'auth_home': args.remote_auth_home, 'workspace': str(remote / 'workspace')}))
    save(root / 'laptop-worker.json', dict(common, token_file=str(root / 'laptop.token'), node_id='laptop',
        capabilities=['leader', 'codex.readonly'], codex={'executable': '/usr/local/bin/codex',
            'auth_home': args.local_auth_home, 'workspace': str(root / 'workspace')}))
    save(root / 'operator.json', dict(common, token_file=str(root / 'operator.token')))
    save(root / 'cloud-operator.json', dict(common, token_file=str(remote / 'operator.token')))
    (root / 'workspace').mkdir(mode=0o700, exist_ok=True)
    print(json.dumps({'prepared': True, 'state_directory': str(root), 'budget_enabled': False}))


if __name__ == '__main__':
    main()
