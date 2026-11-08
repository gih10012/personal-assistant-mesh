"""Original Registry leases and original task queue; no model/device claims."""
import copy
import json

from assistant_mesh.phone_maintenance import device_scope
from assistant_mesh.store import Conflict
from tests.test_enrollment_relay import owned
from tests.test_phone_authority import NODE_TOKEN, PhoneAuthorityTests


class PhoneRuntimeTests(PhoneAuthorityTests):
    # Inherit the original transport/allocation regression checks as well.
    def _registration(self):
        node = self.config['nodes'][0]
        node['enrollment'] = {'registration_receipt': {'registry_ref': 'capability:target:1',
            'publication_sha256': 'd' * 64}}
        with self.original.store.transaction() as db:
            db.execute('UPDATE capabilities SET spec=?,deadline=? WHERE id=?',
                       (json.dumps({'enrollment_publication_sha256': 'd' * 64}), 999, 'target'))
        self.service.installed = copy.deepcopy(self.config)
        owned(self.config_path, self.config)

    def test_expired_registration_renews_same_epoch_only_to_real_presence(self):
        self._registration()
        result = self.connect()
        cap = self.original.registry.describe('target')
        self.assertTrue(cap['available'])
        self.assertEqual(1, cap['epoch'])
        self.assertEqual(result['session']['network']['expires_at'], cap['deadline'])
        self.assertEqual(1, self.original.row_count('capabilities'))
        self.original.now = cap['deadline'] + 1
        self.assertFalse(self.original.registry.describe('target')['available'])
        self.connect()
        self.assertTrue(self.original.registry.describe('target')['available'])

    def test_offline_withdrawal_then_revoke_and_epoch_change_never_resurrect(self):
        self._registration(); self.connect()
        self.call('mesh_node_update', control_id='offline', capabilities=[], online=False)
        self.assertFalse(self.original.registry.describe('target')['available'])
        with self.original.store.transaction() as db:
            db.execute('UPDATE capabilities SET revoked=1 WHERE id=?', ('target',))
        self.call('mesh_node_update', control_id='online', capabilities=['target'], online=True)
        self.assertFalse(self.original.registry.describe('target')['available'])
        with self.original.store.transaction() as db:
            db.execute('UPDATE capabilities SET revoked=0,epoch=2 WHERE id=?', ('target',))
        self.call('mesh_node_update', control_id='newer', capabilities=['target'], online=True)
        cap = self.original.registry.describe('target')
        self.assertEqual(2, cap['epoch']); self.assertFalse(cap['available'])

    def enable_maintenance(self):
        node = self.config['nodes'][0]
        node['scope'].append('phone:maintenance')
        node['maintenance'] = {'enabled': True, 'diagnostics': ['signed_data_sync', 'receipt_reconciliation']}
        self.service.installed = copy.deepcopy(self.config)
        owned(self.config_path, self.config)

    def maintenance(self, action='submit', incident='original-incident', snapshot=None):
        args = {'session_id': 'phone-session', 'incident_id': incident}
        if action == 'submit':
            args['snapshot'] = snapshot or {'diagnostic': 'signed_data_sync', 'state': 'verification_failed',
                                           'data_version': 7, 'pending_receipts': 1}
        return self.service.node(NODE_TOKEN, {'action': 'mesh_maintenance_' + action, 'arguments': args})

    def test_maintenance_opt_in_narrow_snapshot_and_original_id_admission(self):
        with self.assertRaises(PermissionError):
            self.maintenance()
        self.enable_maintenance()
        # Background callback admission does not fabricate foreground presence.
        self.call('mesh_node_update', control_id='background', capabilities=[], online=False)
        first = self.maintenance()
        second = self.maintenance()
        self.assertTrue(first['task_created']); self.assertFalse(second['task_created'])
        self.assertEqual(first['task_id'], second['task_id'])
        self.assertEqual(first['operation_id'], second['operation_id'])
        self.assertEqual(2, self.original.row_count('tasks'))
        with self.assertRaises(Conflict):
            self.maintenance(snapshot={'diagnostic': 'signed_data_sync', 'state': 'unconfirmed',
                                       'data_version': 7, 'pending_receipts': 1})
        with self.assertRaises(PermissionError):
            self.maintenance('status', 'foreign-incident')
        with self.assertRaises(ValueError):
            self.maintenance(snapshot={'diagnostic': 'node_connection', 'state': 'unconfirmed',
                                       'data_version': 7, 'pending_receipts': 1})

    def test_real_queue_worker_result_device_memory_scope_and_disable(self):
        self.enable_maintenance()
        first = self.maintenance()
        self.original.store.heartbeat('maintenance-worker', ['agent', 'phone-maintenance'])
        task = self.original.store.claim('maintenance-worker')
        self.assertEqual(first['task_id'], task['id'])
        self.assertEqual((device_scope('cloud'), device_scope('cloud')),
                         self.original.store.memory_scopes(task['context']))
        self.assertEqual('D1', task['context']['governance']['difficulty'])
        self.assertNotEqual('leader:owner', task['scope'])
        self.original.store.update_task(task['id'], 'maintenance-worker', task['epoch'],
                                        status='completed', result='Original signed data trust was retained.')
        result = self.maintenance('result')
        self.assertEqual('completed', result['status'])
        self.assertEqual('Original signed data trust was retained.', result['result'])
        self.assertEqual('not_configured', result['local_cognition'])
        self.assertFalse(result['automatic_repair'])
        self.config['nodes'][0]['enabled'] = False
        owned(self.config_path, self.config)
        with self.assertRaises(PermissionError):
            self.maintenance('result')

    def test_different_device_cannot_read_original_incident(self):
        self.enable_maintenance(); self.maintenance()
        # Same token does not adopt a changed owner binding without ready reload.
        self.config['nodes'][0]['node_id'] = 'another-phone'
        owned(self.config_path, self.config)
        with self.assertRaises(PermissionError):
            self.maintenance('status')
