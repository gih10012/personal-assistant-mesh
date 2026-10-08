"""Private deployment maintenance; never emit token/config/message contents."""
import argparse
import datetime
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from assistant_mesh.config import private_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['audit-effects', 'set-cloud-runtime'])
    parser.add_argument('--executable')
    parser.add_argument('--sha256')
    args = parser.parse_args()
    path = Path(args.config)
    value = private_json(path)
    if args.command == 'audit-effects':
        db = sqlite3.connect('file:' + value['database'] + '?mode=ro', uri=True)
        uncertain = []
        for task_id, status, checkpoint in db.execute('SELECT id,status,checkpoint FROM tasks'):
            if status not in ('completed', 'failed') and json.loads(checkpoint).get('side_effect_started'):
                uncertain.append({'id': task_id, 'status': status})
        db.close()
        print(json.dumps({'unresolved_effects': uncertain}))
        return
    if not args.executable or not args.sha256:
        parser.error('runtime update requires executable and verified sha256')
    executable = Path(args.executable)
    if executable.is_symlink() or not executable.is_file():
        raise ValueError('runtime_must_be_regular_file')
    digest = hashlib.sha256()
    with executable.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != args.sha256:
        raise ValueError('runtime_integrity_mismatch')
    suffix = datetime.datetime.utcnow().strftime('.before-runtime-%Y%m%dT%H%M%S')
    backup = str(path) + suffix
    shutil.copy2(str(path), backup)
    value['codex']['executable'] = str(executable)
    value['codex']['model_policy'] = 'catalog-first'
    value['codex'].pop('model', None)
    value['codex']['native_memories'] = True
    with path.open('w', encoding='utf8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    path.chmod(0o600)
    print(json.dumps({'configuration_updated': True, 'backup_created': True, 'integrity_verified': True}))


if __name__ == '__main__':
    main()
