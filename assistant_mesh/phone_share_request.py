"""Request a finite phone share or owner-approved app launch on a live task."""
import argparse
import base64
import hashlib
import json
import re
import sys

from .config import private_json
from .ingress import _identifier
from .phone_screen_share import stream_policy
from .phone_screen_share_reader import OBSERVATION_ROUTE
from .worker import Client

# Tencent's public HarmonyOS SDK (1.0.23) declares these constants.
# The retail device decides whether it permits this specific launch.
WECHAT_TARGET = {'bundle_name': 'com.tencent.wechat', 'ability_name': 'EntryAbility'}


def scope_for(task_id, epoch, target=None):
    _identifier(task_id, 'screen_share_task')
    if type(epoch) is not int or epoch < 1:
        raise ValueError('screen_share_original_task_epoch_required')
    result = {'task_id': task_id, 'task_epoch': epoch, 'purpose': 'screen_share'}
    if target is None:
        return result
    if (not isinstance(target, dict) or set(target) != {'bundle_name', 'ability_name'}
            or any(not isinstance(name, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.]{0,127}', name) is None
                   for name in target.values())
            or target['bundle_name'] == 'org.personalassistant.mesh'):
        raise ValueError('screen_share_original_app_target_required')
    result.update(purpose='screen_share_open_app', target=dict(target))
    return result


def selection_for(task_id, task_epoch, capability, capability_epoch, grant_id,
                  duration_ms=30000, max_fps=1, target=None):
    scope = scope_for(task_id, task_epoch, target)
    for value in (capability, grant_id):
        _identifier(value, 'screen_share_identity')
    if type(capability_epoch) is not int or capability_epoch < 1:
        raise ValueError('screen_share_original_capability_epoch_required')
    workload = {'format': 'amita-phone-screen-share/1', 'command': 'screen_share',
                'duration_ms': duration_ms, 'max_fps': max_fps}
    if target is not None:
        workload.update(format='amita-phone-screen-share/2', command='screen_share_open_app',
                        target=scope['target'])
    workload = stream_policy(workload)
    raw = json.dumps(workload, sort_keys=True, separators=(',', ':')).encode()
    blob = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'base64': base64.b64encode(raw).decode('ascii')}
    return {'capability_id': capability, 'epoch': capability_epoch,
            'action': 'action' if target is not None else 'inspect',
            'scope': scope, 'workload': {'input': blob}, 'grant_id': grant_id}


def observe(client, selection):
    scope = selection['scope']
    return client.request(OBSERVATION_ROUTE, {
        'task_id': scope['task_id'], 'task_epoch': scope['task_epoch'], 'selection': selection})


def reserve(client, operation, selection, observation):
    _identifier(operation, 'screen_share_identity')
    scope, capability = selection['scope'], selection['capability_id']
    return client.request('/v1/allocation/action', {'action': 'reserve', 'arguments': {
        'operation_id': operation, 'task_id': scope['task_id'], 'task_epoch': scope['task_epoch'],
        'plan': {'target': capability, 'selections': [dict(selection, observations=[observation])],
                 'route': []}, 'lease_seconds': 90}})


def submit(client, task_id, task_epoch, operation, capability, capability_epoch, grant_id,
           duration_ms=30000, max_fps=1, target=None):
    _identifier(operation, 'screen_share_identity')
    selection = selection_for(task_id, task_epoch, capability, capability_epoch, grant_id,
                              duration_ms, max_fps, target)
    return reserve(client, operation, selection, observe(client, selection))


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
    for sub in (grant, reserve):
        sub.add_argument('--bundle-name', help='Exact exported destination bundle')
        sub.add_argument('--ability-name', help='Exact destination entry UIAbility')
        sub.add_argument('--wechat', action='store_true', help='Target the published WeChat entry UIAbility')
    inspect = actions.add_parser('inspect')
    inspect.add_argument('--operation-id', required=True)
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error('action required')
    try:
        bundle = getattr(args, 'bundle_name', None)
        ability = getattr(args, 'ability_name', None)
        if getattr(args, 'wechat', False):
            if bundle or ability:
                parser.error('--wechat cannot be combined with explicit destination names')
            bundle, ability = WECHAT_TARGET['bundle_name'], WECHAT_TARGET['ability_name']
        if bool(bundle) != bool(ability):
            parser.error('pass both destination bundle and ability names')
        target = {'bundle_name': bundle, 'ability_name': ability} if bundle else None
        scope = scope_for(args.task_id, args.task_epoch, target)
        client = Client(private_json(args.config))
        if args.action == 'request-grant':
            result = client.request('/v1/resource/action', {'action': 'request_grant', 'arguments': {
                'id': args.capability_id, 'action': 'action' if target else 'inspect',
                'scope': scope, 'request_id': args.request_id}})
            print(json.dumps({'request_id': args.request_id, 'status': result.get('status'),
                              'owner_grant_required': True}))
            return 0
        if args.action == 'inspect':
            result = client.request('/v1/allocation/action', {'action': 'inspect',
                                      'arguments': {'operation_id': args.operation_id}})
        else:
            result = submit(client, args.task_id, args.task_epoch, args.operation_id,
                            args.capability_id, args.capability_epoch, args.grant_id, args.duration_ms,
                            target=target)
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
