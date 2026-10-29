"""Small synthetic snapshot/cache fixtures; no private history or live services."""
import base64
import copy
import hashlib
import sqlite3
import types
import unittest

from assistant_mesh.business_blob_cache import BlobCache
from assistant_mesh.business_journal import canonical, seal
from assistant_mesh.business_replica import ReplicaConflict, pull
from assistant_mesh.business_replica_api import attach
from tests import test_business_replication as fixtures


class BusinessBlobCacheTests(unittest.TestCase):
    setUp = fixtures.BusinessReplicationTests.setUp

    def seed(self, blobs):
        path = self.root / 'seed.db'
        db = sqlite3.connect(str(path))
        try:
            db.execute('CREATE TABLE session_chunks(id TEXT,part INTEGER,body BLOB,PRIMARY KEY(id,part))')
            db.executemany('INSERT INTO session_chunks VALUES(?,?,?)',
                           [('unrelated-local-id', index, raw) for index, raw in enumerate(blobs)])
            db.commit()
        finally:
            db.close()
        return path

    def client(self):
        api = types.SimpleNamespace(store=self.store, dispatch=lambda *args: 'incumbent')
        journal = attach(api, self.config)
        reader = dict(role='replica_reader', node='replica-node', capabilities=['business.replica.read'])
        requests = []
        def request(path, payload):
            output = api.dispatch('POST', path, payload, reader)
            requests.append((copy.deepcopy(payload), copy.deepcopy(output)))
            return output
        return types.SimpleNamespace(request=request, api=api, journal=journal, reader=reader, requests=requests)

    def test_local_full_chunks_reused_one_missing_fetch_and_incremental_remains_inline(self):
        missing = b'new chunk missing from local backup\x00\xff'
        with self.store.transaction() as db:
            db.execute('INSERT INTO session_chunks VALUES(?,?,?)', ('missing', 1, missing))
        client = self.client()
        with BlobCache([self.seed([self.blob, b'\x00\xffreceipt'])], self.root / 'cache') as cache:
            state = pull(client, self.replica, 'source-authority', pages=100, limit=3, blob_cache=cache)
            self.assertTrue(state['caught_up_to_observed_head'])
            rows = self.replica.query('source-authority', state['incarnation'], 'session_chunks')['rows']
            self.assertEqual({'artifact': self.blob, 'missing': missing},
                             {row['value']['id']: row['value']['body'] for row in rows})
            fetches = [payload for payload, _ in client.requests if payload.get('action') == 'blob']
            self.assertEqual([hashlib.sha256(missing).hexdigest()], [item['sha256'] for item in fetches])
            self.assertEqual(1, cache.status()['stored_chunks'])
            self.assertGreaterEqual(cache.status()['seed_hits'], 2)
            self.assertEqual(missing, cache.get(hashlib.sha256(missing).hexdigest(), len(missing)))
            with self.store.transaction() as db:
                db.execute('INSERT INTO session_chunks VALUES(?,?,?)', ('later', 2, b'incremental-inline'))
            client.requests[:] = []
            pull(client, self.replica, 'source-authority', blob_cache=cache)
            self.assertFalse(any(payload.get('action') == 'blob' for payload, _ in client.requests))
            records = [record for _, page in client.requests for record in page.get('records', [])]
            self.assertIn(['blob', base64.b64encode(b'incremental-inline').decode('ascii')], records[-1]['row'])

    def test_page_fetch_is_owner_expiry_and_last_page_bound_with_fixed_replay_and_inline_compatibility(self):
        client = self.client()
        now = [100]
        client.journal.clock, client.journal.snapshot_ttl = lambda: now[0], 10
        route = '/v1/business-replica/snapshot'
        start = client.request(route, dict(action='begin', blob_mode='references'))
        cursor = 0
        while True:
            page = client.request(route, dict(action='page', snapshot_id=start['snapshot_id'], after=cursor, limit=1))
            if page['blob_sizes']:
                break
            cursor = page['cursor']
        sha256, size = next(iter(page['blob_sizes'].items()))
        request = dict(action='blob', snapshot_id=start['snapshot_id'], sha256=sha256)
        response = client.request(route, request)
        self.assertEqual(size, len(base64.b64decode(response['base64'])))
        other = dict(client.reader, node='other-reader')
        with self.assertRaisesRegex(ValueError, 'expired_or_unowned'):
            client.api.dispatch('POST', route, request, other)
        for invalid in (dict(request, path='/not-a-file-endpoint'), dict(request, sha256='0' * 64)):
            with self.assertRaises(ValueError):
                client.request(route, invalid)
        repeated = client.request(route, dict(action='page', snapshot_id=start['snapshot_id'], after=cursor, limit=500))
        self.assertEqual(canonical(page), canonical(repeated))
        with self.assertRaises(ValueError):
            client.request(route, dict(action='page', snapshot_id=start['snapshot_id'], after=cursor, blob_mode='inline'))
        client.request(route, dict(action='page', snapshot_id=start['snapshot_id'], after=page['cursor'], limit=1))
        with self.assertRaisesRegex(ValueError, 'not_in_current_page'):
            client.request(route, request)
        now[0] += 11
        with self.assertRaisesRegex(ValueError, 'expired_or_unowned'):
            client.request(route, request)
        inline = client.request(route, {'action': 'begin'})
        page = client.request(route, dict(action='page', snapshot_id=inline['snapshot_id'], after=0))
        self.assertNotIn('blob_mode', page)
        self.assertNotIn('blob_sizes', page)
        self.assertTrue(any(value[0] == 'blob' for row in page['rows'] for value in row['row']))
        self.assertTrue(self.replica.apply(page, 'source-authority')['baseline_published'])

    def test_missing_wrong_hash_and_lost_apply_response_keep_wire_receipt_and_old_view(self):
        start, _ = fixtures.BusinessReplicationTests.baseline(self)
        incarnation = start['incarnation']
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET result='new unpublished result' WHERE id='original'")
        start = self.journal.snapshot_begin(self.owner, blob_mode='references')
        cursor = 0
        while True:
            page = self.journal.snapshot_page(self.owner, start['snapshot_id'], cursor, limit=2)
            if page['blob_sizes']:
                break
            self.replica.apply(page, 'source-authority')
            cursor = page['cursor']
        with self.assertRaisesRegex(ReplicaConflict, 'dependency_missing'):
            self.replica.apply(page, 'source-authority')
        with self.assertRaisesRegex(ReplicaConflict, 'blob_content_conflict'):
            self.replica.apply(page, 'source-authority', blob_resolver=lambda *args: b'corrupt')
        forged = copy.deepcopy(page)
        forged['blob_sizes'][next(iter(forged['blob_sizes']))] += 1
        calls = []
        with self.assertRaisesRegex(ReplicaConflict, 'page_content_conflict'):
            self.replica.apply(forged, 'source-authority', blob_resolver=lambda *args: calls.append(args))
        self.assertEqual([], calls)
        def resolver(sha256, size):
            response = self.journal.snapshot_blob(self.owner, start['snapshot_id'], sha256)
            return base64.b64decode(response['base64'])
        self.replica.apply(page, 'source-authority', blob_resolver=resolver)
        self.journal.snapshot_end(self.owner, start['snapshot_id'])
        self.assertTrue(self.replica.apply(page, 'source-authority')['duplicate'])
        with self.replica.transaction() as db:
            self.assertEqual(page['sha256'], db.execute(
                'SELECT fingerprint FROM replica_snapshot_receipts WHERE snapshot_id=? AND after_cursor=?',
                (start['snapshot_id'], cursor)).fetchone()[0])
        changed = copy.deepcopy(page)
        changed.pop('sha256')
        changed['coverage_issues'] = ['forged-replay']
        seal(changed)
        with self.assertRaisesRegex(ReplicaConflict, 'snapshot_replay_conflict'):
            self.replica.apply(changed, 'source-authority')
        original = next(row['value'] for row in self.replica.query('source-authority', incarnation, 'tasks')['rows']
                        if row['value']['id'] == 'original')
        self.assertEqual('Full original result', original['result'])
        self.assertEqual(self.blob, self.replica.query('source-authority', incarnation, 'session_chunks')['rows'][0]['value']['body'])

    def test_changed_seed_and_corrupt_fetched_blob_cannot_publish(self):
        seed = self.seed([b'local-seed'])
        sha256 = hashlib.sha256(b'local-seed').hexdigest()
        with BlobCache([seed], self.root / 'cache') as cache:
            self.assertEqual(b'local-seed', cache.get(sha256, len(b'local-seed')))
            db = sqlite3.connect(str(seed))
            try:
                db.execute("UPDATE session_chunks SET body=?", (b'changed!!!',))
                db.commit()
            finally:
                db.close()
            self.assertIsNone(cache.get(sha256, len(b'local-seed')))
            start, _ = fixtures.BusinessReplicationTests.baseline(self)
            client = self.client()
            # Force an explicit new generation/bootstrap while retaining the
            # existing published view, then corrupt one authenticated reply.
            self.journal.refresh()
            actual_request = client.request
            def corrupt(path, payload):
                result = actual_request(path, payload)
                if payload.get('action') == 'blob':
                    result = dict(result, base64=base64.b64encode(b'bad').decode('ascii'))
                return result
            client.request = corrupt
            with self.assertRaisesRegex(ReplicaConflict, 'blob_content_conflict'):
                pull(client, self.replica, 'source-authority', blob_cache=cache)
            state = self.replica.state('source-authority', start['incarnation'])
            self.assertEqual(start['generation'], state['generation'])
            self.assertEqual(self.blob, self.replica.query('source-authority', start['incarnation'], 'session_chunks')['rows'][0]['value']['body'])
