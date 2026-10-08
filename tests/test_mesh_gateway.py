"""Unified extra mesh tool; real local HTTP, no native execution or paid API."""
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.codex import Codex
from assistant_mesh.model_tools import TOOLS
from assistant_mesh.server import serve
from assistant_mesh.worker import Client, Worker


class MeshGatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.server = None
        ready = threading.Event()
        self.tokens = {}
        peers = []
        self.capabilities = ['agent', 'leader', 'mesh.node:local', 'a2a.send']
        for role in ('operator', 'worker'):
            token = self.root / (role + '.token')
            self.tokens[role] = role + '-' + 'g' * 64
            token.write_text(self.tokens[role])
            token.chmod(0o600)
            peer = {'role': role, 'token_file': str(token)}
            if role == 'worker':
                peer.update(node='local', capabilities=self.capabilities)
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
        url = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.operator = Client({'control_url': url, 'token_file': str(self.root / 'operator.token')})
        self.worker_config = {'control_url': url, 'token_file': str(self.root / 'worker.token'),
            'node_id': 'local', 'capabilities': self.capabilities,
            'codex': {'workspace': str(self.root), 'sandbox': 'danger-full-access', 'approval_policy': 'never'}}
        path = self.root / 'worker.json'
        path.write_text(json.dumps(self.worker_config))
        path.chmod(0o600)
        self.worker = Worker(self.worker_config, config_path=path)
        self.parent_id = self.operator.request('/v1/tasks', {'input': 'gateway test only',
            'required': ['agent', 'mesh.node:local']})['id']
        self.worker.heartbeat()
        self.worker.current = self.worker.client.request('/v1/claim', {})['task']
        self.assertEqual(self.parent_id, self.worker.current['id'])

    def tearDown(self):
        if self.server:
            self.server.shutdown()
        self.thread.join(timeout=3)
        self.directory.cleanup()

    @staticmethod
    def value(result):
        return json.loads(result['contentItems'][0]['text'])

    def gateway(self, action, arguments=None, call_id='gateway-call'):
        return self.worker.on_tool({'tool': 'mesh', 'callId': call_id,
            'arguments': {'action': action, 'arguments': arguments or {}}})

    def advertise(self, identity='tool-created-by-model'):
        response = self.gateway('advertise', {'id': identity, 'kind': 'future.unenumerated.kind',
            'spec': {'adapter': 'not-installed', 'version': 'test', 'token': 'private-metadata-never-print'}},
            call_id='advertise-' + identity)
        self.assertTrue(response['success'])
        return self.value(response)

    def count(self, table):
        with self.api.store.transaction() as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_mesh_metadata_is_extra_gateway_with_open_arguments_and_legacy_tools(self):
        names = {tool['name'] for tool in TOOLS}
        self.assertTrue({'mesh', 'mesh_resource', 'mesh_remote_delegate', 'mesh_delegate',
                         'mesh_children', 'mesh_wait_children', 'mesh_remember', 'mesh_recall', 'mesh_notify'} <= names)
        gateway = next(tool for tool in TOOLS if tool['name'] == 'mesh')
        self.assertNotIn('enum', gateway['inputSchema']['properties']['action'])
        self.assertTrue(gateway['inputSchema']['properties']['arguments']['additionalProperties'])
        self.assertIn('不是本机原生工具门禁', gateway['description'])
        self.assertIn('不拦截 Shell', gateway['description'])
        self.assertIn('没有执行适配器', gateway['description'])

    def test_open_kind_advertise_and_authenticated_discover_describe_graph_audit(self):
        created = self.advertise()
        self.assertEqual('node:local', created['principal'])
        discovered = self.value(self.gateway('discover', {'kind': 'future.unenumerated.kind'}))
        self.assertEqual([created['id']], [cap['id'] for cap in discovered['capabilities']])
        described = self.value(self.gateway('describe', {'id': created['id']}))
        self.assertEqual('declared', described['verification'])
        self.assertNotIn('private-metadata-never-print', json.dumps(described))
        self.assertFalse(self.value(self.gateway('graph'))['invocation_authorized'])
        self.assertEqual('advertise', self.value(self.gateway('audit'))['events'][0]['operation'])

    def test_resource_wrapper_routes_existing_api_without_assuming_new_execution(self):
        response = self.gateway('resource', {'action': 'advertise', 'arguments': {
            'id': 'wrapper-created', 'kind': 'tool.completely.new.kind'}})
        self.assertTrue(response['success'])
        self.assertEqual('node:local', self.value(response)['principal'])
        self.assertNotIn('completed', self.value(response))

    def test_resource_cannot_forge_actor_or_approve_own_grant(self):
        for identity, arguments in (('forged-actor', {'actor': 'operator'}),
                                    ('forged-principal', {'principal': 'operator'})):
            response = self.gateway('advertise', dict(arguments, id=identity, kind='open.kind'), call_id=identity)
            self.assertFalse(response['success'])
            self.assertEqual(403, self.value(response)['http_status'])
        response = self.gateway('resource', {'action': 'grant', 'arguments': {'request_id': 'not-owner-approved'}})
        self.assertFalse(response['success'])
        self.assertEqual(403, self.value(response)['http_status'])
        self.assertEqual(0, self.count('capabilities'))

    def test_permission_check_is_not_capability_execution(self):
        created = self.advertise()
        response = self.gateway('authorize', {'id': created['id'], 'action': 'arbitrary-new-action',
                                             'scope': {'file': 'owner-authorized-example'}})
        self.assertTrue(response['success'])
        authorization = self.value(response)
        self.assertTrue(authorization['allowed'])
        self.assertEqual('same_principal', authorization['reason'])
        self.assertNotIn('completed', authorization)
        unavailable = self.gateway('invoke', {'id': created['id'], 'input': 'must not execute anything'})
        self.assertFalse(unavailable['success'])
        self.assertEqual(400, self.value(unavailable)['http_status'])
        self.assertEqual(1, self.count('tasks'))

    def test_resource_epoch_conflict_is_structured_not_task_lease_loss(self):
        created = self.advertise()
        response = self.gateway('renew', {'id': created['id'], 'epoch': created['epoch'] + 1})
        self.assertFalse(response['success'])
        self.assertEqual(409, self.value(response)['http_status'])
        self.assertTrue(self.gateway('children')['success'])

    def test_local_delegate_children_and_wait_use_current_fenced_parent(self):
        created = self.value(self.gateway('delegate', {'input': 'local child', 'agent_id': 'same-project-worker',
            'project_id': 'project', 'required': ['agent']}, call_id='create-local'))
        self.assertEqual(self.parent_id, created['parent_id'])
        with self.api.store.transaction() as db:
            child = db.execute('SELECT required,context FROM tasks WHERE id=?', (created['id'],)).fetchone()
        self.assertIn('mesh.node:local', json.loads(child['required']))
        self.assertEqual('same-project-worker', json.loads(child['context'])['agent_id'])
        children = self.value(self.gateway('children'))
        self.assertEqual([created['id']], [task['id'] for task in children['tasks']])
        self.assertFalse(self.worker.wait_children)
        waiting = self.value(self.gateway('wait_children', call_id='wait-for-local'))
        self.assertTrue(waiting['continue_after_children'])
        self.assertTrue(self.worker.wait_children)

    def test_remote_delegate_is_authorized_queued_idempotent_not_complete(self):
        arguments = {'peer': 'authorized-peer', 'input': 'remote child', 'project_id': 'project', 'agent_id': 'worker'}
        first = self.value(self.gateway('remote_delegate', arguments, call_id='stable-remote'))
        second = self.value(self.gateway('remote_delegate', arguments, call_id='stable-remote'))
        self.assertEqual(first, second)
        self.assertTrue(first['queued'])
        self.assertFalse(first['remote_completion_verified'])
        self.assertEqual('waiting_remote', first['status'])
        self.assertEqual(self.parent_id, first['parent_id'])
        self.assertEqual(1, self.count('mesh_deliveries'))
        self.assertEqual(2, self.count('tasks'))

    def test_unauthorized_remote_peer_is_structured_refusal_without_queue(self):
        for peer in ('denied-peer', 'not-enrolled'):
            response = self.gateway('remote_delegate', {'peer': peer, 'input': 'not sent'}, call_id=peer)
            self.assertFalse(response['success'])
            self.assertEqual(403, self.value(response)['http_status'])
        self.assertEqual(0, self.count('mesh_deliveries'))

    def test_remote_identity_injection_is_rejected_by_current_contract(self):
        response = self.gateway('remote_delegate', {'peer': 'authorized-peer', 'input': 'not sent',
            'node': 'different-node', 'epoch': 999, 'task_id': 'different-parent'})
        self.assertFalse(response['success'])
        self.assertEqual(400, self.value(response)['http_status'])
        self.assertEqual(0, self.count('mesh_deliveries'))

    def test_remote_content_conflict_409_still_terminates_native_worker(self):
        self.gateway('remote_delegate', {'peer': 'authorized-peer', 'input': 'first'}, call_id='same-remote-call')
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.gateway('remote_delegate', {'peer': 'authorized-peer', 'input': 'changed'}, call_id='same-remote-call')
        self.assertEqual(409, failure.exception.code)
        self.assertEqual(1, self.count('mesh_deliveries'))

    def test_stale_task_lease_409_is_not_hidden_as_resource_rejection(self):
        self.operator.request('/v1/task/control', {'id': self.parent_id, 'command': 'pause'})
        for action in ('discover', 'delegate', 'remote_delegate', 'children', 'wait_children'):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.gateway(action)
            self.assertEqual(409, failure.exception.code)

    def test_local_coordination_permission_error_is_structured_and_wait_not_set(self):
        error = urllib.error.HTTPError('http://127.0.0.1/fixture', 403, 'forbidden', {}, None)
        error.close()  # match actual Client's closed HTTP-error contract
        with patch.object(self.worker, 'tick'), patch.object(self.worker.client, 'request', side_effect=error):
            response = self.gateway('wait_children')
        self.assertFalse(response['success'])
        self.assertEqual(403, self.value(response)['http_status'])
        self.assertFalse(self.worker.wait_children)

    def test_local_coordination_409_is_not_converted_to_successful_result(self):
        error = urllib.error.HTTPError('http://127.0.0.1/fixture', 409, 'stale', {}, None)
        error.close()
        with patch.object(self.worker, 'tick'), patch.object(self.worker.client, 'request', side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                self.gateway('children')

    def test_invalid_gateway_envelopes_return_errors_without_new_actions(self):
        for arguments in ({}, {'action': 7}, {'action': ''}, {'action': 'discover', 'arguments': []},
                          {'action': 'discover', 'actor': 'operator'}, 'not-object'):
            response = self.worker.on_tool({'tool': 'mesh', 'callId': 'invalid', 'arguments': arguments})
            self.assertFalse(response['success'])
            self.assertEqual('invalid_mesh_gateway_arguments', self.value(response)['error'])
        response = self.gateway('remote_delegate', {'input': 'no peer'})
        self.assertFalse(response['success'])
        self.assertEqual('remote_delegation_peer_required', self.value(response)['error'])
        self.assertEqual(0, self.count('agent_actions'))

    def test_legacy_tools_remain_compatible_with_the_unified_entry(self):
        remembered = self.worker.on_tool({'tool': 'mesh_remember', 'callId': 'legacy-memory',
                                         'arguments': {'text': 'verified fixture fact'}})
        self.assertTrue(remembered['success'])
        recalled = self.value(self.gateway('recall', {'query': 'verified'}))
        self.assertEqual('verified fixture fact', recalled['memories'][0]['text'])
        advertised = self.worker.on_tool({'tool': 'mesh_resource', 'callId': 'legacy-resource', 'arguments': {
            'action': 'advertise', 'arguments': {'id': 'legacy-created', 'kind': 'open.legacy.kind'}}})
        self.assertTrue(advertised['success'])
        self.assertEqual('legacy-created', self.value(self.gateway('describe', {'id': 'legacy-created'}))['id'])

    def test_reference_keeps_cli_handles_and_never_exposes_token_or_native_gate(self):
        before = json.dumps(self.worker.config, sort_keys=True)
        reference = self.worker.resource_reference()
        self.assertEqual('mesh(action, arguments)', reference['fresh_thread_tool'])
        self.assertFalse(reference['gateway']['native_tools_intercepted'])
        self.assertEqual('resources', reference['cli']['argv'][-1])
        self.assertEqual('mesh-delegate', reference['a2a']['resume_cli'][0])
        self.assertEqual('mesh_resource(action, arguments)', reference['legacy_tool'])
        for secret in self.tokens.values():
            self.assertNotIn(secret, json.dumps(reference))
        self.assertEqual(before, json.dumps(self.worker.config, sort_keys=True))
        self.assertEqual('danger-full-access', self.worker.config['codex']['sandbox'])

    def test_existing_thread_is_resumed_without_replacing_dynamic_or_native_tools(self):
        agent = Codex.__new__(Codex)
        agent.config = dict(self.worker_config['codex'])
        agent.tools, agent.on_activity = TOOLS, None
        calls = []
        def rpc(method, parameters, **kwargs):
            calls.append((method, parameters))
            if method in ('thread/start', 'thread/resume'):
                return {'thread': {'id': 'native-continuous-thread'}}
            if method == 'turn/start':
                return {'turn': {'id': 'fixture-turn'}}
            return {}
        agent.rpc = rpc
        agent.start('continue original native context', {'thread_id': 'native-continuous-thread'})
        self.assertEqual('thread/resume', calls[0][0])
        self.assertNotIn('dynamicTools', calls[0][1])
        self.assertEqual('danger-full-access', calls[0][1]['sandbox'])
        calls[:] = []
        agent.start('new native child')
        self.assertEqual('thread/start', calls[0][0])
        names = {tool['name'] for tool in calls[0][1]['dynamicTools']}
        self.assertTrue({'mesh', 'mesh_resource', 'mesh_delegate', 'mesh_remote_delegate'} <= names)


if __name__ == '__main__':
    unittest.main()
