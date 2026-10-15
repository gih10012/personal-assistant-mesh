"""Real loopback relay; synthetic owner intent, no phone/Cloud/production.

The reference authority is Python/SQLite, not a runtime requirement on a phone.
The dummy adapter is never executable or invoked. All HTTP tests explicitly
model a trusted loopback TLS proxy; plaintext is otherwise rejected.
"""
import base64
import copy
import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

from assistant_mesh.enrollment import (EnrollmentError, EnrollmentJournal,
                                      document_sha256, result_envelope)
from assistant_mesh.enrollment_bootstrap import EnrollmentBootstrap
from assistant_mesh.enrollment_relay import (EnrollmentRelay, OWNER_FORMAT,
    REQUEST_FORMAT, RESPONSE_FORMAT, make_http_handler)


TOKEN = 'SYNTHETIC_ENROLLMENT_ONLY_' + 'a' * 48
RESOURCE = 'https://mesh.example.test/enrollment/node'


def owned(path, value):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode('utf8')
    path.write_bytes(raw)
    path.chmod(0o600)
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def fixture(directory):
    now = int(time.time())
    document = {'format': 'mesh-enrollment/1', 'mesh_id': 'synthetic-mesh',
        'node_id': 'synthetic-phone', 'owner_binding': 'synthetic-owner',
        'repo_pin': {'repository': 'https://github.com/example/mesh', 'commit': 'a' * 40},
        'runtime': {'kind': 'interactive', 'expires_at': None, 'state_ref': 'private:phone-state'},
        'join': {'peer_id': 'synthetic-peer', 'authority_id': 'synthetic-authority',
                 'route_ref': 'private:original-join-route', 'credential_ref': 'private:scoped-token',
                 'trust_ref': 'private:owner-root'},
        'continuation': {'project_id': '合成手机入网', 'session_ref': 'native:original-specialist',
                         'state_ref': 'private:original-checkpoint'}}
    payload = bytes(range(256)) * 200
    # Bootstrap's existing owner recipe is retained, but the remote executor
    # does not need this POSIX adapter, Node, Python, or this journal.
    adapter = owned(directory / 'never-invoked.adapter', b'SYNTHETIC_NOT_EXECUTABLE')
    config = {'schema': 1, 'enabled': True, 'authority_kind': 'synthetic',
        'state_dir': str(directory / 'state'), 'enrollment': document,
        'authorization': {'enrollment_sha256': document_sha256(document), 'valid_until': now + 3600,
                          'capabilities': [{'id': 'synthetic-phone:bytes-v1',
                              'actions': ['challenge.bytes.repeat'], 'source_device': None,
                              'proxy_binding': None}]}, 'verifier_id': 'separate-owner-byte-oracle',
        'adapter': {'argv': [adapter['path']], 'pins': [adapter], 'timeout_seconds': 1},
        'challenge': {'operation_id': 'original-enrollment-operation',
            'capability_id': 'synthetic-phone:bytes-v1', 'action': 'challenge.bytes.repeat',
            'revision': 1, 'ttl_seconds': 300, 'input': owned(directory / 'input.bin', payload),
            'oracle': owned(directory / 'oracle.bin', payload * 3), 'proxy': None},
        'inventory': {'commands': [], 'network_metadata': False,
            'harness': {'name': 'native-phone-agent', 'version': 'not-observed', 'command': None},
            'candidates': []}}
    path = directory / 'bootstrap-owner.json'
    owned(path, config)
    bootstrap = EnrollmentBootstrap(path)
    bootstrap.bootstrap()
    challenge = bootstrap.challenge('original-enrollment-operation')
    binding = {key: challenge[key] for key in ('mesh_id', 'node_id', 'owner_binding',
                                              'operation_id', 'nonce', 'expires_at')}
    binding.update(enrollment_sha256=document_sha256(document),
        request_sha256=document_sha256(challenge), task_id='original-mesh-task',
        task_epoch=1, entry_request_id='original-owner-message')
    relay_config = {'format': OWNER_FORMAT, 'enabled': True, 'resource_url': RESOURCE,
        'token_sha256': hashlib.sha256(TOKEN.encode('ascii')).hexdigest(),
        'binding': binding, 'max_request_bytes': 1024 * 1024}
    relay_path = directory / 'relay-owner.json'
    owned(relay_path, relay_config)
    return bootstrap, config, path, challenge, relay_config, relay_path, payload


class EnrollmentRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-enrollment-relay.')
        self.directory = Path(self.temp.name)
        self.directory.chmod(0o700)
        (self.bootstrap, self.bootstrap_config, self.bootstrap_path, self.challenge,
         self.config, self.config_path, self.payload) = fixture(self.directory)
        self.validated = []
        self.relay = self.build()
        self.servers = []
        self.server = self.listen(self.relay)

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.temp.cleanup()

    def validate(self, binding):
        self.validated.append(copy.deepcopy(binding))
        return binding == self.config['binding']

    def build(self, **kwargs):
        kwargs.setdefault('live_task_validator', self.validate)
        return EnrollmentRelay(self.bootstrap, self.config_path, **kwargs)

    def listen(self, relay, peers=('127.0.0.1',)):
        server = HTTPServer(('127.0.0.1', 0), make_http_handler(relay, trusted_proxy_peers=peers))
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server

    def envelope(self, action='challenge', arguments=None):
        return {'format': REQUEST_FORMAT, 'binding': copy.deepcopy(self.config['binding']),
                'action': action, 'arguments': {} if arguments is None else arguments}

    def post(self, envelope=None, *, raw=None, headers=None, path='/enrollment/node', method='POST', server=None):
        raw = raw if raw is not None else json.dumps(envelope or self.envelope()).encode('utf8')
        supplied = {'Host': 'mesh.example.test', 'X-Forwarded-Proto': 'https',
            'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json',
            'Content-Length': str(len(raw))}
        supplied.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', (server or self.server).server_port, timeout=5)
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in supplied.items():
            if value is not None:
                for item in value if isinstance(value, list) else [value]:
                    connection.putheader(name, item)
        connection.endheaders(raw)
        try:
            reply = connection.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            connection.close()

    def state(self):
        return self.bootstrap.status(self.challenge['operation_id'])['state']

    def result(self):
        return result_envelope(self.challenge, self.payload * 3)

    def test_full_original_result_real_http_no_verification_registration_or_adapter(self):
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no adapter process')):
            code, admitted = self.post()
            self.assertEqual(200, code)
            self.assertEqual(RESPONSE_FORMAT, admitted['format'])
            self.assertTrue(admitted['may_execute_original_challenge'])
            self.assertEqual(self.challenge, admitted['challenge'])
            self.assertEqual(self.config['binding'], self.validated[0])
            code, reported = self.post(self.envelope('result', self.result()))
        self.assertEqual(200, code)
        self.assertEqual('result_reported', self.state())
        self.assertEqual('provider_reported', reported['status']['verification'])
        self.assertEqual('not_registered', reported['status']['registration_state'])
        for key in ('managed_use_authorized', 'native_execution_attested', 'automatic_replay',
                    'relay_performed_verification', 'relay_performed_registration', 'native_features_restricted'):
            self.assertFalse(reported[key])
        receipt = self.bootstrap.receipt(self.challenge['operation_id'])
        self.assertEqual(153600, receipt['result']['result']['bytes'])
        self.assertEqual(self.payload * 3, base64.b64decode(receipt['result']['result']['base64']))
        self.assertIsNone(receipt['verification'])
        self.assertIsNone(receipt['registration'])
        with sqlite3.connect('file:' + str(self.directory / 'state/enrollment.sqlite') + '?mode=ro', uri=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual({'enrollment_metadata', 'enrollment_reports', 'enrollment_operations'}, tables)
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM enrollment_operations').fetchone()[0])

    def test_lost_admission_ack_reopen_preserves_only_nonexecutable_original_challenge(self):
        # Discard the first response, exactly the case where device execution
        # cannot be inferred from delivery. Reopen does not reset attempted.
        self.post()
        reopened = self.build(live_task_validator=None)
        original = reopened.handle(TOKEN, self.envelope())
        self.assertEqual('attempted', original['status']['state'])
        self.assertEqual(self.challenge, original['challenge'])
        self.assertFalse(original['may_execute_original_challenge'])
        self.assertFalse(original['automatic_replay'])
        self.assertEqual(1, len(self.validated))

    def test_concurrent_admissions_have_one_durable_executable_grant(self):
        barrier = threading.Barrier(2)
        def execute_once():
            barrier.wait(5)
            return self.relay.handle(TOKEN, self.envelope())
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: execute_once(), range(2)))
        self.assertEqual([False, True], sorted(reply['may_execute_original_challenge'] for reply in replies))
        self.assertEqual('attempted', self.state())
        self.assertTrue(all(reply['challenge'] == self.challenge for reply in replies))

    def test_unknown_original_execution_is_observed_not_reexecuted(self):
        def disconnected(request):
            raise OSError('synthetic ACK loss after possible remote effect')
        self.bootstrap._journal().execute(self.challenge['operation_id'], disconnected)
        code, observed = self.post()
        self.assertEqual(200, code)
        self.assertEqual('unknown', observed['status']['state'])
        self.assertFalse(observed['may_execute_original_challenge'])
        self.assertEqual([], self.validated)
        self.assertEqual(200, self.post(self.envelope('result', self.result()))[0])
        self.assertEqual('result_reported', self.state())

    def test_live_validator_required_and_must_observe_original_task_epoch_request(self):
        for validator in (None, lambda binding: False, lambda binding: {'approved': True}, lambda binding: 1):
            with self.subTest(validator=validator):
                isolated = self.build(live_task_validator=validator)
                with self.assertRaises(PermissionError):
                    isolated.handle(TOKEN, self.envelope())
                self.assertEqual('intent', self.state())
                self.assertEqual('intent', isolated.handle(TOKEN, self.envelope('status'))['status']['state'])
        self.post()
        denied = self.build(live_task_validator=lambda binding: binding['task_epoch'] == 2)
        with self.assertRaises(PermissionError):
            denied.handle(TOKEN, self.envelope('result', self.result()))
        self.assertEqual('attempted', self.state())

    def test_binding_mutations_and_boolean_integer_aliases_never_admit(self):
        for key, value in [('mesh_id', 'other-mesh'), ('node_id', 'other-node'),
            ('owner_binding', 'other-owner'), ('enrollment_sha256', 'b' * 64),
            ('operation_id', 'replacement-operation'), ('request_sha256', 'b' * 64),
            ('nonce', 'b' * 64), ('expires_at', self.challenge['expires_at'] + 1),
            ('task_id', 'replacement-task'), ('task_epoch', 2), ('task_epoch', True),
            ('entry_request_id', 'replacement-message')]:
            changed = self.envelope()
            changed['binding'][key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(409, self.post(changed)[0])
                self.assertEqual('intent', self.state())
        self.assertEqual([], self.validated)

    def test_unadmitted_or_malformed_or_wrong_original_result_is_not_saved(self):
        self.assertEqual(409, self.post(self.envelope('result', self.result()))[0])
        self.post()
        for key, value in [('operation_id', 'new-operation'), ('nonce', 'b' * 64),
                           ('request_sha256', 'b' * 64), ('continuation', {}), ('proxy', {})]:
            result = self.result()
            result[key] = value
            with self.subTest(key=key):
                self.assertEqual(409, self.post(self.envelope('result', result))[0])
        malformed = self.result()
        malformed['result']['bytes'] -= 1
        self.assertEqual(409, self.post(self.envelope('result', malformed))[0])
        self.assertEqual('attempted', self.state())
        self.assertIsNone(self.bootstrap.receipt(self.challenge['operation_id'])['result'])

    def test_same_full_result_idempotent_after_disable_expiry_no_new_validation(self):
        self.post()
        original = self.result()
        self.assertEqual(200, self.post(self.envelope('result', original))[0])
        self.config['enabled'] = False
        owned(self.config_path, self.config)
        self.bootstrap_config['enabled'] = False
        owned(self.bootstrap_path, self.bootstrap_config)
        offline = self.build(live_task_validator=None, clock=lambda: self.challenge['expires_at'] + 1)
        self.assertEqual('result_reported', offline.handle(TOKEN, self.envelope('status'))['status']['state'])
        self.assertFalse(offline.handle(TOKEN, self.envelope())['may_execute_original_challenge'])
        saved = offline.handle(TOKEN, self.envelope('result', original))
        self.assertTrue(saved['identical_existing_result'])
        self.assertEqual(2, len(self.validated))
        conflict = result_envelope(self.challenge, b'not the original terminal bytes')
        with self.assertRaisesRegex(EnrollmentError, 'original_result_conflict'):
            offline.handle(TOKEN, self.envelope('result', conflict))

    def test_expired_disabled_or_invalid_clock_refuses_new_write_preserves_read(self):
        for clock in (lambda: self.challenge['expires_at'], lambda: float('nan'),
                      lambda: float('-inf'), lambda: True, lambda: self.challenge['created_at'] - 1):
            isolated = self.build(clock=clock)
            with self.assertRaisesRegex(EnrollmentError, 'challenge_expired'):
                isolated.handle(TOKEN, self.envelope())
            self.assertEqual('intent', isolated.handle(TOKEN, self.envelope('status'))['status']['state'])
        self.config['enabled'] = False
        owned(self.config_path, self.config)
        self.assertEqual(409, self.post()[0])
        self.assertEqual(200, self.post(self.envelope('status'))[0])
        self.config['enabled'] = True
        owned(self.config_path, self.config)
        self.post()
        expired = self.build(clock=lambda: self.challenge['expires_at'])
        with self.assertRaisesRegex(EnrollmentError, 'challenge_expired'):
            expired.handle(TOKEN, self.envelope('result', self.result()))
        self.assertEqual('attempted', self.state())

    def test_first_result_checks_original_journal_lifetime_inside_transaction(self):
        self.post()
        journal = EnrollmentJournal(self.bootstrap.root, self.bootstrap.document,
            self.bootstrap.config['authorization'], self.bootstrap.config['verifier_id'],
            initialize=False, clock=lambda: self.challenge['expires_at'] + 1)
        with self.assertRaisesRegex(EnrollmentError, 'expired'):
            journal.record_result(self.challenge['operation_id'], self.result(), require_live=True)
        self.assertEqual('attempted', self.state())
        # Preserve existing native semantics: a late original observation is
        # still recordable locally, without creating a new remote grant.
        journal.record_result(self.challenge['operation_id'], self.result())
        self.assertEqual('result_reported', self.state())
        journal.record_result(self.challenge['operation_id'], self.result(), require_live=True)

    def test_factory_and_status_are_readonly_missing_original_journal_never_recreated(self):
        database = self.directory / 'state/enrollment.sqlite'
        before = (database.read_bytes(), database.stat().st_ino, database.stat().st_mtime_ns)
        self.build(live_task_validator=None)
        make_http_handler(self.relay)
        for _ in range(2):
            self.post(self.envelope('status'))
        self.assertEqual(before, (database.read_bytes(), database.stat().st_ino, database.stat().st_mtime_ns))
        database.unlink()
        with self.assertRaises(EnrollmentError):
            self.build()
        self.assertFalse(database.exists())

    def test_bootstrap_owner_disable_is_not_overridden_by_relay_owner_enable(self):
        self.bootstrap_config['enabled'] = False
        owned(self.bootstrap_path, self.bootstrap_config)
        self.assertEqual(409, self.post()[0])
        code, status = self.post(self.envelope('status'))
        self.assertEqual(200, code)
        self.assertFalse(status['status']['owner_enabled'])
        self.assertEqual('intent', self.state())
        self.assertEqual([], self.validated)

    def test_owner_file_rebinding_or_insecure_permissions_cannot_move_the_operation(self):
        for key, value in [('resource_url', 'https://mesh.example.test/cloud/node'),
                           ('token_sha256', 'b' * 64), ('max_request_bytes', 2 * 1024 * 1024)]:
            changed = copy.deepcopy(self.config)
            changed[key] = value
            owned(self.config_path, changed)
            self.assertEqual(409, self.post()[0])
            self.assertEqual('intent', self.state())
        owned(self.config_path, self.config)
        self.config_path.chmod(0o644)
        self.assertEqual(409, self.post()[0])
        self.assertEqual('intent', self.state())

    def test_http_authority_exact_route_body_and_header_boundary_before_admission(self):
        for headers, expected in [({'Authorization': None}, 401),
            ({'Authorization': 'Bearer ' + TOKEN + 'wrong'}, 403),
            ({'Host': 'attacker.test'}, 400), ({'Host': ['mesh.example.test'] * 2}, 400),
            ({'X-Forwarded-Proto': 'http'}, 403), ({'X-Forwarded-Proto': ['https'] * 2}, 403),
            ({'Origin': 'https://attacker.test'}, 400), ({'Cookie': 'unrelated=secret'}, 400),
            ({'Content-Length': str(2 * 1024 * 1024)}, 413),
            ({'Transfer-Encoding': 'chunked'}, 400), ({'Content-Type': 'text/plain'}, 415)]:
            with self.subTest(headers=headers):
                status, value = self.post(headers=headers)
                self.assertEqual(expected, status)
                self.assertNotIn(TOKEN, json.dumps(value))
        for path in ('/v1/tasks', '/cloud/node', '/enrollment/node/', '/enrollment/node?op=x'):
            self.assertEqual(404, self.post(path=path)[0])
        self.assertEqual(405, self.post(method='GET')[0])
        for raw in (b'{"action":"challenge","action":"result"}', b'[]', b'{"x":NaN}'):
            self.assertEqual(400, self.post(raw=raw)[0])
        self.assertEqual('intent', self.state())
        self.assertEqual([], self.validated)

    def test_plain_http_is_not_accepted_from_a_nonconfigured_proxy(self):
        plain = self.listen(self.relay, ())
        self.assertEqual(403, self.post(server=plain)[0])
        self.assertEqual('intent', self.state())

    def test_extra_fields_or_remote_verify_register_commands_are_not_protocol(self):
        for action in ('verify', 'register', 'run', 'bootstrap', 'create', 'call'):
            self.assertEqual(409, self.post(self.envelope(action))[0])
        envelope = self.envelope()
        envelope['approved'] = True
        self.assertEqual(409, self.post(envelope)[0])
        envelope = self.envelope()
        envelope['arguments'] = {'authorized': True}
        self.assertEqual(409, self.post(envelope)[0])
        self.assertEqual('intent', self.state())


if __name__ == '__main__':
    unittest.main()
