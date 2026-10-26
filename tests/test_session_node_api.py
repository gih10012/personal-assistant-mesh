"""Session ingress auth at the real API, using its one existing authority."""
from pathlib import Path
import tempfile
import unittest

from assistant_mesh.server import API


class SessionNodeAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-session-api.')
        root = Path(self.temp.name)
        key = root / 'ingress.token'
        key.write_text('synthetic-ingress-only-key-' + 'x' * 40)
        key.chmod(0o600)
        self.binding = {'owner_id': 'owner-fixture', 'source': 'chatgpt-chat',
                        'subject': 'owner-chat', 'scopes': ['tasks:read', 'tasks:submit']}
        self.peer = {'role': 'ingress', 'token_file': str(key), 'ingress': self.binding}
        self.policy = {'subjects': {'owner-chat': {
            'capabilities': ['agent'], 'visible_nodes': {'authority-fixture': 'AMITA'}}}}
        self.api = API({'database': str(root / 'original.sqlite'), 'node_id': 'authority-fixture',
                        'peers': [self.peer], 'ingress': {'owner_id': 'owner-fixture', 'required': ['agent']},
                        'session_nodes': self.policy})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, action, payload, peer=None):
        return self.api.dispatch('POST', '/v1/ingress/mesh/' + action,
                                 payload, peer or self.peer)

    def test_scoped_discovery_and_join_reuse_original_store_without_task(self):
        self.assertIs(self.api.store, self.api.session_nodes.store)
        discovered = self.call('discover', {})
        self.assertEqual('reachable', discovered['network']['state'])
        joined = self.call('connect', {'session_id': 'chat-one', 'control_id': 'join-one',
                                      'capabilities': ['agent']})
        self.assertTrue(joined['session']['node_id'])
        self.assertFalse(joined['business_task_created'])
        self.assertIsNone(self.call('work_pull', {'session_id': 'chat-one', 'pull_id': 'pull-one'})['work'])
        with self.api.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
        for peer in ({'role': 'operator'}, {'role': 'worker', 'node': 'cloud'}, {'role': 'viewer'}):
            with self.subTest(peer=peer), self.assertRaises(PermissionError):
                self.call('discover', {}, peer)
        for path in ('/v1/status', '/v1/resources', '/v1/claim', '/v1/session-nodes/authorize'):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                self.api.dispatch('POST', path, {}, self.peer)

    def test_only_owner_route_grants_original_work_and_keeps_lineage(self):
        receipt = self.api.dispatch('POST', '/v1/ingress/tasks',
                                    {'request_id': 'original-request', 'input': 'Bounded owner work'}, self.peer)
        grant = {'subject': 'owner-chat', 'request_id': 'original-request',
                 'operation_id': 'original-operation', 'conversation_id': 'original-conversation'}
        route = '/v1/session-nodes/authorize'
        for peer in (self.peer, {'role': 'worker', 'node': 'cloud'}, {'role': 'viewer'}):
            with self.subTest(peer=peer), self.assertRaises(PermissionError):
                self.api.dispatch('POST', route, grant, peer)
        installed = self.api.dispatch('POST', route, grant, {'role': 'operator'})
        self.assertEqual(receipt['task_id'], installed['task_id'])
        self.call('connect', {'session_id': 'chat-one', 'control_id': 'join-one', 'capabilities': ['agent']})
        work = self.call('work_pull', {'session_id': 'chat-one', 'pull_id': 'pull-one'})['work']
        self.assertEqual(receipt['task_id'], work['task_id'])
        self.assertEqual('original-operation', work['operation_id'])
        self.assertEqual('original-conversation', work['conversation_id'])
        with self.api.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
        with self.assertRaises(ValueError):
            self.api.dispatch('POST', route, dict(grant, authorized=True), {'role': 'operator'})

    def test_unconfigured_and_alternate_url_forms_cannot_fall_through(self):
        for suffix in ('?ignored=', '?foo=bar', '#ignored'):
            with self.subTest(suffix=suffix), self.assertRaises(PermissionError):
                self.api.dispatch('POST', '/v1/ingress/mesh/discover' + suffix, {}, self.peer)
        with self.assertRaises(PermissionError):
            self.api.dispatch('GET', '/v1/ingress/mesh/discover', {}, self.peer)
        original = self.api.session_nodes
        self.api.session_nodes = None
        with self.assertRaises(PermissionError):
            self.call('discover', {})
        self.api.session_nodes = original
        for policy in ({}, dict(self.policy, authority='foreign'), None):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.api.install_session_nodes(policy)
        self.assertIs(original, self.api.session_nodes)


if __name__ == '__main__':
    unittest.main()
