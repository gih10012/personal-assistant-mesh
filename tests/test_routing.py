import copy
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.allocations import Allocations
from assistant_mesh.networking import Network
from assistant_mesh.remote import Remote
from assistant_mesh.resources import Registry
from assistant_mesh.routing import Routing
from assistant_mesh.store import Conflict, Store


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = Store(Path(self.temp.name) / 'mesh.db', clock=lambda: self.now)
        self.store.heartbeat('leader-node', ['leader', 'agent'])
        self.store.create_task('routing fixture', task_id='task-a')
        self.task = self.store.claim('leader-node')
        self.routing = Routing(self.store, 'authority-fixture')
        self.body = {'decision_id': 'decision-a', 'work': {'action': 'future.read', 'scope': {'project': 'p'}},
                     'candidates': [{'issuer': 'peer-a', 'kind': 'open.future.kind', 'id': 'cap-a'}],
                     'selection': {'state': 'waiting_evidence'},
                     'rationale': 'Need actual scope/workload evidence.',
                     'evidence_refs': [{'source': 'projection', 'issuer': 'peer-a', 'id': 'cap-a', 'revision': 8}]}

    def tearDown(self):
        self.temp.cleanup()

    def propose(self, body=None):
        return self.routing.propose('task-a', 'leader-node', self.task['epoch'],
                                    self.body if body is None else body)

    def inspect(self, identity='decision-a'):
        return self.routing.inspect('task-a', 'leader-node', self.task['epoch'], identity)

    def link(self, kind, reference, identity='decision-a'):
        return self.routing.link('task-a', 'leader-node', self.task['epoch'], identity, kind, reference)

    def dump(self, exclude_routing=False):
        db = sqlite3.connect(self.store.path)
        try:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            result = {}
            for name in names:
                if exclude_routing and name == 'route_decisions':
                    continue
                result[name] = db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid').fetchall()
            return result
        finally:
            db.close()

    def remote(self, reference='call-remote', parent='task-a'):
        network = Network(self.store, 'source-node')
        remote = Remote(self.store, network)
        return remote.delegate(parent, 'leader-node', self.task['epoch'], reference, 'peer-a',
                               {'input': 'bounded fixture', 'project_id': 'p', 'agent_id': 'specialist'})

    def allocation(self, operation='op-a'):
        registry = Registry(self.store, trusted_verifiers=['verifier-fixture'])
        bridge = Allocations(self.store, registry)
        scope, workload = {'project': 'p'}, {'input_bytes': 64}
        registry.advertise('node:leader-node', 'cap-local', 'future.any.kind')
        registry.observe('verifier-fixture', 'cap-local', 'latency', 3, 'ms', 'fixture',
                         evidence={'scope': scope, 'workload': workload}, verification='verified',
                         epoch=1, observation_id='measurement-local')
        bridge.define_pool('operator', 'shared', {'concurrency': {'capacity': 2, 'unit': 'slot'}})
        bridge.bind_pool('operator', 'cap-local', 1, 'shared', 1, 'concurrency', 1, 'slot')
        plan = {'target': 'cap-local', 'route': [], 'selections': [{
            'capability_id': 'cap-local', 'epoch': 1, 'action': 'future.read', 'scope': scope,
            'workload': workload, 'observations': [{'observation_id': 'measurement-local',
                'metric': 'latency', 'unit': 'ms', 'max_age_seconds': 60}]}]}
        return bridge, bridge.reserve('node:leader-node', operation, 'task-a', self.task['epoch'], plan)

    def test_proposal_is_declared_immutable_coordination_not_execution(self):
        before = self.dump(exclude_routing=True)
        first = self.propose()
        self.assertEqual(before, self.dump(exclude_routing=True))
        self.assertTrue(first['proposal_only'])
        self.assertTrue(first['proposal_created'])
        self.assertEqual('model_declared', first['proposal_verification'])
        self.assertEqual('unchecked_model_references', first['evidence_refs_verification'])
        self.assertIsNone(first['linked_execution'])
        for key in ('managed_invocation_authorized', 'capacity_reserved', 'execution_verified',
                    'plan_alignment_verified', 'model_selection_verified',
                    'native_tools_intercepted', 'task_or_operation_replayed'):
            self.assertFalse(first[key])
        self.body['work']['new_field'] = 'caller mutation'
        self.assertNotIn('new_field', self.inspect()['decision']['work'])

    def test_same_content_retry_ignores_dict_key_order(self):
        first = self.propose()
        reversed_body = dict(reversed(list(self.body.items())))
        second = self.propose(reversed_body)
        self.assertFalse(second['proposal_created'])
        self.assertEqual(first['content_sha256'], second['content_sha256'])
        self.assertEqual(first['created'], second['created'])
        self.assertEqual(1, len(self.routing.list('task-a', 'leader-node', self.task['epoch'])['decisions']))

    def test_changed_content_or_other_task_same_id_conflicts(self):
        self.propose()
        changed = copy.deepcopy(self.body)
        changed['selection'] = {'peer': 'other'}
        with self.assertRaises(Conflict):
            self.propose(changed)
        self.store.create_task('other job', task_id='task-b', context={'session_scope': 'other'})
        other = self.store.claim('leader-node')
        with self.assertRaises(Conflict):
            self.routing.propose(other['id'], 'leader-node', other['epoch'], self.body)
        with self.assertRaises(PermissionError):
            self.routing.inspect(other['id'], 'leader-node', other['epoch'], 'decision-a')

    def test_same_task_resumed_by_new_node_preserves_original_binding(self):
        original = self.propose()
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET status=?,node=?,epoch=?,deadline=?,leader_epoch=? WHERE id=?',
                       ('running', 'new-node', 2, 1090, 2, 'task-a'))
            db.execute('UPDATE leader SET node=?,epoch=?,deadline=?', ('new-node', 2, 1060))
        replay = self.routing.propose('task-a', 'new-node', 2, self.body)
        self.assertFalse(replay['proposal_created'])
        self.assertEqual(original['original_node'], replay['original_node'])
        self.assertEqual(1, replay['original_epoch'])
        self.assertEqual(1, replay['original_leader_epoch'])
        self.assertEqual(original['created'], replay['created'])
        with self.assertRaises(Conflict):
            self.propose()

    def test_stale_node_epoch_task_or_leader_rejects_all_calls(self):
        self.propose()
        for node, epoch in (('wrong', 1), ('leader-node', 2), ('leader-node', True)):
            for name in ('propose', 'inspect', 'list'):
                args = [self.body] if name == 'propose' else ['decision-a'] if name == 'inspect' else []
                with self.subTest(node=node, epoch=epoch, method=name), self.assertRaises((Conflict, ValueError)):
                    getattr(self.routing, name)('task-a', node, epoch, *args)
        with self.store.transaction() as db:
            db.execute('UPDATE leader SET epoch=epoch+1')
        for name, args in (('propose', [self.body]), ('inspect', ['decision-a']), ('list', [])):
            with self.assertRaises(Conflict):
                getattr(self.routing, name)('task-a', 'leader-node', 1, *args)

    def test_expired_and_completed_task_cannot_read_or_replay(self):
        self.propose()
        self.now += 91
        with self.assertRaises(Conflict):
            self.inspect()
        with self.assertRaises(Conflict):
            self.propose()
        self.now = 1000
        self.store.update_task('task-a', 'leader-node', 1, status='completed')
        with self.assertRaises(Conflict):
            self.inspect()

    def test_restart_and_authority_namespace_guard(self):
        first = self.propose()
        store = Store(self.store.path, clock=lambda: self.now, recover_inflight=False)
        restored = Routing(store, 'authority-fixture')
        self.assertEqual(first['content_sha256'], restored.inspect('task-a', 'leader-node', 1, 'decision-a')['content_sha256'])
        with self.assertRaisesRegex(Conflict, 'authority_mismatch'):
            Routing(store, 'other-authority')

    def test_bounded_list_is_task_scoped_without_mutation(self):
        for index in range(3):
            body = dict(self.body, decision_id='decision-' + str(index))
            self.propose(body)
        before = self.dump()
        page = self.routing.list('task-a', 'leader-node', 1, limit=2)
        self.assertTrue(page['has_more'])
        self.assertEqual(2, len(page['decisions']))
        self.assertEqual(before, self.dump())
        for limit in (True, 0, 101, '20', None):
            with self.assertRaises(ValueError):
                self.routing.list('task-a', 'leader-node', 1, limit=limit)

    def test_body_schema_rejects_fake_bindings_execution_claims_and_bad_json(self):
        for key in ('authority', 'actor', 'task_id', 'task_epoch', 'node', 'execution_link', 'outcome', 'verification'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.propose(dict(self.body, **{key: 'made-up'}))
        for field, bad in (('work', {}), ('work', []), ('selection', None), ('candidates', 'not-list'),
                           ('candidates', [None]), ('evidence_refs', [1]), ('rationale', 2),
                           ('rationale', '\ud800'), ('rationale', 'x' * 8001)):
            body = dict(self.body, **{field: bad})
            with self.subTest(field=field, bad=repr(bad)[:80]), self.assertRaises(ValueError):
                self.propose(body)
        for nested in ({1: 'coerced-key'}, {'number': float('nan')}, {'number': float('inf')},
                       {'object': object()}, {'text': '\ud800'}, {'\ud800': 'invalid key'}):
            with self.assertRaises(ValueError):
                self.propose(dict(self.body, work=nested))
        cyclic = {'value': None}
        cyclic['value'] = cyclic
        with self.assertRaises(ValueError):
            self.propose(dict(self.body, work=cyclic))

    def test_private_metadata_and_url_auth_rejected_not_redacted_contract(self):
        for work in ({'token': 'fixture-secret'}, {'endpoint': 'https://user:pass@example.invalid/'},
                     {'endpoint': 'https://example.invalid/?auth=private'}):
            with self.assertRaisesRegex(ValueError, 'private_routing_metadata'):
                self.propose(dict(self.body, work=work))
        self.assertEqual([], self.routing.list('task-a', 'leader-node', 1)['decisions'])

    def test_overall_and_list_bounds(self):
        for field, value in (('work', {'large': 'x' * 65536}),
                             ('candidates', [{}] * 65), ('evidence_refs', [{}] * 129)):
            with self.assertRaises(ValueError):
                self.propose(dict(self.body, **{field: value}))

    def test_parallel_same_id_only_one_insert(self):
        responses, errors = [], []
        start = threading.Event()

        def write():
            try:
                start.wait(2)
                responses.append(self.propose())
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=write) for _ in range(4)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(4, len(responses))
        self.assertEqual(1, sum(r['proposal_created'] for r in responses))

    def test_transaction_failure_rolls_back_proposal(self):
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER refuse_routing BEFORE INSERT ON route_decisions BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.propose()
        self.assertEqual([], self.routing.list('task-a', 'leader-node', 1)['decisions'])

    def test_no_process_network_model_or_core_writes(self):
        before = self.dump(exclude_routing=True)
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no process')), \
                mock.patch('socket.socket', side_effect=AssertionError('no network')):
            self.propose()
            self.inspect()
            self.routing.list('task-a', 'leader-node', 1)
        self.assertEqual(before, self.dump(exclude_routing=True))

    def test_link_real_remote_identity_and_read_state_without_result_text(self):
        self.propose()
        child = self.remote()
        before = self.dump(exclude_routing=True)
        linked = self.link('remote_delegation', child['id'])
        self.assertTrue(linked['link_created'])
        self.assertEqual(child['message_id'], linked['linked_execution']['message_id'])
        self.assertFalse(linked['linked_execution']['execution_verified'])
        self.assertFalse(linked['linked_execution']['plan_alignment_verified'])
        self.assertTrue(linked['linked_execution']['association_caller_selected'])
        self.assertFalse(linked['linked_execution']['model_selection_verified'])
        self.assertEqual(before, self.dump(exclude_routing=True))
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET result=?,status=? WHERE id=?',
                       ('private fixture result', 'unknown', child['id']))
        view = self.inspect()
        self.assertEqual('unknown', view['linked_execution']['state'])
        self.assertEqual(hashlib.sha256(b'private fixture result').hexdigest(), view['linked_execution']['result_sha256'])
        self.assertNotIn('private fixture result', json.dumps(view))
        self.assertFalse(self.link('remote_delegation', child['id'])['link_created'])

    def test_link_different_ref_conflicts_even_unknown(self):
        self.propose()
        first = self.remote('remote-a')
        second = self.remote('remote-b')
        self.link('remote_delegation', first['id'])
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET status=? WHERE id=?', ('unknown', first['id']))
        with self.assertRaises(Conflict):
            self.link('remote_delegation', second['id'])
        self.assertEqual(first['id'], self.inspect()['linked_execution']['reference_id'])

    def test_missing_wrong_kind_and_wrong_task_references_rejected(self):
        self.propose()
        with self.assertRaisesRegex(ValueError, 'ledger_unavailable'):
            self.link('remote_delegation', 'missing')
        with self.assertRaises(ValueError):
            self.link('arbitrary_execution', 'missing')
        self.remote()
        with self.assertRaisesRegex(ValueError, 'reference_not_found'):
            self.link('remote_delegation', 'missing')
        self.store.create_task('other', task_id='task-b', context={'session_scope': 'other'})
        other = self.store.claim('leader-node')
        child = self.remote('other-call', parent=other['id'])
        with self.assertRaises(PermissionError):
            self.link('remote_delegation', child['id'])
        self.assertIsNone(self.inspect()['linked_execution'])

    def test_link_actual_allocation_metadata_not_execution_and_never_writes_effects(self):
        self.propose()
        bridge, allocation = self.allocation()
        before = self.dump(exclude_routing=True)
        linked = self.link('managed_allocation', allocation['operation_id'])
        self.assertEqual('reserved', linked['linked_execution']['state'])
        self.assertEqual(before, self.dump(exclude_routing=True))
        bridge.accept('node:leader-node', 'op-a', 'cap-local', 1, 'receipt-a')
        bridge.unknown('node:leader-node', 'op-a', 'cap-local', 2)
        before = self.dump(exclude_routing=True)
        self.assertEqual('unknown', self.inspect()['linked_execution']['state'])
        self.assertFalse(self.inspect()['execution_verified'])
        self.assertFalse(self.link('managed_allocation', 'op-a')['link_created'])
        self.assertEqual(before, self.dump(exclude_routing=True))
        self.assertEqual(1, bridge.pools('node:leader-node')['pools'][0]['dimensions']['concurrency']['remaining'])

    def test_allocation_principal_mismatch_is_not_linkable(self):
        self.propose()
        self.allocation()
        with self.store.transaction() as db:
            db.execute('UPDATE managed_allocations SET actor=? WHERE operation_id=?', ('node:other', 'op-a'))
        with self.assertRaisesRegex(PermissionError, 'principal_mismatch'):
            self.link('managed_allocation', 'op-a')
        self.assertIsNone(self.inspect()['linked_execution'])

    def test_link_survives_resume_and_unknown_keeps_original_principal(self):
        self.propose()
        self.allocation()
        self.link('managed_allocation', 'op-a')
        with self.store.transaction() as db:
            db.execute('UPDATE managed_allocations SET state=? WHERE operation_id=?', ('unknown', 'op-a'))
            db.execute('UPDATE tasks SET node=?,epoch=?,leader_epoch=? WHERE id=?', ('new-node', 2, 2, 'task-a'))
            db.execute('UPDATE leader SET node=?,epoch=?', ('new-node', 2))
        view = self.routing.inspect('task-a', 'new-node', 2, 'decision-a')
        self.assertEqual('leader-node', view['original_node'])
        self.assertEqual('unknown', view['linked_execution']['state'])
        self.assertFalse(self.routing.link('task-a', 'new-node', 2, 'decision-a', 'managed_allocation', 'op-a')['link_created'])

    def test_link_rolls_back_when_ledger_schema_or_child_identity_broken(self):
        self.propose()
        child = self.remote()
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET parent_id=? WHERE id=?', ('unrelated', child['id']))
        with self.assertRaisesRegex(Conflict, 'identity_mismatch'):
            self.link('remote_delegation', child['id'])
        with self.store.transaction() as db:
            db.execute('DROP TABLE remote_delegations')
            db.execute('CREATE TABLE remote_delegations(child_id TEXT)')
        with self.assertRaisesRegex(ValueError, 'ledger_unavailable'):
            self.link('remote_delegation', child['id'])
        self.assertIsNone(self.inspect()['linked_execution'])

    def test_remote_original_caller_mismatch_rejected(self):
        self.propose()
        child = self.remote()
        with self.store.transaction() as db:
            db.execute('UPDATE remote_delegations SET caller_node=? WHERE child_id=?',
                       ('other-node', child['id']))
        with self.assertRaisesRegex(PermissionError, 'principal_mismatch'):
            self.link('remote_delegation', child['id'])
        self.assertIsNone(self.inspect()['linked_execution'])

    def test_owner_historical_read_after_completion_has_no_pseudo_worker_binding(self):
        self.propose()
        child = self.remote()
        self.link('remote_delegation', child['id'])
        self.store.update_task('task-a', 'leader-node', 1, status='completed')
        before = self.dump()
        with self.assertRaises(Conflict):
            self.inspect()
        read = self.routing.owner_read(decision_id='decision-a', task_id='task-a')
        self.assertEqual(1, len(read['decisions']))
        self.assertEqual(child['id'], read['decisions'][0]['linked_execution']['reference_id'])
        self.assertEqual(before, self.dump())
        self.assertFalse(read['execution_verified'])

    def test_owner_read_filters_limits_and_fixed_missing_error(self):
        self.propose()
        self.assertEqual([], self.routing.owner_read(task_id='other-task')['decisions'])
        self.assertEqual('decision-a', self.routing.owner_read()['decisions'][0]['decision_id'])
        with self.assertRaisesRegex(ValueError, 'decision_not_found'):
            self.routing.owner_read(decision_id='decision-a', task_id='wrong-task')
        with self.assertRaisesRegex(ValueError, 'decision_not_found'):
            self.routing.owner_read(decision_id='missing')
        for value in (True, 0, 101, '1'):
            with self.assertRaises(ValueError):
                self.routing.owner_read(limit=value)

    def test_page_total_byte_bound_for_worker_and_owner_read(self):
        for index in range(3):
            self.propose(dict(self.body, decision_id='decision-' + str(index)))
        page = self.routing.list('task-a', 'leader-node', 1, limit=1)
        encoded = json.dumps(page, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf8')
        with mock.patch('assistant_mesh.routing.MAX_OUTPUT_BYTES', len(encoded) + 32):
            for read in (lambda: self.routing.list('task-a', 'leader-node', 1, limit=100),
                         lambda: self.routing.owner_read(task_id='task-a', limit=100)):
                bounded = read()
                self.assertTrue(bounded['has_more'])
                self.assertEqual(1, len(bounded['decisions']))
                self.assertLessEqual(len(json.dumps(bounded, sort_keys=True, separators=(',', ':'),
                                                   ensure_ascii=True).encode('utf8')), len(encoded) + 32)

    def test_dispatch_metadata_is_bounded_and_marks_truncation(self):
        self.propose()
        self.allocation()
        with self.store.transaction() as db:
            for index in range(40):
                db.execute('INSERT INTO managed_dispatch VALUES(?,?,?,?,?,?,?,?,?)',
                           ('op-a', 'extra-' + str(index), 'node:leader-node', 'unknown',
                            1, None, None, self.now, self.now))
        linked = self.link('managed_allocation', 'op-a')['linked_execution']
        self.assertTrue(linked['dispatch_truncated'])
        self.assertEqual(32, len(linked['dispatch']))


if __name__ == '__main__':
    unittest.main()
