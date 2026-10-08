"""Private deployment maintenance; never emit token/config/message contents."""
import argparse
import datetime
import json
import shutil
import sqlite3
from pathlib import Path

from assistant_mesh.config import private_json
from scripts.upgrade_runtime import verify_installation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['audit-effects', 'set-cloud-runtime', 'set-account-pool'])
    parser.add_argument('--executable')
    parser.add_argument('--sha256')
    parser.add_argument('--package-dir', help='complete package installed by scripts.upgrade_runtime')
    parser.add_argument('--version')
    parser.add_argument('--target')
    parser.add_argument('--auth-home', action='append')
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
    if args.command == 'set-account-pool':
        if not args.auth_home:
            parser.error('account pool requires explicit authorized auth-home paths')
        from assistant_mesh.config import discover_codex_auth
        homes = [discover_codex_auth(home, strict=True) for home in args.auth_home]
        if len(set(homes)) != len(homes):
            raise ValueError('duplicate_account_home')
        backup = str(path) + datetime.datetime.utcnow().strftime('.before-accounts-%Y%m%dT%H%M%S')
        shutil.copy2(str(path), backup)
        value['codex_accounts'] = [{'auth_home': home} for home in homes]
        with path.open('w', encoding='utf8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        path.chmod(0o600)
        print(json.dumps({'account_pool_configured': True, 'authorized_profiles': len(homes), 'backup_created': True,
                          'communication_account_changed': False, 'effectful_turns_auto_replayed': False}))
        return
    if not args.package_dir or not args.sha256:
        parser.error('runtime update requires complete package-dir and whole-artifact sha256')
    package = verify_installation(args.package_dir, args.sha256, args.version, args.target)
    executable = Path(package['executable'])
    if args.executable is not None and Path(args.executable) != executable:
        raise ValueError('runtime_executable_not_from_verified_package')
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
    print(json.dumps({'configuration_updated': True, 'backup_created': True, 'integrity_verified': True,
                      'complete_package_verified': True, 'version': package['version'], 'target': package['target']}))


if __name__ == '__main__':
    main()
