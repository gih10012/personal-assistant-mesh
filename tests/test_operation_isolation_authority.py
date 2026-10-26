"""Authenticated parent/owner API over the exact original frozen authority."""
from pathlib import Path
import tempfile
import types
import unittest

from assistant_mesh import operation_isolation as isolation
from assistant_mesh.operation_isolation_authority import ROUTE, attach
from assistant_mesh.store import Store
from tests import test_conversation_final as legacy


class IsolationAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-isolation-authority.')
        self.addCleanup(self.temp.cleanup)
        frozen, unused_conflict, unused_source = legacy.frozen_store()
        selected = isolation.make_store_class(frozen, Store)
        self.store = selected(Path(self.temp.name) / 'original.sqlite', clock=lambda: 1000, recover_inflight=False)
        self.store.heartbeat('node', ['leader', 'agent'])
        self.store.elect()
        self.parent = self.store.create_task('Bounded independent parent', task_id='parent',
            context={'session_scope':'project:parent'})
        self.parent_task = self.store.claim('node', task_ids=[self.parent])
        self.child = self.store.create_task('Actual child', parent_id=self.parent, task_id='child',
            context={'operation_id':'original-child-operation'})
        self.api = types.SimpleNamespace(store=self.store, dispatch=lambda *args: {'previous':'retained'})
        attach(self.api)

    def request(self, action, peer=None, **changes):
        value = {'action':action,'task_id':self.child}
        value.update(changes)
        return self.api.dispatch('POST', ROUTE, value, peer or {'role':'operator'})

    def test_worker_uses_actual_live_parent_and_cannot_self_approve(self):
        proof = self.request('inspect', {'role':'worker','node':'node'},
            parent_task_id=self.parent, epoch=self.parent_task['epoch'])
        arguments = {'declaration_id':'owner-child-declaration','operation_id':'original-child-operation',
            'resources':['file:separate-child-output'],'independent_of':proof['blockers'],
            'reason':'Actual parent delegates a separate child resource'}
        for peer in ({'role':'worker','node':'other'},{'role':'viewer'},{'role':'ingress'}):
            with self.subTest(peer=peer), self.assertRaises(PermissionError):
                self.request('declare', peer, parent_task_id=self.parent,
                             epoch=self.parent_task['epoch'], **arguments)
        with self.assertRaises(ValueError):
            self.request('declare', authorized=True, **arguments)
        result = self.request('declare', {'role':'worker','node':'node'}, parent_task_id=self.parent,
                              epoch=self.parent_task['epoch'], **arguments)
        self.assertFalse(result['task_created'])
        self.assertFalse(result['unknown_settled'])
        claimed = self.store.claim('node', task_ids=[self.child])
        self.assertEqual('child', claimed['id'])
        self.assertEqual({}, claimed['sessions'])

    def test_operator_route_is_exact_and_original_dispatch_is_retained(self):
        for method, path in [('GET', ROUTE),('POST',ROUTE+'?x=1'),('POST',ROUTE+'#x')]:
            with self.subTest(method=method,path=path), self.assertRaises(PermissionError):
                self.api.dispatch(method,path,{'action':'inspect','task_id':self.child},{'role':'operator'})
        self.assertEqual({'previous':'retained'},self.api.dispatch('POST','/old-ready-route',{},{}))
        proof = self.request('inspect')
        first = self.request('declare', declaration_id='operator-declaration', operation_id='original-child-operation',
            resources=['file:child-output'],independent_of=proof['blockers'],reason='Owner-approved independent child')
        self.assertTrue(first['declaration']['native_scope'].startswith('execution:'))
        with self.store.transaction() as db:
            self.assertEqual(2,db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])

    def test_task_without_explicit_operation_keeps_existing_task_id_fallback(self):
        bare = self.store.create_task('Unstarted owner request',task_id='bare-owner-request')
        proof = self.request('inspect',task_id=bare)
        args = {'task_id':bare,'declaration_id':'bare-owner-declaration','resources':['file:bare-output'],
                'independent_of':proof['blockers'],'reason':'Owner declares a new independent request'}
        with self.assertRaisesRegex(ValueError,'original_operation_changed'):
            self.request('declare',operation_id='replacement-operation',**args)
        accepted = self.request('declare',operation_id=bare,**args)
        self.assertEqual(bare,accepted['declaration']['operation_id'])
