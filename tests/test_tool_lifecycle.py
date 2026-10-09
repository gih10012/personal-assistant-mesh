"""Local author/install/publish fixtures, not model-authored acceptance proof."""
import copy
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest import mock

from assistant_mesh.provider import ManagedProvider
from assistant_mesh.provider_runtime import ProviderRuntime
from assistant_mesh.server import API, serve
from assistant_mesh.tool_lifecycle import ToolLifecycle, ToolLifecycleError
from assistant_mesh.worker import Client


class LifecycleClient:
    """Same public response/error redaction as the actual server."""
    def __init__(self, api):
        self.api, self.calls = api, []
        self.before, self.after = None, None

    def request(self, path, body=None):
        self.calls.append((path, copy.deepcopy(body)))
        if self.before:
            self.before(path, body)
        try:
            value = self.api.dispatch('GET' if body is None else 'POST', path, body,
                                      {'role': 'worker', 'node': 'builder'})
        except (ValueError, KeyError, TypeError):
            raise urllib.error.HTTPError('private-url', 400, 'Bad Request', {},
                                         io.BytesIO(b'{"error":"invalid_request"}')) from None
        return self.after(path, body, value) if self.after else value

    def posts(self):
        return [body for path, body in self.calls if path == '/v1/resource/action']


class ToolLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-local-lifecycle.')
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.owner_path = self.root / 'owner.json'
        self.candidate_path = self.root / 'candidate.json'
        self.publication_path = self.root / 'publication.json'
        self.module = self.root / 'candidate.py'
        self.report = self.root / 'test-report.json'
        self.marker = self.root / 'module-executed'
        self.journal = self.root / 'provider.sqlite'
        self.owner = {'schema': 1, 'provider': 'node:builder', 'authority_id': 'authority',
                      'journal': str(self.journal), 'adapters': []}
        self.write(self.owner_path, self.owner)
        self.api = API({'node_id': 'authority', 'database': str(self.root / 'authority.sqlite'), 'peers': []})
        self.client = LifecycleClient(self.api)
        self.lifecycle = ToolLifecycle(self.client, self.owner_path, self.state)
        self.candidate(1, 'v1')

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def write(path, value):
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode('utf8'))
        path.chmod(0o600)

    def candidate(self, epoch, version, source=None):
        if source is None:
            source = ("from pathlib import Path\nPath({!r}).write_text('executed')\n"
                      "def invoke(context):\n"
                      "    return {{'outcome':'completed','resource_quiescent':True,"
                      "'result_reference':'fixture-{}'}}\n").format(str(self.marker), version).encode('utf8')
        self.write(self.module, source)
        self.write(self.report, {'status': 'passed', 'version': version, 'test_name': 'fixture self-test reference'})
        self.manifest = dict(self.owner, adapters=[{
            'capability_id': 'future-tool', 'handle': 'open.tool', 'version': version,
            'module': str(self.module), 'sha256': hashlib.sha256(source).hexdigest(), 'function': 'invoke',
            'capability_epoch': epoch, 'action': 'owner.future.action', 'new_spend_minor': 0}])
        self.publication = {'kind': 'arbitrary.future.kind', 'description': 'A locally authored capability',
                            'spec': {'owner-chosen': ['arbitrary', 17]},
                            'test_reference': {'path': str(self.report),
                                               'sha256': hashlib.sha256(self.report.read_bytes()).hexdigest()}}
        self.write(self.candidate_path, self.manifest)
        self.write(self.publication_path, self.publication)

    def stage(self, operation='deploy'):
        return self.lifecycle.stage(operation, self.candidate_path, self.publication_path)

    def deployed(self, operation='deploy'):
        self.stage(operation)
        self.assertEqual('published', self.lifecycle.publish(operation)['state'])
        self.assertEqual('manifest_activated', self.lifecycle.activate(operation)['state'])

    def record(self, operation='deploy'):
        with closing(sqlite3.connect(self.lifecycle.path)) as db:
            return json.loads(db.execute('SELECT record FROM tool_deployments WHERE operation_id=?', (operation,)).fetchone()[0])

    def test_stage_is_offline_snapshots_bytes_compiles_without_exec_and_no_provider_journal(self):
        self.client.before = lambda *args: self.fail('stage contacted authority')
        value = self.stage()
        self.assertEqual('staged', value['state'])
        self.assertFalse(value['publication_attempted'])
        self.assertFalse(value['execution_verified'])
        self.assertFalse(value['performance_verified'])
        self.assertEqual('self_tested', value['test_verification'])
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.journal.exists())
        record = self.record()
        self.assertEqual(self.module.read_bytes(), Path(record['entry']['module']).read_bytes())
        self.assertNotEqual(str(self.module), record['entry']['module'])
        self.assertEqual(0o600, Path(record['entry']['module']).stat().st_mode & 0o777)
        self.assertEqual(0o700, Path(record['release']).stat().st_mode & 0o777)
        self.write(self.module, b'def invoke(context):\n    raise RuntimeError("changed")\n')
        self.assertEqual(value['managed_adapter'], self.lifecycle.inspect('deploy')['managed_adapter'])

    def test_publication_and_manifest_activation_are_distinct_without_service_or_native_changes(self):
        original = self.owner_path.read_bytes()
        self.stage()
        value = self.lifecycle.publish('deploy')
        self.assertEqual('published', value['state'])
        self.assertEqual(original, self.owner_path.read_bytes())
        self.assertFalse(value['manifest_activated'])
        activated = self.lifecycle.activate('deploy')
        self.assertTrue(activated['manifest_activated'])
        self.assertTrue(activated['runtime_reload_required'])
        self.assertFalse(activated['runtime_loaded_verified'])
        self.assertFalse(activated['provider_journal_modified'])
        self.assertFalse(activated['native_tools_intercepted'])
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.journal.exists())
        active = json.loads(self.owner_path.read_text())
        self.assertEqual(str(self.journal), active['journal'])
        self.assertEqual(1, len(active['adapters']))
        self.assertEqual('future-tool', active['adapters'][0]['capability_id'])
        self.assertEqual(original, (Path(self.record()['release']) / 'owner-before.json').read_bytes())
        self.assertEqual('declared', self.api.resources.describe('future-tool')['verification'])

    def test_actual_runtime_only_executes_after_fresh_managed_admission(self):
        self.deployed()
        runtime = ProviderRuntime(self.client, self.owner_path)
        self.assertFalse(self.marker.exists())
        self.api.store.heartbeat('builder', ['agent'])
        self.api.store.create_task('test admitted tool', required=['agent'], task_id='task')
        task = self.api.store.claim('builder')
        self.api.allocations.define_pool('operator', 'local-calls', {'calls': {'capacity': 1, 'unit': 'call'}})
        self.api.allocations.bind_pool('operator', 'future-tool', 1, 'local-calls', 1, 'calls', 1, 'call')
        self.api.resources.trusted_verifiers = frozenset({'node:checker'})
        self.api.resources.observe('node:checker', 'future-tool', 'quality', 1, 'fixture', 'fixture verifier',
                                   epoch=1, verification='verified', observation_id='observation',
                                   evidence={'scope': {'fixture': True}, 'workload': {'fixture': 'local'}})
        self.api.allocations.reserve('node:builder', 'actual-operation', 'task', task['epoch'], {
            'target': 'future-tool', 'route': [], 'selections': [{
                'capability_id': 'future-tool', 'epoch': 1, 'action': 'owner.future.action',
                'scope': {'fixture': True}, 'workload': {'fixture': 'local'},
                'observations': [{'observation_id': 'observation', 'metric': 'quality', 'unit': 'fixture',
                                  'max_age_seconds': 60}]}]})
        value = runtime.poll_once()
        self.assertEqual('settled', value['dispatch'][0]['state'])
        self.assertTrue(self.marker.exists())
        self.assertFalse(value['execution_verified'])
        self.assertEqual(1, len(self.client.posts()))

    def test_same_operation_is_idempotent_but_changed_candidate_or_reference_conflicts(self):
        first = self.stage()
        self.assertEqual(first, self.stage())
        self.candidate(1, 'different-version')
        with self.assertRaisesRegex(ToolLifecycleError, '^lifecycle_operation_conflict$'):
            self.stage()
        self.assertEqual(1, len(list(self.state.glob('release-*'))))

    def test_reply_loss_after_commit_cold_restart_confirms_same_epoch_with_one_post(self):
        self.stage()
        def lost(path, body, value):
            if path == '/v1/resource/action':
                raise urllib.error.URLError('DO_NOT_PRINT private token')
            return value
        self.client.after = lost
        value = self.lifecycle.publish('deploy')
        self.assertEqual('unknown', value['state'])
        self.assertNotIn('DO_NOT_PRINT', json.dumps(value))
        self.client.after = None
        restarted = ToolLifecycle(self.client, self.owner_path, self.state)
        self.assertEqual('published', restarted.publish('deploy')['state'])
        self.assertEqual(1, len(self.client.posts()))
        self.assertEqual(1, self.api.resources.describe('future-tool')['epoch'])

    def test_lost_request_before_remote_commit_never_reposts_even_when_proven_absent(self):
        self.stage()
        def lost(path, body):
            if path == '/v1/resource/action':
                raise ConnectionError('DO_NOT_PRINT')
        self.client.before = lost
        self.assertEqual('unknown', self.lifecycle.publish('deploy')['state'])
        self.client.before = None
        restarted = ToolLifecycle(self.client, self.owner_path, self.state)
        for _ in range(3):
            self.assertEqual('unknown', restarted.publish('deploy')['state'])
        self.assertEqual(1, len(self.client.posts()))
        self.assertEqual([], self.api.resources.discover()['capabilities'])

    def test_crash_after_durable_intent_before_post_is_query_only_on_restart(self):
        self.stage()
        original_set = self.lifecycle._set
        def crash(operation, state, **kwargs):
            original_set(operation, state, **kwargs)
            if state == 'publication_intent':
                raise SystemExit('crash fixture')
        with mock.patch.object(self.lifecycle, '_set', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.lifecycle.publish('deploy')
        restarted = ToolLifecycle(self.client, self.owner_path, self.state)
        self.assertEqual('unknown', restarted.inspect('deploy')['state'])
        self.assertEqual('unknown', restarted.publish('deploy')['state'])
        self.assertEqual([], self.client.posts())

    def test_lost_readback_after_successful_post_requires_authenticated_query_confirmation(self):
        self.stage()
        posted = [False]
        def unavailable(path, body):
            if path == '/v1/resource/action':
                posted[0] = True
            elif posted[0] and path == '/v1/mesh/hello':
                raise TimeoutError('private details')
        self.client.before = unavailable
        self.assertEqual('unknown', self.lifecycle.publish('deploy')['state'])
        self.client.before = None
        self.assertEqual('published', self.lifecycle.publish('deploy')['state'])
        self.assertEqual(1, len(self.client.posts()))

    def test_authority_or_principal_or_predecessor_mismatch_never_publishes(self):
        self.stage()
        def wrong_authority(path, body, value):
            return dict(value, authority='other-authority') if path == '/v1/mesh/hello' else value
        self.client.after = wrong_authority
        with self.assertRaisesRegex(ToolLifecycleError, '^publication_authority_mismatch$'):
            self.lifecycle.publish('deploy')
        self.assertEqual([], self.client.posts())
        self.client.after = None
        self.api.resources.advertise('node:other', 'future-tool', 'other-kind')
        with self.assertRaisesRegex(ToolLifecycleError, '^publication_predecessor_mismatch$'):
            self.lifecycle.publish('deploy')
        self.assertEqual([], self.client.posts())

    def test_partial_activation_before_replace_restores_original_and_resumes_same_intent(self):
        self.stage()
        self.lifecycle.publish('deploy')
        original = self.owner_path.read_bytes()
        with mock.patch.object(self.lifecycle, '_replace_manifest', side_effect=OSError('private path')):
            with self.assertRaisesRegex(ToolLifecycleError, '^tool_lifecycle_failed$'):
                self.lifecycle.activate('deploy')
        self.assertEqual(original, self.owner_path.read_bytes())
        restarted = ToolLifecycle(self.client, self.owner_path, self.state)
        self.assertEqual('manifest_activated', restarted.activate('deploy')['state'])
        self.assertEqual(original, (Path(self.record()['release']) / 'owner-before.json').read_bytes())
        self.assertEqual(1, len(self.client.posts()))

    def test_crash_after_manifest_replace_reconciles_without_replacement_or_extra_post(self):
        self.stage()
        self.lifecycle.publish('deploy')
        original_replace = self.lifecycle._replace_manifest
        def crash(desired, expected):
            original_replace(desired, expected)
            raise SystemExit('crash fixture')
        with mock.patch.object(self.lifecycle, '_replace_manifest', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.lifecycle.activate('deploy')
        active = self.owner_path.read_bytes()
        restarted = ToolLifecycle(self.client, self.owner_path, self.state)
        with mock.patch.object(restarted, '_replace_manifest', side_effect=AssertionError('should not replace again')):
            self.assertEqual('manifest_activated', restarted.activate('deploy')['state'])
        self.assertEqual(active, self.owner_path.read_bytes())
        self.assertEqual(1, len(self.client.posts()))

    def test_changed_owner_or_authority_between_publication_activation_is_not_overwritten(self):
        self.stage()
        self.lifecycle.publish('deploy')
        self.api.resources.advertise('node:builder', 'future-tool', 'another-tool', expected_epoch=1)
        original = self.owner_path.read_bytes()
        with self.assertRaisesRegex(ToolLifecycleError, '^activation_publication_changed$'):
            self.lifecycle.activate('deploy')
        self.assertEqual(original, self.owner_path.read_bytes())
        self.owner['journal'] = str(self.root / 'other-journal.sqlite')
        self.write(self.owner_path, self.owner)
        with self.assertRaisesRegex(ToolLifecycleError, '^owner_identity_changed$'):
            self.lifecycle.inspect('deploy')
        with self.assertRaisesRegex(ToolLifecycleError, '^lifecycle_journal_identity_mismatch$'):
            ToolLifecycle(self.client, self.owner_path, self.state)

    def test_preserves_other_owner_adapters_and_unknown_actual_execution_journal_bytes(self):
        bridge = ManagedProvider(self.client, self.journal, 'node:builder', {}, 'authority')
        with bridge._db() as db:
            db.execute('INSERT INTO provider_executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('old', 'unknown-tool', 'receipt', 'fingerprint', '{}', 'unknown', 2, 'nonce',
                        None, 'adapter_outcome_unknown', 1.0, 1.0))
        original_journal = self.journal.read_bytes()
        other = dict(self.manifest['adapters'][0], capability_id='another-tool')
        self.owner['adapters'] = [other]
        self.write(self.owner_path, self.owner)
        self.deployed()
        self.assertEqual(original_journal, self.journal.read_bytes())
        with closing(sqlite3.connect(str(self.journal))) as db:
            self.assertEqual(('unknown', 'nonce'), db.execute('SELECT state,invocation_nonce FROM provider_executions').fetchone())
        active = json.loads(self.owner_path.read_text())
        self.assertEqual(other, active['adapters'][0])
        self.assertEqual(str(self.journal), active['journal'])

    def test_upgrade_and_rollback_use_new_epochs_retained_bytes_and_new_binding_locations(self):
        self.deployed('version-one')
        first = self.record('version-one')
        self.candidate(2, 'v2')
        self.deployed('version-two')
        self.assertEqual(2, self.api.resources.describe('future-tool')['epoch'])
        historical = self.lifecycle.inspect('version-one')
        self.assertEqual('manifest_activated', historical['state'])
        self.assertTrue(historical['activation_recorded'])
        self.assertFalse(historical['manifest_binding_current'])
        self.assertFalse(historical['manifest_activated'])
        self.assertFalse(historical['runtime_reload_required'])
        self.assertFalse(self.lifecycle.publish('version-one')['manifest_binding_current'])
        self.write(self.module, b'def invoke(context):\n    return {}\n')
        value = self.lifecycle.prepare_rollback('rollback', 'version-one', 3)
        self.assertEqual('staged', value['state'])
        self.assertEqual('version-one', value['rollback_of'])
        self.assertEqual(first['entry']['sha256'], value['managed_adapter']['sha256'])
        self.assertNotEqual(first['publication']['spec']['managed_adapter']['module_path_sha256'],
                            value['managed_adapter']['module_path_sha256'])
        self.assertEqual('published', self.lifecycle.publish('rollback')['state'])
        self.assertEqual('manifest_activated', self.lifecycle.activate('rollback')['state'])
        active = json.loads(self.owner_path.read_text())['adapters'][0]
        self.assertEqual('v1', active['version'])
        self.assertEqual(3, active['capability_epoch'])
        self.assertEqual(3, self.api.resources.describe('future-tool')['epoch'])
        with self.assertRaisesRegex(ToolLifecycleError, '^rollback_requires_new_epoch$'):
            self.lifecycle.prepare_rollback('wrong', 'version-two', 2)
        self.assertFalse(self.marker.exists())

    def test_tampered_retained_module_report_or_manifest_rejected_before_publication(self):
        self.stage()
        record = self.record()
        source = Path(record['entry']['module'])
        original = source.read_bytes()
        self.write(source, b'def invoke(context):\n    return {}\n')
        with self.assertRaises(ToolLifecycleError):
            self.lifecycle.publish('deploy')
        self.write(source, original)
        self.write(Path(record['release']) / 'test-report', b'changed test evidence')
        with self.assertRaisesRegex(ToolLifecycleError, '^retained_release_invalid$'):
            self.lifecycle.publish('deploy')
        self.assertEqual([], self.client.posts())

    def test_invalid_private_publication_dupkeys_sensitive_spec_and_test_digest_never_stage(self):
        cases = [dict(self.publication, token='private'),
                 dict(self.publication, spec={'secret': 'do not publish'}),
                 dict(self.publication, spec={'url': 'https://example.org/?token=secret'}),
                 dict(self.publication, spec={'managed_adapter': {}}),
                 dict(self.publication, test_reference={'path': str(self.report), 'sha256': '0' * 64})]
        for index, value in enumerate(cases):
            self.write(self.publication_path, value)
            with self.subTest(index=index), self.assertRaisesRegex(ToolLifecycleError, '^tool_publication_invalid$'):
                self.stage('invalid-' + str(index))
        self.write(self.publication_path, b'{"kind":"a","kind":"b"}')
        with self.assertRaisesRegex(ToolLifecycleError, '^tool_publication_invalid$'):
            self.stage()
        self.assertEqual([], list(self.state.glob('release-*')))

    def test_manifest_code_epoch_or_identity_invalid_never_executes_or_initializes_provider(self):
        for field, value in (('capability_epoch', True), ('new_spend_minor', 1), ('function', 'missing')):
            self.candidate(1, 'v1')
            self.manifest['adapters'][0][field] = value
            self.write(self.candidate_path, self.manifest)
            with self.subTest(field=field), self.assertRaises(ToolLifecycleError):
                self.stage(field)
        self.candidate(1, 'v1')
        self.manifest['provider'] = 'node:other'
        self.write(self.candidate_path, self.manifest)
        with self.assertRaisesRegex(ToolLifecycleError, '^candidate_identity_invalid$'):
            self.stage()
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.journal.exists())

    def test_complete_descriptor_and_generated_metadata_private_fields_or_oversize_rejected(self):
        self.manifest['adapters'][0]['action'] = 'https://example.org/?token=DO_NOT_PRINT'
        self.write(self.candidate_path, self.manifest)
        with self.assertRaisesRegex(ToolLifecycleError, '^tool_publication_invalid$'):
            self.stage('sensitive-descriptor')
        self.candidate(1, 'v1')
        self.publication['spec'] = {'open-field': 'x' * 32500}
        self.write(self.publication_path, self.publication)
        with self.assertRaisesRegex(ToolLifecycleError, '^tool_publication_invalid$'):
            self.stage('oversize-advertisement')
        self.assertEqual([], self.client.posts())
        self.assertFalse(self.marker.exists())

    def test_symlink_hardlink_bad_permission_files_and_state_directory_rejected(self):
        self.module.chmod(0o644)
        with self.assertRaises(ToolLifecycleError):
            self.stage()
        self.module.chmod(0o600)
        link = self.root / 'linked.py'
        os.link(str(self.module), str(link))
        with self.assertRaises(ToolLifecycleError):
            self.stage()
        link.unlink()
        link.symlink_to(self.module)
        self.manifest['adapters'][0]['module'] = str(link)
        self.write(self.candidate_path, self.manifest)
        with self.assertRaises(ToolLifecycleError):
            self.stage()
        self.state.chmod(0o755)
        with self.assertRaisesRegex(ToolLifecycleError, '^lifecycle_private_directory_required$'):
            ToolLifecycle(self.client, self.owner_path, self.state)

    def test_concurrent_controller_uses_local_lock_to_avoid_duplicate_post(self):
        self.stage()
        entered, release = threading.Event(), threading.Event()
        def blocked(path, body):
            if path == '/v1/resource/action':
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('fixture lock timeout')
        self.client.before = blocked
        other = ToolLifecycle(self.client, self.owner_path, self.state)
        errors = []
        def publish():
            try:
                self.lifecycle.publish('deploy')
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=publish)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaisesRegex(ToolLifecycleError, '^lifecycle_busy$'):
                other.publish('deploy')
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, len(self.client.posts()))

    def test_unknown_operation_cannot_confirm_different_epoch_or_edited_descriptor(self):
        self.stage()
        def lost(path, body, value):
            if path == '/v1/resource/action':
                raise TimeoutError('fixture lost response')
            return value
        self.client.after = lost
        self.lifecycle.publish('deploy')
        self.client.after = None
        self.api.resources.advertise('node:builder', 'future-tool', 'replacement', expected_epoch=1)
        self.assertEqual('unknown', self.lifecycle.publish('deploy')['state'])
        self.assertEqual(1, len(self.client.posts()))
        with self.assertRaisesRegex(ToolLifecycleError, '^activation_requires_publication$'):
            self.lifecycle.activate('deploy')

    def test_directory_full_without_target_is_not_proof_of_absence(self):
        self.stage()
        def full(path, body, value):
            if path.startswith('/v1/resources?'):
                return {'capabilities': [{'id': 'cap-' + str(index)} for index in range(1000)]}
            return value
        self.client.after = full
        with self.assertRaisesRegex(ToolLifecycleError, '^publication_directory_incomplete$'):
            self.lifecycle.publish('deploy')
        self.assertEqual([], self.client.posts())

    def test_actual_authenticated_http_client_stages_publishes_and_activates(self):
        token = self.root / 'provider.token'
        self.write(token, b'fixture-token-not-real-credentials')
        ready, holder = threading.Event(), {}
        config = {'node_id': 'authority', 'database': str(self.root / 'http-authority.sqlite'), 'port': 0,
                  'peers': [{'role': 'worker', 'node': 'builder', 'token_file': str(token)}]}
        def on_ready(server, api, channel):
            holder.update(server=server, api=api, channel=channel)
            ready.set()
        thread = threading.Thread(target=serve, args=(config, on_ready), daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5))
        server = holder['server']
        try:
            client = Client({'control_url': 'http://127.0.0.1:' + str(server.server_port), 'token_file': str(token)})
            controller = ToolLifecycle(client, self.owner_path, self.state)
            self.assertEqual('staged', controller.stage('http', self.candidate_path, self.publication_path)['state'])
            self.assertEqual('published', controller.publish('http')['state'])
            self.assertEqual('manifest_activated', controller.activate('http')['state'])
            self.assertIsNone(holder['channel'])
            self.assertEqual(1, holder['api'].resources.describe('future-tool')['epoch'])
            self.assertFalse(self.marker.exists())
        finally:
            server.shutdown()
            thread.join(5)

    def test_provider_journal_aliases_rejected_before_control_or_effect_file_creation(self):
        owner_lock = self.lifecycle.owner_lock
        cases = [self.state / 'lifecycle.sqlite', self.state / 'lifecycle.lock',
                 self.state / 'release-future' / 'provider.sqlite', owner_lock, self.owner_path]
        for index, journal in enumerate(cases):
            owner = dict(self.owner, journal=str(journal))
            self.write(self.owner_path, owner)
            before = self.lifecycle.path and Path(self.lifecycle.path).read_bytes()
            with self.subTest(journal=journal.name), self.assertRaisesRegex(
                    ToolLifecycleError, '^lifecycle_provider_journal_collision$'):
                ToolLifecycle(self.client, self.owner_path, self.state)
            self.assertEqual(before, Path(self.lifecycle.path).read_bytes())
        self.assertFalse(owner_lock.exists())
        fresh = self.root / 'fresh-state'
        fresh.mkdir(mode=0o700)
        self.write(self.owner_path, dict(self.owner, journal=str(fresh / 'lifecycle.sqlite')))
        with self.assertRaisesRegex(ToolLifecycleError, '^lifecycle_provider_journal_collision$'):
            ToolLifecycle(self.client, self.owner_path, fresh)
        self.assertFalse((fresh / 'lifecycle.sqlite').exists())
        self.assertEqual([], list(fresh.iterdir()))

    def test_external_owner_edit_immediately_before_replace_is_preserved_and_fixed_error(self):
        self.stage()
        self.lifecycle.publish('deploy')
        original_replace = self.lifecycle._replace_manifest
        changed = dict(self.owner, dispatch_limit=9)
        def edit_then_replace(desired, expected):
            self.write(self.owner_path, changed)
            original_replace(desired, expected)
        with mock.patch.object(self.lifecycle, '_replace_manifest', side_effect=edit_then_replace):
            with self.assertRaisesRegex(ToolLifecycleError, '^activation_owner_changed$'):
                self.lifecycle.activate('deploy')
        self.assertEqual(changed, json.loads(self.owner_path.read_text()))
        self.assertEqual([], json.loads(self.owner_path.read_text())['adapters'])
        self.assertEqual('activation_intent', self.lifecycle.inspect('deploy')['state'])
        restarted = ToolLifecycle(self.client, self.owner_path, self.state)
        with self.assertRaisesRegex(ToolLifecycleError, '^activation_owner_changed$'):
            restarted.activate('deploy')
        self.assertEqual(changed, json.loads(self.owner_path.read_text()))

    def test_distinct_state_controllers_share_owner_lock_and_cannot_overwrite_activation(self):
        self.stage('first')
        self.lifecycle.publish('first')
        other_state = self.root / 'other-state'
        other_state.mkdir(mode=0o700)
        other = ToolLifecycle(self.client, self.owner_path, other_state)
        self.candidate(2, 'v2')
        other.stage('second', self.candidate_path, self.publication_path)
        self.assertEqual(self.lifecycle.owner_lock, other.owner_lock)
        entered, release = threading.Event(), threading.Event()
        original_replace = self.lifecycle._replace_manifest
        def blocked(desired, expected):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture activation timeout')
            original_replace(desired, expected)
        errors = []
        def activate():
            try:
                with mock.patch.object(self.lifecycle, '_replace_manifest', side_effect=blocked):
                    self.lifecycle.activate('first')
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=activate)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaisesRegex(ToolLifecycleError, '^lifecycle_busy$'):
                other.publish('second')
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, json.loads(self.owner_path.read_text())['adapters'][0]['capability_epoch'])
        self.assertEqual('published', other.publish('second')['state'])
        self.assertEqual('manifest_activated', other.activate('second')['state'])
        self.assertEqual(2, json.loads(self.owner_path.read_text())['adapters'][0]['capability_epoch'])

    def test_owner_edit_during_authenticated_readback_is_merged_not_silently_lost(self):
        self.stage()
        self.lifecycle.publish('deploy')
        def edit_during_readback(path, body, value):
            if path.startswith('/v1/resource?'):
                self.write(self.owner_path, dict(self.owner, dispatch_limit=9))
            return value
        self.client.after = edit_during_readback
        self.assertEqual('manifest_activated', self.lifecycle.activate('deploy')['state'])
        self.assertEqual(9, json.loads(self.owner_path.read_text())['dispatch_limit'])

    def test_activation_exceeding_manifest_adapter_limit_preserves_valid_original_bytes(self):
        other = dict(self.manifest['adapters'][0], module='/x', handle='h', action='a', function='f', version='1')
        self.owner['adapters'] = [dict(other, capability_id='old-' + str(index)) for index in range(256)]
        self.write(self.owner_path, self.owner)
        original = self.owner_path.read_bytes()
        self.assertLess(len(original), 65536)
        self.stage()
        self.lifecycle.publish('deploy')
        with self.assertRaisesRegex(ToolLifecycleError, '^activation_manifest_invalid$'):
            self.lifecycle.activate('deploy')
        self.assertEqual(original, self.owner_path.read_bytes())
        self.assertEqual('published', self.lifecycle.inspect('deploy')['state'])
        self.assertFalse((Path(self.record()['release']) / 'owner-before.json').exists())

    def test_activation_exceeding_manifest_byte_limit_preserves_valid_original_bytes(self):
        other = dict(self.manifest['adapters'][0], version='long-owner-version')
        # All entries are valid manifest metadata, including an arbitrarily long
        # absolute module location. No old module is opened during activation.
        other['module'] = str(self.root) + '/' + 'p' * 1200 + '.py'
        self.owner['adapters'] = [dict(other, capability_id='existing-' + str(index)) for index in range(36)]
        base = len(json.dumps(self.owner, separators=(',', ':')).encode('utf8'))
        extra = 65500 - base
        self.assertGreater(extra, 0)
        self.owner['adapters'][0]['module'] += 'x' * extra
        self.write(self.owner_path, json.dumps(self.owner, separators=(',', ':')).encode('utf8'))
        original = self.owner_path.read_bytes()
        self.assertEqual(65500, len(original))
        self.stage()
        self.lifecycle.publish('deploy')
        with self.assertRaisesRegex(ToolLifecycleError, '^activation_manifest_invalid$'):
            self.lifecycle.activate('deploy')
        self.assertEqual(original, self.owner_path.read_bytes())
        self.assertEqual('published', self.lifecycle.inspect('deploy')['state'])


if __name__ == '__main__':
    unittest.main()
