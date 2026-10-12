"""Local isolated contract/reference tests; no real enrollment or model calls."""
import base64
import contextlib
import copy
import hashlib
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from assistant_mesh.enrollment import (
    CHALLENGE_FORMAT, ENROLLMENT_FORMAT, REGISTRATION_FORMAT, REPORT_FORMAT,
    EnrollmentError, EnrollmentJournal, document_sha256, result_envelope,
    validate_adaptation_report, validate_enrollment,
)


def document(kind='persistent'):
    return {'format': ENROLLMENT_FORMAT, 'mesh_id': 'owner-mesh-fixture',
            'node_id': 'adapted-node-fixture', 'owner_binding': 'owner-fixture',
            'repo_pin': {'repository': 'https://github.com/example/mesh', 'commit': 'a' * 40},
            'runtime': {'kind': kind, 'expires_at': 1100 if kind == 'temporary' else None,
                        'state_ref': 'mesh:private-state'},
            'join': {'peer_id': 'existing-peer', 'authority_id': 'existing-authority',
                     'route_ref': 'private:transport', 'credential_ref': 'private:credential',
                     'trust_ref': 'owner-authorized-root'},
            'continuation': {'project_id': 'mesh-self-maintenance',
                             'session_ref': 'native:continuous-specialist', 'state_ref': 'mesh:checkpoint'}}


def authorization(enrollment, proxy=False):
    return {'enrollment_sha256': document_sha256(enrollment), 'valid_until': 2000,
            'capabilities': [{'id': 'fixture-bytes', 'actions': ['challenge.bytes'],
                              'source_device': 'source-device-fixture' if proxy else None,
                              'proxy_binding': 'owner-approved-proxy' if proxy else None}]}


def report(system='Windows'):
    return {'format': REPORT_FORMAT, 'mesh_id': 'owner-mesh-fixture',
            'node_id': 'adapted-node-fixture',
            'system': {'name': system, 'version': 'fixture-only'},
            'harness': {'name': 'native-agent', 'version': 'unknown', 'available': True},
            'storage': {'mode': 'mesh', 'state_ref': 'private:remote-state'},
            'capabilities': [{'id': 'fixture-bytes', 'revision': 1, 'actions': ['challenge.bytes']}],
            'gaps': ['No local daemon; adapt using the current native terminal.'],
            'native_features_restricted': False}


def receipt(publication):
    return {'format': REGISTRATION_FORMAT, 'operation_id': publication['operation_id'],
            'authority_id': publication['authority_id'],
            'publication_sha256': document_sha256(publication), 'registry_ref': 'fixture:registry-record'}


class EnrollmentDocumentTests(unittest.TestCase):
    def test_three_lifetimes_and_opaque_non_posix_refs_are_valid_detached_documents(self):
        for kind in ('persistent', 'temporary', 'interactive'):
            source = document(kind)
            source['runtime']['state_ref'] = 'opaque:authority-owned-storage'
            source['join']['route_ref'] = 'native-web-callback-without-ssh'
            source['join']['credential_ref'] = 'Windows Credential Manager:Mesh'
            source['continuation']['session_ref'] = 'mobile-session-reference'
            with mock.patch('os.getuid', side_effect=AssertionError('POSIX must not be consulted')):
                validated = validate_enrollment(source)
            self.assertEqual(source, validated)
            validated['join']['route_ref'] = 'different'
            self.assertEqual('native-web-callback-without-ssh', source['join']['route_ref'])

    def test_temporary_expiration_required_but_pure_parser_does_not_claim_liveness(self):
        source = document('temporary')
        source['runtime']['expires_at'] = 1
        self.assertEqual(1, validate_enrollment(source)['runtime']['expires_at'])
        source['runtime']['expires_at'] = None
        with self.assertRaisesRegex(EnrollmentError, 'expiration_required'):
            validate_enrollment(source)

    def test_boolean_timestamp_unknown_fields_and_unpinned_source_rejected(self):
        for change in ('bool', 'extra', 'pin', 'unsafe_int', 'runtime'):
            source = document()
            if change == 'bool':
                source['runtime']['expires_at'] = True
            elif change == 'extra':
                source['owner_approved'] = True
            elif change == 'pin':
                source['repo_pin']['commit'] = 'main'
            elif change == 'unsafe_int':
                source['runtime']['expires_at'] = 2 ** 53
            else:
                source['runtime']['kind'] = 'linux-systemd-required'
            with self.assertRaises(EnrollmentError):
                validate_enrollment(source)

    def test_reports_for_harmony_windows_or_unknown_harness_are_only_declarations(self):
        for system in ('HarmonyOS', 'Windows', 'Android', 'an-unanticipated-system'):
            source = report(system)
            source['harness']['available'] = False
            source['storage'] = {'mode': 'none', 'state_ref': None}
            validated = validate_adaptation_report(source)
            self.assertEqual(source, validated)
            self.assertNotIn('verification', validated)
            self.assertNotIn('registered', validated)

    def test_report_cannot_self_verify_register_or_request_native_interception(self):
        for name, value in (('verified', True), ('registered', True),
                            ('native_features_restricted', True)):
            source = report()
            source[name] = value
            with self.assertRaises(EnrollmentError):
                validate_adaptation_report(source)

    def test_canonical_json_hash_order_and_import_do_not_change_environment(self):
        before = dict(os.environ)
        source = document()
        reordered = dict(reversed(list(source.items())))
        self.assertEqual(document_sha256(source), document_sha256(reordered))
        validate_enrollment(source)
        validate_adaptation_report(report())
        self.assertEqual(before, dict(os.environ))


class EnrollmentJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-enrollment.')
        self.root = Path(self.temp.name) / 'private'
        self.now = 1000
        self.document = document()
        self.authorization = authorization(self.document)
        self.journal = self.reopen()
        self.payload = b'fixture challenge bytes\x00\xff\n'
        self.oracle = self.payload * 3

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self, enrollment=None, owner=None, verifier='independent-owner-fixture', path=None):
        return EnrollmentJournal(path or self.root, enrollment or self.document,
                                 owner or self.authorization, verifier,
                                 clock=lambda: self.now)

    def intent(self, operation='join-original', **kwargs):
        return self.journal.intent(operation, 'fixture-bytes', 'challenge.bytes',
                                   self.payload, self.oracle, **kwargs)

    def execute(self, operation='join-original'):
        return self.journal.execute(operation, lambda request: result_envelope(request, self.oracle))

    def verify(self, operation='join-original'):
        self.intent(operation)
        self.execute(operation)
        return self.journal.verify(operation, self.oracle)

    def test_report_preserves_candidates_without_creating_operation_or_registration(self):
        result = self.journal.record_adaptation(report())
        self.assertEqual('declared', result['verification'])
        self.assertFalse(result['capabilities_registered'])
        with contextlib.closing(sqlite3.connect(str(self.journal.path))) as db, db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM enrollment_operations').fetchone()[0])
        changed = report()
        changed['node_id'] = 'another-node'
        with self.assertRaisesRegex(EnrollmentError, 'identity_mismatch'):
            self.journal.record_adaptation(changed)

    def test_original_intent_nonce_bytes_and_request_hash_survive_reopen(self):
        first = self.intent()
        self.assertEqual(CHALLENGE_FORMAT, first['format'])
        self.assertEqual(self.payload, base64.b64decode(first['input']['base64']))
        self.now += 3
        other = self.reopen()
        second = other.intent('join-original', 'fixture-bytes', 'challenge.bytes',
                              self.payload, self.oracle)
        self.assertEqual(first, second)
        self.assertEqual(document_sha256(first), other.inspect('join-original')['request_sha256'])
        with self.assertRaisesRegex(EnrollmentError, 'original_intent_conflict'):
            other.intent('join-original', 'fixture-bytes', 'challenge.bytes', b'changed', self.oracle)

    def test_unowned_candidate_or_action_is_not_promoted_and_native_remains_unrestricted(self):
        for capability, action in (('unowned', 'challenge.bytes'), ('fixture-bytes', 'remote.shell')):
            with self.assertRaisesRegex(EnrollmentError, 'not_authorized'):
                self.journal.intent('another', capability, action, self.payload, self.oracle)
        self.intent()
        self.assertFalse(self.journal.inspect('join-original')['native_features_restricted'])

    def test_isolated_actual_owned_process_returns_complete_artifact_verified_independently(self):
        # Actual Linux Python process is only the reference executor fixture;
        # it does not prove a Windows/Harmony node or model/tool execution.
        self.payload = bytes(range(256)) * 200
        self.oracle = self.payload * 3  # 153600 bytes, not a hash-only receipt.
        request = self.intent()
        def executor(challenge):
            process = subprocess.Popen([sys.executable, '-c',
                'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read() * 3)'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                output, error = process.communicate(input=base64.b64decode(challenge['input']['base64']), timeout=10)
            except BaseException:
                process.kill()
                process.communicate()
                raise
            self.assertEqual(0, process.returncode)
            self.assertEqual(b'', error)
            return result_envelope(challenge, output)
        reported = self.journal.execute('join-original', executor)
        self.assertEqual('provider_reported', reported['verification'])
        self.assertEqual('not_registered', reported['registration_state'])
        verified = self.journal.verify('join-original', self.oracle)
        self.assertEqual('challenge_bytes_verified', verified['verification'])
        self.assertEqual(153600, verified['artifact_bytes'])
        self.assertEqual(hashlib.sha256(self.oracle).hexdigest(), verified['artifact_sha256'])
        self.assertEqual(self.oracle, self.reopen().read_result('join-original'))
        self.assertFalse(verified['native_execution_attested'])
        self.assertEqual(document_sha256(request), verified['request_sha256'])

    def test_uncertain_callback_effect_never_runs_again_and_original_late_result_is_allowed(self):
        request = self.intent()
        effects = []
        def uncertain(challenge):
            effects.append(challenge['operation_id'])
            raise RuntimeError('PRIVATE_PATH/private-auth: reply lost after effect')
        result = self.journal.execute('join-original', uncertain)
        self.assertEqual('unknown', result['state'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.reopen().execute('join-original', uncertain)
        self.assertEqual(['join-original'], effects)
        late = result_envelope(request, self.oracle)
        self.journal.record_result('join-original', late)
        self.assertEqual(self.oracle, self.journal.read_result('join-original'))

    def test_crash_after_durable_intent_before_callback_is_not_reexecuted(self):
        self.intent()
        with contextlib.closing(sqlite3.connect(str(self.journal.path))) as db, db:
            db.execute("UPDATE enrollment_operations SET state='attempted' WHERE id='join-original'")
        called = []
        status = self.reopen().execute('join-original', lambda request: called.append(request))
        self.assertEqual('attempted', status['state'])
        self.assertEqual([], called)

    def test_result_fences_nonce_request_continuation_proxy_and_immutable_bytes(self):
        request = self.intent()
        with self.assertRaisesRegex(EnrollmentError, 'not_admitted'):
            self.journal.record_result('join-original', result_envelope(request, self.oracle))
        self.execute()
        original = result_envelope(request, self.oracle)
        for field, value in (('nonce', 'different'), ('request_sha256', 'b' * 64),
                             ('continuation', {'project_id': 'another'}), ('proxy', {'arbitrary': True})):
            changed = copy.deepcopy(original)
            changed[field] = value
            with self.assertRaisesRegex(EnrollmentError, 'binding_mismatch'):
                self.journal.record_result('join-original', changed)
        with self.assertRaisesRegex(EnrollmentError, 'original_result_conflict'):
            self.journal.record_result('join-original', result_envelope(request, b'changed'))
        self.assertEqual(self.oracle, self.journal.read_result('join-original'))

    def test_tampered_or_truncated_artifact_and_self_verified_result_are_not_accepted(self):
        request = self.intent()
        self.journal.execute('join-original', lambda unused: None)
        for modification in ('bytes', 'sha256', 'base64', 'self_verify'):
            changed = result_envelope(request, self.oracle)
            if modification == 'bytes':
                changed['result']['bytes'] += 1
            elif modification == 'sha256':
                changed['result']['sha256'] = 'c' * 64
            elif modification == 'base64':
                changed['result']['base64'] = changed['result']['base64'][:-4]
            else:
                changed['verified'] = True
            with self.assertRaises(EnrollmentError):
                self.journal.record_result('join-original', changed)

    def test_independent_oracle_cannot_be_swapped_to_match_wrong_executor_output(self):
        self.intent()
        self.journal.execute('join-original', lambda request: result_envelope(request, b'wrong'))
        with self.assertRaisesRegex(EnrollmentError, 'oracle_binding_mismatch'):
            self.journal.verify('join-original', b'wrong')
        with self.assertRaisesRegex(EnrollmentError, 'result_oracle_mismatch'):
            self.journal.verify('join-original', self.oracle)
        self.assertEqual('provider_reported', self.journal.inspect('join-original')['verification'])

    def test_verification_and_registration_are_explicit_separate_stages(self):
        self.intent()
        self.execute()
        with self.assertRaisesRegex(EnrollmentError, 'requires_verification'):
            self.journal.register('join-original', receipt)
        verified = self.journal.verify('join-original', self.oracle)
        self.assertEqual('not_registered', verified['registration_state'])
        registered = self.journal.register('join-original', receipt)
        self.assertEqual('registered', registered['registration_state'])
        self.assertEqual('trusted_registrar_receipt', registered['registration_evidence'])
        self.assertTrue(registered['registered_candidate_fresh'])
        self.assertFalse(registered['managed_use_authorized'])
        self.assertFalse(registered['source_control_granted'])

    def test_registration_ack_loss_and_readback_do_not_submit_again_even_when_missing(self):
        self.verify()
        attempts, saved = [], []
        def registrar(publication):
            attempts.append(publication['operation_id'])
            saved.append(receipt(publication))
            raise RuntimeError('PRIVATE_REGISTRY: ACK lost')
        self.assertEqual('unknown', self.journal.register('join-original', registrar)['registration_state'])
        self.reopen().register('join-original', registrar)
        with self.assertRaises(EnrollmentError):
            self.journal.reconcile_registration('join-original', lambda unused: None)
        self.journal.register('join-original', registrar)
        self.assertEqual(['join-original'], attempts)
        reconciled = self.journal.reconcile_registration('join-original', lambda unused: saved[0])
        self.assertEqual('registered', reconciled['registration_state'])

    def test_registration_receipt_wrong_authority_original_hash_or_identity_stays_unknown(self):
        for name, value in (('authority_id', 'another-authority'),
                            ('operation_id', 'other-operation'), ('publication_sha256', 'd' * 64)):
            operation = 'register-' + name
            self.verify(operation)
            def registrar(publication):
                result = receipt(publication)
                result[name] = value
                return result
            result = self.journal.register(operation, registrar)
            self.assertEqual('unknown', result['registration_state'])

    def test_temporary_expiry_blocks_new_effects_but_result_and_original_readback_survive(self):
        self.document = document('temporary')
        self.authorization = authorization(self.document)
        self.journal = self.reopen(path=Path(self.temp.name) / 'temporary')
        self.verify()
        saved = []
        def lost(publication):
            saved.append(receipt(publication))
            raise RuntimeError('lost')
        self.journal.register('join-original', lost)
        self.now = 3000
        self.assertEqual(self.oracle, self.journal.read_result('join-original'))
        with self.assertRaisesRegex(EnrollmentError, 'authorization_expired'):
            self.intent('new-operation')
        status = self.journal.reconcile_registration('join-original', lambda unused: saved[0])
        self.assertEqual('registered', status['registration_state'])
        self.assertFalse(status['registered_candidate_fresh'])

    def test_challenge_expiration_does_not_renew_old_nonce_or_trust_result_timestamp(self):
        first = self.intent(ttl_seconds=2)
        self.execute()
        self.now += 3
        same = self.intent(ttl_seconds=2)
        self.assertEqual(first, same)
        with self.assertRaisesRegex(EnrollmentError, 'challenge_expired'):
            self.journal.verify('join-original', self.oracle)
        self.assertEqual(self.oracle, self.journal.read_result('join-original'))

    def test_owner_repo_route_identity_policy_or_verifier_cannot_change_existing_journal(self):
        self.intent()
        for field in ('node_id', 'owner_binding', 'repo_pin', 'join'):
            changed = copy.deepcopy(self.document)
            if field == 'repo_pin':
                changed[field]['commit'] = 'b' * 40
            elif field == 'join':
                changed[field]['route_ref'] = 'different-transport'
            else:
                changed[field] = 'different'
            with self.assertRaisesRegex(EnrollmentError, 'journal_identity_mismatch'):
                self.reopen(enrollment=changed, owner=authorization(changed))
        widened = copy.deepcopy(self.authorization)
        widened['capabilities'][0]['actions'].append('remote.shell')
        with self.assertRaisesRegex(EnrollmentError, 'journal_identity_mismatch'):
            self.reopen(owner=widened)
        with self.assertRaisesRegex(EnrollmentError, 'journal_identity_mismatch'):
            self.reopen(verifier='different')

    def test_concurrent_original_operation_callbacks_are_admitted_at_most_once(self):
        self.intent()
        callbacks = []
        lock = threading.Lock()
        def executor(request):
            with lock:
                callbacks.append(request['operation_id'])
            return result_envelope(request, self.oracle)
        def run(unused):
            return self.reopen().execute('join-original', executor)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(run, range(8)))
        self.assertEqual(['join-original'], callbacks)
        self.assertEqual(self.oracle, self.journal.read_result('join-original'))

    def test_private_storage_permissions_symlinks_and_hardlinks_rejected(self):
        self.assertEqual(0o600, stat.S_IMODE(self.journal.path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
        self.root.chmod(0o755)
        with self.assertRaises(EnrollmentError):
            self.reopen()
        self.root.chmod(0o700)
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.root)
        with self.assertRaises(EnrollmentError):
            self.reopen(path=alias)
        linked = Path(self.temp.name) / 'linked.sqlite'
        os.link(str(self.journal.path), str(linked))
        with self.assertRaises(EnrollmentError):
            self.reopen()

    def test_unrelated_existing_database_schema_is_not_migrated(self):
        location = Path(self.temp.name) / 'unrelated'
        location.mkdir(mode=0o700)
        database = location / 'enrollment.sqlite'
        with contextlib.closing(sqlite3.connect(str(database))) as db, db:
            db.execute('CREATE TABLE owner_data(value TEXT)')
            db.execute("INSERT INTO owner_data VALUES('preserve-original')")
        database.chmod(0o600)
        with self.assertRaisesRegex(EnrollmentError, 'schema_invalid'):
            self.reopen(path=location)
        with contextlib.closing(sqlite3.connect(str(database))) as db, db:
            self.assertEqual(['owner_data'], [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")])
            self.assertEqual('preserve-original', db.execute('SELECT value FROM owner_data').fetchone()[0])

    def test_corrupted_saved_request_or_result_is_not_accepted_as_a_new_identity(self):
        self.intent()
        self.execute()
        with contextlib.closing(sqlite3.connect(str(self.journal.path))) as db, db:
            saved_request, saved_result = db.execute("SELECT request,result FROM enrollment_operations WHERE id='join-original'").fetchone()
            altered = json.loads(saved_request)
            altered['nonce'] = 'corrupted-nonce'
            db.execute("UPDATE enrollment_operations SET request=? WHERE id='join-original'", (json.dumps(altered),))
        with self.assertRaisesRegex(EnrollmentError, 'request_integrity_failed'):
            self.journal.inspect('join-original')
        with contextlib.closing(sqlite3.connect(str(self.journal.path))) as db, db:
            db.execute("UPDATE enrollment_operations SET request=?,result=? WHERE id='join-original'",
                       (saved_request, saved_result + ' '))
        with self.assertRaisesRegex(EnrollmentError, 'result_integrity_failed'):
            self.journal.read_result('join-original')


class ProxyEnrollmentTests(unittest.TestCase):
    reopen = EnrollmentJournalTests.reopen
    intent = EnrollmentJournalTests.intent
    tearDown = EnrollmentJournalTests.tearDown

    def setUp(self):
        EnrollmentJournalTests.setUp(self)
        self.authorization = authorization(self.document, proxy=True)
        self.journal = self.reopen(path=Path(self.temp.name) / 'proxy')
        self.proxy = {'source_device': 'source-device-fixture',
                      'proxy_binding': 'owner-approved-proxy', 'revision': 4, 'ttl_seconds': 40}

    def connection(self, request):
        return {'source_device': self.proxy['source_device'], 'proxy_binding': self.proxy['proxy_binding'],
                'revision': self.proxy['revision'], 'checked_at': self.now,
                'expires_at': request['expires_at'], 'method': 'actual_connection_challenge',
                'evidence_sha256': hashlib.sha256(b'fixture-only-connection-evidence').hexdigest()}

    def proxy_intent(self):
        return self.journal.intent('proxy-original', 'fixture-bytes', 'challenge.bytes',
                                   self.payload, self.oracle, proxy=self.proxy)

    def proxy_execute(self):
        self.proxy_intent()
        self.journal.execute('proxy-original', lambda request: result_envelope(request, self.oracle))

    def test_proxy_requires_exact_source_and_binding_without_new_device_permissions(self):
        with self.assertRaisesRegex(EnrollmentError, 'binding_required'):
            self.intent()
        changed = dict(self.proxy, source_device='a-device-the-owner-never-authorized')
        with self.assertRaisesRegex(EnrollmentError, 'proxy_not_authorized'):
            self.journal.intent('unowned', 'fixture-bytes', 'challenge.bytes',
                                self.payload, self.oracle, proxy=changed)
        self.proxy_execute()
        with self.assertRaisesRegex(EnrollmentError, 'observer_required'):
            self.journal.verify('proxy-original', self.oracle)
        verified = self.journal.verify('proxy-original', self.oracle, self.connection)
        self.assertTrue(verified['source_connection_verified'])
        self.assertFalse(verified['source_control_granted'])
        self.assertFalse(verified['managed_use_authorized'])

    def test_proxy_declared_ping_revision_or_expired_lease_is_not_connection_verification(self):
        self.proxy_execute()
        for name, value in (('method', 'observed_ping_only'), ('revision', 3),
                            ('source_device', 'other-device'), ('proxy_binding', 'other-holder'),
                            ('checked_at', self.now + 1), ('expires_at', self.now),
                            ('expires_at', self.now + 41)):
            def observer(request):
                evidence = self.connection(request)
                evidence[name] = value
                return evidence
            with self.assertRaises(EnrollmentError):
                self.journal.verify('proxy-original', self.oracle, observer)

    def test_proxy_ttl_and_revision_are_immutable_registration_never_implies_control(self):
        request = self.proxy_intent()
        self.assertEqual(1040, request['expires_at'])
        self.proxy_execute()
        self.journal.verify('proxy-original', self.oracle, self.connection)
        publications = []
        def registrar(publication):
            publications.append(publication)
            return receipt(publication)
        self.journal.register('proxy-original', registrar)
        self.assertEqual(self.proxy, publications[0]['proxy'])
        self.assertFalse(publications[0]['source_control_granted'])
        self.proxy['revision'] = 5
        with self.assertRaisesRegex(EnrollmentError, 'intent_conflict'):
            self.proxy_intent()
        self.now = 1040
        self.assertFalse(self.journal.inspect('proxy-original')['registered_candidate_fresh'])
        self.assertEqual(self.oracle, self.journal.read_result('proxy-original'))

    def test_publication_does_not_outlive_shorter_real_connection_evidence(self):
        self.proxy_execute()
        def shorter(request):
            evidence = self.connection(request)
            evidence['expires_at'] = self.now + 10
            return evidence
        self.journal.verify('proxy-original', self.oracle, shorter)
        published = []
        def registrar(publication):
            published.append(publication)
            return receipt(publication)
        self.journal.register('proxy-original', registrar)
        self.assertEqual(1010, published[0]['expires_at'])
        self.now = 1010
        self.assertFalse(self.journal.inspect('proxy-original')['registered_candidate_fresh'])

    def test_independent_actual_loopback_connection_challenge_not_remote_device_claim(self):
        # Independent server and two TCP roundtrips. This is a local fixture,
        # NOT proof that an owner NAS/phone/SSH host has been connected.
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        listener.settimeout(5)
        address = listener.getsockname()
        failures = []
        def serve():
            try:
                for unused in range(2):
                    connection, unused_address = listener.accept()
                    with connection:
                        connection.settimeout(5)
                        body = bytearray()
                        while True:
                            part = connection.recv(4096)
                            if not part:
                                break
                            body.extend(part)
                        connection.sendall(bytes(body) * 3)
            except Exception as exc:
                failures.append(type(exc).__name__)
            finally:
                listener.close()
        thread = threading.Thread(target=serve)
        thread.daemon = True
        thread.start()
        def roundtrip(raw):
            with socket.create_connection(address, timeout=5) as connection:
                connection.sendall(raw)
                connection.shutdown(socket.SHUT_WR)
                body = bytearray()
                while True:
                    part = connection.recv(4096)
                    if not part:
                        return bytes(body)
                    body.extend(part)
        self.proxy_intent()
        self.journal.execute('proxy-original', lambda request: result_envelope(request,
            roundtrip(base64.b64decode(request['input']['base64']))))
        def observer(request):
            challenge = bytes.fromhex(request['nonce'])
            actual = roundtrip(challenge)
            self.assertEqual(challenge * 3, actual)
            evidence = self.connection(request)
            evidence['evidence_sha256'] = hashlib.sha256(actual).hexdigest()
            return evidence
        try:
            verified = self.journal.verify('proxy-original', self.oracle, observer)
            self.assertTrue(verified['source_connection_verified'])
        finally:
            thread.join(timeout=6)
            listener.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual([], failures)


if __name__ == '__main__':
    unittest.main()
