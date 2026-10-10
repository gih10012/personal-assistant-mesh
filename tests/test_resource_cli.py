import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import assistant_mesh.worker as worker_module
from assistant_mesh.cli import main, resource_payload
from assistant_mesh.codex import Codex
from assistant_mesh.model_tools import TOOLS
from assistant_mesh.server import serve
from assistant_mesh.worker import Client, Worker


class ResourceCLITests(unittest.TestCase):
    def setUp(self):
        self.original_umask = os.umask(0o077)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = None
        self.ready = threading.Event()
        peers = []
        for role in ('worker', 'operator', 'viewer'):
            token = self.root / (role + '.token')
            token.write_text(role + '-' + 'a' * 64)
            token.chmod(0o600)
            peer = {'role': role, 'token_file': str(token)}
            if role == 'worker':
                peer.update(node='a', capabilities=['leader', 'agent'])
            peers.append(peer)

        def ready(server, api, channel):
            self.server, self.api = server, api
            self.ready.set()

        self.thread = threading.Thread(target=serve, args=({'database': str(self.root / 'db'), 'peers': peers, 'port': 0}, ready))
        self.thread.daemon = True
        self.thread.start()
        self.assertTrue(self.ready.wait(3))
        url = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.configs = {}
        for role in ('worker', 'operator', 'viewer'):
            config = {'control_url': url, 'token_file': str(self.root / (role + '.token')),
                      'node_id': 'a', 'capabilities': ['leader', 'agent'], 'codex': {'workspace': str(self.root)}}
            path = self.write_private(role + '.json', config)
            self.configs[role] = (path, config)

    def tearDown(self):
        if self.server:
            self.server.shutdown()
        self.thread.join(timeout=3)
        self.temp.cleanup()
        os.umask(self.original_umask)

    def write_private(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def cli(self, role, *args):
        stream = io.StringIO()
        argv = ['assistant-mesh', '--config', str(self.configs[role][0])] + list(args)
        with mock.patch('sys.argv', argv), contextlib.redirect_stdout(stream):
            main()
        return json.loads(stream.getvalue())

    def test_private_file_advertise_and_discover_use_real_authenticated_http(self):
        payload = self.write_private('advertise.json', {'id': 'created-tool', 'kind': 'model.tool.unenumerated',
                                                       'spec': {'version': 'test-1', 'token': 'do-not-display'}})
        created = self.cli('worker', 'resource', '--action', 'advertise', '--payload-file', str(payload))
        self.assertEqual('node:a', created['principal'])
        discovered = self.cli('viewer', 'resources', '--kind', 'model.tool.unenumerated')
        self.assertEqual('created-tool', discovered['capabilities'][0]['id'])
        self.assertNotIn('do-not-display', json.dumps(discovered))
        graph = self.cli('viewer', 'resource-graph')
        self.assertFalse(graph['invocation_authorized'])
        events = self.cli('viewer', 'capability-events', '--after', '0', '--limit', '1')
        self.assertEqual('advertise', events['events'][0]['operation'])
        self.assertEqual('after', events['page']['mode'])

    def test_complete_payload_and_action_conflict_are_explicit(self):
        payload = self.write_private('full.json', {'action': 'advertise', 'arguments': {'id': 'full', 'kind': 'tool.new'}})
        self.assertEqual('full', self.cli('worker', 'resource', '--payload-file', str(payload))['id'])
        with self.assertRaises(ValueError):
            resource_payload(payload, 'revoke')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli('worker', 'resource', '--action', 'describe', '--principal', 'operator')

    def test_unsafe_symlink_and_git_worktree_payload_files_are_rejected(self):
        payload = self.write_private('args.json', {'id': 'new', 'kind': 'new.tool'})
        payload.chmod(0o644)
        with self.assertRaises(ValueError):
            resource_payload(payload, 'advertise')
        payload.chmod(0o600)
        symlink = self.root / 'link.json'
        symlink.symlink_to(payload)
        with self.assertRaises(ValueError):
            resource_payload(symlink, 'advertise')
        worktree = self.root / 'public'
        worktree.mkdir()
        (worktree / '.git').mkdir()
        inside = worktree / 'payload.json'
        inside.write_text('{}')
        inside.chmod(0o600)
        with self.assertRaises(ValueError):
            resource_payload(inside, 'advertise')
        self.assertEqual([], self.cli('viewer', 'resources')['capabilities'])

    def test_http_privilege_and_caller_forgery_rejected_without_mutation(self):
        payload = self.write_private('forge.json', {'id': 'forged', 'kind': 'future.tool', 'actor': 'operator'})
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.cli('worker', 'resource', '--action', 'advertise', '--payload-file', str(payload))
        self.assertEqual(403, failure.exception.code)
        empty = self.cli('viewer', 'resources')
        self.assertEqual([], empty['capabilities'])
        allowed = self.write_private('valid.json', {'id': 'valid', 'kind': 'future.tool'})
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.cli('viewer', 'resource', '--action', 'advertise', '--payload-file', str(allowed))
        self.assertEqual(403, failure.exception.code)
        client = Client(self.configs['worker'][1])
        client.token = 'invalid-but-not-logged'
        with self.assertRaises(urllib.error.HTTPError) as failure:
            client.request('/v1/resources')
        self.assertEqual(401, failure.exception.code)

    def test_worker_generic_tool_is_lease_gated_and_uses_peer_not_model_actor(self):
        operator = Client(self.configs['operator'][1])
        operator.request('/v1/tasks', {'input': 'create a reusable tool'})
        worker = Worker(self.configs['worker'][1], config_path=self.configs['worker'][0])
        worker.heartbeat()
        worker.current = worker.client.request('/v1/claim', {})['task']
        response = worker.on_tool({'tool': 'mesh_resource', 'callId': 'one',
                                  'arguments': {'action': 'advertise', 'arguments': {'id': 'native-tool', 'kind': 'created.kind'}}})
        self.assertTrue(response['success'])
        self.assertEqual('node:a', json.loads(response['contentItems'][0]['text'])['principal'])
        rejected = worker.on_tool({'tool': 'mesh_resource', 'callId': 'two',
                                  'arguments': {'action': 'grant', 'arguments': {'request_id': 'unauthorized'}}})
        self.assertFalse(rejected['success'])
        self.assertEqual(403, json.loads(rejected['contentItems'][0]['text'])['http_status'])
        operator.request('/v1/task/control', {'id': worker.current['id'], 'command': 'pause'})
        with self.assertRaises(urllib.error.HTTPError) as failure:
            worker.on_tool({'tool': 'mesh_resource', 'callId': 'stale',
                            'arguments': {'action': 'advertise', 'arguments': {'id': 'stale', 'kind': 'created.kind'}}})
        self.assertEqual(409, failure.exception.code)
        self.assertEqual(['native-tool'], [cap['id'] for cap in self.cli('viewer', 'resources')['capabilities']])

    def test_existing_native_thread_gets_private_cli_handle_not_rebuilt_memory(self):
        worker = Worker(self.configs['worker'][1], config_path=self.configs['worker'][0])
        reference = worker.resource_reference()
        self.assertEqual(str(self.configs['worker'][0]), reference['cli']['argv'][-2])
        self.assertNotIn('a' * 64, json.dumps(reference))
        self.assertEqual(Path(worker_module.__file__).resolve().parent.parent,
                         Path(reference['cli']['cwd']).resolve())
        agent = Codex.__new__(Codex)
        agent.config = {'workspace': str(self.root)}
        agent.tools, agent.on_activity = TOOLS, None
        agent.deferred = []
        calls = []

        def rpc(method, parameters, **kwargs):
            calls.append((method, parameters))
            if method in ('thread/start', 'thread/resume'):
                return {'thread': {'id': 'continuous-thread'}}
            if method == 'turn/start':
                return {'turn': {'id': 'turn'}}
            if method == 'thread/goal/get':
                return {'goal': None}
            return {}

        agent.rpc = rpc
        agent.start('continue', {'thread_id': 'continuous-thread'})
        self.assertEqual('thread/resume', calls[0][0])
        self.assertNotIn('dynamicTools', calls[0][1])
        self.assertFalse(any(method == 'thread/start' for method, _ in calls))
        calls[:] = []
        agent.start('new child')
        self.assertIn('mesh_resource', [tool['name'] for tool in calls[0][1]['dynamicTools']])


if __name__ == '__main__':
    unittest.main()
