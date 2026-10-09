"""Private fixture execution/observation; not a real model or cross-host proof."""
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.provider_runtime import ProviderRuntime
from assistant_mesh.server import API
from scripts import observe_installed_tools as observer


class FixtureClient:
    """In-process credential-role fixture, not a real authenticated transport."""
    def __init__(self, api, peer):
        self.api, self.peer = api, peer
        self.calls = []
        self.after = None

    def request(self, path, body=None):
        self.calls.append((path, copy.deepcopy(body)))
        value = self.api.dispatch('GET' if body is None else 'POST', path, body, self.peer)
        return self.after(path, body, value) if self.after else value


class InstalledToolObserverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='installed-tool-observer.', dir='/var/tmp')
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.contract_path = self.root / 'contract.json'
        self.reference_path = self.root / 'owner-reference.json'
        self.owner_config_path = self.root / 'owner.json'
        self.module_path = self.root / 'arbitrary-tool.py'
        self.journal_path = self.root / 'provider.sqlite'
        self.api = API({'database': str(self.root / 'authority.sqlite'), 'node_id': 'fixture-authority',
                        'peers': [], 'resources': {'trusted_verifiers': ['node:checker']}})
        self.api.store.clock = lambda: 1000.0
        self.api.store.heartbeat('requester', ['agent'])
        self.api.store.create_task('Fixture installed-tool observer parent', task_id='parent', required=['agent'])
        self.task = self.api.store.claim('requester')
        self.api.allocations.define_pool('operator', 'fixture-pool', {'calls': {'capacity': 2, 'unit': 'call'}})
        self.provider = FixtureClient(self.api, {'role': 'worker', 'node': 'provider'})
        self.owner = FixtureClient(self.api, {'role': 'operator'})
        self.contract = {'schema': 1, 'format': observer.CONTRACT, 'run_id': 'fixture-run',
            'authority_id': 'fixture-authority', 'provider': 'node:provider',
            'journal': str(self.journal_path), 'operations': []}
        self.reference = {'schema': 1, 'format': observer.REFERENCE, 'run_id': 'fixture-run',
            'oracle_source_sha256': hashlib.sha256(b'independent fixture oracle source metadata').hexdigest(),
            'operations': []}
        self.install_and_execute(1)
        self.write_inputs()

    def tearDown(self):
        self.temporary.cleanup()

    def private_write(self, path, value):
        path = Path(path)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf8'))
        path.chmod(0o600)
        return path

    def write_inputs(self):
        self.private_write(self.contract_path, self.contract)
        self.private_write(self.reference_path, self.reference)

    def install_and_execute(self, epoch):
        operation_id = 'operation-' + str(epoch)
        input_path = self.private_write(self.root / ('input-' + str(epoch) + '.json'), {'items': ['任意', 7, {'nested': True}]})
        artifact_path = self.root / ('artifact-' + str(epoch) + '.json')
        source = '''import hashlib
import json
import os
def invoke(context):
    with open(context['scope']['input'], encoding='utf8') as source:
        data = json.load(source)
    value = {{'items': data['items'], 'version': {epoch}, 'workload': context['workload']}}
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode('utf8')
    with open(context['scope']['output'], 'wb') as output:
        output.write(raw)
    os.chmod(context['scope']['output'], 0o600)
    return {{'outcome': 'completed', 'resource_quiescent': True,
             'result_reference': 'sha256:' + hashlib.sha256(raw).hexdigest(),
             'evidence': {{'fixture_version': {epoch}}}}}
'''.format(epoch=epoch)
        self.private_write(self.module_path, source.encode('utf8'))
        module_hash = hashlib.sha256(self.module_path.read_bytes()).hexdigest()
        manifest = {'schema': 1, 'provider': 'node:provider', 'authority_id': 'fixture-authority',
                    'journal': str(self.journal_path), 'adapters': [{
            'capability_id': 'arbitrary-installed-tool', 'handle': 'owner.arbitrary.未来',
            'version': str(epoch), 'module': str(self.module_path), 'sha256': module_hash,
            'function': 'invoke', 'capability_epoch': epoch, 'action': 'owner.arbitrary.action',
            'new_spend_minor': 0}]}
        self.private_write(self.owner_config_path, manifest)
        runtime = ProviderRuntime(self.provider, str(self.owner_config_path))
        binding = runtime.describe_bindings()[0]
        self.api.resources.advertise('node:provider', 'arbitrary-installed-tool', 'owner.future-kind',
            {'managed_adapter': binding['managed_adapter']}, expected_epoch=None if epoch == 1 else epoch - 1)
        scope = {'input': str(input_path), 'output': str(artifact_path), 'run': 'fixture-run'}
        workload = {'items': 3, 'arbitrary-shape': ['future-kind', epoch]}
        self.api.resources.observe('node:checker', 'arbitrary-installed-tool', 'items', 3, 'item',
            'Fixture retained input, not performance', epoch=epoch, verification='verified',
            observation_id='observation-' + str(epoch), evidence={'scope': scope, 'workload': workload})
        request_id = 'grant-request-' + str(epoch)
        self.api.resources.request_grant('node:requester', 'arbitrary-installed-tool', 'owner.arbitrary.action',
                                        scope, request_id=request_id)
        self.api.resources.grant('operator', request_id)
        self.api.allocations.bind_pool('operator', 'arbitrary-installed-tool', epoch, 'fixture-pool', 1,
                                      'calls', 1, 'call', expected_epoch=None if epoch == 1 else epoch - 1)
        selection = {'capability_id': 'arbitrary-installed-tool', 'epoch': epoch, 'action': 'owner.arbitrary.action',
                     'scope': scope, 'workload': workload, 'observations': [{
                         'observation_id': 'observation-' + str(epoch), 'metric': 'items',
                         'unit': 'item', 'max_age_seconds': 60}]}
        self.api.allocations.reserve('node:requester', operation_id, 'parent', self.task['epoch'],
            {'target': 'arbitrary-installed-tool', 'route': [], 'selections': [selection]}, 40)
        report = runtime.poll_once()
        self.assertEqual('settled', report['dispatch'][0]['state'])
        self.contract['operations'].append({'operation_id': operation_id, 'capability_id': 'arbitrary-installed-tool',
            'capability_epoch': epoch, 'task_id': 'parent', 'task_epoch': self.task['epoch'], 'leader_epoch': None,
            'requester': 'node:requester', 'scope': scope, 'workload': workload,
            'adapter_handle': binding['adapter_handle'], 'managed_adapter': binding['managed_adapter'],
            'artifact': {'path': str(artifact_path), 'sha256': hashlib.sha256(artifact_path.read_bytes()).hexdigest()}})
        expected = {'version': epoch, 'workload': workload, 'items': ['任意', 7, {'nested': True}]}
        self.reference['operations'].append({'operation_id': operation_id, 'capability_id': 'arbitrary-installed-tool',
            'expected_canonical_artifact_sha256': observer.canonical_sha256(expected)})

    def observe(self):
        return observer.observe(self.contract_path, self.reference_path, self.owner)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                for path in self.root.rglob('*') if path.is_file()}

    def update_journal(self, sql, values=()):
        with contextlib.closing(sqlite3.connect(str(self.journal_path))) as db, db:
            db.execute(sql, values)

    def test_arbitrary_installed_descriptor_and_actual_artifact_are_verified_without_mutation(self):
        before = self.snapshot()
        with patch('assistant_mesh.provider.ManagedProvider.__init__', side_effect=AssertionError('DO_NOT_CONSTRUCT')):
            report = self.observe()
        self.assertEqual(before, self.snapshot())
        self.assertTrue(report['all_declared_results_and_settlements_verified'])
        row = report['operations'][0]
        self.assertTrue(row['actual_artifact_bytes_match_owner_reference'])
        self.assertTrue(row['recorded_result_binds_actual_artifact'])
        self.assertTrue(row['registry_binding_verified'])
        self.assertFalse(row['registry_binding_superseded'])
        self.assertTrue(row['authority_settlement_confirmed'])
        self.assertTrue(row['authority_capacity_release_confirmed'])
        self.assertNotIn(str(self.root), json.dumps(report))
        self.assertEqual(['/v1/mesh/hello', '/v1/allocation/action',
                          '/v1/resource?id=arbitrary-installed-tool&include_unavailable=1'],
                         [path for path, body in self.owner.calls])
        self.assertEqual('inspect', self.owner.calls[1][1]['action'])
        for name in ('independent_performance_verified', 'independent_physical_host_verified',
                     'resource_quiescence_independently_verified', 'native_model_provenance_attested',
                     'oracle_code_executed', 'journal_or_runtime_created', 'models_or_services_started_by_observer'):
            self.assertIs(False, report[name])

    def test_v1_before_upgrade_then_shared_v1_v2_journal_distinguishes_superseded_binding(self):
        first = self.observe()
        self.assertTrue(first['operations'][0]['registry_binding_verified'])
        self.install_and_execute(2)
        self.write_inputs()
        before = self.snapshot()
        report = self.observe()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(2, report['operation_count'])
        self.assertTrue(report['all_declared_results_and_settlements_verified'])
        self.assertTrue(report['operations'][0]['registry_binding_superseded'])
        self.assertFalse(report['operations'][0]['registry_binding_verified'])
        self.assertFalse(report['operations'][0]['historical_advertisement_independently_verified'])
        self.assertTrue(report['operations'][1]['registry_binding_verified'])
        self.assertEqual(1, sum(path.startswith('/v1/resource?') for path, body in self.owner.calls[-4:]))

    def test_owner_reference_mismatch_is_separate_from_settlement(self):
        self.reference['operations'][0]['expected_canonical_artifact_sha256'] = '0' * 64
        self.write_inputs()
        row = self.observe()['operations'][0]
        self.assertFalse(row['actual_artifact_bytes_match_owner_reference'])
        self.assertTrue(row['recorded_result_binds_actual_artifact'])
        self.assertTrue(row['authority_settlement_confirmed'])
        self.assertTrue(row['authority_capacity_release_confirmed'])

    def test_actual_result_can_be_observed_before_local_settlement_without_claiming_local_completion(self):
        self.update_journal("UPDATE provider_executions SET state='settlement_unknown',authority_epoch=authority_epoch-1,error='authority_settlement_unknown'")
        report = self.observe()
        self.assertFalse(report['all_declared_results_and_settlements_verified'])
        self.assertTrue(report['operations'][0]['authority_settlement_confirmed'])
        self.assertFalse(report['operations'][0]['local_settlement_recorded'])

    def test_unknown_result_is_not_inferred_from_correct_artifact_or_capacity_release(self):
        self.update_journal("UPDATE provider_executions SET state='unknown',result=NULL,error='adapter_outcome_unknown'")
        report = self.observe()
        row = report['operations'][0]
        self.assertFalse(report['all_declared_results_and_settlements_verified'])
        self.assertTrue(row['actual_artifact_bytes_match_owner_reference'])
        self.assertFalse(row['recorded_result_binds_actual_artifact'])
        self.assertFalse(row['authority_settlement_confirmed'])
        self.assertFalse(row['authority_capacity_release_confirmed'])

    def test_native_metadata_is_source_bound_but_not_model_attestation(self):
        self.contract['native_provenance'] = [{'task_id': 'fixture-native-task', 'thread_id': 'fixture-thread',
            'turn_id': 'fixture-turn', 'source_sha256': self.contract['operations'][0]['managed_adapter']['sha256']}]
        self.write_inputs()
        report = self.observe()
        self.assertEqual(self.contract['native_provenance'], report['native_provenance_recorded'])
        self.assertFalse(report['native_model_provenance_attested'])
        self.contract['native_provenance'][0]['source_sha256'] = '0' * 64
        self.write_inputs()
        with self.assertRaisesRegex(observer.ObservationError, 'native_source_unbound'):
            self.observe()

    def test_wrong_binding_and_descriptor_fail_before_remote_reads(self):
        original = copy.deepcopy(self.contract)
        for field, value in [('sha256', '0' * 64), ('function', 'other'), ('version', '2'),
                             ('module_path_sha256', '0' * 64), ('action', 'other-action'), ('handle', 'other-handle')]:
            with self.subTest(field=field):
                self.contract = copy.deepcopy(original)
                self.contract['operations'][0]['managed_adapter'][field] = value
                self.write_inputs()
                with self.assertRaisesRegex(observer.ObservationError, 'binding_handle_mismatch'):
                    self.observe()
        self.assertEqual([], self.owner.calls)

    def test_wrong_journal_context_fields_and_fingerprint_are_rejected(self):
        with contextlib.closing(sqlite3.connect(str(self.journal_path))) as db:
            original = db.execute('SELECT context,fingerprint FROM provider_executions').fetchone()
        for field, value in [('operation_id', 'other'), ('capability_id', 'other'), ('capability_epoch', 2),
                             ('task_epoch', 2), ('scope', {'other': True}), ('workload', {'other': 1}),
                             ('adapter_handle', 'other'), ('provider', 'node:other'), ('receipt_id', 'other')]:
            with self.subTest(field=field):
                changed = json.loads(original[0])
                changed[field] = value
                self.update_journal('UPDATE provider_executions SET context=?,fingerprint=?',
                    (json.dumps(changed), observer._digest(changed)))
                with self.assertRaisesRegex(observer.ObservationError, 'journal_context_mismatch'):
                    self.observe()
        self.update_journal('UPDATE provider_executions SET context=?,fingerprint=?', (original[0], '0' * 64))
        with self.assertRaisesRegex(observer.ObservationError, 'journal_context_mismatch'):
            self.observe()

    def test_missing_or_extra_journal_operation_is_rejected(self):
        self.update_journal("UPDATE provider_executions SET operation_id='unexpected'")
        with self.assertRaisesRegex(observer.ObservationError, 'journal_operation_set_mismatch'):
            self.observe()

    def test_bool_cannot_impersonate_integer_journal_epoch_even_with_expected_fingerprint(self):
        operation = self.contract['operations'][0]
        context = observer.expected_context(self.contract, operation)
        context['task_epoch'] = True
        self.update_journal('UPDATE provider_executions SET context=?,fingerprint=?',
            (json.dumps(context), observer._digest(observer.expected_context(self.contract, operation))))
        with self.assertRaisesRegex(observer.ObservationError, 'journal_context_mismatch'):
            self.observe()

    def test_valid_arbitrary_string_scope_is_not_an_observer_whitelist(self):
        operation = self.contract['operations'][0]
        operation['scope'] = 'future-arbitrary-scope'
        self.write_inputs()
        context = observer.expected_context(self.contract, operation)
        self.update_journal('UPDATE provider_executions SET context=?,fingerprint=?',
                            (json.dumps(context), observer._digest(context)))
        def changed(path, body, value):
            if path == '/v1/allocation/action':
                value['plan']['selections'][0]['scope'] = operation['scope']
            return value
        self.owner.after = changed
        self.assertTrue(self.observe()['all_declared_results_and_settlements_verified'])

    def test_namespace_change_or_corrupt_journal_is_rejected_without_recreation(self):
        self.update_journal("UPDATE provider_metadata SET value='node:other' WHERE key='provider'")
        with self.assertRaisesRegex(observer.ObservationError, 'journal_namespace_mismatch'):
            self.observe()
        self.private_write(self.journal_path, b'not SQLite DO_NOT_PRINT')
        before = self.snapshot()
        with self.assertRaisesRegex(observer.ObservationError, 'journal_invalid'):
            self.observe()
        self.assertEqual(before, self.snapshot())

    def test_open_wal_shm_and_rollback_sidecars_are_rejected_without_checkpoint(self):
        for suffix in ('-wal', '-shm', '-journal'):
            with self.subTest(suffix=suffix):
                sidecar = self.private_write(Path(str(self.journal_path) + suffix), b'private fixture sidecar')
                before = self.snapshot()
                with self.assertRaisesRegex(observer.ObservationError, 'closed_journal_required'):
                    self.observe()
                self.assertEqual(before, self.snapshot())
                sidecar.unlink()

    def test_active_operation_lock_is_not_bypassed_or_recreated(self):
        operation = self.contract['operations'][0]
        lock = self.private_write(Path(str(self.journal_path) + '.lock-' +
            observer._digest([operation['operation_id'], operation['capability_id']])), b'')
        with lock.open('rb') as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.snapshot()
            with self.assertRaisesRegex(observer.ObservationError, 'execution_active'):
                self.observe()
            self.assertEqual(before, self.snapshot())

    def test_missing_journal_is_not_created(self):
        self.journal_path.unlink()
        before = self.snapshot()
        with self.assertRaises(Exception):
            self.observe()
        self.assertFalse(self.journal_path.exists())
        self.assertEqual(before, self.snapshot())

    def test_same_epoch_wrong_registry_binding_wrong_principal_and_epoch_fail_closed(self):
        def changed(field, replacement):
            def hook(path, body, value):
                if path.startswith('/v1/resource?'):
                    value[field] = replacement
                return value
            return hook
        for field, value, error in [('principal', 'node:other', 'registry_identity_mismatch'),
                                    ('epoch', 0, 'registry_identity_mismatch'),
                                    ('epoch', True, 'registry_identity_mismatch'),
                                    ('spec', {'managed_adapter': {}}, 'registry_binding_mismatch')]:
            with self.subTest(field=field, value=value):
                self.owner.after = changed(field, value)
                with self.assertRaisesRegex(observer.ObservationError, error):
                    self.observe()

    def test_wrong_authenticated_hello_authority_or_protocol_is_rejected(self):
        for field, value in [('node', 'other'), ('authority', 'other'), ('protocol', 'other')]:
            with self.subTest(field=field):
                def hook(path, body, result):
                    if path == '/v1/mesh/hello':
                        result[field] = value
                    return result
                self.owner.after = hook
                with self.assertRaisesRegex(observer.ObservationError, 'authority_identity_mismatch'):
                    self.observe()

    def test_wrong_authority_task_actor_leader_selection_and_receipt_are_rejected(self):
        def hook_for(field, replacement):
            def hook(path, body, value):
                if path == '/v1/allocation/action':
                    if field == 'receipt_id':
                        value['dispatch'][0][field] = replacement
                    elif field == 'selection':
                        value['plan']['selections'][0]['epoch'] = replacement
                    else:
                        value[field] = replacement
                return value
            return hook
        for field, value in [('task_id', 'other'), ('task_epoch', 2), ('task_epoch', True),
                             ('actor', 'node:other'), ('leader_epoch', 2), ('selection', 2), ('receipt_id', 'other')]:
            with self.subTest(field=field):
                self.owner.after = hook_for(field, value)
                with self.assertRaises(observer.ObservationError):
                    self.observe()

    def test_settlement_mismatch_or_capacity_hold_does_not_become_reference_failure(self):
        for field in ('settlement', 'capacity_held'):
            with self.subTest(field=field):
                def hook(path, body, value):
                    if path == '/v1/allocation/action':
                        if field == 'settlement':
                            value['dispatch'][0]['settlement']['evidence']['fixture_version'] = 99
                        else:
                            value['capacity_held'] = True
                    return value
                self.owner.after = hook
                report = self.observe()
                self.assertFalse(report['all_declared_results_and_settlements_verified'])
                self.assertTrue(report['operations'][0]['actual_artifact_bytes_match_owner_reference'])
                self.assertFalse(report['operations'][0]['authority_capacity_release_confirmed'])

    def test_wrong_artifact_bytes_or_result_reference_are_rejected(self):
        artifact = Path(self.contract['operations'][0]['artifact']['path'])
        original = artifact.read_bytes()
        self.private_write(artifact, b'{"wrong":true}')
        with self.assertRaisesRegex(observer.ObservationError, 'artifact_raw_sha_mismatch'):
            self.observe()
        self.private_write(artifact, original)
        with contextlib.closing(sqlite3.connect(str(self.journal_path))) as db:
            result = json.loads(db.execute('SELECT result FROM provider_executions').fetchone()[0])
        result['result_reference'] = 'sha256:' + '0' * 64
        self.update_journal('UPDATE provider_executions SET result=?', (json.dumps(result),))
        with self.assertRaisesRegex(observer.ObservationError, 'result_artifact_binding_mismatch'):
            self.observe()

    def test_private_symlink_hardlink_modes_git_ancestry_and_traversal_are_rejected(self):
        original = self.contract_path.read_bytes()
        self.contract_path.chmod(0o644)
        with self.assertRaises(observer.ObservationError):
            self.observe()
        self.contract_path.chmod(0o600)
        link = self.root / 'contract-alias.json'
        os.link(str(self.contract_path), str(link))
        with self.assertRaises(observer.ObservationError):
            self.observe()
        link.unlink()
        self.contract_path.rename(self.root / 'real-contract.json')
        self.contract_path.symlink_to(self.root / 'real-contract.json')
        with self.assertRaises(observer.ObservationError):
            self.observe()
        self.contract_path.unlink()
        self.private_write(self.contract_path, original)
        marker = self.root / '.git'
        marker.symlink_to(self.root / 'missing-git')
        with self.assertRaises(observer.ObservationError):
            self.observe()
        marker.unlink()
        with self.assertRaises(observer.ObservationError):
            observer.private_bytes(self.root / '..' / self.root.name / 'contract.json', 1024 * 1024)

    def test_strict_json_rejects_duplicate_nonfinite_overflow_and_surrogate_values(self):
        for raw in (b'{"n":1,"n":2}', b'{"n":NaN}', b'{"n":Infinity}', b'{"n":1e999}', b'{"n":"\\ud800"}'):
            with self.subTest(raw=raw):
                with self.assertRaises(observer.ObservationError):
                    observer.strict_json(raw)

    def test_reference_missing_extra_duplicate_operations_or_wrong_run_are_rejected(self):
        original = copy.deepcopy(self.reference)
        for mutation in ('missing', 'extra', 'duplicate', 'run'):
            with self.subTest(mutation=mutation):
                self.reference = copy.deepcopy(original)
                if mutation == 'missing':
                    self.reference['operations'] = []
                elif mutation == 'extra':
                    row = copy.deepcopy(self.reference['operations'][0])
                    row['operation_id'] = 'extra'
                    self.reference['operations'].append(row)
                elif mutation == 'duplicate':
                    self.reference['operations'].append(copy.deepcopy(self.reference['operations'][0]))
                else:
                    self.reference['run_id'] = 'other'
                self.write_inputs()
                with self.assertRaises(observer.ObservationError):
                    self.observe()

    def test_unsupported_old_sqlite_does_not_create_sidecars(self):
        before = self.snapshot()
        with patch.object(observer.sqlite3, 'sqlite_version_info', (3, 7, 17)):
            with self.assertRaisesRegex(observer.ObservationError, 'immutable_reader_required'):
                self.observe()
        self.assertEqual(before, self.snapshot())

    def test_cli_failure_does_not_echo_credential_path_or_remote_error_body(self):
        config = self.private_write(self.root / 'client.json', {'control_url': 'http://127.0.0.1:1',
                                                               'token_file': str(self.root / 'DO_NOT_PRINT.token')})
        output = io.StringIO()
        with patch.object(observer, 'Client', side_effect=ValueError('DO_NOT_PRINT_CREDENTIAL')), contextlib.redirect_stdout(output):
            code = observer.main(['--contract', str(self.contract_path), '--reference', str(self.reference_path), '--config', str(config)])
        self.assertEqual(1, code)
        self.assertNotIn('DO_NOT_PRINT', output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_cli_report_is_only_explicit_private_immutable_publication(self):
        config = self.private_write(self.root / 'client.json', {'control_url': 'http://127.0.0.1:1', 'token_file': 'fixture-unused'})
        arguments = ['--contract', str(self.contract_path), '--reference', str(self.reference_path), '--config', str(config)]
        before = self.snapshot()
        with patch.object(observer, 'Client', return_value=self.owner), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, observer.main(arguments))
        self.assertEqual(before, self.snapshot())
        report_path = self.root / 'report.json'
        with patch.object(observer, 'Client', return_value=self.owner), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, observer.main(arguments + ['--report', str(report_path)]))
        self.assertEqual(0o600, report_path.stat().st_mode & 0o777)
        self.assertTrue(json.loads(report_path.read_text())['all_declared_results_and_settlements_verified'])
        self.private_write(report_path, b'original retained report')
        with patch.object(observer, 'Client', return_value=self.owner), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, observer.main(arguments + ['--report', str(report_path)]))
        self.assertEqual(b'original retained report', report_path.read_bytes())


if __name__ == '__main__':
    unittest.main()
