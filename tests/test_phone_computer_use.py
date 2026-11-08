"""Real in-process authority and provider transport; no GUI/model execution."""
import copy
import contextlib
import hashlib
import io
import json
import unittest
from unittest import mock

from assistant_mesh.capability_call import TaskCapabilityCalls
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.phone_authority import PhoneAuthority
from assistant_mesh.phone_computer_use import PhoneComputerUse, encode_blob, main
from assistant_mesh.phone_control import CONTRACT
from tests import test_capability_call as fixtures
from tests.test_enrollment_relay import owned


TOKEN = 'SYNTHETIC_PHONE_COMPUTER_USE_' + 'x' * 48


class Client:
    def __init__(self, api, phone):
        self.api, self.phone, self.role, self.requests = api, phone, 'worker', []
        self.lose_reserve_ack = False

    def request(self, path, body=None):
        self.requests.append((path, copy.deepcopy(body)))
        if path == '/v1/phone/result':
            return self.phone.read_result('node:worker' if self.role == 'worker' else 'operator', **body)
        result = self.api.dispatch('POST' if body is not None else 'GET', path, body or {},
                                   {'role': self.role, 'node': 'worker'})
        if self.lose_reserve_ack and path == '/v1/allocation/action' and body['action'] == 'reserve':
            self.lose_reserve_ack = False
            raise TimeoutError('synthetic lost reserve ACK')
        return result


class PhoneComputerUseTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CapabilityCallTests(); self.f.setUp()
        self.api, self.gui = self.f.api, 'synthetic-phone:ui-control-v1'
        self.api.ingress = Ingress(self.api.store, 'authority', {'owner_id': 'owner'})
        self.api.ingress_results = IngressResults(self.api.store, 'authority', {'owner_id': 'owner'})
        self.api.install_session_nodes({'subjects': {'existing-chat': {'capabilities': [], 'visible_nodes': {}}}})
        path = self.f.root / 'phone.json'
        owned(path, {'format': 'mesh-phone-authority/1', 'enabled': True, 'nodes': [{
            'node_id': 'synthetic-phone', 'subject': 'phone', 'session_id': 'ui-session',
            'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest(), 'enabled': True,
            'capabilities': [], 'scope': ['phone:node'], 'enrollment': {},
            'control_session_id': 'service-session', 'installed_control_adapter': {
                'enabled': True, 'capability_id': self.gui, 'contract': CONTRACT,
                'actions': ['inspect', 'action'], 'epoch': 1}}]})
        self.phone = PhoneAuthority(self.api, path)
        self.client = Client(self.api, self.phone)
        self.calls = TaskCapabilityCalls(self.client, str(self.f.root / 'phone-calls.db'),
                                        'authority', 'worker', self.f.task['id'], self.f.task['epoch'])
        self.tool = PhoneComputerUse(self.calls, 'synthetic-phone', ['inspect_ui', 'perform_action'])
        self.phone_call('mesh_connect', control_id='actual-synthetic-service', capabilities=[self.gui])
        self.observations = [{'observation_id': 'synthetic-ui-probe', 'metric': 'ui.probe',
                              'unit': 'result', 'max_age_seconds': 60}]

    def tearDown(self):
        self.f.tearDown()

    def phone_call(self, action, **args):
        return self.phone.node(TOKEN, {'action': action, 'arguments': dict(args, session_id='service-session')})

    def report(self, action, **args):
        return self.phone_call('mesh_work_report', report_id='device-' + action,
            provider={'action': action, 'arguments': dict(args, operation_id='ui-op', capability_id=self.gui)})['provider']

    def qualify(self):
        work = self.tool.prepare('inspect_ui', 'ui-op')
        self.api.resources.observe('checker', self.gui, 'ui.probe', True, 'result', 'synthetic independent oracle',
            epoch=1, observation_id='synthetic-ui-probe', verification='verified',
            evidence={'scope': work['scope'], 'workload': work['workload']})
        self.api.resources.request_grant('node:worker', self.gui, 'inspect', work['scope'], request_id='ui-grant')
        self.api.resources.grant('operator', 'ui-grant')
        self.api.allocations.bind_pool('operator', self.gui, 1, 'shared', 1, 'requests', 1, 'slot')

    def invoke(self):
        return self.tool.invoke('inspect_ui', 'ui-op', 'ui-decision', observations=self.observations, lease_seconds=40)

    def reserves(self):
        return [r for r in self.client.requests if r[0] == '/v1/allocation/action' and r[1]['action'] == 'reserve']

    def test_exact_grant_and_independent_real_scope_are_required_without_fabrication(self):
        denied = self.invoke()
        self.assertEqual('exact_grant_required', denied['reason']); self.assertEqual([], self.reserves())
        self.qualify()
        self.assertTrue(self.tool.capability_status('inspect_ui', 'ui-op', observations=self.observations)['ready_to_reserve'])
        screenshot = self.tool.capability_status('screenshot', 'screen-op', observations=self.observations)
        self.assertEqual('command_not_owner_enabled_or_probed', screenshot['reason'])
        self.f.now += 61
        self.assertFalse(self.tool.capability_status('inspect_ui', 'ui-op', observations=self.observations)['available'])

    def test_original_queue_full_provider_bytes_and_no_new_task(self):
        self.qualify(); queued = self.invoke()
        self.assertEqual('reserved', queued['operation_state'])
        self.assertEqual('pending', queued['provider_receipt']['state'])
        self.assertEqual('ui-op', queued['operation_id'])
        pending = self.phone_call('mesh_work_pull', pull_id='pull')['dispatch'][0]
        self.assertEqual('ui-op', pending['operation_id'])
        accepted = self.report('accept', epoch=1, receipt_id='synthetic-device-approval')
        started = self.report('start', epoch=accepted['epoch'], receipt_id='synthetic-device-approval')
        output = encode_blob({'format': CONTRACT, 'synthetic_elements': [], 'effect_verified': False})
        recorded = self.report('result', receipt_id='synthetic-device-approval', result=output)
        self.report('settle', epoch=started['epoch'], receipt_id='synthetic-device-approval', outcome='completed',
                    evidence=dict(recorded, resource_quiescent=True))
        result = self.tool.result('ui-op')
        self.assertEqual(output, result['result']); self.assertTrue(result['result_available'])
        self.assertEqual([], result['json']['synthetic_elements']); self.assertFalse(result['execution_verified'])
        with self.api.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])

    def test_lost_reservation_ack_only_reads_original_operation_on_repeat(self):
        self.qualify(); self.client.lose_reserve_ack = True
        first = self.invoke(); self.assertEqual('unresolved', first['phase'])
        again = self.invoke(); self.assertEqual('ui-op', again['operation_id'])
        self.assertEqual(1, len(self.reserves()))
        with self.assertRaises(ValueError):
            self.tool.invoke('inspect_ui', 'ui-op', 'ui-decision', arguments={'max_elements': 2}, observations=self.observations)
        self.assertEqual(1, len(self.reserves()))

    def test_operator_cannot_use_new_task_bound_worker_path(self):
        self.qualify(); self.client.role = 'operator'
        with self.assertRaises(PermissionError): self.invoke()
        self.assertEqual([], self.reserves())

    def test_perform_action_binds_task_operation_and_rejects_remote_approval_fields(self):
        work = self.tool.prepare('perform_action', 'original-action',
                                {'action': 'tap', 'element_ref': 'original-view:1:0', 'text': ''})
        self.assertEqual('action', work['action'])
        self.assertEqual(self.f.task['id'], work['scope']['task_id'])
        with self.assertRaises(ValueError):
            self.tool.prepare('perform_action', 'original-action',
                {'action': 'tap', 'element_ref': 'view:1:0', 'text': '', 'approved': True})

    def test_cli_prepare_returns_exact_scope_workload_without_network_or_credentials(self):
        output = io.StringIO()
        with mock.patch('assistant_mesh.phone_computer_use.load_entry', return_value=self.tool), contextlib.redirect_stdout(output):
            code = main(['--binding', '/SYNTHETIC/owner-binding.json', 'prepare',
                         '--command', 'inspect_ui', '--operation-id', 'ui-op'])
        self.assertEqual(0, code)
        value = json.loads(output.getvalue())
        self.assertEqual(self.tool.prepare('inspect_ui', 'ui-op'), value)
        self.assertEqual([], self.client.requests)
        self.assertNotIn(TOKEN, output.getvalue())
