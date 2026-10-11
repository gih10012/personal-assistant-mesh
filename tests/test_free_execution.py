"""Contract/fake-native tests: no model POST, external client or production write."""
import base64
import copy
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh import free_execution as free
from assistant_mesh.provider import Adapter, ManagedProvider
from tests.test_provider import BoundClient
from assistant_mesh.server import API


class FakeNative:
    instances = []
    failure = None
    mismatch = False
    reaped = True
    model_override = None
    cost = 0

    def __init__(self, config, root, work, deadline):
        self.config, self.root, self.work = config, root, work
        self.events = []
        self.stops = 0
        self.instances.append(self)

    def execute(self, request):
        if self.failure:
            raise free.FreeExecutionError(self.failure)
        self.messages = [{'info': {'id': 'user-1', 'role': 'user'}, 'parts': []},
            {'info': {'id': 'assistant-1', 'role': 'assistant', 'providerID': 'opencode',
                      'modelID': self.model_override or self.config['model'].split('/')[1],
                      'cost': self.cost, 'time': {'created': 1, 'completed': 2}},
             'parts': [{'type': 'tool', 'tool': 'bash', 'state': {'status': 'completed', 'output': 'fixture'}},
                       {'type': 'text', 'text': 'fixture result'}]}]
        (self.work / 'result.txt').write_bytes(b'fixture\n')
        return 'ses_fixture1', self.messages

    def stop(self):
        self.stops += 1
        return self.reaped

    def export(self, session):
        return {'info': {'id': session, 'cost': 0},
                'messages': [] if self.mismatch else copy.deepcopy(self.messages)}


class FreeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.requests, self.artifacts = self.root / 'requests', self.root / 'artifacts'
        self.requests.mkdir(mode=0o700)
        self.artifacts.mkdir(mode=0o700)
        self.catalog = self.root / 'catalog.json'
        catalog = {'opencode': {'models': {'step-5-preview-free': {
            'tool_call': True, 'cost': {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0},
            'variants': {'low': {}, 'medium': {}, 'high': {}}}}}}
        self.catalog_sha = self.private(self.catalog, catalog)
        request = {'schema': 1, 'prompt': 'Read inputs and create result.txt; no external side effects.',
                   'inputs_base64': {'input.txt': base64.b64encode(b'fixture input').decode('ascii')},
                   'artifacts': ['result.txt']}
        raw = free._canonical(request)
        self.request_sha = hashlib.sha256(raw).hexdigest()
        free._write(self.requests / (self.request_sha + '.json'), raw)
        self.config = {'schema': 1, 'provider': 'node:fixture', 'capability_id': 'free-candidate',
            'capability_epoch': 1, 'action': 'native.free.execute', 'artifact_root': str(self.artifacts),
            'request_root': str(self.requests), 'client': '/usr/bin/opencode', 'client_sha256': '1' * 64,
            'bubblewrap': '/usr/bin/bwrap', 'bubblewrap_sha256': '2' * 64,
            'catalog': str(self.catalog), 'catalog_sha256': self.catalog_sha,
            'model': 'opencode/step-5-preview-free', 'physical_model_reference': 'stepfun/step-5-preview',
            'effort': 'default', 'expires_at': time.time() + 300, 'max_seconds': 180,
            'max_input_bytes': 1024, 'max_output_bytes': 4096, 'steps': 3, 'output_tokens': 4096,
            'allowed_privacy': ['public-synthetic'], 'proxy_url': ''}
        self.config_path = self.root / 'config.json'
        self.config_sha = self.private(self.config_path, self.config)
        self.context = {'provider': 'node:fixture', 'capability_id': 'free-candidate', 'capability_epoch': 1,
            'action': 'native.free.execute', 'operation_id': 'op-1', 'task_id': 'task-1', 'task_epoch': 1,
            'receipt_id': 'receipt-1', 'adapter_handle': 'stock-v1', 'invocation_nonce': '3' * 32,
            'scope': {'request_sha256': self.request_sha, 'privacy': 'public-synthetic'},
            'workload': {'kind': 'native-free-execution', 'timeout_seconds': 180, 'max_output_bytes': 4096}}
        FakeNative.instances = []
        FakeNative.failure, FakeNative.mismatch, FakeNative.reaped = None, False, True
        FakeNative.model_override, FakeNative.cost = None, 0
        self.executable = patch.object(free, '_executable', side_effect=lambda path, digest: path)
        self.backend = patch.object(free, '_NativeStock', FakeNative)
        self.executable.start()
        self.backend.start()

    def tearDown(self):
        self.executable.stop()
        self.backend.stop()
        self.temp.cleanup()

    def private(self, path, value):
        raw = free._canonical(value)
        if path.exists():
            path.unlink()  # Only this test-owned disposable file.
        free._write(path, raw)
        return hashlib.sha256(raw).hexdigest()

    def configure(self, **changes):
        self.config.update(changes)
        self.config_sha = self.private(self.config_path, self.config)

    def run_callback(self, context=None):
        return free.execute(context or self.context, self.config_path, self.config_sha)

    def test_completed_requires_native_messages_export_and_real_output_bytes(self):
        result = self.run_callback()
        self.assertEqual('completed', result['outcome'])
        self.assertTrue(result['resource_quiescent'])
        self.assertEqual(1, len(FakeNative.instances))
        self.assertEqual(2, FakeNative.instances[0].stops)
        evidence = result['evidence']
        self.assertFalse(evidence['leader_replaced'])
        self.assertFalse(evidence['native_tools_intercepted'])
        self.assertIsNone(evidence['wire_model_post_count'])
        self.assertFalse(evidence['downstream_egress_verified'])
        self.assertEqual(0, evidence['cash_cost_minor'])
        value = free.read_result(self.config_path, self.config_sha, evidence['artifact_id'], evidence['artifact_sha256'])
        self.assertEqual(value['native_messages'], value['native_export']['messages'])
        self.assertEqual(b'fixture\n', base64.b64decode(value['artifacts'][0]['base64']))
        self.assertEqual(0o600, (self.artifacts / evidence['artifact_id'] / 'result.json').stat().st_mode & 0o777)
        self.assertNotIn('fixture result', json.dumps(result))
        self.assertNotIn('ses_fixture1', json.dumps(result))

    def test_same_original_operation_cannot_replay_with_new_nonce(self):
        self.run_callback()
        context = copy.deepcopy(self.context)
        context['invocation_nonce'] = '4' * 32
        with self.assertRaisesRegex(free.FreeExecutionError, 'original_invocation_no_replay'):
            self.run_callback(context)
        self.assertEqual(1, len(FakeNative.instances))

    def test_timeout_preserves_intent_and_does_not_replay(self):
        FakeNative.failure = 'free_execution_timeout_unknown'
        with self.assertRaisesRegex(free.FreeExecutionError, 'timeout_unknown'):
            self.run_callback()
        self.assertEqual(1, FakeNative.instances[0].stops)
        private = next(self.artifacts.iterdir())
        self.assertTrue((private / 'intent.json').exists())
        self.assertFalse((private / 'result.json').exists())
        FakeNative.failure = None
        with self.assertRaisesRegex(free.FreeExecutionError, 'original_invocation_no_replay'):
            self.run_callback()

    def test_paid_or_changed_model_does_not_settle(self):
        FakeNative.model_override = 'paid-model'
        with self.assertRaisesRegex(free.FreeExecutionError, 'native_outcome_unproved'):
            self.run_callback()
        self.assertEqual(1, FakeNative.instances[0].stops)

    def test_nonzero_reported_cost_does_not_settle(self):
        FakeNative.cost = .01
        with self.assertRaisesRegex(free.FreeExecutionError, 'native_outcome_unproved'):
            self.run_callback()

    def test_export_mismatch_does_not_settle(self):
        FakeNative.mismatch = True
        with self.assertRaisesRegex(free.FreeExecutionError, 'export_mismatch'):
            self.run_callback()

    def test_no_quiescence_is_unknown_even_if_final_text_says_success(self):
        FakeNative.reaped = False
        with self.assertRaisesRegex(free.FreeExecutionError, 'quiescence_unproved'):
            self.run_callback()

    def test_owner_ttl_privacy_and_workload_checked_before_client_entry(self):
        for mutation in (lambda value: value['scope'].update(privacy='owner-approved-third-party'),
                         lambda value: value['workload'].update(timeout_seconds=181),
                         lambda value: value['workload'].update(max_output_bytes=4097),
                         lambda value: value.update(provider='node:elsewhere')):
            context = copy.deepcopy(self.context)
            mutation(context)
            with self.assertRaises(free.FreeExecutionError):
                self.run_callback(context)
        self.configure(expires_at=time.time() - 1)
        with self.assertRaisesRegex(free.FreeExecutionError, 'expired'):
            self.run_callback()
        self.assertEqual([], FakeNative.instances)
        self.assertEqual([], list(self.artifacts.iterdir()))

    def test_private_request_hash_and_symlink_are_rejected_before_invocation(self):
        path = self.requests / (self.request_sha + '.json')
        path.write_text('{}')
        with self.assertRaisesRegex(free.FreeExecutionError, 'hash_changed'):
            self.run_callback()
        path.unlink()
        path.symlink_to(self.config_path)
        with self.assertRaisesRegex(free.FreeExecutionError, 'private_file_unavailable'):
            self.run_callback()
        self.assertEqual([], FakeNative.instances)

    def test_catalog_price_drift_is_not_converted_to_paid_fallback(self):
        catalog = json.loads(self.catalog.read_text())
        catalog['opencode']['models']['step-5-preview-free']['cost']['input'] = 1
        sha = self.private(self.catalog, catalog)
        self.configure(catalog_sha256=sha)
        with self.assertRaisesRegex(free.FreeExecutionError, 'nonzero_catalog_cost'):
            self.run_callback()
        self.assertEqual([], FakeNative.instances)

    def test_request_paths_and_base64_are_bounded(self):
        for name in ('../secret', '/etc/passwd', '.git/config', 'a/../b', 'a//b', 'a\\b'):
            with self.subTest(name=name), self.assertRaises(free.FreeExecutionError):
                free._relative(name)
        self.assertEqual('a/b.txt', free._relative('a/b.txt'))

    def test_artifact_symlink_or_changed_hash_cannot_be_read_as_result(self):
        work = self.root / 'work'
        work.mkdir(mode=0o700)
        (work / 'out').symlink_to(self.config_path)
        with self.assertRaises(free.FreeExecutionError):
            free._artifact(work, 'out', 10000)
        result = self.run_callback()['evidence']
        with self.assertRaisesRegex(free.FreeExecutionError, 'hash_changed'):
            free.read_result(self.config_path, self.config_sha, result['artifact_id'], '5' * 64)

    def test_history_result_read_does_not_need_current_runtime_dependencies_or_availability(self):
        result = self.run_callback()['evidence']
        self.catalog.unlink()
        self.configure(expires_at=time.time() - 1)
        with patch.object(free, '_executable', side_effect=AssertionError('read must not inspect executable')):
            value = free.read_result(self.config_path, self.config_sha, result['artifact_id'], result['artifact_sha256'])
        self.assertEqual(free.FORMAT, value['format'])
        self.assertEqual(1, len(FakeNative.instances))

    def test_stock_namespace_keeps_native_tools_and_original_environment_without_keys(self):
        # Constructor only: do NOT launch bwrap, OpenCode, a model, or any network.
        self.backend.stop()
        native_root = self.root / 'namespace'
        native_root.mkdir(mode=0o700)
        work = native_root / 'work'
        work.mkdir(mode=0o700)
        with patch.dict(os.environ, {'SOME_API_KEY': 'must-not-copy'}):
            backend = free._NativeStock(self.config, native_root, work, time.monotonic() + 100)
        self.assertEqual(os.environ['HOME'], backend.env['HOME'])
        self.assertNotIn('SOME_API_KEY', backend.env)
        self.assertNotIn('OPENCODE_AUTH_JSON', backend.env)
        self.assertNotIn('--unshare-net', backend.command)
        self.assertIn('--pure', backend.command)
        self.assertEqual('/usr/bin/opencode', backend.command[-2])
        self.assertIn('--tmpfs', backend.command)
        self.assertIn(os.environ['HOME'], backend.command)
        self.assertNotIn('permission', backend.env)
        self.assertTrue(backend.stop())
        self.backend.start()

    def managed(self):
        config = {'database': str(self.root / 'authority.db'), 'peers': [],
                  'resources': {'trusted_verifiers': ['node:checker']}}
        api = API(config)
        api.store.heartbeat('laptop', ['leader', 'agent'])
        api.store.create_task('synthetic free external capability', task_id='task-1')
        task = api.store.claim('laptop')
        self.context['task_epoch'] = task['epoch']
        scope, workload = self.context['scope'], self.context['workload']
        api.allocations.define_pool('operator', 'free-pool', {'calls': {'capacity': 1, 'unit': 'request'}})
        api.resources.advertise('node:fixture', 'free-candidate', 'native.free.execution')
        api.resources.observe('node:checker', 'free-candidate', 'latency', 1, 'ms', 'fixture only',
            epoch=1, verification='verified', observation_id='free-observation',
            evidence={'scope': scope, 'workload': workload})
        api.resources.request_grant('node:laptop', 'free-candidate', 'native.free.execute', scope, request_id='grant')
        api.resources.grant('operator', 'grant')
        api.allocations.bind_pool('operator', 'free-candidate', 1, 'free-pool', 1, 'calls', 1, 'request')
        selection = {'capability_id': 'free-candidate', 'epoch': 1, 'action': 'native.free.execute',
            'scope': scope, 'workload': workload, 'observations': [{'observation_id': 'free-observation',
                'metric': 'latency', 'unit': 'ms', 'max_age_seconds': 60}]}
        plan = {'target': 'free-candidate', 'route': [], 'selections': [selection]}
        api.allocations.reserve('node:laptop', 'op-1', 'task-1', task['epoch'], plan, 40)
        client = BoundClient(api, node='fixture')
        provider = ManagedProvider(client, self.root / 'provider.db', 'node:fixture',
            {'free-candidate': Adapter('stock-v1', 1, 'native.free.execute', lambda context: self.run_callback(context))},
            'fixture-authority')
        pending = client.request('/v1/allocation/action', {'action': 'pending', 'arguments': {}})['dispatch'][0]
        return api, client, provider, pending

    def test_real_managed_pipeline_fake_native_executes_once_and_reconciles_same_id(self):
        api, client, provider, pending = self.managed()
        result = provider.run(pending)
        self.assertEqual('settled', result['state'])
        self.assertFalse(result['execution_verified'])
        self.assertEqual(1, len(FakeNative.instances))
        self.assertEqual(result, provider.run(pending))
        self.assertEqual(result, provider.reconcile('op-1', 'free-candidate'))
        self.assertEqual(1, len(FakeNative.instances))
        self.assertEqual(0, api.allocations.pools('operator')['pools'][0]['dimensions']['calls']['held'])

    def test_managed_timeout_remains_unknown_and_keeps_capacity_no_replay(self):
        api, client, provider, pending = self.managed()
        FakeNative.failure = 'free_execution_timeout_unknown'
        result = provider.run(pending)
        self.assertEqual('unknown', result['state'])
        self.assertFalse(result['local_adapter_result_recorded'])
        self.assertEqual(1, len(FakeNative.instances))
        provider.run(pending)
        provider.reconcile('op-1', 'free-candidate')
        self.assertEqual(1, len(FakeNative.instances))
        self.assertEqual(1, api.allocations.pools('operator')['pools'][0]['dimensions']['calls']['held'])


if __name__ == '__main__':
    unittest.main()
