"""Private installed-member reload, independent of onboarding task freshness."""
import copy
import contextlib
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from assistant_mesh.phone_control import CONTRACT
from assistant_mesh.phone_owner import PREFIX, install_dispatch, main
from assistant_mesh.store import Conflict
from tests import test_phone_authority as fixtures
from tests.test_enrollment_relay import owned


class PhoneReloadTests(unittest.TestCase):
    def setUp(self):
        self.phone = fixtures.PhoneAuthorityTests(); self.phone.setUp()
        self.service, self.api, self.f = self.phone.service, self.phone.api, self.phone.original
        self.api.dispatch = lambda *unused: {'original': True}
        install_dispatch(self.api, self.service, 'https://mesh.example.test')

    def tearDown(self):
        self.phone.tearDown()

    def write(self, candidate):
        owned(self.phone.config_path, candidate)
        return hashlib.sha256(self.phone.config_path.read_bytes()).hexdigest()

    def reload(self, digest, role='operator', **extra):
        return self.api.dispatch('POST', PREFIX + 'reload-installed',
                                 dict(extra, expected_sha256=digest), {'role': role})

    def maintenance(self):
        candidate = copy.deepcopy(self.phone.config)
        candidate['nodes'][0]['scope'].append('phone:maintenance')
        candidate['nodes'][0]['maintenance'] = {'enabled': True, 'diagnostics': ['signed_data_sync']}
        return candidate

    def test_operator_only_exact_digest_bad_candidate_does_not_replace_installed(self):
        original = copy.deepcopy(self.service.installed)
        subjects, bindings = copy.deepcopy(self.service.sessions.subjects), copy.deepcopy(self.service.sessions.node_bindings)
        candidate = self.maintenance(); digest = self.write(candidate)
        for role in ('worker', 'viewer', 'ingress', None):
            with self.assertRaises(PermissionError): self.reload(digest, role)
        with self.assertRaises(Conflict): self.reload('0' * 64)
        with self.assertRaises(ValueError): self.reload(digest, config_path='/tmp/not-allowed')
        bad = copy.deepcopy(candidate); bad['nodes'][0]['token_sha256'] = 'c' * 64
        with self.assertRaises(PermissionError): self.reload(self.write(bad))
        bad = copy.deepcopy(candidate); bad['nodes'][0]['maintenance']['diagnostics'] = ['shell']
        with self.assertRaises(ValueError): self.reload(self.write(bad))
        self.assertEqual(original, self.service.installed)
        self.assertEqual(subjects, self.service.sessions.subjects)
        self.assertEqual(bindings, self.service.sessions.node_bindings)
        self.assertEqual(1, self.f.row_count('capabilities'))

    def test_terminal_original_task_and_same_digest_retry_preserve_identity_and_epochs(self):
        self.f.store.update_task(self.f.task['id'], 'laptop', self.f.task['epoch'], status='completed')
        candidate = self.maintenance(); digest = self.write(candidate)
        first = self.reload(digest)
        self.assertFalse(first['already_installed']); self.assertEqual(digest, first['installed_sha256'])
        self.assertEqual('cloud', self.service.principal(fixtures.NODE_TOKEN, 'phone:maintenance')['node_id'])
        self.f.now += 5
        second = self.reload(digest)
        self.assertTrue(second['already_installed']); self.assertEqual(digest, second['installed_sha256'])
        for key in ('node_id', 'subject', 'session_id', 'token_sha256', 'enrollment', 'capabilities'):
            self.assertEqual(self.phone.config['nodes'][0][key], self.service.installed['nodes'][0][key])
        self.assertEqual(1, self.f.registry.describe('target')['epoch'])
        self.assertEqual(1, self.f.row_count('tasks'))
        self.assertFalse(second['credentials_rotated']); self.assertFalse(second['unknown_settled'])
        transport = self.phone.config_path.parent / 'operator.json'; owned(transport, {})
        client = SimpleNamespace(request=lambda path, body: self.api.dispatch('POST', path, body, {'role': 'operator'}))
        with patch('assistant_mesh.worker.Client', return_value=client), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, main(['--config', str(transport), 'reload-installed', '--expected-sha256', digest]))
        self.assertEqual(digest, json.loads(output.getvalue())['installed_sha256'])

    def test_new_declared_control_starts_unavailable_and_reload_never_revives_revoke(self):
        candidate = self.maintenance(); node = candidate['nodes'][0]
        node['control_session_id'] = 'actual-control-service'
        node['installed_control_adapter'] = {'enabled': True, 'capability_id': 'cloud:ui-control-v1',
            'contract': CONTRACT, 'actions': ['inspect', 'action'], 'epoch': 1}
        digest = self.write(candidate); self.reload(digest)
        cap = self.f.registry.describe('cloud:ui-control-v1')
        self.assertFalse(cap['available']); self.assertEqual('declared', cap['verification'])
        self.assertEqual(1, cap['epoch']); self.assertNotIn('enrollment_publication_sha256', cap['spec'])
        self.service.node(fixtures.NODE_TOKEN, {'action': 'mesh_connect', 'arguments': {
            'session_id': 'actual-control-service', 'control_id': 'real-connect', 'capabilities': ['cloud:ui-control-v1']}})
        before = self.f.registry.describe('cloud:ui-control-v1')['deadline']
        self.f.now += 5; self.reload(digest)
        self.assertEqual(before, self.f.registry.describe('cloud:ui-control-v1')['deadline'])
        self.f.registry.revoke('operator', 'cloud:ui-control-v1', 1)
        candidate['nodes'][0]['maintenance']['diagnostics'].append('receipt_reconciliation')
        self.reload(self.write(candidate))
        self.assertTrue(self.f.registry.describe('cloud:ui-control-v1')['revoked'])
        self.assertFalse(self.f.registry.describe('cloud:ui-control-v1')['available'])
