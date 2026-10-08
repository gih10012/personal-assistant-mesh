import json
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.server import API
from assistant_mesh.store import Conflict


class ResourceAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.api = API({'database': str(Path(self.temp.name) / 'db'), 'peers': [],
                        'resources': {'trusted_verifiers': ['node:checker']}})
        self.api.store.clock = lambda: self.now
        self.worker = {'role': 'worker', 'node': 'a'}
        self.other = {'role': 'worker', 'node': 'b'}
        self.owner = {'role': 'operator', 'node': 'ignored-for-owner'}
        self.viewer = {'role': 'viewer'}

    def tearDown(self):
        self.temp.cleanup()

    def action(self, action, arguments=None, peer=None):
        return self.api.dispatch('POST', '/v1/resource/action',
                                 {'action': action, 'arguments': arguments or {}}, peer or self.worker)

    def advertise(self, **extra):
        return self.action('advertise', dict(id='cap-a', kind='model.created.future-tool', **extra))

    def test_authenticated_actor_binding_cannot_be_overridden(self):
        first = self.advertise()
        self.assertEqual('node:a', first['principal'])
        owner = self.action('advertise', {'id': 'owner-cap', 'kind': 'owner.tool'}, self.owner)
        self.assertEqual('operator', owner['principal'])
        for forged in ({'actor': 'operator'}, {'owner': 'operator'}, {'node': 'b'},
                       {'role': 'operator'}, {'trusted_verifiers': ['node:a']}, {'principal': 'node:b'}):
            with self.subTest(forged=forged), self.assertRaises(PermissionError):
                self.action('advertise', dict(id='forged', kind='bad.tool', **forged))
        with self.assertRaises(PermissionError):
            self.api.dispatch('POST', '/v1/resource/action',
                              {'action': 'advertise', 'actor': 'operator',
                               'arguments': {'id': 'forged', 'kind': 'bad.tool'}}, self.worker)
        self.assertEqual(2, len(self.api.resources.discover()['capabilities']))

    def test_viewer_get_metadata_but_no_resource_action_even_with_node(self):
        self.advertise(spec={'endpoint': 'https://user:pass@host.test/run?token=private', 'token': 'do-not-emit'})
        self.action('observe', {'id': 'cap-a', 'metric': 'latency', 'value': 25, 'unit': 'ms',
                                'source': 'provider check', 'epoch': 1, 'evidence': {'workload': 'small'}})
        for peer in (self.viewer, dict(self.viewer, node='a'), self.worker):
            read = self.api.dispatch('GET', '/v1/resources', {}, peer)
            self.assertNotIn('do-not-emit', json.dumps(read))
            self.assertNotIn('user:pass', json.dumps(read))
            described = self.api.dispatch('GET', '/v1/resource?id=cap-a', {}, peer)
            self.assertEqual(25, described['metrics'][0]['value'])
            self.assertEqual('small', described['metrics'][0]['evidence']['workload'])
            self.assertEqual('declared', described['metrics'][0]['verification'])
            self.assertEqual([], self.api.dispatch('GET', '/v1/resource/graph', {}, peer)['edges'])
        for peer in (self.viewer, dict(self.viewer, node='a')):
            for action in ('discover', 'advertise', 'authorize', 'grant', 'revoke_grant'):
                with self.subTest(peer=peer, action=action), self.assertRaises(PermissionError):
                    self.action(action, peer=peer)
        with self.assertRaises(PermissionError):
            self.api.dispatch('GET', '/v1/resources', {}, {'role': 'unknown'})

    def test_worker_request_is_not_owner_approval(self):
        self.advertise()
        request = self.action('request_grant', {'id': 'cap-a', 'action': 'tool.read', 'scope': '/one/path',
                                               'request_id': 'request'}, self.other)
        self.assertEqual('node:b', request['principal'])
        with self.assertRaises(PermissionError):
            self.action('grant', {'request_id': 'request'}, self.other)
        self.assertFalse(self.action('authorize', {'id': 'cap-a', 'action': 'tool.read', 'scope': '/one/path'}, self.other)['allowed'])
        granted = self.action('grant', {'request_id': 'request', 'expires_seconds': 30}, self.owner)
        self.assertEqual('operator', granted['approved_by'])
        self.assertTrue(self.action('authorize', {'id': 'cap-a', 'action': 'tool.read', 'scope': '/one/path'}, self.other)['allowed'])
        self.assertFalse(self.action('authorize', {'id': 'cap-a', 'action': 'tool.write', 'scope': '/one/path'}, self.other)['allowed'])
        with self.assertRaises(PermissionError):
            self.action('revoke_grant', {'grant_id': granted['id']}, self.other)
        self.action('revoke_grant', {'grant_id': granted['id']}, self.owner)
        self.assertFalse(self.action('authorize', {'id': 'cap-a', 'action': 'tool.read', 'scope': '/one/path'}, self.other)['allowed'])

    def test_api_rechecks_lease_epoch_and_revocation(self):
        self.advertise(lease_seconds=20)
        self.action('request_grant', {'id': 'cap-a', 'action': 'read', 'scope': '/path', 'request_id': 'request'}, self.other)
        self.action('grant', {'request_id': 'request'}, self.owner)
        self.now += 21
        self.assertEqual([], self.api.dispatch('GET', '/v1/resources', {}, self.worker)['capabilities'])
        self.assertFalse(self.action('authorize', {'id': 'cap-a', 'action': 'read', 'scope': '/path'}, self.other)['allowed'])
        with self.assertRaises(Conflict):
            self.action('renew', {'id': 'cap-a', 'epoch': 1})
        restored = self.advertise(lease_seconds=20, expected_epoch=1)
        self.assertEqual(2, restored['epoch'])
        self.assertFalse(self.action('authorize', {'id': 'cap-a', 'action': 'read', 'scope': '/path'}, self.other)['allowed'])
        with self.assertRaises(Conflict):
            self.action('revoke', {'id': 'cap-a', 'epoch': 1})
        self.action('revoke', {'id': 'cap-a', 'epoch': 2}, self.owner)
        self.assertTrue(self.api.dispatch('GET', '/v1/resource?id=cap-a', {}, self.viewer)['revoked'])

    def test_verified_identity_is_deployment_bound_not_request_bound(self):
        self.advertise()
        observation = {'id': 'cap-a', 'metric': 'correctness', 'value': 1, 'unit': 'ratio',
                       'source': 'bounded probe', 'verification': 'verified', 'epoch': 1,
                       'evidence': {'workload': 'fixed example'}}
        with self.assertRaises(PermissionError):
            self.action('observe', observation)
        with self.assertRaises(PermissionError):
            self.action('observe', dict(observation, actor='node:checker'))
        checked = self.action('observe', observation, {'role': 'worker', 'node': 'checker'})
        self.assertEqual('verified', checked['verification'])
        self.assertEqual('node:checker', checked['actor'])

    def test_default_event_page_is_recent_not_first_page_forever(self):
        self.advertise()
        for number in range(130):
            self.action('authorize', {'id': 'cap-a', 'action': 'read', 'scope': str(number)}, self.other)
        recent = self.api.dispatch('GET', '/v1/capability-events', {}, self.viewer)
        self.assertEqual('recent', recent['page']['mode'])
        self.assertEqual(100, len(recent['events']))
        self.assertEqual(32, recent['events'][0]['sequence'])
        self.assertEqual(131, recent['events'][-1]['sequence'])
        self.assertFalse(recent['page']['has_more'])
        first = self.api.dispatch('GET', '/v1/capability-events?after=0&limit=2', {}, self.worker)
        self.assertEqual([1, 2], [event['sequence'] for event in first['events']])
        self.assertTrue(first['page']['has_more'])
        next_page = self.api.dispatch('GET', '/v1/capability-events?after=2&limit=2', {}, self.worker)
        self.assertEqual([3, 4], [event['sequence'] for event in next_page['events']])
        filtered = self.api.dispatch('GET', '/v1/capability-events?principal=node%3Aa', {}, self.viewer)
        self.assertEqual([1], [event['sequence'] for event in filtered['events']])

    def test_discovery_filters_are_queries_not_permissions(self):
        self.advertise()
        result = self.action('discover', {'principal': 'node:a'}, self.other)
        self.assertEqual('node:a', result['capabilities'][0]['principal'])
        self.assertFalse(self.action('authorize', {'id': 'cap-a', 'action': 'run', 'scope': 'exact'}, self.other)['allowed'])
        for name in ('_audit', '_provider', 'transaction', '__init__', 'execute', 'scan_network'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.action(name)
        with self.assertRaises(ValueError):
            self.api.dispatch('GET', '/v1/resources?include_unavailable=yes', {}, self.worker)
        with self.assertRaises(ValueError):
            self.api.dispatch('GET', '/v1/resource', {}, self.worker)


if __name__ == '__main__':
    unittest.main()
