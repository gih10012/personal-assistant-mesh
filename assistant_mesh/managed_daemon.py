"""Launch or attach one independently supervised, role- and account-bound Codex."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from .config import private_json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def private_directory(path):
    entry = os.lstat(path)
    if (not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.geteuid()
            or stat.S_IMODE(entry.st_mode) != 0o700 or os.path.realpath(path) != path):
        raise ValueError('codex_managed_private_directory_required')


def locked_file(path):
    fd = os.open(str(path), os.O_RDWR | os.O_NOFOLLOW | os.O_CREAT | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('codex_managed_lock_unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('codex_managed_owner_busy') from None
        return fd
    except BaseException:
        os.close(fd)
        raise


def process_start(pid):
    # Use boot ID and kernel start ticks to distinguish a recycled service PID.
    if type(pid) is not int or pid <= 0:
        raise ValueError('codex_managed_owner_invalid')
    fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    return [Path('/proc/sys/kernel/random/boot_id').read_text().strip(), fields[19]]


class ManagedBinding:
    def __init__(self, config, home, command):
        role = config.get('native_daemon_role')
        root = config.get('native_daemon_root')
        state_root = config.get('native_daemon_state_root', root)
        if (not isinstance(role, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', role)
                or not isinstance(root, str) or not os.path.isabs(root)
                or not isinstance(state_root, str) or not os.path.isabs(state_root)
                or config.get('strict_auth_home') is not True
                or config.get('model_provider', 'openai') != 'openai'
                or config.get('cost_policy', 'existing_subscription') != 'existing_subscription'
                or config.get('native_goal_drain') or config.get('native_goal_yield')):
            raise ValueError('codex_managed_role_profile_required')
        private_directory(root)
        private_directory(state_root)
        home = os.path.realpath(home)
        auth = private_json(Path(home) / 'auth.json')
        principal = (auth.get('tokens') or {}).get('account_id')
        if auth.get('auth_mode') != 'chatgpt' or not isinstance(principal, str) or not principal:
            raise ValueError('codex_managed_subscription_account_required')
        self.profile = home
        self.principal = principal
        self.role = role
        self.binding = digest([role, home, principal])
        self.directory = os.path.join(root, self.binding[:32])
        self.state_root = Path(state_root)
        self.path = Path(self.directory)
        self.owner_path = self.path / 'owner.json'
        self.lock_fd = self.thread = None
        # Inherited worker and systemd environments can differ. The daemon alone
        # owns its inherited environment. Compare explicit deployment settings.
        from .ai_egress import model_network_env
        network = model_network_env(config, {})

        self.launch_digest = digest([command, network])

    def owner(self):
        private_directory(self.directory)
        owner = private_json(self.owner_path)
        if (owner.get('binding') != self.binding or owner.get('launch') != self.launch_digest
                or not isinstance(owner.get('instance'), str)
                or owner.get('start') != process_start(owner.get('pid'))):
            raise ValueError('codex_managed_binding_changed')
        return owner

    def serve(self, command, env):
        self.path.mkdir(mode=0o700, exist_ok=True)
        private_directory(self.directory)
        # One writer to an original Codex home even if another role is
        # accidentally configured with the same profile.
        fd = locked_file(self.state_root / ('profile-' + digest(self.profile)))
        os.set_inheritable(fd, True)
        try:
            pid = os.getpid()
            owner = dict(binding=self.binding, launch=self.launch_digest, instance=uuid.uuid4().hex,
                         pid=pid, start=process_start(pid))
            with tempfile.NamedTemporaryFile(mode='w', dir=self.directory, delete=False) as stream:
                json.dump(owner, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(stream.name, str(self.owner_path))
            for key in ('OPENAI_API_KEY', 'CODEX_API_KEY'):
                env.pop(key, None)
            os.umask(0o077)
            os.execvpe(command[0], command + ['app-server', '--listen', 'unix://' + str(self.path / 'app.sock')], env)
        finally:
            os.close(fd)

    def receipt(self, instance):
        return dict(binding=self.binding, role=self.role, instance=instance)

    def admit(self, checkpoint):
        from .model_recovery import startable
        if not startable({'checkpoint': checkpoint}):
            raise ValueError('codex_managed_original_operation_unsettled')
        previous = (checkpoint.get('native_lifecycle') or {}).get('managed_daemon')
        if previous and (previous.get('binding') != self.binding or previous.get('role') != self.role):
            raise ValueError('codex_managed_original_binding_mismatch')

    def claim(self, thread):
        if not isinstance(thread, str) or not thread:
            raise ValueError('codex_managed_original_thread_required')
        if self.thread == thread:
            return
        if self.lock_fd is not None:
            raise ValueError('codex_managed_original_thread_mismatch')
        # Shared across role directories. The first admission binds a legacy
        # thread, subsequent workers require its original owner and exclusive lease.
        fd = locked_file(self.state_root / ('thread-' + digest(thread)))
        try:
            owner = os.read(fd, 128)
            if not owner:
                os.write(fd, self.binding.encode())
                os.fsync(fd)
            elif owner != self.binding.encode():
                raise ValueError('codex_managed_original_binding_mismatch')
            self.thread, self.lock_fd = thread, fd
        except BaseException:
            os.close(fd)
            raise

    def release(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = self.thread = None


def main():
    from .codex import Codex, native_launch
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='owner-private worker JSON')
    parser.add_argument('--role', choices=('leader', 'entrance', 'specialist'), help='role of an auto worker')
    parser.add_argument('--account-index', type=int, help='exact entry from codex_accounts')
    parser.add_argument('action', choices=('serve', 'probe', 'inspect'))
    parser.add_argument('--thread', help='original thread ID for read-only inspect')
    args = parser.parse_args()
    config = private_json(args.config)
    native = dict(config['codex'])
    accounts = config.get('codex_accounts')
    if accounts:
        if args.account_index is None or not 0 <= args.account_index < len(accounts):
            parser.error('select the original account with --account-index')
        native.update(accounts[args.account_index], strict_auth_home=True)
    elif args.account_index is not None:
        parser.error('no account pool is configured')
    if native.get('native_daemon_role') == 'auto':
        if not args.role:
            parser.error('--role is required for auto workers')
        native['native_daemon_role'] = args.role
    elif args.role and native.get('native_daemon_role') != args.role:
        parser.error('role conflicts with pinned worker')
    if native.get('native_transport') != 'managed-unix':
        parser.error('managed-unix transport is required')
    if args.action == 'serve':
        _, home, env, command = native_launch(native)
        ManagedBinding(native, home, command).serve(command, env)
    else:
        with Codex(native) as agent:
            account = agent.account()
            if not account.get('authenticated'):
                raise ValueError('codex_auth_required')
            result = dict(instance=agent.managed_instance, role=agent.managed.role,
                          account_verified=account.get('type') == 'chatgpt')
            if args.action == 'inspect':
                if not args.thread:
                    parser.error('--thread is required')
                # Do not acquire or create any ownership record during inspection.
                if (agent.managed.state_root / ('thread-' + digest(args.thread))).read_bytes() != agent.managed.binding.encode():
                    raise ValueError('codex_managed_original_binding_mismatch')
                thread = agent.rpc('thread/read', dict(threadId=args.thread, includeTurns=False))['thread']
                if thread.get('id') != args.thread:
                    raise ValueError('codex_managed_original_thread_mismatch')
                result.update(thread_id=args.thread, status=thread['status'])
            print(json.dumps(result))


if __name__ == '__main__':
    main()
