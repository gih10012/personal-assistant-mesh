"""Risk boundaries and actual isolated adapter process, not a production node."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from assistant_mesh.enrollment import (EnrollmentError, EnrollmentJournal,
                                      document_sha256, result_envelope)
from assistant_mesh.enrollment_bootstrap import BootstrapError, EnrollmentBootstrap, main


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / 'deploy/enrollment-stdio-adapter.mjs'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def owned_file(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)
    return {'path': str(path), 'sha256': digest(path)}


def fixture(directory, adapter=None, argv=None, kind='interactive'):
    now = int(time.time())
    node = Path(shutil.which('node') or '/unavailable/node').resolve()
    script = adapter or ADAPTER
    document = {'format': 'mesh-enrollment/1', 'mesh_id': 'isolated-owner-mesh',
        'node_id': 'isolated-adapter', 'owner_binding': 'synthetic-owner-not-production',
        'repo_pin': {'repository': 'https://github.com/gih10012/personal-assistant-mesh', 'commit': 'a' * 40},
        'runtime': {'kind': kind, 'expires_at': now + 3600 if kind == 'temporary' else None,
                    'state_ref': 'private:existing-durable-authority-state'},
        'join': {'peer_id': 'isolated-authority', 'authority_id': 'synthetic-authority',
                 'route_ref': 'private:owned-stdio', 'credential_ref': 'private:no-secret-fixture',
                 'trust_ref': 'private:synthetic-owner-policy'},
        'continuation': {'project_id': '设备 bootstrap 🧪', 'session_ref': 'native:original-specialist',
                         'state_ref': 'mesh:original-checkpoint'}}
    payload = bytes(range(256)) * 200
    config = {'schema': 1, 'enabled': True, 'authority_kind': 'synthetic',
        'state_dir': str(directory / 'state'), 'enrollment': document,
        'authorization': {'enrollment_sha256': document_sha256(document), 'valid_until': now + 3600,
                          'capabilities': [{'id': 'byte-challenge', 'actions': ['challenge.bytes.repeat'],
                                            'source_device': None, 'proxy_binding': None}]},
        'verifier_id': 'separate-synthetic-byte-oracle',
        'adapter': {'argv': argv or [str(node), str(script), '--repeat', '3'],
                    'pins': [{'path': str(node), 'sha256': digest(node)},
                             {'path': str(script), 'sha256': digest(script)}], 'timeout_seconds': 10},
        'challenge': {'operation_id': 'original-bootstrap-challenge', 'capability_id': 'byte-challenge',
                      'action': 'challenge.bytes.repeat', 'revision': 1, 'ttl_seconds': 60,
                      'input': owned_file(directory / 'input.bin', payload),
                      'oracle': owned_file(directory / 'oracle.bin', payload * 3), 'proxy': None},
        'inventory': {'commands': ['node', 'git', 'no-such-fixture-command'], 'network_metadata': False,
                      'harness': {'name': 'native-agent-unknown', 'version': 'not-invoked', 'command': None},
                      'candidates': [{'id': 'byte-challenge', 'class': 'compute.bytes', 'revision': 1,
                          'actions': ['challenge.bytes.repeat'], 'scope': {'task': 'bootstrap-only'},
                          'resource_pool': 'one-owned-process', 'fault_domain': 'isolated-local-fixture',
                          'source_device': None, 'proxy_binding': None}]}}
    path = directory / 'owner.json'
    owned_file(path, json.dumps(config, ensure_ascii=False).encode('utf8'))
    return config, path, payload


@unittest.skipUnless(shutil.which('node'), 'Node reference adapter unavailable; not a protocol requirement')
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-bootstrap-test.')
        self.directory = Path(self.temp.name)
        self.directory.chmod(0o700)
        self.config, self.path, self.payload = fixture(self.directory)

    def tearDown(self):
        self.temp.cleanup()

    def tool(self, **kwargs):
        return EnrollmentBootstrap(self.path, **kwargs)

    def save(self):
        owned_file(self.path, json.dumps(self.config).encode('utf8'))

    def ready(self):
        tool = self.tool()
        tool.bootstrap()
        challenge = tool.challenge('original-bootstrap-challenge')
        return tool, challenge

    def test_plan_and_command_metadata_do_not_initialize_or_invoke_wrappers(self):
        tool = self.tool()
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no execution')):
            plan = tool.plan()
            inventory = tool.inventory()
        self.assertFalse(plan['initialized'])
        self.assertFalse(Path(self.config['state_dir']).exists())
        self.assertEqual('interactive', plan['enrollment']['runtime']['kind'])
        self.assertIsNone(plan['required_platform_or_language'])
        self.assertFalse(plan['join_transport_is_business_egress_grant'])
        self.assertEqual(['declared'], [row['verification'] for row in plan['candidate_instances']])
        self.assertFalse(inventory['adaptation_report']['harness']['available'])
        self.assertFalse(any(row['invocation_performed'] for row in inventory['commands']))
        self.assertFalse(plan['native_features_restricted'])

    def test_actual_independent_js_adapter_full_bytes_original_receipt_and_readonly_reopen(self):
        tool, challenge = self.ready()
        result = tool.run(challenge['operation_id'])
        self.assertEqual('result_reported', result['status']['state'])
        self.assertEqual(0, result['process_observation']['direct_process_returncode'])
        self.assertTrue(result['process_observation']['direct_process_reaped'])
        self.assertFalse(result['process_observation']['remote_effect_closure_verified'])
        verified = tool.verify(challenge['operation_id'])
        self.assertEqual(153600, verified['artifact_bytes'])
        self.assertEqual('challenge_bytes_verified', verified['verification'])
        self.assertEqual('not_registered', verified['registration_state'])
        path = Path(self.config['state_dir']) / 'enrollment.sqlite'
        before, metadata = path.read_bytes(), path.stat()
        cold = self.tool()
        full = cold.receipt(challenge['operation_id'])
        self.assertEqual(challenge, full['challenge'])
        self.assertEqual(document_sha256(challenge), full['result']['request_sha256'])
        self.assertEqual(self.payload * 3, base64.b64decode(full['result']['result']['base64']))
        self.assertEqual(153600, full['result']['result']['bytes'])
        self.assertEqual(challenge['continuation'], full['result']['continuation'])
        cold.status(challenge['operation_id'])
        cold.plan()
        self.assertEqual(before, path.read_bytes())
        self.assertEqual((metadata.st_ino, metadata.st_mtime_ns), (path.stat().st_ino, path.stat().st_mtime_ns))
        self.assertFalse(full['status']['native_execution_attested'])
        self.assertFalse(full['status']['managed_use_authorized'])

    def test_existing_result_observed_without_adapter_or_oracle_availability(self):
        tool, challenge = self.ready()
        tool.run(challenge['operation_id'])
        tool.verify(challenge['operation_id'])
        Path(self.config['challenge']['oracle']['path']).unlink()
        with mock.patch('assistant_mesh.enrollment_bootstrap._adapter_bytes', side_effect=AssertionError('not invoked')):
            cold = self.tool()
            self.assertEqual('verified', cold.run(challenge['operation_id'])['status']['state'])
            self.assertEqual(153600, cold.receipt(challenge['operation_id'])['result']['result']['bytes'])

    def test_owned_adapter_failure_never_replays_or_accepts_new_operation_identity(self):
        script = self.directory / 'failing.mjs'
        owned_file(script, b"import fs from 'node:fs'; fs.writeFileSync('effect-marker', 'once', {flag:'wx'}); process.exitCode=1;")
        self.config, self.path, self.payload = fixture(self.directory, adapter=script,
            argv=[str(Path(shutil.which('node')).resolve()), str(script)])
        tool, challenge = self.ready()
        first = tool.run(challenge['operation_id'])
        self.assertEqual('unknown', first['status']['state'])
        marker = Path(self.config['state_dir']) / 'effect-marker'
        inode = marker.stat().st_ino
        second = self.tool().run(challenge['operation_id'])
        self.assertEqual('unknown', second['status']['state'])
        self.assertIsNone(second['process_observation'])
        self.assertEqual(inode, marker.stat().st_ino)
        with self.assertRaisesRegex(BootstrapError, 'original_operation_required'):
            self.tool().challenge('new-id-would-replay')
        late = result_envelope(challenge, self.payload * 3)
        self.tool().record_result(challenge['operation_id'], late)
        self.assertEqual('result_reported', self.tool().status(challenge['operation_id'])['state'])

    def test_live_disable_stops_new_managed_actions_but_preserves_original_full_evidence(self):
        tool, challenge = self.ready()
        tool.run(challenge['operation_id'])
        tool.verify(challenge['operation_id'])
        self.config['enabled'] = False
        self.save()
        for action in ('run', 'challenge', 'verify'):
            with self.assertRaisesRegex(BootstrapError, 'owner_disabled'):
                getattr(tool, action)(challenge['operation_id'])
        self.assertFalse(tool.status(challenge['operation_id'])['owner_enabled'])
        self.assertEqual(self.payload * 3, base64.b64decode(tool.receipt(challenge['operation_id'])['result']['result']['base64']))

    def test_changed_recipe_cannot_rebind_existing_state_or_observe_as_new_identity(self):
        tool, challenge = self.ready()
        self.config['adapter']['argv'][-1] = '2'
        self.save()
        with self.assertRaisesRegex(BootstrapError, 'original_binding_changed'):
            self.tool().bootstrap()
        with self.assertRaisesRegex(BootstrapError, 'original_binding_changed'):
            self.tool().status(challenge['operation_id'])

    def test_missing_original_journal_is_not_reinitialized_into_a_new_challenge(self):
        tool, challenge = self.ready()
        path = Path(self.config['state_dir']) / 'enrollment.sqlite'
        path.unlink()
        for call in (lambda: tool.bootstrap(), lambda: tool.challenge(challenge['operation_id']),
                     lambda: tool.status(challenge['operation_id'])):
            with self.assertRaises(EnrollmentError):
                call()
            self.assertFalse(path.exists())

    def test_pre_admission_changed_pinned_source_or_oracle_does_not_consume_attempt(self):
        script = self.directory / 'owned.mjs'
        owned_file(script, ADAPTER.read_bytes())
        self.config, self.path, self.payload = fixture(self.directory, adapter=script)
        tool, challenge = self.ready()
        owned_file(script, ADAPTER.read_bytes() + b'\n// changed\n')
        with self.assertRaisesRegex(BootstrapError, 'adapter_changed'):
            tool.run(challenge['operation_id'])
        self.assertEqual('intent', tool.status(challenge['operation_id'])['state'])
        owned_file(Path(self.config['challenge']['oracle']['path']), b'changed')
        with self.assertRaisesRegex(BootstrapError, 'input_or_oracle_changed'):
            tool.challenge(challenge['operation_id'])

    def test_temporary_lifetime_receipt_reads_after_expiry_without_creating_state_or_grant(self):
        self.config, self.path, self.payload = fixture(self.directory, kind='temporary')
        tool, challenge = self.ready()
        tool.run(challenge['operation_id'])
        tool.verify(challenge['operation_id'])
        journal = EnrollmentJournal.open_readonly(self.config['state_dir'], self.config['enrollment'],
            self.config['authorization'], self.config['verifier_id'], clock=lambda: int(time.time()) + 7200)
        self.assertEqual(self.payload * 3, journal.read_result(challenge['operation_id']))
        self.assertFalse(journal.inspect(challenge['operation_id'])['registered_candidate_fresh'])
        with self.assertRaisesRegex(EnrollmentError, 'journal_readonly'):
            journal.execute(challenge['operation_id'], lambda request: self.fail('not permitted'))
        missing = self.directory / 'does-not-exist'
        with self.assertRaises(EnrollmentError):
            EnrollmentJournal.open_readonly(missing, self.config['enrollment'], self.config['authorization'],
                                            self.config['verifier_id'])
        self.assertFalse(missing.exists())

    def test_proxy_claim_without_trusted_connection_observer_does_not_verify_or_register(self):
        self.config['authorization']['capabilities'][0].update(source_device='delegated-earpiece', proxy_binding='original-pairing')
        self.config['challenge']['proxy'] = {'source_device': 'delegated-earpiece', 'proxy_binding': 'original-pairing',
                                           'revision': 1, 'ttl_seconds': 60}
        self.save()
        tool, challenge = self.ready()
        journal = tool._journal()
        journal.execute(challenge['operation_id'], lambda request: result_envelope(request, self.payload * 3))
        with self.assertRaisesRegex(EnrollmentError, 'connection_observer_required'):
            tool.verify(challenge['operation_id'])
        self.assertEqual('provider_reported', tool.status(challenge['operation_id'])['verification'])
        with self.assertRaisesRegex(BootstrapError, 'registrar_not_configured'):
            tool.register(challenge['operation_id'])

    def test_separate_trusted_registrar_receipt_retained_without_business_grants(self):
        receipts = []
        def registrar(publication):
            receipts.append({'format': 'mesh-enrollment-registration/1',
                'operation_id': publication['operation_id'], 'authority_id': publication['authority_id'],
                'publication_sha256': document_sha256(publication), 'registry_ref': 'synthetic:registry-row'})
            return receipts[-1]
        tool = self.tool(registrar=registrar)
        tool.bootstrap()
        challenge = tool.challenge('original-bootstrap-challenge')
        tool.run(challenge['operation_id'])
        tool.verify(challenge['operation_id'])
        status = tool.register(challenge['operation_id'])
        tool.register(challenge['operation_id'])
        self.assertEqual(1, len(receipts))
        self.assertEqual('registered', status['registration_state'])
        self.assertFalse(status['managed_use_authorized'])
        self.assertEqual(receipts[0], tool.receipt(challenge['operation_id'])['registration'])

    def test_adapter_timeout_keeps_unknown_and_owned_process_reaped(self):
        script = self.directory / 'hanging.mjs'
        owned_file(script, b'setInterval(() => {}, 1000);')
        self.config, self.path, self.payload = fixture(self.directory, adapter=script,
            argv=[str(Path(shutil.which('node')).resolve()), str(script)])
        self.config['adapter']['timeout_seconds'] = 1
        self.save()
        tool, challenge = self.ready()
        result = tool.run(challenge['operation_id'])
        self.assertEqual('unknown', result['status']['state'])
        self.assertTrue(result['process_observation']['direct_process_reaped'])
        self.assertEqual('unknown', self.tool().run(challenge['operation_id'])['status']['state'])

    def test_cli_full_receipt_and_fixed_errors_do_not_expose_config_paths_or_stderr(self):
        tool, challenge = self.ready()
        tool.run(challenge['operation_id'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['--config', str(self.path), 'receipt', '--operation-id', challenge['operation_id']])
        self.assertEqual(0, code)
        self.assertEqual(153600, json.loads(output.getvalue())['result']['result']['bytes'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['--config', str(self.path), 'run', '--operation-id', 'new-id'])
        self.assertEqual(1, code)
        self.assertNotIn(str(self.path), output.getvalue())
        self.assertIn('original_operation_required', output.getvalue())

    def test_duplicate_owner_keys_or_unpinned_executable_arguments_rejected(self):
        owned_file(self.path, b'{"schema":1,"schema":2}')
        with self.assertRaisesRegex(BootstrapError, 'json_invalid'):
            self.tool()
        self.config['adapter']['argv'].append('/unreviewed/script.mjs')
        self.save()
        with self.assertRaisesRegex(BootstrapError, 'unpinned_path'):
            self.tool()

    def test_js_canonical_unicode_and_integer_wire_matches_existing_python_codec(self):
        value = {'\U0001f9ea': '设备😀\x7f', '\uffff': '\u2028', 'negative': -2, 'array': [None, True, 1]}
        source = "import {canonical} from " + json.dumps(ADAPTER.as_uri()) + "; process.stdout.write(canonical(JSON.parse(process.argv[1])));"
        process = subprocess.run([shutil.which('node'), '--input-type=module', '-e', source, json.dumps(value)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(0, process.returncode)
        self.assertEqual(document_sha256(value), hashlib.sha256(process.stdout).hexdigest())

    def test_packaged_node_recipe_has_no_python_or_owner_secret_and_never_overwrites(self):
        spec = importlib.util.spec_from_file_location('enrollment_packager', ROOT / 'deploy/build-enrollment-adapter.py')
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        output = self.directory / 'adapter.zip'
        result = package.build(output)
        self.assertFalse(result['requires_python_on_node'])
        with zipfile.ZipFile(output) as archive:
            self.assertEqual({'MANIFEST.json', 'README.md', 'LICENSE', 'enrollment.json.example',
                              'enrollment-stdio-adapter.mjs'}, set(archive.namelist()))
            manifest = json.loads(archive.read('MANIFEST.json'))
            self.assertFalse(manifest['production_node_enrolled'])
            for name, record in manifest['files'].items():
                self.assertEqual(record['sha256'], hashlib.sha256(archive.read(name)).hexdigest())
            self.assertNotIn(str(self.path).encode('utf8'), b''.join(archive.read(name) for name in archive.namelist()))
        with self.assertRaises(FileExistsError):
            package.build(output)


if __name__ == '__main__':
    unittest.main()
