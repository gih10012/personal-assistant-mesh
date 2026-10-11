"""Opt-in server lifecycle; synthetic already-granted authority, no iLink IO."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from assistant_mesh.channel import Channel
from assistant_mesh.channel_authority import ChannelAuthority, validate_config
from assistant_mesh.channel_supervisor import ChannelSupervisor
from assistant_mesh.networking import digest
from assistant_mesh.remote_notifications import PROTOCOL as NOTIFY_PROTOCOL
from assistant_mesh.server import _start_channel
from assistant_mesh.store import Store
from tests import test_channel_fence_integration as fixtures


class ChannelServerSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.store = Store(self.root / 'original.db')
        self.transport = fixtures.SyntheticILink()
        self.binding = digest([NOTIFY_PROTOCOL, self.transport.base,
            self.transport.account['ilink_user_id'], self.transport.account['ilink_bot_id']])
        self.store.set('owner_notification_channel_binding', self.binding)
        self.store.set('cursor', 'original-canonical-cursor')
        self.config = {'node_id':'cloud'}
        self.authority = ChannelAuthority(self.store, {'witness_id':'fixture-witness',
            'owner_ref':'owner-ref','transport_mode':'fenced'}, 'cloud',self.transport,self.binding)
        self.api = SimpleNamespace(channel_authority=self.authority,ilink=self.transport,store=self.store)
        self.supervisor = None

    def tearDown(self):
        if self.supervisor:
            self.assertTrue(self.supervisor.stop(2))
        self.tmp.cleanup()

    def grant(self):
        from assistant_mesh.channel_carrier import PROTOCOL
        self.contract = {'protocol':PROTOCOL,'node':'cloud','owner_ref':'owner-ref',
            'canonical_authority':'cloud','account_binding':self.binding,
            'private_credential_ref':'synthetic-private-ref','revision':1,
            'runtime':{'adapter':'ilink/0.3.0','fence_protocol':PROTOCOL,
                'transport_available':True,'private_state':True}}
        self.authority.witness.handoff_verifier = fixtures.ChannelFenceIntegrationTests.handoff
        self.authority.witness.candidate('cloud', self.contract, 300)
        return self.authority.witness.grant(self.binding, 0,'cloud',60,'synthetic-bootstrap')

    def test_private_config_opt_in_only_with_fenced_mode(self):
        base = {'witness_id':'fixture-witness','owner_ref':'owner-ref','transport_mode':'fenced'}
        self.assertEqual(base, validate_config(base))
        result = validate_config(dict(base, supervisor={'renew_seconds':120,'renew_interval':20}))
        self.assertEqual(120, result['supervisor']['renew_seconds'])
        with self.assertRaisesRegex(ValueError,'channel_supervisor_requires_fenced_transport'):
            validate_config(dict(base,transport_mode='disabled',supervisor={}))
        for options in ({'auto_grant':True},{'renew_interval':True},
                        {'renew_seconds':20,'renew_interval':20},{'candidate_contract':False}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                validate_config(dict(base,supervisor=options))

    def test_server_explicit_supervisor_keeps_original_identity_and_owns_cleanup(self):
        original = self.grant()
        self.authority.config['supervisor'] = {'renew_interval':0.02,'candidate_contract':self.contract}
        polled, sent = threading.Event(), threading.Event()
        with patch.object(Channel,'poll_once',side_effect=polled.set), \
                patch.object(Channel,'send_once',side_effect=sent.set):
            channel,self.supervisor = _start_channel(self.api,self.config)
            self.assertIs(channel,self.supervisor.channel)
            self.assertIsInstance(self.supervisor,ChannelSupervisor)
            self.assertTrue(polled.wait(1))
            self.assertTrue(sent.wait(1))
            self.assertTrue(self.supervisor.stop(2))
        self.assertEqual(original['epoch'],self.authority.witness.inspect(self.binding)['epoch'])
        self.assertEqual([],self.transport.calls)
        self.assertTrue(self.supervisor.snapshot()['owned_threads_stopped'])

    def test_frozen_start_refuses_new_holder_without_threads_or_transport(self):
        self.authority.config['supervisor'] = {}
        with patch('assistant_mesh.server.threading.Thread') as thread:
            with self.assertRaisesRegex(ValueError,'channel_carrier_local_term_not_granted'):
                _start_channel(self.api,self.config)
        thread.assert_not_called()
        self.assertEqual([],self.transport.calls)

    def test_disabled_and_legacy_are_not_implicitly_supervised(self):
        self.authority.config['transport_mode'] = 'disabled'
        with patch('assistant_mesh.server.threading.Thread') as thread:
            self.assertEqual((None,None),_start_channel(self.api,self.config))
        thread.assert_not_called()
        self.api.channel_authority = None
        with patch('assistant_mesh.server.threading.Thread') as thread:
            channel,supervisor = _start_channel(self.api,self.config)
        self.assertIsNone(supervisor)
        self.assertIsNone(channel.carrier_fence)
        self.assertEqual(2,thread.call_count)
        self.assertEqual([],self.transport.calls)


if __name__ == '__main__':
    unittest.main()
