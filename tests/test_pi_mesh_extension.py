"""Actual Pi extension TS + actual authenticated HTTP; Pi SDK registration shim.

No Pi model, desktop, deployed service, existing account or paid API is touched.
Native-route tests simulate Pi's tool-call event pipeline, not model execution.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from assistant_mesh.server import serve
from assistant_mesh.worker import Client


class PiMeshExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('Node is required to execute the actual Pi extension')
        probe = subprocess.run([cls.node, '-p', "typeof require('node:module').stripTypeScriptTypes"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        if probe.returncode or probe.stdout.strip() != 'function':
            raise unittest.SkipTest('Node TypeScript stripping is required for the extension contract test')
        cls.repo = Path(__file__).resolve().parent.parent
        cls.runner = cls.repo / 'tests' / 'pi_mesh_contract.mjs'
        cls.extension = cls.repo / 'extensions' / 'pi-mesh.ts'

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.server = None
        ready = threading.Event()
        self.tokens = {}
        peers = []
        caps = ['agent', 'leader', 'mesh.node:local', 'a2a.send']
        for role in ('operator', 'worker', 'viewer'):
            self.tokens[role] = role + '-' + 'p' * 64
            path = self.root / (role + '.token')
            path.write_text(self.tokens[role])
            path.chmod(0o600)
            peer = {'role': role, 'token_file': str(path)}
            if role == 'worker':
                peer.update(node='local', capabilities=caps)
            peers.append(peer)

        def started(server, api, channel):
            self.server, self.api = server, api
            ready.set()

        config = {'database': str(self.root / 'ledger.sqlite'), 'port': 0, 'node_id': 'local',
                  'peers': peers, 'network_peers': {'authorized-peer': {'send_allowed': True},
                                                   'denied-peer': {'send_allowed': False}}}
        self.thread = threading.Thread(target=serve, args=(config, started), daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(3))
        self.url = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.operator = Client({'control_url': self.url, 'token_file': str(self.root / 'operator.token')})
        worker = Client({'control_url': self.url, 'token_file': str(self.root / 'worker.token')})
        self.parent_id = self.operator.request('/v1/tasks', {'input': 'Pi contract test',
            'required': ['agent', 'mesh.node:local']})['id']
        worker.request('/v1/heartbeat', {'capabilities': caps})
        task = worker.request('/v1/claim', {})['task']
        self.epoch = task['epoch']
        self.grant = {'control_url': self.url, 'token_file': str(self.root / 'worker.token'),
                      'task_id': self.parent_id, 'epoch': self.epoch}
        self.grant_path = self.root / 'pi-grant.json'
        self.write_grant(self.grant)

    def tearDown(self):
        if self.server:
            self.server.shutdown()
        self.thread.join(timeout=3)
        self.directory.cleanup()

    def write_grant(self, grant):
        self.grant_path.write_text(json.dumps(grant))
        self.grant_path.chmod(0o600)

    def run_extension(self, calls=(), grant=None):
        if grant is not None:
            self.write_grant(grant)
        env = os.environ.copy()
        env['MESH_PI_GRANT_FILE'] = str(self.grant_path)
        invocation = subprocess.run([self.node, str(self.runner)],
            input=json.dumps({'extension': str(self.extension), 'calls': list(calls)}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=env, timeout=30)
        self.assertEqual(0, invocation.returncode, invocation.stderr[:1000])
        for token in self.tokens.values():
            self.assertNotIn(token, invocation.stdout)
        return json.loads(invocation.stdout)

    @staticmethod
    def mesh(action, arguments=None, call_id='unified-call'):
        return {'tool': 'mesh', 'callId': call_id,
                'arguments': {'action': action, 'arguments': arguments or {}}}

    @staticmethod
    def data(output, index=0):
        result = output['results'][index]['result']
        parsed = json.loads(result['content'][0]['text'])
        if parsed != result['details']:
            raise AssertionError('Pi content/details contracts disagree')
        return parsed

    def test_registration_is_additional_and_does_not_install_native_tool_hook(self):
        output = self.run_extension()
        self.assertIsNone(output['initializationError'])
        self.assertEqual([], output['hooks'])
        self.assertEqual([], output['requests'])
        names = {tool['name'] for tool in output['tools']}
        self.assertTrue({'mesh', 'mesh_remember', 'mesh_recall', 'mesh_delegate', 'mesh_children',
                         'mesh_wait_children', 'mesh_notify', 'mesh_remote_delegate', 'mesh_resource'} <= names)
        self.assertFalse(names & set(output['nativeToolNames']))
        unified = next(tool for tool in output['tools'] if tool['name'] == 'mesh')
        self.assertNotIn('enum', unified['parameters']['properties']['action'])
        self.assertTrue(unified['parameters']['properties']['arguments']['additionalProperties'])
        self.assertIn('not a native tool gate', unified['description'])

    def test_actual_ts_discover_describe_graph_audit_and_open_kind_advertise(self):
        calls = [self.mesh('advertise', {'id': 'pi-created-tool', 'kind': 'future.unenumerated.kind',
                    'spec': {'token': 'private-source-never-display'}}, 'advertise'),
                 self.mesh('discover'), self.mesh('describe', {'id': 'pi-created-tool'}),
                 self.mesh('graph'), self.mesh('audit')]
        output = self.run_extension(calls)
        self.assertEqual('node:local', self.data(output)['principal'])
        self.assertEqual('pi-created-tool', self.data(output, 1)['capabilities'][0]['id'])
        self.assertEqual('declared', self.data(output, 2)['verification'])
        self.assertNotIn('private-source-never-display', json.dumps(output))
        self.assertFalse(self.data(output, 3)['invocation_authorized'])
        self.assertEqual('advertise', self.data(output, 4)['events'][0]['operation'])
        self.assertEqual(5, sum(row['path'] == '/v1/task/update' for row in output['requests']))

    def test_resource_wrapper_and_legacy_resource_are_compatible(self):
        output = self.run_extension([
            self.mesh('resource', {'action': 'advertise', 'arguments': {'id': 'wrapper', 'kind': 'new.kind'}}),
            {'tool': 'mesh_resource', 'callId': 'legacy-resource', 'arguments': {
                'action': 'describe', 'arguments': {'id': 'wrapper'}}}])
        self.assertEqual('node:local', self.data(output)['principal'])
        self.assertEqual('wrapper', self.data(output, 1)['id'])

    def test_actor_forgery_and_self_grant_are_structured_permission_refusals(self):
        output = self.run_extension([
            self.mesh('advertise', {'id': 'forged', 'kind': 'new.kind', 'actor': 'operator'}),
            self.mesh('resource', {'action': 'grant', 'arguments': {'request_id': 'forged-grant'}})])
        for index in (0, 1):
            self.assertEqual(403, self.data(output, index)['http_status'])
            self.assertFalse(self.data(output, index)['success'])
            self.assertTrue(output['results'][index]['result']['isError'])
        self.assertEqual([], self.api.resources.discover()['capabilities'])

    def test_local_delegate_children_wait_and_legacy_memory_use_original_parent(self):
        output = self.run_extension([
            self.mesh('delegate', {'input': 'local child', 'agent_id': 'specialist'}, 'local-child'),
            self.mesh('children', call_id='children'), self.mesh('wait_children', call_id='wait'),
            {'tool': 'mesh_remember', 'callId': 'legacy-memory', 'arguments': {'text': 'verified Pi fixture fact'}},
            self.mesh('recall', {'query': 'verified'}, 'recall')])
        child = self.data(output)
        self.assertEqual(self.parent_id, child['parent_id'])
        self.assertEqual(child['id'], self.data(output, 1)['tasks'][0]['id'])
        self.assertTrue(self.data(output, 2)['continue_after_children'])
        self.assertEqual('verified Pi fixture fact', self.data(output, 4)['memories'][0]['text'])
        with self.api.store.transaction() as db:
            row = db.execute('SELECT required FROM tasks WHERE id=?', (child['id'],)).fetchone()
        self.assertIn('mesh.node:local', json.loads(row['required']))

    def test_remote_delegate_unified_and_legacy_share_same_idempotent_contract(self):
        args = {'peer': 'authorized-peer', 'input': 'remote child', 'agent_id': 'specialist'}
        output = self.run_extension([self.mesh('remote_delegate', args, 'stable-remote'),
            {'tool': 'mesh_remote_delegate', 'callId': 'stable-remote', 'arguments': args}])
        self.assertEqual(self.data(output), self.data(output, 1))
        self.assertTrue(self.data(output)['queued'])
        self.assertFalse(self.data(output)['remote_completion_verified'])
        self.assertEqual('waiting_remote', self.data(output)['status'])
        with self.api.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM mesh_deliveries').fetchone()[0])

    def test_remote_permission_denial_does_not_queue_or_claim_native_block(self):
        output = self.run_extension([self.mesh('remote_delegate', {'peer': 'denied-peer', 'input': 'not sent'}),
                                     {'tool': 'network', 'callId': 'native-network'}])
        self.assertEqual(403, self.data(output)['http_status'])
        self.assertTrue(output['results'][1]['completed'])
        self.assertTrue(output['results'][1]['native'])
        with self.api.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM mesh_deliveries').fetchone()[0])

    def test_resource409_is_structured_and_capability_authorization_is_not_execution(self):
        output = self.run_extension([
            self.mesh('advertise', {'id': 'check-only', 'kind': 'new.kind'}),
            self.mesh('renew', {'id': 'check-only', 'epoch': 999}),
            self.mesh('authorize', {'id': 'check-only', 'action': 'future-action', 'scope': {'example': 'exact'}}),
            self.mesh('invoke', {'id': 'check-only'}), self.mesh('children', call_id='still-live')])
        self.assertEqual(409, self.data(output, 1)['http_status'])
        self.assertTrue(self.data(output, 2)['allowed'])
        self.assertNotIn('completed', self.data(output, 2))
        self.assertEqual(400, self.data(output, 3)['http_status'])
        self.assertEqual([], self.data(output, 4)['tasks'])

    def test_remote409_is_thrown_and_not_downgraded_to_successful_tool_result(self):
        output = self.run_extension([
            self.mesh('remote_delegate', {'peer': 'authorized-peer', 'input': 'first'}, 'same-remote'),
            self.mesh('remote_delegate', {'peer': 'authorized-peer', 'input': 'changed'}, 'same-remote')])
        self.assertTrue(self.data(output)['queued'])
        self.assertTrue(output['results'][1]['thrown'])
        self.assertEqual(409, output['results'][1]['status'])

    def test_stale_mesh_fence_throws_but_native_tools_are_not_intercepted(self):
        self.operator.request('/v1/task/control', {'id': self.parent_id, 'command': 'pause'})
        output = self.run_extension([self.mesh('discover'), {'tool': 'bash', 'callId': 'native-bash'},
                                     {'tool': 'read', 'callId': 'native-read'}])
        self.assertTrue(output['results'][0]['thrown'])
        self.assertEqual(409, output['results'][0]['status'])
        self.assertEqual(['/v1/task/update'], [row['path'] for row in output['requests']])
        self.assertTrue(all(row['native'] and row['completed'] for row in output['results'][1:]))
        self.assertEqual([], output['hooks'])

    def test_native_event_pipeline_never_contacts_unavailable_mesh(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as selected:
            selected.bind(('127.0.0.1', 0))
            unavailable_port = selected.getsockname()[1]
        grant = dict(self.grant, control_url='http://127.0.0.1:' + str(unavailable_port))
        output = self.run_extension([{'tool': name, 'callId': 'native-' + name}
            for name in ('bash', 'read', 'write', 'network', 'mcp_native')], grant=grant)
        self.assertEqual([], output['requests'])
        self.assertEqual([], output['hooks'])
        self.assertTrue(all(row['native'] and row['completed'] for row in output['results']))

    def test_native_event_pipeline_never_contacts_mesh_with_http403_grant(self):
        grant = dict(self.grant, token_file=str(self.root / 'viewer.token'))
        output = self.run_extension([self.mesh('discover'), {'tool': 'bash', 'callId': 'native-bash'},
                                     {'tool': 'mcp_native', 'callId': 'native-mcp'}], grant=grant)
        self.assertEqual(403, output['results'][0]['status'])
        self.assertEqual(1, len(output['requests']))
        self.assertEqual([], output['hooks'])
        self.assertTrue(all(row['native'] and row['completed'] for row in output['results'][1:]))

    def test_invalid_mesh_envelope_does_not_touch_authority(self):
        calls = [{'tool': 'mesh', 'callId': 'invalid', 'arguments': arguments}
            for arguments in ({}, {'action': 7}, {'action': ''}, {'action': 'discover', 'arguments': []},
                              {'action': 'discover', 'actor': 'operator'})]
        output = self.run_extension(calls)
        self.assertEqual([], output['requests'])
        self.assertTrue(all(row['result']['isError'] for row in output['results']))

    def test_private_grant_and_control_url_checks_fail_before_mesh_requests(self):
        self.grant_path.chmod(0o644)
        output = self.run_extension()
        self.assertEqual('private_mesh_grant_required', output['initializationError'])
        for url in ('http://example.invalid', 'http://user:password@127.0.0.1',
                    self.url + '?unexpected=query', self.url + '#fragment'):
            output = self.run_extension(grant=dict(self.grant, control_url=url))
            self.assertIsNotNone(output['initializationError'])
            self.assertEqual([], output['requests'])


if __name__ == '__main__':
    unittest.main()
