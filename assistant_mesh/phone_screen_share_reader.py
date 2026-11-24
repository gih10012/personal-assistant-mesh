"""One private frame reader on the existing authenticated API dispatch chain.

No listener, Store, credential, actor-from-JSON or global frame catalogue.
Installation is explicit in the incumbent phone ready hook. Import is inert.
"""
from urllib.parse import urlsplit

from .ingress import _identifier
from .phone_screen_share import BINDING_FIELDS, stream_policy


ROUTE = '/v1/phone/screen-share/frame'
OBSERVATION_ROUTE = '/v1/phone/screen-share/observation'
LOOKUP_FIELDS = frozenset(('operation_id', 'capability_id', 'task_id', 'task_epoch'))
VERIFIER = 'source:phone-control-presence'
METRIC = 'phone.control.reachable'


def _observe(service, actor, payload):
    if (not actor.startswith('node:') or not isinstance(payload, dict)
            or set(payload) != {'task_id', 'task_epoch', 'selection'}):
        raise PermissionError('phone_screen_observation_original_worker_required')
    task_id, epoch, selection = payload['task_id'], payload['task_epoch'], payload['selection']
    _identifier(task_id, 'phone_screen_observation_task')
    if (type(epoch) is not int or epoch < 1 or not isinstance(selection, dict)
            or set(selection) != {'capability_id', 'epoch', 'action', 'scope', 'workload', 'grant_id'}
            or selection['action'] != 'inspect'
            or type(selection['epoch']) is not int or selection['epoch'] < 1
            or selection['scope'] != {'task_id': task_id, 'task_epoch': epoch, 'purpose': 'screen_share'}):
        raise ValueError('phone_screen_observation_original_scope_required')
    capability_id = selection['capability_id']
    _identifier(capability_id, 'phone_screen_observation_capability')
    _identifier(selection['grant_id'], 'phone_screen_observation_grant')
    registry, allocations = service.api.resources, service.api.allocations
    with service.lock:
        with service.store.transaction() as db:
            now = service.store.clock()
            allocations._task(db, actor, task_id, epoch, now)
            cap = registry._find(db, capability_id)
            if (cap['kind'] != 'device.gui' or cap['epoch'] != selection['epoch']
                    or not registry._live(cap, now) or not cap['principal'].startswith('node:')):
                raise PermissionError('phone_screen_observation_live_adapter_required')
            node = service.screen_share._current_node(cap['principal'][5:])
            adapter = node.get('installed_control_adapter', {})
            if adapter.get('screen_share_enabled') is not True:
                raise PermissionError('phone_screen_observation_share_adapter_required')
            control = service.control.select(node, node['control_session_id'])
            presence = service._presence(db, control, live=True, capability=capability_id)
            stream_policy(service.control.validate_selection(control, selection))
            allocations._authorize(db, actor, cap, selection, now)
            # The Source received this real signed presence. The observation
            # proves control transport only, never capture, permission or FPS.
            sampled = presence['expires'] - service.sessions.ttl
        metric = registry.observe(VERIFIER, capability_id, METRIC, 1, 'observed',
                                  'authenticated phone control presence', sample_time=sampled,
                                  evidence={'scope': selection['scope'], 'workload': selection['workload'],
                                            'capture_verified': False}, verification='verified',
                                  epoch=selection['epoch'])
    return {'observation_id': metric['id'], 'metric': METRIC, 'unit': 'observed',
            'max_age_seconds': service.sessions.ttl}


def _actor(peer):
    if not isinstance(peer, dict) or peer.get('role') not in ('operator', 'worker'):
        raise PermissionError('screen_share_reader_route_not_authorized')
    # phone_http receives the incumbent API's ALREADY authenticated peer,
    # exactly as its existing private /v1/phone/result route. Never read an
    # actor/node override from the request or promote operator to a worker.
    node = peer.get('node')
    if peer['role'] == 'operator':
        actor = 'operator'
    elif isinstance(node, str) and node:
        _identifier(node, 'screen_share_reader_node')
        actor = 'node:' + node
    else:
        raise PermissionError('screen_share_reader_route_not_authorized')
    _identifier(actor, 'screen_share_reader_actor')
    return actor


def install_dispatch(api, service):
    """Append to the actual ready chain after PhoneAuthority is installed.

    Peer authentication remains owned by the incumbent API. Source lookup
    independently binds that actor to its original current task/allocation.
    """
    if (service.api is not api or service.store is not api.store
            or getattr(api, 'phone_authority', service) is not service
            or getattr(api, '_phone_screen_share_reader_installed', False)):
        raise ValueError('screen_share_reader_original_authority_required')
    previous = api.dispatch

    def dispatch(method, path, payload, peer):
        parsed = urlsplit(path)
        if not parsed.path.startswith('/v1/phone/screen-share'):
            return previous(method, path, payload, peer)
        if parsed.path not in (ROUTE, OBSERVATION_ROUTE) or parsed.query or parsed.fragment or method != 'POST':
            raise PermissionError('screen_share_reader_route_not_authorized')
        if parsed.path == OBSERVATION_ROUTE:
            return _observe(service, _actor(peer), payload)
        if not isinstance(payload, dict) or set(payload) not in (LOOKUP_FIELDS, BINDING_FIELDS):
            raise ValueError('screen_share_reader_arguments_required')
        arguments = dict(payload)
        actor = _actor(peer)
        # lookup/read each recheck exact allocation.actor/current original task
        # epoch, selected provider and control presence before returning pixels.
        if set(arguments) == LOOKUP_FIELDS:
            return service.screen_share.lookup(actor, arguments)
        return service.screen_share.read(actor, arguments)

    api.dispatch = dispatch
    # The Source verifier is internal. Resource API identities are fixed to
    # operator or node:<id>; request arguments cannot impersonate this actor.
    api.resources.trusted_verifiers = frozenset(api.resources.trusted_verifiers) | {VERIFIER}
    api._phone_screen_share_reader_installed = True
    return service.screen_share
