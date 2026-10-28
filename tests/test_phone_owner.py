"""Synthetic owner operations over one original live authority; no real phone."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from assistant_mesh.enrollment import EnrollmentError, document_sha256, result_envelope
from assistant_mesh.enrollment_bootstrap import EnrollmentBootstrap
from assistant_mesh.enrollment_relay import REQUEST_FORMAT
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_backend import LocalIngressBackend
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.phone_authority import PhoneAuthority
from assistant_mesh.phone_owner import PREFIX, SCOPE, install_dispatch, main
from assistant_mesh.resources import Registry
from assistant_mesh.session_nodes import LocalSessionNodes
from assistant_mesh.store import Store
from tests.test_enrollment_relay import fixture, owned
from types import SimpleNamespace


class PhoneOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='phone-owner-synthetic.')
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.store = Store(self.root / 'original.db', recover_inflight=False)
        ingress = Ingress(self.store, 'synthetic-authority', {'owner_id': 'synthetic-owner'})
        results = IngressResults(self.store, 'synthetic-authority', {'owner_id': 'synthetic-owner'})
        sessions = LocalSessionNodes(LocalIngressBackend(ingress, results),
            {'existing-chat': {'capabilities': ['agent'], 'visible_nodes': {}}}, mesh_id='synthetic-mesh')
        self.config_path = self.root / 'authority.json'
        owned(self.config_path, {'format': 'mesh-phone-authority/1', 'enabled': True, 'nodes': []})
        self.api = SimpleNamespace(store=self.store, resources=Registry(self.store), session_nodes=sessions,
                                   dispatch=lambda *args: {'original_dispatch': True})
        self.service = PhoneAuthority(self.api, self.config_path)
        self.owner_root = self.root / 'phone-owner'; self.owner_root.mkdir(mode=0o700)
        owned(self.owner_root / 'bindings.json', {'enrollments': ['phone1', 'phone2']})
        self.settings = {}
        for index in (1, 2):
            identity = 'phone' + str(index)
            path = self.owner_root / identity; path.mkdir(mode=0o700)
            unused, config, unused2, unused3, unused4, unused5, raw = fixture(path)
            config['enrollment']['node_id'] = 'synthetic-' + identity
            config['authorization']['enrollment_sha256'] = document_sha256(config['enrollment'])
            config['challenge']['operation_id'] += '-' + identity
            config['challenge']['capability_id'] = identity + ':bytes'
            config['authorization']['capabilities'][0]['id'] = identity + ':bytes'
            # fixture's original synthetic state is deliberately separate;
            # this bootstrap has not initialized or created a challenge.
            (path / 'state').rename(path / 'fixture-state')
            config['state_dir'] = str(path / 'state')
            owned(path / 'bootstrap.json', config)
            settings = {'format': 'amita-phone-owner-binding/1', 'node_id': 'synthetic-' + identity,
                'subject': identity, 'session_id': identity + '-session', 'capabilities': [identity + ':bytes'],
                'visible_nodes': {}, 'task_id': 'original-task', 'task_epoch': 1,
                'entry_request_id': 'original-request'}
            owned(path / 'settings.json', settings)
            self.settings[identity] = settings
        self.raw = raw
        self.store.heartbeat('original-worker', ['agent', 'leader'])
        self.store.create_task('synthetic original request', task_id='original-task',
            context={'request_id': 'original-request', 'session_scope': SCOPE})
        self.task = self.store.claim('original-worker')
        install_dispatch(self.api, self.service, 'https://mesh.example.test')
        self.owner = self.service.owner_operations

    def tearDown(self):
        self.temp.cleanup()

    def call(self, action, identity='phone1', role='operator', extra=None):
        body = {'enrollment_id': identity}
        if extra:
            body.update(extra)
        return self.api.dispatch('POST', PREFIX + action, body, {'role': role})

    def bootstrap(self, identity='phone1'):
        return EnrollmentBootstrap(self.owner_root / identity / 'bootstrap.json')

    def relay_roundtrip(self, identity='phone1'):
        self.call('prepare-existing-binding', identity)
        exported = json.loads((self.owner_root / identity / 'relay-export.json').read_text())
        client, token = exported['client'], exported['credential']['token']
        def request(action, args):
            return self.service.enrollment(token, {'format': REQUEST_FORMAT, 'binding': client['binding'],
                                                  'action': action, 'arguments': args})
        admitted = request('challenge', {})
        self.assertTrue(admitted['may_execute_original_challenge'])
        self.assertFalse(request('challenge', {})['may_execute_original_challenge'])
        request('result', result_envelope(admitted['challenge'], self.raw * 3))
        return exported

    def registered(self, identity='phone1'):
        self.relay_roundtrip(identity)
        self.call('verify', identity)
        self.call('register', identity)

    def test_plan_is_read_only_cli_thin_and_operator_exact_routes(self):
        empty = self.api.dispatch('POST', PREFIX + 'plan', {}, {'role': 'operator'})
        self.assertFalse(empty['phone_enrolled'])
        result = self.call('plan')
        self.assertFalse(result['initialized'])
        self.assertFalse((self.owner_root / 'phone1' / 'state').exists())
        for role in ('worker', 'viewer', 'ingress', None):
            with self.assertRaises(PermissionError):
                self.call('prepare-existing-binding', role=role)
        for action in ('issue?x=1', 'run', 'register/other'):
            with self.assertRaises(PermissionError):
                self.call(action)
        for identity in ('../phone1', '/tmp', 'not-configured'):
            with self.assertRaises(ValueError):
                self.call('plan', identity)
        with self.assertRaises(ValueError):
            self.call('prepare-existing-binding', extra={'bootstrap_config': '/tmp/anything'})
        config = self.root / 'operator.json'; owned(config, {})
        fake = SimpleNamespace(request=lambda path, body: self.api.dispatch('POST', path, body, {'role': 'operator'}))
        with patch('assistant_mesh.worker.Client', return_value=fake), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, main(['--config', str(config), 'plan', '--enrollment-id', 'phone1']))
        self.assertEqual('plan', json.loads(out.getvalue())['action'])
        self.assertEqual({'original_dispatch': True}, self.api.dispatch('GET', '/original', {}, {'role': 'operator'}))

    def test_original_journal_verify_registry_issue_and_second_live_binding(self):
        self.relay_roundtrip()
        prepared = self.call('prepare-existing-binding')
        self.assertEqual('result_reported', prepared['status']['state'])
        self.assertEqual(1, len(self.service.relays))
        with self.assertRaises((PermissionError, ValueError)):
            self.call('issue')
        self.call('verify'); self.call('register')
        self.assertTrue(self.call('issue')['node_loaded'])
        first = (self.owner_root / 'phone1' / 'issued.json').read_bytes()
        self.call('issue')
        self.assertEqual(first, (self.owner_root / 'phone1' / 'issued.json').read_bytes())
        self.registered('phone2'); self.call('issue', 'phone2')
        self.assertEqual(2, len(self.service.installed['nodes']))
        self.assertEqual(2, len(self.service.relays))
        for identity in ('phone1', 'phone2'):
            exported = json.loads((self.owner_root / identity / 'issued.json').read_text())['bootstrap']
            response = self.service.node(exported['credential']['token'], {'action': 'mesh_connect', 'arguments': {
                'session_id': identity + '-session', 'control_id': 'original-connect', 'capabilities': [identity + ':bytes']}})
            self.assertEqual('synthetic-' + identity, response['session']['node_id'])
        self.assertIn('existing-chat', self.service.sessions.subjects)
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT count(*) FROM tasks').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT epoch FROM tasks WHERE id=?', ('original-task',)).fetchone()[0])

    def test_revocation_lost_ack_and_registry_tamper_fail_closed(self):
        self.registered(); self.call('issue')
        exported = json.loads((self.owner_root / 'phone1' / 'issued.json').read_text())['bootstrap']
        config = json.loads(self.config_path.read_text())
        config['nodes'][0]['enabled'] = False; owned(self.config_path, config)
        with self.assertRaises(PermissionError):
            self.service.principal(exported['credential']['token'], 'phone:node')
        with self.assertRaisesRegex(ValueError, 'revoked'):
            self.call('issue')
        cap = self.api.resources.describe('phone1:bytes')
        self.api.resources.revoke('operator', cap['id'], cap['epoch'])
        with self.assertRaises(Exception):
            self.call('issue')
        self.assertEqual('registered', self.bootstrap().status('original-enrollment-operation-phone1')['registration_state'])

    def test_cancel_and_bad_metadata_cannot_prepare_or_mutate_maps(self):
        settings = copy.deepcopy(self.settings['phone1'])
        settings['visible_nodes'] = {'original-worker': {'display_name': 'computer', 'capabilities': ['private-project']}}
        owned(self.owner_root / 'phone1' / 'settings.json', settings)
        with self.assertRaisesRegex(ValueError, 'not_observed'):
            self.call('prepare-existing-binding')
        self.assertFalse((self.owner_root / 'phone1' / 'state').exists())
        owned(self.owner_root / 'phone1' / 'settings.json', self.settings['phone1'])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='paused' WHERE id='original-task'")
        with self.assertRaises(PermissionError):
            self.call('prepare-existing-binding')
        self.assertEqual(['existing-chat'], list(self.service.sessions.subjects))

    def test_registration_unknown_only_observes_original_registry(self):
        self.relay_roundtrip(); self.call('verify')
        original_advertise = self.api.resources.advertise
        def lost_ack(*args, **kwargs):
            original_advertise(*args, **kwargs)
            raise ConnectionError('synthetic lost registrar acknowledgement')
        with patch.object(self.api.resources, 'advertise', side_effect=lost_ack) as advertise:
            with patch.object(self.api.resources, 'describe', side_effect=ConnectionError):
                with self.assertRaises(EnrollmentError):
                    self.call('register')
            self.assertEqual('unknown', self.call('status')['status']['registration_state'])
            self.assertEqual('registered', self.call('register')['status']['registration_state'])
            self.assertEqual(1, advertise.call_count)

    def test_original_http_cli_accepts_operator_and_rejects_worker(self):
        from assistant_mesh.server import serve
        operator_token = owned(self.root / 'operator.token', b'SYNTHETIC_OPERATOR_' + b'o' * 48)
        worker_token = owned(self.root / 'worker.token', b'SYNTHETIC_WORKER_' + b'w' * 48)
        config = {'database': str(self.root / 'http-original.db'), 'node_id': 'synthetic-http-authority',
            'port': 0, 'notification_policy': {'mode': 'private'},
            'peers': [{'role': 'operator', 'token_file': operator_token['path']},
                      {'role': 'worker', 'node': 'synthetic-worker', 'token_file': worker_token['path']}],
            'ingress': {'owner_id': 'synthetic-owner'},
            'session_nodes': {'subjects': {'existing-chat': {'capabilities': ['agent'], 'visible_nodes': {}}},
                              'mesh_id': 'synthetic-mesh'}}
        ready_event, captured = threading.Event(), []
        def ready(server, api, channel):
            service = PhoneAuthority(api, self.config_path)
            install_dispatch(api, service, 'https://mesh.example.test')
            captured.append(server); ready_event.set()
        thread = threading.Thread(target=serve, args=(config,), kwargs={'ready': ready}, daemon=True)
        thread.start()
        self.assertTrue(ready_event.wait(5))
        try:
            for token, expected in ((operator_token, 0), (worker_token, 1)):
                transport = self.root / 'cli-transport.json'
                owned(transport, {'control_url': 'http://127.0.0.1:' + str(captured[0].server_port),
                                  'token_file': token['path']})
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    result = main(['--config', str(transport), 'plan', '--enrollment-id', 'phone1'])
                self.assertEqual(expected, result)
                self.assertNotIn('SYNTHETIC_OPERATOR', out.getvalue())
                self.assertNotIn('SYNTHETIC_WORKER', out.getvalue())
            self.assertFalse((self.owner_root / 'phone1' / 'state').exists())
        finally:
            captured[0].shutdown(); thread.join(3)

    def test_restart_restores_original_relay_without_consuming_another_challenge(self):
        self.call('prepare-existing-binding')
        nonce = self.service.relays[0][0].binding['nonce']
        self.service.relays = []
        self.owner.restore()
        self.assertEqual(nonce, self.service.relays[0][0].binding['nonce'])
        self.assertEqual('intent', self.call('status')['status']['state'])

    def test_discovery_owner_metadata_filters_capabilities_and_private_task_counts(self):
        self.store.heartbeat('original-worker', ['agent', 'leader', 'private-project'])
        settings = copy.deepcopy(self.settings['phone1'])
        settings['visible_nodes'] = {'original-worker': {'display_name': 'computer', 'capabilities': ['agent']}}
        owned(self.owner_root / 'phone1' / 'settings.json', settings)
        self.registered(); self.call('issue')
        token = json.loads((self.owner_root / 'phone1' / 'issued.json').read_text())['bootstrap']['credential']['token']
        def node(action, **values):
            return self.service.node(token, {'action': action, 'arguments': dict(values, session_id='phone1-session')})
        node('mesh_connect', control_id='connect', capabilities=['phone1:bytes'])
        discovered = node('mesh_discover')['nodes']
        computer = next(n for n in discovered if n.get('display_name') == 'computer')
        self.assertEqual(['agent'], computer['capabilities'])
        self.assertNotIn('tasks', computer)
        self.assertNotIn('private-project', json.dumps(discovered))


if __name__ == '__main__':
    unittest.main()
