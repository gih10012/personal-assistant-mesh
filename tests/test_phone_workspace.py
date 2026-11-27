import base64
import hashlib
import unittest

from assistant_mesh.phone_authority import PhoneAuthority
from assistant_mesh.phone_workspace import FORMAT, STATUS, SUFFIX, VERIFIER, WRITE
from assistant_mesh.session_nodes import LocalSessionNodes
from tests import test_phone_authority as fixture
from tests.test_enrollment_relay import owned


class PhoneWorkspaceTest(unittest.TestCase):
    def test_authentication_observation_and_original_write_receipt(self):
        handset = fixture.PhoneAuthorityTests()
        handset.setUp()
        self.addCleanup(handset.tearDown)
        node = handset.config['nodes'][0]
        policy = handset.config_path.with_name('workspace-owner.json')
        owned(policy, {'format': FORMAT, 'node_id': node['node_id'], 'session_id': node['session_id']})
        handset.api.session_nodes = LocalSessionNodes(handset.service.sessions.backend, {
            'existing-chat': {'capabilities': ['agent'], 'visible_nodes': {}}})
        handset.service = PhoneAuthority(handset.api, handset.config_path)
        f = handset.original
        cap = node['node_id'] + SUFFIX
        self.assertTrue(handset.call(STATUS)['workspace']['enabled'])
        self.assertFalse(f.registry.describe(cap)['available'])
        handset.call('mesh_connect', control_id='workspace-ready', capabilities=['target', cap])
        self.assertTrue(f.registry.describe(cap)['available'])
        f.registry.trusted_verifiers |= {VERIFIER}
        f.binding(cap)

        task = {'task_id': f.task['id'], 'task_epoch': f.task['epoch']}
        path = 'agent-note.txt'
        content = b'original workspace data'
        size = len(content)
        digest = hashlib.sha256(content).hexdigest()
        inputs = {'bytes': size, 'sha256': digest, 'base64': base64.b64encode(content).decode()}
        scope = dict(task, path=path)
        f.registry.request_grant(f.actor, cap, WRITE, scope, request_id='workspace-owner-grant')
        grant = f.registry.grant('operator', 'workspace-owner-grant')['id']
        selection = {'capability_id': cap, 'epoch': 1, 'action': WRITE, 'scope': scope,
                     'workload': {'path': path, 'input': inputs}, 'grant_id': grant}
        with self.assertRaises(ValueError):
            handset.service.workspace.observe(f.actor, dict(task, selection=dict(selection,
                workload={'path': '../escape', 'input': inputs})))
        observation = handset.service.workspace.observe(f.actor, dict(task, selection=selection))
        selection['observations'] = [observation]
        op = 'original-workspace-write'
        f.reserve(operation=op, plan={'target': cap, 'selections': [selection]})
        pulled = handset.call('mesh_work_pull', pull_id='original-workspace-pull')['dispatch']
        self.assertEqual([op], [row['operation_id'] for row in pulled])
        args = {'operation_id': op, 'capability_id': cap, 'receipt_id': 'original-device-write'}
        accepted = handset.report('accept', epoch=pulled[0]['epoch'], **args)
        started = handset.report('start', epoch=accepted['epoch'], report_id='first-start', **args)
        self.assertTrue(started['execute_once'])
        repeated = handset.report('start', epoch=accepted['epoch'], report_id='same-operation-repeat', **args)
        self.assertFalse(repeated['execute_once'])
        policy.unlink()
        self.assertFalse(handset.call(STATUS)['workspace']['enabled'])
        self.assertEqual('running', f.bridge.inspect(f.actor, op)['state'])
