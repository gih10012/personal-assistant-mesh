"""Local adapter + journal/API tests, not remote-exec or independent-eval proof."""
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from pathlib import Path

from assistant_mesh.provider import Adapter, ManagedProvider
from assistant_mesh.server import API, serve
from assistant_mesh.worker import Client


class BoundClient:
    """In-process authenticated API transport fixture, not a remote provider."""
    def __init__(self, api, node='cloud'):
        self.api = api
        self.peer = {'role': 'worker', 'node': node}
        self.calls = []
        self.before = None
        self.after = None

    def request(self, path, body=None):
        self.calls.append(copy.deepcopy(body))
        if self.before:
            self.before(body)
        value = self.api.dispatch('POST', path, body, self.peer)
        return self.after(body, value) if self.after else value


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000.0
        self.config = {'database': str(self.root / 'authority.db'), 'peers': [],
                       'resources': {'trusted_verifiers': ['node:checker']}}
        self.api = API(self.config)
        self.api.store.clock = lambda: self.now
        self.api.store.heartbeat('laptop', ['leader', 'agent'])
        self.api.store.create_task('local read-only fixture', task_id='parent')
        self.task = self.api.store.claim('laptop')
        self.input = self.root / 'input.bin'
        self.input.write_bytes(os.urandom(64))
        self.input.chmod(0o600)
        self.scope = {'fixture_path': str(self.input)}
        self.workload = {'input_bytes': 64}
        self.invocations = []
        self.api.allocations.define_pool('operator', 'shared', {'requests': {'capacity': 2, 'unit': 'request'}})
        self.selection('target', 'node:cloud')
        self.plan = {'target': 'target', 'route': [], 'selections': [self.selections['target']]}
        self.api.allocations.reserve('node:laptop', 'operation', 'parent', self.task['epoch'], self.plan, 40)
        self.client = BoundClient(self.api)
        self.journal = self.root / 'provider.db'
        self.binding = Adapter('owner.fixture.sha256:v1', 1, 'fixture.hash', self.hash_adapter)
        self.provider = self.make_provider()

    def tearDown(self):
        self.temp.cleanup()

    def selection(self, identity, actor):
        self.api.resources.advertise(actor, identity, 'future.owner-installed.tool')
        self.api.resources.observe('node:checker', identity, 'latency', 3, 'ms', 'independent fixture',
                                   epoch=1, verification='verified', observation_id='observed-' + identity,
                                   evidence={'scope': self.scope, 'workload': self.workload})
        self.api.resources.request_grant('node:laptop', identity, 'fixture.hash', self.scope,
                                        request_id='request-' + identity)
        self.api.resources.grant('operator', 'request-' + identity)
        self.api.allocations.bind_pool('operator', identity, 1, 'shared', 1, 'requests', 1, 'request')
        if not hasattr(self, 'selections'):
            self.selections = {}
        self.selections[identity] = {'capability_id': identity, 'epoch': 1, 'action': 'fixture.hash',
                                    'scope': copy.deepcopy(self.scope), 'workload': copy.deepcopy(self.workload),
                                    'observations': [{'observation_id': 'observed-' + identity,
                                                      'metric': 'latency', 'unit': 'ms', 'max_age_seconds': 60}]}

    def hash_adapter(self, context):
        self.assertEqual('owner.fixture.sha256:v1', context['adapter_handle'])
        self.assertEqual(1, context['capability_epoch'])
        self.assertEqual('fixture.hash', context['action'])
        self.assertEqual(self.scope, context['scope'])
        self.assertEqual(self.workload, context['workload'])
        self.invocations.append(copy.deepcopy(context))
        # A real owned local file read + SHA calculation. No subprocess/model.
        with self.input.open('rb') as source:
            data = source.read()
        digest = hashlib.sha256(data).hexdigest()
        return {'outcome': 'completed', 'resource_quiescent': True,
                'result_reference': 'sha256:' + digest,
                'evidence': {'sha256': digest, 'input_bytes': len(data)}}

    def make_provider(self, client=None, journal=None, bindings=None, provider='node:cloud', authority='fixture-authority'):
        return ManagedProvider(client or self.client, journal or self.journal, provider,
                               {'target': self.binding} if bindings is None else bindings,
                               authority_id=authority, clock=lambda: self.now)

    def pending(self, client=None):
        return (client or self.client).request('/v1/allocation/action', {
            'action': 'pending', 'arguments': {}})['dispatch'][0]

    def remaining(self):
        return self.api.allocations.pools('operator')['pools'][0]['dimensions']['requests']['remaining']

    def local_row(self):
        with closing(sqlite3.connect(str(self.journal))) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute('SELECT * FROM provider_executions').fetchone())

    def test_real_local_read_only_adapter_settles_once_and_does_not_claim_independent_verification(self):
        pending = self.pending()
        result = self.provider.run(pending)
        expected = 'sha256:' + hashlib.sha256(self.input.read_bytes()).hexdigest()
        self.assertEqual('settled', result['state'])
        self.assertEqual(expected, result['result_reference'])
        self.assertTrue(result['local_invocation_started'])
        self.assertTrue(result['local_adapter_result_recorded'])
        self.assertTrue(result['authority_settlement_confirmed'])
        self.assertFalse(result['execution_verified'])
        self.assertFalse(result['resource_quiescence_independently_verified'])
        self.assertFalse(result['native_tools_intercepted'])
        self.assertEqual(1, len(self.invocations))
        self.assertEqual(result, self.provider.run(pending))
        self.assertEqual(result, self.make_provider().run(pending))
        self.assertEqual(1, len(self.invocations))
        self.assertEqual(2, self.remaining())
        remote = self.api.allocations.inspect('node:cloud', 'operation')
        self.assertEqual('completed', remote['dispatch'][0]['state'])
        self.assertFalse(remote['execution_verified'])
        with self.api.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])

    def test_journal_intents_are_durable_before_rpc_or_callback(self):
        def before(body):
            if body['action'] in ('accept', 'start', 'settle'):
                expected = {'accept': 'accept_intent', 'start': 'start_intent', 'settle': 'settlement_intent'}
                self.assertEqual(expected[body['action']], self.local_row()['state'])
        self.client.before = before
        original = self.binding.invoke

        def adapter(context):
            row = self.local_row()
            self.assertEqual('active', row['state'])
            self.assertEqual(context['invocation_nonce'], row['invocation_nonce'])
            self.assertTrue(row['receipt_id'])
            return original(context)
        provider = self.make_provider(bindings={'target': Adapter(self.binding.handle, 1, 'fixture.hash', adapter)})
        self.assertEqual('settled', provider.run(self.pending())['state'])

    def test_missing_adapter_is_explicit_and_has_no_accept_start_or_execution(self):
        self.client.calls = []
        result = self.make_provider(bindings={}).run(self.pending())
        self.assertEqual('not_executed', result['state'])
        self.assertEqual('owner_adapter_missing', result['error'])
        self.assertFalse(result['local_invocation_started'])
        self.assertEqual(['pending'], [call['action'] for call in self.client.calls])
        self.assertEqual([], self.invocations)
        self.assertEqual('reserved', self.api.allocations.inspect('node:laptop', 'operation')['state'])
        # Owner installation can subsequently use the same operation identity.
        self.assertEqual('settled', self.provider.run(self.pending())['state'])

    def test_owner_binding_epoch_action_and_handle_cannot_be_plan_selected(self):
        original = self.pending()
        for field, value in (('epoch', 2), ('action', 'fixture.shell'), ('capability_id', 'elsewhere')):
            pending = copy.deepcopy(original)
            pending['selection'][field] = value
            with self.subTest(field=field), self.assertRaises(PermissionError):
                self.provider.run(pending)
        pending = copy.deepcopy(original)
        pending['selection']['adapter_handle'] = 'model.chosen.handle'
        result = self.provider.run(pending)
        self.assertEqual('settled', result['state'])
        self.assertEqual(self.binding.handle, self.invocations[0]['adapter_handle'])
        self.assertEqual(1, len(self.invocations))

    def test_owner_binding_object_is_snapshotted_not_mutable_plan_authority(self):
        self.binding.handle = 'mutated-handle'
        self.binding.capability_epoch = 2
        self.binding.action = 'shell'
        self.binding.invoke = lambda context: self.fail('mutated adapter was used')
        self.assertEqual('settled', self.provider.run(self.pending())['state'])
        self.assertEqual('owner.fixture.sha256:v1', self.invocations[0]['adapter_handle'])

    def test_forged_pending_scope_is_checked_against_authenticated_authority_plan(self):
        pending = self.pending()
        pending['selection']['scope'] = {'fixture_path': '/unapproved/path'}
        pending['selection']['workload'] = {'input_bytes': 999}
        result = self.provider.run(pending)
        self.assertEqual('unknown', result['state'])
        self.assertEqual('authority_contract_unknown', result['error'])
        self.assertFalse(result['local_invocation_started'])
        self.assertEqual([], self.invocations)
        self.assertFalse(any(call['action'] in ('accept', 'start') for call in self.client.calls))
        self.assertEqual('reserved', self.api.allocations.inspect('node:laptop', 'operation')['state'])

    def test_rejects_new_spend_and_non_callback_model_configuration(self):
        for value in (True, 1, -1, 0.0):
            with self.subTest(value=value), self.assertRaises(PermissionError):
                Adapter('versioned', 1, 'run', self.hash_adapter, value)
        with self.assertRaises(ValueError):
            Adapter('versioned', 1, 'run', 'sh -c model-text')
        with self.assertRaises(ValueError):
            self.make_provider(bindings={'target': {'invoke': 'model shell'}})

    def test_admission_timeout_after_authority_accept_never_invokes_or_retries_new_id(self):
        pending = self.pending()

        def after(body, value):
            if body['action'] == 'accept':
                raise TimeoutError('private transport detail must not escape')
            return value
        self.client.after = after
        result = self.provider.run(pending)
        self.assertEqual('unknown', result['state'])
        self.assertFalse(result['local_invocation_started'])
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual([], self.invocations)
        self.assertEqual('unknown', self.make_provider().run(pending)['state'])
        self.assertEqual(1, sum(call['action'] == 'accept' for call in self.client.calls))
        self.client.after = None
        reconciled = self.provider.reconcile('operation', 'target')
        self.assertEqual('unknown', reconciled['state'])
        self.assertEqual('unknown', self.api.allocations.inspect('node:laptop', 'operation')['state'])
        self.assertEqual(1, self.remaining())

    def test_start_timeout_after_commit_never_invokes_even_after_restart(self):
        pending = self.pending()

        def after(body, value):
            if body['action'] == 'start':
                raise TimeoutError('start reply lost')
            return value
        self.client.after = after
        result = self.provider.run(pending)
        self.assertEqual('unknown', result['state'])
        self.assertFalse(result['local_invocation_started'])
        self.assertEqual('running', self.api.allocations.inspect('node:cloud', 'operation')['state'])
        self.assertEqual('unknown', self.make_provider().run(pending)['state'])
        self.assertEqual([], self.invocations)
        self.client.after = None
        self.provider.reconcile('operation', 'target')
        self.now += 120
        self.assertEqual(0, self.api.allocations.expire('operator')['expired_unaccepted'])
        self.assertEqual(1, self.remaining())

    def test_adapter_timeout_and_invalid_outcome_cannot_settle_or_release_capacity(self):
        pending = self.pending()

        def failed(context):
            self.invocations.append(context)
            raise TimeoutError('adapter may still have an active child')
        provider = self.make_provider(bindings={'target': Adapter(self.binding.handle, 1, 'fixture.hash', failed)})
        result = provider.run(pending)
        self.assertEqual('unknown', result['state'])
        self.assertTrue(result['local_invocation_intent_recorded'])
        self.assertIsNone(result['local_invocation_started'])
        self.assertFalse(result['local_adapter_result_recorded'])
        self.assertEqual('unknown', self.make_provider().run(pending)['state'])
        self.assertEqual(1, len(self.invocations))
        self.assertFalse(any(call['action'] == 'settle' for call in self.client.calls))
        self.assertEqual(1, self.remaining())
        for bad in ({}, {'outcome': 'completed', 'resource_quiescent': False, 'result_reference': 'result'},
                    {'outcome': 'completed', 'resource_quiescent': True},
                    {'outcome': 'completed', 'resource_quiescent': True, 'result_reference': '   '},
                    {'outcome': 'completed', 'resource_quiescent': True, 'result_reference': 'result',
                     'evidence': {'token': 'private'}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ManagedProvider._adapter_result(bad)

    def test_removing_adapter_does_not_erase_prior_unknown_invocation_evidence(self):
        pending = self.pending()

        def failed(context):
            raise TimeoutError('unknown child state')
        original = self.make_provider(bindings={'target': Adapter(self.binding.handle, 1, 'fixture.hash', failed)})
        self.assertEqual('unknown', original.run(pending)['state'])
        result = self.make_provider(bindings={}).run(pending)
        self.assertEqual('unknown', result['state'])
        self.assertTrue(result['local_invocation_intent_recorded'])
        self.assertIsNone(result['local_invocation_started'])
        self.assertEqual(1, self.remaining())

    def test_settlement_response_loss_reconciles_recorded_actual_result_without_reexecution(self):
        pending = self.pending()

        def after(body, value):
            if body['action'] == 'settle':
                raise TimeoutError('settlement reply lost')
            return value
        self.client.after = after
        result = self.provider.run(pending)
        self.assertEqual('settlement_unknown', result['state'])
        self.assertTrue(result['local_adapter_result_recorded'])
        self.assertEqual(1, len(self.invocations))
        self.client.after = None
        restored = self.make_provider()
        self.assertEqual('settlement_unknown', restored.run(pending)['state'])
        reconciled = restored.reconcile('operation', 'target')
        self.assertEqual('settled', reconciled['state'])
        self.assertTrue(reconciled['authority_settlement_confirmed'])
        self.assertEqual(1, len(self.invocations))
        ids = {call['arguments']['receipt_id'] for call in self.client.calls if call['action'] == 'settle'}
        self.assertEqual(1, len(ids))

    def test_lost_leader_term_denies_start_and_no_callback_runs(self):
        def before(body):
            if body['action'] == 'start':
                with self.api.store.transaction() as db:
                    db.execute('UPDATE leader SET epoch=epoch+1')
        self.client.before = before
        result = self.provider.run(self.pending())
        self.assertEqual('unknown', result['state'])
        self.assertFalse(result['local_invocation_started'])
        self.assertEqual([], self.invocations)
        self.assertEqual('accepted', self.api.allocations.inspect('node:laptop', 'operation')['state'])

    def test_authority_loss_after_actual_callback_keeps_result_for_later_reporting(self):
        def before(body):
            if body['action'] == 'settle':
                raise ConnectionError('authority disconnected')
        self.client.before = before
        result = self.provider.run(self.pending())
        self.assertEqual('settlement_unknown', result['state'])
        self.assertTrue(result['local_adapter_result_recorded'])
        self.assertEqual(1, self.remaining())
        self.now += 120
        self.client.before = None
        self.assertEqual('settled', self.make_provider().reconcile('operation', 'target')['state'])
        self.assertEqual(1, len(self.invocations))
        self.assertEqual(2, self.remaining())

    def test_wrong_authenticated_credential_or_pending_provider_does_not_execute(self):
        pending = self.pending()
        wrong = self.make_provider(client=BoundClient(self.api, 'stranger'))
        self.assertEqual('unknown', wrong.run(pending)['state'])
        self.assertEqual([], self.invocations)
        forged = dict(pending, provider='node:stranger')
        with self.assertRaises(PermissionError):
            self.provider.run(forged)

    def test_response_identity_and_epoch_forgery_are_rejected_before_invocation(self):
        context = {'operation_id': 'operation', 'capability_id': 'target', 'receipt_id': 'receipt'}
        valid = dict(context, provider='node:cloud', state='running', epoch=3)
        for key, value in (('operation_id', 'other'), ('capability_id', 'other'), ('receipt_id', 'other'),
                           ('provider', 'node:other'), ('state', 'completed'), ('epoch', True),
                           ('epoch', 3.0), ('epoch', 4)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.provider._response(dict(valid, **{key: value}), context, ('running',), (3,))

        def after(body, value):
            return dict(value, receipt_id='different-receipt') if body['action'] == 'start' else value
        self.client.after = after
        result = self.provider.run(self.pending())
        self.assertEqual('unknown', result['state'])
        self.assertFalse(result['local_invocation_started'])
        self.assertEqual([], self.invocations)

    def test_start_execute_once_false_is_not_invocation_permission(self):
        self.client.after = lambda body, value: dict(value, execute_once=False) if body['action'] == 'start' else value
        result = self.provider.run(self.pending())
        self.assertEqual('unknown', result['state'])
        self.assertEqual([], self.invocations)

    def assert_restart_after_rpc_intent(self, action, state):
        class SimulatedCrash(BaseException):
            pass
        pending = self.pending()

        def before(body):
            if body['action'] == action:
                raise SimulatedCrash()
        self.client.before = before
        with self.assertRaises(SimulatedCrash):
            self.provider.run(pending)
        self.assertEqual(state, self.local_row()['state'])
        calls = len(self.client.calls)
        self.client.before = None
        self.assertEqual(state, self.make_provider().run(pending)['state'])
        self.assertEqual(calls, len(self.client.calls))
        self.assertEqual([], self.invocations)

    def test_accept_intent_survives_injected_restart_without_replay(self):
        self.assert_restart_after_rpc_intent('accept', 'accept_intent')

    def test_start_intent_survives_injected_restart_without_replay(self):
        self.assert_restart_after_rpc_intent('start', 'start_intent')

    def test_invocation_intent_survives_injected_restart_without_calling_adapter(self):
        class SimulatedCrash(BaseException):
            pass
        pending = self.pending()
        transition = self.provider._transition

        def crash_after_intent(identity, states, **changes):
            row = transition(identity, states, **changes)
            if changes.get('state') == 'invocation_intent':
                raise SimulatedCrash()
            return row
        self.provider._transition = crash_after_intent
        with self.assertRaises(SimulatedCrash):
            self.provider.run(pending)
        self.assertEqual('invocation_intent', self.local_row()['state'])
        self.assertTrue(self.local_row()['invocation_nonce'])
        self.assertEqual('invocation_intent', self.make_provider().run(pending)['state'])
        self.assertEqual([], self.invocations)

    def test_actual_result_in_settlement_intent_survives_injected_restart(self):
        class SimulatedCrash(BaseException):
            pass

        def before(body):
            if body['action'] == 'settle':
                raise SimulatedCrash()
        self.client.before = before
        with self.assertRaises(SimulatedCrash):
            self.provider.run(self.pending())
        self.assertEqual('settlement_intent', self.local_row()['state'])
        self.assertTrue(self.local_row()['result'])
        self.client.before = None
        self.assertEqual('settled', self.make_provider().reconcile('operation', 'target')['state'])
        self.assertEqual(1, len(self.invocations))

    def test_concurrent_same_dispatch_and_reconciliation_do_not_call_adapter_twice(self):
        entered, release = threading.Event(), threading.Event()
        pending = self.pending()

        def blocked(context):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture release missing')
            return self.hash_adapter(context)
        provider = self.make_provider(bindings={'target': Adapter(self.binding.handle, 1, 'fixture.hash', blocked)})
        results = []
        thread = threading.Thread(target=lambda: results.append(provider.run(pending)), daemon=True)
        thread.start()
        self.assertTrue(entered.wait(5))
        try:
            other = self.make_provider()
            self.assertTrue(other.run(pending)['execution_instance_active'])
            self.assertEqual('execution_instance_active', other.reconcile('operation', 'target')['reconciliation'])
            self.assertFalse(any(call['action'] == 'unknown' for call in self.client.calls))
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual('settled', results[0]['state'])
        self.assertEqual(1, len(self.invocations))

    def test_multi_provider_receipts_are_isolated_for_same_operation(self):
        # Replace the unaccepted single-target fixture with a two-provider plan.
        self.api.allocations.cancel('node:laptop', 'operation', 1)
        self.selection('edge-target', 'node:edge')
        plan = {'target': 'target', 'route': [], 'selections': list(self.selections.values())}
        self.api.allocations.reserve('node:laptop', 'multi', 'parent', self.task['epoch'], plan, 40)
        cloud = self.provider.run(self.pending())
        edge_client = BoundClient(self.api, 'edge')
        edge = self.make_provider(client=edge_client, journal=self.root / 'edge.db', provider='node:edge',
                                  bindings={'edge-target': self.binding})
        edge_result = edge.run(self.pending(edge_client))
        self.assertEqual('settled', cloud['state'])
        self.assertEqual('settled', edge_result['state'])
        self.assertNotEqual(cloud['receipt_id'], edge_result['receipt_id'])
        self.assertEqual(2, len(self.invocations))
        with self.assertRaises(ValueError):
            self.make_provider(provider='node:edge')
        with self.assertRaises(ValueError):
            self.make_provider(authority='different-authority')
        self.assertEqual(2, self.remaining())

    def test_journal_file_parent_sidecar_and_filename_safety(self):
        self.assertEqual(0o600, self.journal.stat().st_mode & 0o777)
        bad_parent = self.root / 'public'
        bad_parent.mkdir(mode=0o755)
        # CLI tests may have deliberately tightened the process umask. This
        # negative fixture must really be public, regardless of caller umask.
        bad_parent.chmod(0o755)
        with self.assertRaises(ValueError):
            self.make_provider(journal=bad_parent / 'journal.db')
        with self.assertRaises(ValueError):
            self.make_provider(journal=Path('relative.db'))
        with self.assertRaises(ValueError):
            self.make_provider(journal=self.root / 'file:journal.db')
        symbolic = self.root / 'symbolic.db'
        symbolic.symlink_to(self.journal)
        with self.assertRaises(ValueError):
            self.make_provider(journal=symbolic)
        hard = self.root / 'hard.db'
        os.link(str(self.journal), str(hard))
        with self.assertRaises(ValueError):
            self.make_provider(journal=hard)
        hard.unlink()
        self.journal.chmod(0o644)
        with self.assertRaises(ValueError):
            self.make_provider()
        self.journal.chmod(0o600)
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.journal) + suffix)
            sidecar.symlink_to(self.input)
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.make_provider()
            sidecar.unlink()
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.make_provider(journal=alias / 'elsewhere.db')

    def test_active_wal_and_operation_lock_files_are_private(self):
        with self.provider._db():
            for suffix in ('-wal', '-shm'):
                metadata = Path(str(self.journal) + suffix).stat()
                self.assertEqual(os.getuid(), metadata.st_uid)
                self.assertEqual(0o600, metadata.st_mode & 0o777)
        pending = self.pending()
        self.provider.run(pending)
        locks = list(self.root.glob('provider.db.lock-*'))
        self.assertEqual(1, len(locks))
        self.assertEqual(0o600, locks[0].stat().st_mode & 0o777)
        locks[0].unlink()
        locks[0].symlink_to(self.input)
        with self.assertRaises(ValueError):
            self.provider.run(pending)

    def test_schema_mismatch_is_not_silently_reset(self):
        with closing(sqlite3.connect(str(self.journal))) as db:
            db.execute("UPDATE provider_metadata SET value='99' WHERE key='schema'")
            db.commit()
        with self.assertRaises(ValueError):
            self.make_provider()
        with closing(sqlite3.connect(str(self.journal))) as db:
            self.assertEqual('99', db.execute("SELECT value FROM provider_metadata WHERE key='schema'").fetchone()[0])
        unrelated = self.root / 'unrelated.db'
        with closing(sqlite3.connect(str(unrelated))) as db:
            db.execute('CREATE TABLE user_data(value TEXT)')
        unrelated.chmod(0o600)
        with self.assertRaises(ValueError):
            self.make_provider(journal=unrelated)
        with closing(sqlite3.connect(str(unrelated))) as db:
            self.assertEqual('delete', db.execute('PRAGMA journal_mode').fetchone()[0])

    @contextmanager
    def http_client(self):
        token = self.root / 'provider.token'
        descriptor = os.open(str(token), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write('owned-fixture-credential-not-real-' + 'x' * 32)
        config = dict(self.config, port=0, peers=[{
            'role': 'worker', 'node': 'cloud', 'token_file': str(token)}])
        ready, holder = threading.Event(), {}

        def on_ready(server, api, channel):
            api.store.clock = lambda: self.now
            holder['server'] = server
            ready.set()
        thread = threading.Thread(target=serve, args=(config, on_ready), daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5))
        client_config = {'control_url': 'http://127.0.0.1:%d' % holder['server'].server_address[1],
                         'token_file': str(token)}
        try:
            yield Client(client_config), client_config
        finally:
            holder['server'].shutdown()
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_actual_loopback_client_to_local_adapter_and_journal(self):
        with self.http_client() as (client, config):
            provider = self.make_provider(client=client)
            result = provider.run_pending()
        self.assertEqual('settled', result['dispatch'][0]['state'])
        self.assertFalse(result['execution_verified'])
        self.assertEqual(1, len(self.invocations))

    def test_real_process_exit_after_invocation_intent_is_not_replayed_by_new_process_owner(self):
        with self.http_client() as (client, config):
            script = '''
import hashlib, json, os, sys
from pathlib import Path
from assistant_mesh.provider import Adapter, ManagedProvider
from assistant_mesh.worker import Client
cfg, journal, source = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
def adapter(context):
    with Path(source).open('rb') as handle:
        hashlib.sha256(handle.read()).hexdigest()
    os._exit(73)
provider = ManagedProvider(Client(cfg), journal, 'node:cloud',
    {'target': Adapter('owner.fixture.sha256:v1', 1, 'fixture.hash', adapter)},
    authority_id='fixture-authority')
provider.run_pending()
'''
            child = subprocess.run([sys.executable, '-c', script, json.dumps(config),
                                    str(self.journal), str(self.input)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(73, child.returncode)
            row = self.local_row()
            self.assertEqual('active', row['state'])
            self.assertTrue(row['invocation_nonce'])
            restored = self.make_provider(client=client)
            # The old authority row is now running, so run_pending cannot replay.
            self.assertEqual([], restored.run_pending()['dispatch'])
            result = restored.reconcile('operation', 'target')
            self.assertEqual('unknown', result['state'])
            self.assertTrue(result['local_invocation_intent_recorded'])
            self.assertIsNone(result['local_invocation_started'])
            self.assertFalse(result['local_adapter_result_recorded'])
            self.assertEqual([], self.invocations)
            self.assertEqual(1, self.remaining())


if __name__ == '__main__':
    unittest.main()
