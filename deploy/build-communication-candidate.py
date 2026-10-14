"""NEW frozen original receiver + communication sidecars, no data/IO/install.

Reuse the exact earlier minimal builder; do not copy HEAD Store/Worker. This
bundle can serve an independent broker AND the old task-API producer adapter.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path

MODULES = ('communication_store.py', 'communication_migration.py',
           'communication_inbox.py', 'communication_broker.py',
           'communication_producer.py', 'channel_checkpoint.py')


def build(base_root, destination, support_root=None):
    support = Path(support_root or Path(__file__).resolve().parents[1])
    spec = importlib.util.spec_from_file_location('_exact_legacy_receiver_builder',
        str(support / 'deploy' / 'build-legacy-fenced-receiver.py'))
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    original = legacy.build(base_root, destination, support)
    target = Path(destination)
    for name in MODULES:
        data = legacy._regular_bytes(support / 'assistant_mesh' / name)
        compile(data, '<communication-sidecar-source>', 'exec')
        with (target / 'assistant_mesh' / name).open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted((target / 'assistant_mesh').glob('*.py'))}
    report = {'protocol': 'mesh-communication-source-candidate/1',
        'base_manifest_sha256': original['base_manifest_sha256'],
        'original_modules': original['original_modules'],
        'unchanged_original_modules': original['unchanged_original_modules'],
        'changed_original_modules': original['changed_original_modules'],
        'added_communication_modules': list(MODULES), 'source_sha256': files,
        'production_installed': False, 'ledger_opened': False,
        'carrier_started': False, 'authority_migrated': False, 'grant_authorized': False}
    with (target / 'communication-candidate.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, sort_keys=True)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description='Prepare frozen communication SOURCE, not data migration')
    parser.add_argument('--base-root', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--support-root')
    args = parser.parse_args()
    report = build(args.base_root, args.destination, args.support_root)
    print(json.dumps({key: value for key, value in report.items() if key != 'source_sha256'}, sort_keys=True))


if __name__ == '__main__':
    main()
