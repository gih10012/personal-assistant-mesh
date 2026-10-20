"""Narrow synthetic off-host boundary checks; no SSH/service/native/account."""
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
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


if __name__ == '__main__':
    unittest.main()
