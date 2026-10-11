"""Original receipt-bound artifact transfer; synthetic HTTP, no remote deployment."""
import base64
import copy
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from assistant_mesh import campus_adapter, egress_adapter
from assistant_mesh.artifact_transport import ArtifactProducer, ArtifactTransportError, fetch_artifact
from assistant_mesh.provider import Adapter, ManagedProvider
from assistant_mesh.server import API


class BoundClient:
    def __init__(self, api):
        self.api = api

    def request(self, path, body=None):
        return self.api.dispatch('POST' if body is not None else 'GET', path, body,
                                 {'role': 'worker', 'node': 'campus-node'})


class ArtifactTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-artifact-fixture.')
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir(mode=0o700)
        self.destination = self.root / 'destination'
        self.destination.mkdir(mode=0o700)
        self.database = self.root / 'authority.db'
        self.api = API({'node_id': 'campus-node', 'database': str(self.database),
                        'peers': [], 'resources': {'trusted_verifiers': ['operator']}})
        self.database.chmod(0o600)
        self.message = {'protocol': 'mesh-a2a/1', 'id': 'original-message', 'to': 'campus-node',
                        'input': 'fixture task, not a campus business request', 'project': 'campus',
                        'agent': 'specialist', 'parent_ref': 'original-parent-proxy'}
        self.receipt = self.api.network.receive('cloud', self.message)
        self.api.store.heartbeat('campus-node', ['agent', 'mesh.node:campus-node'])
        self.task = self.api.store.claim('campus-node')
        self.assertEqual(self.receipt['task_id'], self.task['id'])
        self.scope = {'operation': 'get', 'url': 'http://10.42.1.5/fixture'}
        self.workload = {'kind': 'campus_http', 'max_bytes': 262144, 'timeout_seconds': 5}
        self.owner_path = self.root / 'adapter-owner.json'
        self.owner = {'schema': 1, 'provider': 'node:campus-node', 'capability_id': 'campus-cap',
            'capability_epoch': 1, 'action': 'network.campus.http', 'egress_node': 'campus-node',
            'artifact_root': str(self.artifacts), 'allowed_networks': ['10.42.0.0/16'],
            'allowed_ports': [[80, 443]], 'allowed_schemes': ['http', 'https'],
            'expires_at': time.time() + 120, 'max_bytes': 262144, 'timeout_seconds': 20}
        self.owner_sha = self.save(self.owner_path, self.owner)
        self.api.resources.advertise('node:campus-node', 'campus-cap', 'network.egress.campus_http')
        self.api.resources.observe('operator', 'campus-cap', 'reachability', 1, 'bool', 'synthetic fixture',
            epoch=1, verification='verified', observation_id='fixture-observation',
            evidence={'scope': self.scope, 'workload': self.workload})
        self.api.allocations.define_pool('operator', 'fixture-requests',
                                        {'requests': {'capacity': 1, 'unit': 'request'}})
        self.api.allocations.bind_pool('operator', 'campus-cap', 1, 'fixture-requests', 1,
                                       'requests', 1, 'request')
        selection = {'capability_id': 'campus-cap', 'epoch': 1, 'action': 'network.campus.http',
            'scope': self.scope, 'workload': self.workload,
            'observations': [{'observation_id': 'fixture-observation', 'metric': 'reachability',
                              'unit': 'bool', 'max_age_seconds': 60}]}
        self.api.allocations.reserve('node:campus-node', 'original-operation', self.task['id'],
            self.task['epoch'], {'target': 'campus-cap', 'route': [], 'selections': [selection]}, 60)
        self.journal = self.root / 'provider.db'
        self.provider = ManagedProvider(BoundClient(self.api), self.journal, 'node:campus-node',
            {'campus-cap': Adapter('owner.campus@1', 1, 'network.campus.http',
                lambda context: campus_adapter.execute(context, self.owner_path, self.owner_sha))},
            authority_id='campus-node')
        self.body = b'private fixture, never log: ' + bytes(range(256)) * 430
        reply = self.body + b'\nMESH_CAMPUS_METADATA:200 10.42.1.5 0'
        pending = self.api.allocations.pending('node:campus-node')['dispatch'][0]
        with mock.patch('assistant_mesh.campus_adapter._bounded_capture', return_value=(0, reply)):
            self.result = self.provider.run(pending)
        self.assertEqual('settled', self.result['state'])
        self.transport_path = self.root / 'transport-owner.json'
        self.transport_config = {'schema': 1, 'authority': 'campus-node', 'sender': 'cloud',
            'authority_db': str(self.database), 'max_artifact_bytes': 24 * 1024 * 1024,
            'bindings': {'campus-cap': {'adapter': 'campus', 'provider': 'node:campus-node',
                'provider_journal': str(self.journal), 'owner_config_path': str(self.owner_path),
                'owner_config_sha256': self.owner_sha}}}
        self.transport_sha = self.save(self.transport_path, self.transport_config)
        self.producer = ArtifactProducer(self.transport_path, self.transport_sha)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def save(path, value):
        raw = json.dumps(value, ensure_ascii=False).encode('utf8')
        path.write_bytes(raw)
        path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    def manifest(self):
        return self.producer.manifest('original-message', 'original-operation', 'campus-cap')

    def stream(self):
        stream = io.BytesIO()
        manifest = self.producer.write_to('original-message', 'original-operation', 'campus-cap', stream)
        stream.seek(0)
        return manifest, stream

    def mutate(self, database, sql, values=()):
        with self.db(database) as db:
            db.execute(sql, values)

    @staticmethod
    @contextmanager
    def db(path):
        connection = sqlite3.connect(str(path))
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def test_complete_exact_original_bytes_large_body_and_two_hashes(self):
        manifest, stream = self.stream()
        original = (self.artifacts / manifest['artifact_id']).read_bytes()
        self.assertGreater(len(self.body), 65536)
        self.assertEqual(original, stream.getvalue())
        self.assertEqual(hashlib.sha256(original).hexdigest(), manifest['artifact_sha256'])
        self.assertEqual(hashlib.sha256(self.body).hexdigest(), manifest['body_sha256'])
        self.assertNotEqual(manifest['artifact_sha256'], manifest['body_sha256'])
        output = self.destination / 'original.json'
        result = fetch_artifact(stream, manifest, output)
        self.assertEqual(original, output.read_bytes())
        self.assertEqual(self.body, base64.b64decode(json.loads(output.read_text())['response_base64']))
        self.assertEqual(0o600, output.stat().st_mode & 0o777)
        self.assertTrue(result['artifact_content_verified'])
        self.assertEqual(self.task['id'], manifest['task_id'])
        self.assertEqual(self.task['epoch'], manifest['task_epoch'])
        self.assertEqual('provider_reported', manifest['evidence_level'])
        self.assertTrue(manifest['native_owner_file_authority'])
        self.assertFalse(manifest['managed_artifact_grant_verified'])
        self.assertFalse(manifest['execution_verified'])
        self.assertNotIn('10.42.', json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn('private fixture', json.dumps(result))

    def test_producer_read_never_constructs_store_provider_or_calls_network(self):
        before = self.database.read_bytes(), self.journal.read_bytes()
        with mock.patch('assistant_mesh.store.Store.__init__', side_effect=AssertionError('Store forbidden')), \
             mock.patch('assistant_mesh.provider.ManagedProvider.__init__', side_effect=AssertionError('provider forbidden')), \
             mock.patch('assistant_mesh.campus_adapter.execute', side_effect=AssertionError('HTTP forbidden')), \
             mock.patch('assistant_mesh.egress_adapter.execute', side_effect=AssertionError('HTTP forbidden')):
            self.stream()
        self.assertEqual(before, (self.database.read_bytes(), self.journal.read_bytes()))

    def test_readonly_sqlite_connection_and_query_only(self):
        from assistant_mesh.artifact_transport import _readonly
        with _readonly(self.database, ('mesh_messages',)) as db:
            self.assertEqual(1, db.execute('PRAGMA query_only').fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM mesh_messages')

    def test_original_mapping_and_sender_are_owner_bound(self):
        for arguments in (('foreign-message', 'original-operation', 'campus-cap'),
                          ('original-message', 'foreign-operation', 'campus-cap'),
                          ('original-message', 'original-operation', 'unknown-cap')):
            with self.subTest(arguments=arguments), self.assertRaises(ArtifactTransportError):
                self.producer.manifest(*arguments)
        self.mutate(self.database, 'UPDATE mesh_messages SET sender=?', ('another-sender',))
        with self.assertRaises(ArtifactTransportError):
            self.manifest()

    def test_original_task_context_origin_and_mapping_mismatch_rejected(self):
        self.mutate(self.database, 'UPDATE tasks SET context=?', (json.dumps({'authority': 'local:campus-node',
            'origin': {'kind': 'a2a', 'peer': 'foreign', 'message_id': 'original-message'}}),))
        with self.assertRaisesRegex(ArtifactTransportError, 'task_origin_mismatch'):
            self.manifest()

    def test_historical_completed_artifact_read_survives_task_epoch_change(self):
        self.mutate(self.database, "UPDATE tasks SET epoch=epoch+1,status='pending',input='new explicit work'")
        self.assertEqual(1, self.manifest()['task_epoch'])

    def test_unsettled_original_provider_is_not_reconciled_or_replayed(self):
        for state in ('unknown', 'settlement_unknown', 'active'):
            self.mutate(self.journal, 'UPDATE provider_executions SET state=?', (state,))
            with self.subTest(state=state), self.assertRaisesRegex(ArtifactTransportError, 'provider_unsettled'):
                self.manifest()
            with self.db(self.journal) as db:
                self.assertEqual(state, db.execute('SELECT state FROM provider_executions').fetchone()[0])

    def test_settlement_mismatch_does_not_accept_child_or_provider_self_report(self):
        self.mutate(self.database, 'UPDATE managed_dispatch SET settlement=?',
                    (json.dumps({'outcome': 'completed', 'evidence': {'artifact_sha256': 'a' * 64}}),))
        with self.assertRaisesRegex(ArtifactTransportError, 'settlement_mismatch'):
            self.manifest()

    def test_provider_receipt_and_original_context_are_checked(self):
        self.mutate(self.journal, 'UPDATE provider_executions SET receipt_id=?', ('forged-receipt',))
        with self.assertRaisesRegex(ArtifactTransportError, 'provider_context_mismatch'):
            self.manifest()

    def test_original_db_missing_or_unsupported_schema_never_created(self):
        config = copy.deepcopy(self.transport_config)
        missing = self.root / 'does-not-exist.sqlite'
        config['authority_db'] = str(missing)
        pin = self.save(self.transport_path, config)
        with self.assertRaises(ArtifactTransportError):
            ArtifactProducer(self.transport_path, pin)
        self.assertFalse(missing.exists())
        self.save(self.transport_path, self.transport_config)
        self.mutate(self.database, 'ALTER TABLE mesh_messages ADD COLUMN unsupported TEXT')
        with self.assertRaisesRegex(ArtifactTransportError, 'source_schema_unsupported'):
            self.manifest()

    def test_private_owner_config_pin_cannot_change_or_override_sender(self):
        self.transport_config['sender'] = 'foreign'
        self.save(self.transport_path, self.transport_config)
        with self.assertRaisesRegex(ArtifactTransportError, 'owner_config_changed'):
            self.manifest()

    def test_consumer_truncation_extra_bytes_and_hash_mismatch_publish_nothing(self):
        manifest, stream = self.stream()
        raw = stream.getvalue()
        for bad in (raw[:-1], raw + b'x', b'x' + raw[1:]):
            with self.subTest(length=len(bad)), self.assertRaises(ArtifactTransportError):
                fetch_artifact(io.BytesIO(bad), manifest, self.destination / 'original.json')
            self.assertEqual([], list(self.destination.iterdir()))

    def test_external_manifest_body_identity_tamper_rejected_even_if_raw_hash_is_correct(self):
        manifest, stream = self.stream()
        for key, value in (('body_sha256', 'a' * 64), ('task_id', 'foreign-task'), ('receipt_id', 'foreign-receipt'),
                           ('invocation_nonce', 'b' * 32), ('scope_sha256', 'b' * 64)):
            changed = dict(manifest)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ArtifactTransportError):
                fetch_artifact(io.BytesIO(stream.getvalue()), changed, self.destination / 'original.json')
            self.assertEqual([], list(self.destination.iterdir()))

    def test_identical_existing_output_is_idempotent_without_reading_stream(self):
        manifest, stream = self.stream()
        output = self.destination / 'original.json'
        fetch_artifact(stream, manifest, output)
        inode = output.stat().st_ino
        unread = mock.Mock()
        result = fetch_artifact(unread, manifest, output)
        self.assertTrue(result['already_present'])
        unread.read.assert_not_called()
        self.assertEqual(inode, output.stat().st_ino)

    def test_existing_different_file_symlink_and_wrong_permissions_are_not_overwritten(self):
        manifest, stream = self.stream()
        output = self.destination / 'original.json'
        output.write_bytes(b'owner file to preserve')
        output.chmod(0o600)
        with self.assertRaises(ArtifactTransportError):
            fetch_artifact(io.BytesIO(stream.getvalue()), manifest, output)
        self.assertEqual(b'owner file to preserve', output.read_bytes())
        alternate = self.destination / 'symlink.json'
        alternate.symlink_to(output)
        with self.assertRaises(ArtifactTransportError):
            fetch_artifact(io.BytesIO(stream.getvalue()), manifest, alternate)
        self.destination.chmod(0o755)
        with self.assertRaises(ArtifactTransportError):
            fetch_artifact(io.BytesIO(stream.getvalue()), manifest, self.destination / 'new.json')

    def test_limit_evidence_level_and_extra_manifest_fields_rejected(self):
        manifest, stream = self.stream()
        changed = dict(manifest, managed_artifact_grant_verified=True)
        with self.assertRaisesRegex(ArtifactTransportError, 'manifest_evidence_invalid'):
            fetch_artifact(stream, changed, self.destination / 'original.json')
        with self.assertRaises(ArtifactTransportError):
            fetch_artifact(stream, dict(manifest, private_path='SECRET'), self.destination / 'original.json')
        with self.assertRaises(ArtifactTransportError):
            fetch_artifact(stream, manifest, self.destination / 'original.json', max_artifact_bytes=64)

    def test_private_stream_short_write_and_redacted_transport_error(self):
        with self.assertRaisesRegex(ArtifactTransportError, 'stream_short_write'):
            self.producer.write_to('original-message', 'original-operation', 'campus-cap',
                                   mock.Mock(write=lambda ignored: 1))
        manifest = self.manifest()
        stream = mock.Mock(read=mock.Mock(side_effect=OSError('PRIVATE_PATH_AND_TOKEN')))
        with self.assertRaises(ArtifactTransportError) as raised:
            fetch_artifact(stream, manifest, self.destination / 'original.json')
        self.assertNotIn('PRIVATE_PATH_AND_TOKEN', str(raised.exception))
        self.assertEqual([], list(self.destination.iterdir()))

    def test_publish_conflict_that_disappears_does_not_claim_saved_output(self):
        manifest, stream = self.stream()
        with mock.patch('assistant_mesh.artifact_transport.os.link', side_effect=FileExistsError()):
            with self.assertRaisesRegex(ArtifactTransportError, 'artifact_output_changed'):
                fetch_artifact(stream, manifest, self.destination / 'original.json')
        self.assertEqual([], list(self.destination.iterdir()))

    def test_parent_directory_replaced_during_transfer_is_not_acknowledged(self):
        manifest, original = self.stream()
        moved = self.root / 'previous-destination'
        class MovingStream:
            def __init__(inner):
                inner.moved = False

            def read(inner, maximum):
                if not inner.moved:
                    self.destination.rename(moved)
                    self.destination.mkdir(mode=0o700)
                    inner.moved = True
                return original.read(maximum)
        with self.assertRaisesRegex(ArtifactTransportError, 'output_directory_changed'):
            fetch_artifact(MovingStream(), manifest, self.destination / 'original.json')
        self.assertEqual([], list(self.destination.iterdir()))
        # A fully published file may remain in the old directory after a late
        # ACK failure. Keep it; never delete an owner's complete artifact.
        self.assertEqual(original.getvalue(), (moved / 'original.json').read_bytes())
        self.assertEqual(['original.json'], sorted(path.name for path in moved.iterdir()))

    def test_https_adapter_original_read_is_supported_without_another_request(self):
        # Preserve every original binding while representing a synthetic HTTPS
        # callback in these fixture journals. No external HTTP/SSH/model.
        context = None
        with self.db(self.journal) as db:
            context = json.loads(db.execute('SELECT context FROM provider_executions').fetchone()[0])
            nonce = db.execute('SELECT invocation_nonce FROM provider_executions').fetchone()[0]
        context.update(action='network.https', scope={'operation': 'get', 'url': 'https://fixture.example/body'},
                       workload={'kind': 'https', 'max_bytes': 262144, 'timeout_seconds': 5})
        owner = dict(self.owner, action='network.https', transport_mode='direct', allowed_origins=['*'])
        for key in ('allowed_networks', 'allowed_ports', 'allowed_schemes'):
            owner.pop(key)
        owner_sha = self.save(self.owner_path, owner)
        with mock.patch('assistant_mesh.egress_adapter._bounded_capture', return_value=(0,
                self.body + b'\nMESH_EGRESS_METADATA:200 0')):
            result = egress_adapter.execute(dict(context, invocation_nonce=nonce), self.owner_path, owner_sha)
        self.assertEqual('completed', result['outcome'])
        from assistant_mesh.resources import _json
        encoded = _json(context, 'fixture', 65536)
        with self.db(self.journal) as db:
            db.execute('UPDATE provider_executions SET context=?,fingerprint=?,result=?',
                (encoded, hashlib.sha256(encoded.encode()).hexdigest(), _json(result, 'fixture', 8192)))
        with self.db(self.database) as db:
            plan = json.loads(db.execute('SELECT plan FROM managed_allocations').fetchone()[0])
            plan['selections'][0].update(action=context['action'], scope=context['scope'], workload=context['workload'])
            db.execute('UPDATE managed_allocations SET plan=?', (_json(plan, 'fixture', 65536),))
            evidence = dict(result['evidence'], resource_quiescent=True, result_reference=result['result_reference'],
                            adapter_handle=context['adapter_handle'], invocation_nonce=nonce)
            db.execute('UPDATE managed_dispatch SET settlement=?',
                       (_json({'outcome': 'completed', 'evidence': evidence}, 'fixture', 8192),))
        self.transport_config['bindings']['campus-cap'].update(adapter='https', owner_config_sha256=owner_sha)
        pin = self.save(self.transport_path, self.transport_config)
        self.producer = ArtifactProducer(self.transport_path, pin)
        with mock.patch('assistant_mesh.egress_adapter.execute', side_effect=AssertionError('no second request')):
            manifest, stream = self.stream()
        self.assertEqual('mesh-https-artifact/1', manifest['artifact_format'])
        self.assertEqual(self.body, base64.b64decode(json.loads(stream.getvalue())['response_base64']))


if __name__ == '__main__':
    unittest.main()
