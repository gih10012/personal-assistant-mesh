import os
import socket
import tempfile
import unittest
from pathlib import Path

from scripts.check_reverse_socket import prepare


class ReverseSocketTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.path = self.root / 'only-owned-reverse.sock'

    def tearDown(self):
        self.directory.cleanup()

    def test_missing_socket_does_not_remove_anything(self):
        self.assertFalse(prepare(self.path)['stale_socket_removed'])

    def test_live_socket_is_not_removed(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
            self.path.chmod(0o600)
            listener.listen(1)
            with self.assertRaisesRegex(ValueError, 'still_active'):
                prepare(self.path)
            self.assertTrue(self.path.exists())

    def test_only_private_stale_socket_is_removed(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
        self.path.chmod(0o600)
        self.assertTrue(prepare(self.path)['stale_socket_removed'])
        self.assertFalse(self.path.exists())

    def test_regular_file_and_symlink_are_preserved(self):
        self.path.touch(mode=0o600)
        with self.assertRaises(ValueError):
            prepare(self.path)
        link = self.root / 'link.sock'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            prepare(link)
        self.assertTrue(self.path.exists())

    def test_accessible_parent_or_socket_is_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            prepare(self.path)
        self.root.chmod(0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
        self.path.chmod(0o666)
        with self.assertRaises(ValueError):
            prepare(self.path)
        self.assertTrue(self.path.exists())

    def test_parent_requires_exact_0700_not_merely_no_other_permissions(self):
        for mode in (0o500, 0o600, 0o1700, 0o755):
            self.root.chmod(mode)
            try:
                with self.assertRaisesRegex(ValueError, 'owned_0700_directory'):
                    prepare(self.path)
                self.assertEqual(mode, self.root.stat().st_mode & 0o7777)
            finally:
                self.root.chmod(0o700)

    def test_socket_requires_exact_0600_and_never_removes_wrong_mode(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
        for mode in (0o200, 0o400, 0o700, 0o1600, 0o666):
            self.path.chmod(mode)
            with self.assertRaisesRegex(ValueError, 'private_owned_socket'):
                prepare(self.path)
            self.assertTrue(self.path.exists())
            self.assertEqual(mode, self.path.stat().st_mode & 0o7777)

    def test_symlink_ancestor_is_rejected_without_unlinking_socket(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
        self.path.chmod(0o600)
        alias = self.root / 'parent-link'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'absolute_nonsymlink_path'):
            prepare(alias / self.path.name)
        self.assertTrue(self.path.exists())


if __name__ == '__main__':
    unittest.main()
