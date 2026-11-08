"""Real in-process authority and provider transport; no GUI/model execution."""
import copy
import contextlib
import base64
import hashlib
import io
import json
import unittest
from unittest import mock

from assistant_mesh.capability_call import TaskCapabilityCalls
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.phone_authority import PhoneAuthority
from assistant_mesh.phone_computer_use import PhoneComputerUse, encode_blob, main, result_output
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

    def qualify(self, command='inspect_ui'):
        work = self.tool.prepare(command, 'ui-op')
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
        command = json.loads(base64.b64decode(work['workload']['input']['base64']))
        self.assertEqual({'format': CONTRACT, 'command': 'perform_action', 'action': 'tap',
                          'element_ref': 'original-view:1:0', 'text': ''}, command)
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

    def test_cli_screenshot_exports_original_bytes_privately_and_reuses_without_reserve(self):
        self.tool.commands.append('screenshot'); self.qualify('screenshot')
        self.tool.invoke('screenshot', 'ui-op', 'ui-decision', observations=self.observations, lease_seconds=40)
        accepted = self.report('accept', epoch=1, receipt_id='synthetic-device-approval')
        started = self.report('start', epoch=accepted['epoch'], receipt_id='synthetic-device-approval')
        # Synthetic marker bytes test byte preservation/storage only. No image
        # codec or actual phone screen capture is claimed by this fixture.
        jpeg = b'\xff\xd8\xff\xe0synthetic-transport-fixture\xff\xd9'
        image = dict(encode_blob({}), format='image/jpeg', width=400, height=800, display_id=0,
                     crop_left=0, crop_top=0, scale_x=0.5, scale_y=0.5, user_selected=False)
        image.update(bytes=len(jpeg), sha256=hashlib.sha256(jpeg).hexdigest(), base64=base64.b64encode(jpeg).decode())
        body = encode_blob({'format': 'amita-phone-ui-control-result/1', 'contract': CONTRACT,
            'principal': 'node:synthetic-phone', 'operation_id': 'ui-op', 'task_id': self.f.task['id'],
            'task_epoch': self.f.task['epoch'], 'command': 'screenshot', 'screenshot': image})
        saved = self.report('result', receipt_id='synthetic-device-approval', result=body)
        self.report('settle', epoch=started['epoch'], receipt_id='synthetic-device-approval', outcome='completed',
                    evidence=dict(saved, resource_quiescent=True))
        image_path, body_path = self.f.root / 'screen.jpg', self.f.root / 'original-result.json'
        for attempt in range(2):
            with mock.patch('assistant_mesh.phone_computer_use.load_entry', return_value=self.tool), contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = main(['--binding', '/SYNTHETIC/binding.json', 'result', '--operation-id', 'ui-op',
                             '--image-out', str(image_path), '--output-file', str(body_path)])
            self.assertEqual(0, code)
            metadata = json.loads(stdout.getvalue())
            self.assertNotIn('base64', stdout.getvalue()); self.assertLess(len(stdout.getvalue().encode()), 8192)
            self.assertEqual(bool(attempt), metadata['image_file']['reused'])
            self.assertEqual(str(image_path), metadata['image_file']['path'])
            self.assertEqual(0.5, metadata['screenshot']['scale_x'])
            self.assertEqual(jpeg, image_path.read_bytes()); self.assertEqual(0o600, image_path.stat().st_mode & 0o777)
            self.assertEqual(base64.b64decode(body['base64']), body_path.read_bytes())
        self.assertEqual(1, len(self.reserves()))
        image_path.write_bytes(b'different-existing-private-file')
        with mock.patch('assistant_mesh.phone_computer_use.load_entry', return_value=self.tool), contextlib.redirect_stdout(io.StringIO()):
            code = main(['--binding', '/SYNTHETIC/binding.json', 'result', '--operation-id', 'ui-op', '--image-out', str(image_path)])
        self.assertEqual(1, code); self.assertEqual(b'different-existing-private-file', image_path.read_bytes())
        self.assertEqual(1, len(self.reserves()))

    def test_screenshot_inner_hash_failure_never_creates_output(self):
        image = {'format': 'image/jpeg', 'width': 1, 'height': 1, 'display_id': 0,
                 'crop_left': 0, 'crop_top': 0, 'scale_x': 1, 'scale_y': 1, 'user_selected': False,
                 'bytes': 4, 'sha256': '0' * 64, 'base64': base64.b64encode(b'bad!').decode()}
        body = encode_blob({'format': 'amita-phone-ui-control-result/1', 'contract': CONTRACT,
            'principal': 'node:synthetic-phone', 'operation_id': 'ui-op', 'task_id': self.f.task['id'],
            'task_epoch': self.f.task['epoch'], 'command': 'screenshot', 'screenshot': image})
        receipt = {'format': 'mesh-phone-computer-use/1', 'operation_id': 'ui-op', 'task_id': self.f.task['id'],
            'original_task_epoch': self.f.task['epoch'], 'result_available': True, 'result': body,
            'provider_receipt': {'provider': 'node:synthetic-phone'}}
        path = self.f.root / 'bad.jpg'
        with self.assertRaises(ValueError): result_output(receipt, image_out=str(path))
        self.assertFalse(path.exists())
