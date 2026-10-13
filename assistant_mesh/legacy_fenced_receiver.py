"""Opt-in bridge for the frozen 3198 receiver, not a second receiver or HA.

The original server, Worker and all non-ingestion Store methods stay unchanged.
Only owner-held Python hooks can verify a handoff. JSON and HTTP cannot grant.
The shared owner IPC wire remains status/stop, with no start or reset action.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .channel import Channel
from .channel_authority import ChannelAuthority, RemoteChannelState, validate_config, validate_peer
from .channel_carrier import CarrierFence
from .channel_supervisor import ChannelSupervisor
from .config import private_json, read_secret
from .legacy_channel_owner import (
    LegacyChannelOwnerControl, OwnerControlError, _SERVE_LOCK, _stop_timeout,
    request_owner,
)
from .networking import digest
from .remote_notifications import PROTOCOL as NOTIFY_PROTOCOL


# Deployment-specific pins, not a platform requirement for the Mesh protocol.
# The server pin includes only the published carrier HTTP-budget backport.
STORE_SHA256 = '081633994459ffd2fb253e5514ea398d011b0eb8adcf059b251bbde10a705f08'
WORKER_SHA256 = '816a8eb2f6bc5a24a22ae345b1b7b94b0dd5d0ce52f99a6658688e683ce9cbf0'
CHANNEL_SHA256 = 'ed95ff4cfb33f385edc3f319085532adf543f581cdf8303864f6ac1bfc8c5261'
# Filled by the exact published budget patch, never the current HEAD server.
SERVER_SHA256 = 'cc5bb8b492d7e03f68a5f8720eb1399d9ab4ac8e303f46919713c0a6592fd19c'


class OwnedFencedSupervisor(ChannelSupervisor):
    """Real three-thread ownership plus full callback-return observations.

    Unlike a PID or an empty snapshot, unstarted/partial handles never report
    closure. The existing same-term maintenance logic is reused unchanged.
    Local closure always leaves external/canonical/grant claims false.
    """
    _LANES = frozenset(('maintenance', 'poll', 'send'))

    def __init__(self, channel, **options):
        if getattr(channel, 'loop_lifecycle', None) is not None:
            raise OwnerControlError('owner_fenced_group_already_owned')
        super().__init__(channel, **options)
        self._entered, self._returned, self._failed = set(), set(), set()
        self._start_failed = False
        channel.loop_lifecycle = self  # old serve.finally retains these handles

    def _run_owned(self, lane, callback):
        with self._lock:
            self._entered.add(lane)
        try:
            callback()
        except BaseException:
            with self._lock:
                self._failed.add(lane)
            raise
        finally:
            with self._lock:
                self._returned.add(lane)

    def _request_stop(self):
        self.ready.clear()
        # Share the exact admission lock with Channel._run_loop, as the old
        # legacy lifecycle does. Already admitted work is not interrupted.
        with self.channel._loop_lock:
            self.channel.stop.set()

    def run_maintenance(self):
        # A successful owner-controlled initial inspect/renew must not be
        # immediately undone by a racing maintenance clear before poll/send
        # get their first admission. Afterwards reuse the original cadence.
        if self.ready.is_set() and self.channel.stop.wait(self.interval):
            self.ready.clear()
            return
        super().run_maintenance()

    def start(self):
        with self._lock:
            if self._started or self.channel.stop.is_set():
                raise OwnerControlError('owner_fenced_group_already_started')
            with self.channel._loop_lock:
                if self.channel._running_loops:
                    raise OwnerControlError('owner_fenced_loop_already_running')
            callbacks = {'maintenance': self.run_maintenance,
                         'poll': self.channel.run_poll, 'send': self.channel.run_send}
            self._threads = {lane: threading.Thread(target=self._run_owned,
                args=(lane, callback), name='mesh-channel-' + lane, daemon=True)
                for lane, callback in callbacks.items()}
            self._started = True
            try:
                # Explicit start, not construction: same already-granted term
                # only, no poll/send/grant. Failure stays gated while the one
                # owned maintenance loop later retries inspect/renew, not IO.
                try:
                    self.maintenance_once()
                except Exception:
                    self.ready.clear()
                for lane in ('maintenance', 'poll', 'send'):
                    self._threads[lane].start()
            except Exception:
                self._start_failed = True
                self._request_stop()
                raise OwnerControlError('owner_fenced_group_start_failed') from None
        return self

    def stop(self, timeout=5):
        _stop_timeout(timeout)
        self._request_stop()
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = tuple(self._threads.values())
        for thread in threads:
            if (isinstance(thread, threading.Thread) and thread.ident is not None
                    and thread is not threading.current_thread()):
                thread.join(max(0, deadline - time.monotonic()))
        return self.snapshot()['owned_threads_stopped']

    def snapshot(self):
        with self._lock:
            threads = dict(self._threads)
            entered, returned, failed = set(self._entered), set(self._returned), set(self._failed)
            started, partial = self._started, self._start_failed
        actual = (set(threads) == self._LANES
            and all(isinstance(thread, threading.Thread) and thread.ident is not None
                    for thread in threads.values())
            and len({id(thread) for thread in threads.values()}) == 3)
        alive = {lane: thread.is_alive() for lane, thread in threads.items()}
        calls = self.channel.loop_calls()
        with self.channel._loop_lock:
            running = sorted(self.channel._running_loops)
            stopped = self.channel.stop.is_set()
        ended = bool(started and actual and stopped and not any(alive.values())
                     and entered == returned == self._LANES)
        callbacks = bool(stopped and all(not row['active'] and row['admitted'] == row['returned']
                                        for row in calls.values()))
        return {'scope': 'communication_carrier_only', 'runtime': 'fenced',
            'started': started, 'start_failed': partial, 'stop_requested': stopped,
            'actual_owned_thread_handles': actual, 'threads_alive': alive,
            'loop_entries_observed': sorted(entered), 'loop_returns_observed': sorted(returned),
            'loop_failures_observed': sorted(failed), 'owned_threads_stopped': ended,
            'running_loops': running, 'loop_calls': calls,
            'local_callbacks_returned': callbacks,
            'local_stop_closed': bool(ended and callbacks and running == []),
            'ready': self.ready.is_set() and not stopped, 'errors': self.diagnostics.snapshot(),
            'channel_errors': self.channel.local_errors(),
            'external_operation_quiescence_verified': False,
            'canonical_channel_state_closed_verified': False,
            'handoff_authorized': False, 'native_features_restricted': False}


class _DisabledCarrier:
    """Explicit absence of a local carrier, never proof an old one stopped."""
    def stop(self, timeout=5):
        _stop_timeout(timeout)
        return False

    def snapshot(self):
        return {'scope': 'communication_carrier_only', 'runtime': 'disabled',
            'started': False, 'start_failed': False, 'stop_requested': False,
            'actual_owned_thread_handles': False, 'threads_alive': {},
            'loop_entries_observed': [], 'loop_returns_observed': [],
            'loop_failures_observed': [], 'owned_threads_stopped': False,
            'running_loops': [], 'loop_calls': None, 'local_callbacks_returned': False,
            'local_stop_closed': False, 'external_operation_quiescence_verified': False,
            'canonical_channel_state_closed_verified': False,
            'handoff_authorized': False, 'native_features_restricted': False}


class FencedChannelOwnerControl(LegacyChannelOwnerControl):
    """Reuse the verified same-UID/PID IPC, attach only this actual fenced group."""
    def attach(self, http_server, api, channel):
        if self._group is not None or self._closed.is_set() or os.getpid() != self.owner_pid:
            raise OwnerControlError('owner_control_already_attached_or_closed')
        authority = getattr(api, 'channel_authority', None)
        if not isinstance(authority, ChannelAuthority):
            raise OwnerControlError('owner_fenced_authority_unavailable')
        if authority.config['transport_mode'] == 'disabled' and channel is None:
            group = _DisabledCarrier()
        else:
            group = getattr(channel, 'loop_lifecycle', None)
            if (not isinstance(group, OwnedFencedSupervisor) or group.channel is not channel
                    or channel._supervisor is not group
                    or not isinstance(channel.carrier_fence, CarrierFence)
                    or channel.store is not authority.state):
                raise OwnerControlError('owner_fenced_group_unavailable')
            observation = group.snapshot()
            if not observation['started'] or not observation['actual_owned_thread_handles']:
                raise OwnerControlError('owner_fenced_group_unavailable')
        self._channel, self._group = channel, group
        self._thread = threading.Thread(target=self._accept, name='mesh-channel-owner-ipc', daemon=True)
        self._thread.start()

    def attach_remote(self, channel):
        """Retain actual remote carrier handles, not a fabricated local API."""
        if self._group is not None or self._closed.is_set() or os.getpid() != self.owner_pid:
            raise OwnerControlError('owner_control_already_attached_or_closed')
        group = getattr(channel, 'loop_lifecycle', None)
        state = getattr(channel, 'store', None)
        if (not isinstance(group, OwnedFencedSupervisor) or group.channel is not channel
                or channel._supervisor is not group or not isinstance(state, RemoteChannelState)
                or not isinstance(channel.carrier_fence, CarrierFence)
                or state.client is not channel.carrier_fence.client):
            raise OwnerControlError('owner_remote_fenced_group_unavailable')
        observed = group.snapshot()
        if not observed['started'] or not observed['actual_owned_thread_handles']:
            raise OwnerControlError('owner_remote_fenced_group_unavailable')
        self._channel, self._group = channel, group
        self._thread = threading.Thread(target=self._accept, name='mesh-channel-owner-ipc', daemon=True)
        self._thread.start()


def _options(config):
    policy = config.get('notification_policy') if isinstance(config, dict) else None
    if (not isinstance(config, dict) or not config.get('node_id') or not config.get('ilink_account')
            or 'channel_carrier' not in config
            or (policy is not None and (not isinstance(policy, dict)
                                        or policy.get('mode', 'channel') != 'channel'))):
        raise OwnerControlError('owner_fenced_config_required')
    return validate_config(config['channel_carrier'])


def make_api_class(base_api, handoff_verifier=None, operation_verifier=None):
    """Static wrapper around the original API; no request-driven class loading."""
    if any(hook is not None and not callable(hook)
           for hook in (handoff_verifier, operation_verifier)):
        raise OwnerControlError('owner_verifier_invalid')

    class FencedAPI(base_api):
        def __init__(self, config):
            carrier = _options(config)
            # Reject all malformed/aliasing carrier roles BEFORE Store opens.
            peers = []
            for peer in config['peers']:
                if peer.get('role') == 'channel_carrier':
                    validate_peer(peer, carrier['owner_ref'], config['node_id'])
                peers.append((read_secret(peer['token_file']), peer))
            for index, (secret, peer) in enumerate(peers):
                for previous_secret, previous_peer in peers[:index]:
                    if ('channel_carrier' in (peer.get('role'), previous_peer.get('role'))
                            and hmac.compare_digest(secret, previous_secret)):
                        raise ValueError('ambiguous_channel_carrier_credential')
            super().__init__(config)
            # Base API reads private files itself. If an owner edits one during
            # startup, never retain a different/ambiguous principal snapshot
            # from the one checked before opening Store.
            if (len(self.peers) != len(peers) or any(
                    peer != earlier_peer or not hmac.compare_digest(secret, earlier_secret)
                    for (secret, peer), (earlier_secret, earlier_peer) in zip(self.peers, peers))):
                raise ValueError('channel_carrier_credential_changed_during_startup')
            if getattr(self, 'channel_authority', None) is not None:
                raise OwnerControlError('owner_original_api_required')
            if self.ilink is None:
                raise ValueError('channel_carrier_requires_channel_notification_route')
            binding = digest([NOTIFY_PROTOCOL, self.ilink.base,
                self.ilink.account['ilink_user_id'], self.ilink.account['ilink_bot_id']])
            for _, peer in peers:
                if peer.get('role') == 'channel_carrier' and peer['channel']['account_binding'] != binding:
                    raise ValueError('channel_carrier_account_binding_changed')
            self.channel_authority = ChannelAuthority(self.store, carrier,
                config['node_id'], self.ilink, binding,
                handoff_verifier=handoff_verifier, operation_verifier=operation_verifier)

        def dispatch(self, method, path, payload, peer):
            parsed = urlsplit(path)
            if peer.get('role') == 'channel_carrier' or parsed.path.startswith('/channel-carrier/'):
                # Carrier node != Worker/operator. Never fall through, including
                # global status, health, task, resource or ingress actions.
                if (peer.get('role') != 'channel_carrier' or method != 'POST'
                        or parsed.query or parsed.fragment or not parsed.path.startswith('/channel-carrier/')):
                    raise PermissionError('channel_carrier_route_not_authorized')
                authority, binding = self.channel_authority, peer['channel']
                if (binding['account_binding'] != authority.binding
                        or binding['owner_ref'] != authority.owner_ref
                        or binding['canonical_authority'] != authority.authority):
                    raise PermissionError('channel_carrier_binding_changed')
                return authority.dispatch(peer['node'], parsed.path[len('/channel-carrier/'):], payload)
            return super().dispatch(method, path, payload, peer)
    return FencedAPI


def _check_frozen_source(server):
    root = Path(server.__file__).resolve().parent
    pins = {'server.py': SERVER_SHA256, 'store.py': STORE_SHA256,
            'worker.py': WORKER_SHA256, 'channel.py': CHANNEL_SHA256}
    for filename, expected in pins.items():
        try:
            actual = hashlib.sha256((root / filename).read_bytes()).hexdigest()
        except OSError:
            raise OwnerControlError('owner_frozen_receiver_source_missing') from None
        if actual != expected:
            raise OwnerControlError('owner_frozen_receiver_source_changed')


def serve_with_fenced_owner_control(config, socket_path, handoff_verifier=None,
                                   operation_verifier=None, before_channel=None, ready=None):
    """One owner-controlled frozen receiver; no default grant, poll or send.

    `before_channel(api)` is trusted local Python code, never JSON/module input.
    It may inspect original state and install an independently verified term.
    Without that term fenced startup fails before starting any transport lane.
    `disabled` exposes only the original authority RPC, with no local carrier.
    """
    _options(config)
    if any(hook is not None and not callable(hook)
           for hook in (handoff_verifier, operation_verifier, before_channel, ready)):
        raise OwnerControlError('owner_start_hook_invalid')
    from . import server
    _check_frozen_source(server)
    if not _SERVE_LOCK.acquire(False):
        raise OwnerControlError('owner_service_already_running')
    original = (server.Store, server.API, server._start_channel)
    control, owned = None, []

    def passive_store(path, **kwargs):
        kwargs['recover_inflight'] = False
        return original[0](path, **kwargs)

    api_class = make_api_class(original[1], handoff_verifier, operation_verifier)

    def start_channel(api, current_config):
        if before_channel is not None:
            before_channel(api)
        authority = api.channel_authority
        if authority.config['transport_mode'] == 'disabled':
            return None, None
        term = authority.witness.inspect(authority.binding)
        if term['holder'] != current_config['node_id'] or term['frozen']:
            raise OwnerControlError('owner_local_term_not_granted')
        channel = Channel(authority.state, api.ilink,
            CarrierFence(authority.bound_client(current_config['node_id']), term))
        group = OwnedFencedSupervisor(channel, **authority.config.get('supervisor', {}))
        owned.append(group)  # retained even when a thread fails midway through start
        group.start()
        return channel, None  # old server.finally stops channel.loop_lifecycle

    replacements = (passive_store, api_class, start_channel)

    def restore():
        for name, old, replacement in zip(('Store', 'API', '_start_channel'), original, replacements):
            current = getattr(server, name)
            if current is replacement:
                setattr(server, name, old)
            elif current is not old:
                raise OwnerControlError('owner_start_factory_changed')

    def attached(http_server, api, channel):
        restore()  # before HTTP processing; requests never alter module factories
        control.attach(http_server, api, channel)
        if ready is not None:
            ready(http_server, api, channel)

    try:
        control = FencedChannelOwnerControl(socket_path)
        server.Store, server.API, server._start_channel = replacements
        return server.serve(config, ready=attached)
    finally:
        try:
            restore()
        finally:
            for group in owned:
                group.stop(5)
            if control is not None:
                control.close()
            _SERVE_LOCK.release()


def run_remote_with_owner_control(channel, socket_path, ready=None, **supervisor_options):
    """Own an already-configured remote Channel; never construct/grant a Store.

    The trusted deployment supplies its original bound client, account and
    private spool. All canonical IO still reaches the one original authority.
    This moves only a carrier, not authority/state or a failure domain.
    """
    if (not isinstance(channel, Channel) or not isinstance(channel.store, RemoteChannelState)
            or not isinstance(channel.carrier_fence, CarrierFence)
            or channel.store.client is not channel.carrier_fence.client
            or (ready is not None and not callable(ready))):
        raise OwnerControlError('owner_remote_fenced_channel_required')
    if not _SERVE_LOCK.acquire(False):
        raise OwnerControlError('owner_service_already_running')
    control, group = None, None
    try:
        control = FencedChannelOwnerControl(socket_path)
        group = OwnedFencedSupervisor(channel, **supervisor_options)
        group.start()
        control.attach_remote(channel)
        if ready is not None:
            ready(control, group)
        while not channel.stop.wait(0.2):
            pass
    finally:
        try:
            if group is not None:
                group.stop(5)
        finally:
            if control is not None:
                control.close()
            _SERVE_LOCK.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Frozen fenced receiver owner status/stop; no grant command')
    parser.add_argument('--socket', required=True)
    actions = parser.add_subparsers(dest='action')
    service = actions.add_parser('serve')
    service.add_argument('--config', required=True)
    status = actions.add_parser('status')
    status.add_argument('--expected-pid', type=int)
    stop = actions.add_parser('stop')
    stop.add_argument('--expected-pid', type=int, required=True)
    stop.add_argument('--instance-id', required=True)
    stop.add_argument('--timeout', type=float, default=5)
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error('an action is required')
    try:
        if args.action == 'serve':
            serve_with_fenced_owner_control(private_json(args.config), args.socket)
        else:
            print(json.dumps(request_owner(args.socket, args.action,
                instance_id=getattr(args, 'instance_id', None),
                timeout=getattr(args, 'timeout', 5), expected_pid=args.expected_pid),
                ensure_ascii=False, separators=(',', ':')))
    except Exception:
        print(json.dumps({'ok': False, 'error': 'owner_fenced_receiver_unconfirmed'}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
