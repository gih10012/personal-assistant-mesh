import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlencode

from .config import discover_codex_auth, private_json


def resource_payload(path, action=None):
    """Owned private JSON outside git worktrees; secrets never enter argv."""
    arguments = {} if path is None else private_json(path)
    if path is not None:
        selected = Path(path).expanduser().resolve()
        if any((parent / '.git').exists() for parent in selected.parents):
            raise ValueError('payload_file_must_be_outside_git_worktrees')
    if not isinstance(arguments, dict):
        raise ValueError('resource_payload_requires_object')
    if 'action' in arguments:
        if action is not None and action != arguments['action']:
            raise ValueError('resource_action_conflict')
        payload = arguments
    else:
        if not action:
            raise ValueError('resource_requires_action')
        payload = {'action': action, 'arguments': arguments}
    if set(payload) - {'action', 'arguments'} or not isinstance(payload.get('action'), str) or not isinstance(payload.get('arguments', {}), dict):
        raise ValueError('invalid_resource_payload')
    return payload


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Durable personal-assistant mesh')
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['serve', 'worker', 'recovery', 'status', 'submit', 'notify', 'doctor', 'probe-codex',
                                          'resources', 'resource', 'capability-events', 'resource-graph', 'node',
                                          'mesh-hello', 'mesh-links', 'mesh-local', 'mesh-queue', 'mesh-task', 'mesh-delegate'])
    parser.add_argument('--text')
    parser.add_argument('--request-id')
    parser.add_argument('--action', help='Resource API method, not a native terminal restriction')
    parser.add_argument('--payload-file', help='Owned 0600 JSON outside git worktrees; no credential values in argv')
    parser.add_argument('--kind')
    parser.add_argument('--principal', help='Discovery filter only, never an actor override')
    parser.add_argument('--include-unavailable', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--after', type=int)
    parser.add_argument('--id', help='A2A message ID for an owner-bound task read')
    parser.add_argument('--task-id')
    parser.add_argument('--epoch', type=int)
    parser.add_argument('--call-id')
    args = parser.parse_args()
    config = private_json(args.config)
    if args.command == 'serve':
        from .server import serve
        return serve(config)
    if args.command == 'worker':
        from .worker import Worker
        return Worker(config, config_path=str(Path(args.config).expanduser().resolve())).run()
    if args.command == 'node':
        from .node import run
        return run(config, str(Path(args.config).expanduser().resolve()))
    if args.command == 'recovery':
        from .recovery import run
        return run(config)
    if args.command in ('status', 'submit', 'notify', 'resources', 'resource', 'capability-events', 'resource-graph',
                        'mesh-hello', 'mesh-links', 'mesh-local', 'mesh-queue', 'mesh-task', 'mesh-delegate'):
        from .worker import Client
        client = Client(config)
        if args.command in ('mesh-hello', 'mesh-links'):
            value = client.request('/v1/mesh/' + args.command[5:])
        elif args.command == 'mesh-task':
            if not args.id:
                parser.error('mesh-task requires --id')
            value = client.request('/v1/mesh/task?' + urlencode({'id': args.id}))
        elif args.command in ('mesh-local', 'mesh-queue', 'mesh-delegate'):
            if not args.payload_file:
                parser.error(args.command + ' requires --payload-file')
            # Reuse the same regular/owned/0600 and outside-worktree gate,
            # without interpreting model text as sender identity or permission.
            wrapper = resource_payload(args.payload_file, 'mesh-payload')
            payload = wrapper['arguments']
            if args.command == 'mesh-delegate':
                if not args.task_id or args.epoch is None or not args.call_id:
                    parser.error('mesh-delegate requires task-id, epoch, call-id')
                if set(payload) != {'peer', 'arguments'}:
                    parser.error('mesh-delegate payload requires peer and arguments')
                payload = dict(payload, task_id=args.task_id, epoch=args.epoch,
                               call_id=args.task_id + ':' + args.call_id)
            value = client.request('/v1/mesh/' + args.command[5:], payload)
        elif args.command == 'status':
            value = client.request('/v1/status')
        elif args.command == 'submit':
            value = client.request('/v1/tasks', {'input': args.text})
        elif args.command == 'notify':
            if not args.request_id:
                parser.error('notify requires --request-id')
            value = client.request('/v1/notify', {'text': args.text, 'request_id': args.request_id})
        elif args.command == 'resource':
            if args.principal is not None:
                parser.error('--principal is a discovery filter, not a resource actor override')
            try:
                payload = resource_payload(args.payload_file, args.action)
            except ValueError as exc:
                parser.error(str(exc))
            value = client.request('/v1/resource/action', payload)
        else:
            routes = {'resources': '/v1/resources', 'capability-events': '/v1/capability-events',
                      'resource-graph': '/v1/resource/graph'}
            query = {k: getattr(args, k) for k in ('limit', 'after', 'kind', 'principal') if getattr(args, k) is not None}
            if args.include_unavailable:
                query['include_unavailable'] = '1'
            value = client.request(routes[args.command] + ('?' + urlencode(query) if query else ''))
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
