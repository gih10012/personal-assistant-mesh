"""Opt-in task-authority producer using the EXISTING durable notify relay.

The original Store/Worker/native code is retained. Only this owner-selected
API Store redirects implicit channel notices to its configured owner relay.
No iLink account, poller, witness or communication cursor is owned here.
"""
import argparse
import json
import sys

from .config import private_json
from .legacy_channel_owner import _SERVE_LOCK


def relay_store_class(original_store):
    class RelayStore(original_store):
        def _enqueue(self, db, request_id, text, delivery_route=None, explicit=False):
            if (delivery_route is None and self.notification_policy.get('mode') == 'private'
                    and self.notification_policy.get('owner_relay') is not None):
                # Preserve notice content/request ID. Existing source journal
                # and receiver receipt carry the one original relay identity.
                explicit = True
            return super()._enqueue(db, request_id, text, delivery_route, explicit)
    return RelayStore


def serve_task_producer(config, ready=None):
    """Root-controlled source API restart; not run by migration/constructor."""
    policy = config.get('notification_policy', {}) if isinstance(config, dict) else {}
    if (not isinstance(config, dict) or config.get('ilink_account') or 'channel_carrier' in config
            or policy.get('mode') != 'private' or not isinstance(policy.get('owner_relay'), dict)):
        raise ValueError('communication_task_producer_config_required')
    if ready is not None and not callable(ready):
        raise ValueError('communication_owner_hook_invalid')
    from . import server
    from .legacy_fenced_receiver import _check_frozen_source
    # This wrapper must not accidentally select HEAD's newer native Store.
    _check_frozen_source(server)
    if not _SERVE_LOCK.acquire(False):
        raise ValueError('communication_source_service_already_running')
    original = server.Store
    selected = relay_store_class(original)
    def passive(path, **options):
        options['recover_inflight'] = False
        return selected(path, **options)
    def connected(http, api, channel):
        if server.Store is not passive or channel is not None:
            raise ValueError('communication_source_factory_or_channel_changed')
        server.Store = original
        if ready is not None:
            ready(http, api, channel)
    try:
        server.Store = passive
        return server.serve(config, ready=connected)
    finally:
        if server.Store is passive:
            server.Store = original
        _SERVE_LOCK.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Frozen task API producer, no channel account or receiver')
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        serve_task_producer(private_json(args.config))
    except Exception:
        print(json.dumps({'ok': False, 'error': 'communication_task_producer_unconfirmed'}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
