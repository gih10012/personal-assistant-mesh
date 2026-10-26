"""Private metadata fence over the exact frozen Store, not current Store SQL.

No receiver, worker, native model, real account or standing service is started.
"""
import ast
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from assistant_mesh import worker_maintenance as maintenance
from tests.test_legacy_fenced_receiver import _source_base


class WorkerMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = tempfile.TemporaryDirectory(prefix='worker-maintenance-frozen-')
        cls.addClassCleanup(cls.source.cleanup)
        path = Path(cls.source.name) / 'store.py'
        path.write_bytes(_source_base()['store.py'])
        spec = importlib.util.spec_from_file_location('_maintenance_exact_frozen_store', str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.Store = module.Store

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='worker-maintenance-case-')
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / 'authority.sqlite'
        self.now = 1000
        self.store = self.Store(self.database, clock=lambda: self.now, recover_inflight=False)
        self.a = {'role': 'worker', 'node': 'existing-a', 'capabilities': ['a']}
        self.b = {'role': 'worker', 'node': 'existing-b', 'capabilities': ['b']}
        self.operator = {'role': 'operator'}
        self.calls, self.committed = [], []
        self.before_claim = None
        self.store.heartbeat(self.a['node'], ['a'])
        self.store.heartbeat(self.b['node'], ['b'])
        self.api = self.make_api(self.store)

    def make_api(self, store):
        def dispatch(method, path, payload, peer):
            self.calls.append((method, path, peer.get('node')))
            node = peer.get('node')
            if method == 'POST' and path == '/v1/claim' and peer.get('role') == 'worker':
                if self.before_claim is not None:
                    self.before_claim(node)
                result = store.claim(node)
                self.committed.append((node, result['id'] if result else None))
                return {'task': result}
            if method == 'POST' and path == '/v1/heartbeat':
                return store.heartbeat(node, peer['capabilities'])
            if method == 'POST' and path == '/v1/task/update':
                return store.update_task(payload['id'], node, payload['epoch'],
                    payload.get('checkpoint'), payload.get('result'), payload.get('status'))
            if method == 'GET' and path == '/v1/status':
                return store.status()
            return {'existing_dispatch': path}
        return SimpleNamespace(store=store, peers=[('PRIVATE_A', self.a), ('PRIVATE_B', self.b),
            ('PRIVATE_OPERATOR', self.operator)], dispatch=dispatch)

    def post(self, action='status', node='existing-a', operation=None, control=None, **options):
        payload = dict(action=action, node=node, **options)
        if operation is not None:
            payload['operation_id'] = operation
        if control is not None:
            payload['control_id'] = control
        return self.api.dispatch('POST', maintenance.ROUTE, payload, self.operator)

    def claim(self, peer=None):
        return self.api.dispatch('POST', '/v1/claim', {}, peer or self.a)['task']

    def task(self, label, required='a'):
        return self.store.create_task(label, [required], context={'session_scope': 'independent:' + label})

    def test_concurrent_claim_acquire_waits_for_commit_and_only_blocks_target_new_claims(self):
        current = self.task('already-running')
        current_task = self.store.claim(self.a['node'])
        self.assertEqual(current, current_task['id'])
        self.store.update_task(current, self.a['node'], current_task['epoch'],
            checkpoint={'side_effect_started': True, 'operation_id': 'original-operation'})
        selected, other = self.task('prior-admitted'), self.task('other-node', 'b')
        maintenance.attach(self.api)
        entered, unblock, acquiring, acquired = [threading.Event() for _ in range(4)]
        results, errors = {}, []

        def before(node):
            if node == self.a['node']:
                entered.set()
                if not unblock.wait(2):
                    raise AssertionError('isolated claim was not released')
        self.before_claim = before

        def claim_prior():
            try:
                results['task'] = self.claim()
            except BaseException as error:
                errors.append(error)

        def acquire():
            acquiring.set()
            try:
                results['lease'] = self.post('acquire', operation='original-maintenance', control='acquire-once')
                results['committed_before_acquire'] = (self.a['node'], selected) in self.committed
            except BaseException as error:
                errors.append(error)
            finally:
                acquired.set()
        first = threading.Thread(target=claim_prior)
        second = threading.Thread(target=acquire)
        first.start()
        try:
            self.assertTrue(entered.wait(1))
            second.start()
            self.assertTrue(acquiring.wait(1))
            self.assertFalse(acquired.wait(.1))
            # These use the original path while A's claim/acquire is blocked.
            self.assertEqual(other, self.claim(self.b)['id'])
            self.api.dispatch('POST', '/v1/heartbeat', {}, self.a)
            self.api.dispatch('GET', '/v1/status', {}, self.operator)
            self.assertTrue(self.api.dispatch('POST', '/v1/task/update',
                {'id': current, 'epoch': current_task['epoch']}, self.a)['ok'])
        finally:
            unblock.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(selected, results['task']['id'])
        self.assertTrue(results['committed_before_acquire'])
        self.assertTrue(results['lease']['claim_blocked'])
        after = self.task('after-fence')
        self.assertIsNone(self.claim())
        with self.store.transaction() as db:
            self.assertEqual(('pending', 0), tuple(db.execute('SELECT status,epoch FROM tasks WHERE id=?', (after,)).fetchone()))
            checkpoint = json.loads(db.execute('SELECT checkpoint FROM tasks WHERE id=?', (current,)).fetchone()[0])
            self.assertEqual('original-operation', checkpoint['operation_id'])
            self.assertTrue(checkpoint['side_effect_started'])
        self.before_claim = None
        self.post('release', operation='original-maintenance', control='release-once')
        self.assertEqual(after, self.claim()['id'])

    def test_attach_no_database_auth_finite_workers_and_changed_control_content(self):
        original_dispatch = self.api.dispatch
        with mock.patch.object(self.store, 'transaction', side_effect=AssertionError('attach touched DB')):
            installed = maintenance.attach(self.api)
            wrapped = self.api.dispatch
            self.assertIs(installed, maintenance.attach(self.api))
            self.assertIs(wrapped, self.api.dispatch)
        self.assertIsNot(original_dispatch, wrapped)
        ast.parse(Path(maintenance.__file__).read_text(), feature_version=(3, 6))
        payload = {'action': 'status', 'node': self.a['node']}
        for peer in (self.a, {'role': 'viewer'}, {'role': 'ingress'}, {'role': 'channel_carrier'}, {}):
            with self.subTest(peer=peer), self.assertRaises(PermissionError):
                self.api.dispatch('POST', maintenance.ROUTE, payload, peer)
        for method, path in (('GET', maintenance.ROUTE), ('POST', maintenance.ROUTE + '?'),
                             ('POST', maintenance.ROUTE + '?x=1'), ('POST', maintenance.ROUTE + '#fragment'),
                             ('POST', 'http://127.0.0.1' + maintenance.ROUTE)):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                self.api.dispatch(method, path, payload, self.operator)
        for node in ('not-configured', [], None):
            with self.subTest(node=node), self.assertRaises(ValueError):
                self.post(node=node)
        for ttl in (14, 601, True, 15.0, None):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.post('acquire', operation='bad-ttl', control='bad-ttl', ttl=ttl)
        acquired = self.post('acquire', operation='maint-a', control='control-a')
        self.assertEqual(1120, acquired['lease']['expires_at'])
        for changes in ({'action': 'release'}, {'node': self.b['node']},
                        {'operation_id': 'different-operation'}, {'ttl': 121}):
            request = {'action': 'acquire', 'node': self.a['node'], 'operation_id': 'maint-a', 'control_id': 'control-a'}
            request.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.api.dispatch('POST', maintenance.ROUTE, request, self.operator)
        with self.assertRaises(ValueError):
            self.post('acquire', operation='maint-a', control='different-content', ttl=121)
        with self.assertRaises(ValueError):
            self.post('acquire', node=self.b['node'], operation='maint-a', control='other-node')

    def test_expiry_explicit_renew_release_and_old_control_replays_never_reacquire(self):
        maintenance.attach(self.api)
        first = self.post('acquire', operation='maint-a', control='acquire-a', ttl=15)
        self.now = 1005
        replay = self.post('acquire', operation='maint-a', control='acquire-a', ttl=15)
        self.assertTrue(replay['replayed'])
        self.assertEqual(first['receipt'], replay['receipt'])
        self.assertEqual(1015, replay['lease']['expires_at'])
        renewed = self.post('renew', operation='maint-a', control='renew-a', ttl=15)
        self.assertEqual(1020, renewed['lease']['expires_at'])
        self.now = 1020
        expired = self.post(operation='maint-a', control='renew-a')
        self.assertEqual('expired', expired['lease']['state'])
        self.assertFalse(expired['claim_blocked'])
        self.store.heartbeat(self.a['node'], ['a'])
        task = self.task('claim-after-expiry')
        self.assertEqual(task, self.claim()['id'])
        with self.assertRaises(ValueError):
            self.post('renew', operation='maint-a', control='too-late', ttl=15)
        self.post('acquire', operation='maint-b', control='acquire-b', ttl=15)
        with self.assertRaises(ValueError):
            self.post('release', operation='maint-a', control='cannot-release-b')
        old = self.post('acquire', operation='maint-a', control='acquire-a', ttl=15)
        self.assertEqual('maint-b', old['lease']['operation_id'])
        self.assertEqual('expired', old['operation']['state'])
        self.assertTrue(old['claim_blocked'])
        self.post('release', operation='maint-b', control='release-b')
        for action, control in (('acquire', 'acquire-b'), ('renew', 'new-renew-b')):
            if action == 'renew':
                with self.assertRaises(ValueError):
                    self.post(action, operation='maint-b', control=control, ttl=15)
            else:
                released = self.post(action, operation='maint-b', control=control, ttl=15)
                self.assertEqual('released', released['lease']['state'])
                self.assertFalse(released['claim_blocked'])
        fresh_control = self.post('acquire', operation='maint-b', control='new-acquire-same-operation', ttl=15)
        self.assertFalse(fresh_control['receipt']['applied'])
        self.assertFalse(fresh_control['claim_blocked'])
        self.post('acquire', operation='maint-c', control='acquire-c')
        released_replay = self.post('release', operation='maint-b', control='release-b')
        self.assertEqual('maint-c', released_replay['lease']['operation_id'])
        self.assertTrue(released_replay['claim_blocked'])

    def test_persisted_lease_reinstated_on_ready_without_constructor_migration_or_task_changes(self):
        task = self.task('preserved-pending')
        maintenance.attach(self.api)
        acquired = self.post('acquire', operation='original-maintenance', control='original-acquire')
        with self.store.transaction() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM tasks')]
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        reopened = self.Store(self.database, clock=lambda: self.now, recover_inflight=False)
        self.api = self.make_api(reopened)
        with mock.patch.object(reopened, 'transaction', side_effect=AssertionError('ready opened DB')):
            maintenance.attach(self.api)
        self.assertIs(reopened, self.api.store)
        self.assertIsNone(self.claim())
        replay = self.post('acquire', operation='original-maintenance', control='original-acquire')
        self.assertEqual(acquired['receipt'], replay['receipt'])
        self.assertTrue(replay['claim_blocked'])
        with reopened.transaction() as db:
            self.assertEqual(before, [tuple(row) for row in db.execute('SELECT * FROM tasks')])
            self.assertEqual(tables, [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])
        released = self.post('release', operation='original-maintenance', control='original-release')
        self.assertEqual('released', released['lease']['state'])
        self.assertEqual(task, self.claim()['id'])


if __name__ == '__main__':
    unittest.main()
