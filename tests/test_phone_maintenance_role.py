"""Synthetic claimed tasks and original route planner; never a model probe."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
import sys
from types import SimpleNamespace

from assistant_mesh.leader_governance import Governance
from assistant_mesh.phone_maintenance_role import (RoleHold, guard_task, guard_resume, resume_identity,
                                                  scoped_governance, install)


NODE = 'SYNTHETIC_PHONE'
SCOPE = 'phone-maintenance:' + hashlib.sha256(NODE.encode()).hexdigest()[:40]
RATING = {'difficulty': 'D1', 'risk': 'low', 'priority': 2, 'rationale': 'Synthetic bounded device diagnostic.'}
POLICY = {'revision': 'synthetic-owner-policy', 'defaults': {'D1': 'ordinary', 'D2': 'ordinary', 'D3': 'advanced'},
    'routes': [{'id': 'ordinary', 'model': 'gpt-6-luna', 'effort': 'max', 'harness': 'codex',
                'cost_basis': 'included_subscription', 'quality': 'standard', 'difficulties': ['D1', 'D2']},
               {'id': 'advanced', 'model': 'gpt-6.1-sol', 'effort': 'high', 'harness': 'codex',
                'cost_basis': 'included_subscription', 'quality': 'strong', 'difficulties': ['D1', 'D2', 'D3']}]}


def task():
    return {'id': 'original-incident-task', 'epoch': 1, 'status': 'running', 'scope': SCOPE,
        'required': ['agent', 'phone-maintenance'], 'checkpoint': {}, 'context': {
            'session_scope': SCOPE, 'memory_scope': SCOPE, 'origin': {'kind': 'node-maintenance', 'peer': NODE},
            'project_id': 'phone-maintenance', 'governance': copy.deepcopy(RATING), 'phone_maintenance': {
                'node_id': NODE, 'operator_granted': False, 'snapshot': {'diagnostic': 'node_connection'},
                'allowed_repairs': ['diagnose', 'reconcile_original_receipts']}}}


def closed(model='gpt-6-luna', effort='max'):
    return {'thread_id': 'original-native-thread', 'side_effect_started': False, 'codex_provider_identity': 'openai',
        'codex_node': 'laptop', 'harness': 'codex',
        'native_lifecycle': {'thread_id': 'original-native-thread', 'settled': True, 'runtime_closed': True,
            'model_route_selection': {'model': model, 'reasoning_effort': effort, 'provider': 'openai',
                                      'effective_inference_verified': False}}}


class PhoneMaintenanceRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name); self.root.chmod(0o700)
        self.config = {'node_id': 'laptop', 'codex': {'auth_home': '/SYNTHETIC/account', 'workspace': str(self.root)}}
        self.gov = scoped_governance(Governance, SCOPE)(self.root / 'governance.sqlite', POLICY, clock=lambda: 1000)
        self.profile = {'model': 'gpt-6-luna', 'reasoning_effort': 'max'}

    def tearDown(self):
        self.temp.cleanup()

    def test_device_d1_only_and_unknown_never_opens_a_route(self):
        current = task(); guard_task(current, SCOPE, RATING)
        for field, value in (('difficulty', 'D2'), ('risk', 'high')):
            rating = dict(RATING, **{field: value})
            with self.assertRaises(RoleHold): guard_task(current, SCOPE, rating)
        current['context']['phone_maintenance']['node_id'] = 'another-device'
        self.assertEqual('hold_original_operation', self.gov.recovery_plan(current, self.config)['status'])
        current = task(); current['checkpoint']['side_effect_started'] = True
        self.assertEqual('hold_original_operation', self.gov.recovery_plan(current, self.config)['status'])

    def test_luna_is_preference_and_actual_qualified_sol_survives_cached_outage(self):
        current = task()
        plan = self.gov.recovery_plan(current, self.config, self.profile, _bindings=True)
        self.assertEqual(['gpt-6-luna', 'gpt-6.1-sol'], [b['identity']['model'] for b in plan['_bindings']])
        self.gov.route_failure(plan['_bindings'][0]['identity'], ValueError('usage_limit_exceeded'), phase='account_rpc')
        recovered = self.gov.recovery_plan(current, self.config, self.profile, _bindings=True)
        self.assertEqual(['gpt-6.1-sol'], [b['identity']['model'] for b in recovered['_bindings']])
        self.assertEqual('ready', recovered['status'])

    def test_safe_existing_thread_keeps_exact_route_and_resume_never_sets_model(self):
        current = task(); current['checkpoint'] = closed('gpt-6.1-sol', 'high')
        plan = self.gov.recovery_plan(current, self.config, self.profile, _bindings=True)
        self.assertEqual(['gpt-6.1-sol'], [b['identity']['model'] for b in plan['_bindings']])
        identity = resume_identity(current)
        params = {'threadId': identity['thread_id'], 'model': 'gpt-6.1-sol', 'modelProvider': 'openai',
                  'config': {'model_reasoning_effort': 'high'}, 'excludeTurns': True}
        guarded = guard_resume(params, identity)
        self.assertNotIn('model', guarded); self.assertNotIn('modelProvider', guarded); self.assertNotIn('config', guarded)
        self.assertEqual('original-native-thread', guarded['threadId'])
        with self.assertRaises(RoleHold): guard_resume(dict(params, model='gpt-6-luna'), identity)
        current['checkpoint'] = closed('unqualified-existing-model', 'max')
        held = self.gov.recovery_plan(current, self.config, self.profile, _bindings=True)
        self.assertEqual('hold_original_operation', held['status']); self.assertEqual([], held['_bindings'])
        current['checkpoint']['native_lifecycle']['settled'] = False
        with self.assertRaises(RoleHold): resume_identity(current)

    def test_composition_old_scope_unchanged_and_no_second_claim(self):
        counters = {'old_backend': 0, 'phone_backend': 0}
        class Worker:
            def __init__(self, config, client=None, config_path=None, **unused):
                self.config, self.client, self.config_path = config, client, config_path
                self.current, self.harness = None, 'codex'
            def open_backend(self): counters['old_backend'] += 1; return 'legacy'
            def on_tool(self, *unused): pass
            def on_activity(self, *unused): pass
            def on_interaction(self, *unused): pass
            def tick(self, *unused): pass
        def governed(base, codex, governance, **options):
            class Phone(base):
                def open_backend(self):
                    self.assert_no_claim = True
                    counters['phone_backend'] += 1
                    self.plan = governance.recovery_plan(self.current, self.config, self.profile, _bindings=True)
                    return 'phone-backend'
                profile = self.profile
            return Phone
        settings = {'format': 'amita-phone-maintenance-role/1', 'scope': SCOPE, 'workspace': str(self.root),
                    'profile_path': '/SYNTHETIC/profile', 'governance_database': str(self.root / 'role.sqlite'),
                    'governance_policy': '/SYNTHETIC/policy'}
        incumbent = SimpleNamespace(Worker=Worker, make_worker=lambda *a, **k: Worker)
        install(incumbent, object, settings, lambda path: POLICY if path.endswith('policy') else self.profile,
                Governance, governed)
        composed = incumbent.make_worker()
        worker = composed(self.config, client=SimpleNamespace(request=lambda *unused: self.fail('no RPC needed in fixture')))
        worker.current = dict(task(), scope='old-workspace-scope')
        self.assertEqual('legacy', worker.open_backend())
        worker.current = task()
        self.assertEqual('phone-backend', worker.open_backend())
        self.assertEqual({'old_backend': 1, 'phone_backend': 1}, counters)
        self.assertIs(worker.current, worker._phone_backend.current)
        self.assertIs(worker.client, worker._phone_backend.client)
        self.assertEqual(self.config, worker.config)

    @unittest.skipUnless(os.environ.get('AMITA_PHONE_ROLE_CANDIDATE_ROOT'), 'actual candidate not requested')
    def test_actual_candidate_import_opens_no_ledger_or_model_process(self):
        candidate = os.environ['AMITA_PHONE_ROLE_CANDIDATE_ROOT']
        script = '''
import importlib.util, sqlite3, subprocess, sys
def forbidden(*a, **k): raise AssertionError('candidate import must not open a ledger or launch a process')
sqlite3.connect = forbidden
subprocess.Popen = forbidden
path = sys.argv[1] + '/launch_worker.py'
spec = importlib.util.spec_from_file_location('controlled_phone_role_candidate', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module._parent._incumbent._phone_maintenance_role_installed is True
assert module._parent._incumbent._native_worker_release_installed is True
print('candidate import composed; no ledger/model/process')
'''
        output = subprocess.check_output([sys.executable, '-c', script, candidate], stderr=subprocess.STDOUT).decode()
        self.assertIn('no ledger/model/process', output)
