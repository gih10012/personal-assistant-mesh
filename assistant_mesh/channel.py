"""Independent iLink transport. No personal-WeChat desktop dependency.

Wire shape verified against wechatbot-sdk 0.3.0 and Tencent/openclaw-weixin.
Secrets/raw attachment references remain in private runtime state.
"""
import base64
import json
import math
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import private_json
from .channel_carrier import CarrierError, CarrierFence
from .channel_supervisor import BoundedBackoff, LocalDiagnostics
from .networking import digest
from .remote_notifications import PROTOCOL as NOTIFY_PROTOCOL


class ChannelError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ChannelError('redirect_refused')


class ILink:
    def __init__(self, account_path, request=None):
        self.account = private_json(account_path)
        origin = urllib.parse.urlsplit(self.account['baseurl'])
        if (origin.scheme != 'https' or not origin.hostname or not origin.hostname.endswith('.weixin.qq.com')
                or origin.username or origin.password or origin.path not in ('', '/') or origin.query or origin.fragment
                or origin.port not in (None, 443)):
            raise ValueError('untrusted_ilink_origin')
        self.base = 'https://' + origin.hostname
        self.request_override = request
        for key in ('bot_token', 'ilink_user_id', 'ilink_bot_id'):
            if not self.account.get(key):
                raise ValueError('ilink_credentials_missing')

    def request(self, endpoint, body, timeout):
        if self.request_override:
            return self.request_override(endpoint, body, timeout)
        body = dict(body, base_info={'channel_version': '0.3.0', 'bot_agent': 'PersonalAssistantMesh/0.1.0'})
        headers = {'Content-Type': 'application/json', 'AuthorizationType': 'ilink_bot_token',
                   'Authorization': 'Bearer ' + self.account['bot_token'],
                   'X-WECHAT-UIN': base64.b64encode(str(struct.unpack('>I', os.urandom(4))[0]).encode()).decode(),
                   'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': '768'}
        req = urllib.request.Request(self.base + '/ilink/bot/' + endpoint, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ChannelError('response_too_large')
                value = json.loads(raw)
        except (socket.timeout, TimeoutError):
            raise ChannelError('network_timeout') from None
        except urllib.error.HTTPError as exc:
            raise ChannelError('http_' + str(exc.code)) from None
        except (urllib.error.URLError, OSError):
            raise ChannelError('network_error') from None
        except (ValueError, TypeError):
            raise ChannelError('invalid_response') from None
        if not isinstance(value, dict):
            raise ChannelError('invalid_response')
        codes = [value.get('ret'), value.get('errcode')]
        if -14 in codes:
            raise ChannelError('auth_required')
        for code in codes:
            if code not in (None, 0):
                raise ChannelError('business_' + str(code))
        return value

    def poll(self, cursor):
        return self.request('getupdates', {'get_updates_buf': cursor}, 40)

    def send_body(self, row):
        """One shared body builder for the wire call and its fenced fingerprint."""
        message = {'from_user_id': '', 'to_user_id': self.account['ilink_user_id'], 'client_id': row['client_id'],
                   'message_type': 2, 'message_state': 2,
                   'item_list': [{'type': 1, 'text_item': {'text': row['body']}}]}
        if row.get('media_items'):
            message['item_list'] = json.loads(row['media_items'])
        if row.get('context_token'):
            message['context_token'] = row['context_token']
        return {'msg': message}

    def send(self, row):
        return self.request('sendmessage', self.send_body(row), 15)


class Channel:
    def __init__(self, store, transport, carrier_fence=None):
        self.store, self.transport = store, transport
        self.carrier_fence = carrier_fence
        self.stop = threading.Event()
        self._supervisor = None
        self.loop_lifecycle = None
        self._diagnostics = LocalDiagnostics()
        self._loop_lock, self._running_loops = threading.Lock(), set()
        self._loop_calls = {lane: {'admitted': 0, 'returned': 0, 'active': False}
                            for lane in ('poll', 'send')}
        if carrier_fence is not None:
            if not isinstance(carrier_fence, CarrierFence):
                raise ChannelError('channel_fence_invalid')
            account = transport.account
            binding = digest([NOTIFY_PROTOCOL, getattr(transport, 'base', None),
                              account.get('ilink_user_id'), account.get('ilink_bot_id')])
            previous = store.get('owner_notification_channel_binding')
            if (binding != carrier_fence.term['account_binding']
                    or (previous is not None and previous != binding)):
                raise ChannelError('channel_fence_account_mismatch')
            if not isinstance(store.get('cursor'), str):
                raise ChannelError('channel_fence_checkpoint_required')
            required = ('channel_prepare_poll', 'channel_prepare_send', 'channel_mark_dispatched',
                        'channel_record_result', 'channel_commit_poll', 'channel_commit_send',
                        'channel_mark_uncertain')
            if not all(callable(getattr(store, name, None)) for name in required):
                raise ChannelError('channel_fence_store_unavailable')
            if not callable(getattr(transport, 'send_body', None)):
                raise ChannelError('channel_fence_transport_unavailable')
            # The proof-bound canonical checkpoint must already be installed.
            # A candidate must NEVER execute old account.pending commands or
            # overwrite the original context by constructing another Channel.
            return
        if self.store.get('cursor') is None:
            account = transport.account
            self.store.ingest(account.get('pending', []), account.get('cursor', ''), account['ilink_user_id'], account['ilink_bot_id'])
            context = account.get('owner_context', {})
            if context.get('user_id') == account['ilink_user_id'] and context.get('token') and not self.store.get('owner_context'):
                self.store.set('owner_context', {'token': context['token'],
                                               'version': context.get('message_fingerprint', 'imported'),
                                               'created': context.get('message_created_at_ms', 0)})

    def poll_once(self):
        if self.carrier_fence is not None:
            self._check_supervisor()
            return self._poll_fenced()
        account = self.transport.account
        batch = self.transport.poll(self.store.get('cursor', account.get('cursor', '')))
        result = self.store.ingest(batch.get('msgs'), batch.get('get_updates_buf'), account['ilink_user_id'], account['ilink_bot_id'])
        self.store.set('channel_error', None)
        return result

    def send_once(self):
        if self.carrier_fence is not None:
            self._check_supervisor()
            return self._send_fenced()
        row = self.store.next_send()
        if not row:
            return False
        try:
            value = self.transport.send(row)
            self.store.finish_send(row['id'], 'accepted', {'delivery_verified': False, 'message_id': value.get('message_id')})
        except ChannelError as exc:
            status = 'rejected' if exc.code == 'business_-2' else 'waiting_auth' if exc.code == 'auth_required' else 'unknown'
            self.store.finish_send(row['id'], status, {'code': exc.code, 'delivery_verified': False})
        except Exception:
            self.store.finish_send(row['id'], 'unknown', {'code': 'transport_failed', 'delivery_verified': False})
        return True

    @staticmethod
    def _transport_error(error):
        code = error.code if isinstance(error, ChannelError) else 'transport_failed'
        if code not in ('business_-2', 'auth_required', 'network_timeout', 'network_error',
                        'response_too_large', 'invalid_response', 'redirect_refused'):
            code = 'transport_failed'
        status = 'rejected' if code == 'business_-2' else 'waiting_auth' if code == 'auth_required' else 'unknown'
        return {'status': status, 'detail': {'code': code, 'delivery_verified': False}}

    def _run_fenced(self, operation_id, kind, identity, body, call, persist):
        def admitted(ticket):
            # The token is only available after a fresh dispatch ACK. This
            # original local intent must be durable BEFORE the iLink call.
            self.store.channel_mark_dispatched(operation_id, ticket)
        try:
            return self.carrier_fence.run(operation_id, kind, identity,
                digest({'endpoint': 'getupdates' if kind == 'poll' else 'sendmessage', 'body': body}),
                call, persist, on_dispatch=admitted,
                uncertain=lambda result: result['status'] == 'unknown')
        except Exception:
            # This only records the original guard; it never rolls pending
            # back, changes client_id, repeats IO, or calls bare finish_send.
            try:
                self.store.channel_mark_uncertain(operation_id)
            except Exception:
                pass
            raise ChannelError('channel_fence_unconfirmed') from None

    def _poll_fenced(self):
        account, term = self.transport.account, self.carrier_fence.term
        cursor = self.store.get('cursor')
        if not isinstance(cursor, str):
            raise ChannelError('channel_fence_checkpoint_required')
        prepared = self.store.channel_prepare_poll(term, cursor)
        if prepared is None:
            raise ChannelError('channel_fence_pending')
        operation_id, accepted = prepared['operation_id'], [None]

        def call():
            try:
                outcome = {'status': 'updates', 'batch': self.transport.poll(cursor)}
            except Exception as error:
                outcome = self._transport_error(error)
            # Full private batch/status is preserved before a remote outcome
            # ACK can be lost. No cursor/context/task write occurs here.
            self.store.channel_record_result(operation_id, outcome)
            return outcome

        def persist(outcome, ticket):
            receipt = self.store.channel_commit_poll(operation_id, ticket, outcome,
                account['ilink_user_id'], account['ilink_bot_id'])
            accepted[0] = receipt['accepted']
            return receipt['sha256']

        outcome = self._run_fenced(operation_id, 'poll',
            {'authority': term['canonical_authority'], 'cursor_sha256': digest(cursor)},
            {'get_updates_buf': cursor}, call, persist)
        if outcome['status'] != 'updates':
            raise ChannelError(outcome['detail']['code'])
        return accepted[0]

    def _send_fenced(self):
        term = self.carrier_fence.term
        prepared = self.store.channel_prepare_send(term)
        if prepared is None:
            return False
        operation_id, row = prepared['operation_id'], prepared['row']
        try:
            body = self.transport.send_body(row)
        except Exception:
            self.store.channel_mark_uncertain(operation_id)
            raise ChannelError('channel_fence_request_invalid') from None

        def call():
            try:
                value = self.transport.send(row)
                outcome = {'status': 'accepted', 'response': value,
                           'detail': {'delivery_verified': False, 'message_id': value.get('message_id')}}
            except Exception as error:
                outcome = self._transport_error(error)
            self.store.channel_record_result(operation_id, outcome)
            return outcome

        def persist(outcome, ticket):
            return self.store.channel_commit_send(operation_id, ticket,
                                                  outcome['status'], outcome['detail'])

        self._run_fenced(operation_id, 'send', {'authority': term['canonical_authority'],
            'outbox_id': row['id'], 'client_id': row['client_id']}, body, call, persist)
        return True

    def _check_supervisor(self):
        if self._supervisor is not None and not self._supervisor.ready.is_set():
            raise ChannelError('channel_fence_blocked')

    def local_errors(self):
        """Bounded fixed diagnostics remain readable when the authority is down."""
        return self._diagnostics.snapshot()

    def _loop_error(self, lane, error):
        code = error.code if isinstance(error, ChannelError) else lane + '_failed'
        if lane == 'send' and isinstance(error, (ChannelError, CarrierError)):
            code = 'channel_fence_blocked'
        if code not in LocalDiagnostics._CODES:
            code = lane + '_failed'
        # Fenced/remote errors must not make a second call into the unavailable
        # authority just to report the first error. Original outcomes/guards
        # are handled by the existing atomic canonical intent path, not logging.
        report_failed = False
        if self.carrier_fence is None:
            try:
                self.store.set('channel_error', {'code': code, 'at': time.time()})
            except Exception:
                report_failed = True
        self._diagnostics.record(lane, code, report_failed)

    def _run_loop(self, lane, operation, interval):
        with self._loop_lock:
            if lane in self._running_loops:
                return  # Never start a replacement while the original is alive.
            self._running_loops.add(lane)
        backoff = BoundedBackoff(interval)
        try:
            while not self.stop.is_set():
                # Admission and an owner lifecycle's stop request share this
                # lock. A callback already admitted is not cancelled/replayed;
                # no NEW callback is admitted after that stop boundary.
                with self._loop_lock:
                    if self.stop.is_set():
                        break
                    state = self._loop_calls[lane]
                    state['admitted'] += 1
                    state['active'] = True
                try:
                    try:
                        operation()
                    finally:
                        with self._loop_lock:
                            state['returned'] += 1
                            state['active'] = False
                    self._diagnostics.clear(lane)
                    delay = backoff.reset()
                except Exception as error:
                    self._loop_error(lane, error)
                    # Preserve legacy send's normal/error cadence. Opt-in
                    # remote loops instead back off on failed reconnects.
                    delay = interval if lane == 'send' and self.carrier_fence is None else backoff.failed()
                self.stop.wait(delay)
        finally:
            with self._loop_lock:
                self._running_loops.remove(lane)

    def run_poll(self):
        self._run_loop('poll', self.poll_once, 1)

    def run_send(self):
        self._run_loop('send', self.send_once, 0.5)

    def loop_calls(self):
        """Actual local loop callback returns, not canonical/external closure."""
        with self._loop_lock:
            return {lane: dict(state) for lane, state in self._loop_calls.items()}


class ChannelLoopLifecycle:
    """Owner-held legacy/unsupervised loop handles; no term or proof authority.

    The server still starts the same poll/send callbacks with the same cadence.
    This group retains their actual Thread objects and observes both callback
    entry and return. Stop only sets this Channel's existing event and joins
    these owned threads, never an HTTP server, worker, Leader or history writer.
    A deadline does not cancel an iLink call or turn an unknown send into success.
    """
    def __init__(self, channel):
        if (getattr(channel, 'loop_lifecycle', None) is not None
                or getattr(channel, '_supervisor', None) is not None
                or not isinstance(getattr(channel, 'stop', None), threading.Event)
                or not all(callable(getattr(channel, 'run_' + lane, None))
                           for lane in ('poll', 'send'))):
            raise ChannelError('channel_loop_lifecycle_unavailable')
        self.channel = channel
        self._lock = threading.Lock()
        self._started, self._start_failed = False, False
        self._threads, self._entered, self._returned, self._failed = {}, set(), set(), set()
        channel.loop_lifecycle = self

    def _run(self, lane, target):
        with self._lock:
            self._entered.add(lane)
        try:
            target()
        except BaseException:
            with self._lock:
                self._failed.add(lane)
            raise
        finally:
            with self._lock:
                self._returned.add(lane)

    def _request_stop(self):
        lock = getattr(self.channel, '_loop_lock', None)
        if lock is None:
            self.channel.stop.set()
        else:
            with lock:
                self.channel.stop.set()

    def start(self):
        with self._lock:
            if self._started or self.channel.stop.is_set():
                raise ChannelError('channel_loop_lifecycle_already_started')
            loop_lock = getattr(self.channel, '_loop_lock', None)
            if loop_lock is not None:
                with loop_lock:
                    if self.channel._running_loops:
                        raise ChannelError('channel_loop_already_running')
            self._threads = {lane: threading.Thread(target=self._run,
                args=(lane, getattr(self.channel, 'run_' + lane)),
                name='mesh-channel-' + lane, daemon=True) for lane in ('poll', 'send')}
            # Keep even a partial start. It cannot become a fresh group or
            # evidence that an unstarted/missing handle has returned.
            self._started = True
            try:
                for lane in ('poll', 'send'):
                    self._threads[lane].start()
            except Exception:
                self._start_failed = True
                self._request_stop()
                raise ChannelError('channel_loop_start_failed') from None
        return self

    def stop(self, timeout=5):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 <= timeout <= 60):
            raise ValueError('channel_loop_stop_timeout_invalid')
        self._request_stop()
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            if (isinstance(thread, threading.Thread) and thread.ident is not None
                    and thread is not threading.current_thread()):
                thread.join(max(0, deadline - time.monotonic()))
        return self.snapshot()['owned_threads_stopped']

    def snapshot(self):
        with self._lock:
            threads = dict(self._threads)
            entered, returned, failed = set(self._entered), set(self._returned), set(self._failed)
            started, start_failed = self._started, self._start_failed
        actual = (set(threads) == {'poll', 'send'}
            and all(isinstance(thread, threading.Thread) and thread.ident is not None
                    for thread in threads.values())
            and len({id(thread) for thread in threads.values()}) == 2)
        alive = {lane: thread.is_alive() if isinstance(thread, threading.Thread) else None
                 for lane, thread in threads.items()}
        stopped = self.channel.stop.is_set()
        ended = (started and actual and stopped and not any(alive.values())
                 and entered == returned == {'poll', 'send'})
        observe = getattr(self.channel, 'loop_calls', None)
        calls = observe() if callable(observe) else None
        loop_lock = getattr(self.channel, '_loop_lock', None)
        if loop_lock is not None:
            with loop_lock:
                running = sorted(self.channel._running_loops)
        else:
            running = None
        callbacks_returned = (stopped and calls is not None and all(
            not row['active'] and row['admitted'] == row['returned'] for row in calls.values()))
        return {'scope': 'communication_carrier_only', 'started': started,
            'start_failed': start_failed, 'stop_requested': stopped,
            'actual_owned_thread_handles': actual, 'threads_alive': alive,
            'loop_entries_observed': sorted(entered), 'loop_returns_observed': sorted(returned),
            'loop_failures_observed': sorted(failed), 'owned_threads_stopped': bool(ended),
            'running_loops': running, 'loop_calls': calls,
            'local_callbacks_returned': bool(callbacks_returned),
            'local_stop_closed': bool(ended and callbacks_returned and running == []),
            'external_operation_quiescence_verified': False,
            'canonical_channel_state_closed_verified': False,
            'handoff_authorized': False, 'native_features_restricted': False}
