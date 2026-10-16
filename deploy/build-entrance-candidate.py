"""Build a NEW frozen receiver source + four additive entrance modules only.

No production path/ledger/credentials are opened, installed, started or copied.
The exact previously reviewed legacy builder preserves 38/40 original modules.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path


MODULES = ('entrance.py', 'entrance_failure.py', 'entrance_runtime.py', 'entrance_media.py')


def build(base_root, destination, support_root=None):
    support = Path(support_root or Path(__file__).resolve().parents[1])
    spec = importlib.util.spec_from_file_location('_entrance_legacy_builder',
        str(support / 'deploy' / 'build-legacy-fenced-receiver.py'))
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    base = legacy.build(base_root, destination, support)
    root = Path(destination)
    for name in MODULES:
        data = legacy._regular_bytes(support / 'assistant_mesh' / name)
        compile(data, '<entrance-addon-source>', 'exec')
        with (root / 'assistant_mesh' / name).open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
    sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted((root / 'assistant_mesh').glob('*.py'))}
    report = {'protocol': 'mesh-entrance-source-candidate/1',
        'base_manifest_sha256': base['base_manifest_sha256'],
        'original_modules': base['original_modules'],
        'unchanged_original_modules': base['unchanged_original_modules'],
        'changed_original_modules': base['changed_original_modules'],
        'added_entrance_modules': list(MODULES), 'source_sha256': sources,
        'production_installed': False, 'ledger_opened': False, 'carrier_started': False,
        'new_model_turn_started': False, 'old_input_replayed': False}
    with (root / 'entrance-candidate.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, sort_keys=True)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description='Prepare entrance frozen SOURCE only, not a service install')
    parser.add_argument('--base-root', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--support-root')
    args = parser.parse_args()
    output = build(args.base_root, args.destination, args.support_root)
    print(json.dumps({key: value for key, value in output.items() if key != 'source_sha256'}, sort_keys=True))


if __name__ == '__main__':
    main()
