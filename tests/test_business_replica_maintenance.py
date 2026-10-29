"""Bounded maintenance fixtures over synthetic ledgers; no live authorities."""
import copy
import json
import sqlite3
import threading
import types
import unittest

from assistant_mesh.business_journal import ACK_FIELDS, ACK_PROTOCOL, Journal, PREFIX, canonical, digest
from assistant_mesh.business_replica import pull
from assistant_mesh.business_replica_api import attach
from assistant_mesh.business_replica_service import acknowledge_committed, save_private
from tests import test_business_replication as fixtures


class BusinessReplicaMaintenanceTests(unittest.TestCase):
    def setUp(self):
        fixtures.BusinessReplicationTests.setUp(self)
        self.addCleanup(self.journal.close)

    def write_events(self, count):
        with self.store.transaction() as db:
            for index in range(count):
                db.execute("UPDATE tasks SET result=? WHERE id='original'", ('new-result-' + str(index),))

    def payload(self, journal=None, cursor=None, **changes):
        current = (journal or self.journal).status()
        value = dict(authority=current['authority'], incarnation=current['incarnation'],
            generation=current['generation'], cursor=current['head'] if cursor is None else cursor,
            manifest_sha256=current['manifest']['sha256'])
        value.update(changes)
        value['receipt_id'] = digest(value)
        return value

    def api(self):
        api = types.SimpleNamespace(store=self.store, dispatch=lambda *args: 'incumbent')
        journal = attach(api, self.config)
        self.addCleanup(journal.close)
        reader = dict(role='replica_reader', node='replica-node', capabilities=['business.replica.read'])
        return api, journal, reader

    def test_absolute_timers_close_reader_and_cache_without_a_followup_rpc(self):
        armed, timers = threading.Event(), []
        now = [100]
        def factory(delay, callback):
            def fire():
                armed.wait(2)
                callback()
            timer = threading.Timer(0, fire)
            timers.append((delay, timer))
            return timer
        journal = Journal(self.store, self.config, snapshot_ttl=10,
                          clock=lambda: now[0], timer_factory=factory)
        self.addCleanup(journal.close)
        first = journal.snapshot_begin(self.owner, 'references')
        value = journal.snapshots[first['snapshot_id']]
        reader = value['db']
        journal.snapshot_page(self.owner, first['snapshot_id'], 0, limit=2)
        self.assertTrue(value['blobs'])
        second = journal.snapshot_begin(self.owner)
        second_reader = journal.snapshots[second['snapshot_id']]['db']
        with self.assertRaisesRegex(ValueError, 'capacity'):
            journal.snapshot_begin(self.owner)
        self.assertEqual([10, 10], [delay for delay, _ in timers])
        journal.snapshot_end(self.owner, second['snapshot_id'])
        with self.assertRaises(sqlite3.ProgrammingError):
            second_reader.execute('SELECT 1')
        self.write_events(1)
        now[0] = 111
        armed.set()
        for _, timer in timers:
            timer.join(2)
            self.assertFalse(timer.is_alive())
        # No source method/RPC has been called since advancing the clock.
        self.assertEqual({}, journal.snapshots)
        self.assertEqual({}, value)
        with self.assertRaises(sqlite3.ProgrammingError):
            reader.execute('SELECT 1')

    def test_two_peer_ack_monotonic_bounded_prune_and_old_cursor_rebootstrap_preserve_unknown(self):
        initial, _ = fixtures.BusinessReplicationTests.baseline(self)
        before = self.journal.status()
        self.journal.configure_retention(['one', 'two'], keep_events=2, batch=3)
        self.assertEqual(before['manifest'], self.journal.status()['manifest'])
        self.assertEqual(before['generation'], self.journal.status()['generation'])
        self.write_events(7)
        head = self.journal.status()['head']
        one = self.payload()
        self.assertEqual('required_ack_missing', self.journal.acknowledge('one', one)['retention'])
        with self.store.transaction() as db:
            self.assertEqual(head, db.execute('SELECT COUNT(*) FROM ' + PREFIX + 'events').fetchone()[0])
        result = self.journal.acknowledge('two', self.payload())
        self.assertEqual(3, result['pruned_events'])
        floor = result['retained_after']
        for invalid in (self.payload(cursor=head - 1), self.payload(cursor=head + 1),
                        self.payload(generation=before['generation'] + 1),
                        self.payload(manifest_sha256='0' * 64)):
            with self.assertRaises(ValueError):
                self.journal.acknowledge('one', invalid)
            self.assertEqual(floor, self.journal.status()['retained_after'])
        for _ in range(10):
            result = self.journal.acknowledge('one', one)
            self.assertTrue(result['duplicate'])
            self.assertLessEqual(result['pruned_events'], 3)
            self.assertGreaterEqual(result['retained_after'], floor)
            floor = result['retained_after']
            if not result['pruned_events']:
                break
        with self.store.transaction() as db:
            self.assertEqual(2, db.execute('SELECT COUNT(*) FROM ' + PREFIX + 'events').fetchone()[0])
            self.assertIsNone(db.execute('SELECT 1 FROM ' + PREFIX + 'cells WHERE sequence<=?', (floor,)).fetchone())
            self.assertEqual(self.unknown, json.loads(db.execute(
                "SELECT checkpoint FROM tasks WHERE id='original'").fetchone()[0]))
        status = self.journal.status()
        self.assertEqual(head, status['head'])
        with self.assertRaisesRegex(ValueError, 'bootstrap_required'):
            self.journal.export(0, status['incarnation'], status['generation'])
        remaining = self.journal.export(floor, status['incarnation'], status['generation'])
        self.assertEqual(head, remaining['cursor'])
        api, _, reader = self.api()
        client = types.SimpleNamespace(request=lambda path, body: api.dispatch('POST', path, body, reader))
        result = pull(client, self.replica, 'source-authority')
        self.assertEqual(initial['incarnation'], result['incarnation'])
        self.assertEqual(before['generation'], result['generation'])
        original = next(row['value'] for row in self.replica.query(
            'source-authority', initial['incarnation'], 'tasks')['rows'] if row['value']['id'] == 'original')
        self.assertEqual(self.unknown, json.loads(original['checkpoint']))
        self.assertFalse(result['execution_authorized'])

    def test_active_snapshot_bridge_is_held_or_explicitly_closed_across_instances(self):
        self.journal.configure_retention(['replica'], keep_events=1, batch=2000)
        self.write_events(2)
        snapshot = self.journal.snapshot_begin(self.owner)
        self.write_events(3)
        result = self.journal.acknowledge('replica', self.payload())
        self.assertLessEqual(result['retained_after'], snapshot['baseline_cursor'])
        self.journal.snapshot_end(self.owner, snapshot['snapshot_id'])
        other = Journal(self.store, self.config)
        self.addCleanup(other.close)
        snapshot = other.snapshot_begin(self.owner)
        pinned = other.snapshots[snapshot['snapshot_id']]['db']
        self.write_events(3)
        result = self.journal.acknowledge('replica', self.payload())
        self.assertGreater(result['retained_after'], snapshot['baseline_cursor'])
        with self.assertRaisesRegex(ValueError, 'bootstrap_required'):
            other.snapshot_page(self.owner, snapshot['snapshot_id'], 0)
        self.assertNotIn(snapshot['snapshot_id'], other.snapshots)
        with self.assertRaises(sqlite3.ProgrammingError):
            pinned.execute('SELECT 1')

    def test_retention_operator_and_own_ack_reader_roles_are_separate_and_manifest_stable(self):
        api, journal, reader = self.api()
        operator = {'role': 'operator'}
        route = '/v1/business-replica/'
        config = dict(required_peers=['replica-node'], keep_events=1, batch=2)
        original = journal.status()
        with self.store.transaction() as db:
            self.assertEqual(1000, journal._retention(db)['keep_events'])
            self.assertEqual([], journal._retention(db)['required_peers'])
        with self.assertRaises(PermissionError):
            api.dispatch('POST', route + 'configure-retention', config, reader)
        api.dispatch('POST', route + 'configure-retention', config, operator)
        payload = self.payload(journal)
        for peer in (operator, dict(reader, role='worker'), dict(reader, role='agent_peer')):
            with self.assertRaises(PermissionError):
                api.dispatch('POST', route + 'ack', payload, peer)
        with self.assertRaises(ValueError):
            api.dispatch('POST', route + 'ack', dict(payload, peer='someone-else'), reader)
        with self.assertRaisesRegex(ValueError, 'receipt_invalid'):
            api.dispatch('POST', route + 'ack', dict(payload, receipt_id='wrong'), reader)
        result = api.dispatch('POST', route + 'ack', payload, reader)
        self.assertEqual('authenticated_receiver_assertion', result['assertion'])
        self.assertFalse(result['remote_disk_independently_verified'])
        self.assertEqual(original['manifest'], journal.status()['manifest'])
        self.assertEqual(original['generation'], journal.status()['generation'])

    def test_service_fsync_receipts_precede_ack_and_lost_response_reuses_original_id(self):
        start, _ = fixtures.BusinessReplicationTests.baseline(self)
        result = dict(self.replica.state('source-authority', start['incarnation']),
                      ack_protocol=ACK_PROTOCOL, retained_after=0)
        state = self.root / 'service-state'
        state.mkdir(mode=0o700)
        save_private(state / 'last-sync.json', result)
        received = []
        def request(path, payload):
            self.assertEqual('/v1/business-replica/ack', path)
            pending = json.loads((state / 'pending-ack.json').read_text())
            sync = json.loads((state / 'last-sync.json').read_text())
            self.assertEqual(payload, pending['ack'])
            self.assertEqual(result['cursor'], sync['cursor'])
            self.assertEqual(result['cursor'], self.replica.state('source-authority', start['incarnation'])['cursor'])
            received.append(copy.deepcopy(payload))
            response = self.journal.acknowledge('replica-node', payload)
            if len(received) == 1:
                raise OSError('lost ACK response after source commit')
            return response
        client = types.SimpleNamespace(request=request)
        self.assertEqual('pending', acknowledge_committed(client, result, state)['status'])
        pending = json.loads((state / 'pending-ack.json').read_text())
        self.assertEqual('acknowledged', acknowledge_committed(client, result, state)['status'])
        self.assertEqual(received[0], received[1])
        self.assertEqual(digest({key: received[0][key] for key in ACK_FIELDS}), pending['ack']['receipt_id'])
        self.assertFalse((state / 'pending-ack.json').exists())
        result['ack_protocol'] = None
        unsupported = types.SimpleNamespace(request=lambda *args: self.fail('old source must not receive ACK'))
        self.assertEqual('not_supported', acknowledge_committed(unsupported, result, state)['status'])
        self.assertEqual(start['baseline_cursor'], self.replica.state('source-authority', start['incarnation'])['cursor'])
