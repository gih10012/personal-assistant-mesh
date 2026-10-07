import json
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.server import API


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        key = self.root / 'peer.token'
        key.write_text('a' * 64)
        key.chmod(0o600)
        self.peer = {'node': 'n', 'role': 'worker', 'capabilities': ['leader'], 'token_file': str(key)}
        self.api = API({'database': str(self.root / 'db'), 'peers': [self.peer]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_bad_token(self):
        self.assertIsNone(self.api.principal('Bearer wrong'))
        self.assertEqual(self.peer, self.api.principal('Bearer ' + 'a' * 64))

    def test_worker_cannot_send_arbitrary_notification(self):
        with self.assertRaises(PermissionError):
            self.api.dispatch('POST', '/v1/notify', {'text': 'x', 'request_id': 'x'}, self.peer)

    def test_node_id_is_bound_to_credential(self):
        self.api.dispatch('POST', '/v1/heartbeat', {'node': 'spoofed', 'capabilities': ['leader']}, self.peer)
        self.assertEqual('n', self.api.store.status()['nodes'][0]['id'])

    def test_capability_escalation_denied(self):
        with self.assertRaises(ValueError):
            self.api.dispatch('POST', '/v1/heartbeat', {'capabilities': ['leader', 'purchase']}, self.peer)

    def test_no_auth_or_message_values_in_status(self):
        self.assertNotIn('token', json.dumps(self.api.store.status()))


if __name__ == '__main__':
    unittest.main()
