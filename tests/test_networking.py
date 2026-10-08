import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.networking import Network, PROTOCOL, SSHExecution, digest, native_scope
from assistant_mesh.server import API
from assistant_mesh.store import Conflict, Store


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.store = Store(Path(self.tmp.name) / 'node.db', lambda: self.now)
        self.net = Network(self.store, 'laptop')
        self.message = {'protocol': PROTOCOL, 'id': 'msg1', 'to': 'laptop', 'input': 'bounded task',
                        'project': 'project1', 'agent': 'specialist'}

    def tearDown(self):
        self.tmp.cleanup()

    def hello(self, epoch=2, node='cloud-leader', authority='cloud'):
        return {'node': 'cloud', 'authority': authority, 'protocol': PROTOCOL,
                'leader': {'node': node, 'epoch': epoch, 'remaining_seconds': 50}}

    def test_link_lease_uses_local_clock_and_does_not_elect(self):
        self.net.observe_link('cloud', ok=True, hello=self.hello())
        self.assertEqual('connected', self.net.links()['mode'])
        self.now += 51
        state = self.net.links()
        self.assertEqual('autonomous', state['mode'])
        self.assertTrue(state['local_work_allowed'])
        self.assertFalse(state['global_takeover_allowed'])
        self.assertIsNone(self.store.status()['leader']['node'])

    def test_peer_and_protocol_identity(self):
        for hello in ({'node': 'impostor', 'protocol': PROTOCOL}, {'node': 'cloud', 'protocol': 'other'}):
            with self.assertRaises(ValueError):
                self.net.observe_link('cloud', ok=True, hello=hello)

    def test_failure_backoff_survives_restart_and_reset(self):
        self.net.observe_link('cloud', ok=False)
        self.assertFalse(Network(self.store, 'laptop').probe_due('cloud'))
        self.now += 4
        self.assertTrue(self.net.probe_due('cloud'))
        self.net.observe_link('cloud', ok=False)
        self.assertEqual(2, self.net.links()['links'][0]['failures'])
        self.net.observe_link('cloud', ok=True, hello=self.hello())
        self.assertEqual(0, self.net.links()['links'][0]['failures'])

    def test_stale_or_conflicting_leader_does_not_overwrite(self):
        self.net.observe_link('cloud', ok=True, hello=self.hello())
        for hello in (self.hello(epoch=1), self.hello(node='split-brain')):
            with self.assertRaises(Conflict):
                self.net.observe_link('cloud', ok=True, hello=hello)
        self.assertEqual('cloud-leader', self.net.links()['links'][0]['leader']['node'])

    def test_execution_reachability_is_not_agent_or_leader(self):
        self.net.observe_link('cloud', kind='execution', ok=True, hello={'node': 'cloud'})
        row = self.net.links()['links'][0]
        self.assertTrue(row['reachable'])
        self.assertFalse(row['leader_available'])
        self.assertEqual('autonomous', self.net.links()['mode'])

    def test_message_idempotency_and_content_conflict(self):
        first = self.net.receive('cloud', self.message)
        self.assertEqual(first, Network(self.store, 'laptop').receive('cloud', self.message))
        self.assertEqual(1, self.store.status()['tasks']['pending'])
        with self.assertRaises(Conflict):
            self.net.receive('cloud', dict(self.message, input='different'))

    def test_different_senders_cannot_alias_or_read_tasks(self):
        first = self.net.receive('cloud', self.message)
        second = self.net.receive('other', self.message)
        self.assertNotEqual(first['task_id'], second['task_id'])
        self.assertNotEqual(self.store.task_status(first['task_id'])['scope'], self.store.task_status(second['task_id'])['scope'])
        with self.assertRaises(PermissionError):
            self.net.task_for_sender('stranger', 'msg1')
        self.assertEqual(first['task_id'], self.net.task_for_sender('cloud', 'msg1')['task_id'])

    def test_wrong_destination_and_authority_fields_rejected(self):
        with self.assertRaises(PermissionError):
            self.net.receive('cloud', dict(self.message, to='wrong-node'))
        for key in ('from', 'node', 'role', 'session_scope', 'cwd', 'token', 'context'):
            with self.assertRaises(ValueError):
                self.net.receive('cloud', dict(self.message, **{key: 'owner'}))

    def test_local_scope_is_continuous_but_never_global_leader(self):
        first = self.net.receive('cloud', self.message)
        second = self.net.receive('cloud', dict(self.message, id='msg2'))
        one, two = self.store.task_status(first['task_id']), self.store.task_status(second['task_id'])
        self.assertEqual(one['scope'], two['scope'])
        self.assertNotEqual('leader:owner', one['scope'])
        self.assertNotEqual(native_scope('laptop', 'x:y', 'z'), native_scope('laptop', 'x', 'y:z'))
        self.assertNotEqual(native_scope('laptop', 'p', 'a'), native_scope('cloud', 'p', 'a'))

    def test_local_task_claim_works_without_remote_leader(self):
        receipt = self.net.receive('operator', self.message)
        self.store.heartbeat('laptop', ['agent', 'mesh.node:laptop'])
        task = self.store.claim('laptop')
        self.assertEqual(receipt['task_id'], task['id'])
        self.assertEqual('local:laptop', task['context']['authority'])
        self.assertIsNone(self.store.status()['leader']['node'])

    def test_agent_task_cannot_escape_destination_node(self):
        self.net.receive('operator', self.message)
        self.store.heartbeat('other', ['agent'])
        self.assertIsNone(self.store.claim('other'))
        self.store.heartbeat('laptop', ['agent', 'mesh.node:laptop'])
        self.assertIsNotNone(self.store.claim('laptop'))

    def test_remote_peer_cannot_read_private_owner_memories_or_prior_peer(self):
        with self.store.transaction() as db:
            db.execute('INSERT INTO memories VALUES(?,?,?,?)', ('owner-memory', 'owner', 'private sentinel', self.now))
        self.store.heartbeat('laptop', ['agent', 'mesh.node:laptop'])
        self.net.receive('peer-a', self.message)
        task = self.store.claim('laptop')
        self.assertEqual([], task['memories'])
        self.store.session_action(task['id'], 'laptop', task['epoch'], 'commit',
                                  {'state': {'thread_id': 'private-peer-a-thread'}, 'harness': 'codex', 'parts': 0})
        self.store.agent_action(task['id'], 'laptop', task['epoch'], 'remember-a', 'remember', {'text': 'peer-a-private'})
        recalled = self.store.agent_action(task['id'], 'laptop', task['epoch'], 'recall-a', 'recall', {})
        self.assertEqual(['peer-a-private'], [m['text'] for m in recalled['memories']])
        self.store.update_task(task['id'], 'laptop', task['epoch'], status='completed')
        self.net.receive('peer-b', self.message)
        other = self.store.claim('laptop')
        self.assertEqual([], other['memories'])
        self.assertIsNone(other.get('session'))
        recalled = self.store.agent_action(other['id'], 'laptop', other['epoch'], 'recall-b', 'recall', {})
        self.assertEqual([], recalled['memories'])

    def test_native_plan_goal_survive_a2a(self):
        for prefix in ('/plan ', '/goal '):
            receipt = self.net.receive('cloud', dict(self.message, id=prefix.strip(), input=prefix + 'test objective'))
            with self.store.transaction() as db:
                context = json.loads(db.execute('SELECT context FROM tasks WHERE id=?', (receipt['task_id'],)).fetchone()[0])
            self.assertEqual('plan' if prefix == '/plan ' else 'active', context.get('mode') or context['goal']['status'])

    def test_receipt_and_task_rollback_together(self):
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON mesh_messages BEGIN SELECT RAISE(ABORT,'fail'); END")
        with self.assertRaises(Exception):
            self.net.receive('cloud', self.message)
        self.assertEqual({}, self.store.status()['tasks'])

    def test_parallel_duplicate_receives_create_single_task(self):
        errors = []
        def receive():
            try:
                self.net.receive('cloud', self.message)
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=receive) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual([], errors)
        self.assertEqual(1, self.store.status()['tasks']['pending'])

    def test_delivery_uncertain_retries_identical_id_not_duplicate_execution(self):
        self.net.enqueue('laptop', self.message)
        attempt = self.net.claim_delivery('laptop')
        receipt = self.net.receive('cloud', attempt['message'])
        self.net.finish_delivery(attempt)  # receipt lost after acceptance
        retry = self.net.claim_delivery('laptop')
        self.assertEqual(attempt['message'], retry['message'])
        self.assertEqual(receipt, self.net.receive('cloud', retry['message']))
        self.net.finish_delivery(retry, receipt)
        self.assertIsNone(self.net.claim_delivery('laptop'))
        self.assertEqual(1, self.store.status()['tasks']['pending'])

    def test_delivery_claim_and_ack_fenced(self):
        self.net.enqueue('laptop', self.message)
        old = self.net.claim_delivery('laptop')
        self.assertIsNone(self.net.claim_delivery('laptop'))
        self.now += 31
        newer = self.net.claim_delivery('laptop')
        with self.assertRaises(Conflict):
            self.net.finish_delivery(old)
        with self.assertRaises(ValueError):
            self.net.finish_delivery(newer, {'id': 'different'})
        self.net.finish_delivery(newer)

    def test_definite_refusal_is_not_retried(self):
        self.net.enqueue('laptop', self.message)
        attempt = self.net.claim_delivery('laptop')
        self.net.reject_delivery(attempt)
        self.assertIsNone(self.net.claim_delivery('laptop'))
        with self.assertRaises(Conflict):
            self.net.finish_delivery(attempt)

    def test_refusal_after_lost_receipt_does_not_prove_non_execution(self):
        self.net.enqueue('laptop', self.message)
        first = self.net.claim_delivery('laptop')
        receipt = self.net.receive('cloud', first['message'])
        self.net.finish_delivery(first)  # receipt lost: remote task exists
        second = self.net.claim_delivery('laptop')
        refusal = self.net.reject_delivery(second)
        self.assertEqual('unknown', refusal['state'])
        self.assertFalse(refusal['delivery_not_accepted'])
        third = self.net.claim_delivery('laptop')
        self.assertEqual(second['message'], third['message'])
        self.net.finish_delivery(third, receipt)
        self.assertIsNone(self.net.claim_delivery('laptop'))
        self.assertEqual(1, self.store.status()['tasks']['pending'])

    def test_report_reconciliation_never_creates_or_replays_task(self):
        body = {'task_id': 'local-task', 'project': 'p', 'status': 'completed', 'result': 'evidence'}
        self.net.report('cloud', 'r1', body)
        self.net.report('cloud', 'r1', body)
        self.assertEqual({}, self.store.status()['tasks'])
        with self.assertRaises(Conflict):
            self.net.report('cloud', 'r1', dict(body, result='different'))

    def test_execution_unknown_survives_restart_and_never_auto_retries(self):
        args, scope = ['test', 'literal; not local shell'], {'target': 'cloud'}
        self.assertTrue(self.net.begin_execution('cloud', 'e1', args, scope)['new'])
        retry = Network(self.store, 'laptop').begin_execution('cloud', 'e1', args, scope)
        self.assertFalse(retry['new'])
        self.assertEqual('unknown', retry['state'])
        with self.assertRaises(Conflict):
            self.net.begin_execution('cloud', 'e1', ['different'], scope)

    def test_execution_permission_and_scope_checked_before_journal_or_launch(self):
        runner = SSHExecution(self.net, {'cloud': {'execution_allowed': True, 'ssh_alias': 'ai-server',
                                                 'execution_scope': {'target': 'cloud'}}})
        with patch('assistant_mesh.networking.subprocess.Popen') as launch:
            with self.assertRaises(PermissionError):
                runner.execute('cloud', 'e1', ['id'], {'target': 'other'})
            with self.assertRaises(PermissionError):
                runner.execute('unknown', 'e1', ['id'], {'target': 'cloud'})
            launch.assert_not_called()
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM mesh_executions').fetchone()[0])

    def test_ssh_argv_safe_and_stable_retry_does_not_launch(self):
        runner = SSHExecution(self.net, {'cloud': {'execution_allowed': True, 'ssh_alias': 'ai-server',
                                                 'execution_scope': {'target': 'cloud'}}}, ssh_config='/private/ssh-config')
        with patch('assistant_mesh.networking.subprocess.Popen') as launch:
            launch.return_value.returncode = 0
            runner.execute('cloud', 'e1', ['printf', '%s', 'literal; $(never-local)'], {'target': 'cloud'})
            args = launch.call_args[0][0]
            self.assertIn('StrictHostKeyChecking=yes', args)
            self.assertEqual(['--', 'ai-server', "printf %s 'literal; $(never-local)'"], args[-3:])
            self.assertNotIn('shell', launch.call_args[1])
            second = runner.execute('cloud', 'e1', ['printf', '%s', 'literal; $(never-local)'], {'target': 'cloud'})
            self.assertEqual(1, launch.call_count)
            self.assertEqual('finished', second['state'])


class NetworkAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        key = root / 'key'
        key.write_text('a' * 64)
        key.chmod(0o600)
        self.peer = {'node': 'cloud', 'role': 'agent_peer', 'token_file': str(key), 'capabilities': ['a2a.delegate', 'a2a.report']}
        self.api = API({'node_id': 'laptop', 'database': str(root / 'db'), 'peers': [self.peer],
                        'network_peers': {'cloud': {'send_allowed': True}}})
        self.message = {'protocol': PROTOCOL, 'id': 'm1', 'to': 'laptop', 'input': 'test', 'project': 'p', 'agent': 'a'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_peer_task_permission_separate_from_worker_leases(self):
        value = self.api.dispatch('POST', '/v1/mesh/send', {'message': self.message}, self.peer)
        self.assertEqual('accepted', value['state'])
        self.assertEqual(value['task_id'], self.api.dispatch('GET', '/v1/mesh/task?id=m1', {}, self.peer)['task_id'])
        # A peer is not a native worker and must not claim or overwrite leases.
        for path in ('/v1/claim', '/v1/heartbeat'):
            with self.assertRaises(PermissionError):
                self.api.dispatch('POST', path, {}, self.peer)

    def test_viewer_and_missing_delegation_cannot_create_tasks(self):
        for peer in ({'role': 'viewer'}, dict(self.peer, capabilities=[]), dict(self.peer, node='impostor', role='viewer')):
            with self.assertRaises(PermissionError):
                self.api.dispatch('POST', '/v1/mesh/send', {'message': self.message}, peer)

    def test_spoofed_sender_cannot_be_in_request(self):
        with self.assertRaises(ValueError):
            self.api.dispatch('POST', '/v1/mesh/send', {'message': self.message, 'sender': 'operator'}, self.peer)

    def test_queue_requires_configured_remote_permission(self):
        payload = {'peer': 'cloud', 'message': dict(self.message, to='cloud')}
        self.api.dispatch('POST', '/v1/mesh/queue', payload, {'role': 'operator'})
        with self.assertRaises(PermissionError):
            self.api.dispatch('POST', '/v1/mesh/queue', payload, self.peer)
        with self.assertRaises(PermissionError):
            self.api.dispatch('POST', '/v1/mesh/queue', dict(payload, peer='unknown'), {'role': 'operator'})


if __name__ == '__main__':
    unittest.main()
