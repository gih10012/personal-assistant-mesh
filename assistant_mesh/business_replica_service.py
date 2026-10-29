"""Model-independent passive replication loop for an existing private client.

This process never claims business work or changes a source role/lease. The
source's bounded snapshot lifecycle and Replica's durable apply own data safety.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import threading
import time

from .business_blob_cache import BlobCache
from .business_journal import ACK_FIELDS, ACK_PROTOCOL, digest
from .business_replica import Replica, pull
from .config import private_json
from .worker import Client


def save_private(path, value):
    temporary = path.with_name('.' + path.name + '.' + os.urandom(12).hex())
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True).encode('utf8')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(path))
        parent = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _remove_private(path):
    path.unlink()
    parent = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def acknowledge_committed(client, result, state):
    """Called only AFTER durable Replica apply AND save_private(last-sync).

    The source receives an authenticated receiver assertion, never independent
    remote-disk proof. Pending content/ID survives a lost response unchanged.
    """
    if not {'authority', 'incarnation', 'generation', 'cursor', 'manifest'} <= set(result):
        return {'status': 'baseline_not_published'}
    if result.get('ack_protocol') != ACK_PROTOCOL:
        value = {'status': 'not_supported', 'source_task_claimed': False}
        save_private(state / 'last-ack.json', value)
        return value
    payload = {key: result[key] for key in ('authority', 'incarnation', 'generation', 'cursor')}
    payload['manifest_sha256'] = result['manifest']['sha256']
    payload['receipt_id'] = digest(payload)
    pending_path = state / 'pending-ack.json'
    pending = private_json(pending_path)['ack'] if pending_path.exists() else None
    if pending is not None:
        if (not isinstance(pending, dict) or set(pending) != ACK_FIELDS | {'receipt_id'}
                or digest({key: pending[key] for key in ACK_FIELDS}) != pending['receipt_id']):
            raise ValueError('business_replica_pending_ack_invalid')
        scope = ('authority', 'incarnation', 'generation', 'manifest_sha256')
        if (any(pending[key] != payload[key] for key in scope)
                or pending['cursor'] < result.get('retained_after', 0)):
            # An authenticated generation/floor change requires a newly
            # committed baseline. Preserve the old original ID as evidence;
            # never retry it as an assertion about another source generation.
            save_private(state / 'last-superseded-ack.json', dict(ack=pending,
                status='new_committed_baseline_required', replacement_receipt_id=payload['receipt_id']))
            pending = None

    def send(content):
        try:
            response = client.request('/v1/business-replica/ack', content)
            if (not isinstance(response, dict) or response.get('receipt_id') != content['receipt_id']
                    or response.get('assertion') != 'authenticated_receiver_assertion'
                    or response.get('remote_disk_independently_verified') is not False):
                raise ValueError('business_replica_ack_response_invalid')
            value = dict(status='acknowledged', ack=content, source_response=response,
                         source_task_claimed=False)
            save_private(state / 'last-ack.json', value)
            _remove_private(pending_path)
            return value
        except InterruptedError:
            raise
        except Exception as error:
            value = dict(status='pending', receipt_id=content['receipt_id'],
                         category=type(error).__name__, source_task_claimed=False)
            save_private(state / 'last-ack.json', value)
            return value

    if pending is not None:
        previous = send(pending)
        if previous['status'] != 'acknowledged' or pending['receipt_id'] == payload['receipt_id']:
            return previous
    save_private(pending_path, dict(ack=payload, protocol=ACK_PROTOCOL))
    return send(payload)


def run(client_path, database, authority, state_directory, seed_databases=(),
        cache_directory=None, interval=15, pages=1000):
    if not 5 <= interval <= 300:
        raise ValueError('business_replica_interval_invalid')
    state = Path(state_directory).absolute()
    if state.is_symlink() or any(parent.is_symlink() for parent in state.parents):
        raise ValueError('business_replica_private_state_required')
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = state.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('business_replica_private_state_required')
    lock = os.open(str(state / 'writer.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stopped = threading.Event()
        previous = {}
        for name in (signal.SIGTERM, signal.SIGINT):
            previous[name] = signal.signal(name, lambda unused, frame: stopped.set())
        cache = None
        try:
            replica = Replica(database)
            cache = BlobCache(seed_databases=seed_databases, directory=cache_directory)
            attempt, failures = 0, 0
            while not stopped.is_set():
                attempt += 1
                started = time.time()
                try:
                    client = Client(private_json(client_path))

                    class InterruptibleClient:
                        def request(self, path, payload=None):
                            # Even during shutdown, end the original snapshot.
                            if stopped.is_set() and not (
                                    isinstance(payload, dict) and payload.get('action') == 'end'):
                                raise InterruptedError('business_replica_service_stopped')
                            return client.request(path, payload)

                    result = pull(InterruptibleClient(), replica, authority,
                                  pages=pages, limit=500, blob_cache=cache)
                    failures = 0
                    receipt = dict(result, observed_at=time.time(), attempt=attempt,
                                   duration_seconds=time.time() - started,
                                   cache_counts=cache.status(), source_task_claimed=False)
                    save_private(state / 'last-sync.json', receipt)
                    # Apply committed before pull returned; both this success
                    # receipt and the pending ACK are fsynced before network ACK.
                    ack = acknowledge_committed(InterruptibleClient(), result, state)
                    receipt['ack_status'] = ack['status']
                    print(json.dumps({key: receipt.get(key) for key in (
                        'authority', 'cursor', 'caught_up_to_observed_head', 'observed_at',
                        'execution_authorized', 'source_task_claimed', 'ack_status')}), flush=True)
                    wait = interval
                except InterruptedError:
                    break
                except Exception as error:
                    failures += 1
                    # Never publish exception bodies, credentials or history.
                    failure = {'observed_at': time.time(), 'attempt': attempt,
                               'failures': failures, 'category': type(error).__name__,
                               'published_view_retained': True, 'source_task_claimed': False}
                    save_private(state / 'last-failure.json', failure)
                    print(json.dumps(failure), flush=True)
                    wait = min(300, interval * (2 ** min(failures - 1, 5)))
                stopped.wait(wait)
        finally:
            if cache is not None:
                cache.close()
            for name, handler in previous.items():
                signal.signal(name, handler)
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('client', 'database', 'authority', 'state-directory'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--seed-db', action='append', default=[])
    parser.add_argument('--blob-cache-dir')
    parser.add_argument('--interval', type=int, default=15)
    parser.add_argument('--pages', type=int, default=1000)
    args = parser.parse_args()
    return run(args.client, args.database, args.authority, args.state_directory,
               args.seed_db, args.blob_cache_dir, args.interval, args.pages)


if __name__ == '__main__':
    main()
