"""Authenticated loopback/SQLite sync; never models, real accounts or spend."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.cli import main
from assistant_mesh.federation_sync import FederationSync
from assistant_mesh.server import serve
from assistant_mesh.store import Store
from assistant_mesh.worker import Client, Worker


class FederationSyncTests(unittest.TestCase):
    def setUp(self):
        self.mask = os.umask(0o077)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000.0
        self.server, self.api = None, None
        self.clients, peers = {}, []
        for role in ('operator', 'viewer', 'reader', 'denied', 'worker'):
            token = self.root / (role + '.token')
            token.write_text('fixture-only-' + role + '-' + 'x' * 64)
            token.chmod(0o600)
            peer = {'role': role if role in ('operator', 'viewer', 'worker') else 'agent_peer',
                    'token_file': str(token)}
            if role in ('reader', 'denied', 'worker'):
                peer.update(node=role, capabilities=['agent'] + (['capability.catalog'] if role == 'reader' else []))
            peers.append(peer)
        ready = threading.Event()
        def installed(server, api, channel):
            self.server, self.api = server, api
            self.api.store.clock = lambda: self.now
            ready.set()
        self.config = {'node_id': 'source', 'port': 0, 'database': str(self.root / 'source.sqlite'),
                       'peers': peers, 'federation': {'export_enabled': True, 'projection_enabled': True}}
        self.thread = threading.Thread(target=serve, args=(self.config, installed), daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(3))
        for role in ('operator', 'viewer', 'reader', 'denied', 'worker'):
            value = {'control_url': 'http://127.0.0.1:' + str(self.server.server_address[1]),
                     'token_file': str(self.root / (role + '.token'))}
            self.clients[role] = Client(value)
            path = self.root / (role + '.json')
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        self.dest = Store(str(self.root / 'destination.sqlite'), clock=lambda: self.now,
                          recover_inflight=False)

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.thread.join(timeout=3)
            self.server.server_close()
        self.temp.cleanup()
        os.umask(self.mask)

    def advertise(self, identity='egress'):
        return self.api.resources.advertise('node:source-worker', identity, 'network.egress',
                                          spec={'declared_only': True}, lease_seconds=300)

    def sync(self, client=None, **options):
        return FederationSync(self.dest, 'source', 'source', client or self.clients['reader'], **options)

    def test_authenticated_read_export_requires_explicit_peer_catalog_grant(self):
        self.advertise()
        for role in ('reader', 'operator', 'viewer'):
            page = self.clients[role].request('/v1/mesh/capability-export')
            self.assertEqual('source', page['issuer'])
            self.assertEqual('egress', page['records'][0]['id'])
        for role in ('denied', 'worker'):
            with self.subTest(role=role), self.assertRaises(urllib.error.HTTPError) as caught:
                self.clients[role].request('/v1/mesh/capability-export')
            self.assertEqual(403, caught.exception.code)
        self.assertEqual([], self.clients['reader'].request('/v1/mesh/capability-projection')['capabilities'])

    def test_routes_reject_post_forged_issuer_unknown_query_and_duplicate_cursor(self):
        for path, body in (('/v1/mesh/capability-export', {}),
                           ('/v1/mesh/capability-export?issuer=impostor', None),
                           ('/v1/mesh/capability-export?after=0&after=1', None),
                           ('/v1/mesh/capability-projection?actor=operator', None),
                           ('/v1/mesh/capability-projection', {'page': 'DO_NOT_IMPORT'})):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                self.clients['reader'].request(path, body)
            self.assertIn(caught.exception.code, (400, 403))

    def test_default_disabled_export_does_not_become_remote_admission(self):
        self.api.federation_source = None
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.clients['operator'].request('/v1/mesh/capability-export')
        self.assertEqual(403, caught.exception.code)
        self.assertFalse(self.sync().step()['managed_invocation_authorized'])

    def test_actual_http_bounded_pages_preserve_source_identity_and_cursor(self):
        for identity in ('a', 'b', 'c'):
            self.advertise(identity)
        synchronizer = self.sync(page_limit=1)
        for index in range(3):
            result = synchronizer.step()
            self.assertEqual('observed', result['state'])
            self.assertEqual(1, result['applied'])
            self.assertEqual(index == 2, result['complete'])
            self.now += 16
        candidates = synchronizer.projection.discover(include_unavailable=True)['capabilities']
        self.assertEqual(['a', 'b', 'c'], [row['id'] for row in candidates])
        self.assertTrue(all(row['issuer'] == 'source' and not row['managed_invocation_authorized'] for row in candidates))
        with self.dest.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='capabilities'").fetchone())

    def test_hello_identity_mismatch_prevents_catalogue_request(self):
        calls = []
        class WrongHello:
            def request(inner, path):
                calls.append(path)
                return {'protocol': 'mesh-a2a/1', 'node': 'source', 'authority': 'other'}
        result = self.sync(WrongHello()).step()
        self.assertEqual(['catalog_identity_rejected'], result['diagnostics'])
        self.assertEqual(['/v1/mesh/hello'], calls)
        self.assertEqual(0, result['projection']['cursor'])

    def test_disconnect_local_task_survives_reopen_backoff_and_rejoin_tombstone(self):
        self.advertise()
        target = self.clients['reader']
        class Switchable:
            fail = False
            def request(inner, path):
                if inner.fail:
                    raise OSError('DO_NOT_PRINT private endpoint')
                return target.request(path)
        transport = Switchable()
        synchronizer = self.sync(transport)
        self.assertEqual('observed', synchronizer.step()['state'])
        local = self.dest.create_task('Local work continues', task_id='offline-local-task', required=['agent'])
        transport.fail = True
        self.now += 16
        failed = synchronizer.step()
        self.assertEqual('degraded', failed['state'])
        self.assertEqual('disconnected', failed['projection']['connection_state'])
        self.assertEqual([], synchronizer.projection.discover()['capabilities'])
        self.assertEqual('pending', self.dest.task_status(local)['status'])
        reopened = Store(self.dest.path, clock=lambda: self.now, recover_inflight=False)
        cold = FederationSync(reopened, 'source', 'source', transport)
        self.assertEqual('waiting', cold.step()['state'])
        self.assertEqual(failed['retry_at'], cold.status()['retry_at'])
        self.api.resources.revoke('node:source-worker', 'egress', 1)
        transport.fail = False
        self.now += 3
        self.assertEqual('observed', cold.step()['state'])
        cap = cold.projection.discover(include_unavailable=True)['capabilities'][0]
        self.assertTrue(cap['capability']['revoked'])
        self.assertEqual(1, cap['capability']['epoch'])
        self.assertEqual(2, cap['revision'])
        self.assertEqual('pending', self.dest.task_status(local)['status'])

    def test_no_change_poll_does_not_refresh_capability_receipt_or_lease(self):
        self.advertise()
        synchronizer = self.sync()
        synchronizer.step()
        before = synchronizer.projection.discover()['capabilities'][0]
        self.now += 16
        synchronizer.step()
        after = synchronizer.projection.discover()['capabilities'][0]
        self.assertEqual(before['received_at'], after['received_at'])
        self.assertEqual(before['local_deadline_estimate'], after['local_deadline_estimate'])
        self.assertLess(after['remaining_seconds_estimate'], before['remaining_seconds_estimate'])

    def test_source_head_rollback_is_not_reset_or_second_identity(self):
        self.advertise()
        synchronizer = self.sync()
        synchronizer.step()
        with self.api.store.transaction() as db:
            db.execute('DELETE FROM capability_audit')
        self.now += 16
        result = synchronizer.step()
        self.assertEqual('degraded', result['state'])
        self.assertEqual(1, result['projection']['cursor'])
        self.assertEqual(['catalog_source_conflict'], result['diagnostics'])

    def test_stop_during_read_discards_unapplied_page_without_touching_tasks(self):
        self.advertise()
        stopped, target = threading.Event(), self.clients['reader']
        class StopAfterPage:
            def request(inner, path):
                result = target.request(path)
                if 'capability-export' in path:
                    stopped.set()
                return result
        synchronizer = self.sync(StopAfterPage())
        self.assertEqual('stopped', synchronizer.step(stop=stopped)['state'])
        self.assertEqual(0, synchronizer.projection.status('source')['cursor'])
        self.assertEqual([], synchronizer.projection.discover(include_unavailable=True)['capabilities'])

    def test_cli_and_model_read_same_projection_without_import_route(self):
        page = self.api.federation_source.export()
        self.api.federation_projection.apply('source', page)
        for command, extra in (('mesh-capabilities', ['--issuer', 'source']), ('mesh-capability-export', ['--after', '0'])):
            stream = io.StringIO()
            with patch('sys.argv', ['mesh', '--config', str(self.root / 'reader.json'), command] + extra), contextlib.redirect_stdout(stream):
                main()
            self.assertIsInstance(json.loads(stream.getvalue()), dict)
        worker = Worker({'node_id': 'worker', 'capabilities': ['agent']}, client=self.clients['worker'])
        with patch.object(worker, 'tick'):
            reply = worker.on_tool({'tool': 'mesh', 'callId': 'read-only',
                'arguments': {'action': 'federated_capabilities', 'arguments': {'include_unavailable': True}}})
        body = json.loads(reply['contentItems'][0]['text'])
        self.assertTrue(reply['success'])
        self.assertFalse(body['managed_invocation_authorized'])
        with patch.object(worker, 'tick'):
            denied = worker.on_tool({'tool': 'mesh', 'callId': 'forged',
                'arguments': {'action': 'federated_capabilities', 'arguments': {'actor': 'operator'}}})
        self.assertFalse(denied['success'])

    def test_options_are_not_model_authority_or_native_restrictions(self):
        for options in ({'interval_seconds': 0}, {'interval_seconds': True}, {'page_limit': False},
                        {'page_limit': 1001}, {'freshness_seconds': 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.sync(**options)
        self.assertFalse(self.sync().status()['native_tools_intercepted'])

    def test_actual_http_wire_fits_compact_source_page_budget(self):
        spec = {'field-%04d' % index: 'small-value' for index in range(1000)}
        for index in range(300):
            self.api.resources.advertise('node:source-worker', 'large-%04d' % index,
                'open.kind', spec=spec, description='x' * 7900, lease_seconds=300)
        page = self.clients['reader'].request('/v1/mesh/capability-export?limit=1000')
        self.assertFalse(page['complete'])
        self.assertGreater(len(page['records']), 100)
        wire = json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode('utf8')
        self.assertLessEqual(len(wire), 8 * 1024 * 1024)
        remainder = self.clients['reader'].request('/v1/mesh/capability-export?limit=1000&after=' +
                                                   str(page['next_cursor']))
        self.assertTrue(remainder['complete'])
        self.assertEqual(300, len(page['records']) + len(remainder['records']))


if __name__ == '__main__':
    unittest.main()
