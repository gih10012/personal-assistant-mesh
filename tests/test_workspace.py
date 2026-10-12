"""Real isolated authority ledgers; synthetic provider, not production evidence."""
import base64
import copy
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.artifact_transport import ArtifactProducer
from assistant_mesh.campus_adapter import execute
from assistant_mesh.provider import Adapter, ManagedProvider
from assistant_mesh.server import API
from assistant_mesh.workspace import MeshWorkspace, WorkspaceError


class BoundClient:
    def __init__(self, api, node='worker', grants=None):
        self.api, self.node = api, node
        self.grants = ['a2a.send'] if grants is None else grants
        self.calls, self.fail_before, self.fail_after = [], None, None

    def request(self, path, body=None):
        action = body.get('action', 'delegate') if body else 'hello'
        key = action if 'routing' in path or 'delegate' in path else path
        self.calls.append(key)
        if self.fail_before == key:
            self.fail_before = None
            raise TimeoutError('PRIVATE_SECRET_PATH')
        value = self.api.dispatch('POST' if body is not None else 'GET', path,
            body or {}, {'role': 'worker', 'node': self.node, 'capabilities': self.grants})
        if self.fail_after == key:
            self.fail_after = None
            raise TimeoutError('PRIVATE_SECRET_PATH')
        return value


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.api = API({'database': str(self.root / 'authority.db'), 'peers': [],
            'node_id': 'cloud', 'network_peers': {'laptop': {'send_allowed': True}},
            'resources': {'trusted_verifiers': ['checker']}})
        self.now = 1000.0
        self.api.store.clock = lambda: self.now
        self.api.store.heartbeat('worker', ['leader', 'agent'])
        self.api.store.create_task('isolated workspace project', task_id='parent')
        self.task = self.api.store.claim('worker')
        self.client = BoundClient(self.api)
        self.path = self.root / 'workspace.db'
        self.destination = self.root / 'destination'
        self.destination.mkdir(mode=0o700)
        self.workspace = self.make()
        self.scope = {'operation': 'get', 'url': 'http://10.42.1.5/fixture'}
        self.workload = {'kind': 'campus_http', 'max_bytes': 262144, 'timeout_seconds': 5}
        self.arguments = {'issuer': 'laptop', 'operation_id': 'op', 'decision_id': 'choice',
            'selection': {'capability_id': 'campus-cap', 'epoch': 1, 'action': 'network.campus.http',
                'scope': self.scope, 'workload': self.workload, 'observations': [{
                    'observation_id': 'measurement', 'metric': 'reachability',
                    'unit': 'bool', 'max_age_seconds': 60}]},
            'candidates': [{'issuer': 'laptop', 'capability_id': 'campus-cap', 'epoch': 1}],
            'rationale': 'Synthetic measured campus route.', 'project_id': 'mesh-self-maintenance',
            'agent_id': 'network-specialist'}

    def tearDown(self):
        self.temp.cleanup()

    def make(self, epoch=None, readers=None):
        return MeshWorkspace(self.client, str(self.path), 'cloud', 'worker', 'parent',
            self.task['epoch'] if epoch is None else epoch, str(self.destination), readers)

    def call(self, **overrides):
        return self.workspace.call(**dict(copy.deepcopy(self.arguments), **overrides))

    def writes(self):
        return [name for name in self.client.calls if name in ('propose', 'delegate', 'link')]

    def test_remote_composition_keeps_model_scope_and_project(self):
        result = self.call()
        self.assertEqual('observed', result['phase'])
        self.assertEqual(['propose', 'delegate', 'link'], self.writes())
        self.assertEqual('waiting_remote', result['routing']['linked_execution']['state'])
        with sqlite3.connect(self.api.store.path) as db:
            raw = db.execute('SELECT body FROM mesh_deliveries').fetchone()[0]
        message = json.loads(raw)
        self.assertEqual('mesh-self-maintenance', message['project'])
        self.assertEqual('network-specialist', message['agent'])
        self.assertIn('"operation_id": "op"', message['input'])
        self.assertEqual(self.scope, result['routing']['decision']['work']['scope'])
        self.assertFalse(result['execution_verified'])
        self.assertFalse(result['native_tools_intercepted'])

    def test_restart_exact_request_reads_only_original_receipts(self):
        first = self.call()
        self.workspace = self.make()
        second = self.call()
        self.assertEqual(first['remote'], second['remote'])
        self.assertEqual(first['request_sha256'], second['request_sha256'])
        self.assertEqual(['propose', 'delegate', 'link'], self.writes())

    def test_changed_content_or_issuer_cannot_fallback(self):
        self.call()
        for changes in ({'issuer': 'other'}, {'rationale': 'new'}, {'project_id': 'other'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(WorkspaceError, 'intent_conflict'):
                self.call(**changes)
        self.assertEqual(3, len(self.writes()))

    def test_lost_ack_each_stage_recovers_only_original_read_then_next_stage(self):
        for stage in ('propose', 'delegate', 'link'):
            with self.subTest(stage=stage):
                args = dict(self.arguments, operation_id='op-' + stage, decision_id='choice-' + stage)
                self.client.calls.clear()
                self.client.fail_after = stage
                self.assertEqual('unresolved', self.workspace.call(**args)['phase'])
                self.workspace = self.make()
                self.assertEqual('observed', self.workspace.call(**args)['phase'])
                self.assertEqual(['propose', 'delegate', 'link'], self.writes())

    def test_no_effect_timeout_is_not_permission_to_resubmit(self):
        self.client.fail_before = 'delegate'
        self.assertEqual('unresolved', self.call()['phase'])
        self.workspace = self.make()
        self.assertEqual('unresolved', self.call()['phase'])
        self.assertEqual(['propose', 'delegate'], self.writes())

    def test_different_original_delegate_with_same_ids_never_confirms_or_links(self):
        self.client.fail_before = 'delegate'
        self.call()
        identity = self.workspace._remote_identity(self.workspace._row('op'))
        self.api.remote.delegate('parent', 'worker', self.task['epoch'], identity['call_id'],
                                 'laptop', {'input': 'different actual work', 'project_id': 'other',
                                            'agent_id': 'different-specialist'})
        result = self.call()
        self.assertEqual('unresolved', result['phase'])
        self.assertEqual('attempted', result['stages']['delegate'])
        self.assertNotIn('link', self.writes())

    def test_wire_bytes_are_frozen_across_prompt_renderer_upgrade(self):
        first = self.call()
        row = self.workspace._row('op')
        message = self.workspace._remote_message(row)
        changed = dict(message, input='new source prompt')
        with mock.patch.object(MeshWorkspace, '_render_message', return_value=changed):
            self.workspace = self.make()
            result = self.call()
            self.assertEqual(message, self.workspace._remote_message(self.workspace._row('op')))
        self.assertEqual('observed', result['phase'])
        self.assertEqual(first['request_sha256'], result['request_sha256'])
        self.assertEqual(3, len(self.writes()))

    def test_foreign_authority_rejects_unenrolled_peer_without_retry(self):
        self.assertEqual('unresolved', self.call(issuer='not-enrolled')['phase'])
        self.assertEqual('unresolved', self.call(issuer='not-enrolled')['phase'])
        self.assertEqual(1, self.writes().count('delegate'))
        with sqlite3.connect(self.api.store.path) as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM remote_delegations').fetchone()[0])

    def test_epoch_change_does_not_start_a_ready_stage(self):
        self.client.fail_after = 'propose'
        self.call()
        with sqlite3.connect(self.api.store.path) as db:
            db.execute("UPDATE tasks SET epoch=2 WHERE id='parent'")
        self.workspace = self.make(epoch=2)
        self.assertEqual('original_epoch_read_only', self.call()['phase'])
        self.assertEqual(['propose'], self.writes())

    def test_discover_preserves_scope_units_and_truncation_not_probe(self):
        value = self.workspace.discover(limit=1)
        self.assertEqual('mesh-routing-context/1', value['format'])
        self.assertIn('local_pools', value['sections'])
        self.assertIn('federated_capabilities', value['sections'])
        self.assertFalse(value['network_probed'])
        self.assertFalse(value['managed_invocation_authorized'])
        self.assertEqual([], self.writes())

    def test_artifact_gate_never_runs_an_invocation(self):
        self.call()
        count = len(self.writes())
        self.assertEqual('artifact_unavailable', self.workspace.artifact('op')['phase'])
        self.assertEqual(count, len(self.writes()))

    def test_local_call_uses_original_admission_not_remote_queue(self):
        selection = self.arguments['selection']
        self.api.resources.advertise('node:worker', 'campus-cap', 'network.egress.campus_http')
        self.api.resources.observe('checker', 'campus-cap', 'reachability', 1, 'bool', 'fixture',
            epoch=1, observation_id='measurement', verification='verified',
            evidence={'scope': self.scope, 'workload': self.workload})
        self.api.allocations.define_pool('operator', 'shared', {'requests': {'capacity': 1, 'unit': 'slot'}})
        self.api.allocations.bind_pool('operator', 'campus-cap', 1, 'shared', 1, 'requests', 1, 'slot')
        result = self.call(issuer='cloud', selection=selection)
        self.assertEqual('reserved', result['operation_state'])
        self.assertNotIn('delegate', self.writes())
        self.assertEqual('reserved', self.workspace.status('op')['operation_state'])

    @staticmethod
    def save(path, value):
        raw = json.dumps(value).encode('utf8')
        path.write_bytes(raw)
        path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    def original_artifact(self):
        self.call()
        remote_root = self.root / 'remote'
        remote_root.mkdir(mode=0o700)
        artifact_root = remote_root / 'artifacts'
        artifact_root.mkdir(mode=0o700)
        api = API({'node_id': 'laptop', 'database': str(remote_root / 'authority.db'),
                   'peers': [], 'resources': {'trusted_verifiers': ['checker']}})
        Path(api.store.path).chmod(0o600)
        api.store.clock = lambda: self.now
        row = self.workspace._row('op')
        message = self.workspace._remote_message(row)
        api.network.receive('cloud', message)
        api.store.heartbeat('laptop', ['agent', 'mesh.node:laptop'])
        task = api.store.claim('laptop')
        api.resources.advertise('node:laptop', 'campus-cap', 'network.egress.campus_http')
        api.resources.observe('checker', 'campus-cap', 'reachability', 1, 'bool', 'fixture',
            epoch=1, observation_id='measurement', verification='verified',
            evidence={'scope': self.scope, 'workload': self.workload})
        api.allocations.define_pool('operator', 'requests', {'requests': {'capacity': 1, 'unit': 'slot'}})
        api.allocations.bind_pool('operator', 'campus-cap', 1, 'requests', 1, 'requests', 1, 'slot')
        api.allocations.reserve('node:laptop', 'op', task['id'], task['epoch'],
            {'target': 'campus-cap', 'route': [], 'selections': [self.arguments['selection']]}, 60)
        owner_path = remote_root / 'adapter.json'
        owner_sha = self.save(owner_path, {'schema': 1, 'provider': 'node:laptop',
            'capability_id': 'campus-cap', 'capability_epoch': 1, 'action': 'network.campus.http',
            'egress_node': 'laptop', 'artifact_root': str(artifact_root),
            'allowed_networks': ['10.42.0.0/16'], 'allowed_ports': [[80, 443]],
            'allowed_schemes': ['http', 'https'], 'expires_at': 9999999999,
            'max_bytes': 262144, 'timeout_seconds': 20})
        client = BoundClient(api, node='laptop')
        journal = remote_root / 'provider.db'
        provider = ManagedProvider(client, journal, 'node:laptop', {'campus-cap': Adapter(
            'campus@1', 1, 'network.campus.http', lambda context: execute(context, owner_path, owner_sha))},
            authority_id='laptop')
        body = bytes(range(256)) * 600
        pending = api.allocations.pending('node:laptop')['dispatch'][0]
        with mock.patch('assistant_mesh.campus_adapter._bounded_capture',
                        return_value=(0, body + b'\nMESH_CAMPUS_METADATA:200 10.42.1.5 0')):
            self.assertEqual('settled', provider.run(pending)['state'])
        transport_path = remote_root / 'transport.json'
        transport_sha = self.save(transport_path, {'schema': 1, 'authority': 'laptop', 'sender': 'cloud',
            'authority_db': str(api.store.path), 'max_artifact_bytes': 24 * 1024 * 1024,
            'bindings': {'campus-cap': {'adapter': 'campus', 'provider': 'node:laptop',
                'provider_journal': str(journal), 'owner_config_path': str(owner_path),
                'owner_config_sha256': owner_sha}}})
        producer = ArtifactProducer(transport_path, transport_sha)

        class Reader:
            def manifest(self, *args):
                return producer.manifest(*args)

            def read(self, *args):
                output = io.BytesIO()
                producer.write_to(*args, output)
                return output.getvalue()

        return Reader(), body

    def test_automatic_original_full_artifact_and_restart_no_http_replay(self):
        reader, body = self.original_artifact()
        self.workspace = self.make(readers={'laptop': reader})
        with mock.patch('assistant_mesh.campus_adapter.execute', side_effect=AssertionError('HTTP replay')):
            result = self.workspace.artifact('op')
            repeated = self.make(readers={'laptop': reader}).artifact('op')
        self.assertEqual('artifact_verified', result['phase'])
        self.assertTrue(result['artifact']['artifact_content_verified'])
        self.assertFalse(result['execution_verified'])
        raw = Path(result['artifact_path']).read_bytes()
        self.assertEqual(body, base64.b64decode(json.loads(raw)['response_base64']))
        self.assertEqual(len(body), result['artifact']['body_bytes'])
        self.assertTrue(repeated['artifact']['already_present'])
        self.assertEqual(0o600, Path(result['artifact_path']).stat().st_mode & 0o777)

    def test_manifest_message_fingerprint_and_body_tamper_rejected(self):
        reader, body = self.original_artifact()
        original = reader.manifest
        reader.manifest = lambda *args: dict(original(*args), message_fingerprint='f' * 64)
        self.workspace = self.make(readers={'laptop': reader})
        self.assertEqual('artifact_unavailable', self.workspace.artifact('op')['phase'])
        reader.manifest = original
        raw_read = reader.read
        reader.read = lambda *args: raw_read(*args)[:-1]
        self.assertEqual('artifact_unavailable', self.workspace.artifact('op')['phase'])
        self.assertEqual([], list(self.destination.iterdir()))
