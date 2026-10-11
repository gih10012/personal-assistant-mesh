"""In-process real ledgers, not network/provider execution evidence."""
import copy
from contextlib import closing
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.capability_call import CapabilityCallError, TaskCapabilityCalls
from assistant_mesh.server import API


class FixtureClient:
    def __init__(self, api):
        self.api = api
        self.calls = []
        self.fail_before = None
        self.fail_after = None
        self.alter = None

    def request(self, path, body=None):
        action = body['action'] if body else 'hello'
        key = ('routing:' if 'routing' in path else 'allocation:' if 'allocation' in path else '') + action
        self.calls.append((key, copy.deepcopy(body)))
        if self.fail_before == key:
            self.fail_before = None
            raise OSError('private credential must not leak')
        value = self.api.dispatch('POST' if body is not None else 'GET', path,
                                  body or {}, {'role': 'worker', 'node': 'worker'})
        if self.fail_after == key:
            self.fail_after = None
            raise TimeoutError('private control URL must not leak')
        if self.alter:
            value = self.alter(key, value)
        return value


class CapabilityCallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.now = 1000.0
        self.api = API({'database': str(self.root / 'authority.db'), 'peers': [],
                        'node_id': 'authority', 'resources': {'trusted_verifiers': ['checker']}})
        self.api.store.clock = lambda: self.now
        self.api.store.heartbeat('worker', ['leader', 'agent'])
        self.api.store.create_task('no external effect fixture', task_id='parent')
        self.task = self.api.store.claim('worker')
        self.scope = {'operation': 'get', 'url': 'https://example.org/path'}
        self.workload = {'kind': 'https', 'max_bytes': 1024, 'timeout_seconds': 5}
        self.api.resources.advertise('node:worker', 'cap', 'network.https')
        self.api.resources.observe('checker', 'cap', 'path.latency', 2, 'ms', 'fixture',
            epoch=1, observation_id='real-observation', verification='verified',
            evidence={'scope': self.scope, 'workload': self.workload})
        self.api.allocations.define_pool('operator', 'shared', {'requests': {'capacity': 1, 'unit': 'slot'}})
        self.api.allocations.bind_pool('operator', 'cap', 1, 'shared', 1, 'requests', 1, 'slot')
        self.client = FixtureClient(self.api)
        self.path = self.root / 'calls.db'
        self.calls = self.helper()
        self.arguments = {'operation_id': 'op', 'decision_id': 'decision',
            'selection': {'capability_id': 'cap', 'epoch': 1, 'action': 'network.https',
                'scope': self.scope, 'workload': self.workload, 'observations': [{
                    'observation_id': 'real-observation', 'metric': 'path.latency',
                    'unit': 'ms', 'max_age_seconds': 60}]},
            'candidates': [{'capability_id': 'cap', 'epoch': 1},
                           {'capability_id': 'another-declared-candidate', 'epoch': 1}],
            'rationale': 'Model chose the measured path; not a proof of target content.',
            'lease_seconds': 40}

    def tearDown(self):
        self.temp.cleanup()

    def helper(self, epoch=None, node='worker', authority='authority', task='parent'):
        return TaskCapabilityCalls(self.client, str(self.path), authority, node, task,
                                   self.task['epoch'] if epoch is None else epoch)

    def invoke(self, **overrides):
        args = copy.deepcopy(self.arguments)
        args.update(overrides)
        return self.calls.call(**args)

    def writes(self):
        return [key for key, body in self.client.calls
                if key in ('routing:propose', 'allocation:reserve', 'routing:link')]

    def test_compose_original_ids_and_provider_queue_without_execution(self):
        result = self.invoke()
        self.assertEqual('observed', result['phase'])
        self.assertEqual('reserved', result['operation_state'])
        self.assertEqual(['routing:propose', 'allocation:reserve', 'routing:link'], self.writes())
        self.assertEqual({'propose': 'confirmed', 'reserve': 'confirmed', 'link': 'confirmed'}, result['stages'])
        proposal = result['routing']['decision']
        self.assertEqual(self.scope, proposal['work']['scope'])
        self.assertEqual(self.arguments['candidates'], proposal['candidates'])
        self.assertEqual('real-observation', proposal['evidence_refs'][0]['observation_id'])
        self.assertEqual('op', result['routing']['linked_execution']['reference_id'])
        self.assertEqual('pending', result['allocation']['dispatch'][0]['state'])
        self.assertTrue(result['allocation']['capacity_held'])
        for field in ('execution_verified', 'target_content_verified', 'authentication_verified',
                      'inference_verified', 'automatic_invocation_replay', 'retry_with_new_id',
                      'native_tools_intercepted'):
            self.assertFalse(result[field])
        with self.api.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM capability_metrics').fetchone()[0])

    def test_same_request_repeat_and_restart_only_inspect_confirmed_stages(self):
        first = self.invoke()
        self.calls = self.helper()
        second = self.invoke()
        self.assertEqual(first['request_sha256'], second['request_sha256'])
        self.assertEqual(3, len(self.writes()))
        self.assertEqual('reserved', second['operation_state'])
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)

    def test_changed_content_and_reused_decision_conflict(self):
        self.invoke()
        with self.assertRaisesRegex(CapabilityCallError, 'identity_content_conflict'):
            self.invoke(rationale='changed intent')
        with self.assertRaisesRegex(CapabilityCallError, 'decision_identity_conflict'):
            self.invoke(operation_id='other-op')
        self.assertEqual(3, len(self.writes()))

    def test_propose_ack_lost_inspect_original_then_only_next_writes(self):
        self.client.fail_after = 'routing:propose'
        self.assertEqual('unresolved', self.invoke()['phase'])
        self.calls = self.helper()
        result = self.invoke()
        self.assertEqual('observed', result['phase'])
        self.assertEqual(['routing:propose', 'allocation:reserve', 'routing:link'], self.writes())

    def test_reserve_ack_lost_inspect_original_no_reservation_replay(self):
        self.client.fail_after = 'allocation:reserve'
        first = self.invoke()
        self.assertEqual('attempted', first['stages']['reserve'])
        self.calls = self.helper()
        result = self.invoke()
        self.assertEqual('observed', result['phase'])
        self.assertEqual(1, self.writes().count('allocation:reserve'))

    def test_link_ack_lost_only_read_original_link(self):
        self.client.fail_after = 'routing:link'
        self.assertEqual('unresolved', self.invoke()['phase'])
        self.calls = self.helper()
        self.assertEqual('observed', self.invoke()['phase'])
        self.assertEqual(3, len(self.writes()))

    def test_attempt_without_effect_absent_lookup_never_retries(self):
        self.client.fail_before = 'allocation:reserve'
        self.invoke()
        self.calls = self.helper()
        result = self.invoke()
        self.assertEqual('unresolved', result['phase'])
        self.assertEqual(1, self.writes().count('allocation:reserve'))
        self.assertNotIn('routing:link', self.writes())

    def test_unknown_original_operation_does_not_fallback_or_replay(self):
        self.invoke()
        with self.api.store.transaction() as db:
            db.execute("UPDATE managed_allocations SET state='unknown' WHERE operation_id='op'")
            db.execute("UPDATE managed_dispatch SET state='unknown' WHERE operation_id='op'")
        result = self.invoke()
        self.assertEqual('unknown', result['operation_state'])
        self.assertTrue(result['allocation']['capacity_held'])
        self.assertEqual(3, len(self.writes()))

    def test_actual_core_rejects_forged_observation_and_changed_scope(self):
        for field in ('observation', 'scope'):
            self.client.calls.clear()
            args = copy.deepcopy(self.arguments)
            args['operation_id'] += '-' + field
            args['decision_id'] += '-' + field
            if field == 'observation':
                args['selection']['observations'][0]['observation_id'] = 'fake-observation'
            else:
                args['selection']['scope']['url'] = 'https://example.org/new-target'
            result = self.calls.call(**args)
            self.assertEqual('unresolved', result['phase'])
            self.assertNotIn('routing:link', self.writes())
        with self.api.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM managed_allocations').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM capability_metrics').fetchone()[0])

    def test_foreign_provider_still_requires_owner_exact_grant(self):
        with self.api.store.transaction() as db:
            db.execute("UPDATE capabilities SET principal='node:other' WHERE id='cap'")
        result = self.invoke()
        self.assertEqual('unresolved', result['phase'])
        with self.api.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM capability_grants').fetchone()[0])

    def test_changed_epoch_can_read_original_but_not_write_pending_stage(self):
        self.client.fail_after = 'allocation:reserve'
        self.invoke()
        with self.api.store.transaction() as db:
            db.execute("UPDATE tasks SET epoch=epoch+1 WHERE id='parent'")
        self.calls = self.helper(epoch=2)
        self.assertEqual('reserved', self.calls.status('op')['operation_state'])
        result = self.invoke()
        self.assertEqual('original_epoch_read_only', result['phase'])
        self.assertEqual(1, self.writes().count('allocation:reserve'))
        self.assertNotIn('routing:link', self.writes())

    def test_expired_task_status_keeps_allocation_read_without_task_write(self):
        self.invoke()
        self.now += 100
        result = self.calls.status('op')
        self.assertEqual('reserved', result['operation_state'])
        self.assertIsNone(result['routing'])
        self.assertEqual(3, len(self.writes()))
        self.assertEqual('unresolved', self.invoke()['phase'])

    def test_authority_changed_blocks_all_requests_after_hello(self):
        self.invoke()
        self.client.alter = lambda key, value: dict(value, authority='different') if key == 'hello' else value
        self.assertEqual('unresolved', self.calls.status('op')['phase'])
        self.assertEqual('unresolved', self.invoke()['phase'])
        self.assertEqual(3, len(self.writes()))
        for overrides in ({'authority': 'different'}, {'node': 'different'}):
            with self.assertRaisesRegex(CapabilityCallError, 'journal_identity_mismatch'):
                self.helper(**overrides)

    def test_current_task_identity_not_model_override_or_other_task(self):
        self.invoke()
        with self.assertRaisesRegex(CapabilityCallError, 'task_identity_mismatch'):
            self.helper(task='different').status('op')
        self.client.alter = lambda key, value: dict(value, task={'id': 'parent', 'node': 'other', 'epoch': 1}) if key == 'routing:context' else value
        result = self.invoke(operation_id='other-op', decision_id='other-decision')
        self.assertEqual('unresolved', result['phase'])
        self.assertEqual(3, len(self.writes()))

    def test_result_has_original_provider_report_not_artifact_body_proof(self):
        self.invoke()
        accepted = self.api.allocations.accept('node:worker', 'op', 'cap', 1, 'receipt')
        started = self.api.allocations.start('node:worker', 'op', 'cap', accepted['epoch'], 'receipt')
        self.api.allocations.settle('node:worker', 'op', 'cap', started['epoch'], 'receipt', 'completed',
            {'resource_quiescent': True, 'result_reference': 'mesh-https-artifact:sha256:' + 'a' * 64,
             'http_status': 401, 'tls_verify_result': 0})
        result = self.calls.result('op')
        self.assertEqual('completed', result['operation_state'])
        self.assertEqual(1, len(result['results']))
        item = result['results'][0]
        self.assertEqual('provider_reported', item['verification'])
        digest = hashlib.sha256(json.dumps(item['settlement'], sort_keys=True,
            separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
        self.assertEqual(digest, item['settlement_sha256'])
        self.assertFalse(result['target_content_verified'])
        self.assertFalse(result['authentication_verified'])
        self.assertFalse(result['inference_verified'])
        self.assertFalse(result['allocation']['capacity_held'])
        self.assertEqual(3, len(self.writes()))

    def test_bad_request_private_fields_rejected_before_intent(self):
        for key in ('actor', 'provider', 'token', 'authority'):
            selection = dict(self.arguments['selection'], **{key: 'not-allowed'})
            with self.assertRaisesRegex(CapabilityCallError, 'invalid_capability_call_request'):
                self.invoke(selection=selection)
        with self.assertRaisesRegex(CapabilityCallError, 'invalid_capability_call_request'):
            self.invoke(rationale='https://name:password@example.org/path')
        self.assertEqual([], self.client.calls)

    def test_rpc_budget_checked_before_remember_or_network(self):
        # _request already imposes protocol body bounds; explicitly test the
        # actual wire guard too, independent of compact local journal budget.
        request = TaskCapabilityCalls._request(**self.arguments)
        request['decision']['work']['large'] = 'x' * (128 * 1024)
        with mock.patch.object(TaskCapabilityCalls, '_request', return_value=request):
            with self.assertRaisesRegex(CapabilityCallError, 'rpc_body_too_large'):
                self.invoke()
        self.assertEqual([], self.client.calls)
        with closing(sqlite3.connect(str(self.path))) as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM capability_calls').fetchone()[0])

    def test_private_journal_symlink_permissions_and_bool_epoch_reject(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(CapabilityCallError, 'private_journal_required'):
            self.helper()
        self.path.chmod(0o600)
        link = self.root / 'link.db'
        link.symlink_to(self.path)
        with self.assertRaisesRegex(CapabilityCallError, 'private_journal_required'):
            TaskCapabilityCalls(self.client, str(link), 'authority', 'worker', 'parent', 1)
        with self.assertRaises(CapabilityCallError):
            self.helper(epoch=True)

    def test_connection_always_closed_on_success_and_exception(self):
        opened = []
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            value = real_connect(*args, **kwargs)
            opened.append(value)
            return value

        with mock.patch('assistant_mesh.capability_call.sqlite3.connect', side_effect=connect):
            self.invoke()
            with self.assertRaises(CapabilityCallError):
                self.helper(authority='other')
        self.assertTrue(opened)
        for db in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')

    def test_malformed_original_receipt_unknown_no_new_write(self):
        self.invoke()
        self.client.alter = lambda key, value: dict(value, operation_id='other') if key == 'allocation:inspect' else value
        result = self.invoke()
        self.assertEqual('unresolved', result['phase'])
        self.assertEqual(3, len(self.writes()))
        self.assertNotIn('private', json.dumps(result))

    def test_wrong_dispatch_identity_is_not_returned_as_original_result(self):
        self.invoke()

        def alter(key, value):
            if key == 'allocation:inspect':
                value['dispatch'][0]['operation_id'] = 'foreign-op'
            return value

        self.client.alter = alter
        result = self.calls.result('op')
        self.assertEqual('unresolved', result['phase'])
        self.assertEqual([], result['results'])
        self.assertIsNone(result['allocation'])

    def test_concurrent_same_intent_writes_each_stage_at_most_once(self):
        original = self.client.request
        entered, release = threading.Event(), threading.Event()

        def request(path, body=None):
            if body and body['action'] == 'propose':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test barrier timed out')
            return original(path, body)

        self.client.request = request
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(self.invoke()))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            # Another process sees the durable attempt before the RPC commit;
            # it can only read, not race a second propose/reserve.
            second = self.helper().call(**copy.deepcopy(self.arguments))
            self.assertEqual('unresolved', second['phase'])
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual('observed', outcomes[0]['phase'])
        self.assertEqual(['routing:propose', 'allocation:reserve', 'routing:link'], self.writes())


if __name__ == '__main__':
    unittest.main()
