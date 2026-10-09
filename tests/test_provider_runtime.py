"""Private installed Python + polling fixtures, not deployed cross-host proof."""
import copy
import codecs
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

from assistant_mesh.provider import ManagedProvider
from assistant_mesh.provider_runtime import ProviderRuntime, ProviderRuntimeError
from assistant_mesh.server import API


class RuntimeClient:
    def __init__(self, api):
        self.api = api
        self.calls = []
        self.before = None
        self.after = None

    def request(self, path, body=None):
        self.calls.append((path, copy.deepcopy(body)))
        if self.before:
            self.before(path, body)
        value = self.api.dispatch('GET' if body is None else 'POST', path, body,
                                  {'role': 'worker', 'node': 'cloud'})
        return self.after(path, body, value) if self.after else value


class ProviderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-owner-runtime.')
        self.root = Path(self.temp.name)
        self.module = self.root / 'owned_tool.py'
        self.config_path = self.root / 'owner.json'
        self.journal = self.root / 'provider.db'
        self.counter = self.root / 'counter'
        self.import_marker = self.root / 'import-marker'
        self.manifest = {'schema': 1, 'provider': 'node:cloud', 'authority_id': 'fixture-authority',
            'journal': str(self.journal), 'poll_interval_seconds': 0.01,
            'adapters': [{'capability_id': 'arbitrary-owner-tool', 'handle': 'owner.arbitrary.tool',
                         'version': '1', 'module': str(self.module), 'sha256': '', 'function': 'invoke',
                         'capability_epoch': 1, 'action': 'owner.arbitrary.action', 'new_spend_minor': 0}]}
        self.install('v1')
        self.api = API({'database': str(self.root / 'authority.db'), 'peers': [],
                        'resources': {'trusted_verifiers': ['node:checker']}})
        self.api.store.clock = lambda: 1000.0
        self.api.store.heartbeat('laptop', ['leader', 'agent'])
        self.api.store.create_task('fixture parent', task_id='parent')
        self.task = self.api.store.claim('laptop')
        self.api.allocations.define_pool('operator', 'owned-pool', {'calls': {'capacity': 2, 'unit': 'call'}})
        self.client = RuntimeClient(self.api)
        self.runtime = ProviderRuntime(self.client, self.config_path)
        self.scope = {'counter': str(self.counter), 'nested': {'arbitrary': 'scope'}}
        self.workload = {'arbitrary': ['future-kind', 23]}
        self.publish(1)

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self):
        self.config_path.write_text(json.dumps(self.manifest))
        self.config_path.chmod(0o600)

    def install(self, version, fail=False, source=None):
        if source is None:
            source = '''from pathlib import Path
Path({marker!r}).write_text('trusted top-level effect')
def invoke(context):
    path = Path(context['scope']['counter'])
    with path.open('a') as output:
        output.write({version!r} + '\\n')
    {failure}
    return {{'outcome': 'completed', 'resource_quiescent': True,
            'result_reference': 'fixture:' + {version!r},
            'evidence': {{'version': {version!r}, 'workload': context['workload'],
                         'owner_binding': context['owner_binding']}}}}
'''.format(marker=str(self.import_marker), version=version,
           failure="raise TimeoutError('DO_NOT_PRINT unknown subprocess')" if fail else 'pass')
        self.module.write_text(source)
        self.module.chmod(0o600)
        self.manifest['adapters'][0]['sha256'] = hashlib.sha256(self.module.read_bytes()).hexdigest()
        self.write_config()

    def publish(self, epoch):
        description = self.runtime.describe_bindings()[0]['managed_adapter']
        spec = {'managed_adapter': description, 'owner_defined_kind': 'not-hardcoded'}
        self.api.resources.advertise('node:cloud', 'arbitrary-owner-tool', 'owner.future.capability', spec,
                                    expected_epoch=None if epoch == 1 else epoch - 1)
        self.api.resources.observe('node:checker', 'arbitrary-owner-tool', 'quality', 7, 'owner-unit',
            'actual fixture verifier', epoch=epoch, verification='verified', observation_id='observation-' + str(epoch),
            evidence={'scope': self.scope, 'workload': self.workload})
        request = 'grant-request-' + str(epoch)
        self.api.resources.request_grant('node:laptop', 'arbitrary-owner-tool', 'owner.arbitrary.action',
                                        self.scope, request_id=request)
        self.api.resources.grant('operator', request)
        self.api.allocations.bind_pool('operator', 'arbitrary-owner-tool', epoch, 'owned-pool', 1, 'calls', 1, 'call',
                                       expected_epoch=None if epoch == 1 else epoch - 1)

    def reserve(self, operation='operation', epoch=1):
        selection = {'capability_id': 'arbitrary-owner-tool', 'epoch': epoch, 'action': 'owner.arbitrary.action',
                     'scope': copy.deepcopy(self.scope), 'workload': copy.deepcopy(self.workload),
                     'observations': [{'observation_id': 'observation-' + str(epoch), 'metric': 'quality',
                                       'unit': 'owner-unit', 'max_age_seconds': 60}]}
        return self.api.allocations.reserve('node:laptop', operation, 'parent', self.task['epoch'],
                    {'target': 'arbitrary-owner-tool', 'route': [], 'selections': [selection]}, 40)

    def calls(self):
        return self.counter.read_text().splitlines() if self.counter.exists() else []

    def allocation_calls(self, action):
        return [body for path, body in self.client.calls if body is not None and body.get('action') == action]

    def test_real_private_module_executes_only_after_admission_and_preserves_arbitrary_context(self):
        self.assertFalse(self.import_marker.exists())
        self.reserve()
        result = self.runtime.poll_once()
        self.assertEqual('ok', result['status'])
        self.assertEqual(['v1'], self.calls())
        self.assertTrue(self.import_marker.exists())
        self.assertEqual('settled', result['dispatch'][0]['state'])
        self.assertFalse(result['execution_verified'])
        self.assertFalse(result['native_tools_intercepted'])
        descriptor = self.runtime.describe_bindings()[0]
        self.assertEqual(self.manifest['adapters'][0]['sha256'], descriptor['managed_adapter']['sha256'])
        self.assertIn('#binding-sha256:', descriptor['adapter_handle'])
        with closing(sqlite3.connect(str(self.journal))) as db:
            context = json.loads(db.execute('SELECT context FROM provider_executions').fetchone()[0])
        self.assertEqual(self.scope, context['scope'])
        self.assertEqual(self.workload, context['workload'])
        self.assertEqual('ok', self.runtime.poll_once()['status'])
        self.assertEqual(['v1'], self.calls())

    def test_module_snapshot_does_not_dynamic_import_changed_disk_source_or_model_scope(self):
        self.install('v2')
        self.reserve()
        # Old runtime owns the immutable verified v1 source and matching v1
        # advertisement. A disk edit does not silently mutate that invocation.
        self.assertEqual('settled', self.runtime.poll_once()['dispatch'][0]['state'])
        self.assertEqual(['v1'], self.calls())

    def test_model_pending_module_function_and_source_fields_do_not_select_executable_code(self):
        self.reserve()
        def model_fields(path, body, value):
            if body and body.get('action') == 'pending':
                value['dispatch'][0]['selection'].update(module='/DO_NOT_PRINT/model.py',
                    function='model_chosen', source='raise RuntimeError("DO_NOT_PRINT")',
                    adapter_handle='model-chosen-handle')
            return value
        self.client.after = model_fields
        report = self.runtime.poll_once()
        self.assertEqual('settled', report['dispatch'][0]['state'])
        self.assertEqual(['v1'], self.calls())
        self.assertNotIn('DO_NOT_PRINT', json.dumps(report))

    def test_current_advertisement_identity_availability_epoch_and_exact_binding_are_checked(self):
        self.reserve()
        invoke = self.runtime.bridge.bindings['arbitrary-owner-tool'].invoke
        original = self.api.resources.describe('arbitrary-owner-tool')
        forged = []
        for field, value in (('principal', 'node:stranger'), ('available', False), ('epoch', True), ('epoch', 2)):
            forged.append(dict(original, **{field: value}))
        for field, replacement in (('version', '2'), ('sha256', '0' * 64), ('action', 'other-action'),
                                   ('function', 'other'), ('module_path_sha256', '0' * 64), ('new_spend_minor', False)):
            value = copy.deepcopy(original)
            value['spec']['managed_adapter'][field] = replacement
            forged.append(value)
        for value in forged:
            self.client.after = lambda path, body, actual, forged=value: forged if body is None else actual
            with self.subTest(epoch=value['epoch'], principal=value['principal']), self.assertRaisesRegex(
                    ProviderRuntimeError, '^owner_advertisement_mismatch$'):
                invoke({'scope': self.scope, 'workload': self.workload})
        self.assertFalse(self.import_marker.exists())
        self.assertEqual([], self.calls())

    def test_local_new_version_without_capability_epoch_advance_is_rejected_before_module_execution(self):
        self.install('v2')
        self.manifest['adapters'][0]['version'] = '2'
        self.write_config()
        runtime = ProviderRuntime(self.client, self.config_path)
        self.reserve()
        report = runtime.poll_once()
        self.assertEqual('degraded', report['status'])
        self.assertFalse(report['recoverable'])
        self.assertIn('owner_advertisement_mismatch', report['diagnostics'])
        self.assertEqual('unknown', report['dispatch'][0]['state'])
        self.assertEqual([], self.calls())
        self.assertFalse(self.import_marker.exists())
        self.assertEqual(1, self.api.allocations.pools('operator')['pools'][0]['dimensions']['calls']['remaining'])

    def test_same_source_with_new_function_or_location_cannot_silently_serve_existing_epoch(self):
        source = self.module.read_text() + '\ndef other(context):\n    raise RuntimeError("different entrypoint")\n'
        self.install('v2', source=source)
        self.manifest['adapters'][0].update(version='2', capability_epoch=2)
        self.write_config()
        self.runtime = ProviderRuntime(self.client, self.config_path)
        self.publish(2)
        self.manifest['adapters'][0]['function'] = 'other'
        self.write_config()
        changed_function = ProviderRuntime(self.client, self.config_path)
        self.reserve('function-change', epoch=2)
        self.assertIn('owner_advertisement_mismatch', changed_function.poll_once()['diagnostics'])
        self.manifest['adapters'][0]['function'] = 'invoke'
        relocated = self.root / 'relocated.py'
        relocated.write_bytes(self.module.read_bytes())
        relocated.chmod(0o600)
        self.manifest['adapters'][0]['module'] = str(relocated)
        self.write_config()
        changed_location = ProviderRuntime(self.client, self.config_path)
        self.reserve('location-change', epoch=2)
        self.assertIn('owner_advertisement_mismatch', changed_location.poll_once()['diagnostics'])
        self.assertFalse(self.import_marker.exists())
        self.assertEqual([], self.calls())

    def test_new_version_requires_matching_advertised_advanced_epoch(self):
        self.reserve('old')
        self.assertEqual('settled', self.runtime.poll_once()['dispatch'][0]['state'])
        self.install('v2')
        self.manifest['adapters'][0].update(version='2', capability_epoch=2)
        self.write_config()
        self.runtime = ProviderRuntime(self.client, self.config_path)
        self.publish(2)
        self.reserve('new', epoch=2)
        self.assertEqual('settled', self.runtime.poll_once()['dispatch'][0]['state'])
        self.assertEqual(['v1', 'v2'], self.calls())

    def test_old_unaccepted_version_does_not_abort_new_valid_dispatch_or_release_its_hold(self):
        self.reserve('a-old')
        self.install('v2')
        self.manifest['adapters'][0].update(version='2', capability_epoch=2)
        self.write_config()
        self.runtime = ProviderRuntime(self.client, self.config_path)
        self.publish(2)
        self.reserve('b-new', epoch=2)
        report = self.runtime.poll_once()
        self.assertEqual('degraded', report['status'])
        self.assertTrue(report['recoverable'])
        self.assertIn('owner_adapter_contract_mismatch', report['diagnostics'])
        self.assertEqual(['not_executed', 'settled'], [item['state'] for item in report['dispatch']])
        self.assertEqual(['v2'], self.calls())
        old = self.api.allocations.inspect('operator', 'a-old')
        self.assertEqual('pending', old['dispatch'][0]['state'])
        self.assertIsNone(old['dispatch'][0]['receipt_id'])
        self.assertTrue(old['capacity_held'])
        self.assertEqual(1, self.api.allocations.pools('operator')['pools'][0]['dimensions']['calls']['remaining'])
        with closing(sqlite3.connect(str(self.journal))) as db:
            self.assertEqual([('b-new',)], db.execute('SELECT operation_id FROM provider_executions').fetchall())

    def test_old_pending_version_does_not_block_recorded_same_id_late_settlement(self):
        self.reserve('recorded-old')
        def lost(path, body, value):
            if body and body.get('action') == 'settle':
                raise TimeoutError('DO_NOT_PRINT lost response')
            return value
        self.client.after = lost
        self.assertEqual('settlement_unknown', self.runtime.poll_once()['dispatch'][0]['state'])
        self.client.after = None
        self.reserve('a-stale')
        self.install('v2')
        self.manifest['adapters'][0].update(version='2', capability_epoch=2)
        self.write_config()
        self.runtime = ProviderRuntime(self.client, self.config_path)
        self.publish(2)
        self.reserve('b-new', epoch=2)
        report = self.runtime.poll_once()
        self.assertEqual(['v1', 'v2'], self.calls())
        self.assertEqual('settled', report['reconciled'][0]['state'])
        self.assertEqual('recorded-old', report['reconciled'][0]['operation_id'])
        self.assertEqual(['not_executed', 'settled'], [item['state'] for item in report['dispatch']])
        recorded_reports = [item for item in self.allocation_calls('settle')
                            if item['arguments']['operation_id'] == 'recorded-old']
        self.assertEqual(2, len(recorded_reports))
        self.assertEqual(1, len({item['arguments']['receipt_id'] for item in recorded_reports}))

    def test_malformed_selection_is_not_downgraded_to_a_version_mismatch(self):
        self.reserve()
        for field, value in (('capability_id', 'forged'), ('epoch', True), ('action', [])):
            def malformed(path, body, actual, field=field, value=value):
                if body and body.get('action') == 'pending':
                    actual['dispatch'][0]['selection'][field] = value
                return actual
            self.client.after = malformed
            with self.subTest(field=field), self.assertRaisesRegex(ProviderRuntimeError, '^provider_dispatch_failed$'):
                self.runtime.poll_once()
        self.assertEqual([], self.calls())
        self.assertEqual([], self.allocation_calls('accept'))

    def test_stop_before_poll_does_not_contact_authority_or_start_a_callback(self):
        self.reserve()
        stop = threading.Event()
        stop.set()
        before = list(self.client.calls)
        self.assertEqual([], self.runtime.poll_once(stop=stop)['dispatch'])
        self.assertEqual(before, self.client.calls)
        self.assertEqual([], self.calls())

    def test_stop_during_callback_finishes_and_settles_only_that_call(self):
        self.reserve('a-first')
        self.reserve('b-second')
        stop = threading.Event()
        adapter = self.runtime.bridge.bindings['arbitrary-owner-tool']
        invoke = adapter.invoke
        def finish_then_stop(context):
            result = invoke(context)
            stop.set()
            return result
        adapter.invoke = finish_then_stop
        report = self.runtime.serve(stop=stop, max_cycles=5)
        self.assertEqual(1, report['cycles'])
        self.assertEqual(['v1'], self.calls())
        self.assertEqual(['settled'], [item['state'] for item in report['last_report']['dispatch']])
        second = self.api.allocations.inspect('operator', 'b-second')['dispatch'][0]
        self.assertEqual('pending', second['state'])
        self.assertIsNone(second['receipt_id'])
        restarted = ProviderRuntime(self.client, self.config_path)
        self.assertEqual(['settled'], [item['state'] for item in restarted.poll_once()['dispatch']])
        self.assertEqual(['v1', 'v1'], self.calls())
        self.assertEqual(2, len(self.allocation_calls('start')))

    def test_stop_during_pending_rpc_skips_all_not_yet_admitted_work(self):
        self.reserve()
        stop = threading.Event()
        def pending_stop(path, body):
            if body and body.get('action') == 'pending':
                stop.set()
        self.client.before = pending_stop
        self.assertEqual([], self.runtime.poll_once(stop=stop)['dispatch'])
        self.assertEqual([], self.calls())
        self.assertEqual([], self.allocation_calls('accept'))

    def test_programmatic_bridge_batch_respects_stop_before_rpc_and_between_calls(self):
        self.reserve('a-first')
        self.reserve('b-second')
        stop = threading.Event()
        stop.set()
        before = list(self.client.calls)
        self.assertEqual([], self.runtime.bridge.run_pending(stop=stop)['dispatch'])
        self.assertEqual(before, self.client.calls)
        stop.clear()
        adapter = self.runtime.bridge.bindings['arbitrary-owner-tool']
        invoke = adapter.invoke
        def finish_then_stop(context):
            result = invoke(context)
            stop.set()
            return result
        adapter.invoke = finish_then_stop
        report = self.runtime.bridge.run_pending(stop=stop)
        self.assertEqual(['settled'], [item['state'] for item in report['dispatch']])
        self.assertEqual(['v1'], self.calls())
        self.assertEqual('pending', self.api.allocations.inspect('operator', 'b-second')['dispatch'][0]['state'])

    def test_stop_during_late_settlement_preserves_the_next_recorded_result(self):
        self.reserve('a-first')
        self.reserve('b-second')
        def lost(path, body, value):
            if body and body.get('action') == 'settle':
                raise TimeoutError('lost response')
            return value
        self.client.after = lost
        self.assertEqual(2, len(self.runtime.poll_once()['dispatch']))
        stop = threading.Event()
        def settle_then_stop(path, body, value):
            if body and body.get('action') == 'settle':
                stop.set()
            return value
        self.client.after = settle_then_stop
        report = self.runtime.poll_once(stop=stop)
        self.assertEqual(1, len(report['reconciled']))
        self.assertEqual(1, report['local_unsettled'])
        self.assertEqual(['v1', 'v1'], self.calls())
        self.client.after = None
        later = self.runtime.poll_once()
        self.assertEqual(1, len(later['reconciled']))
        self.assertEqual(0, later['local_unsettled'])
        self.assertEqual(['v1', 'v1'], self.calls())

    def test_old_recorded_result_settles_same_ids_after_adapter_upgrade_without_reexecution(self):
        self.reserve()
        def lost(path, body, value):
            if body is not None and body.get('action') == 'settle':
                raise TimeoutError('DO_NOT_PRINT lost response')
            return value
        self.client.after = lost
        first = self.runtime.poll_once()
        self.assertEqual('settlement_unknown', first['dispatch'][0]['state'])
        self.assertEqual(1, len(self.allocation_calls('settle')))
        self.install('v2')
        self.manifest['adapters'][0].update(version='2', capability_epoch=2)
        self.write_config()
        self.runtime = ProviderRuntime(self.client, self.config_path)
        self.publish(2)
        self.client.after = None
        report = self.runtime.poll_once()
        self.assertEqual('settled', report['reconciled'][0]['state'])
        self.assertEqual(['v1'], self.calls())
        receipts = {item['arguments']['receipt_id'] for item in self.allocation_calls('settle')}
        self.assertEqual(1, len(receipts))
        self.assertTrue(all(item['arguments']['operation_id'] == 'operation' for item in self.allocation_calls('settle')))

    def test_callback_timeout_unknown_survives_runtime_restart_and_is_never_replayed(self):
        self.install('v1', fail=True)
        # Publish the changed exact bytes as a genuine versioned epoch, rather
        # than pretending a local replacement still is the old capability.
        self.manifest['adapters'][0].update(version='failure-1', capability_epoch=2)
        self.write_config()
        self.runtime = ProviderRuntime(self.client, self.config_path)
        self.publish(2)
        self.reserve(epoch=2)
        report = self.runtime.poll_once()
        self.assertEqual('unknown', report['dispatch'][0]['state'])
        self.assertIn('adapter_outcome_unknown', report['diagnostics'])
        self.assertEqual(['v1'], self.calls())
        restarted = ProviderRuntime(self.client, self.config_path)
        for _ in range(3):
            later = restarted.poll_once()
            self.assertEqual('degraded', later['status'])
            self.assertEqual([], later['reconciled'])
        self.assertEqual(['v1'], self.calls())
        self.assertEqual(1, len(self.allocation_calls('start')))
        self.assertEqual([], self.allocation_calls('settle'))
        self.assertNotIn('DO_NOT_PRINT', json.dumps(report))

    def test_trusted_module_top_level_fault_is_unknown_not_healthy_and_never_replayed(self):
        source = "from pathlib import Path\nPath({!r}).write_text('entered')\nraise RuntimeError('DO_NOT_PRINT')\ndef invoke(context):\n    return {{}}\n".format(str(self.import_marker))
        self.install('failure', source=source)
        self.manifest['adapters'][0].update(version='failure', capability_epoch=2)
        self.write_config()
        self.runtime = ProviderRuntime(self.client, self.config_path)
        self.publish(2)
        self.assertFalse(self.import_marker.exists())
        self.reserve(epoch=2)
        report = self.runtime.poll_once()
        self.assertEqual('degraded', report['status'])
        self.assertEqual('unknown', report['dispatch'][0]['state'])
        self.assertTrue(self.import_marker.exists())
        self.assertEqual([], self.calls())
        self.import_marker.unlink()
        restarted = ProviderRuntime(self.client, self.config_path)
        self.assertEqual('degraded', restarted.poll_once()['status'])
        self.assertFalse(self.import_marker.exists())
        self.assertEqual(1, len(self.allocation_calls('start')))
        self.assertNotIn('DO_NOT_PRINT', json.dumps(report))

    def test_recorded_result_can_reconcile_after_owner_removes_all_installed_bindings(self):
        self.reserve()
        def lost(path, body, value):
            if body and body.get('action') == 'settle':
                raise TimeoutError('lost result report')
            return value
        self.client.after = lost
        self.assertEqual('settlement_unknown', self.runtime.poll_once()['dispatch'][0]['state'])
        self.client.after = None
        self.manifest['adapters'] = []
        self.write_config()
        self.module.unlink()
        restarted = ProviderRuntime(self.client, self.config_path)
        self.assertEqual('settled', restarted.poll_once()['reconciled'][0]['state'])
        self.assertEqual(['v1'], self.calls())

    def test_network_loss_before_pending_recovers_without_new_operation_or_journal_reset(self):
        self.reserve()
        def unavailable(path, body):
            if body is not None and body.get('action') == 'pending':
                raise ConnectionError('DO_NOT_PRINT unavailable')
        self.client.before = unavailable
        report = self.runtime.poll_once()
        self.assertEqual(['authority_unavailable'], report['diagnostics'])
        self.assertTrue(report['recoverable'])
        self.assertEqual([], self.calls())
        self.client.before = None
        resumed = ProviderRuntime(self.client, self.config_path).poll_once()
        self.assertEqual('settled', resumed['dispatch'][0]['state'])
        self.assertEqual(['v1'], self.calls())
        self.assertEqual(['operation'], [item['arguments']['operation_id'] for item in self.allocation_calls('start')])

    def test_admission_response_loss_does_not_auto_reconcile_or_replay_no_result(self):
        self.reserve()
        def lost(path, body, value):
            if body is not None and body.get('action') == 'start':
                raise TimeoutError('DO_NOT_PRINT admitted response lost')
            return value
        self.client.after = lost
        report = self.runtime.poll_once()
        self.assertEqual('unknown', report['dispatch'][0]['state'])
        self.client.after = None
        restarted = ProviderRuntime(self.client, self.config_path)
        self.assertEqual([], restarted.poll_once()['reconciled'])
        self.assertEqual([], self.calls())
        self.assertEqual(1, len(self.allocation_calls('start')))
        self.assertEqual(1, self.api.allocations.pools('operator')['pools'][0]['dimensions']['calls']['remaining'])

    def test_entire_manifest_and_all_source_validated_before_any_module_execution(self):
        bad = copy.deepcopy(self.manifest['adapters'][0])
        bad.update(capability_id='another-tool', sha256='0' * 64)
        self.manifest['adapters'].append(bad)
        new_journal = self.root / 'fresh-provider.db'
        self.manifest['journal'] = str(new_journal)
        self.write_config()
        with self.assertRaisesRegex(ProviderRuntimeError, '^owner_module_integrity_failed$'):
            ProviderRuntime(self.client, self.config_path)
        self.assertFalse(self.import_marker.exists())
        self.assertFalse(new_journal.exists())

    def test_module_cookie_cannot_execute_a_registered_decoder_during_constructor(self):
        decoded = []
        name = 'meshprobe' + hashlib.sha256(str(self.root).encode('utf8')).hexdigest()
        def decode(value, errors='strict'):
            decoded.append(True)
            return codecs.utf_8_decode(value, errors)
        def lookup(encoding):
            if encoding == name:
                return codecs.CodecInfo(name=name, encode=codecs.utf_8_encode, decode=decode)
        codecs.register(lookup)
        try:
            source = '# coding: ' + name + '\n' + self.module.read_text()
            self.install('cookie', source=source)
            runtime = ProviderRuntime(self.client, self.config_path)
            self.assertEqual([], decoded)
            self.assertFalse(self.import_marker.exists())
            self.assertEqual(1, len(runtime.describe_bindings()))
            self.module.write_bytes(b'\xff\ndef invoke(context):\n    return {}\n')
            self.module.chmod(0o600)
            self.manifest['adapters'][0]['sha256'] = hashlib.sha256(self.module.read_bytes()).hexdigest()
            self.write_config()
            with self.assertRaisesRegex(ProviderRuntimeError, '^owner_module_compile_failed$'):
                ProviderRuntime(self.client, self.config_path)
            self.assertEqual([], decoded)
        finally:
            if hasattr(codecs, 'unregister'):
                codecs.unregister(lookup)

    def test_existing_journal_namespace_is_validated_before_any_module_execution(self):
        self.manifest['authority_id'] = 'other-authority'
        self.write_config()
        with self.assertRaisesRegex(ProviderRuntimeError, '^provider_journal_invalid$'):
            ProviderRuntime(self.client, self.config_path)
        self.assertFalse(self.import_marker.exists())
        self.assertEqual([], self.calls())

    def test_private_config_module_symlinks_hardlinks_permissions_and_shared_parent_rejected(self):
        for path in (self.config_path, self.module):
            path.chmod(0o644)
            with self.subTest(path=path.name), self.assertRaises(ProviderRuntimeError):
                ProviderRuntime(self.client, self.config_path)
            path.chmod(0o600)
            linked = self.root / 'linked'
            os.link(str(path), str(linked))
            with self.assertRaises(ProviderRuntimeError):
                ProviderRuntime(self.client, self.config_path)
            linked.unlink()
        symlink = self.root / 'alias.json'
        symlink.symlink_to(self.config_path)
        with self.assertRaisesRegex(ProviderRuntimeError, '^owner_config_unavailable$'):
            ProviderRuntime(self.client, symlink)
        old = self.manifest['adapters'][0]['module']
        alias = self.root / 'alias.py'
        alias.symlink_to(self.module)
        self.manifest['adapters'][0]['module'] = str(alias)
        self.write_config()
        with self.assertRaisesRegex(ProviderRuntimeError, '^owner_module_unavailable$'):
            ProviderRuntime(self.client, self.config_path)
        self.manifest['adapters'][0]['module'] = old
        self.write_config()
        self.root.chmod(0o755)
        try:
            with self.assertRaises(ProviderRuntimeError):
                ProviderRuntime(self.client, self.config_path)
        finally:
            self.root.chmod(0o700)

    def test_invalid_owner_spend_epoch_function_duplicate_or_schema_never_executes(self):
        original = copy.deepcopy(self.manifest)
        for field, value in (('new_spend_minor', 1), ('new_spend_minor', False),
                             ('capability_epoch', True), ('function', '__import__("os")'),
                             ('function', 'missing'), ('sha256', 'NOT-A-HASH')):
            self.manifest = copy.deepcopy(original)
            self.manifest['adapters'][0][field] = value
            self.write_config()
            with self.subTest(field=field, value=value), self.assertRaises(ProviderRuntimeError):
                ProviderRuntime(self.client, self.config_path)
        self.manifest = copy.deepcopy(original)
        self.manifest['adapters'].append(copy.deepcopy(self.manifest['adapters'][0]))
        self.write_config()
        with self.assertRaisesRegex(ProviderRuntimeError, '^owner_config_invalid$'):
            ProviderRuntime(self.client, self.config_path)
        self.config_path.write_text('{"schema":1,"schema":1}')
        with self.assertRaisesRegex(ProviderRuntimeError, '^owner_config_invalid$'):
            ProviderRuntime(self.client, self.config_path)
        self.assertFalse(self.import_marker.exists())

    def test_fatal_journal_corruption_or_bad_pending_is_not_reported_as_healthy(self):
        with closing(sqlite3.connect(str(self.journal))) as db:
            db.execute('DROP TABLE provider_executions')
        with self.assertRaisesRegex(ProviderRuntimeError, '^provider_journal_failed$'):
            self.runtime.poll_once()
        self.assertFalse(self.import_marker.exists())

    def test_malformed_pending_and_auth_rejection_use_fixed_fail_closed_diagnostics(self):
        self.client.after = lambda path, body, value: {'dispatch': [None]} if body and body.get('action') == 'pending' else value
        with self.assertRaisesRegex(ProviderRuntimeError, '^provider_dispatch_failed$'):
            self.runtime.poll_once()
        self.client.after = None
        def rejected(path, body):
            raise urllib.error.HTTPError('https://DO_NOT_PRINT.invalid', 403, 'DO_NOT_PRINT', {}, io.BytesIO())
        self.client.before = rejected
        reports = []
        result = self.runtime.serve(on_report=reports.append, max_cycles=3)
        self.assertEqual(1, result['cycles'])
        self.assertEqual(['authority_rejected'], reports[0]['diagnostics'])
        self.assertFalse(reports[0]['recoverable'])
        self.assertNotIn('DO_NOT_PRINT', json.dumps(result))

    def test_serve_recovers_transient_pending_poll_and_stops_without_replaying(self):
        self.reserve()
        reports = []
        def unavailable(path, body):
            if body and body.get('action') == 'pending' and not reports:
                raise TimeoutError('DO_NOT_PRINT transient')
        self.client.before = unavailable
        result = self.runtime.serve(on_report=reports.append, max_cycles=3)
        self.assertEqual(3, result['cycles'])
        self.assertEqual(['degraded', 'ok', 'ok'], [item['status'] for item in reports])
        self.assertEqual(['v1'], self.calls())
        stopped = threading.Event()
        stopped.set()
        self.assertEqual(0, self.runtime.serve(stop=stopped)['cycles'])


if __name__ == '__main__':
    unittest.main()
