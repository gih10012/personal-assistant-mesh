import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.artifact_parts import ArtifactFetchError, join, split


class ArtifactPartsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source, self.parts, self.output = self.root / 'source', self.root / 'parts', self.root / 'output'
        self.body = bytes(range(256)) * 257 + b'trailing remainder'
        self.source.write_bytes(self.body)
        self.source.chmod(0o600)
        self.digest = hashlib.sha256(self.body).hexdigest()

    def tearDown(self):
        self.temporary.cleanup()

    def split(self, **changes):
        return split(self.source, self.parts, len(self.body), changes.get('digest', self.digest), 8)

    def join(self):
        return join(self.parts, self.output, len(self.body), self.digest, 8)

    def test_private_whole_verified_roundtrip_and_idempotent_output(self):
        self.assertTrue(self.split()['sha256_verified'])
        self.assertEqual(0o700, self.parts.stat().st_mode & 0o777)
        self.assertEqual(8, len(list(self.parts.iterdir())))
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 and p.stat().st_nlink == 1
                            for p in self.parts.iterdir()))
        self.assertTrue(self.join()['sha256_verified'])
        self.assertEqual(self.body, self.output.read_bytes())
        self.assertEqual(0o600, self.output.stat().st_mode & 0o777)
        self.assertTrue(self.join()['already_present'])

    def test_wrong_whole_sha_does_not_publish_parts(self):
        with self.assertRaisesRegex(ArtifactFetchError, 'sha256_mismatch'):
            self.split(digest='0' * 64)
        self.assertFalse(self.parts.exists())

    def test_existing_parts_preserved_and_not_overwritten(self):
        self.split()
        before = {p.name: p.read_bytes() for p in self.parts.iterdir()}
        with self.assertRaisesRegex(ArtifactFetchError, 'destination_exists'):
            self.split()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.parts.iterdir()})

    def test_partial_wrong_bytes_unknown_names_never_publish(self):
        self.split()
        part = self.parts / 'part-003'
        original = part.read_bytes()
        part.write_bytes(original[:-1])
        with self.assertRaisesRegex(ArtifactFetchError, 'incomplete'):
            self.join()
        part.write_bytes(b'x' * len(original))
        with self.assertRaisesRegex(ArtifactFetchError, 'sha256_mismatch'):
            self.join()
        part.write_bytes(original)
        (self.parts / 'unexpected').write_bytes(b'not a part')
        with self.assertRaisesRegex(ArtifactFetchError, 'part_set_mismatch'):
            self.join()
        self.assertFalse(self.output.exists())

    def test_user_output_preserved_when_mismatched(self):
        self.split()
        self.output.write_bytes(b'user-owned-data')
        self.output.chmod(0o600)
        with self.assertRaisesRegex(ArtifactFetchError, 'existing_output_mismatch'):
            self.join()
        self.assertEqual(b'user-owned-data', self.output.read_bytes())

    def test_publish_conflict_then_missing_output_never_reports_success(self):
        self.split()
        with patch('scripts.artifact_parts.os.link', side_effect=FileExistsError('simulated disappearing winner')):
            with self.assertRaisesRegex(ArtifactFetchError, 'artifact_output_changed'):
                self.join()
        self.assertFalse(self.output.exists())
        self.assertEqual(8, len(list(self.parts.iterdir())))

    def test_symlink_and_nonprivate_source_are_rejected(self):
        self.source.chmod(0o644)
        with self.assertRaisesRegex(ArtifactFetchError, 'private_source_required'):
            self.split()
        self.source.chmod(0o600)
        alias = self.root / 'alias'
        alias.symlink_to(self.source)
        with self.assertRaisesRegex(ArtifactFetchError, 'symlink'):
            split(alias, self.parts, len(self.body), self.digest)

    def test_umask_groupwrite_never_weakens_published_parts(self):
        previous = os.umask(0o002)
        try:
            self.split()
            self.join()
        finally:
            os.umask(previous)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.parts.iterdir()))


if __name__ == '__main__':
    unittest.main()
