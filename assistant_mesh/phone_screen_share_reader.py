"""One private frame reader on the existing authenticated API dispatch chain.

No listener, Store, credential, actor-from-JSON or global frame catalogue.
Installation is explicit in the incumbent phone ready hook. Import is inert.
"""
from urllib.parse import urlsplit

from .ingress import _identifier
from .phone_screen_share import BINDING_FIELDS


ROUTE = '/v1/phone/screen-share/frame'
LOOKUP_FIELDS = frozenset(('operation_id', 'capability_id', 'task_id', 'task_epoch'))


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
        if parsed.path != ROUTE or parsed.query or parsed.fragment or method != 'POST':
            raise PermissionError('screen_share_reader_route_not_authorized')
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
    api._phone_screen_share_reader_installed = True
    return service.screen_share
