"""Target-scoped app launch through the real phone, allocation and reader routes."""
import copy
import json
import unittest

from assistant_mesh.phone_control import CONTRACT
from assistant_mesh.phone_screen_share import LAUNCH_COMMAND, LAUNCH_FORMAT, stream_policy
from assistant_mesh.phone_screen_share_reader import OBSERVATION_ROUTE, ROUTE, install_dispatch
from assistant_mesh.store import Conflict
from tests import test_phone_reload as reload_fixtures
from tests.test_phone_authority import NODE_TOKEN
from tests.test_phone_control import blob


class ScreenShareLaunchTests(unittest.TestCase):
    def setUp(self):
        self.owner = reload_fixtures.PhoneReloadTests()
        self.owner.setUp()
        self.addCleanup(self.owner.tearDown)
        self.f, self.api, self.service = self.owner.f, self.owner.api, self.owner.service
        self.config = copy.deepcopy(self.service.installed)
        node = self.config['nodes'][0]
        self.cap = node['node_id'] + ':ui-control-v1'
        self.session = node['control_session_id'] = 'control-session'
        self.adapter = node['installed_control_adapter'] = {
            'enabled': True, 'capability_id': self.cap, 'contract': CONTRACT,
            'actions': ['inspect'], 'epoch': 1, 'screen_share_enabled': True}
        self.owner.reload(self.owner.write(self.config))
        self.connect('v1-connect')
        self.f.binding(self.cap)
        install_dispatch(self.api, self.service)
        self.target = {'bundle_name': 'org.example.app', 'ability_name': 'EntryAbility'}
        self.body = {'format': LAUNCH_FORMAT, 'command': LAUNCH_COMMAND, 'duration_ms': 30000,
                     'max_fps': 1, 'target': self.target}
        self.task = {'task_id': self.f.task['id'], 'task_epoch': self.f.task['epoch']}
        self.scope = dict(self.task, purpose=LAUNCH_COMMAND, target=self.target)

    def call(self, action, **arguments):
        return self.service.node(NODE_TOKEN, {'action': action, 'arguments': dict(arguments, session_id=self.session)})

    def connect(self, control_id):
        return self.call('mesh_connect', control_id=control_id, capabilities=[self.cap])['session']

    def upgrade(self):
        self.adapter.update(screen_share_launch_enabled=True, epoch=2)
        digest = self.owner.write(self.config)
        self.owner.reload(digest)
        self.assertTrue(self.owner.reload(digest)['already_installed'])
        self.assertFalse(self.f.registry.describe(self.cap)['available'])
        with self.assertRaises(Conflict):
            self.connect('v1-connect')
        self.assertTrue(self.connect('v2-connect')['installed_adapter']['screen_share_launch_enabled'])
        self.f.binding(self.cap, capability_epoch=2, expected_epoch=1)

    def grant(self, action, scope, request):
        self.f.registry.request_grant(self.f.actor, self.cap, action, scope, request_id=request)
        return self.f.registry.grant('operator', request)['id']

    def selection(self, grant, **overrides):
        return dict({'capability_id': self.cap, 'epoch': 2, 'action': 'action', 'scope': self.scope,
                     'workload': {'input': blob(json.dumps(self.body).encode())}, 'grant_id': grant}, **overrides)

    def worker(self, route, request):
        return self.api.dispatch('POST', route, request, {'role': 'worker', 'node': 'laptop'})

    def observe(self, selection):
        return self.worker(OBSERVATION_ROUTE, dict(self.task, selection=selection))

    def reserve(self, selection):
        selection['observations'] = [self.observe(selection)]
        self.f.reserve(operation='launch-op', plan={'target': self.cap, 'selections': [selection], 'route': []})

    def report(self, action, epoch):
        return self.call('mesh_work_report', report_id=action, provider={'action': action, 'arguments': {
            'operation_id': 'launch-op', 'capability_id': self.cap, 'epoch': epoch, 'receipt_id': 'phone-receipt'}})['provider']

    def test_owner_upgrade_target_grant_dispatch_and_private_frames(self):
        old = self.grant('inspect', dict(self.task, purpose='screen_share'), 'old-inspect')
        with self.assertRaises(PermissionError):
            self.observe(self.selection(old, epoch=1))
        self.upgrade()
        with self.assertRaises(PermissionError):
            self.observe(self.selection(old))
        for action, scope, identity in (
                ('inspect', self.scope, 'same-scope-inspect'),
                ('action', dict(self.scope, target=dict(self.target, ability_name='Other')), 'other-ability')):
            with self.assertRaises(PermissionError):
                self.observe(self.selection(self.grant(action, scope, identity)))
        launch = self.grant('action', self.scope, 'launch')
        for changes in ({'action': 'inspect'}, {'scope': dict(self.scope, target=dict(self.target, bundle_name='other'))},
                        {'scope': dict(self.scope, task_epoch=True)}, {'epoch': 1}):
            with self.assertRaises((ValueError, PermissionError)):
                self.observe(self.selection(launch, **changes))
        self.reserve(self.selection(launch))
        dispatch = self.call('mesh_work_pull', pull_id='requested')['dispatch'][0]
        self.assertEqual('launch-op', dispatch['operation_id'])
        accepted = self.report('accept', dispatch['epoch'])
        self.assertTrue(self.report('start', accepted['epoch'])['execute_once'])
        self.assertFalse(self.report('start', accepted['epoch'])['execute_once'])
        lookup = dict(self.task, operation_id='launch-op', capability_id=self.cap)
        binding = dict(lookup, primary_session_id='phone-session', stream_id='launch-stream',
                       binding_digest='a' * 64, expires_at=self.f.now + 20)
        self.call('mesh_screen_share_open', **binding)
        frame = dict(blob(b'\xff\xd8\xffsynthetic pixels\xff\xd9'), revision=1, capture_timestamp=self.f.now,
                     width=8, height=8)
        self.call('mesh_screen_share_frame', frame=frame, **binding)
        self.assertEqual(frame, self.worker(ROUTE, lookup)['frame'])
        with self.assertRaises(PermissionError):
            self.api.dispatch('POST', ROUTE, lookup, {'role': 'worker', 'node': 'elsewhere'})
        self.f.registry.revoke_grant('operator', launch)
        with self.assertRaises(PermissionError):
            self.worker(ROUTE, lookup)
        self.assertFalse(self.service.screen_share._slots)
        self.assertEqual('running', self.f.bridge.inspect(self.f.actor, 'launch-op')['state'])

    def test_reject_extra_fields_urls_own_app_and_arbitrary_action(self):
        for body in (dict(self.body, url='https://example.test'), dict(self.body, format=CONTRACT),
                     dict(self.body, max_fps=True), dict(self.body, duration_ms=999),
                     dict(self.body, target=dict(self.target, extras={})),
                     dict(self.body, target=dict(self.target, bundle_name='org.personalassistant.mesh')),
                     *(dict(self.body, target=dict(self.target, ability_name=name))
                       for name in ('https://example.test', '.Entry', 'A/B', '权', 'A\n', 'A' * 129))):
            with self.subTest(body=body), self.assertRaises(PermissionError):
                stream_policy(body)
        self.upgrade()
        grant = self.grant('action', self.scope, 'launch')
        generic = self.selection(grant, workload={'input': blob(b'{"command":"arbitrary"}')})
        with self.assertRaises(PermissionError):
            self.observe(generic)
        # Even a legacy adapter that already supports generic action cannot
        # consume a target-scoped launch grant with a different workload.
        node = copy.deepcopy(self.config['nodes'][0])
        node['installed_control_adapter']['actions'].append('action')
        with self.assertRaises(PermissionError):
            self.service.control.validate_selection(node, generic)

    def test_upgrade_refuses_changed_actions_and_unsettled_original_dispatch(self):
        self.adapter.update(screen_share_launch_enabled=True, epoch=2, actions=['inspect', 'action'])
        with self.assertRaises(PermissionError):
            self.owner.reload(self.owner.write(self.config))
        self.adapter.update(screen_share_launch_enabled=False, epoch=1, actions=['inspect'])
        self.owner.reload(self.owner.write(self.config))
        scope = dict(self.task, purpose='screen_share')
        body = {'format': 'amita-phone-screen-share/1', 'command': 'screen_share', 'duration_ms': 30000, 'max_fps': 1}
        self.reserve(self.selection(self.grant('inspect', scope, 'original'), epoch=1, action='inspect',
                                    scope=scope, workload={'input': blob(json.dumps(body).encode())}))
        self.report('accept', 1)
        self.adapter.update(screen_share_launch_enabled=True, epoch=2)
        with self.assertRaisesRegex(Conflict, 'unsettled_control_dispatch'):
            self.owner.reload(self.owner.write(self.config))
        self.assertEqual(1, self.f.registry.describe(self.cap)['epoch'])
        self.assertEqual('accepted', self.f.bridge.inspect(self.f.actor, 'launch-op')['state'])
