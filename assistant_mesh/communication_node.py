"""Exact existing Node with producer-only embedded API and NO old relay.

Owner selects two private configs. All non-notification server fields must be
identical; Node's A2A/Worker logic and Clients remain original. No host changes.
"""
import argparse
import json
import signal
import sys

from .communication_producer import serve_task_producer
from .config import private_json
from .networking import canonical

PROTOCOL = 'mesh-communication-node-owner/1'


def validate_configs(node_config, node_server, producer):
    if (not isinstance(node_config, dict) or node_config.get('start_server', True) is not True
            or not isinstance(node_server, dict) or not isinstance(producer, dict)
            or node_server.get('ilink_account') or producer.get('ilink_account')
            or 'channel_carrier' in node_server or 'channel_carrier' in producer):
        raise ValueError('communication_node_producer_required')
    quiet = node_server.get('notification_policy', {})
    selected = producer.get('notification_policy', {})
    if (quiet != {'mode': 'private'} or selected.get('mode') != 'private'
            or not isinstance(selected.get('owner_relay'), dict)):
        raise ValueError('communication_node_separate_relay_policy_required')
    before = {key: value for key, value in node_server.items() if key != 'notification_policy'}
    after = {key: value for key, value in producer.items() if key != 'notification_policy'}
    if canonical(before) != canonical(after):
        raise ValueError('communication_node_non_notification_config_changed')


def serve(config, ready=None):
    if (not isinstance(config, dict) or set(config) != {'protocol', 'node_config', 'task_server_config'}
            or config['protocol'] != PROTOCOL):
        raise ValueError('communication_node_owner_config_invalid')
    if ready is not None and not callable(ready):
        raise ValueError('communication_owner_hook_invalid')
    node_config = private_json(config['node_config'])
    node_server = private_json(node_config['local_server_config'])
    producer = private_json(config['task_server_config'])
    validate_configs(node_config, node_server, producer)
    from . import node as original_node, server
    from .legacy_fenced_receiver import _check_frozen_source
    _check_frozen_source(server)
    original_store = original_node.Store
    def passive(path, **options):
        options['recover_inflight'] = False
        return original_store(path, **options)
    def producer_server(ignored, on_ready):
        return serve_task_producer(producer, ready=on_ready)
    # Single fresh process, construction only; restore before any Node thread.
    original_node.Store = passive
    try:
        runtime = original_node.Node(node_config, config_path=config['node_config'],
                                     server_runner=producer_server)
    finally:
        if original_node.Store is passive:
            original_node.Store = original_store
    if ready is not None:
        ready(runtime)
    handlers = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            handlers[number] = signal.signal(number, lambda signum, frame: runtime.stop_event.set())
        runtime.run_forever()
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Owner-selected Node API/relay placement, no channel')
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        serve(private_json(args.config))
    except Exception:
        print(json.dumps({'ok': False, 'error': 'communication_node_unconfirmed'}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
