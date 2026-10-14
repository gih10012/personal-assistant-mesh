"""Fixed private tool bundle with real isolated journals and synthetic HTTP."""
import base64
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

from assistant_mesh import local_artifact_tool as tool
from assistant_mesh.workspace_cli import CommandArtifactReader
from tests import test_workspace_local_artifact as local_fixture


class LocalArtifactToolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = local_fixture.WorkspaceLocalArtifactTests()
        self.fixture.setUp()
        self.fixture.thin_journal_only()
        self.root = self.fixture.root
        self.spec_path = self.root / 'source.json'
        self.spec = {'format': tool.SOURCE_FORMAT, 'owner_config_path': str(self.fixture.transport_path),
                     'owner_config_sha256': self.fixture.transport_sha, 'journal_path': str(self.fixture.fixture.path)}
        self.spec_sha = self.fixture.fixture.save(self.spec_path, self.spec)
        self.source = tool.LocalArtifactSource(self.spec_path, self.spec_sha)
        self.reference = {'task_id': 'parent', 'operation_id': 'op', 'capability_id': 'campus-cap'}
        self.candidate = self.root / 'candidate'
        self.candidate.mkdir(mode=0o700)

    def tearDown(self):
        self.fixture.tearDown()

    def stage(self):
        return tool.stage(str(self.spec_path), self.spec_sha, str(self.candidate), sys.executable)

    def command(self, action, report=None):
        report = self.stage() if report is None else report
        command = [sys.executable, '-I', report['package_path'], action]
        for key in ('task_id', 'operation_id', 'capability_id'):
            command += ['--' + key.replace('_', '-'), self.reference[key]]
        process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(0, process.returncode, process.stderr.decode('utf8', 'replace'))
        return json.loads(process.stdout)

    def test_source_inspection_entire_journals_and_full_raw_without_constructor_rpc_or_cache(self):
        before = self.fixture.source_snapshot(), self.fixture.fixture.path.read_bytes()
        with mock.patch('assistant_mesh.store.Store.__init__', side_effect=AssertionError('no Store')), \
             mock.patch('assistant_mesh.provider.ManagedProvider.__init__', side_effect=AssertionError('no provider')), \
             mock.patch('assistant_mesh.campus_adapter.execute', side_effect=AssertionError('no GET')):
            result = tool.inspect_source(self.source, self.reference)
        self.assertTrue(result['source_complete_bytes_verified'])
        self.assertTrue(result['source_journals_unchanged_observed'])
        self.assertFalse(result['destination_content_verified'])
        self.assertFalse(result['native_leader_consumption_verified'])
        self.assertEqual(153600, result['manifest']['body_bytes'])
        self.assertNotIn('message_id', result['manifest'])
        self.assertEqual(before, (self.fixture.source_snapshot(), self.fixture.fixture.path.read_bytes()))
        self.assertEqual([], list(self.fixture.fixture.destination.iterdir()))
        self.assertEqual({'call_identity', 'capability_calls'}, set(result['source_snapshots_before']['call_journal']['tables']))

    def test_old_a2a_owner_is_reused_but_origin_is_local_without_rewriting_configuration(self):
        owner = dict(self.fixture.transport, sender='old-a2a-sender')
        owner.pop('node')
        sha = self.fixture.fixture.save(self.fixture.transport_path, owner)
        self.spec['owner_config_sha256'] = sha
        spec_sha = self.fixture.fixture.save(self.spec_path, self.spec)
        original_bytes = self.fixture.transport_path.read_bytes()
        source = tool.LocalArtifactSource(self.spec_path, spec_sha)
        result = tool.inspect_source(source, self.reference)
        self.assertEqual('local_allocation', result['manifest']['source_kind'])
        self.assertEqual('worker', result['manifest']['task_node'])
        self.assertNotIn('sender', result['manifest'])
        self.assertEqual(original_bytes, self.fixture.transport_path.read_bytes())

    def test_candidate_fixed_cli_and_reader_transfer_complete_original_bytes(self):
        before = self.fixture.source_snapshot(), self.fixture.fixture.path.read_bytes()
        report = self.stage()
        self.assertFalse(report['production_installed'])
        self.assertFalse(report['source_journal_write_requested'])
        self.assertIn('assistant_mesh/local_artifact_tool.py', report['source_sha256'])
        self.assertEqual(report['package_sha256'], hashlib.sha256(Path(report['package_path']).read_bytes()).hexdigest())
        inspection = self.command('inspect', report)
        self.assertTrue(inspection['source_complete_bytes_verified'])
        binding = json.loads(Path(report['reader_binding_path']).read_bytes())
        self.assertEqual('local_task', binding['reference_kind'])
        reader = CommandArtifactReader(binding)
        manifest = reader.manifest('parent', 'op', 'campus-cap')
        raw = reader.read('parent', 'op', 'campus-cap')
        self.assertEqual(raw, (self.fixture.source / manifest['artifact_id']).read_bytes())
        self.assertEqual(self.fixture.body, base64.b64decode(json.loads(raw)['response_base64']))
        consumed = self.command('consume', report)
        self.assertEqual('artifact_verified', consumed['phase'])
        self.assertFalse(consumed['workspace_intent_present'])
        self.assertTrue(consumed['source_journals_unchanged_observed'])
        self.assertEqual(raw, Path(consumed['artifact_path']).read_bytes())
        self.assertEqual(before, (self.fixture.source_snapshot(), self.fixture.fixture.path.read_bytes()))
        for filename in ('local-artifact.pyz', 'reader-binding.json', 'READY.json'):
            self.assertEqual(0o600, (self.candidate / filename).stat().st_mode & 0o777)

    def test_bundle_is_deterministic_idempotent_and_never_overwrites_conflicting_owner_file(self):
        first = self.stage()
        package = Path(first['package_path'])
        inode = package.stat().st_ino
        self.assertEqual(first, self.stage())
        self.assertEqual(inode, package.stat().st_ino)
        package.write_bytes(b'owner artifact to preserve')
        package.chmod(0o600)
        with self.assertRaises(Exception):
            self.stage()
        self.assertEqual(b'owner artifact to preserve', package.read_bytes())
        self.assertEqual([], list(self.candidate.glob('.local-artifact-*')))

    def test_mismatched_capability_unknown_provider_and_changed_pin_do_not_consume(self):
        for reference in (dict(self.reference, task_id='other'), dict(self.reference, capability_id='other')):
            with self.subTest(reference=reference), self.assertRaises(Exception):
                tool.inspect_source(self.source, reference)
        self.fixture.mutate(self.fixture.provider_db, "UPDATE provider_executions SET state='unknown'")
        before = self.fixture.source_snapshot()
        with self.assertRaises(Exception):
            tool.consume(self.source, self.reference, self.fixture.fixture.destination)
        self.assertEqual(before, self.fixture.source_snapshot())
        self.spec['format'] = 'changed'
        self.fixture.fixture.save(self.spec_path, self.spec)
        with self.assertRaises(Exception):
            self.source.manifest('parent', 'op', 'campus-cap')
        self.assertEqual([], list(self.fixture.fixture.destination.iterdir()))

    def test_entire_row_fingerprints_detect_noncount_change_and_readonly_queries_do_not_migrate(self):
        before = tool._journal_snapshot(self.fixture.fixture.path)
        self.fixture.mutate(self.fixture.fixture.path, "UPDATE capability_calls SET propose='attempted'")
        after = tool._journal_snapshot(self.fixture.fixture.path)
        self.assertEqual(before['tables']['capability_calls']['rows'], after['tables']['capability_calls']['rows'])
        self.assertNotEqual(before['tables']['capability_calls']['rows_sha256'], after['tables']['capability_calls']['rows_sha256'])
        with closing(sqlite3.connect(str(self.fixture.fixture.path))) as db:
            self.assertEqual([], db.execute("SELECT name FROM sqlite_master WHERE name='workspace_calls'").fetchall())

    def test_fixed_entrypoint_cannot_accept_source_path_override_or_binary_error_json(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(2, tool.tool_main(str(self.spec_path), self.spec_sha, str(self.candidate / 'artifacts'),
                ['consume', '--config', '/PRIVATE_SECRET', '--operation-id', 'op', '--capability-id', 'campus-cap']))
        self.assertNotIn('PRIVATE_SECRET', stream.getvalue())
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(2, tool.tool_main(str(self.spec_path), self.spec_sha, str(self.candidate / 'artifacts'), ['read', 'bad!']))
        self.assertEqual('', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
