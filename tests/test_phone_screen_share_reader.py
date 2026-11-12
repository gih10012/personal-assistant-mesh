"""Private original-worker lookup, real incumbent Store/Allocation, no HTTP."""
import json
import unittest

from assistant_mesh.phone_screen_share_reader import ROUTE, install_dispatch
from assistant_mesh.server import API
from assistant_mesh.store import Conflict
from tests import test_phone_screen_share as fixtures
from tests import test_phone_authority as authority_fixtures


class PhoneScreenShareReaderTests(unittest.TestCase):
    def test_original_worker_lookup_rechecks_epoch_and_never_exposes_frames_in_graph_or_provider_routes(self):
        fixture = fixtures.PhoneScreenShareTests(); fixture.setUp()
        try:
            fixture.call('open'); frame = fixture.frame(); fixture.call('frame', frame=frame)
            service, store = fixture.service, fixture.f.store
            # Bind the actual API.dispatch class to the already-created fixture
            # authority. Do not run API.__init__ or create a second Store.
            api = API.__new__(API); api.__dict__.update(service.api.__dict__)
            api.network = None; api.phone_authority = service; service.api = api
            original_count = fixture.f.row_count('tasks')
            install_dispatch(api, service)
            lookup = {key: fixture.binding[key] for key in ('operation_id', 'capability_id', 'task_id', 'task_epoch')}
            worker = {'role': 'worker', 'node': 'laptop'}
            result = api.dispatch('POST', ROUTE, lookup, worker)
            self.assertEqual(frame['base64'], result['frame']['base64'])
            self.assertEqual(fixture.binding['stream_id'], result['stream_id'])
            self.assertEqual(fixture.binding['binding_digest'], result['binding_digest'])
            self.assertNotIn('nonce', json.dumps(result)); self.assertNotIn('approval', json.dumps(result))
            self.assertEqual(result, api.dispatch('POST', ROUTE, fixture.binding, worker))
            for peer in ({'role': 'operator', 'node': 'laptop'}, {'role': 'viewer', 'node': 'laptop'},
                         {'role': 'worker', 'node': 'cloud'}, {'role': 'worker', 'node': 'foreign'},
                         {'role': 'ingress', 'node': 'laptop'}, {'role': 'native_maintenance', 'node': 'laptop'}):
                with self.subTest(peer=peer), self.assertRaises(PermissionError):
                    api.dispatch('POST', ROUTE, lookup, peer)
            with self.assertRaises(PermissionError):
                api.dispatch('POST', ROUTE, dict(lookup, task_epoch=lookup['task_epoch'] + 1), worker)
            with self.assertRaises(ValueError):
                api.dispatch('POST', ROUTE, dict(lookup, actor='node:laptop'), worker)
            with self.assertRaises(PermissionError):
                api.dispatch('GET', ROUTE, {}, worker)
            with self.assertRaises(PermissionError):
                api.dispatch('POST', ROUTE + '?task_epoch=1', lookup, worker)
            with self.assertRaises(PermissionError):
                service.node(authority_fixtures.NODE_TOKEN, {'action': 'mesh_screen_share_read', 'arguments': fixture.binding})
            graph = api.dispatch('GET', '/v1/resource/graph', {}, {'role': 'viewer'})
            self.assertNotIn(frame['base64'], json.dumps(graph))
            self.assertNotIn(fixture.binding['stream_id'], json.dumps(graph))
            self.assertEqual(original_count, fixture.f.row_count('tasks'))
            self.assertEqual(0, fixture.f.row_count('phone_result_blobs'))
            with store.transaction() as db:
                db.execute('UPDATE tasks SET epoch=epoch+1 WHERE id=?', (lookup['task_id'],))
            with self.assertRaises(Conflict): api.dispatch('POST', ROUTE, lookup, worker)
            self.assertNotIn('cloud', service.screen_share._slots)
        finally:
            fixture.tearDown()
