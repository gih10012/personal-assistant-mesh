"""Local allocation/full-file integration; real journals, synthetic HTTP only."""
import base64
import copy
import hashlib
import io
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from assistant_mesh import workspace_cli
from assistant_mesh.artifact_transport import LOCAL_FORMAT, ArtifactTransportError, LocalArtifactProducer
from assistant_mesh.campus_adapter import execute
from assistant_mesh.provider import Adapter, ManagedProvider
from tests import test_workspace as workspace_fixture


class ProducerReader:
    def __init__(self, producer):
        self.producer, self.calls = producer, []

    def manifest(self, *identities):
        self.calls.append(('manifest', identities))
        return self.producer.manifest(*identities)

    def read(self, *identities):
        self.calls.append(('read', identities))
        stream = io.BytesIO()
        self.producer.write_to(*identities, binary_stream=stream)
        return stream.getvalue()


class WorkspaceLocalArtifactTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace_fixture.WorkspaceTests()
        self.fixture.setUp()
        self.root, self.api, self.client = self.fixture.root, self.fixture.api, self.fixture.client
        self.workspace = self.fixture.workspace
        Path(self.api.store.path).chmod(0o600)
        self.arguments = dict(copy.deepcopy(self.fixture.arguments), issuer='cloud')
        self.api.resources.advertise('node:worker', 'campus-cap', 'network.egress.campus_http')
        self.api.resources.observe('checker', 'campus-cap', 'reachability', 1, 'bool', 'synthetic fixture',
            epoch=1, observation_id='measurement', verification='verified',
            evidence={'scope': self.fixture.scope, 'workload': self.fixture.workload})
        self.api.allocations.define_pool('operator', 'requests', {'requests': {'capacity': 1, 'unit': 'slot'}})
        self.api.allocations.bind_pool('operator', 'campus-cap', 1, 'requests', 1, 'requests', 1, 'slot')
        self.assertEqual('reserved', self.workspace.call(**self.arguments)['operation_state'])
        self.source = self.root / 'source-artifacts'
        self.source.mkdir(mode=0o700)
        self.owner_path = self.root / 'local-adapter.json'
        self.owner = {'schema': 1, 'provider': 'node:worker', 'capability_id': 'campus-cap',
            'capability_epoch': 1, 'action': 'network.campus.http', 'egress_node': 'worker',
            'artifact_root': str(self.source), 'allowed_networks': ['10.42.0.0/16'],
            'allowed_ports': [[80, 443]], 'allowed_schemes': ['http', 'https'],
            'expires_at': 9999999999, 'max_bytes': 262144, 'timeout_seconds': 20}
        self.owner_sha = self.fixture.save(self.owner_path, self.owner)
        self.provider_db = self.root / 'local-provider.db'
        provider = ManagedProvider(self.client, self.provider_db, 'node:worker', {
            'campus-cap': Adapter('owner.campus@1', 1, 'network.campus.http',
                lambda context: execute(context, self.owner_path, self.owner_sha))}, authority_id='cloud')
        self.body = bytes(range(256)) * 600
        pending = self.api.allocations.pending('node:worker')['dispatch'][0]
        with mock.patch('assistant_mesh.campus_adapter._bounded_capture',
                        return_value=(0, self.body + b'\nMESH_CAMPUS_METADATA:200 10.42.1.5 0')):
            self.assertEqual('settled', provider.run(pending)['state'])
        self.transport_path = self.root / 'local-producer-owner.json'
        self.transport = {'schema': 1, 'authority': 'cloud', 'node': 'worker',
            'authority_db': str(self.api.store.path), 'max_artifact_bytes': 24 * 1024 * 1024,
            'bindings': {'campus-cap': {'adapter': 'campus', 'provider': 'node:worker',
                'provider_journal': str(self.provider_db), 'owner_config_path': str(self.owner_path),
                'owner_config_sha256': self.owner_sha}}}
        self.transport_sha = self.fixture.save(self.transport_path, self.transport)
        self.producer = LocalArtifactProducer(self.transport_path, self.transport_sha)
        self.reader = ProducerReader(self.producer)
        self.workspace = self.fixture.make(readers={'cloud': self.reader})
        self.client.calls.clear()

    def tearDown(self):
        self.fixture.tearDown()

    def fetch(self):
        return self.workspace.artifact('op')

    def mutate(self, path, statement, parameters=()):
        with closing(sqlite3.connect(str(path))) as db:
            with db:
                db.execute(statement, parameters)

    def source_snapshot(self):
        values = []
        for path in (self.api.store.path, self.provider_db, self.fixture.path):
            with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as db:
                values.append(tuple(db.iterdump()))
        return values

    def test_complete_local_artifact_no_fabricated_a2a_origin(self):
        result = self.fetch()
        self.assertEqual('artifact_verified', result['phase'])
        manifest = result['artifact']
        self.assertEqual(LOCAL_FORMAT, manifest['format'])
        self.assertEqual('local_allocation', result['artifact_source_kind'])
        self.assertIsNone(result['remote'])
        self.assertEqual({'propose': 'confirmed', 'reserve': 'confirmed', 'link': 'confirmed'}, result['stages'])
        self.assertTrue(manifest['artifact_content_verified'])
        self.assertFalse(manifest['execution_verified'])
        self.assertFalse(manifest['managed_artifact_grant_verified'])
        self.assertFalse(result['automatic_invocation_replay'])
        self.assertFalse(result['native_tools_intercepted'])
        self.assertEqual('worker', manifest['task_node'])
        self.assertEqual('parent', manifest['task_id'])
        for key in ('sender', 'message_id', 'message_fingerprint'):
            self.assertNotIn(key, manifest)
        raw = Path(result['artifact_path']).read_bytes()
        self.assertEqual(raw, (self.source / manifest['artifact_id']).read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['artifact_sha256'])
        self.assertEqual(self.body, base64.b64decode(json.loads(raw)['response_base64']))
        self.assertEqual(len(self.body), manifest['body_bytes'])
        self.assertEqual(0o600, Path(result['artifact_path']).stat().st_mode & 0o777)
        self.assertEqual([('manifest', ('parent', 'op', 'campus-cap')),
                          ('read', ('parent', 'op', 'campus-cap'))], self.reader.calls)

    def test_read_only_after_source_and_private_journal_snapshot_no_api_or_constructor(self):
        before = self.source_snapshot()
        with mock.patch.object(self.client, 'request', side_effect=AssertionError('no RPC')), \
             mock.patch('assistant_mesh.store.Store.__init__', side_effect=AssertionError('no Store')), \
             mock.patch('assistant_mesh.provider.ManagedProvider.__init__', side_effect=AssertionError('no provider')), \
             mock.patch('assistant_mesh.campus_adapter.execute', side_effect=AssertionError('no GET')), \
             mock.patch('assistant_mesh.egress_adapter.execute', side_effect=AssertionError('no GET')), \
             mock.patch('assistant_mesh.workspace.TaskCapabilityCalls.__init__', side_effect=AssertionError('no call')):
            self.assertEqual('artifact_verified', self.fetch()['phase'])
        self.assertEqual(before, self.source_snapshot())
        self.assertEqual([], self.client.calls)

    def test_continued_task_and_expired_adapter_read_original_complete_file(self):
        self.mutate(self.api.store.path, "UPDATE tasks SET epoch=2,node='replacement',status='pending',deadline=0 "
                    "WHERE id='parent'")
        self.owner['expires_at'] = 1
        owner_sha = self.fixture.save(self.owner_path, self.owner)
        self.transport['bindings']['campus-cap']['owner_config_sha256'] = owner_sha
        sha = self.fixture.save(self.transport_path, self.transport)
        self.reader = ProducerReader(LocalArtifactProducer(self.transport_path, sha))
        self.workspace = self.fixture.make(epoch=2, readers={'cloud': self.reader})
        with mock.patch.object(self.client, 'request', side_effect=TimeoutError('offline')):
            result = self.fetch()
        self.assertEqual('artifact_verified', result['phase'])
        self.assertEqual(1, result['artifact']['task_epoch'])
        self.assertEqual(2, result['current_epoch'])
        self.assertEqual('worker', result['artifact']['task_node'])

    def test_provider_unknown_and_unsettled_dispatch_remain_unchanged(self):
        for state in ('unknown', 'settlement_unknown', 'active'):
            self.mutate(self.provider_db, 'UPDATE provider_executions SET state=?', (state,))
            before = self.source_snapshot()
            self.assertEqual('artifact_unavailable', self.fetch()['phase'])
            self.assertEqual(before, self.source_snapshot())
            self.assertEqual([], list(self.fixture.destination.iterdir()))
        self.mutate(self.provider_db, "UPDATE provider_executions SET state='settled'")
        self.mutate(self.api.store.path, "UPDATE managed_dispatch SET state='unknown'")
        before = self.source_snapshot()
        self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.assertEqual(before, self.source_snapshot())

    def test_missing_local_call_table_is_not_created_by_read(self):
        self.mutate(self.fixture.path, 'DROP TABLE capability_calls')
        before = self.source_snapshot()
        self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.assertEqual(before, self.source_snapshot())
        self.assertEqual([], self.reader.calls)
        self.assertEqual([], list(self.fixture.destination.iterdir()))

    def test_local_call_intent_change_or_reserve_not_attempted_cannot_read(self):
        with closing(sqlite3.connect(str(self.fixture.path))) as db:
            original = db.execute("SELECT request FROM capability_calls WHERE operation_id='op'").fetchone()[0]
        changed = json.loads(original)
        changed['decision']['rationale'] = 'changed after original invocation'
        encoded = json.dumps(changed, sort_keys=True, separators=(',', ':'))
        self.mutate(self.fixture.path, 'UPDATE capability_calls SET request=?,fingerprint=?',
                    (encoded, hashlib.sha256(encoded.encode('utf8')).hexdigest()))
        self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.mutate(self.fixture.path, "UPDATE capability_calls SET request=?,fingerprint=?,reserve='ready'",
                    (original, hashlib.sha256(original.encode('utf8')).hexdigest()))
        self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.assertEqual([], self.reader.calls)

    def test_origin_epoch_plan_scope_and_workload_manifest_mismatches_never_read_body(self):
        original = self.reader.manifest
        for key, value in (('authority', 'other'), ('task_node', 'other'), ('task_id', 'other'),
                           ('task_epoch', 2), ('capability_epoch', 2), ('capability_id', 'other'),
                           ('operation_id', 'other'), ('source_kind', 'a2a'),
                           ('allocation_plan_sha256', 'f' * 64), ('scope_sha256', 'e' * 64),
                           ('workload_sha256', 'd' * 64)):
            with self.subTest(key=key):
                self.reader.manifest = lambda *args: dict(original(*args), **{key: value})
                with mock.patch.object(self.reader, 'read', side_effect=AssertionError('body must not read')):
                    self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.assertEqual([], list(self.fixture.destination.iterdir()))

    def test_wrong_owner_node_authority_and_remote_proxy_refused_at_producer(self):
        for key, value in (('node', 'other'), ('authority', 'other')):
            config = dict(self.transport, **{key: value})
            sha = self.fixture.save(self.transport_path, config)
            with self.subTest(key=key), self.assertRaises(ArtifactTransportError):
                LocalArtifactProducer(self.transport_path, sha).manifest('parent', 'op', 'campus-cap')
        self.fixture.save(self.transport_path, self.transport)
        self.mutate(self.api.store.path, 'UPDATE tasks SET context=?', (json.dumps({'remote_proxy': True}),))
        with self.assertRaises(ArtifactTransportError):
            self.producer.manifest('parent', 'op', 'campus-cap')

    def test_body_hash_raw_truncation_and_fake_remote_fields_publish_nothing(self):
        original_manifest, original_read = self.reader.manifest, self.reader.read
        self.reader.manifest = lambda *args: dict(original_manifest(*args), body_sha256='e' * 64)
        self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.reader.manifest = original_manifest
        self.reader.read = lambda *args: original_read(*args)[:-1]
        self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.reader.read = original_read
        self.reader.manifest = lambda *args: dict(original_manifest(*args), sender='cloud',
                                                message_id='fake', message_fingerprint='f' * 64)
        self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.assertEqual([], list(self.fixture.destination.iterdir()))

    def test_settlement_self_report_is_not_complete_provider_evidence(self):
        self.mutate(self.api.store.path, 'UPDATE managed_dispatch SET settlement=?',
                    (json.dumps({'outcome': 'completed', 'evidence': {'artifact_sha256': 'a' * 64}}),))
        self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.assertEqual([], list(self.fixture.destination.iterdir()))

    def test_restart_same_bytes_idempotent_and_conflicting_output_preserved(self):
        first = self.fetch()
        output = Path(first['artifact_path'])
        inode, raw = output.stat().st_ino, output.read_bytes()
        self.workspace = self.fixture.make(readers={'cloud': self.reader})
        again = self.fetch()
        self.assertTrue(again['artifact']['already_present'])
        self.assertEqual(inode, output.stat().st_ino)
        output.write_bytes(b'owner conflict to preserve')
        output.chmod(0o600)
        self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.assertEqual(b'owner conflict to preserve', output.read_bytes())
        self.assertGreater(len(raw), 65536)
        self.assertEqual([], self.client.calls)

    def thin_journal_only(self):
        self.mutate(self.fixture.path, 'DROP TABLE workspace_calls')
        self.mutate(self.fixture.path, 'DROP TABLE workspace_identity')

    def test_existing_thin_intent_artifact_only_keeps_entire_journal_and_no_workspace_history(self):
        self.thin_journal_only()
        before, raw_before = self.source_snapshot(), self.fixture.path.read_bytes()
        self.workspace = self.fixture.make(readers={'cloud': self.reader})
        with mock.patch.object(self.client, 'request', side_effect=AssertionError('no RPC')):
            result = self.fetch()
        self.assertEqual('artifact_verified', result['phase'])
        self.assertFalse(result['workspace_intent_present'])
        self.assertEqual('task_capability_calls', result['intent_source'])
        with closing(sqlite3.connect(str(self.fixture.path))) as db:
            original = db.execute("SELECT fingerprint FROM capability_calls WHERE operation_id='op'").fetchone()[0]
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(original, result['request_sha256'])
        self.assertEqual(original, result['local_call_request_sha256'])
        self.assertFalse(names & {'workspace_calls', 'workspace_identity'})
        self.assertEqual(before, self.source_snapshot())
        self.assertEqual(raw_before, self.fixture.path.read_bytes())
        with mock.patch('assistant_mesh.workspace.TaskCapabilityCalls.__init__', side_effect=AssertionError('no constructor')):
            status = self.workspace.status('op')
        self.assertEqual('completed', status['operation_state'])
        self.assertFalse(status['workspace_intent_present'])
        self.assertEqual(original, status['local_call_request_sha256'])
        self.assertEqual(before, self.source_snapshot())
        self.assertEqual(raw_before, self.fixture.path.read_bytes())

    def test_thin_only_identity_task_and_original_epoch_conflicts_never_adopt(self):
        self.thin_journal_only()
        self.mutate(self.fixture.path, "UPDATE call_identity SET node='foreign'")
        with self.assertRaises(Exception):
            self.fixture.make(readers={'cloud': self.reader})
        self.mutate(self.fixture.path, "UPDATE call_identity SET node='worker'")
        self.workspace = self.fixture.make(readers={'cloud': self.reader})
        self.mutate(self.fixture.path, "UPDATE capability_calls SET task_id='foreign'")
        with self.assertRaises(Exception):
            self.fetch()
        self.mutate(self.fixture.path, "UPDATE capability_calls SET task_id='parent',task_epoch=2")
        self.assertEqual('artifact_unavailable', self.fetch()['phase'])
        self.assertEqual([], list(self.fixture.destination.iterdir()))

    def test_real_new_workspace_call_initializes_only_its_own_intent_not_old_history(self):
        self.thin_journal_only()
        with closing(sqlite3.connect(str(self.fixture.path))) as db:
            original = db.execute("SELECT * FROM capability_calls WHERE operation_id='op'").fetchone()
        self.workspace = self.fixture.make(readers={'cloud': self.reader})
        exact = self.workspace.call(**self.arguments)
        self.assertEqual('completed', exact['operation_state'])
        with closing(sqlite3.connect(str(self.fixture.path))) as db:
            self.assertEqual([], db.execute("SELECT name FROM sqlite_master WHERE name='workspace_calls'").fetchall())
        result = self.workspace.call(**dict(self.arguments, operation_id='new-op', decision_id='new-choice'))
        self.assertEqual('reserved', result['operation_state'])
        with closing(sqlite3.connect(str(self.fixture.path))) as db:
            self.assertEqual(original, db.execute("SELECT * FROM capability_calls WHERE operation_id='op'").fetchone())
            self.assertEqual([('new-op',)], db.execute('SELECT operation_id FROM workspace_calls').fetchall())
        self.assertFalse(self.fetch()['workspace_intent_present'])

    def test_read_only_constructor_and_local_status_never_migrate_or_write(self):
        before, raw_before = self.source_snapshot(), self.fixture.path.read_bytes()
        from assistant_mesh.workspace import MeshWorkspace
        workspace = MeshWorkspace(self.client, str(self.fixture.path), 'cloud', 'worker', 'parent', 1,
            str(self.fixture.destination), {'cloud': self.reader}, read_only=True)
        with mock.patch('assistant_mesh.workspace.TaskCapabilityCalls.__init__', side_effect=AssertionError('no constructor')):
            result = workspace.status('op')
        self.assertEqual('completed', result['operation_state'])
        with self.assertRaises(Exception):
            workspace.call(**dict(self.arguments, operation_id='must-not-write', decision_id='never-write'))
        self.assertEqual(before, self.source_snapshot())
        self.assertEqual(raw_before, self.fixture.path.read_bytes())

    def test_cli_read_only_missing_journal_or_schema_is_not_created_or_migrated(self):
        client_path = self.root / 'read-only-client.json'
        client_sha = self.fixture.save(client_path, {'node_id': 'worker'})
        missing = self.root / 'missing.db'
        owner_path = self.root / 'read-only-owner.json'
        config = {'format': 'mesh-workspace-owner/1', 'client_config': str(client_path),
            'client_config_sha256': client_sha, 'authority': 'cloud', 'node': 'worker',
            'journal_path': str(missing), 'artifact_dir': str(self.fixture.destination), 'artifact_readers': {}}
        self.fixture.save(owner_path, config)
        for action in ('status', 'artifact'):
            with self.subTest(action=action), self.assertRaises(Exception):
                workspace_cli.execute(str(owner_path), 'parent', 1, action, operation_id='op',
                                      client_factory=lambda config: self.client)
            self.assertFalse(missing.exists())
        missing.touch(mode=0o600)
        before = missing.read_bytes()
        with self.assertRaises(Exception):
            workspace_cli.execute(str(owner_path), 'parent', 1, 'artifact', operation_id='op',
                                  client_factory=lambda config: self.client)
        self.assertEqual(before, missing.read_bytes())

    def test_fixed_command_template_real_child_transfers_local_full_bytes(self):
        # A LOCAL command uses an explicit task_id wire. The remote default
        # retains message_id; no model-selectable mode/path/config is added.
        self.thin_journal_only()
        script = self.root / 'local-fixed-reader.py'
        source = str(Path(__file__).resolve().parents[1])
        script.write_text('import base64,json,sys\n'
            'sys.path.insert(0,' + repr(source) + ')\n'
            'from assistant_mesh.artifact_transport import LocalArtifactProducer,_decode\n'
            'request=_decode(base64.b64decode(sys.argv[2]+"="*(-len(sys.argv[2])%4),altchars=b"-_",validate=True))\n'
            'assert set(request)=={"task_id","operation_id","capability_id"}\n'
            'producer=LocalArtifactProducer(' + repr(str(self.transport_path)) + ',' + repr(self.transport_sha) + ')\n'
            'args=[request[key] for key in ("task_id","operation_id","capability_id")]\n'
            'if sys.argv[1]=="manifest": print(json.dumps(producer.manifest(*args)))\n'
            'else: producer.write_to(*args,binary_stream=sys.stdout.buffer)\n')
        script.chmod(0o600)
        binding = {'manifest_argv': [sys.executable, str(script), 'manifest', '{request_base64url}'],
                   'read_argv': [sys.executable, str(script), 'read', '{request_base64url}'],
                   'timeout_seconds': 5, 'reference_kind': 'local_task'}
        owner_client = self.root / 'cli-client.json'
        sha = self.fixture.save(owner_client, {'node_id': 'worker'})
        owner_file = self.root / 'cli-owner.json'
        self.fixture.save(owner_file, {'format': 'mesh-workspace-owner/1', 'client_config': str(owner_client),
            'client_config_sha256': sha, 'authority': 'cloud', 'node': 'worker',
            'journal_path': str(self.fixture.path), 'artifact_dir': str(self.fixture.destination),
            'artifact_readers': {'cloud': binding}})
        before, raw_before, inode = self.source_snapshot(), self.fixture.path.read_bytes(), self.fixture.path.stat().st_ino
        result = workspace_cli.execute(str(owner_file), 'parent', 2, 'artifact', operation_id='op',
                                       client_factory=lambda config: self.client)
        self.assertEqual('artifact_verified', result['phase'])
        self.assertFalse(result['workspace_intent_present'])
        self.assertEqual(self.body, base64.b64decode(json.loads(Path(result['artifact_path']).read_bytes())['response_base64']))
        self.assertEqual(before, self.source_snapshot())
        self.assertEqual(raw_before, self.fixture.path.read_bytes())
        self.assertEqual(inode, self.fixture.path.stat().st_ino)
        self.assertEqual([], self.client.calls)


if __name__ == '__main__':
    unittest.main()
