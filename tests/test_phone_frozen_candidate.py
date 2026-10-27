"""One loopback acceptance over Root's actual frozen source snapshot.

Requires the private SOURCE-only snapshot; no production DB/account or actual
phone is opened. The fixture composes the incumbent passive Store factories.
"""
import base64
import hashlib
import http.client
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = Path(os.environ.get('AMITA_PHONE_FROZEN_SNAPSHOT',
                              '/var/tmp/amita-phone-baseline-20261028/snapshot.json'))
BASE = Path(os.environ.get('AMITA_PHONE_FROZEN_BASE',
    '/var/tmp/mesh-native-goal-release-20261024.Vbz1L4AQ/final-candidate-20261022/assistant_mesh'))
SNAPSHOT_SHA = 'd27e5fc5f24e86f79fa69a42dbe27c8cf6fd86d8cc50d23e5305061925b499e5'
EXTRA_SHA = '486b2715a69a82572fc3cd61affab29567a90c8b3b711ccc6450304c13c7209b'
TOKEN = 'SYNTHETIC_FROZEN_PHONE_' + 'f' * 48


class PhoneFrozenCandidateTests(unittest.TestCase):
    @unittest.skipUnless(SNAPSHOT.is_file() and BASE.is_dir(), 'actual frozen SOURCE snapshot unavailable')
    def test_actual_frozen_api_assembly_and_original_dispatch_full_result(self):
        spec = importlib.util.spec_from_file_location('phone_frozen_builder',
            ROOT / 'deploy' / 'build-phone-frozen-candidate.py')
        builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory(prefix='amita-phone-frozen.') as temporary:
            root = Path(temporary); candidate = root / 'candidate'
            report = builder.build(SNAPSHOT, candidate, snapshot_sha256=SNAPSHOT_SHA,
                runtime_config='/PRIVATE/phone-runtime.json', origin='https://mesh.example.test',
                extra_snapshot=SNAPSHOT.with_name('extra-provider_runtime.json'), extra_snapshot_sha256=EXTRA_SHA)
            self.assertFalse(report['ledger_opened'])
            self.assertEqual(17684, report['port'])
            self.assertEqual(set(builder.PATCHED), set(report['patched_modules']))
            for forbidden in builder.FORBIDDEN:
                self.assertFalse((candidate / 'assistant_mesh' / forbidden).exists())
            namespace = (candidate / 'assistant_mesh' / '__init__.py').read_bytes()
            self.assertTrue(namespace.endswith((SNAPSHOT.parent / 'incumbent/assistant_mesh/__init__.py').read_bytes()))
            with self.assertRaisesRegex(ValueError, 'manifest_pin_mismatch'):
                builder.load_snapshot(SNAPSHOT, '0' * 64)
            # Execute the generated namespace with only actual frozen source
            # fallbacks relocated locally. No modern repo API/Store is present.
            package_name = 'amita_frozen_phone_fixture'
            package_spec = importlib.util.spec_from_file_location(package_name,
                candidate / 'assistant_mesh' / '__init__.py', submodule_search_locations=[
                    str(candidate / 'assistant_mesh'), str(SNAPSHOT.parent / 'resolved'),
                    str(SNAPSHOT.parent / 'incumbent/assistant_mesh'), str(BASE)])
            package = importlib.util.module_from_spec(package_spec)
            sys.modules[package_name] = package; package_spec.loader.exec_module(package)
            def module(name):
                return importlib.import_module(package_name + '.' + name)
            server, store = module('server'), module('store')
            self.assertEqual(SNAPSHOT.parent / 'resolved/server.py', Path(server.__file__))
            self.assertEqual(SNAPSHOT.parent / 'resolved/store.py', Path(store.__file__))
            self.assertEqual('be2df48e03021f48c382efc4690fa7a8ee5e67da8bd93497e725566532e30c0b',
                             hashlib.sha256(Path(store.__file__).read_bytes()).hexdigest())
            # Same factory composition as incumbent launch_receiver.py. Passive
            # construction is explicit and never rewrites a running authority.
            methods = module('amita_task_methods')
            isolated = module('operation_isolation').make_store_class(store.Store, methods.Store)
            entrance = module('entrance_runtime').entrance_store_class(isolated,
                {'owner_ref': 'synthetic-owner', 'conversation_id': 'synthetic-conversation',
                 'agent_id': 'synthetic-claw'})
            selected = module('task_transaction_adapter').make_store_class(entrance, methods.Store)
            class Passive(selected):
                def __init__(self, path, **options):
                    options['recover_inflight'] = False
                    super().__init__(path, **options)
            original = server.Store; server.Store = Passive
            try:
                api = server.API({'database': str(root / 'original.db'), 'node_id': 'frozen-authority',
                    'peers': [], 'notification_policy': {'mode': 'private'},
                    'resources': {'trusted_verifiers': ['probe:frozen-independent']},
                    'ingress': {'owner_id': 'synthetic-owner', 'required': ['agent'],
                                'session_scope': 'leader:project:amita-harmony-onboarding'}})
            finally:
                server.Store = original
            module('session_node_authority').attach(api, {'subjects': {
                'existing-chat': {'capabilities': ['agent'], 'visible_nodes': {}}}})
            module('worker_maintenance').attach(api)
            module('operation_isolation_authority').attach(api)
            owner_path = root / 'phone-owner.json'
            owner_path.write_text(json.dumps({'format': 'mesh-phone-authority/1', 'enabled': True, 'nodes': [{
                'node_id': 'synthetic-phone', 'subject': 'synthetic-phone-subject', 'session_id': 'frozen-phone',
                'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest(), 'enabled': True,
                'capabilities': ['synthetic-phone:bytes'], 'scope': ['phone:node'],
                'enrollment': {'synthetic_source_fixture': True}}]})); owner_path.chmod(0o600)
            # Port selected for this test only; generated production default is
            # 17684 and Root verifies ownership before installation.
            from http.server import HTTPServer
            import threading
            service = module('phone_authority').PhoneAuthority(api, owner_path)
            listener = HTTPServer(('127.0.0.1', 0), module('phone_http').handler(service,
                'https://mesh.example.test', trusted_proxy_peers=('127.0.0.1',)))
            thread = threading.Thread(target=lambda: listener.serve_forever(poll_interval=.01), daemon=True)
            thread.start()
            def call(action, **arguments):
                arguments['session_id'] = 'frozen-phone'
                raw = json.dumps({'action': action, 'arguments': arguments})
                connection = http.client.HTTPConnection('127.0.0.1', listener.server_port, timeout=5)
                connection.request('POST', '/phone/node', raw, headers={'Host': 'mesh.example.test',
                    'X-Forwarded-Proto': 'https', 'Authorization': 'Bearer ' + TOKEN,
                    'Content-Type': 'application/json'})
                response = connection.getresponse(); body = json.loads(response.read()); connection.close()
                self.assertEqual(200, response.status, body)
                return body
            def provider(action, **arguments):
                arguments.update(operation_id='original-frozen-op', capability_id='synthetic-phone:bytes')
                return call('mesh_work_report', report_id=action,
                    provider={'action': action, 'arguments': arguments})['provider']
            try:
                joined = call('mesh_connect', control_id='join', capabilities=['synthetic-phone:bytes'])
                self.assertEqual('synthetic-phone', joined['session']['node_id'])
                api.store.heartbeat('original-caller', ['agent'])
                api.store.create_task('synthetic original bytes request', required=['agent'], task_id='original-task')
                task = api.store.claim('original-caller')
                registry, allocations = api.resources, api.allocations
                scope, workload = {'fixture': 'bytes-roundtrip'}, {'input_bytes': 4, 'operation': 'repeat'}
                registry.advertise('node:synthetic-phone', 'synthetic-phone:bytes', 'compute.bytes')
                registry.observe('probe:frozen-independent', 'synthetic-phone:bytes', 'bytes.roundtrip', 1,
                    'success', 'synthetic independent source fixture', verification='verified', epoch=1,
                    observation_id='frozen-measurement', evidence={'scope': scope, 'workload': workload})
                registry.request_grant('node:original-caller', 'synthetic-phone:bytes', 'challenge.bytes.repeat',
                                       scope, request_id='original-grant-request')
                registry.grant('operator', 'original-grant-request')
                allocations.define_pool('operator', 'phone-slots', {'concurrent': {'capacity': 1, 'unit': 'slot'}})
                allocations.bind_pool('operator', 'synthetic-phone:bytes', 1, 'phone-slots', 1, 'concurrent', 1, 'slot')
                selection = {'capability_id': 'synthetic-phone:bytes', 'epoch': 1,
                    'action': 'challenge.bytes.repeat', 'scope': scope, 'workload': workload,
                    'observations': [{'observation_id': 'frozen-measurement', 'metric': 'bytes.roundtrip',
                                      'unit': 'success', 'max_age_seconds': 60}]}
                allocations.reserve('node:original-caller', 'original-frozen-op', task['id'], task['epoch'],
                    {'target': 'synthetic-phone:bytes', 'selections': [selection], 'route': []})
                pending = call('mesh_work_pull', pull_id='original-pull')['dispatch'][0]
                self.assertEqual('original-task', pending['task_id'])
                accepted = provider('accept', epoch=pending['epoch'], receipt_id='original-receipt')
                started = provider('start', epoch=accepted['epoch'], receipt_id='original-receipt')
                self.assertTrue(started['execute_once'])
                self.assertFalse(provider('start', epoch=accepted['epoch'], receipt_id='original-receipt')['execute_once'])
                raw = bytes(range(256)) * 200; digest = hashlib.sha256(raw).hexdigest()
                blob = provider('result', receipt_id='original-receipt', result={
                    'bytes': len(raw), 'sha256': digest, 'base64': base64.b64encode(raw).decode()})
                settled = provider('settle', epoch=started['epoch'], receipt_id='original-receipt', outcome='completed',
                    evidence=dict(blob, resource_quiescent=True))
                self.assertEqual('completed', settled['state'])
                self.assertEqual(raw, base64.b64decode(service.read_result('node:original-caller',
                    'original-frozen-op', 'synthetic-phone:bytes')['base64']))
                with api.store.transaction() as db:
                    self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            finally:
                listener.shutdown(); listener.server_close(); thread.join(2)
                for name in list(sys.modules):
                    if name == package_name or name.startswith(package_name + '.'):
                        del sys.modules[name]


if __name__ == '__main__':
    unittest.main()
