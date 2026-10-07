import tempfile
import unittest
from pathlib import Path

from assistant_mesh.channel import Channel, ChannelError
from assistant_mesh.store import Store


class FakeTransport:
    account = {'ilink_user_id': 'owner', 'ilink_bot_id': 'bot'}

    def send(self, row):
        if self.error:
            raise ChannelError(self.error)
        return {'message_id': 'accepted'}


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db')
        self.transport = FakeTransport()
        self.transport.error = None
        self.channel = Channel(self.store, self.transport)
        self.store.enqueue('test', 'hello')

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepted_is_not_delivery_verified(self):
        self.channel.send_once()
        self.assertEqual('accepted', self.store.send_status('test')['status'])
        self.assertFalse(self.store.send_status('test')['detail']['delivery_verified'])

    def test_timeout_not_retried(self):
        self.transport.error = 'network_timeout'
        self.channel.send_once()
        self.assertEqual('unknown', self.store.send_status('test')['status'])
        self.assertFalse(self.channel.send_once())

    def test_context_rejected(self):
        self.transport.error = 'business_-2'
        self.channel.send_once()
        self.assertEqual('rejected', self.store.send_status('test')['status'])
        self.assertFalse(self.channel.send_once())

    def test_auth_error_not_treated_as_context_error(self):
        self.transport.error = 'auth_required'
        self.channel.send_once()
        self.assertEqual('waiting_auth', self.store.send_status('test')['status'])


if __name__ == '__main__':
    unittest.main()
