"""Narrow synthetic off-host boundary checks; no SSH/service/native/account."""
import gzip
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
import sqlite3
from contextlib import redirect_stdout
from unittest.mock import patch

from tests import test_review_entrance_abandon as fixture_cases


loader = importlib.util.spec_from_file_location('entrance_backup_offhost',
    str(Path(__file__).resolve().parents[1] / 'scripts' / 'verify_entrance_backup_offhost.py'))
offhost = importlib.util.module_from_spec(loader)
loader.loader.exec_module(offhost)


class OffHostBackupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_cases.EntranceAbandonTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def evidence(self):
        fixture = self.fixture
        self.snapshot = fixture.reviews / 'source-snapshot.json'
        self.backup = fixture.reviews / 'complete-owner-backup.gz'
        self.receipt = fixture.reviews / 'offhost-receipt.json'
        output = io.BytesIO()
        captured = offhost.capture_export(str(fixture.f.worker), str(fixture.f.server),
            fixture.task_id, 3, str(fixture.f.rollout), self.snapshot, output)
        self.backup.write_bytes(output.getvalue())
        self.backup.chmod(0o600)
        # Python3.6 can leave __main__.__file__ relative for the documented CLI.
        # Source auditor hashes must still use checked absolute paths.
        relative_script = os.path.relpath(offhost.__file__, os.getcwd())
        with patch.object(offhost, '__file__', relative_script):
            verified = offhost.verify_local(self.snapshot, self.backup, fixture.f.rollout,
                                            self.receipt, fixture.reviews)
        snapshot = json.loads(self.snapshot.read_text())
        self.spec = copy.deepcopy(fixture.spec)
        self.spec['expected'] = snapshot['expected']
        # The apply host has no complete backup at this owner-machine label.
        self.spec['backup'] = dict(snapshot['backup'], path='/OWNER/OFFHOST/not-on-apply-host.gz')
        self.spec['backup_verification'] = {'mode': 'owner-off-host', 'snapshot_path': str(self.snapshot),
            'snapshot_sha256': captured['snapshot_sha256'], 'receipt_path': str(self.receipt),
            'receipt_sha256': verified['receipt_sha256']}
        self.assertEqual(fixture_cases.review.case_id(fixture.spec), fixture_cases.review.case_id(self.spec))
        fixture.f.private('owner-abandon.json', self.spec)
        return fixture_cases.review

    def test_capture_streams_one_complete_read_snapshot_without_store_construction(self):
        fixture = self.fixture
        target = fixture.reviews / 'source-snapshot.json'
        output = io.BytesIO()
        before = fixture.f.database.read_bytes(), fixture.f.rollout.read_bytes()
        with patch('assistant_mesh.store.Store.__init__', side_effect=AssertionError('no Store constructor')):
            report = offhost.capture_export(str(fixture.f.worker), str(fixture.f.server),
                fixture.task_id, 3, str(fixture.f.rollout), target, output)
        packed = output.getvalue()
        sql = gzip.decompress(packed)
        snapshot = json.loads(target.read_text())
        self.assertEqual(before, (fixture.f.database.read_bytes(), fixture.f.rollout.read_bytes()))
        self.assertEqual(snapshot['backup'], report['backup'])
        self.assertEqual(hashlib.sha256(packed).hexdigest(), snapshot['backup']['sha256'])
        self.assertEqual(hashlib.sha256(sql).hexdigest(), snapshot['backup']['sql_sha256'])
        self.assertEqual(len(packed), snapshot['backup']['size'])
        self.assertEqual(len(sql), snapshot['backup']['sql_size'])
        self.assertTrue(sql.startswith(b'BEGIN TRANSACTION;\n') and sql.endswith(b'COMMIT;\n'))
        self.assertEqual(fixture.task, snapshot['selected']['task'])
        self.assertEqual(fixture.session, snapshot['selected']['session'])
        self.assertEqual(3, len(snapshot['expected']['action_row_sha256']))
        self.assertEqual('str', type(snapshot['expected']['rollout_file']['mtime_ns']).__name__)
        self.assertFalse(report['owner_approved'])
        self.assertFalse(report['backup_file_written_on_source_host'])
        self.assertFalse(report['native_terminal_proven'])
        self.assertEqual(0o600, target.stat().st_mode & 0o777)

    def test_complete_offhost_validation_still_needs_exact_owner_spec_and_apply_does_not_restore_backup(self):
        review = self.evidence()
        fixture = self.fixture
        original = fixture.f.database.read_bytes(), fixture.f.rollout.read_bytes()
        context = review.inspect(fixture.path)
        self.assertEqual('owner-off-host', context['backup']['mode'])
        self.assertFalse(context['backup']['host_attestation'])
        self.assertFalse(context['backup']['complete_backup_verified_on_apply_host'])
        self.assertEqual(original, (fixture.f.database.read_bytes(), fixture.f.rollout.read_bytes()))
        printed = io.StringIO()
        with redirect_stdout(printed):
            self.assertEqual(0, review.main(['--spec-file', str(fixture.path)]))
        audit = json.loads(printed.getvalue())
        self.assertTrue(audit['eligible'])
        self.assertEqual(self.spec['backup']['sha256'], audit['backup_sha256'])
        self.assertFalse(audit['backup_verification']['complete_backup_verified_on_apply_host'])
        with self.assertRaisesRegex(review.util.ReviewDenied, 'explicit_spec_approval'):
            review.apply(fixture.path, 'unapproved-receipt-is-not-authority')
        real_connect = sqlite3.connect
        def no_remote_restore(path, *args, **kwargs):
            if '.entrance-offhost-backup.' in str(path) or str(path).endswith('verification.sqlite'):
                raise AssertionError('apply must not restore the owner-machine backup')
            return real_connect(path, *args, **kwargs)
        with patch.object(sqlite3, 'connect', side_effect=no_remote_restore):
            result = review.apply(fixture.path, review.load(fixture.path)[1]['sha256'])
        self.assertTrue(result['applied'], result)
        self.assertFalse(result['backup_verification']['complete_backup_verified_on_apply_host'])
        self.assertFalse(result['native_terminal_proven'])
        self.assertFalse(result['historical_reader_eof_proven'])
        self.assertEqual(original[1], fixture.f.rollout.read_bytes())
        observed = review.status(fixture.path, current_session=True)
        self.assertTrue(observed['applied'])
        self.assertFalse(observed['retry_authorized'])
        with self.assertRaisesRegex(review.util.ReviewDenied, 'existing_intent_use_status'):
            review.apply(fixture.path, review.load(fixture.path)[1]['sha256'])

    def test_complete_input_or_approved_evidence_mismatch_never_becomes_a_passed_receipt(self):
        review = self.evidence()
        fixture = self.fixture
        saved = self.backup.read_bytes()
        self.backup.write_bytes(saved[:-4])
        denied_receipt = fixture.reviews / 'must-not-exist.json'
        with self.assertRaises(ValueError):
            offhost.verify_local(self.snapshot, self.backup, fixture.f.rollout, denied_receipt, fixture.reviews)
        self.assertFalse(denied_receipt.exists())
        self.backup.write_bytes(saved)
        original_receipt = self.receipt.read_bytes()
        for field in ('checks', 'request', 'auditor'):
            value = json.loads(original_receipt)
            if field == 'checks':
                value['checks']['complete_gzip_crc_eof'] = False
            elif field == 'request':
                value['request']['selected_sha256'] = '0' * 64
            else:
                value['auditor']['tool_sources']['review_entrance_abandon.py'] = '0' * 64
            self.receipt.write_text(json.dumps(value))
            self.spec['backup_verification']['receipt_sha256'] = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
            fixture.f.private('owner-abandon.json', self.spec)
            with self.subTest(field=field), self.assertRaises(ValueError):
                review.inspect(fixture.path)
        self.assertFalse((fixture.reviews / review.case_id(self.spec)).exists())

    def test_receipt_changed_after_last_prewrite_check_denies_commit_and_retains_original_status_only(self):
        review = self.evidence()
        fixture = self.fixture
        original = fixture.f.database.read_bytes(), fixture.f.rollout.read_bytes()
        observations = []
        def change_before_commit(unused):
            observations.append(True)
            if len(observations) == 3:
                self.receipt.write_bytes(self.receipt.read_bytes() + b'\n')
            return {'synthetic_current_boundary': True}
        with patch.object(review, 'closure', side_effect=change_before_commit):
            result = review.apply(fixture.path, review.load(fixture.path)[1]['sha256'])
        self.assertTrue(result['outcome_unknown'], result)
        self.assertTrue(review.status(fixture.path)['outcome_unknown'])
        self.assertEqual(original, (fixture.f.database.read_bytes(), fixture.f.rollout.read_bytes()))
        with self.assertRaisesRegex(review.util.ReviewDenied, 'existing_intent_use_status'):
            review.apply(fixture.path, review.load(fixture.path)[1]['sha256'])


if __name__ == '__main__':
    unittest.main()
