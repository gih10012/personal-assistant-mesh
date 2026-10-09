"""Actual localhost HTTP and durable SQLite fixtures; no iLink/model calls."""
import copy
import hashlib
import json
import multiprocessing
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

from assistant_mesh.notification_relay import NotificationRelay, NotificationRelayError
from assistant_mesh.remote_notifications import PROTOCOL, RemoteNotifications, receipt_id
from assistant_mesh.store import Conflict, Store
from assistant_mesh.worker import Client


SOURCE = 'fixture-laptop'
PEER = 'fixture-cloud-peer'
AUTHORITY = 'fixture-cloud-authority'
POLICY = {'mode': 'private', 'owner_relay': {'peer': PEER, 'authority': AUTHORITY}}
ACCOUNT_BINDING = hashlib.sha256(b'fixture-owner-bot-origin').hexdigest()
TOKEN = hashlib.sha256(b'fixture-control-token-only').hexdigest()


class ThreadHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def crash_after_remote_receipt(path, config):
    """Real process death after remote enqueue, before local result persistence."""
    store = Store(path, notification_policy=POLICY, node_id=SOURCE)
    client = Client(config)
    original = client.request
    def request(route, body=None):
        value = original(route, body)
        if route == '/v1/mesh/notify':
            os._exit(31)
        return value
    client.request = request
    NotificationRelay(store, SOURCE, PEER, AUTHORITY, client).poll_once()
    os._exit(32)


def poll_in_child(path, config, output):
    store = Store(path, notification_policy=POLICY, node_id=SOURCE)
    output.put(NotificationRelay(store, SOURCE, PEER, AUTHORITY, Client(config)).poll_once())


class NotificationRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-notify-fixture.')
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.now = 1000.0
        self.local = Store(self.root / 'node.db', clock=lambda: self.now,
                           notification_policy=POLICY, node_id=SOURCE)
        self.remote = Store(self.root / 'channel.db', clock=lambda: self.now)
        self.notifications = RemoteNotifications(self.remote, AUTHORITY, route_id=ACCOUNT_BINDING)
        self.route = self.notifications.route_id
        self.token = self.root / 'fixture.token'
        self.token.write_text(TOKEN)
        self.token.chmod(0o600)
        self.calls = []
        self.post_count = 0
        self.transform = None
        self.drop_post = False
        self.drop_precommit_post = False
        self.deny = None
        self.before = None
        self.after = None
        test = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                body = json.loads(raw.decode('utf8'))
                test.calls.append((self.path, copy.deepcopy(body)))
                if test.before:
                    test.before(self.path, body)
                if test.drop_precommit_post and self.path == '/v1/mesh/notify':
                    test.drop_precommit_post = False
                    test.post_count += 1
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                code = 200
                if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    code, response = 401, {'private': 'DO_NOT_EXPOSE bad token'}
                elif test.deny:
                    code, response = test.deny, {'private': 'DO_NOT_EXPOSE channel error'}
                else:
                    try:
                        if self.path == '/v1/mesh/notify/status':
                            response = test.notifications.status(SOURCE, body)
                        elif self.path == '/v1/mesh/notify':
                            test.post_count += 1
                            response = test.notifications.receive(SOURCE, body)
                        else:
                            code, response = 404, {'private': 'DO_NOT_EXPOSE missing route'}
                    except Conflict:
                        code, response = 409, {'private': 'DO_NOT_EXPOSE content conflict'}
                    except PermissionError:
                        code, response = 403, {'private': 'DO_NOT_EXPOSE not ready'}
                    except ValueError:
                        code, response = 400, {'private': 'DO_NOT_EXPOSE invalid fixture request'}
                    if test.after:
                        test.after(self.path, body, response)
                    if test.transform:
                        response = test.transform(self.path, copy.deepcopy(response))
                if test.drop_post and self.path == '/v1/mesh/notify':
                    test.drop_post = False
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                data = json.dumps(response).encode('utf8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.config = {'control_url': 'http://127.0.0.1:' + str(self.server.server_address[1]),
                       'token_file': str(self.token)}
        self.client = Client(self.config)
        self.relay = NotificationRelay(self.local, SOURCE, PEER, AUTHORITY, self.client)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def enqueue(self, identity='explicit-notice', text='隔离节点通知，不发送微信'):
        self.local.enqueue(identity, text)
        return identity

    def row(self, identity='explicit-notice'):
        with self.local.transaction() as db:
            value = db.execute('SELECT * FROM notification_relays WHERE request_id=?', (identity,)).fetchone()
            return dict(value) if value is not None else None

    def due(self):
        self.now += 100

    def remote_state(self, state, identity='explicit-notice'):
        key = receipt_id(SOURCE, identity)
        with self.remote.transaction() as db:
            db.execute('UPDATE outbox SET status=? WHERE id=?', (state, key))

    def remote_rows(self):
        with self.remote.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM outbox')]

    def test_actual_http_durable_intent_and_route_pin_precede_first_post(self):
        self.enqueue()
        seen = []
        def before(path, body):
            row = self.row()
            self.assertEqual('submitting', row['state'])
            self.assertEqual(1, row['attempt'])
            if path == '/v1/mesh/notify':
                self.assertEqual(1, row['post_attempted'])
                pinned = json.loads(row['receipt'])
                self.assertEqual(self.route, pinned['route_id'])
                self.assertFalse(pinned['found'])
                seen.append(True)
            else:
                self.assertEqual(0, row['post_attempted'])
        self.before = before
        result = self.relay.poll_once()
        self.assertEqual('queued', result['state'])
        self.assertFalse(result['delivery_verified'])
        self.assertEqual([True], seen)
        self.assertEqual(['/v1/mesh/notify/status', '/v1/mesh/notify'], [r[0] for r in self.calls])
        self.assertEqual(1, len(self.remote_rows()))
        self.assertEqual('pending', self.local.send_status('explicit-notice')['detail']['receipt']['status'])
        with self.local.transaction() as db:
            out = dict(db.execute('SELECT * FROM outbox').fetchone())
        self.assertEqual('pending', out['status'])
        self.assertEqual('relay', out['delivery_route'])
        self.assertIsNone(self.local.next_send())

    def test_pending_remote_status_is_query_only_and_accepted_is_terminal(self):
        self.enqueue()
        self.relay.poll_once()
        self.due()
        self.assertEqual('queued', self.relay.poll_once()['state'])
        self.assertEqual(1, self.post_count)
        self.remote_state('accepted')
        self.due()
        self.assertEqual('accepted', self.relay.poll_once()['state'])
        self.due()
        count = len(self.calls)
        self.assertEqual('idle', self.relay.poll_once()['state'])
        self.assertEqual(count, len(self.calls))
        self.assertFalse(self.local.send_status('explicit-notice')['delivery_verified'])

    def test_response_lost_after_enqueue_preserves_id_and_queries_no_extra_post(self):
        self.enqueue()
        self.drop_post = True
        result = self.relay.poll_once()
        self.assertEqual('unknown', result['state'])
        self.assertEqual('relay_connection_unavailable', result['error'])
        pinned = json.loads(self.row()['receipt'])
        self.assertEqual(self.route, pinned['route_id'])
        self.assertFalse(pinned['found'])
        self.notifications = RemoteNotifications(self.remote, AUTHORITY, route_id=ACCOUNT_BINDING)
        self.assertEqual(self.route, self.notifications.route_id)
        self.due()
        reopened = Store(self.local.path, clock=lambda: self.now, notification_policy=POLICY, node_id=SOURCE)
        self.assertEqual('queued', NotificationRelay(reopened, SOURCE, PEER, AUTHORITY, self.client).poll_once()['state'])
        self.assertEqual(1, self.post_count)
        self.assertEqual(1, len(self.remote_rows()))
        self.assertEqual('explicit-notice', self.calls[-1][1]['request_id'])

    def test_lost_first_ack_then_same_account_fresh_remote_database_never_reposts(self):
        self.enqueue()
        self.drop_post = True
        self.assertEqual('unknown', self.relay.poll_once()['state'])
        pinned = json.loads(self.row()['receipt'])
        self.assertFalse(pinned['found'])
        self.assertEqual(self.route, pinned['route_id'])
        self.assertEqual(1, self.post_count)
        original = self.remote
        self.assertEqual(1, len(self.remote_rows()))

        # Same authenticated node and owner account, but a brand-new receiver
        # ledger. Its durable random incarnation is NOT the old atomic receipt
        # namespace. The old owner's delivery may have already happened.
        self.remote = Store(self.root / 'replacement-channel.db', clock=lambda: self.now)
        self.notifications = RemoteNotifications(self.remote, AUTHORITY, route_id=ACCOUNT_BINDING)
        self.assertNotEqual(self.route, self.notifications.route_id)
        for _ in range(2):
            self.due()
            reopened = Store(self.local.path, clock=lambda: self.now,
                             notification_policy=POLICY, node_id=SOURCE)
            relay = NotificationRelay(reopened, SOURCE, PEER, AUTHORITY, self.client)
            result = relay.poll_once()
            self.assertEqual('unknown', result['state'])
            self.assertEqual('relay_route_changed', result['error'])
            self.assertEqual(pinned, json.loads(self.row()['receipt']))
            self.assertFalse(result['delivery_verified'])
        self.assertEqual(1, self.post_count)
        self.assertEqual([], self.remote_rows())
        with self.remote.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM mesh_notify_receipts').fetchone()[0])
        with original.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.assertEqual(['/v1/mesh/notify/status', '/v1/mesh/notify',
                          '/v1/mesh/notify/status', '/v1/mesh/notify/status'], [p for p, _ in self.calls])

    def test_lost_first_ack_then_old_snapshot_same_incarnation_never_reposts(self):
        with self.remote.transaction() as db:
            snapshot = '\n'.join(db.iterdump())
        self.enqueue()
        self.drop_post = True
        self.assertEqual('unknown', self.relay.poll_once()['state'])
        pinned = json.loads(self.row()['receipt'])
        self.assertFalse(pinned['found'])
        self.assertEqual(1, self.row()['post_attempted'])
        self.assertEqual(1, self.post_count)
        self.assertEqual(1, len(self.remote_rows()))

        # A real old SQLite snapshot retains the same receiver identity while
        # losing the committed notification. This is not a fresh incarnation.
        restored_path = self.root / 'old-snapshot-channel.db'
        db = sqlite3.connect(str(restored_path))
        try:
            db.executescript(snapshot)
        finally:
            db.close()
        restored_path.chmod(0o600)
        self.remote = Store(restored_path, clock=lambda: self.now)
        self.notifications = RemoteNotifications(self.remote, AUTHORITY, route_id=ACCOUNT_BINDING)
        self.assertEqual(self.route, self.notifications.route_id)
        for _ in range(2):
            self.due()
            reopened = Store(self.local.path, clock=lambda: self.now,
                             notification_policy=POLICY, node_id=SOURCE)
            result = NotificationRelay(reopened, SOURCE, PEER, AUTHORITY, self.client).poll_once()
            self.assertEqual('relay_submission_unconfirmed', result['error'])
            self.assertEqual('unknown', result['state'])
            self.assertEqual(1, self.row()['post_attempted'])
            self.assertEqual(pinned, json.loads(self.row()['receipt']))
        self.assertEqual(1, self.post_count)
        self.assertEqual([], self.remote_rows())

    def test_lost_post_before_remote_commit_is_query_only_not_automatic_retry(self):
        self.enqueue()
        self.drop_precommit_post = True
        self.assertEqual('unknown', self.relay.poll_once()['state'])
        self.assertEqual([], self.remote_rows())
        self.assertEqual(1, self.row()['post_attempted'])
        pinned = json.loads(self.row()['receipt'])
        self.assertFalse(pinned['found'])
        for _ in range(2):
            self.due()
            reopened = Store(self.local.path, clock=lambda: self.now,
                             notification_policy=POLICY, node_id=SOURCE)
            result = NotificationRelay(reopened, SOURCE, PEER, AUTHORITY, self.client).poll_once()
            self.assertEqual('relay_submission_unconfirmed', result['error'])
            self.assertEqual(pinned, json.loads(self.row()['receipt']))
        self.assertEqual(1, self.post_count)
        self.assertEqual([], self.remote_rows())

    def test_status_failure_before_any_post_can_recover_without_replaying_submission(self):
        self.enqueue()
        self.deny = 503
        self.assertEqual('relay_peer_http_error', self.relay.poll_once()['error'])
        self.assertEqual(0, self.row()['post_attempted'])
        self.assertEqual(0, self.post_count)
        self.deny = None
        self.due()
        self.assertEqual('queued', self.relay.poll_once()['state'])
        self.assertEqual(1, self.row()['post_attempted'])
        self.assertEqual(1, self.post_count)
        self.assertEqual(1, len(self.remote_rows()))

    def test_same_account_fresh_database_between_status_and_post_refuses_enqueue(self):
        self.enqueue()
        def before(path, body):
            if path == '/v1/mesh/notify':
                self.assertEqual(self.route, body['route_id'])
                self.remote = Store(self.root / 'replacement-before-post.db', clock=lambda: self.now)
                self.notifications = RemoteNotifications(self.remote, AUTHORITY, route_id=ACCOUNT_BINDING)
                self.assertNotEqual(self.route, self.notifications.route_id)
        self.before = before
        self.assertEqual('relay_remote_conflict', self.relay.poll_once()['error'])
        self.assertEqual([], self.remote_rows())
        self.assertEqual(self.route, json.loads(self.row()['receipt'])['route_id'])
        self.before = None
        self.due()
        self.assertEqual('relay_route_changed', self.relay.poll_once()['error'])
        self.assertEqual(1, self.post_count)
        self.assertEqual([], self.remote_rows())

    def test_process_crash_after_remote_enqueue_recovers_original_journal_query_only(self):
        self.now = __import__('time').time()
        self.enqueue()
        context = multiprocessing.get_context('spawn')
        process = context.Process(target=crash_after_remote_receipt, args=(self.local.path, self.config))
        process.start()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
            self.fail('fixture process did not terminate')
        self.assertEqual(31, process.exitcode)
        self.assertEqual('submitting', self.row()['state'])
        self.assertEqual(self.route, json.loads(self.row()['receipt'])['route_id'])
        self.due()
        self.assertEqual('queued', self.relay.poll_once()['state'])
        self.assertEqual(1, self.post_count)
        self.assertEqual(1, len(self.remote_rows()))

    def test_remote_unknown_is_polled_never_posted_again(self):
        self.enqueue()
        self.relay.poll_once()
        self.remote_state('unknown')
        for _ in range(3):
            self.due()
            self.assertEqual('unknown', self.relay.poll_once()['state'])
        self.assertEqual(1, self.post_count)
        self.assertTrue(json.loads(self.row()['receipt'])['found'])
        self.assertFalse(self.local.send_status('explicit-notice')['delivery_verified'])

    def test_disappearing_known_receipt_never_reposts(self):
        self.enqueue()
        self.relay.poll_once()
        with self.remote.transaction() as db:
            db.execute('DELETE FROM mesh_notify_receipts')
            db.execute('DELETE FROM outbox')
        for _ in range(2):
            self.due()
            result = self.relay.poll_once()
            self.assertEqual('unknown', result['state'])
            self.assertEqual('relay_remote_receipt_missing', result['error'])
            self.assertTrue(json.loads(self.row()['receipt'])['found'])
        self.assertEqual(1, self.post_count)
        self.assertEqual([], self.remote_rows())

    def test_stale_submitting_with_durable_post_intent_queries_only(self):
        self.enqueue()
        # A POST may or may not have reached the remote receiver; not_found is
        # not permission to retry once a local submission intent was committed.
        with self.local.transaction() as db:
            db.execute("UPDATE notification_relays SET state='submitting',attempt=1,post_attempted=1,lease_until=?", (self.now - 1,))
        self.assertEqual('relay_submission_unconfirmed', self.relay.poll_once()['error'])
        self.assertEqual(0, self.post_count)
        self.assertTrue(all(body['request_id'] == 'explicit-notice' for _, body in self.calls))

    def test_wrong_original_source_peer_or_authority_never_calls_network(self):
        self.enqueue()
        for source, peer, authority in [('different-source', PEER, AUTHORITY),
                                        (SOURCE, 'different-peer', AUTHORITY),
                                        (SOURCE, PEER, 'different-authority')]:
            self.due()
            relay = NotificationRelay(self.local, source, peer, authority, self.client)
            result = relay.poll_once()
            self.assertEqual('relay_original_target_mismatch', result['error'])
            row = self.row()
            self.assertEqual((SOURCE, PEER, AUTHORITY), (row['source_node'], row['peer'], row['authority']))
        self.assertEqual([], self.calls)
        self.assertEqual([], self.remote_rows())

    def test_changed_body_or_local_fingerprint_never_calls_network(self):
        self.enqueue()
        with self.local.transaction() as db:
            db.execute("UPDATE outbox SET body='tampered fixture body'")
        self.assertEqual('relay_body_mismatch', self.relay.poll_once()['error'])
        self.assertEqual([], self.calls)

    def test_changed_route_after_pin_never_posts_new_or_existing_message(self):
        self.enqueue()
        stopped = threading.Event()
        self.after = lambda path, body, value: stopped.set()
        self.assertEqual('stopped', self.relay.poll_once(stop=stopped)['state'])
        self.assertEqual(self.route, json.loads(self.row()['receipt'])['route_id'])
        self.assertEqual(0, self.post_count)
        self.after = None
        self.transform = lambda path, value: dict(value, route_id='1' * 64)
        for _ in range(2):
            self.due()
            self.assertEqual('relay_route_changed', self.relay.poll_once()['error'])
        self.assertEqual(0, self.post_count)
        self.assertEqual(self.route, json.loads(self.row()['receipt'])['route_id'])

    def test_post_route_mismatch_preserves_first_pin_and_avoids_retarget(self):
        self.enqueue()
        self.transform = lambda path, value: dict(value, route_id='2' * 64) if path == '/v1/mesh/notify' else value
        self.assertEqual('relay_route_changed', self.relay.poll_once()['error'])
        self.assertEqual(self.route, json.loads(self.row()['receipt'])['route_id'])
        self.transform = None
        self.due()
        self.assertEqual('queued', self.relay.poll_once()['state'])
        self.assertEqual(1, self.post_count)

    def test_route_changes_between_status_and_post_are_rejected_before_enqueue(self):
        self.enqueue()
        def before(path, body):
            if path == '/v1/mesh/notify':
                self.assertEqual(self.route, body['route_id'])
                self.notifications.route_id = '3' * 64
        self.before = before
        result = self.relay.poll_once()
        self.assertEqual('relay_remote_conflict', result['error'])
        self.assertEqual([], self.remote_rows())
        self.assertEqual(self.route, json.loads(self.row()['receipt'])['route_id'])
        self.before = None
        self.due()
        self.assertEqual('relay_route_changed', self.relay.poll_once()['error'])
        self.assertEqual(1, self.post_count)
        self.assertEqual([], self.remote_rows())

    def test_protocol_identity_type_status_and_sha_checks_reject_before_post(self):
        mutations = [lambda v: dict(v, protocol='old-server'),
                     lambda v: dict(v, authority='another-authority'),
                     lambda v: dict(v, source_node='forged-source'),
                     lambda v: dict(v, request_id='different-id'),
                     lambda v: dict(v, fingerprint='0' * 64),
                     lambda v: dict(v, outbox_id='new-random-id'),
                     lambda v: dict(v, found=0),
                     lambda v: dict(v, idempotent_receive=1),
                     lambda v: dict(v, delivery_verified=0),
                     lambda v: dict(v, delivery_verified=True),
                     lambda v: dict(v, route_id='NOT-A-SHA'),
                     lambda v: dict(v, status='delivered'),
                     lambda v: dict(v, found=True),
                     lambda v: dict(v, extra='DO_NOT_EXPOSE')]
        for index, mutation in enumerate(mutations):
            identity = 'invalid-contract-' + str(index)
            self.enqueue(identity)
            self.transform = lambda path, value, change=mutation: change(value)
            result = self.relay.poll_once()
            self.assertEqual('unknown', result['state'])
            self.assertIn(result['error'], ('relay_protocol_invalid', 'relay_receipt_identity_mismatch'))
            # Remove fixture row so a later subcase is not a backoff candidate.
            with self.local.transaction() as db:
                db.execute("UPDATE notification_relays SET state='accepted' WHERE request_id=?", (identity,))
        self.assertEqual(0, self.post_count)
        self.assertEqual([], self.remote_rows())

    def test_missing_protocol_route_owner_channel_auth_is_not_ready_to_post(self):
        self.enqueue()
        for code, category in [(404, 'relay_protocol_unavailable'),
                               (401, 'relay_authentication_rejected'),
                               (403, 'relay_authorization_rejected')]:
            self.deny = code
            self.due()
            result = self.relay.poll_once()
            self.assertEqual(category, result['error'])
            self.assertNotIn('DO_NOT_EXPOSE', json.dumps(result))
            self.assertEqual(category, self.row()['error'])
        self.assertEqual(0, self.post_count)
        self.assertEqual([], self.remote_rows())

    def test_remote_content_conflict_does_not_generate_new_id(self):
        self.enqueue()
        self.notifications.receive(SOURCE, {'request_id': 'explicit-notice', 'text': 'different fixture text',
            'fingerprint': hashlib.sha256('different fixture text'.encode()).hexdigest(), 'route_id': self.route})
        result = self.relay.poll_once()
        self.assertEqual('relay_remote_conflict', result['error'])
        self.assertEqual(0, self.post_count)
        self.assertEqual(1, len(self.remote_rows()))
        self.assertEqual('explicit-notice', self.calls[0][1]['request_id'])

    def test_rejected_waiting_auth_and_remote_submitting_remain_query_only(self):
        self.enqueue()
        self.relay.poll_once()
        for remote, local in [('rejected', 'rejected'), ('waiting_auth', 'waiting_auth'),
                              ('submitting', 'queued')]:
            self.remote_state(remote)
            self.due()
            self.assertEqual(local, self.relay.poll_once()['state'])
        self.assertEqual(1, self.post_count)

    def test_round_robin_updated_order_prevents_pending_from_starving_new_notices(self):
        self.enqueue('first')
        self.enqueue('second')
        self.assertEqual('first', self.relay.poll_once()['request_id'])
        self.assertEqual('second', self.relay.poll_once()['request_id'])
        self.due()
        self.assertEqual('first', self.relay.poll_once()['request_id'])
        self.assertEqual('second', self.relay.poll_once()['request_id'])
        self.assertEqual(2, self.post_count)

    def test_backoff_is_durable_bounded_and_not_reset_by_restart(self):
        self.enqueue()
        self.deny = 403
        for attempt in range(1, 9):
            result = self.relay.poll_once()
            self.assertEqual(attempt, result['attempt'])
            row = self.row()
            self.assertEqual(self.now + min(60, 2 ** min(attempt, 6)), row['lease_until'])
            reopened = Store(self.local.path, clock=lambda: self.now, notification_policy=POLICY, node_id=SOURCE)
            count = len(self.calls)
            self.assertEqual('idle', NotificationRelay(reopened, SOURCE, PEER, AUTHORITY, self.client).poll_once()['state'])
            self.assertEqual(count, len(self.calls))
            self.due()
        self.assertEqual(0, self.post_count)

    def test_live_flock_excludes_concurrent_poll_even_after_clock_lease_expires(self):
        self.enqueue()
        entered, release = threading.Event(), threading.Event()
        def before(path, body):
            if path == '/v1/mesh/notify/status':
                entered.set()
                self.assertTrue(release.wait(timeout=5))
        self.before = before
        results = []
        thread = threading.Thread(target=lambda: results.append(self.relay.poll_once()))
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            self.due()
            second = NotificationRelay(self.local, SOURCE, PEER, AUTHORITY, self.client)
            self.assertEqual('busy', second.poll_once()['state'])
            context = multiprocessing.get_context('spawn')
            output = context.Queue()
            child = context.Process(target=poll_in_child, args=(self.local.path, self.config, output))
            child.start()
            child.join(timeout=5)
            if child.is_alive():
                child.terminate()
                child.join(timeout=3)
                self.fail('fixture lock process did not terminate')
            self.assertEqual(0, child.exitcode)
            self.assertEqual('busy', output.get(timeout=2)['state'])
            output.close()
            output.join_thread()
            self.assertEqual(1, len(self.calls))
            self.assertEqual(1, self.row()['attempt'])
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual('queued', results[0]['state'])
        self.assertEqual(1, self.post_count)

    def test_stop_before_poll_does_not_claim_or_call_and_serve_is_interruptible(self):
        self.enqueue()
        stop = threading.Event()
        stop.set()
        self.assertEqual('stopped', self.relay.poll_once(stop=stop)['state'])
        self.assertEqual(0, self.relay.serve(stop=stop)['cycles'])
        self.assertEqual(0, self.row()['attempt'])
        self.assertEqual([], self.calls)

    def test_stop_after_status_prevents_post_but_retains_pinned_owner_route(self):
        self.enqueue()
        stop = threading.Event()
        self.after = lambda path, body, value: stop.set()
        self.assertEqual('stopped', self.relay.poll_once(stop=stop)['state'])
        self.assertEqual(0, self.post_count)
        self.assertEqual('pending', self.row()['state'])
        self.assertEqual(0, self.row()['post_attempted'])
        self.assertEqual(self.route, json.loads(self.row()['receipt'])['route_id'])
        self.after = None
        self.assertEqual('queued', self.relay.poll_once()['state'])
        self.assertEqual(1, self.post_count)

    def test_stop_during_notify_still_persists_actual_remote_receipt_no_new_calls(self):
        self.enqueue()
        stop = threading.Event()
        def after(path, body, value):
            if path == '/v1/mesh/notify':
                stop.set()
        self.after = after
        self.assertEqual('queued', self.relay.poll_once(stop=stop)['state'])
        self.assertTrue(json.loads(self.row()['receipt'])['found'])
        self.assertEqual(1, self.post_count)
        calls = len(self.calls)
        self.assertEqual('stopped', self.relay.poll_once(stop=stop)['state'])
        self.assertEqual(calls, len(self.calls))

    def test_stop_after_committed_post_intent_never_retries_even_if_no_post_occurred(self):
        self.enqueue()
        stop = threading.Event()
        original_write = self.relay._write
        def write(*args, **kwargs):
            original_write(*args, **kwargs)
            if kwargs.get('mark_post_attempted'):
                stop.set()
        self.relay._write = write
        result = self.relay.poll_once(stop=stop)
        self.assertEqual('stopped', result['state'])
        self.assertEqual('relay_submission_unconfirmed', result['error'])
        self.assertEqual('unknown', self.row()['state'])
        self.assertEqual(1, self.row()['post_attempted'])
        self.assertEqual(0, self.post_count)
        self.assertEqual(self.route, json.loads(self.row()['receipt'])['route_id'])
        self.due()
        reopened = Store(self.local.path, clock=lambda: self.now,
                         notification_policy=POLICY, node_id=SOURCE)
        relay = NotificationRelay(reopened, SOURCE, PEER, AUTHORITY, self.client)
        self.assertEqual('relay_submission_unconfirmed', relay.poll_once()['error'])
        self.assertEqual(0, self.post_count)
        self.assertEqual([], self.remote_rows())

    def test_only_explicit_new_intents_are_eligible_not_history_or_task_results(self):
        with self.local.transaction() as db:
            self.local._enqueue(db, 'historical-channel', 'old fixture', delivery_route='channel')
            self.local._enqueue(db, 'automatic-result', 'automatic fixture result')
        self.assertEqual('idle', self.relay.poll_once()['state'])
        self.assertEqual([], self.calls)
        self.enqueue()
        self.assertEqual('queued', self.relay.poll_once()['state'])
        self.assertEqual(1, len(self.remote_rows()))

    def test_corrupt_local_receipt_cannot_authorize_post(self):
        self.enqueue()
        with self.local.transaction() as db:
            db.execute("UPDATE notification_relays SET receipt='{}'")
        self.assertEqual('relay_local_receipt_invalid', self.relay.poll_once()['error'])
        self.assertEqual([], self.calls)
        self.assertEqual('{}', self.row()['receipt'])

    def test_injected_client_exception_never_persists_private_text(self):
        self.enqueue()
        def request(path, body=None):
            raise NotificationRelayError('DO_NOT_EXPOSE private token URL or body')
        self.client.request = request
        result = self.relay.poll_once()
        self.assertEqual('relay_protocol_invalid', result['error'])
        self.assertNotIn('DO_NOT_EXPOSE', json.dumps(result))
        self.assertEqual('relay_protocol_invalid', self.row()['error'])
        self.assertEqual([], self.remote_rows())

    def test_invalid_persistent_post_intent_flag_fails_closed_before_network(self):
        for index, flag in enumerate([-1, 2, 'invalid']):
            identity = 'invalid-post-marker-' + str(index)
            self.enqueue(identity)
            with self.local.transaction() as db:
                db.execute('UPDATE notification_relays SET post_attempted=? WHERE request_id=?', (flag, identity))
            report = self.relay.poll_once()
            self.assertEqual('relay_local_intent_invalid', report['error'])
            self.assertFalse(report['recoverable'])
            with self.local.transaction() as db:
                db.execute("UPDATE notification_relays SET state='accepted' WHERE request_id=?", (identity,))
        self.assertEqual([], self.calls)

    def test_private_lock_mode_and_symlink_are_rejected_without_network(self):
        self.enqueue()
        lock = self.relay.lock_path
        lock.write_text('fixture lock only')
        lock.chmod(0o644)
        self.assertEqual('relay_lock_not_private', self.relay.poll_once()['error'])
        self.assertEqual(0o644, lock.stat().st_mode & 0o777)
        lock.unlink()
        lock.symlink_to(self.token)
        self.assertEqual('relay_lock_not_private', self.relay.poll_once()['error'])
        self.assertEqual(TOKEN, self.token.read_text())
        self.assertEqual([], self.calls)

    def test_serve_cycle_report_and_validation(self):
        self.enqueue()
        reports = []
        result = self.relay.serve(max_cycles=1, on_report=reports.append, poll_interval_seconds=.01)
        self.assertEqual(1, result['cycles'])
        self.assertEqual('queued', reports[0]['state'])
        self.assertFalse(result['native_tools_intercepted'])
        for value in [True, 0, -1, float('nan'), 61]:
            with self.assertRaises(NotificationRelayError):
                self.relay.serve(max_cycles=1, poll_interval_seconds=value)
        for value in [True, 0, -1, 1.5]:
            with self.assertRaises(NotificationRelayError):
                self.relay.serve(max_cycles=value)


if __name__ == '__main__':
    unittest.main()
