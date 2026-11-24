"""Request a finite read-only share for a task held by this Mesh worker."""
import argparse
import base64
import hashlib
import json
import sys

from .config import private_json
from .ingress import _identifier
from .phone_screen_share import stream_policy
from .phone_screen_share_reader import OBSERVATION_ROUTE
from .worker import Client


def scope_for(task_id, epoch):
    _identifier(task_id, 'screen_share_task')
    if type(epoch) is not int or epoch < 1:
        raise ValueError('screen_share_original_task_epoch_required')
    return {'task_id': task_id, 'task_epoch': epoch, 'purpose': 'screen_share'}


def submit(client, task_id, task_epoch, operation, capability, capability_epoch, grant_id,
           duration_ms=30000, max_fps=1):
    scope = scope_for(task_id, task_epoch)
    for value in (operation, capability, grant_id):
        _identifier(value, 'screen_share_identity')
    if type(capability_epoch) is not int or capability_epoch < 1:
        raise ValueError('screen_share_original_capability_epoch_required')
    workload = stream_policy({'format': 'amita-phone-screen-share/1', 'command': 'screen_share',
                              'duration_ms': duration_ms, 'max_fps': max_fps})
    raw = json.dumps(workload, sort_keys=True, separators=(',', ':')).encode()
    blob = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'base64': base64.b64encode(raw).decode('ascii')}
    selection = {'capability_id': capability, 'epoch': capability_epoch, 'action': 'inspect',
                 'scope': scope, 'workload': {'input': blob}, 'grant_id': grant_id}
    observation = client.request(OBSERVATION_ROUTE, {
        'task_id': task_id, 'task_epoch': task_epoch, 'selection': selection})
    selection['observations'] = [observation]
    return client.request('/v1/allocation/action', {'action': 'reserve', 'arguments': {
        'operation_id': operation, 'task_id': task_id, 'task_epoch': task_epoch,
        'plan': {'target': capability, 'selections': [selection], 'route': []}, 'lease_seconds': 90}})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Original worker private config')
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--task-epoch', type=int, required=True)
    parser.add_argument('--capability-id', required=True)
    actions = parser.add_subparsers(dest='action')
    grant = actions.add_parser('request-grant')
    grant.add_argument('--request-id', required=True)
    reserve = actions.add_parser('reserve')
    reserve.add_argument('--operation-id', required=True)
    reserve.add_argument('--capability-epoch', required=True, type=int)
    reserve.add_argument('--grant-id', required=True)
    reserve.add_argument('--duration-ms', default=30000, type=int)
    inspect = actions.add_parser('inspect')
    inspect.add_argument('--operation-id', required=True)
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error('action required')
    try:
        scope = scope_for(args.task_id, args.task_epoch)
        client = Client(private_json(args.config))
        if args.action == 'request-grant':
            result = client.request('/v1/resource/action', {'action': 'request_grant', 'arguments': {
                'id': args.capability_id, 'action': 'inspect', 'scope': scope, 'request_id': args.request_id}})
            print(json.dumps({'request_id': args.request_id, 'status': result.get('status'),
                              'owner_grant_required': True}))
            return 0
        if args.action == 'inspect':
            result = client.request('/v1/allocation/action', {'action': 'inspect',
                                      'arguments': {'operation_id': args.operation_id}})
        else:
            result = submit(client, args.task_id, args.task_epoch, args.operation_id,
                            args.capability_id, args.capability_epoch, args.grant_id, args.duration_ms)
        if result.get('operation_id') != args.operation_id:
            raise ValueError('screen_share_operation_unconfirmed')
        print(json.dumps({'operation_id': args.operation_id, 'state': result.get('state'),
                          'deadline': result.get('deadline'), 'capture_verified': False}))
    except (OSError, ValueError, KeyError):
        print('Phone screen request unconfirmed. Inspect the original operation ID before proceeding.',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
