import json
import tempfile
import unittest
from pathlib import Path

from scripts.configure_notifications import enable_node_relay, grant_cloud_peer


class NotificationEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        caps = ['agent', 'mesh.node:laptop', 'codex.native', 'a2a.send']
        self.worker = self.save('worker.json', {'node_id': 'laptop', 'capabilities': caps,
                                               'codex': {'sandbox': 'danger-full-access', 'workspace': '/owner/work'}})
        self.server = self.save('server.json', {'node_id': 'laptop', 'peers': [
            {'role': 'worker', 'node': 'laptop', 'capabilities': caps, 'token_file': 'private-old-worker'},
            {'role': 'operator', 'token_file': 'private-old-operator'}]})
        client = self.save('client.json', {'control_url': 'http://127.0.0.1:17680', 'token_file': 'private-old-peer'})
        self.node = self.save('node.json', {'node_id': 'laptop', 'local_server_config': self.server,
            'local_worker_config': self.worker, 'peers': [{'node': 'cloud', 'authority': 'cloud', 'client_config': client}]})
        self.cloud = self.save('cloud.json', {'node_id': 'cloud', 'ilink_account': '/private/owner.json', 'peers': [
            {'role': 'agent_peer', 'node': 'laptop', 'capabilities': ['a2a.delegate', 'a2a.report'], 'token_file': 'private-old-peer'},
            {'role': 'agent_peer', 'node': 'other', 'capabilities': ['a2a.delegate'], 'token_file': 'private-other'},
            {'role': 'operator', 'token_file': 'private-old-operator'}]})

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding='utf8')
        path.chmod(0o600)
        return str(path)

    @staticmethod
    def load(path):
        return json.loads(Path(path).read_text(encoding='utf8'))

    def test_dry_run_preserves_all_private_configs(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.assertFalse(grant_cloud_peer(self.cloud, 'laptop', dry_run=True)['applied'])
        self.assertFalse(enable_node_relay(self.node, 'cloud', 'cloud', dry_run=True)['applied'])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})

    def test_cloud_grant_changes_only_enrolled_peer_capability(self):
        before = self.load(self.cloud)
        result = grant_cloud_peer(self.cloud, 'laptop')
        after = self.load(self.cloud)
        self.assertTrue(result['changed'])
        self.assertEqual(before['peers'][1:], after['peers'][1:])
        self.assertEqual(before['peers'][0]['token_file'], after['peers'][0]['token_file'])
        self.assertEqual(['a2a.delegate', 'a2a.report', 'owner.notify'], after['peers'][0]['capabilities'])
        self.assertEqual(1, len(list(self.root.glob('cloud.json.before-node-*'))))
        self.assertFalse(grant_cloud_peer(self.cloud, 'laptop')['changed'])

    def test_node_grant_preserves_native_codex_and_tokens(self):
        before_server, before_worker, before_node = self.load(self.server), self.load(self.worker), self.load(self.node)
        result = enable_node_relay(self.node, 'cloud', 'cloud')
        after_server, after_worker = self.load(self.server), self.load(self.worker)
        self.assertTrue(result['changed'])
        self.assertFalse(result['native_tools_intercepted'])
        self.assertFalse(result['historical_notifications_enrolled'])
        self.assertEqual(before_worker['codex'], after_worker['codex'])
        self.assertEqual(before_node, self.load(self.node))
        self.assertEqual(before_server['peers'][1:], after_server['peers'][1:])
        self.assertEqual(before_server['peers'][0]['token_file'], after_server['peers'][0]['token_file'])
        self.assertEqual({'mode': 'private', 'owner_relay': {'peer': 'cloud', 'authority': 'cloud'}}, after_server['notification_policy'])
        self.assertEqual(after_worker['capabilities'], after_server['peers'][0]['capabilities'])
        self.assertFalse(enable_node_relay(self.node, 'cloud', 'cloud')['changed'])

    def test_not_enrolled_or_wrong_authority_never_changes_configs(self):
        original = Path(self.server).read_bytes()
        for peer, authority in (('stranger', 'stranger'), ('cloud', 'wrong'), ('laptop', 'laptop')):
            with self.subTest(peer=peer), self.assertRaises(ValueError):
                enable_node_relay(self.node, peer, authority)
        self.assertEqual(original, Path(self.server).read_bytes())
        with self.assertRaises(ValueError):
            grant_cloud_peer(self.cloud, 'stranger')

    def test_existing_route_cannot_be_silently_retargeted(self):
        server = self.load(self.server)
        server['notification_policy'] = {'mode': 'private', 'owner_relay': {'peer': 'cloud', 'authority': 'old'}}
        self.save('server.json', server)
        with self.assertRaisesRegex(ValueError, 'existing_route_conflict'):
            enable_node_relay(self.node, 'cloud', 'cloud')
        self.assertEqual(server, self.load(self.server))

    def test_relay_cannot_start_second_channel_or_mutate_central_node(self):
        server = self.load(self.server)
        server['ilink_account'] = '/private/owner.json'
        self.save('server.json', server)
        with self.assertRaises(ValueError):
            enable_node_relay(self.node, 'cloud', 'cloud')
        node = self.load(self.node)
        node['start_server'] = False
        self.save('node.json', node)
        with self.assertRaises(ValueError):
            enable_node_relay(self.node, 'cloud', 'cloud')

    def test_cloud_grant_requires_channel_and_unique_peer(self):
        cloud = self.load(self.cloud)
        cloud.pop('ilink_account')
        self.save('cloud.json', cloud)
        with self.assertRaises(ValueError):
            grant_cloud_peer(self.cloud, 'laptop')
        cloud['ilink_account'] = '/private/owner.json'
        cloud['peers'].append(dict(cloud['peers'][0]))
        self.save('cloud.json', cloud)
        with self.assertRaises(ValueError):
            grant_cloud_peer(self.cloud, 'laptop')

    def test_worker_capability_mismatch_fails_before_writing(self):
        worker = self.load(self.worker)
        worker['capabilities'].append('unknown-extra')
        self.save('worker.json', worker)
        before = Path(self.server).read_bytes()
        with self.assertRaises(ValueError):
            enable_node_relay(self.node, 'cloud', 'cloud')
        self.assertEqual(before, Path(self.server).read_bytes())


if __name__ == '__main__':
    unittest.main()
