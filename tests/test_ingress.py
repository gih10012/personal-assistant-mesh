"""Local authority contract tests, not a live OAuth/Chat/Cloud connector proof."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from assistant_mesh.ingress import Ingress, validate_config, validate_peer
from assistant_mesh.store import Conflict, Store


class IngressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-ingress.')
        self.store = Store(Path(self.temporary.name) / 'authority.sqlite', clock=lambda: 1000.0)
        self.config = {'owner_id': 'owner-fixture'}
        self.ingress = Ingress(self.store, 'authority-fixture', self.config)
        self.peer = {'role': 'ingress', 'ingress': {
            'owner_id': 'owner-fixture', 'source': 'fixture-chat', 'subject': 'subject-a',
            'scopes': ['tasks:submit', 'tasks:read']}}
        self.payload = {'request_id': 'message-1', 'input': '开放项目：发现本节点能力并制定计划。'}

    def tearDown(self):
        self.temporary.cleanup()

    def count(self, table):
        with self.store.transaction() as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def task(self, task_id):
        with self.store.transaction() as db:
            return dict(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def snapshot(self):
        with self.store.transaction() as db:
            names = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
                    for name in names}

    def submit(self, payload=None, peer=None):
        return self.ingress.submit(self.peer if peer is None else peer,
                                   self.payload if payload is None else payload)

    def status(self, request_id='message-1', peer=None):
        return self.ingress.status(self.peer if peer is None else peer, {'request_id': request_id})

    def test_default_submission_uses_existing_store_and_continuous_leader_scope(self):
        receipt = self.submit()
        self.assertRegex(receipt['task_id'], r'^ingress-[0-9a-f]{64}$')
        task = self.task(receipt['task_id'])
        self.assertEqual(self.payload['input'], task['input'])
        self.assertEqual(['leader'], json.loads(task['required']))
        self.assertEqual('leader:owner', task['scope'])
        self.assertEqual('owner-ingress', json.loads(task['context'])['origin']['kind'])
        self.assertIsNone(task['parent_id'])
        self.assertTrue(receipt['accepted'])
        self.assertTrue(receipt['task_created'])
        self.assertEqual('pending', receipt['status'])
        self.assertFalse(receipt['execution_verified'])
        self.assertFalse(receipt['native_tools_intercepted'])
        self.assertFalse(receipt['result_available'])
        self.assertEqual(0, self.count('native_sessions'))
        self.assertEqual(0, self.count('outbox'))

    def test_request_identity_uses_full_authority_owner_source_subject_namespace(self):
        receipt = self.submit()
        encoded = json.dumps(['owner-ingress/1', 'authority-fixture', 'owner-fixture',
                              'fixture-chat', 'subject-a', 'message-1'],
                             sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        self.assertEqual('ingress-' + hashlib.sha256(encoded.encode()).hexdigest(), receipt['task_id'])

    def test_same_content_retry_ignores_object_order_and_cannot_create_second_task(self):
        first = self.submit()
        before = self.snapshot()
        retry = self.submit(dict(reversed(list(self.payload.items()))))
        self.assertFalse(retry['task_created'])
        self.assertEqual(first['task_id'], retry['task_id'])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(1, self.count('tasks'))
        self.assertEqual(1, self.count('ingress_requests'))

    def test_content_or_requested_project_agent_change_conflicts(self):
        payload = dict(self.payload, project_id='p', agent_id='specialist')
        self.submit(payload)
        before = self.snapshot()
        for change in ({'input': 'different'}, {'project_id': 'q'}, {'agent_id': 'other'}):
            with self.subTest(change=change), self.assertRaises(Conflict):
                self.submit(dict(payload, **change))
        self.assertEqual(before, self.snapshot())

    def test_optional_null_and_absent_labels_have_identical_meaning(self):
        first = self.submit()
        retry = self.submit(dict(self.payload, project_id=None, agent_id=None))
        self.assertEqual(first['task_id'], retry['task_id'])
        self.assertFalse(retry['task_created'])

    def test_open_project_and_agent_labels_are_metadata_not_native_override(self):
        receipt = self.submit(dict(self.payload, project_id='未来工具/陌生平台', agent_id='自由研究员'))
        context = json.loads(self.task(receipt['task_id'])['context'])
        self.assertEqual('未来工具/陌生平台', context['project_id'])
        self.assertEqual('自由研究员', context['ingress']['requested_agent_id'])
        self.assertEqual('leader:owner', context['session_scope'])
        self.assertNotIn('agent_id', context)
        self.assertNotIn('subject', context['ingress'])
        self.assertNotIn('subject-a', json.dumps(context))

    def test_body_cannot_choose_global_task_native_context_or_authority(self):
        denied = ('id', 'task_id', 'required', 'parent_id', 'context', 'session_scope',
                  'actor', 'role', 'node', 'source', 'subject', 'owner_id', 'scopes',
                  'credential', 'token_file', 'capabilities', 'scope', 'budget')
        before = self.snapshot()
        for key in denied:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.submit(dict(self.payload, **{key: 'operator'}))
        self.assertEqual(before, self.snapshot())

    def test_status_is_own_request_lookup_not_global_task_lookup(self):
        receipt = self.submit()
        for payload in ({'task_id': receipt['task_id']}, {'id': receipt['task_id']},
                        {'request_id': 'message-1', 'subject': 'subject-a'},
                        {'request_id': 'message-1', 'include_result': True}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.ingress.status(self.peer, payload)
        with self.assertRaises(PermissionError):
            self.status(receipt['task_id'])

    def test_other_subject_source_or_absent_request_has_same_denial(self):
        self.submit()
        for field, value in (('subject', 'subject-b'), ('source', 'fixture-cloud')):
            peer = copy.deepcopy(self.peer)
            peer['ingress'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                    PermissionError, '^ingress_request_not_visible$'):
                self.status(peer=peer)
        with self.assertRaisesRegex(PermissionError, '^ingress_request_not_visible$'):
            self.status('missing')

    def test_same_request_id_under_different_subject_or_source_is_distinct_work(self):
        first = self.submit()
        ids = {first['task_id']}
        for field, value in (('subject', 'subject-b'), ('source', 'fixture-cloud')):
            peer = copy.deepcopy(self.peer)
            peer['ingress'][field] = value
            receipt = self.submit(peer=peer)
            self.assertTrue(receipt['task_created'])
            ids.add(receipt['task_id'])
        self.assertEqual(3, len(ids))
        self.assertEqual(3, self.count('tasks'))

    def test_role_owner_and_peer_shape_cannot_be_elevated(self):
        peers = [{'role': role} for role in ('operator', 'viewer', 'worker', 'agent_peer', 'unknown')]
        peers += [dict(self.peer, node='cloud'), dict(self.peer, capabilities=['leader']),
                  dict(self.peer, score=100)]
        wrong_owner = copy.deepcopy(self.peer)
        wrong_owner['ingress']['owner_id'] = 'other-owner'
        peers.append(wrong_owner)
        before = self.snapshot()
        for peer in peers:
            for action in (self.submit, lambda peer: self.status(peer=peer)):
                with self.subTest(peer=peer), self.assertRaises(PermissionError):
                    action(peer=peer)
        self.assertEqual(before, self.snapshot())

    def test_scopes_rechecked_and_readonly_same_identity_survives_token_rotation(self):
        receipt = self.submit()
        read_only = copy.deepcopy(self.peer)
        read_only['token_file'] = '/fixture/private/replaced-token'
        read_only['ingress']['scopes'] = ['tasks:read']
        self.assertEqual(receipt['task_id'], self.status(peer=read_only)['task_id'])
        with self.assertRaises(PermissionError):
            self.submit(peer=read_only)
        write_only = copy.deepcopy(self.peer)
        write_only['ingress']['scopes'] = ['tasks:submit']
        self.assertFalse(self.submit(peer=write_only)['task_created'])
        with self.assertRaises(PermissionError):
            self.status(peer=write_only)
        for scopes in ([], ['tasks:submit'], ['tasks:read']):
            peer = copy.deepcopy(self.peer)
            peer['ingress']['scopes'] = scopes
            if not scopes:
                with self.assertRaises(PermissionError):
                    self.submit(peer=peer)
                with self.assertRaises(PermissionError):
                    self.status(peer=peer)

    def test_status_never_returns_private_results_checkpoint_native_or_machine_state(self):
        receipt = self.submit()
        with self.store.transaction() as db:
            db.execute('''UPDATE tasks SET status='needs_review',node=?,result=?,checkpoint=?
                WHERE id=?''', ('PRIVATE_NODE', 'PRIVATE_RESULT', json.dumps({
                    'thread_id': 'PRIVATE_THREAD', 'secret': 'PRIVATE_SECRET',
                    'side_effect_started': True}), receipt['task_id']))
        before = self.snapshot()
        value = self.status()
        self.assertEqual('needs_review', value['status'])
        self.assertTrue(value['result_available'])
        self.assertFalse(value['execution_verified'])
        self.assertNotIn('PRIVATE_', json.dumps(value))
        for key in ('node', 'epoch', 'scope', 'input', 'context', 'result', 'checkpoint', 'native'):
            self.assertNotIn(key, value)
        self.assertEqual(before, self.snapshot())

    def test_retry_unknown_effect_task_preserves_original_identity_and_checkpoint(self):
        first = self.submit()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='needs_review',epoch=7,checkpoint=? WHERE id=?",
                       (json.dumps({'side_effect_started': True, 'turn_id': 'original'}), first['task_id']))
        before = self.snapshot()
        retry = self.submit()
        self.assertEqual(first['task_id'], retry['task_id'])
        self.assertEqual('needs_review', retry['status'])
        self.assertFalse(retry['task_created'])
        self.assertEqual(before, self.snapshot())

    def test_unrecognized_task_status_is_not_reflected_as_arbitrary_private_text(self):
        receipt = self.submit()
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET status=? WHERE id=?', ('PRIVATE_FUTURE_STATE', receipt['task_id']))
        value = self.status()
        self.assertEqual('unrecognized', value['status'])
        self.assertFalse(value['status_recognized'])
        self.assertNotIn('PRIVATE_FUTURE_STATE', json.dumps(value))

    def test_mapping_failure_rolls_back_existing_store_task_creation(self):
        with self.store.transaction() as db:
            db.execute('''CREATE TRIGGER reject_ingress_binding BEFORE INSERT ON ingress_requests
                BEGIN SELECT RAISE(ABORT,'fixture binding failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit()
        self.assertEqual(0, self.count('tasks'))
        self.assertEqual(0, self.count('ingress_requests'))

    def test_task_creation_failure_or_wrong_return_rolls_back_without_binding(self):
        original = self.store._create_task_in_db
        def fail_after_creation(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError('fixture task failure')
        with mock.patch.object(self.store, '_create_task_in_db', fail_after_creation):
            with self.assertRaises(ValueError):
                self.submit()
        self.assertEqual(0, self.count('tasks'))
        self.assertEqual(0, self.count('ingress_requests'))
        def wrong_return(*args, **kwargs):
            original(*args, **kwargs)
            return 'unrelated'
        with mock.patch.object(self.store, '_create_task_in_db', wrong_return):
            with self.assertRaises(Conflict):
                self.submit()
        self.assertEqual(0, self.count('tasks'))
        self.assertEqual(0, self.count('ingress_requests'))

    def test_preexisting_derived_task_without_binding_is_not_adopted_even_identical(self):
        receipt = self.submit()
        with self.store.transaction() as db:
            db.execute('DELETE FROM ingress_requests')
        before = self.snapshot()
        with self.assertRaisesRegex(Conflict, '^ingress_task_identity_conflict$'):
            self.submit()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(1, self.count('tasks'))
        self.assertEqual(receipt['task_id'], self.task(receipt['task_id'])['id'])

    def test_missing_bound_task_does_not_recreate_or_return_other_global_task(self):
        self.submit()
        with self.store.transaction() as db:
            db.execute('DELETE FROM tasks')
        before = self.snapshot()
        with self.assertRaisesRegex(Conflict, '^ingress_task_binding_missing$'):
            self.status()
        with self.assertRaisesRegex(Conflict, '^ingress_task_binding_missing$'):
            self.submit()
        self.assertEqual(before, self.snapshot())

    def test_corrupted_mapping_task_or_fingerprint_fails_closed(self):
        receipt = self.submit()
        with self.store.transaction() as db:
            db.execute("UPDATE ingress_requests SET task_id='some-global-task'")
        with self.assertRaises(Conflict):
            self.status()
        with self.store.transaction() as db:
            db.execute('UPDATE ingress_requests SET task_id=?,fingerprint=?', (receipt['task_id'], '0' * 64))
        with self.assertRaises(Conflict):
            self.status()

    def test_restart_retains_same_binding_without_native_session_creation(self):
        first = self.submit()
        reopened_store = Store(self.store.path, clock=lambda: 1001.0, recover_inflight=False)
        reopened = Ingress(reopened_store, 'authority-fixture', self.config)
        value = reopened.submit(self.peer, self.payload)
        self.assertEqual(first['task_id'], value['task_id'])
        self.assertFalse(value['task_created'])
        self.assertEqual(0, self.count('native_sessions'))

    def test_two_ingress_instances_concurrent_duplicate_create_once(self):
        other_store = Store(self.store.path, clock=lambda: 1000.0, recover_inflight=False)
        other = Ingress(other_store, 'authority-fixture', self.config)
        barrier = threading.Barrier(2)
        def submit(ingress):
            barrier.wait(timeout=10)
            return ingress.submit(copy.deepcopy(self.peer), dict(self.payload))
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(submit, (self.ingress, other)))
        self.assertEqual(receipts[0]['task_id'], receipts[1]['task_id'])
        self.assertEqual([False, True], sorted(row['task_created'] for row in receipts))
        self.assertEqual(1, self.count('tasks'))
        self.assertEqual(1, self.count('ingress_requests'))

    def test_new_deployment_task_policy_never_retargets_old_request(self):
        first = self.submit()
        old_task = self.task(first['task_id'])
        changed = Ingress(self.store, 'authority-fixture', dict(
            self.config, required=['agent', 'mesh.node:fixture-node'], session_scope='owner-configured-scope'))
        retry = changed.submit(self.peer, self.payload)
        self.assertFalse(retry['task_created'])
        self.assertEqual(old_task, self.task(first['task_id']))
        new = changed.submit(self.peer, dict(self.payload, request_id='message-2'))
        new_task = self.task(new['task_id'])
        self.assertEqual(['agent', 'mesh.node:fixture-node'], json.loads(new_task['required']))
        self.assertEqual('owner-configured-scope', new_task['scope'])

    def test_authority_or_owner_namespace_change_rejected_without_writes(self):
        self.submit()
        before = self.snapshot()
        for authority, config in (('other-authority', self.config),
                                  ('authority-fixture', {'owner_id': 'other-owner'})):
            with self.subTest(authority=authority, config=config), self.assertRaises(Conflict):
                Ingress(self.store, authority, config)
        self.assertEqual(before, self.snapshot())

    def test_missing_namespace_for_existing_bindings_rejected(self):
        self.submit()
        with self.store.transaction() as db:
            db.execute('DELETE FROM ingress_metadata')
        with self.assertRaisesRegex(Conflict, '^ingress_namespace_missing$'):
            Ingress(self.store, 'authority-fixture', self.config)

    def test_explicit_plan_goal_commands_keep_store_semantics_without_session_override(self):
        for index, text in enumerate(('/plan 明确依赖与验收', '/goal 推进我的开放目标')):
            receipt = self.submit(dict(self.payload, request_id='command-' + str(index), input=text))
            context = json.loads(self.task(receipt['task_id'])['context'])
            self.assertEqual('leader:owner', context['session_scope'])
            if index == 0:
                self.assertEqual('plan', context['mode'])
            else:
                self.assertEqual('推进我的开放目标', context['goal']['objective'])
        with self.assertRaises(ValueError):
            self.submit(dict(self.payload, request_id='invalid-goal', input='/goal '))
        self.assertEqual(2, self.count('tasks'))

    def test_submit_validation_bounds_types_and_invalid_unicode_without_rows(self):
        payloads = [None, [], {}, {'request_id': 'x'}, {'input': 'x'},
                    dict(self.payload, request_id=''), dict(self.payload, request_id=' padded '),
                    dict(self.payload, request_id='é' * 101), dict(self.payload, request_id='\ud800'),
                    dict(self.payload, input=None), dict(self.payload, input='   '),
                    dict(self.payload, input='\ud800'), dict(self.payload, input='x' * 65537),
                    dict(self.payload, project_id={}), dict(self.payload, agent_id=True)]
        before = self.snapshot()
        for payload in payloads:
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(ValueError):
                self.ingress.submit(self.peer, payload)
        self.assertEqual(before, self.snapshot())
        receipt = self.submit(dict(self.payload, request_id='é' * 100, input='x' * 65536))
        self.assertTrue(receipt['task_created'])

    def test_validation_pure_detached_and_config_mutation_cannot_change_active_policy(self):
        config = dict(self.config, required=['leader'])
        normalized = validate_config(config)
        normalized['required'].append('agent')
        self.assertEqual(['leader'], config['required'])
        bound = validate_peer(self.peer, 'owner-fixture')
        bound['scopes'].clear()
        self.assertEqual(2, len(self.peer['ingress']['scopes']))
        self.config['owner_id'] = 'mutated'
        receipt = self.submit()
        self.assertEqual('leader:owner', self.task(receipt['task_id'])['scope'])

    def test_strict_config_validation_before_runtime_tables(self):
        invalid = [None, [], {}, {'owner_id': 'owner', 'secret': 'forbidden'},
                   {'owner_id': ''}, {'owner_id': '\ud800'},
                   {'owner_id': 'owner', 'required': []},
                   {'owner_id': 'owner', 'required': ['leader', 'leader']},
                   {'owner_id': 'owner', 'required': [True]},
                   {'owner_id': 'owner', 'required': ['\x00bad']},
                   {'owner_id': 'owner', 'session_scope': ''},
                   {'owner_id': 'owner', 'session_scope': 'x' * 257}]
        other_store = Store(Path(self.temporary.name) / 'untouched.sqlite', recover_inflight=False)
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                Ingress(other_store, 'authority-fixture', config)
        with other_store.transaction() as db:
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name='ingress_requests'").fetchone())

    def test_strict_nested_peer_scope_shape_and_private_token_file_validation(self):
        variants = []
        for scopes in ('tasks:read', ['operator'], ['tasks:read', 'tasks:read'], [True]):
            peer = copy.deepcopy(self.peer)
            peer['ingress']['scopes'] = scopes
            variants.append(peer)
        for extra in ({'actor': 'operator'}, {'issuer': 'payload-issuer'}, {'token': 'secret'}):
            peer = copy.deepcopy(self.peer)
            peer['ingress'].update(extra)
            variants.append(peer)
        for field, value in (('source', ''), ('subject', 'x' * 1025), ('subject', '\ud800')):
            peer = copy.deepcopy(self.peer)
            peer['ingress'][field] = value
            variants.append(peer)
        variants += [dict(self.peer, ingress=None), dict(self.peer, token_file={'secret': 'forbidden'})]
        before = self.snapshot()
        for peer in variants:
            with self.subTest(peer=peer), self.assertRaises(ValueError):
                self.submit(peer=peer)
        self.assertEqual(before, self.snapshot())

    def test_unauthorized_role_checked_before_payload_and_never_opens_transaction(self):
        with mock.patch.object(self.store, 'transaction', side_effect=AssertionError('must not open')):
            for payload in (None, {}, {'request_id': 'missing'}):
                with self.subTest(payload=payload), self.assertRaises(PermissionError):
                    self.ingress.submit({'role': 'operator'}, payload)
                with self.subTest(payload=payload), self.assertRaises(PermissionError):
                    self.ingress.status({'role': 'worker', 'node': 'cloud'}, payload)


if __name__ == '__main__':
    unittest.main()
