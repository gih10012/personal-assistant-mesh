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
                    print(json.dumps({key: receipt.get(key) for key in (
                        'authority', 'cursor', 'caught_up_to_observed_head', 'observed_at',
                        'execution_authorized', 'source_task_claimed')}), flush=True)
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
