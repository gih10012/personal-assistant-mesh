"""Independent, zero-turn native observations for an opt-in yielded seal.

The read-only controller is NOT a restriction on the owner's native tools.
Unsupported history/pagination or ambiguous process closure stays unknown.
"""
import copy
import os
import queue
import signal
import socket
import time

from .codex import Codex, CodexError, GOAL_STATUSES
from .native_ws import NativeSocketError, UnixWebSocket


def validate_goal_continuity(baseline, goal, thread_id):
    if not isinstance(thread_id, str) or not thread_id:
        raise CodexError('codex_native_goal_continuity_unverified')
    for value in (baseline, goal):
        if (not isinstance(value, dict) or value.get('threadId') != thread_id
                or not isinstance(value.get('objective'), str)
                or not value['objective'].strip() or len(value['objective']) > 4000
                or not isinstance(value.get('status'), str)
                or value['status'] not in GOAL_STATUSES):
            raise CodexError('codex_native_goal_continuity_unverified')
        for key in ('createdAt', 'updatedAt', 'tokensUsed', 'timeUsedSeconds'):
            number = value.get(key)
            if not isinstance(number, int) or isinstance(number, bool) or number < 0:
                raise CodexError('codex_native_goal_continuity_unverified')
        budget = value.get('tokenBudget')
        if (budget is not None and (not isinstance(budget, int)
                or isinstance(budget, bool) or budget < 0)):
            raise CodexError('codex_native_goal_continuity_unverified')
        if value['updatedAt'] < value['createdAt']:
            raise CodexError('codex_native_goal_continuity_unverified')
    if (any(goal.get(key) != baseline.get(key) for key in
            ('threadId', 'objective', 'createdAt', 'tokenBudget'))
            or any(goal[key] < baseline[key] for key in
                   ('tokensUsed', 'timeUsedSeconds', 'updatedAt'))):
        raise CodexError('codex_native_goal_continuity_unverified')
    return copy.deepcopy(goal)


class NativeGoalObserver(Codex):
    """An owned new app-server that never loads or mutates the original thread."""
    READ_METHODS = frozenset(('initialize', 'thread/loaded/list', 'thread/read',
                              'thread/turns/list', 'thread/goal/get'))

    def __init__(self, config, thread_id, tick, deadline):
        self.observed_thread_id, self.observer_tick = thread_id, tick
        self.observer_deadline = deadline
        selected = dict(config, native_transport='unix', native_goal_drain=False,
                        native_goal_yield=False, native_memories=False,
                        strict_auth_home=True)
        # No original tools, host callbacks or model-producing initialization.
        super().__init__(selected)
        if self._socket_owner_verified is not True:
            self.close()
            raise CodexError('codex_native_observer_boundary_unknown')

    def _check_deadline(self):
        if time.monotonic() >= self.observer_deadline:
            raise CodexError('codex_native_observer_timeout')
        if self.observer_tick:
            self.observer_tick()
        if time.monotonic() >= self.observer_deadline:
            raise CodexError('codex_native_observer_timeout')

    def _connect_native_socket(self):
        # Same ownership checks as the normal transport, with heartbeat during
        # startup. The Unix handshake is bounded, not a hard real-time callback.
        self._socket_owner_verified = False
        deadline = min(self.observer_deadline, time.monotonic() + 10)
        while not os.path.lexists(self._socket_path):
            self._check_deadline()
            if self.process.poll() is not None:
                raise CodexError('codex_native_transport_start_failed')
            if time.monotonic() >= deadline:
                raise CodexError('codex_native_transport_start_timeout')
            time.sleep(.05)
        self._check_deadline()
        target, binding = self._native_socket_binding()
        self._socket_binding, self._socket_target = binding, target
        try:
            self._native_socket = UnixWebSocket(target,
                timeout=max(.01, deadline - time.monotonic()),
                expected_peer_pid=self.process.pid, expected_peer_uid=os.geteuid())
            if self._native_socket_binding() != (target, binding):
                raise CodexError('codex_native_transport_path_unsafe')
            if self.process.poll() is not None:
                raise CodexError('codex_native_transport_start_failed')
            self._check_deadline()
            self._socket_owner_verified = True
        except (NativeSocketError, socket.timeout, EOFError):
            raise CodexError('codex_native_transport_connect_failed') from None

    def rpc(self, method, params, timeout=30):
        if method not in self.READ_METHODS or (method not in
                ('initialize', 'thread/loaded/list') and
                params.get('threadId') != self.observed_thread_id):
            raise CodexError('codex_native_observer_rpc_blocked')
        self._check_deadline()
        result = super().rpc(method, params,
            timeout=min(timeout, max(.01, self.observer_deadline - time.monotonic())))
        self._check_deadline()
        return result

    def send(self, value):
        if value.get('method') not in self.READ_METHODS | {'initialized'}:
            raise CodexError('codex_native_observer_rpc_blocked')
        super().send(value)

    def _inspect_message(self, value):
        if not isinstance(value, dict) or value.get('transport_error'):
            raise CodexError('codex_native_observer_boundary_unknown')
        if 'id' in value and 'method' in value:
            # Do not execute or reply to unexpected host work, even on another
            # thread. This controller never called start/resume.
            raise CodexError('codex_native_observer_unexpected_work')
        if 'id' in value:
            identity = value['id']
            if (isinstance(identity, bool) or not isinstance(identity, (int, str))
                    or (isinstance(identity, str) and not identity)
                    or identity not in self._rpc_pending):
                raise CodexError('codex_native_observer_boundary_unknown')
        method, params = value.get('method'), value.get('params')
        if method in ('turn/started', 'item/started', 'thread/started'):
            raise CodexError('codex_native_observer_unexpected_work')
        if isinstance(params, dict) and params.get('threadId') == self.observed_thread_id:
            if method in ('thread/goal/updated', 'thread/goal/cleared', 'turn/completed'):
                raise CodexError('codex_native_observer_unexpected_work')
            if method == 'thread/status/changed' and params.get('status') != {'type': 'notLoaded'}:
                raise CodexError('codex_native_observer_unexpected_work')

    def event(self, timeout=20):
        until = min(self.observer_deadline, time.monotonic() + timeout)
        while True:
            self._check_deadline()
            remaining = until - time.monotonic()
            if remaining <= 0:
                raise CodexError('codex_timeout')
            try:
                value = self.events.get(timeout=min(.25, remaining))
            except queue.Empty:
                continue
            self._inspect_message(value)
            if value.get('eof'):
                raise CodexError('codex_disconnected')
            self._check_deadline()
            return value

    def unloaded(self):
        cursor, seen = None, set()
        for _ in range(2048):
            response = self.rpc('thread/loaded/list', {'cursor': cursor, 'limit': 128})
            data = response.get('data') if isinstance(response, dict) else None
            if (not isinstance(data, list)
                    or any(not isinstance(identity, str) or not identity for identity in data)
                    or self.observed_thread_id in data):
                raise CodexError('codex_native_observer_thread_loaded')
            if 'nextCursor' not in response:
                raise CodexError('codex_native_observer_snapshot_invalid')
            cursor = response['nextCursor']
            if cursor is None:
                return True
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise CodexError('codex_native_observer_pagination_invalid')
            seen.add(cursor)
        raise CodexError('codex_native_observer_pagination_invalid')

    def read_thread(self, path, include_turns=False):
        response = self.rpc('thread/read', {'threadId': self.observed_thread_id,
                                          'includeTurns': include_turns})
        thread = response.get('thread') if isinstance(response, dict) else None
        if (not isinstance(thread, dict) or thread.get('id') != self.observed_thread_id
                or thread.get('status') != {'type': 'notLoaded'}
                or thread.get('path') != path):
            raise CodexError('codex_native_observer_snapshot_invalid')
        return thread

    def read_goal(self, baseline):
        response = self.rpc('thread/goal/get', {'threadId': self.observed_thread_id})
        return validate_goal_continuity(baseline,
            response.get('goal') if isinstance(response, dict) else None, self.observed_thread_id)

    def closed_turns(self, path, closed):
        cursor, cursors, identities, verified = None, set(), set(), set()
        for page in range(2048):
            try:
                response = self.rpc('thread/turns/list', {
                    'threadId': self.observed_thread_id, 'cursor': cursor, 'limit': 128,
                    'sortDirection': 'asc', 'itemsView': 'full'})
            except CodexError as exc:
                error = getattr(self, 'last_protocol_error', None)
                if (page != 0 or str(exc) != 'codex_rpc_failed_thread_turns_list'
                        or not isinstance(error, dict) or error.get('code') != -32601):
                    raise
                # Only a definite missing method may use legacy full hydration.
                # A paginated/full-read error, timeout, or incomplete response
                # never becomes an empty successful history observation.
                thread = self.read_thread(path, include_turns=True)
                if not isinstance(thread.get('turns'), list):
                    raise CodexError('codex_native_observer_snapshot_invalid')
                response = {'data': thread['turns'], 'nextCursor': None}
            turns = response.get('data') if isinstance(response, dict) else None
            if not isinstance(turns, list) or 'nextCursor' not in response:
                raise CodexError('codex_native_observer_snapshot_invalid')
            for turn in turns:
                identity = turn.get('id') if isinstance(turn, dict) else None
                if (not isinstance(identity, str) or not identity or identity in identities
                        or not isinstance(turn.get('items'), list)
                        # Both installed official schemas default omission to
                        # full; explicit summary/notLoaded is never sufficient.
                        or turn.get('itemsView', 'full') != 'full'):
                    raise CodexError('codex_native_observer_snapshot_invalid')
                identities.add(identity)
                if identity in closed:
                    if turn.get('status') != 'completed' or turn.get('error') is not None:
                        raise CodexError('codex_native_observer_turn_unsettled')
                    verified.add(identity)
            cursor = response['nextCursor']
            if cursor is None:
                if verified != set(closed):
                    raise CodexError('codex_native_observer_turn_unsettled')
                return sorted(verified)
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise CodexError('codex_native_observer_pagination_invalid')
            cursors.add(cursor)
        raise CodexError('codex_native_observer_pagination_invalid')

    def finish_read_only(self):
        self._check_deadline()
        if (self._socket_owner_verified is not True or self._rpc_pending
                or self.process.poll() is not None or self._term_sent):
            raise CodexError('codex_native_observer_boundary_unknown')
        self._term_sent = True
        try:
            os.kill(self.process.pid, signal.SIGTERM)
        except OSError:
            raise CodexError('codex_native_observer_boundary_unknown') from None
        eof = False
        while True:
            self._check_deadline()
            try:
                value = self.deferred.pop(0) if self.deferred else self.events.get(timeout=.25)
            except queue.Empty:
                value = None
            if value is not None:
                self._inspect_message(value)
                if value.get('eof'):
                    if eof:
                        raise CodexError('codex_native_observer_boundary_unknown')
                    eof = True
                elif eof:
                    raise CodexError('codex_native_observer_boundary_unknown')
            if eof and not self.deferred and self.events.empty() and self.process.poll() is not None:
                if self.process.poll() != 0 or self._native_cleanup_forced or self._reader_forced_close:
                    raise CodexError('codex_native_observer_boundary_unknown')
                self.process.wait(timeout=0)
                self.reader.join(timeout=.1)
                self._check_deadline()
                if not self.reader.is_alive():
                    return {'thread_id': self.observed_thread_id, 'pid': self.process.pid,
                            'natural_exit_code': 0, 'reader_eof': True, 'read_only': True,
                            'unloaded_before': True, 'unloaded_after': True}


def observe_native_yield(config, thread_id, path, baseline, closed_turn_ids, tick, deadline):
    observer = NativeGoalObserver(config, thread_id, tick, deadline)
    try:
        observer.unloaded()
        observer.read_thread(path)
        goal = observer.read_goal(baseline)
        verified = observer.closed_turns(path, closed_turn_ids)
        observer.read_thread(path)
        if observer.read_goal(goal) != goal:
            raise CodexError('codex_native_observer_snapshot_changed')
        observer.unloaded()
        proof = observer.finish_read_only()
        proof['turns_verified'] = verified
        return {'goal': goal, 'thread_status': {'type': 'notLoaded'},
                'independent_observer': proof}
    finally:
        # Failure cleanup cannot turn an ambiguous observation into proof.
        observer.close()
