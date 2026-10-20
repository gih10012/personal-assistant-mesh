"""Bounded local event-ledger/transport checks; no external callback or model."""
import base64
import json
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from assistant_mesh import mcp_event_transport as wire
from assistant_mesh.mcp_events import MCPEvents, EventDeliveryLoop, completion_receipt_source
from assistant_mesh.store import Conflict


SECRET = 'whsec_' + base64.b64encode(bytes(range(32))).decode()
ROTATED = 'whsec_' + base64.b64encode(bytes(range(32, 64))).decode()
URL = 'https://callbacks.example.test/events/fixture'


def principal(subject='alice'):
    return {'owner_id': 'owner-fixture', 'source': 'chatgpt-chat', 'subject': subject, 'scopes': ['tasks:read']}


class Transport:
    def __init__(self):
        self.calls = []; self.status = 204; self.challenge_ok = True

    def post(self, url, body, headers):
        value = json.loads(body); self.calls.append((url, body, dict(headers)))
        if value.get('type') == 'verification':
            return 200, json.dumps({'challenge': value['challenge'] if self.challenge_ok else 'wrong'}).encode()
        return self.status, b''


class EventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-work-events.')
        self.addCleanup(self.temp.cleanup)
        self.clock = [1792454400.0]; self.allowed = True; self.transport = Transport()
        self.service = self.reopen()

    def access(self, peer, name, args):
        return (self.allowed and peer['subject'] in ('alice', 'bob')
                and (name is None or args['project_id'] in ('project-one', 'project-two')))

    def reopen(self):
        return MCPEvents(self.temp.name, 'owner-fixture', authorize=self.access,
                         transport=self.transport, clock=lambda: self.clock[0])

    def subscribe(self, **changes):
        values = dict(name='task.completed', arguments={'project_id': 'project-one'},
                      delivery={'mode': 'webhook', 'url': URL, 'secret': SECRET})
        values.update(changes)
        return self.service.subscribe(principal(), **values)

    def event(self, **changes):
        value = dict(event_id='evt-original-final', name='task.completed', owner_id='owner-fixture',
            source='chatgpt-chat', subject='alice', project_id='project-one', request_id='original-request',
            task_id='ingress-' + 'a' * 64, task_epoch=3, occurred_at='2026-10-20T00:00:00Z',
            message_id='mesh-emit-original-receipt')
        value.update(changes)
        return value

    def test_subscribe_refresh_restart_rotation_challenge_and_original_identity(self):
        first = self.subscribe(ttlMs=120000)
        self.assertEqual(first['id'], self.subscribe(ttlMs=180000)['id'])
        self.assertEqual(1, len(self.transport.calls))  # Cached only for this principal+URL+key.
        self.assertFalse(self.transport.calls[0][2]['webhook-id'].startswith('evt-'))
        self.service = self.reopen()
        self.assertEqual(first['id'], self.subscribe(delivery={'mode': 'webhook', 'url': URL, 'secret': ROTATED})['id'])
        self.assertEqual(2, len(self.transport.calls))
        self.assertNotEqual(self.transport.calls[0][2]['webhook-id'], self.transport.calls[1][2]['webhook-id'])
        self.service.record_task_event(**self.event())
        delivered = self.service.deliver_one()
        self.assertTrue(delivered['webhook_receipt_only']); self.assertFalse(delivered['chatgpt_resumed'])
        headers = self.transport.calls[-1][2]
        self.assertEqual(2, len(headers['webhook-signature'].split()))
        self.assertEqual(first['id'], headers['X-MCP-Subscription-Id'])
        self.assertEqual('evt-original-final', headers['webhook-id'])
        with self.assertRaises(Conflict):
            self.service.record_task_event(**self.event(task_epoch=4))

    def test_authorization_project_filter_expiry_revocation_and_cancel(self):
        sub = self.subscribe(ttlMs=60000)
        for changes in ({'subject': 'bob'}, {'project_id': 'project-two'}):
            self.service.record_task_event(**self.event(event_id='evt-' + next(iter(changes)), **changes))
        self.assertFalse(self.service.deliver_one()['attempted'])
        self.service.record_task_event(**self.event())
        self.allowed = False
        self.assertEqual('cancelled', self.service.deliver_one()['state'])
        self.assertEqual(1, len(self.transport.calls))
        self.allowed = True
        self.subscribe()
        self.service.record_task_event(**self.event(event_id='evt-after-refresh'))
        for unused in range(2):
            self.assertEqual({}, self.service.unsubscribe(principal(), name='task.completed',
                arguments={'project_id': 'project-one'}, delivery={'mode': 'webhook', 'url': URL}))
        self.assertFalse(self.service.deliver_one()['attempted'])
        self.subscribe(ttlMs=60000)
        self.service.record_task_event(**self.event(event_id='evt-expired'))
        self.clock[0] += 61
        self.assertEqual('cancelled', self.service.deliver_one()['state'])
        missing = self.service.record_task_event(**self.event(event_id='evt-no-project', project_id=None))
        self.assertEqual('missing_authorized_project', missing['reason']); self.assertTrue(missing['recorded'])
        self.assertIsNotNone(sub['refreshBefore'])
        # Acknowledged cancellation cannot race past an already-admitted send.
        self.subscribe(); self.service.record_task_event(**self.event(event_id='evt-inflight'))
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        original = self.transport.post
        def block(url, body, headers):
            entered.set(); release.wait(2)
            return original(url, body, headers)
        self.transport.post = block
        sending = threading.Thread(target=self.service.deliver_one); sending.start()
        try:
            self.assertTrue(entered.wait(2))
            def cancel():
                self.service.unsubscribe(principal(), name='task.completed', arguments={'project_id': 'project-one'},
                                         delivery={'mode': 'webhook', 'url': URL})
                cancelled.set()
            cancelling = threading.Thread(target=cancel); cancelling.start()
            self.assertFalse(cancelled.wait(0.03))
        finally:
            release.set(); sending.join(3)
        cancelling.join(3); self.assertTrue(cancelled.is_set())
        self.assertFalse(self.service.deliver_one()['attempted'])

    def test_finite_retry_stable_bytes_id_fresh_signature_and_no_410_413_retry(self):
        self.subscribe(); self.service.record_task_event(**self.event())
        self.transport.status = 503
        self.assertEqual('pending', self.service.deliver_one()['state'])
        original = self.transport.calls[-1]
        self.service = self.reopen(); self.clock[0] += 10; self.transport.status = 204
        self.assertEqual('accepted', self.service.deliver_one()['state'])
        retried = self.transport.calls[-1]
        self.assertEqual(original[:2], retried[:2])
        self.assertEqual(original[2]['webhook-id'], retried[2]['webhook-id'])
        self.assertNotEqual(original[2]['webhook-signature'], retried[2]['webhook-signature'])
        self.assertFalse(self.service.record_task_event(**self.event())['new_event'])
        self.assertFalse(self.service.deliver_one()['attempted'])
        for code in (413, 410):
            self.service.record_task_event(**self.event(event_id='evt-' + str(code)))
            self.transport.status = code
            self.assertEqual('failed', self.service.deliver_one()['state'])
            self.clock[0] += 301
            self.assertFalse(self.service.deliver_one()['attempted'])
        self.subscribe(); self.transport.status = 503
        self.service.record_task_event(**self.event(event_id='evt-bounded'))
        for expected in ('pending', 'pending', 'pending', 'pending', 'failed'):
            self.assertEqual(expected, self.service.deliver_one()['state']); self.clock[0] += 301
        self.assertFalse(self.service.deliver_one()['attempted'])

    def test_bad_verification_does_not_activate_and_receipt_loop_cursor_is_durable(self):
        self.transport.challenge_ok = False
        with self.assertRaises(wire.CallbackError):
            self.subscribe()
        self.transport.challenge_ok = True; self.subscribe()
        test = self
        class Finals:
            def completion_events(self, *, after, limit):
                test.assertEqual(20, limit)
                return {'events': [test.event()] if after == 0 else [], 'cursor': 1}
        source = completion_receipt_source(Finals())
        loop = EventDeliveryLoop(self.service, receipt_source=source)
        self.assertTrue(loop.step()['attempted'])
        self.service = self.reopen()
        loop = EventDeliveryLoop(self.service, receipt_source=source)
        self.assertFalse(loop.step()['attempted'])
        loop.start(); loop.close()
        self.assertFalse(loop.thread.is_alive())


class TransportTests(unittest.TestCase):
    def test_standard_signature_fixed_openssl_vector_and_exact_body(self):
        body = b'{"x":1}'
        header = wire.signed_headers('evt_fixture', 1792454400, body, 'sub_fixture', [SECRET])
        # Independently calculated with OpenSSL HMAC-SHA256, fixed 00..1f key.
        expected = bytes.fromhex('d8ced53f901fd203bae6a4072ea5ec7687fc61f0683a7b1aa56be4098e5ddfe4')
        self.assertEqual('v1,' + base64.b64encode(expected).decode(), header['webhook-signature'])
        self.assertNotEqual(header['webhook-signature'], wire.signed_headers('evt_fixture', 1792454400,
                                                                           body + b' ', 'sub_fixture', [SECRET])['webhook-signature'])
        for raw in ('', 'whsec_bad!', 'whsec_' + base64.b64encode(b'x' * 23).decode()):
            with self.assertRaises(ValueError):
                wire.signing_key(raw)

    def test_ssrf_dns_at_connect_tls_hostname_and_no_redirect(self):
        answer = lambda value: [(socket.AF_INET6 if ':' in value else socket.AF_INET,
                                  socket.SOCK_STREAM, 6, '', (value, 443))]
        for value in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:8.8.8.8', '64:ff9b::7f00:1'):
            with self.subTest(value=value), patch.object(wire.socket, 'getaddrinfo', return_value=answer(value)):
                with self.assertRaises(wire.CallbackError):
                    wire.public_addresses('callback.example.test')
        dialed, tls_names = [], []
        fake_socket = SimpleNamespace(settimeout=lambda value: None, connect=dialed.append, close=lambda: None,
                                      do_handshake=lambda: None)
        tls = SimpleNamespace(wrap_socket=lambda sock, server_hostname, do_handshake_on_connect: tls_names.append(server_hostname) or sock)
        connection = SimpleNamespace(host='callback.example.test', timeout=10, _context=tls)
        with patch.object(wire.socket, 'getaddrinfo', return_value=answer('8.8.8.8')), patch.object(wire.socket, 'socket', return_value=fake_socket):
            wire._PinnedHTTPSConnection.connect(connection)
        self.assertEqual([('8.8.8.8', 443)], dialed); self.assertEqual(['callback.example.test'], tls_names)
        requests = []
        response = SimpleNamespace(status=302, read=lambda limit: b'')
        fake = SimpleNamespace(request=lambda *a, **k: requests.append((a, k)), getresponse=lambda: response, close=lambda: None)
        with patch.object(wire, '_PinnedHTTPSConnection', return_value=fake):
            self.assertEqual(302, wire.WebhookTransport().post(URL, b'{}', {})[0])
        self.assertEqual(1, len(requests))
        for url in ('http://example.test/a', 'https://user:secret@example.test/a', 'https://example.test:8443/a'):
            with self.assertRaises(wire.CallbackError):
                wire.callback_url(url)


if __name__ == '__main__':
    unittest.main()
