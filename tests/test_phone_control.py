"""Installed GUI contract, independent presence, original allocations/results."""
import base64
import copy
import hashlib
import json
import unittest

from assistant_mesh.phone_control import CONTRACT, validate_adapter
from assistant_mesh.store import Conflict
from tests import test_phone_authority as fixtures
from tests.test_enrollment_relay import owned


def blob(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'base64': base64.b64encode(raw).decode()}


class PhoneControlTests(unittest.TestCase):
    def setUp(self):
        self.phone = fixtures.PhoneAuthorityTests(); self.phone.setUp()
        self.f, self.service = self.phone.original, self.phone.service
        self.node = self.phone.config['nodes'][0]
        self.gui = 'cloud:ui-control-v1'
        self.node['control_session_id'] = 'control-service-session'
        self.node['installed_control_adapter'] = {'enabled': True, 'capability_id': self.gui,
            'contract': CONTRACT, 'actions': ['inspect', 'action'], 'epoch': 1}
        self.service._config(self.phone.config)
        self.service.installed = copy.deepcopy(self.phone.config)
        owned(self.phone.config_path, self.phone.config)
        self.service.control.install(self.node)

    def tearDown(self):
        self.phone.tearDown()

    def call(self, action, **args):
        args['session_id'] = 'control-service-session'
        return self.service.node(fixtures.NODE_TOKEN, {'action': action, 'arguments': args})

    def connect(self):
        return self.call('mesh_connect', control_id='actual-service-connect', capabilities=[self.gui])

    def report(self, action, **args):
        args.setdefault('operation_id', 'gui-operation'); args.setdefault('capability_id', self.gui)
        return self.call('mesh_work_report', report_id='gui-' + action,
                         provider={'action': action, 'arguments': args})['provider']

    def reserve(self, workload=None, operation='gui-operation'):
        workload = workload or {'input': blob(b'{"format":"amita-phone-ui-control/1","command":"inspect"}')}
        selection = {'capability_id': self.gui, 'epoch': 1, 'action': 'inspect',
                     'scope': self.f.scope, 'workload': workload,
                     'observations': [{'observation_id': 'gui-measured', 'metric': 'latency.future',
                                      'unit': 'ms', 'max_age_seconds': 60}]}
        self.f.registry.observe('probe:independent', self.gui, 'latency.future', 3.2, 'ms', 'actual test probe',
            evidence={'scope': self.f.scope, 'workload': workload}, verification='verified',
            epoch=1, observation_id='gui-measured')
        self.f.registry.request_grant(self.f.actor, self.gui, 'inspect', self.f.scope, request_id='gui-grant')
        self.f.registry.grant('operator', 'gui-grant')
        self.f.binding(self.gui)
        return self.f.reserve(operation=operation,
            plan={'target': self.gui, 'selections': [selection], 'route': []})

    def test_owner_install_is_declared_unavailable_and_rejects_arbitrary_contract(self):
        cap = self.f.registry.describe(self.gui)
        self.assertFalse(cap['available']); self.assertEqual('declared', cap['verification'])
        self.assertEqual('owner_installed_adapter', cap['spec']['installation_authority'])
        self.assertNotIn('enrollment_publication_sha256', cap['spec'])
        bad = copy.deepcopy(self.node); bad['installed_control_adapter']['actions'] = ['shell']
        with self.assertRaises(ValueError): validate_adapter(bad)
        bad = copy.deepcopy(self.node); bad['installed_control_adapter']['capability_id'] = 'foreign:gui'
        with self.assertRaises(ValueError): validate_adapter(bad)
        with self.assertRaises(PermissionError):
            self.phone.call('mesh_node_update', control_id='wrong-session', capabilities=[self.gui])
        with self.assertRaises(PermissionError):
            self.call('mesh_connect', control_id='wrong-cap', capabilities=['target'])

    def test_real_service_presence_does_not_fabricate_node_or_depend_on_ui(self):
        count = self.f.row_count('nodes')
        view = self.connect()['session']
        self.assertEqual('cloud', view['node_id']); self.assertEqual('control-service-session', view['session_id'])
        self.assertEqual(count, self.f.row_count('nodes'))
        self.phone.call('mesh_node_update', control_id='ui-left', capabilities=[], online=False)
        self.assertTrue(self.f.registry.describe(self.gui)['available'])
        self.call('mesh_node_update', control_id='service-disconnected', capabilities=[], online=False)
        self.assertFalse(self.f.registry.describe(self.gui)['available'])
        self.call('mesh_node_update', control_id='service-returned', capabilities=[self.gui], online=True)
        self.assertEqual(1, self.f.registry.describe(self.gui)['epoch'])
        self.assertTrue(self.f.registry.describe(self.gui)['available'])
        self.f.now += 61
        self.assertFalse(self.f.registry.describe(self.gui)['available'])

    def test_original_dispatch_start_receipt_and_full_result_with_session_boundary(self):
        self.connect(); self.reserve()
        self.assertEqual([], self.phone.call('mesh_work_pull', pull_id='ui-pull')['dispatch'])
        pending = self.call('mesh_work_pull', pull_id='service-pull')['dispatch'][0]
        self.assertEqual('node:cloud', pending['provider'])
        with self.assertRaises(PermissionError):
            self.phone.report('inspect', operation_id='gui-operation', capability_id=self.gui)
        accepted = self.report('accept', epoch=1, receipt_id='real-device-approval')
        self.phone.call('mesh_node_update', control_id='ui-background', capabilities=[], online=False)
        started = self.report('start', epoch=accepted['epoch'], receipt_id='real-device-approval')
        self.assertTrue(started['execute_once'])
        self.assertFalse(self.report('start', epoch=accepted['epoch'], receipt_id='real-device-approval')['execute_once'])
        body = blob(b'{"device_receipt":"actual-local-session-reported"}')
        result = self.report('result', receipt_id='real-device-approval', result=body)
        self.assertEqual(body['sha256'], result['sha256'])
        settled = self.report('settle', epoch=started['epoch'], receipt_id='real-device-approval',
                              outcome='completed', evidence=dict(result, resource_quiescent=True))
        self.assertEqual('completed', settled['state'])
        self.assertEqual(body['base64'], self.service.read_result(self.f.actor, 'gui-operation', self.gui)['base64'])
        self.assertEqual(1, self.f.row_count('tasks'))

    def test_oversized_or_non_json_commands_are_not_dispatched_or_started(self):
        with self.assertRaises(ValueError):
            self.service.control.validate_selection(self.node,
                {'capability_id': self.gui, 'epoch': 1, 'action': 'inspect',
                 'workload': {'input': blob(b'x' * 65537)}})
        self.connect(); self.reserve(workload={'input': blob(b'not-json')})
        pulled = self.call('mesh_work_pull', pull_id='oversized')
        self.assertEqual([], pulled['dispatch'])
        self.assertEqual('gui-operation', pulled['rejected_dispatch'][0]['operation_id'])
        with self.assertRaises(ValueError):
            self.report('accept', epoch=1, receipt_id='cannot-approve-invalid-input')

    def test_revoke_and_owner_disable_do_not_revive(self):
        self.connect()
        with self.f.store.transaction() as db:
            db.execute('UPDATE capabilities SET revoked=1 WHERE id=?', (self.gui,))
        self.call('mesh_node_update', control_id='after-revoke', capabilities=[self.gui])
        self.assertFalse(self.f.registry.describe(self.gui)['available'])
        self.node['installed_control_adapter']['enabled'] = False
        owned(self.phone.config_path, self.phone.config)
        with self.assertRaises(PermissionError): self.connect()
        self.service.installed = copy.deepcopy(self.phone.config)
        with self.assertRaises(PermissionError): self.connect()
