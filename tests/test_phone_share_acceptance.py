import tempfile
from pathlib import Path
import time
import unittest
from urllib.error import HTTPError

from assistant_mesh import phone_share_acceptance as acceptance, worker_claim_receipts, worker_maintenance
from assistant_mesh.phone_control import CONTRACT
from assistant_mesh.phone_screen_share_reader import OBSERVATION_ROUTE, install_dispatch
from assistant_mesh.server import API
from assistant_mesh.store import Conflict
from tests import test_phone_authority as fixtures
from tests.test_enrollment_relay import owned
from tests.test_phone_control import blob


class RouteClient:
    """Exercise the incumbent route handlers without a device or network listener."""
    url, unix_socket = 'http://127.0.0.1:17680', None

    def __init__(self, api, peer):
        self.api, self.peer, self.drop = api, peer, None
        self.calls = []

    def request(self, path, body=None):
        self.calls.append((path, body))
        try:
            response = self.api.dispatch('GET' if body is None else 'POST', path, body, self.peer)
        except Conflict as error:
            raise HTTPError(self.url + path, 409, str(error), None, None) from error
        if self.drop == (path, (body or {}).get('action')):
            self.drop = None
            raise OSError('fixture lost the reply after commit')
        return response


class PhoneShareAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.phone = fixtures.PhoneAuthorityTests()
        self.phone.setUp()
        self.addCleanup(self.phone.tearDown)
        f, self.service = self.phone.original, self.phone.service
        api = API({'database': str(Path(f.temp.name) / 'unused.sqlite'), 'peers': []})
        api.store, api.resources, api.allocations = f.store, f.registry, f.bridge
        self.service.api = api
        self.operator = RouteClient(api, {'role': 'operator'})
        worker = {'role': 'worker', 'node': 'manual-operator', 'capabilities': ['mesh.node:manual-operator']}
        self.worker = RouteClient(api, worker)
        api.peers = [('fixture-worker', worker)]
        worker_claim_receipts.install_dispatch(api)
        worker_maintenance.attach(api)
        install_dispatch(api, self.service)

        self.cap = 'cloud:ui-control-v1'
        node = self.phone.config['nodes'][0]
        node['control_session_id'] = 'screen-share-service'
        node['installed_control_adapter'] = {'enabled': True, 'capability_id': self.cap,
            'epoch': 1, 'contract': CONTRACT, 'actions': ['inspect'], 'screen_share_enabled': True}
        owned(self.phone.config_path, self.phone.config)
        self.service.installed = self.service._config()
        self.service.control.install(node)
        f.registry.trusted_verifiers |= {'source:phone-control-presence'}
        f.bridge.define_pool('operator', self.cap + ':foreground',
                             {acceptance.DIMENSION: acceptance.CAPACITY})
        f.now = time.time()
        self.other = f.store.create_task('Unrelated pending fixture', ['mesh.node:manual-operator'])
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        self.directory = Path(private.name)
        self.run = self.new_run()

    def new_run(self):
        return acceptance.Acceptance(self.directory, self.operator, self.worker, 'manual-operator')

    def phone_call(self, action, **arguments):
        arguments['session_id'] = 'screen-share-service'
        return self.service.node(fixtures.NODE_TOKEN, {'action': action, 'arguments': arguments})

    def connect(self):
        self.phone_call('mesh_connect', control_id='fixture-live-phone', capabilities=[self.cap])

    def report(self, action, **args):
        return self.phone_call('mesh_work_report', report_id='fixture-' + action, provider={
            'action': action, 'arguments': {'operation_id': self.run.state['operation_id'],
                                           'capability_id': self.cap, 'receipt_id': 'fixture-local-approval', **args}
        })['provider']

    def test_offline_preflight_creates_no_intent_or_remote_write(self):
        with self.assertRaisesRegex(ValueError, 'waiting_for_authenticated_phone_control_presence'):
            self.run.dispatch('cloud', 0, 30000)
        self.assertFalse(self.run.path.exists())
        self.assertEqual([], self.operator.calls)
        self.assertEqual(['describe'], [body['action'] for _, body in self.worker.calls])

    def test_lost_reserve_reply_reconciles_then_observes_original_private_frame_and_finishes(self):
        self.connect()
        self.worker.drop = ('/v1/allocation/action', 'reserve')
        with self.assertRaises(OSError):
            self.run.dispatch('cloud', 0, 30000)
        self.run = self.new_run()
        state = self.run.public_state()
        self.assertEqual('reserve', state['pending_write']['step'])
        self.assertTrue(state['claim_id'].startswith('c2-'))
        self.assertEqual('pending', self.service.store.task_status(self.other)['status'])
        self.assertFalse(state['capture_verified'])
        with self.assertRaisesRegex(ValueError, 'reconcile_original_pending_write_first'):
            self.run.dispatch('cloud', 0, 30000)
        self.assertEqual('reserved', self.run.reconcile()['state'])
        with self.assertRaisesRegex(ValueError, 'original_operation_already_dispatched'):
            self.run.dispatch('cloud', 0, 30000)
        self.assertEqual(1, sum(body.get('action') == 'reserve' for _, body in self.worker.calls if body))

        dispatch = self.phone_call('mesh_work_pull', pull_id='original-pull')['dispatch'][0]
        self.assertEqual(state['operation_id'], dispatch['operation_id'])
        accepted = self.report('accept', epoch=dispatch['epoch'])
        started = self.report('start', epoch=accepted['epoch'])
        raw = b'\xff\xd8\xffsynthetic fixture pixels\xff\xd9'
        frame = dict(blob(raw), revision=1, capture_timestamp=self.service.store.clock(), width=8, height=8)
        binding = dict(self.run.bindings(), primary_session_id='phone-session', stream_id='fixture-stream',
                       binding_digest='a' * 64, expires_at=self.service.store.clock() + 25)
        self.phone_call('mesh_screen_share_open', **binding)
        self.phone_call('mesh_screen_share_frame', frame=frame, **binding)
        preview = self.run.observe(0)
        path = Path(preview['path'])
        self.assertEqual(raw, path.read_bytes())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.run.path.stat().st_mode & 0o777)
        self.assertNotIn('base64', preview)
        self.assertEqual(preview, self.new_run().observe(0))
        with self.assertRaisesRegex(ValueError, 'original_capture_settlement_unconfirmed'):
            self.run.finish()
        self.phone_call('mesh_screen_share_close', **binding)
        result = blob(b'{"source_closed":true,"capture_quiescent":true}')
        self.report('result', result=result)
        self.report('settle', epoch=started['epoch'], outcome='completed', evidence={
            'result_reference': 'phone-result:' + result['sha256'], 'resource_quiescent': True,
            'sha256': result['sha256'], 'bytes': result['bytes']})
        self.worker.drop = ('/v1/task/update', None)
        with self.assertRaises(OSError):
            self.run.finish()
        self.run = self.new_run()
        self.run.reconcile()
        self.assertIn('finish', self.run.done)

    def test_lost_observation_reconciles_exact_workload_before_first_reserve(self):
        self.connect()
        self.worker.drop = (OBSERVATION_ROUTE, None)
        with self.assertRaises(OSError):
            self.run.dispatch('cloud', 0, 30000)
        self.run = self.new_run()
        original = self.run.public_state()
        self.assertEqual('observation', original['pending_write']['step'])
        self.run.reconcile()
        self.assertEqual('reserved', self.run.dispatch(None, 0, 30000)['state'])
        self.assertEqual(original['operation_id'], self.run.public_state()['operation_id'])
        self.assertEqual(1, sum(path == OBSERVATION_ROUTE for path, _ in self.worker.calls))

    def test_lost_grant_and_claim_acknowledgments_resume_only_the_original(self):
        self.connect()
        self.worker.drop = (worker_claim_receipts.CLAIM, None)
        with self.assertRaises(OSError):
            self.run.dispatch('cloud', 0, 30000)
        self.run = self.new_run()
        self.run.reconcile()
        self.operator.drop = ('/v1/resource/action', 'grant')
        with self.assertRaises(OSError):
            self.run.dispatch(None, 0, 30000)
        self.run = self.new_run()
        self.run.reconcile()
        self.assertEqual('reserved', self.run.dispatch(None, 0, 30000)['state'])
        self.assertEqual(1, sum(path == worker_claim_receipts.CLAIM for path, _ in self.worker.calls))
        self.assertEqual(1, sum(body.get('action') == 'grant' for _, body in self.operator.calls if body))


if __name__ == '__main__':
    unittest.main()
