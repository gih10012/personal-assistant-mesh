"""Durable optional node-to-owner notification relay, not a native tool gate.

Only explicit Store intents are eligible. A private local intent precedes
every network call; the original node, peer, authority, text and request ID
remain fixed. An authenticated protocol receipt pins the channel route before
the first POST. A committed POST intent is never automatically submitted a
second time, even if the receiver later reports not_found after rollback.
Receiving into the remote outbox is not phone delivery.
"""
import fcntl
import json
import math
import os
import re
import stat
import threading
import urllib.error
from pathlib import Path

from .networking import canonical, identifier
from .remote_notifications import PROTOCOL, receipt_id, request_id, text_fingerprint


_SHA = re.compile(r'^[0-9a-f]{64}$')
_REMOTE_STATES = frozenset(('pending', 'submitting', 'accepted', 'rejected',
                          'unknown', 'waiting_auth'))
_LOCAL_STATES = _REMOTE_STATES | frozenset(('queued',))
_RECEIPT_KEYS = frozenset(('protocol', 'authority', 'source_node', 'request_id',
                         'fingerprint', 'outbox_id', 'route_id', 'found', 'status',
                         'idempotent_receive', 'delivery_verified'))
_DIAGNOSTICS = frozenset(('relay_clock_invalid', 'relay_lock_not_private',
                         'relay_local_intent_invalid', 'relay_local_state_conflict',
                         'relay_protocol_invalid', 'relay_receipt_identity_mismatch',
                         'relay_route_changed', 'relay_original_target_mismatch',
                         'relay_body_mismatch', 'relay_local_receipt_invalid',
                         'relay_remote_receipt_missing', 'relay_submit_receipt_missing',
                         'relay_submission_unconfirmed'))


class NotificationRelayError(ValueError):
    """Only fixed diagnostic codes generated here, never remote error text."""


def _failure(error):
    if isinstance(error, NotificationRelayError):
        # Client implementations are injectable. Even one raising this public
        # exception class cannot turn its private exception text into telemetry.
        return str(error) if str(error) in _DIAGNOSTICS else 'relay_protocol_invalid'
    if isinstance(error, urllib.error.HTTPError):
        return {401: 'relay_authentication_rejected', 403: 'relay_authorization_rejected',
                404: 'relay_protocol_unavailable', 409: 'relay_remote_conflict'}.get(
                    error.code, 'relay_peer_http_error')
    if isinstance(error, PermissionError):
        return 'relay_authorization_rejected'
    if isinstance(error, ValueError):
        return 'relay_protocol_invalid'
    return 'relay_connection_unavailable'


class NotificationRelay:
    """Poll one durable explicit notification without blocking Node heartbeats.

    The Node owns a separate thread and an interruptible stop event. Its Client
    uses bounded 15-second requests; stop is checked at each request boundary.
    The flock excludes live threads/processes even if a wall-clock lease expires.
    Leases and backoff persist across restarts, never resetting an unknown send.
    """
    def __init__(self, store, source_node, peer, authority, client):
        self.store = store
        self.source_node, self.peer, self.authority = (
            identifier(source_node), identifier(peer), identifier(authority))
        if self.source_node == self.peer or not callable(getattr(client, 'request', None)):
            raise NotificationRelayError('invalid_notification_relay_config')
        self.client = client
        self.lock_path = Path(store.path + '.notification-relay.lock')

    @staticmethod
    def _report(state, row=None, error=None):
        value = {'state': state, 'error': error, 'recoverable': True,
                 'delivery_verified': False, 'retry_with_new_id': False,
                 'native_tools_intercepted': False}
        if row is not None:
            value['request_id'] = row['request_id']
            value['attempt'] = row['attempt']
        return value

    def _now(self):
        value = self.store.clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise NotificationRelayError('relay_clock_invalid')
        return value

    def _lock(self):
        path = self.lock_path
        if any(parent.is_symlink() for parent in path.parents):
            raise NotificationRelayError('relay_lock_not_private')
        parent = path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700):
            raise NotificationRelayError('relay_lock_not_private')
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            target = os.fstat(fd)
            if (not stat.S_ISREG(target.st_mode) or target.st_uid != os.getuid()
                    or target.st_nlink != 1 or stat.S_IMODE(target.st_mode) != 0o600):
                raise NotificationRelayError('relay_lock_not_private')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _claim(self):
        now = self._now()
        with self.store.transaction() as db:
            row = db.execute('''SELECT r.*, o.body, o.fingerprint AS body_fingerprint,
                    o.delivery_route, o.media_items FROM notification_relays r
                LEFT JOIN outbox o ON o.id=r.request_id
                WHERE r.state!='accepted' AND r.lease_until<=?
                ORDER BY r.updated,r.created,r.request_id LIMIT 1''', (now,)).fetchone()
            if row is None:
                return None
            row = dict(row)
            attempt = row['attempt']
            post_attempted = row['post_attempted']
            if (isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0
                    or type(post_attempted) is not int or post_attempted not in (0, 1)):
                raise NotificationRelayError('relay_local_intent_invalid')
            # Intent already exists from Store.enqueue's transaction. This CAS
            # also records a network attempt before STATUS or notify POST.
            count = db.execute('''UPDATE notification_relays SET state='submitting',
                    attempt=attempt+1,lease_until=?,updated=?
                WHERE request_id=? AND attempt=? AND lease_until<=?''',
                (now + 60, now, row['request_id'], attempt, now)).rowcount
            if count != 1:
                return None
            row['attempt'] += 1
            return row

    def _write(self, row, state, receipt=None, error=None, in_flight=False, immediate=False,
               mark_post_attempted=False):
        now = self._now()
        # Keep the original pinned receipt on response loss or invalid replies.
        encoded = canonical(receipt) if receipt is not None else row.get('receipt')
        delay = 0 if immediate else min(60, 2 ** min(row['attempt'], 6))
        lease = now + 60 if in_flight else now + delay
        attempted = 1 if mark_post_attempted else row['post_attempted']
        with self.store.transaction() as db:
            count = db.execute('''UPDATE notification_relays SET state=?,receipt=?,error=?,
                    lease_until=?,updated=?,post_attempted=? WHERE request_id=? AND attempt=?
                    AND state='submitting' AND source_node=? AND peer=?
                    AND authority=? AND fingerprint=? AND post_attempted=?''',
                (state, encoded, error, lease, now, attempted, row['request_id'], row['attempt'],
                 row['source_node'], row['peer'], row['authority'], row['fingerprint'],
                 row['post_attempted'])).rowcount
            if count != 1:
                raise NotificationRelayError('relay_local_state_conflict')
        row['receipt'] = encoded
        row['post_attempted'] = attempted

    def _validate(self, value, row, previous=None):
        if not isinstance(value, dict) or set(value) != _RECEIPT_KEYS:
            raise NotificationRelayError('relay_protocol_invalid')
        expected = {'protocol': PROTOCOL, 'authority': row['authority'],
                    'source_node': row['source_node'], 'request_id': row['request_id'],
                    'fingerprint': row['fingerprint'],
                    'outbox_id': receipt_id(row['source_node'], row['request_id'])}
        for key, actual in expected.items():
            if not isinstance(value[key], str) or value[key] != actual:
                raise NotificationRelayError('relay_receipt_identity_mismatch')
        if (not isinstance(value['route_id'], str) or not _SHA.fullmatch(value['route_id'])
                or type(value['found']) is not bool
                or value['idempotent_receive'] is not True
                or value['delivery_verified'] is not False
                or not isinstance(value['status'], str)):
            raise NotificationRelayError('relay_protocol_invalid')
        if ((value['found'] and value['status'] not in _REMOTE_STATES)
                or (not value['found'] and value['status'] != 'not_found')):
            raise NotificationRelayError('relay_protocol_invalid')
        if previous is not None and value['route_id'] != previous['route_id']:
            raise NotificationRelayError('relay_route_changed')
        return value

    def _context(self, row):
        if (row['source_node'] != self.source_node or row['peer'] != self.peer
                or row['authority'] != self.authority):
            raise NotificationRelayError('relay_original_target_mismatch')
        if row['state'] not in _LOCAL_STATES:
            raise NotificationRelayError('relay_local_intent_invalid')
        try:
            request_id(row['request_id'])
            actual = text_fingerprint(row['body'])
        except (ValueError, TypeError):
            raise NotificationRelayError('relay_local_intent_invalid') from None
        if (row['delivery_route'] != 'relay' or row['media_items'] is not None
                or row['fingerprint'] != actual or row['body_fingerprint'] != actual):
            raise NotificationRelayError('relay_body_mismatch')
        previous = None
        if row['receipt'] is not None:
            try:
                previous = self._validate(json.loads(row['receipt']), row)
            except (ValueError, TypeError):
                raise NotificationRelayError('relay_local_receipt_invalid') from None
        return previous

    @staticmethod
    def _stopped(stop):
        return stop is not None and stop.is_set()

    def poll_once(self, stop=None):
        if self._stopped(stop):
            return self._report('stopped')
        try:
            fd = self._lock()
        except BlockingIOError:
            return self._report('busy')
        except (OSError, NotificationRelayError):
            report = self._report('degraded', error='relay_lock_not_private')
            report['recoverable'] = False
            return report
        row = None
        try:
            if self._stopped(stop):
                return self._report('stopped')
            row = self._claim()
            if row is None:
                return self._report('idle')
            previous = self._context(row)
            if self._stopped(stop):
                self._write(row, row['state'], immediate=True)
                return self._report('stopped', row)
            body = {'request_id': row['request_id'], 'fingerprint': row['fingerprint']}
            response = self._validate(self.client.request('/v1/mesh/notify/status', body), row, previous)
            if response['found']:
                state = response['status'] if response['status'] not in ('pending', 'submitting') else 'queued'
                self._write(row, state, receipt=response)
                return self._report(state, row)
            if previous is not None and previous['found']:
                # A previously durable remote receipt must not disappear. Even
                # a server saying not_found cannot authorize a replacement.
                raise NotificationRelayError('relay_remote_receipt_missing')
            if row['post_attempted']:
                # Even this same receiver may have restored an old snapshot.
                # A request that failed before reaching it is indistinguishable
                # from a lost ACK plus rolled-back receipt. Never POST again.
                raise NotificationRelayError('relay_submission_unconfirmed')
            # Pin owner/bot/origin route before the first effectful relay POST.
            # The single writer transaction also marks the irreversible local
            # POST intent. A crash or network error never clears this marker.
            if self._stopped(stop):
                self._write(row, row['state'], receipt=response, immediate=True)
                return self._report('stopped', row)
            self._write(row, 'submitting', receipt=response, in_flight=True,
                        mark_post_attempted=True)
            if self._stopped(stop):
                self._write(row, 'unknown', error='relay_submission_unconfirmed')
                return self._report('stopped', row, 'relay_submission_unconfirmed')
            submitted = self._validate(self.client.request('/v1/mesh/notify',
                dict(body, text=row['body'], route_id=response['route_id'])), row, response)
            if not submitted['found']:
                raise NotificationRelayError('relay_submit_receipt_missing')
            state = submitted['status'] if submitted['status'] not in ('pending', 'submitting') else 'queued'
            self._write(row, state, receipt=submitted)
            return self._report(state, row)
        except Exception as error:
            category = _failure(error)
            state = 'waiting_auth' if category in ('relay_authentication_rejected',
                                                   'relay_authorization_rejected') else 'unknown'
            if row is not None:
                self._write(row, state, error=category)
            report = self._report(state if row is not None else 'degraded', row, category)
            if row is None:
                report['recoverable'] = False
            return report
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def serve(self, stop=None, on_report=None, max_cycles=None, poll_interval_seconds=1):
        if (isinstance(poll_interval_seconds, bool)
                or not isinstance(poll_interval_seconds, (int, float))
                or not math.isfinite(poll_interval_seconds) or not 0 < poll_interval_seconds <= 60):
            raise NotificationRelayError('invalid_notification_poll_interval')
        if max_cycles is not None and (isinstance(max_cycles, bool)
                or not isinstance(max_cycles, int) or max_cycles <= 0):
            raise NotificationRelayError('invalid_notification_cycle_limit')
        stop = threading.Event() if stop is None else stop
        cycles, report = 0, None
        while not stop.is_set():
            report = self.poll_once(stop=stop)
            cycles += 1
            if on_report is not None:
                on_report(report)
            if not report['recoverable'] or (max_cycles is not None and cycles >= max_cycles):
                break
            stop.wait(poll_interval_seconds)
        return {'cycles': cycles, 'last_report': report, 'delivery_verified': False,
                'retry_with_new_id': False, 'native_tools_intercepted': False}
