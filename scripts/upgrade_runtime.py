"""Explicit deployment migration after owner's Codex-first/full-host request.

Backs up private configuration once, never prints its contents or tokens.
"""
import argparse
import json
import shutil
from pathlib import Path

from assistant_mesh.config import private_json


def update(path, transform):
    path = Path(path)
    value = private_json(path)
    backup = path.with_suffix(path.suffix + '.before-model-led')
    if not backup.exists():
        shutil.copy2(str(path), str(backup))
    transform(value)
    # Administrative config migration, not public source generation.
    with path.open('w', encoding='utf8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    path.chmod(0o600)


def worker(value):
    value['capabilities'] = sorted(set(value.get('capabilities', [])) | {'agent', 'leader', 'codex.native'})
    value['codex'].update(sandbox='danger-full-access', approval_policy='never', model_policy='catalog-first')
    value['turn_timeout'] = 3600


def server(value):
    for peer in value['peers']:
        if peer.get('node'):
            peer['capabilities'] = sorted(set(peer.get('capabilities', [])) | {'agent', 'codex.native'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    root = Path(args.state)
    for name in ('laptop-worker.json', 'cloud-worker.json'):
        if (root / name).exists():
            update(root / name, worker)
    if (root / 'server.json').exists():
        update(root / 'server.json', server)
    print(json.dumps({'configuration_upgraded': True, 'private_backup_created': True}))
