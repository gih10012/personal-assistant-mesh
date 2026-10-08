import argparse
import json
import os

from .config import discover_codex_auth, private_json


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Durable personal-assistant mesh')
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['serve', 'worker', 'recovery', 'status', 'submit', 'notify', 'doctor', 'probe-codex'])
    parser.add_argument('--text')
    parser.add_argument('--request-id')
    args = parser.parse_args()
    config = private_json(args.config)
    if args.command == 'serve':
        from .server import serve
        return serve(config)
    if args.command == 'worker':
        from .worker import Worker
        return Worker(config).run()
    if args.command == 'recovery':
        from .recovery import run
        return run(config)
    if args.command in ('status', 'submit', 'notify'):
        from .worker import Client
        client = Client(config)
        if args.command == 'status':
            value = client.request('/v1/status')
        elif args.command == 'submit':
            value = client.request('/v1/tasks', {'input': args.text})
        else:
            if not args.request_id:
                parser.error('notify requires --request-id')
            value = client.request('/v1/notify', {'text': args.text, 'request_id': args.request_id})
    elif args.command == 'doctor':
        try:
            root = discover_codex_auth(config.get('codex', {}).get('auth_home'))
            value = {'codex_auth_found': True, 'source': root, 'auth_live_verified': False}
        except ValueError:
            value = {'codex_auth_found': False, 'action': 'Ask owner to authenticate Codex; never request plaintext tokens.'}
    else:
        from .codex import Codex
        with Codex(config['codex']) as agent:
            value = {'account': agent.account(), 'models': [m.get('id') for m in agent.models()]}
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
