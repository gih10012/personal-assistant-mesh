"""Thin opt-in adapter around the existing, fully composed Worker instance.

Call run_worker(lambda: OriginalComposedWorker(...)) from its original launcher.
The factory runs only after standby readiness and activation. Keep the same
config, governance, harness, sessions, observers and receiver elsewhere.
"""
import argparse
import copy
import os
import secrets
import threading
import urllib.error

from .channel_supervisor import BoundedBackoff
from .rolling_runtime import request


TERMINAL = ('completed', 'failed', 'needs_review', 'waiting_children',
            'continuing', 'waiting_backend', 'waiting_auth')


class RunnerSession:
    def __init__(self, socket_path=None, runner=None, token=None):
        self.socket_path = socket_path or os.environ['AMITA_RUNTIME_SOCKET']
        self.runner = runner or os.environ['AMITA_RUNNER_ID']
        self.token = token or os.environ['AMITA_RUNNER_TOKEN']
        self.state, self.busy = 'standby', False
        self.connected = False
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def call(self, action, body=None):
        try:
            result = request(self.socket_path, action, body, self.runner, self.token)
        except (OSError, ValueError):
            self.connected = False
            raise
        with self.lock:
            self.connected = True
            self.state = result.get('state', self.state)
            self.busy = result.get('busy', self.busy)
        return result

    def start(self):
        self.call('ready')

        def heartbeat():
            backoff = BoundedBackoff(1, 5)
            while not self.stop.is_set():
                try:
                    self.call('heartbeat')
                    delay = backoff.reset()
                except (OSError, ValueError):
                    delay = backoff.failed()
                self.stop.wait(delay)
        self.thread = threading.Thread(target=heartbeat, daemon=True)
        self.thread.start()
        return self

    def wait_active(self):
        while not self.stop.wait(0.1):
            if self.connected and self.state == 'active':
                return True
            if self.connected and self.state in ('draining', 'retained', 'stopped'):
                return False
        return False

    def close(self):
        self.stop.set()
        if hasattr(self, 'thread'):
            self.thread.join(5.1)


def safe_boundary(task, update):
    """Authority ACK + original task/session seal, never first-turn heuristics."""
    if not update or update.get('status') not in TERMINAL:
        return False
    checkpoint = task.get('checkpoint', {})
    failure = (update.get('checkpoint') or {}).get('runtime_failure')
    if failure:
        # A backend preflight that never opened native work can be retained by
        # the authority and later claimed under its ORIGINAL task identity.
        return (failure.get('attempt_epoch') == task.get('epoch')
                and failure.get('native_start_attempted') is False
                and checkpoint.get('side_effect_started') is not True)
    if checkpoint.get('side_effect_started') is not False:
        return False
    lifecycle, goal = checkpoint.get('native_lifecycle'), checkpoint.get('goal')
    managed = isinstance(lifecycle, dict) and (lifecycle.get('controller_enabled') or lifecycle.get('goal_managed'))
    if managed or (isinstance(goal, dict) and goal.get('status') == 'active'):
        if (not isinstance(lifecycle, dict) or lifecycle.get('settled') is not True
                or lifecycle.get('runtime_closed') is not True
                or lifecycle.get('thread_id') != checkpoint.get('thread_id')
                or lifecycle.get('goal') != goal):
            return False
        if isinstance(goal, dict) and goal.get('status') == 'active':
            return (lifecycle.get('outcome') == 'yielded' and update['status'] == 'waiting_children'
                    and goal.get('threadId') == checkpoint.get('thread_id'))
        if lifecycle.get('outcome') != 'terminal':
            return False
    return True


class ClaimGate:
    """Original Client passthrough; only admission and receipt observation.

    No transaction/lock spans a network or native call. An ACK-lost claim or
    final update stays unknown and is NEVER sent again by this adapter.
    """
    def __init__(self, original, session):
        self.original, self.session = original, session
        self.task, self.final_update = None, None
        self.unknown = False
        self.pending_checkpoint = None

    def __getattr__(self, name):
        return getattr(self.original, name)

    def _unknown(self):
        self.unknown = True
        try:
            self.session.call('unknown')
        except (OSError, ValueError):
            pass

    def request(self, route, body=None):
        if route == '/v1/claim':
            if self.unknown or self.pending_checkpoint is not None:
                return {'task': None}
            claim = secrets.token_hex(16)
            permission = self.session.call('admit', {'claim': claim})
            if not permission.get('admitted'):
                return {'task': None}
            try:
                result = self.original.request(route, body)
                task = result['task']
                # The private runtime journal needs identities, not prompts,
                # native history, attachments or a second business checkpoint.
                identity = {k: task[k] for k in ('id', 'epoch')} if task else None
                self.session.call('bind', {'claim': claim, 'task': identity})
            except (OSError, ValueError, KeyError, urllib.error.URLError):
                self._unknown()
                raise
            self.task, self.final_update = copy.deepcopy(task), None
            return result
        if route != '/v1/task/update' or not self.task or body.get('id') != self.task['id']:
            return self.original.request(route, body)
        try:
            result = self.original.request(route, body)
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise ValueError('runtime_task_ack_unconfirmed')
        except (OSError, ValueError, urllib.error.URLError):
            self._unknown()
            raise
        self.task.setdefault('checkpoint', {}).update(copy.deepcopy(body.get('checkpoint') or {}))
        if body.get('status') is not None:
            self.final_update = copy.deepcopy(body)
        return result

    def finish_boundary(self, worker):
        if not self.task:
            return
        if (self.unknown or worker.current is not None or worker.agent is not None
                or getattr(worker, '_native_starting', False)
                or getattr(worker, '_native_turn_live', None)
                or not safe_boundary(self.task, self.final_update)):
            self._unknown()
            return
        self.pending_checkpoint = {k: self.task[k] for k in ('id', 'epoch')}
        self.flush_checkpoint()

    def flush_checkpoint(self):
        if self.pending_checkpoint is None:
            return
        # Only the idempotent local control ACK is retried; the original
        # authority/native/session/operation is never called a second time.
        self.session.call('checkpoint', {'task': self.pending_checkpoint, 'safe': True})
        self.pending_checkpoint = None
        self.task, self.final_update = None, None


def run_worker(factory, session=None):
    """Replace ONLY the launcher's final Worker(...).run(), not its assembly.

    Active native Goals keep their original run_once running until its existing
    controller produces a sealed boundary. Draining never interrupts a turn,
    creates another turn, clears a goal, changes model/harness or sends SIGTERM.
    """
    session = session or RunnerSession()
    session.start()
    try:
        while not session.wait_active():
            if session.state in ('draining', 'retained') and not session.busy:
                session.call('quiescent')
            if session.state == 'stopped' or session.stop.is_set():
                return 0
        worker = factory()
        gate = ClaimGate(worker.client, session)
        worker.client = gate
        while not worker.stop.is_set():
            if gate.pending_checkpoint is not None:
                try:
                    gate.flush_checkpoint()
                except (OSError, ValueError):
                    worker.stop.wait(0.2)
                    continue
            if session.connected and session.state in ('draining', 'retained') and not session.busy:
                session.call('quiescent')
            if session.connected and session.state == 'stopped':
                return 0
            if not session.connected or session.state != 'active' or gate.unknown or session.busy:
                worker.stop.wait(0.2)
                continue
            try:
                worker.run_once()
            except (OSError, ValueError, urllib.error.URLError):
                # Match Worker.run's retry policy for control errors. The
                # managed claim guard above prevents any uncertain task retry.
                pass
            try:
                gate.finish_boundary(worker)
            except (OSError, ValueError):
                pass  # Retry this safe control checkpoint after reconnect.
            worker.stop.wait(2)
        return 0
    finally:
        session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)

    def factory():
        # Standby does not even import Worker/Codex or open worker/account
        # configuration. Existing owner launchers should pass their own factory.
        from .config import private_json
        from .worker import Worker
        return Worker(private_json(args.config), config_path=args.config)
    return run_worker(factory)


if __name__ == '__main__':
    raise SystemExit(main())
