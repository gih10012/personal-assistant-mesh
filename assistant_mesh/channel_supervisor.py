"""Owned channel retry/renew loops, not takeover or external-call cancellation.

Only an explicitly supplied, already granted CarrierFence can be supervised.
Inspection/candidate heartbeat/renewal keep that SAME term alive; there is no
enroll, grant, reconciliation, transport replay, or production proof verifier.
Diagnostics are bounded local memory, never a recursive remote logging RPC.
"""
import json
import math
import threading
import time

from .channel_carrier import CarrierError, CarrierFence, PROTOCOL, _duration, _id, _shape, _term
from .networking import canonical
from .retry import BoundedBackoff


class LocalDiagnostics:
    """At most one fixed diagnostic per lane, without IO or exception text."""
    _LANES = frozenset(('poll', 'send', 'maintenance'))
    _CODES = frozenset(('poll_failed', 'send_failed', 'channel_fence_blocked',
        'channel_fence_unconfirmed', 'channel_fence_pending', 'channel_fence_checkpoint_required',
        'channel_fence_request_invalid', 'network_timeout', 'network_error', 'auth_required',
        'transport_failed', 'response_too_large', 'invalid_response', 'redirect_refused',
        'business_-2', 'channel_renew_failed', 'channel_term_changed', 'channel_supervisor_stopped'))

    def __init__(self):
        self._lock, self._errors = threading.Lock(), {}

    def record(self, lane, code, report_failed=False):
        if lane not in self._LANES or code not in self._CODES:
            raise ValueError('channel_diagnostic_invalid')
        with self._lock:
            previous = self._errors.get(lane, {})
            self._errors[lane] = {'code': code, 'at': time.time(),
                'failures': min(2147483647, previous.get('failures', 0) + 1),
                'report_failed': bool(report_failed)}

    def clear(self, lane):
        with self._lock:
            self._errors.pop(lane, None)

    def snapshot(self):
        with self._lock:
            return {lane: dict(error) for lane, error in self._errors.items()}


def validate_config(value):
    """Private owner opt-in options; never grant a term from configuration."""
    if not isinstance(value, dict) or set(value) - {
            'renew_seconds', 'renew_interval', 'candidate_contract', 'candidate_ttl'}:
        raise ValueError('channel_supervisor_config_invalid')
    seconds, interval = value.get('renew_seconds', 60), value.get('renew_interval', 20)
    ttl = value.get('candidate_ttl', 60)
    _duration(seconds)
    _duration(ttl)
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or not 0 < interval < seconds
            or ('candidate_contract' in value and interval >= ttl)
            or ('candidate_contract' in value and not isinstance(value['candidate_contract'], dict))):
        raise ValueError('channel_supervisor_config_invalid')
    # Full candidate binding is checked against the ACTUAL already granted term
    # by ChannelSupervisor; config alone cannot create or choose that identity.
    return json.loads(canonical(value))


class ChannelSupervisor:
    """Explicit single group of poll/send/renew threads for one Channel.

`candidate_contract` is optional, operator-supplied node capability REPORT,
not credential attestation. Without it, the original candidate's TTL eventually
prevents renewal. Supply an updated verified deployment report, not a fabricated
runtime/proof. Construction/start never acquires a new term or changes holder.

Stopping is bounded and reports whether owned threads really ended. A false
return, socket timeout, or stopped supervisor is NOT old-operation quiescence
or migration proof. Already dispatched work retains its original intent.
"""
    def __init__(self, channel, renew_seconds=60, renew_interval=20,
                 candidate_contract=None, candidate_ttl=60):
        fence = getattr(channel, 'carrier_fence', None)
        if not isinstance(fence, CarrierFence):
            raise CarrierError('channel_supervisor_fence_required')
        term = dict(_term(fence.term))
        if term['frozen']:
            raise CarrierError('channel_supervisor_granted_term_required')
        _duration(renew_seconds)
        _duration(candidate_ttl)
        if (isinstance(renew_interval, bool) or not isinstance(renew_interval, (int, float))
                or not math.isfinite(renew_interval) or not 0 < renew_interval < renew_seconds
                or (candidate_contract is not None and renew_interval >= candidate_ttl)):
            raise CarrierError('channel_supervisor_interval_invalid')
        if getattr(channel, '_supervisor', None) is not None:
            raise CarrierError('channel_supervisor_already_owned')
        self.channel, self.fence = channel, fence
        self.seconds, self.interval, self.candidate_ttl = renew_seconds, renew_interval, candidate_ttl
        self._identity = {key: value for key, value in term.items() if key != 'deadline'}
        self.contract = self._candidate(candidate_contract) if candidate_contract is not None else None
        self.ready = threading.Event()
        self.diagnostics = LocalDiagnostics()
        self._lock, self._maintenance_lock = threading.Lock(), threading.Lock()
        self._started, self._threads = False, {}
        # Only this explicitly selected managed channel is paused, not native
        # network/tools or new work elsewhere on the node.
        channel._supervisor = self

    def _candidate(self, value):
        _shape(value, ('protocol', 'node', 'owner_ref', 'account_binding',
            'canonical_authority', 'private_credential_ref', 'revision', 'runtime'))
        _shape(value['runtime'], ('adapter', 'transport_available', 'private_state', 'fence_protocol'))
        _id(value['private_credential_ref'])
        if (value['protocol'] != PROTOCOL or value['node'] != self._identity['holder']
                or any(value[key] != self._identity[key] for key in
                       ('owner_ref', 'account_binding', 'canonical_authority'))
                or isinstance(value['revision'], bool) or not isinstance(value['revision'], int)
                or value['revision'] < 1 or value['runtime']['adapter'] != 'ilink/0.3.0'
                or value['runtime']['fence_protocol'] != PROTOCOL
                or value['runtime']['transport_available'] is not True
                or value['runtime']['private_state'] is not True):
            raise CarrierError('channel_supervisor_candidate_invalid')
        return json.loads(canonical(value))

    def _checked_term(self, value):
        try:
            term = dict(_term(value))
        except Exception:
            raise CarrierError('channel_term_changed') from None
        if any(term[key] != expected for key, expected in self._identity.items()):
            raise CarrierError('channel_term_changed')
        return term

    def _request(self, action, payload):
        if self.channel.stop.is_set():
            raise CarrierError('channel_supervisor_stopped')
        try:
            return self.fence.client.request('/channel-carrier/' + action, payload)
        except Exception:
            raise CarrierError('channel_renew_failed') from None

    def maintenance_once(self):
        """Reinspect then renew original term; unknown ACK permits no new IO.

        A later iteration may inspect/renew that same term again. Renewal is
        not external iLink dispatch; no original operation is ever retried here.
        Server admission rejects expired/frozen terms and unavailable candidates.
        """
        with self._maintenance_lock:
            self.ready.clear()
            try:
                current = self._checked_term(self._request('inspect', {}))
                if self.contract is not None:
                    receipt = self._request('candidate', {'contract': self.contract, 'ttl': self.candidate_ttl})
                    expected = {'protocol': PROTOCOL, 'node': self._identity['holder'], 'candidate': True,
                        'holder_granted': False, 'account_binding': self._identity['account_binding'],
                        'revision': self.contract['revision']}
                    if receipt != expected:
                        raise CarrierError('channel_term_changed')
                renewed = self._checked_term(self._request('renew', {'term': current, 'seconds': self.seconds}))
                if self.channel.stop.is_set():
                    raise CarrierError('channel_supervisor_stopped')
                # Replacing the dict is atomic; already prepared operations may
                # retain the old deadline, but all original term identity matches.
                self.fence.term = renewed
                self.ready.set()
                self.diagnostics.clear('maintenance')
                return dict(renewed)
            except Exception as error:
                code = str(error) if isinstance(error, CarrierError) else 'channel_renew_failed'
                if code not in ('channel_term_changed', 'channel_supervisor_stopped'):
                    code = 'channel_renew_failed'
                self.diagnostics.record('maintenance', code)
                raise CarrierError(code) from None

    def run_maintenance(self):
        backoff = BoundedBackoff(1)
        while not self.channel.stop.is_set():
            try:
                self.maintenance_once()
                backoff.reset()
                delay = self.interval
            except Exception:
                delay = backoff.failed()
            self.channel.stop.wait(delay)
        self.ready.clear()

    def start(self):
        with self._lock:
            if self._started or self.channel.stop.is_set():
                raise CarrierError('channel_supervisor_already_started')
            with self.channel._loop_lock:
                if self.channel._running_loops:
                    raise CarrierError('channel_supervisor_loop_already_running')
            self._started = True
            for lane, target in (('maintenance', self.run_maintenance),
                                 ('poll', self.channel.run_poll), ('send', self.channel.run_send)):
                thread = threading.Thread(target=target, name='mesh-channel-' + lane, daemon=True)
                self._threads[lane] = thread
                thread.start()
        return self

    def stop(self, timeout=5):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 <= timeout <= 60):
            raise ValueError('channel_supervisor_stop_timeout_invalid')
        self.ready.clear()
        self.channel.stop.set()
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in threads)

    def snapshot(self):
        with self._lock:
            alive = {lane: thread.is_alive() for lane, thread in self._threads.items()}
        return {'started': self._started, 'ready': self.ready.is_set(), 'threads_alive': alive,
                'owned_threads_stopped': self._started and not any(alive.values()),
                'errors': self.diagnostics.snapshot(), 'channel_errors': self.channel.local_errors()}
