"""Private original-worker lookup, real incumbent Store/Allocation, no HTTP."""
import json
import base64
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from assistant_mesh.phone_frame_client import FrameStaleError, fetch_frame, read_frame, verify_frame
from assistant_mesh.phone_screen_share import MAX_FRAME_AGE_SECONDS
from assistant_mesh.phone_screen_share_reader import METRIC, ROUTE, VERIFIER, install_dispatch
from assistant_mesh.phone_share_request import scope_for, submit
from assistant_mesh.server import API
from assistant_mesh.store import Conflict
from tests import test_phone_screen_share as fixtures
from tests import test_phone_authority as authority_fixtures
from tests.test_phone_control import blob


class PhoneScreenShareReaderTests(unittest.TestCase):
    def test_authenticated_phone_presence_admits_a_finite_read_only_request(self):
        fixture = fixtures.PhoneScreenShareTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service, original = fixture.service, fixture.f
        api = API.__new__(API)
        api.__dict__.update(service.api.__dict__)
        api.network = None
        api.phone_authority = service
        service.api = api
        install_dispatch(api, service)
        task_id, epoch = original.task['id'], original.task['epoch']
        scope = scope_for(task_id, epoch)
        cap = fixture.control.gui
        original.registry.request_grant(original.actor, cap, 'inspect', scope, request_id='owner-scope')
        approval = original.registry.grant('operator', 'owner-scope')
        client = mock.Mock()
        client.request.side_effect = lambda route, body: api.dispatch(
            'POST', route, body, {'role': 'worker', 'node': 'laptop'})
        result = submit(client, task_id, epoch, 'real-phone-request', cap, 1, approval['id'])
        self.assertEqual('reserved', result['state'])
        self.assertFalse(result['execution_verified'])
        with original.store.transaction() as db:
            measurement = db.execute('SELECT actor,evidence FROM capability_metrics WHERE metric=?',
                                     (METRIC,)).fetchone()
            self.assertEqual(VERIFIER, measurement[0])
            self.assertEqual(scope, json.loads(measurement[1])['scope'])
            self.assertFalse(json.loads(measurement[1])['capture_verified'])
        fixture.control.call('mesh_node_update', control_id='owner-withdrew', capabilities=[], online=False)
        with self.assertRaises((Conflict, PermissionError)):
            submit(client, task_id, epoch, 'after-withdrawal', cap, 1, approval['id'])

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
            with tempfile.TemporaryDirectory() as directory:
                client = mock.Mock()
                client.request.side_effect = lambda path, payload: api.dispatch('POST', path, payload, worker)
                local = read_frame(client, lookup, directory)
                self.assertEqual(Path(local['path']).read_bytes(), base64.b64decode(frame['base64']))
                self.assertEqual(Path(local['path']).stat().st_mode & 0o777, 0o600)
                self.assertNotIn('base64', local)
                self.assertEqual(local['frame_expectation']['stream_id'], fixture.binding['stream_id'])
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

    def test_guard_matches_current_verified_bytes_and_geometry_with_source_receipt_age(self):
        fixture = fixtures.PhoneScreenShareTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.call('open')
        first = fixture.frame()
        fixture.call('frame', frame=first)
        lookup = {key: fixture.binding[key] for key in ('operation_id', 'capability_id', 'task_id', 'task_epoch')}
        client = mock.Mock()
        client.request.side_effect = lambda path, body: fixture.service.screen_share.lookup(fixture.f.actor, body)
        _, _, expected, fresh_for = fetch_frame(client, lookup)
        self.assertEqual(fresh_for, MAX_FRAME_AGE_SECONDS)
        self.assertEqual(verify_frame(client, expected, lambda: 10.0), 10.0 + MAX_FRAME_AGE_SECONDS)
        with self.assertRaises(FrameStaleError):
            verify_frame(client, expected, iter((10.0, 10.0 + MAX_FRAME_AGE_SECONDS)).__next__)
        for change in ({'stream_id': 'other-stream'}, {'binding_digest': 'a' * 64}, {'revision': 2}):
            with self.subTest(change=change), self.assertRaises(FrameStaleError):
                verify_frame(client, dict(expected, **change), lambda: 10.0)

        fixture.f.now += MAX_FRAME_AGE_SECONDS
        fixture.call('frame', frame=first)  # Exact retry cannot rejuvenate a frame.
        with self.assertRaises(FrameStaleError):
            verify_frame(client, expected, lambda: 10.0)
        fixture.call('frame', frame=fixture.frame(2))
        self.assertEqual(verify_frame(client, expected, lambda: 10.0), 10.0 + MAX_FRAME_AGE_SECONDS)

        fixture.f.now += 1
        fixture.call('frame', frame=dict(fixture.frame(3), width=320, height=640))
        with self.assertRaises(FrameStaleError):
            verify_frame(client, expected, lambda: 10.0)
        fixture.f.now += 1
        changed = blob(b'\xff\xd8\xffdifferent screen\xff\xd9')
        fixture.call('frame', frame=dict(fixture.frame(4), **changed))
        with self.assertRaises(FrameStaleError):
            verify_frame(client, expected, lambda: 10.0)

        old_reply = fixture.service.screen_share.lookup(fixture.f.actor, lookup)
        old_reply.pop('source_fresh_for_seconds')
        client.request.side_effect = None
        client.request.return_value = old_reply
        fetch_frame(client, lookup)  # Legacy sources can still be read and used for OCR.
        with self.assertRaisesRegex(ValueError, 'phone_frame_source_freshness_unavailable'):
            verify_frame(client, expected, lambda: 10.0)
