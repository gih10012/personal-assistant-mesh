import json
import tempfile
import threading
import unittest
from pathlib import Path

from assistant_mesh.networking import Network, PROTOCOL, digest
from assistant_mesh.remote import Remote
from assistant_mesh.store import Conflict, Store


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.store = Store(Path(self.tmp.name) / 'local.db', clock=lambda: self.now)
        self.network = Network(self.store, 'cloud')
        self.remote = Remote(self.store, self.network)
        self.store.heartbeat('cloud', ['leader', 'agent'])
        self.parent = self.store.create_task('owner parent', context={'project_id': 'owner-project'})
        self.task = self.store.claim('cloud')
        self.epoch = self.task['epoch']

    def tearDown(self):
        self.tmp.cleanup()

    def delegate(self, call='call-1', peer='laptop', **arguments):
        args = {'input': 'remote task'}
        args.update(arguments)
        return self.remote.delegate(self.parent, 'cloud', self.epoch, call, peer, args)

    def accept(self, child):
        delivery = self.network.claim_delivery(child['peer'])
        remote_id = digest([child['peer'], 'cloud', child['message_id']])
        receipt = {'protocol': PROTOCOL, 'id': child['message_id'],
                   'fingerprint': delivery['fingerprint'], 'task_id': remote_id, 'state': 'accepted'}
        self.network.finish_delivery(delivery, receipt)
        return remote_id

    def reconcile(self, child, remote_id, status='completed', result='actual remote answer'):
        return self.remote.reconcile(child['peer'], child['message_id'], remote_id, status, result)

    def count(self, table):
        with self.store.transaction() as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_delegate_replay_creates_only_one_existing_ledger_child(self):
        first = self.delegate()
        self.assertEqual(first, self.delegate())
        self.assertEqual(2, self.count('tasks'))
        for table in ('remote_delegations', 'mesh_deliveries', 'agent_actions'):
            self.assertEqual(1, self.count(table))
        child = self.store.task_status(first['id'])
        self.assertEqual('waiting_remote', child['status'])
        children = self.store.agent_action(self.parent, 'cloud', self.epoch, 'children', 'children', {})
        self.assertEqual(first['id'], children['tasks'][0]['id'])
        self.assertIsNone(self.store.claim('cloud'))

    def test_changed_arguments_or_peer_cannot_reuse_action_id(self):
        self.delegate()
        for change in ({'input': 'changed'}, {'project_id': 'another'}, {'peer': 'another-node'}):
            with self.assertRaises(Conflict):
                self.delegate(**change)
        self.assertEqual(1, self.count('remote_delegations'))

    def test_stale_task_lease_rejects_new_and_replayed_calls(self):
        self.delegate()
        self.now += 91
        for call in ('call-1', 'call-2'):
            with self.assertRaises(Conflict):
                self.delegate(call=call)
        self.assertEqual(1, self.count('mesh_deliveries'))

    def test_stale_leader_with_live_task_lease_cannot_delegate(self):
        with self.store.transaction() as db:
            db.execute('UPDATE leader SET deadline=?', (self.now,))
        with self.assertRaises(Conflict):
            self.delegate()
        self.assertEqual(0, self.count('remote_delegations'))

    def test_wrong_node_or_epoch_cannot_delegate(self):
        for node, epoch in (('other', self.epoch), ('cloud', self.epoch + 1)):
            with self.assertRaises(Conflict):
                self.remote.delegate(self.parent, node, epoch, 'call', 'laptop', {'input': 'test'})
        self.assertEqual(0, self.count('mesh_deliveries'))

    def test_delegate_and_receipt_are_atomic_on_commit_failure(self):
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_action BEFORE INSERT ON agent_actions BEGIN SELECT RAISE(ABORT,'forced rollback'); END")
        with self.assertRaises(Exception):
            self.delegate()
        self.assertEqual(1, self.count('tasks'))
        for table in ('remote_delegations', 'mesh_deliveries', 'agent_actions'):
            self.assertEqual(0, self.count(table))

    def test_raw_queue_id_collision_cannot_be_promoted_to_child(self):
        message_id = digest(['remote-message', 'cloud', self.parent, 'call-1'])
        self.network.enqueue('laptop', {'protocol': PROTOCOL, 'id': message_id, 'to': 'laptop',
                                        'input': 'raw', 'project': 'p', 'agent': 'a'})
        with self.assertRaises(Conflict):
            self.delegate()
        self.assertEqual(1, self.count('tasks'))
        self.assertEqual(0, self.count('remote_delegations'))

    def test_project_agent_are_model_selected_without_colon_aliasing(self):
        first = self.delegate(project_id='x:y', agent_id='z', role='new specialist type')
        self.now += 0.1
        second = self.delegate(call='other-call', project_id='x', agent_id='y:z')
        self.assertNotEqual(self.store.task_status(first['id'])['scope'], self.store.task_status(second['id'])['scope'])
        delivery = self.network.claim_delivery('laptop')
        self.assertEqual('x:y', delivery['message']['project'])
        self.assertEqual('z', delivery['message']['agent'])
        self.assertEqual(first['id'], delivery['message']['parent_ref'])

    def test_remote_argument_authority_injection_and_ambiguous_identity_rejected(self):
        for arguments in ({'context': {'session_scope': 'leader:owner'}}, {'node': 'owner'},
                          {'project': 'a', 'project_id': 'b'}, {'agent': 'a', 'agent_id': 'b'},
                          {'required': ['leader']}, {'input': ''}, {'input': 'x' * 65537}):
            with self.assertRaises(ValueError):
                self.delegate(**arguments)
        with self.assertRaises(ValueError):
            self.delegate(peer='cloud')
        self.assertEqual(0, self.count('mesh_deliveries'))

    def test_completed_remote_child_wakes_same_parent_and_keeps_native_session(self):
        self.store.session_action(self.parent, 'cloud', self.epoch, 'commit',
                                 {'harness': 'codex', 'state': {'thread_id': 'original-parent-thread'}, 'parts': 0})
        child = self.delegate()
        remote_id = self.accept(child)
        self.store.update_task(self.parent, 'cloud', self.epoch, status='waiting_children')
        result = self.reconcile(child, remote_id)
        self.assertTrue(result['parent_woken'])
        resumed = self.store.claim('cloud')
        self.assertEqual(self.parent, resumed['id'])
        self.assertEqual('actual remote answer', resumed['children'][0]['result'])
        self.assertEqual('original-parent-thread', resumed['sessions']['codex']['state']['thread_id'])
        self.assertEqual({}, self.store.status()['outbox'])

    def test_result_before_parent_yields_does_not_lose_wakeup(self):
        child = self.delegate()
        self.assertFalse(self.reconcile(child, self.accept(child))['parent_woken'])
        self.store.update_task(self.parent, 'cloud', self.epoch, status='waiting_children')
        self.assertEqual('pending', self.store.task_status(self.parent)['status'])

    def test_original_parent_lease_may_expire_before_valid_result_arrives(self):
        child = self.delegate()
        remote_id = self.accept(child)
        self.store.update_task(self.parent, 'cloud', self.epoch, status='waiting_children')
        self.now += 1000
        self.assertTrue(self.reconcile(child, remote_id)['parent_woken'])
        self.store.heartbeat('new-leader', ['leader', 'agent'])
        resumed = self.store.claim('new-leader')
        self.assertEqual(self.parent, resumed['id'])
        self.assertEqual('actual remote answer', resumed['children'][0]['result'])

    def test_paused_parent_stays_paused_when_remote_finishes(self):
        child = self.delegate()
        remote_id = self.accept(child)
        self.store.update_task(self.parent, 'cloud', self.epoch, status='waiting_children')
        self.store.control_task(self.parent, 'pause')
        self.assertFalse(self.reconcile(child, remote_id)['parent_woken'])
        self.assertEqual('paused', self.store.task_status(self.parent)['status'])
        self.assertIsNone(self.store.claim('cloud'))
        self.assertEqual('pending', self.store.control_task(self.parent, 'resume')['status'])
        self.assertEqual(self.parent, self.store.claim('cloud')['id'])

    def test_wait_for_all_remote_and_local_children(self):
        remote_child = self.delegate()
        remote_id = self.accept(remote_child)
        local_child = self.store.agent_action(self.parent, 'cloud', self.epoch, 'local-child',
                                             'delegate', {'input': 'local child'})
        self.store.update_task(self.parent, 'cloud', self.epoch, status='waiting_children')
        self.assertFalse(self.reconcile(remote_child, remote_id)['parent_woken'])
        self.assertEqual('waiting_children', self.store.task_status(self.parent)['status'])
        task = self.store.claim('cloud')
        self.assertEqual(local_child['id'], task['id'])
        self.store.update_task(task['id'], 'cloud', task['epoch'], result='local answer', status='completed')
        self.assertEqual(self.parent, self.store.claim('cloud')['id'])

    def test_active_remote_status_only_updates_existing_proxy_observation(self):
        child = self.delegate()
        remote_id = self.accept(child)
        for status in ('running', 'waiting_auth', 'unknown', 'paused'):
            result = self.reconcile(child, remote_id, status, 'partial observation')
            self.assertFalse(result['terminal'])
            self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])
        self.assertEqual(2, self.count('tasks'))
        self.assertEqual(1, self.count('mesh_deliveries'))

    def test_unknown_delivery_outcome_cannot_settle_child_or_create_second_delivery(self):
        child = self.delegate()
        attempt = self.network.claim_delivery('laptop')
        self.network.finish_delivery(attempt)
        with self.assertRaises(Conflict):
            self.reconcile(child, 'unverified-task')
        self.assertEqual(1, self.count('mesh_deliveries'))
        retry = self.network.claim_delivery('laptop')
        self.assertEqual(attempt['message'], retry['message'])
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])

    def test_wrong_peer_unmapped_or_wrong_task_never_changes_child(self):
        child = self.delegate()
        remote_id = self.accept(child)
        self.assertFalse(self.remote.reconcile('stranger', child['message_id'], remote_id, 'completed', 'spoof')['reconciled'])
        self.assertFalse(self.remote.reconcile('laptop', 'unmapped', remote_id, 'completed', 'spoof')['reconciled'])
        with self.assertRaises(PermissionError):
            self.reconcile(child, 'wrong-remote-task', result='spoof')
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])

    def test_tampered_persisted_acceptance_receipt_rejected(self):
        child = self.delegate()
        remote_id = self.accept(child)
        with self.store.transaction() as db:
            db.execute('UPDATE mesh_deliveries SET response=?',
                       (json.dumps({'protocol': PROTOCOL, 'id': child['message_id'], 'task_id': remote_id,
                                    'state': 'accepted', 'fingerprint': 'tampered'}),))
        with self.assertRaises(PermissionError):
            self.reconcile(child, remote_id)
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])

    def test_terminal_result_immutable_and_stale_active_poll_cannot_undo_it(self):
        child = self.delegate()
        remote_id = self.accept(child)
        first = self.reconcile(child, remote_id)
        self.assertFalse(first['duplicate'])
        self.assertTrue(self.reconcile(child, remote_id)['duplicate'])
        stale = self.reconcile(child, remote_id, 'running', 'older partial output')
        self.assertTrue(stale['duplicate'])
        self.assertEqual('completed', stale['status'])
        for status, result in (('failed', 'different terminal'), ('completed', 'different answer')):
            with self.assertRaises(Conflict):
                self.reconcile(child, remote_id, status, result)
        self.assertEqual('actual remote answer', self.store.task_status(child['id'])['result'])

    def test_failed_and_needs_review_are_true_remote_terminal_states(self):
        for index, status in enumerate(('failed', 'needs_review')):
            child = self.delegate(call='call-' + str(index))
            result = self.reconcile(child, self.accept(child), status, status + ' evidence')
            self.assertTrue(result['terminal'])
            self.assertEqual(status, self.store.task_status(child['id'])['status'])

    def test_untrusted_report_never_completes_proxy_child(self):
        child = self.delegate()
        self.network.report('laptop', 'unsolicited-report', {'task_id': child['id'], 'project': 'p',
                                                            'status': 'completed', 'result': 'fabricated'})
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])
        self.assertIsNone(self.store.task_status(child['id'])['result'])

    def test_result_limits_and_unknown_status_are_enforced_before_mutation(self):
        child = self.delegate()
        remote_id = self.accept(child)
        for status, result in (('fake-complete', 'text'), ('completed', {}), ('completed', 'x' * 65537)):
            with self.assertRaises(ValueError):
                self.reconcile(child, remote_id, status, result)
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])

    def test_reconciliation_mutation_and_parent_wakeup_roll_back_together(self):
        child = self.delegate()
        remote_id = self.accept(child)
        self.store.update_task(self.parent, 'cloud', self.epoch, status='waiting_children')
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_parent BEFORE UPDATE OF status ON tasks WHEN OLD.id='" + self.parent + "' BEGIN SELECT RAISE(ABORT,'forced rollback'); END")
        with self.assertRaises(Exception):
            self.reconcile(child, remote_id)
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])
        with self.store.transaction() as db:
            row = db.execute('SELECT terminal,remote_task_id FROM remote_delegations').fetchone()
        self.assertEqual(0, row['terminal'])
        self.assertIsNone(row['remote_task_id'])

    def test_concurrent_delegate_replays_share_single_child_and_outbox(self):
        results, errors = [], []
        def run():
            try:
                results.append(self.delegate())
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual([results[0]] * 4, results)
        self.assertEqual(2, self.count('tasks'))
        self.assertEqual(1, self.count('mesh_deliveries'))

    def test_concurrent_reconcile_wakes_parent_once(self):
        child = self.delegate()
        remote_id = self.accept(child)
        self.store.update_task(self.parent, 'cloud', self.epoch, status='waiting_children')
        results, errors = [], []
        def run():
            try:
                results.append(self.reconcile(child, remote_id))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual(1, sum(result['parent_woken'] for result in results))
        self.assertEqual(3, sum(result['duplicate'] for result in results))

    def test_first_definite_denial_settles_review_and_wakes_original_parent(self):
        child = self.delegate()
        self.store.update_task(self.parent, 'cloud', self.epoch, status='waiting_children')
        attempt = self.network.claim_delivery('laptop')
        self.network.reject_delivery(attempt)
        outcome = self.remote.reject('laptop', child['message_id'])
        self.assertTrue(outcome['parent_woken'])
        self.assertEqual('needs_review', outcome['status'])
        self.assertEqual('远端权限拒绝，任务未交付；需本人核对授权', self.store.task_status(child['id'])['result'])
        self.assertIsNone(self.network.claim_delivery('laptop'))
        repeated = self.remote.reject('laptop', child['message_id'])
        self.assertTrue(repeated['duplicate'])
        self.assertFalse(repeated['parent_woken'])

    def test_first_denial_does_not_wake_paused_parent(self):
        child = self.delegate()
        self.store.update_task(self.parent, 'cloud', self.epoch, status='waiting_children')
        self.store.control_task(self.parent, 'pause')
        self.network.reject_delivery(self.network.claim_delivery('laptop'))
        self.assertFalse(self.remote.reject('laptop', child['message_id'])['parent_woken'])
        self.assertEqual('paused', self.store.task_status(self.parent)['status'])

    def test_unknown_or_pending_delivery_is_not_non_acceptance_proof(self):
        child = self.delegate()
        self.assertFalse(self.remote.reject('laptop', child['message_id'])['reconciled'])
        attempt = self.network.claim_delivery('laptop')
        self.assertFalse(self.remote.reject('laptop', child['message_id'])['reconciled'])
        self.network.finish_delivery(attempt)
        self.assertFalse(self.remote.reject('laptop', child['message_id'])['reconciled'])
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])

    def test_denial_after_unknown_receipt_never_claims_task_was_not_delivered(self):
        child = self.delegate()
        attempt = self.network.claim_delivery('laptop')
        self.network.finish_delivery(attempt)  # may have been accepted remotely
        retry = self.network.claim_delivery('laptop')
        self.network.reject_delivery(retry)  # grant was revoked before retry
        self.assertFalse(self.remote.reject('laptop', child['message_id'])['reconciled'])
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])
        self.assertIsNone(self.store.task_status(child['id'])['result'])

    def test_accepted_execution_cannot_be_converted_into_non_delivery(self):
        child = self.delegate()
        self.accept(child)
        self.assertFalse(self.remote.reject('laptop', child['message_id'])['reconciled'])
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])

    def test_unmapped_denial_and_invalid_reason_never_settle_child(self):
        child = self.delegate()
        self.assertFalse(self.remote.reject('other', child['message_id'])['reconciled'])
        with self.store.transaction() as db:
            db.execute("UPDATE mesh_deliveries SET state='denied',attempt=1,response=?",
                       (json.dumps({'reason': 'authentication_rejected'}),))
        self.assertFalse(self.remote.reject('laptop', child['message_id'])['reconciled'])
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])

    def test_denied_without_explicit_non_acceptance_proof_is_not_completion(self):
        child = self.delegate()
        for proof in (None, False, 'true', 1):
            with self.store.transaction() as db:
                db.execute("UPDATE mesh_deliveries SET state='denied',attempt=1,response=?",
                           (json.dumps({'reason': 'authorization_rejected', 'delivery_not_accepted': proof}),))
            self.assertFalse(self.remote.reject('laptop', child['message_id'])['reconciled'])
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])

    def test_proxy_cannot_be_resumed_locally_without_remote_control_protocol(self):
        child = self.delegate()
        for operation in ('pause', 'resume'):
            with self.assertRaises(Conflict):
                self.store.control_task(child['id'], operation)
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])
        self.assertIsNone(self.store.claim('cloud'))

    def test_definite_rejection_and_parent_wakeup_roll_back_together(self):
        child = self.delegate()
        self.store.update_task(self.parent, 'cloud', self.epoch, status='waiting_children')
        self.network.reject_delivery(self.network.claim_delivery('laptop'))
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_denied_parent BEFORE UPDATE OF status ON tasks WHEN OLD.id='" + self.parent + "' BEGIN SELECT RAISE(ABORT,'forced rollback'); END")
        with self.assertRaises(Exception):
            self.remote.reject('laptop', child['message_id'])
        self.assertEqual('waiting_remote', self.store.task_status(child['id'])['status'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT terminal FROM remote_delegations').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
