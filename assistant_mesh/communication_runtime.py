"""Owned task-inbox / notification companions, no account or iLink transport.

This is explicitly installed on an execution authority by Root. It uses the
existing exact legacy ingestion and durable NotificationRelay with a SEPARATE
broker Client, never changes a Node's A2A Client, and never copies native tasks.
"""
import argparse
import fcntl
import json
import os
import stat
import sys
import threading

from .communication_inbox import CommunicationInbox
from .communication_producer import relay_store_class
from .communication_store import _private_path
from .config import private_json
from .networking import identifier
from .notification_relay import NotificationRelay
from .worker import Client

PROTOCOL = 'mesh-communication-execution-companion/1'


class Dispatcher:
    def __init__(self, inbox, broker_client, accept_ordinary=False, limit=20):
        if (not isinstance(inbox, CommunicationInbox)
                or not callable(getattr(broker_client, 'request', None))
                or type(accept_ordinary) is not bool or type(limit) is not int or not 0 < limit <= 100):
            raise ValueError('communication_dispatcher_config_invalid')
        self.inbox, self.client = inbox, broker_client
        self.accept_ordinary, self.limit = accept_ordinary, limit

    def step(self):
        pending = self.client.request('/communication/pending', {'limit': self.limit})
        if not isinstance(pending, dict) or set(pending) != {'events'} or not isinstance(pending['events'], list):
            raise ValueError('communication_pending_invalid')
        reports = []
        for row in pending['events']:
            if not isinstance(row, dict):
                raise ValueError('communication_pending_invalid')
            identity = identifier(row.get('message_id'))
            required, target = row.get('required_authority'), row.get('target_authority')
            if (required is not None and required != self.inbox.authority
                    or target is not None and target != self.inbox.authority):
                continue
            attempted = row.get('post_attempted')
            if type(attempted) is not int or attempted not in (0, 1):
                raise ValueError('communication_pending_invalid')
            if not attempted and required is None and not self.accept_ordinary:
                continue
            report = {'message_id': identity, 'post_replayed': False, 'execution_verified': False}
            try:
                if attempted:
                    # Do NOT POST prepare/deliver again after an unknown ACK.
                    # Only the original local durable receipt can reconcile it.
                    receipt = self.inbox.receipt(identity)
                else:
                    prepared = self.client.request('/communication/prepare',
                        {'message_id': identity, 'authority': self.inbox.authority})
                    if (not isinstance(prepared, dict) or prepared.get('message_id') != identity
                            or prepared.get('authority') != self.inbox.authority
                            or type(prepared.get('fresh')) is not bool):
                        raise ValueError('communication_original_prepare_invalid')
                    if prepared['fresh']:
                        event = self.client.request('/communication/event', {'message_id': identity})
                        receipt = self.inbox.deliver(event)
                    else:
                        receipt = self.inbox.receipt(identity)
                if receipt is None:
                    report['state'] = 'unknown'
                else:
                    ack = self.client.request('/communication/receipt',
                        {'message_id': identity, 'receipt': receipt})
                    if not isinstance(ack, dict) or ack.get('confirmed') is not True:
                        raise ValueError('communication_original_ack_invalid')
                    report.update(state='confirmed', task_id=receipt['task_id'])
            except Exception:
                # Original target/attempt/receipt is durable at the broker or
                # local inbox. Never repair an uncertainty with another input.
                report['state'] = 'unknown'
            reports.append(report)
        return {'deliveries': reports, 'replay_authorized': False, 'native_features_restricted': False}


def _lock(database):
    database = _private_path(database)
    fd = os.open(str(database) + '.communication-executor.lock',
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        item = os.fstat(fd)
        if (not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid()
                or stat.S_IMODE(item.st_mode) != 0o600 or item.st_nlink != 1):
            raise ValueError('communication_executor_lock_unsafe')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def serve(config, stop=None, on_report=None):
    if (not isinstance(config, dict) or config.get('protocol') != PROTOCOL
            or set(config) - {'protocol', 'task_server_config', 'broker_client_config',
                              'notification_client_config', 'communication_id',
                              'accept_ordinary', 'poll_interval_seconds', 'new_task_route'}):
        raise ValueError('communication_companion_config_invalid')
    interval = config.get('poll_interval_seconds', 1)
    if type(interval) not in (int, float) or not 0.1 <= interval <= 60:
        raise ValueError('communication_companion_interval_invalid')
    task_config = private_json(config['task_server_config'])
    policy = task_config.get('notification_policy', {})
    if (task_config.get('ilink_account') or 'channel_carrier' in task_config
            or policy.get('mode') != 'private' or not isinstance(policy.get('owner_relay'), dict)):
        raise ValueError('communication_companion_requires_task_producer')
    # No accidental HEAD Store/native code; no recovery of original steering.
    from . import server
    from .legacy_fenced_receiver import _check_frozen_source
    _check_frozen_source(server)
    # Validate fixed private routes before opening any execution Store/schema.
    # Credentials and URLs are references, never read from a task/event body.
    broker_client = Client(private_json(config['broker_client_config']))
    notification_client = (Client(private_json(config['notification_client_config']))
                           if config.get('notification_client_config') else None)
    fd = _lock(task_config['database'])
    stopped = threading.Event() if stop is None else stop
    thread = None
    try:
        store = relay_store_class(server.Store)(task_config['database'], recover_inflight=False,
            notification_policy=policy, node_id=task_config['node_id'])
        inbox = CommunicationInbox(store, task_config['node_id'], config['communication_id'],
                                   config.get('new_task_route'))
        # These are new, independently configured Clients, not Node.peers.
        dispatcher = Dispatcher(inbox, broker_client,
                                config.get('accept_ordinary', False))
        if notification_client is not None:
            route = policy['owner_relay']
            relay = NotificationRelay(store, task_config['node_id'], route['peer'], route['authority'],
                                      notification_client)
            thread = threading.Thread(target=lambda: relay.serve(stop=stopped, on_report=on_report),
                                      name='mesh-communication-notify', daemon=True)
            thread.start()
        while not stopped.is_set():
            try:
                report = dispatcher.step()
            except Exception:
                report = {'state': 'unavailable', 'original_operations_preserved': True,
                          'replay_authorized': False, 'native_features_restricted': False}
            if on_report is not None:
                on_report(report)
            stopped.wait(interval)
    finally:
        stopped.set()
        if thread is not None:
            thread.join(5)
        # A bounded join does not assert any outstanding remote RPC closure.
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Owned communication inbox/notify companion; no iLink')
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        serve(private_json(args.config))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(json.dumps({'ok': False, 'error': 'communication_companion_unconfirmed'}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
