"""Synthetic pixels and identities against the real incumbent Allocation DB.

No device, production config, HTTP/network, model, new grant mechanism or frame
database. Existing fixtures create their original synthetic task and dispatch.
"""
import base64
import copy
import hashlib
import json
import unittest

from assistant_mesh.store import Conflict
from assistant_mesh.phone_control import CONTRACT, spec, validate_adapter
from assistant_mesh.phone_screen_share import stream_policy
from tests import test_phone_control as control_fixtures
from tests import test_phone_authority as authority_fixtures
from tests.test_enrollment_relay import owned


class PhoneScreenShareTests(unittest.TestCase):
    def setUp(self):
        self.control = control_fixtures.PhoneControlTests()
        self.control.phone = authority_fixtures.PhoneAuthorityTests(); self.control.phone.setUp()
        self.control.f, self.control.service = self.control.phone.original, self.control.phone.service
        self.control.node = self.control.phone.config['nodes'][0]
        self.control.gui = 'cloud:ui-control-v1'
        self.control.node['control_session_id'] = 'control-service-session'
        self.control.node['installed_control_adapter'] = {'enabled': True, 'capability_id': self.control.gui,
            'contract': CONTRACT, 'actions': ['inspect', 'action'], 'epoch': 1, 'screen_share_enabled': True}
        self.control.service._config(self.control.phone.config)
        self.control.service.installed = copy.deepcopy(self.control.phone.config)
        owned(self.control.phone.config_path, self.control.phone.config)
        self.control.service.control.install(self.control.node)
        self.f, self.service = self.control.f, self.control.service
        self.control.connect()
        self.control.reserve(workload={'input': control_fixtures.blob(
            b'{"format":"amita-phone-screen-share/1","command":"screen_share","duration_ms":60000,"max_fps":2}')})
        accepted = self.control.report('accept', epoch=1, receipt_id='original-provider-receipt')
        self.control.report('start', epoch=accepted['epoch'], receipt_id='original-provider-receipt')
        self.binding = {'primary_session_id': 'phone-session', 'session_id': 'control-service-session',
            'stream_id': 'original-stream', 'task_id': self.f.task['id'], 'task_epoch': self.f.task['epoch'],
            'operation_id': 'gui-operation', 'capability_id': self.control.gui,
            'binding_digest': 'b' * 64, 'expires_at': self.f.now + 35}

    def tearDown(self):
        self.control.tearDown()

    def call(self, action, binding=None, **arguments):
        args = dict(self.binding if binding is None else binding, **arguments)
        return self.service.node(authority_fixtures.NODE_TOKEN,
            {'action': 'mesh_screen_share_' + action, 'arguments': args})

    def frame(self, revision=1):
        raw = b'\xff\xd8\xff' + b'synthetic pixels' + b'\xff\xd9'
        return {'revision': revision, 'capture_timestamp': 1234.5 + revision * 1000,
                'width': 640, 'height': 320, **control_fixtures.blob(raw)}

    def test_actual_original_running_read_only_dispatch_stores_only_latest_private_bytes(self):
        counts = {table: self.f.row_count(table) for table in ('tasks', 'managed_allocations', 'managed_dispatch')}
        opened = self.call('open')
        self.assertFalse(opened['acknowledged']); self.assertEqual(0, opened['bytes'])
        first = self.frame()
        ack = self.call('frame', frame=first)
        self.assertTrue(ack['acknowledged']); self.assertEqual(first['sha256'], ack['frame_sha256'])
        self.assertEqual(self.binding['task_epoch'], ack['task_epoch'])
        self.assertNotIn('base64', ack); self.assertNotIn('nonce', str(ack)); self.assertNotIn('approval', str(ack))
        read = self.service.screen_share.read(self.f.actor, self.binding)
        self.assertEqual(first['base64'], read['frame']['base64'])
        self.assertEqual(ack, self.call('frame', frame=first))  # exact lost-ACK reconciliation
        self.f.now += 1
        second = self.frame(2); self.call('frame', frame=second)
        self.assertEqual(2, self.service.screen_share.read(self.f.actor, self.binding)['frame']['revision'])
        self.assertEqual(1, len(self.service.screen_share._slots))
        self.assertEqual(counts, {table: self.f.row_count(table) for table in counts})
        self.assertEqual(0, self.f.row_count('phone_result_blobs'))
        self.call('close')
        self.assertEqual(b'', self.service.screen_share._slots['cloud'].body)
        with self.assertRaises(Conflict): self.service.screen_share.read(self.f.actor, self.binding)

    def test_default_deny_foreign_principal_worker_epoch_session_and_action(self):
        self.call('open'); self.call('frame', frame=self.frame())
        for actor in ('operator', 'node:cloud', 'node:another-worker'):
            with self.assertRaises(PermissionError): self.service.screen_share.read(actor, self.binding)
        self.assertTrue(self.service.screen_share.read(self.f.actor, self.binding)['frame'])
        for key, value in (('task_epoch', self.binding['task_epoch'] + 1), ('primary_session_id', 'foreign-primary'),
                           ('session_id', 'phone-session'), ('capability_id', 'target')):
            with self.assertRaises((PermissionError, Conflict)):
                self.call('status', binding=dict(self.binding, **{key: value}))
        with self.assertRaises(PermissionError):
            self.service.node('NOT_THE_NODE_TOKEN_' + 'z' * 40,
                {'action': 'mesh_screen_share_status', 'arguments': self.binding})
        # A node cannot promote an action grant or provider-owned capability to
        # read-only sharing by sending approval flags or a correlation digest.
        with self.assertRaises(ValueError): self.call('open', approved=True)
        with self.f.store.transaction() as db:
            row = self.f.bridge._allocation(db, 'gui-operation')
            plan = json.loads(row['plan']); plan['selections'][0]['action'] = 'action'
            db.execute('UPDATE managed_allocations SET plan=? WHERE operation_id=?',
                       (json.dumps(plan), 'gui-operation'))
        with self.assertRaises((PermissionError, Conflict)): self.call('open')

    def test_revoke_task_epoch_presence_or_owner_loss_erases_pixels_on_next_admission(self):
        mutations = (
            ("UPDATE tasks SET epoch=epoch+1 WHERE id=?", (self.f.task['id'],)),
            ("UPDATE capabilities SET revoked=1 WHERE id=?", (self.control.gui,)),
            ("UPDATE managed_dispatch SET state='unknown' WHERE operation_id=?", ('gui-operation',)),
        )
        for statement, params in mutations:
            with self.subTest(statement=statement):
                self.call('open'); self.call('frame', frame=self.frame())
                with self.f.store.transaction() as db:
                    before = dict(db.execute('SELECT * FROM tasks WHERE id=?', (self.f.task['id'],)).fetchone())
                    cap = dict(db.execute('SELECT * FROM capabilities WHERE id=?', (self.control.gui,)).fetchone())
                    db.execute(statement, params)
                with self.assertRaises((PermissionError, Conflict)):
                    self.service.screen_share.read(self.f.actor, self.binding)
                self.assertNotIn('cloud', self.service.screen_share._slots)
                # Fixture-only restoration permits independent failure cases.
                with self.f.store.transaction() as db:
                    db.execute('UPDATE tasks SET epoch=? WHERE id=?', (before['epoch'], before['id']))
                    db.execute('UPDATE capabilities SET revoked=? WHERE id=?', (cap['revoked'], cap['id']))
                    db.execute("UPDATE managed_dispatch SET state='running' WHERE operation_id=?", ('gui-operation',))
        self.call('open'); self.call('frame', frame=self.frame())
        self.control.call('mesh_node_update', control_id='stream-service-offline', capabilities=[], online=False)
        with self.assertRaises(Conflict): self.call('status')
        self.assertNotIn('cloud', self.service.screen_share._slots)
        self.control.call('mesh_node_update', control_id='stream-service-online', capabilities=[self.control.gui], online=True)
        self.call('open'); self.call('frame', frame=self.frame())
        self.control.node['installed_control_adapter']['enabled'] = False
        owned(self.control.phone.config_path, self.control.phone.config)
        with self.assertRaises(PermissionError): self.service.screen_share.read(self.f.actor, self.binding)
        self.assertNotIn('cloud', self.service.screen_share._slots)

    def test_frame_bounds_hash_replay_ttl_and_no_wire_secrets(self):
        self.call('open'); first = self.frame(); self.call('frame', frame=first)
        for invalid in (dict(first, width=641), dict(first, bytes=42001), dict(first, capture_timestamp=float('nan')),
                        dict(first, sha256='0' * 64), dict(first, base64=base64.b64encode(b'not JPEG').decode()),
                        dict(first, approvalId='local-owner-secret')):
            with self.assertRaises(ValueError): self.call('frame', frame=invalid)
        with self.assertRaises(Conflict): self.call('frame', frame=dict(first, capture_timestamp=999))
        with self.assertRaises(ValueError): self.call('status', nonce='private-local-nonce')
        self.f.now += 20
        self.call('status')  # legitimate provider heartbeat, no fabricated frame
        self.assertEqual(1, self.service.screen_share.read(self.f.actor, self.binding)['frame']['revision'])
        self.f.now += 16
        with self.assertRaises(ValueError): self.call('status')
        self.assertNotIn('cloud', self.service.screen_share._slots)

    def test_idle_ttl_clears_bytes_and_does_not_reopen_the_expired_original_stream(self):
        self.call('open'); self.call('frame', frame=self.frame())
        self.f.now += 31
        with self.assertRaises(Conflict): self.call('status')
        self.assertEqual(b'', self.service.screen_share._slots['cloud'].body)
        with self.assertRaises(Conflict): self.call('open')
        with self.assertRaises(Conflict): self.service.screen_share.read(self.f.actor, self.binding)

    def test_rolling_parent_task_lease_is_rechecked_without_shortening_or_extending_original_allocation(self):
        # Original allocation is valid to t=1040; a temporarily shorter current
        # parent lease may be legitimately renewed by its real worker before loss.
        with self.f.store.transaction() as db:
            db.execute('UPDATE tasks SET deadline=? WHERE id=?', (self.f.now + 5, self.f.task['id']))
        self.call('open'); self.call('frame', frame=self.frame())
        self.f.now += 4
        self.f.store.update_task(self.f.task['id'], 'laptop', self.f.task['epoch'], checkpoint={'synthetic_alive': True})
        self.f.now += 2  # beyond the old task deadline, same actual task epoch
        self.assertTrue(self.call('status')['active'])
        with self.assertRaises(Conflict):
            self.call('open', binding=dict(self.binding, expires_at=1045))  # never extend the original allocation
        with self.f.store.transaction() as db:
            self.assertEqual(1040, self.f.bridge._allocation(db, 'gui-operation')['deadline'])
            db.execute('UPDATE tasks SET deadline=? WHERE id=?', (self.f.now, self.f.task['id']))
        with self.assertRaises(Conflict): self.call('status')
        self.assertNotIn('cloud', self.service.screen_share._slots)

    def test_exact_producer_close_is_delete_only_after_task_end_revocation_or_expiry(self):
        self.call('open'); self.call('frame', frame=self.frame())
        with self.assertRaises(PermissionError):
            self.call('close', binding=dict(self.binding, task_epoch=self.binding['task_epoch'] + 1))
        with self.assertRaises(PermissionError):
            self.service.node('FOREIGN_SYNTHETIC_' + 'x' * 40,
                {'action': 'mesh_screen_share_close', 'arguments': self.binding})
        self.assertTrue(self.service.screen_share._slots['cloud'].body)
        with self.f.store.transaction() as db:
            original_task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (self.f.task['id'],)).fetchone())
            db.execute("UPDATE tasks SET status='completed' WHERE id=?", (self.f.task['id'],))
            db.execute("UPDATE managed_allocations SET state='completed' WHERE operation_id='gui-operation'")
            db.execute("UPDATE managed_dispatch SET state='completed' WHERE operation_id='gui-operation'")
            db.execute('UPDATE capabilities SET revoked=1 WHERE id=?', (self.control.gui,))
        closed = self.call('close')
        self.assertFalse(closed['active']); self.assertFalse(closed['acknowledged']); self.assertEqual(0, closed['bytes'])
        self.assertNotIn('base64', closed)
        self.assertEqual(b'', self.service.screen_share._slots['cloud'].body)
        self.f.now += 50  # cleanup after fixed expiry is still bounded/idempotent
        self.assertEqual(closed, self.call('close'))
        with self.f.store.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (self.f.task['id'],)).fetchone()
            self.assertEqual('completed', task['status'])
            self.assertEqual(original_task['epoch'], task['epoch']); self.assertEqual(original_task['deadline'], task['deadline'])
            self.assertEqual('completed', self.f.bridge._allocation(db, 'gui-operation')['state'])

    def test_invalid_old_stream_does_not_block_new_task_and_late_old_close_preserves_replacement(self):
        self.call('open'); self.call('frame', frame=self.frame())
        self.f.bridge.unknown('node:cloud', 'gui-operation', self.control.gui, 3)
        # The real Store permits only one running task per worker. Its caller
        # explicitly yields the old task; this does not settle its unknown op.
        self.f.store.update_task(self.f.task['id'], 'laptop', self.f.task['epoch'], status='waiting_backend')
        self.f.store.create_task('synthetic independent original request', task_id='next-original-task')
        self.f.task = self.f.store.claim('laptop')
        self.assertEqual('next-original-task', self.f.task['id'])
        self.control.reserve(workload={'input': control_fixtures.blob(
            b'{"format":"amita-phone-screen-share/1","command":"screen_share","duration_ms":60000,"max_fps":2}')},
            operation='next-operation')
        accepted = self.control.call('mesh_work_report', report_id='next-operation-accept', provider={
            'action': 'accept', 'arguments': {'operation_id': 'next-operation', 'capability_id': self.control.gui,
                'epoch': 1, 'receipt_id': 'next-original-receipt'}})['provider']
        self.control.call('mesh_work_report', report_id='next-operation-start', provider={
            'action': 'start', 'arguments': {'operation_id': 'next-operation', 'capability_id': self.control.gui,
                'epoch': accepted['epoch'], 'receipt_id': 'next-original-receipt'}})
        replacement = dict(self.binding, stream_id='replacement-stream', binding_digest='c' * 64,
                           task_id=self.f.task['id'], task_epoch=self.f.task['epoch'], operation_id='next-operation')
        self.assertTrue(self.call('open', binding=replacement)['active'])  # no retry to evict stale slot
        self.call('frame', binding=replacement, frame=self.frame())
        closed_old = self.call('close')
        self.assertFalse(closed_old['active']); self.assertEqual(0, closed_old['bytes'])
        self.assertEqual(replacement, self.service.screen_share._slots['cloud'].binding)
        self.assertTrue(self.service.screen_share.read(self.f.actor, replacement)['frame'])
        with self.f.store.transaction() as db:
            self.assertEqual('unknown', self.f.bridge._allocation(db, 'gui-operation')['state'])
            self.assertEqual('running', self.f.bridge._allocation(db, 'next-operation')['state'])

    def test_feature_is_default_disabled_and_legacy_one_shot_workloads_never_authorize_streams(self):
        declared = self.control.call('mesh_node_update', control_id='declared-screen-share-view',
                                     capabilities=[self.control.gui], online=True)['session']
        self.assertEqual('screen-share', declared['lifecycle'])
        self.assertEqual({'enabled': True, 'screen_share_enabled': True,
                          'capability_id': self.control.gui, 'epoch': 1,
                          'execution_verification': 'declared_only'}, declared['installed_adapter'])
        self.assertFalse(declared['execution_verified'])
        adapter = copy.deepcopy(self.control.node['installed_control_adapter'])
        adapter.pop('screen_share_enabled')
        absent = spec(adapter)
        adapter['screen_share_enabled'] = False
        self.assertEqual(absent, spec(adapter))
        adapter['screen_share_enabled'] = 'true'
        invalid = copy.deepcopy(self.control.node); invalid['installed_control_adapter'] = adapter
        with self.assertRaises(ValueError): validate_adapter(invalid)
        for command in ('inspect_ui', 'screenshot'):
            with self.assertRaisesRegex(PermissionError, 'dedicated_stream_workload'):
                stream_policy({'format': CONTRACT, 'command': command})
        for policy in ({'format': 'amita-phone-screen-share/1', 'command': 'screen_share',
                        'duration_ms': 300001, 'max_fps': 2},
                       {'format': 'amita-phone-screen-share/1', 'command': 'screen_share',
                        'duration_ms': 30000, 'max_fps': 3}):
            with self.assertRaises(PermissionError): stream_policy(policy)
        self.call('open'); first = self.frame(); self.call('frame', frame=first)
        with self.assertRaisesRegex(Conflict, 'rate_exceeded'):
            self.call('frame', frame=self.frame(2))
        self.control.node['installed_control_adapter'].pop('screen_share_enabled')
        self.service.installed = copy.deepcopy(self.control.phone.config)
        owned(self.control.phone.config_path, self.control.phone.config)
        with self.assertRaisesRegex(PermissionError, 'installed_read_only_adapter'):
            self.call('status')
        self.assertNotIn('cloud', self.service.screen_share._slots)


if __name__ == '__main__':
    unittest.main()
