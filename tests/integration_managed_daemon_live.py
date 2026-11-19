"""Opt-in smoke using the installed app-server, a private empty home and no network."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from assistant_mesh.codex import Codex, native_launch
from assistant_mesh.managed_daemon import ManagedBinding


@unittest.skipUnless(shutil.which("codex") and shutil.which("bwrap"), "Codex and bwrap required")
class ManagedDaemonLiveTests(unittest.TestCase):
    def test_disconnect_and_reconnect_keeps_the_server(self):
        with tempfile.TemporaryDirectory(prefix='amita-rpc-') as directory:
            root = Path(directory)
            profile = root / 'fixture'
            profile.mkdir(mode=0o700)
            auth = profile / 'auth.json'
            # Only an account label for the transport binding check. No usable token.
            auth.write_text(json.dumps({'auth_mode':'chatgpt', 'tokens':{'account_id':'fixture-only'}}))
            auth.chmod(0o600)
            config = {'codex': {'executable': shutil.which("codex"),
                     'auth_home': str(profile), 'strict_auth_home': True,
                     'native_transport': 'managed-unix', 'native_daemon_role':'entrance',
                     'native_daemon_root': str(root)}}
            path = root/'worker.json'
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            _, home, _, command = native_launch(config['codex'])
            binding = ManagedBinding(config['codex'], home, command)
            unshare = shutil.which('bwrap')
            self.assertIsNotNone(unshare)
            with (root/'server.log').open('w+') as log:
                server = subprocess.Popen([unshare, '--unshare-net', '--die-with-parent',
                    '--bind','/','/','--', os.sys.executable, '-m', 'assistant_mesh.managed_daemon',
                    '--config', str(path), 'serve'], stdout=log, stderr=log)
                try:
                    socket_file = binding.path/'app.sock'
                    deadline = time.monotonic()+10
                    while not socket_file.exists() and server.poll() is None and time.monotonic()<deadline:
                        time.sleep(.05)
                    log.seek(0)
                    self.assertTrue(socket_file.exists(), log.read()[-1200:])
                    self.assertTrue(stat.S_ISSOCK(socket_file.stat().st_mode))
                    with Codex(config['codex']) as first:
                        token = first.managed_instance
                    self.assertIsNone(server.poll())
                    with Codex(config['codex']) as again:
                        self.assertEqual(token, again.managed_instance)
                    self.assertIsNone(server.poll())
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
