"""Synthetic frozen-file fixtures; no real native history or model execution."""
import base64
import copy
import gzip
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh import sessions


class RecordingClient:
    def __init__(self, hook=None):
        self.calls, self.hook = [], hook

    def request(self, route, body):
        self.calls.append((route, copy.deepcopy(body)))
        if self.hook:
            self.hook(body)
        return {'ok': True}

    def actions(self, action):
        return [body for route, body in self.calls if body['action'] == action]


@unittest.skipUnless(hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY')
                     and os.open in getattr(os, 'supports_dir_fd', ()),
                     'strict fingerprinting requires no-follow directory descriptors')
class SessionFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'synthetic-history.jsonl'
        self.data = os.urandom(65536 * 2 + 17)
        self.path.write_bytes(self.data)
        self.path.chmod(0o600)
        self.task = {'id': 'original-task', 'epoch': 7}
        self.state = {'thread_id': 'original-thread', 'side_effect_started': True}
        self.client = RecordingClient()
        self.expected = self.fingerprint()

    def fingerprint(self, times=False, path=None):
        path = path or self.path
        metadata = path.stat()
        value = {'dev': metadata.st_dev, 'ino': metadata.st_ino,
                 'uid': metadata.st_uid, 'mode': stat.S_IMODE(metadata.st_mode),
                 'size': metadata.st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if times:
            value.update(mtime_ns=metadata.st_mtime_ns, ctime_ns=metadata.st_ctime_ns)
        return value

    def save(self, tick=None, expected=None, path=None):
        return sessions.save(self.client, self.task, 'fixture-node', 'codex', self.state,
                             path or self.path, tick,
                             expected_fingerprint=self.expected if expected is None else expected)

    def no_commit(self):
        self.assertEqual([], self.client.actions('commit'))

    def overwrite_same_size(self, offset=65536):
        with self.path.open('r+b') as source:
            source.seek(offset)
            source.write(bytes([self.data[offset] ^ 1]))

    def test_matching_fingerprint_streams_full_artifact_and_preserves_identity(self):
        ticks = []
        self.assertEqual({'ok': True}, self.save(tick=lambda: ticks.append(True)))
        uploads = self.client.actions('upload')
        self.assertGreater(len(uploads), 1)
        self.assertEqual(list(range(len(uploads))), [body['payload']['part'] for body in uploads])
        packed = b''.join(base64.b64decode(body['payload']['data']) for body in uploads)
        self.assertEqual(self.data, gzip.decompress(packed))
        committed = self.client.actions('commit')
        self.assertEqual(1, len(committed))
        self.assertEqual({'thread_id': 'original-thread', 'side_effect_started': True,
                          'codex_node': 'fixture-node'}, committed[0]['payload']['state'])
        self.assertEqual(self.expected, self.fingerprint())
        self.assertEqual({'thread_id': 'original-thread', 'side_effect_started': True}, self.state)
        self.assertTrue(all(body['task_id'] == 'original-task' and body['epoch'] == 7
                            for route, body in self.client.calls))
        self.assertGreater(len(ticks), 12)

    def test_three_full_verification_phases_use_the_same_open_source_descriptor(self):
        original, observations = sessions._verify_fingerprint, []

        def observe(path, source, expected, ancestors, initial, tick):
            observations.append((source.fileno(), source.closed))
            return original(path, source, expected, ancestors, initial, tick)

        with patch('assistant_mesh.sessions._verify_fingerprint', side_effect=observe):
            self.save()
        self.assertEqual(3, len(observations))
        self.assertEqual(1, len({descriptor for descriptor, closed in observations}))
        self.assertTrue(all(not closed for descriptor, closed in observations))

    def test_optional_nanosecond_times_are_validated(self):
        self.save(expected=self.fingerprint(times=True))
        for key in ('mtime_ns', 'ctime_ns'):
            self.client = RecordingClient()
            expected = self.fingerprint(times=True)
            expected[key] += 1
            with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
                self.save(expected=expected)
            self.assertEqual([], self.client.calls)

    def test_contract_rejects_missing_extra_or_malformed_fields_before_upload(self):
        invalid = [None, [], 'fingerprint', {}, dict(self.expected, extra='unexpected')]
        for key in self.expected:
            invalid.append({field: value for field, value in self.expected.items() if field != key})
        for key in ('dev', 'ino', 'uid', 'mode', 'size', 'mtime_ns', 'ctime_ns'):
            for value in (True, False, -1, 1.0, '1', None):
                invalid.append(dict(self.expected, **{key: value}))
        invalid.extend([dict(self.expected, mode=0o100600),
                        dict(self.expected, sha256='A' * 64),
                        dict(self.expected, sha256='a' * 63),
                        dict(self.expected, sha256='a' * 65),
                        dict(self.expected, sha256='g' * 64),
                        dict(self.expected, sha256=True)])
        for expected in invalid:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, 'fingerprint_invalid'):
                    sessions._expected_fingerprint(expected)
        self.assertEqual([], self.client.calls)

    def test_each_wrong_required_identity_or_content_field_is_rejected(self):
        for key in ('dev', 'ino', 'uid', 'mode', 'size', 'sha256'):
            self.client = RecordingClient()
            expected = dict(self.expected)
            expected[key] = '0' * 64 if key == 'sha256' else expected[key] ^ 1
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
                    self.save(expected=expected)
                self.assertEqual([], self.client.calls)

    def test_strict_save_requires_rollout_and_absolute_path(self):
        with self.assertRaisesRegex(ValueError, 'fingerprint_rollout_required'):
            sessions.save(self.client, self.task, 'fixture-node', 'codex', self.state,
                          expected_fingerprint=self.expected)
        for path in (Path('relative-history.jsonl'), self.folder / '..' / self.folder.name / self.path.name):
            with self.assertRaisesRegex(ValueError, 'fingerprint_path_unsafe'):
                self.save(path=path)
        self.assertEqual([], self.client.calls)

    def test_final_and_ancestor_symlinks_are_not_followed(self):
        alias = self.folder / 'alias.jsonl'
        alias.symlink_to(self.path)
        parent_alias = self.folder / 'parent-alias'
        parent_alias.symlink_to(self.folder, target_is_directory=True)
        for path in (alias, parent_alias / self.path.name):
            with self.subTest(path=path.name):
                with self.assertRaisesRegex(ValueError, 'fingerprint_path_unsafe'):
                    self.save(path=path)
        self.assertEqual(self.data, self.path.read_bytes())
        self.assertEqual([], self.client.calls)

    def test_directory_and_fifo_are_rejected_without_blocking(self):
        fifo = self.folder / 'not-a-regular-file'
        os.mkfifo(str(fifo), 0o600)
        for path in (self.folder, fifo):
            with self.subTest(path=path.name):
                with self.assertRaisesRegex(ValueError, 'fingerprint_path_unsafe'):
                    self.save(path=path)
        self.assertEqual([], self.client.calls)

    def test_unsafe_ancestor_is_rejected_without_chmod(self):
        self.folder.chmod(0o777)
        try:
            with self.assertRaisesRegex(ValueError, 'fingerprint_path_unsafe'):
                self.save()
            self.assertEqual(0o777, stat.S_IMODE(self.folder.stat().st_mode))
            self.assertEqual([], self.client.calls)
        finally:
            self.folder.chmod(0o700)

    def test_sticky_ancestor_and_owned_existing_0755_directory_are_accepted(self):
        shared = self.folder / 'sticky'
        shared.mkdir(mode=0o700)
        shared.chmod(0o1777)
        private = shared / 'owned'
        private.mkdir(mode=0o755)
        private.chmod(0o755)
        source = private / self.path.name
        source.write_bytes(self.data)
        source.chmod(0o600)
        self.save(expected=self.fingerprint(path=source), path=source)
        self.assertEqual(0o1777, stat.S_IMODE(shared.stat().st_mode))
        self.assertEqual(0o755, stat.S_IMODE(private.stat().st_mode))

    def test_foreign_owned_file_or_ancestor_is_rejected(self):
        original = os.fstat
        for kind in ('file', 'directory'):
            def foreign(descriptor):
                metadata = original(descriptor)
                selected = stat.S_ISREG(metadata.st_mode) if kind == 'file' else (
                    stat.S_ISDIR(metadata.st_mode) and metadata.st_ino == self.folder.stat().st_ino)
                if selected:
                    fields = list(metadata)
                    fields[4] = os.getuid() + 1
                    return os.stat_result(fields)
                return metadata
            with self.subTest(kind=kind), patch('assistant_mesh.sessions.os.fstat', side_effect=foreign):
                with self.assertRaisesRegex(ValueError, 'fingerprint_path_unsafe'):
                    self.save()
        self.assertEqual([], self.client.calls)

    def test_missing_nofollow_directory_support_is_fail_closed_only_for_strict_mode(self):
        with patch('assistant_mesh.sessions.os.supports_dir_fd', set()):
            with self.assertRaisesRegex(ValueError, 'fingerprint_unsupported'):
                self.save()
            self.assertEqual([], self.client.calls)
            sessions.save(self.client, self.task, 'fixture-node', 'codex', self.state,
                          self.path, expected_fingerprint=None)
        self.assertEqual(1, len(self.client.actions('commit')))

    def test_empty_regular_file_has_full_sha_and_streaming_gzip_artifact(self):
        self.path.write_bytes(b'')
        self.expected = self.fingerprint()
        self.save()
        self.assertEqual(b'', gzip.decompress(base64.b64decode(
            self.client.actions('upload')[0]['payload']['data'])))
        self.assertEqual(1, len(self.client.actions('commit')))

    def test_expected_fingerprint_is_copied_before_mutating_caller_data(self):
        def mutate(body):
            if body['action'] == 'upload':
                self.expected.clear()
        self.client.hook = mutate
        self.save()
        self.assertEqual(1, len(self.client.actions('commit')))

    def test_change_before_compression_is_rejected_before_upload(self):
        changed = []

        def tick():
            if not changed:
                changed.append(True)
                with self.path.open('ab') as source:
                    source.write(b'late append')

        with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
            self.save(tick=tick)
        self.assertEqual([], self.client.calls)

    def test_actual_compression_bytes_are_hashed_even_if_source_is_restored(self):
        original, phase = sessions._verify_fingerprint, {'checks': 0, 'compression_ticks': 0}

        def verify(*args):
            phase['checks'] += 1
            return original(*args)

        def tick():
            if phase['checks'] != 1:
                return
            # The first full hash has three block ticks plus its opening tick.
            phase['compression_ticks'] += 1
            if phase['compression_ticks'] == 5:
                # Stay beyond any BufferedReader read-ahead at the boundary.
                self.overwrite_same_size(offset=65536 + 16384)
            elif phase['compression_ticks'] == 6:
                self.path.write_bytes(self.data)  # Restore before post-compression verification.

        with patch('assistant_mesh.sessions._verify_fingerprint', side_effect=verify):
            with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
                self.save(tick=tick)
        self.assertEqual(1, phase['checks'])  # The compressed-byte digest itself rejected it.
        self.assertEqual(self.data, self.path.read_bytes())
        self.assertEqual([], self.client.calls)

    def test_append_during_compression_is_rejected_without_upload(self):
        original, phase = sessions._verify_fingerprint, {'checks': 0, 'ticks': 0}

        def verify(*args):
            phase['checks'] += 1
            return original(*args)

        def tick():
            if phase['checks'] == 1:
                phase['ticks'] += 1
                if phase['ticks'] == 5:
                    with self.path.open('ab') as source:
                        source.write(b'late append')

        with patch('assistant_mesh.sessions._verify_fingerprint', side_effect=verify):
            with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
                self.save(tick=tick)
        self.assertEqual([], self.client.calls)

    def test_append_after_last_upload_leaves_parts_uncommitted(self):
        def append(body):
            if body['action'] == 'upload':
                with self.path.open('ab') as source:
                    source.write(b'late append')
        self.client.hook = append
        with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
            self.save()
        self.assertGreater(len(self.client.actions('upload')), 0)
        self.no_commit()

    def test_same_size_write_during_upload_is_rejected_by_full_hash(self):
        original = sessions._fingerprint_metadata

        def stable_times(metadata):
            value = original(metadata)
            value['mtime_ns'] = value['ctime_ns'] = 0
            return value

        self.client.hook = lambda body: self.overwrite_same_size() if body['action'] == 'upload' else None
        # Isolate hash protection: even identical observed times do not suffice.
        with patch('assistant_mesh.sessions._fingerprint_metadata', side_effect=stable_times):
            with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
                self.save()
        self.no_commit()

    def test_change_and_restore_during_upload_is_rejected_by_internal_time_baseline(self):
        changed = []

        def restore(body):
            if body['action'] == 'upload' and not changed:
                changed.append(True)
                self.overwrite_same_size()
                self.path.write_bytes(self.data)

        self.client.hook = restore
        with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
            self.save()
        self.assertEqual(self.data, self.path.read_bytes())
        self.no_commit()

    def test_file_replacement_with_identical_bytes_during_upload_is_rejected(self):
        replaced = []

        def replace(body):
            if body['action'] == 'upload' and not replaced:
                replaced.append(True)
                replacement = self.folder / 'replacement.jsonl'
                replacement.write_bytes(self.data)
                replacement.chmod(0o600)
                os.replace(str(replacement), str(self.path))

        self.client.hook = replace
        with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
            self.save()
        self.assertEqual(self.data, self.path.read_bytes())
        self.no_commit()

    def test_symlink_substitution_after_upload_is_never_followed(self):
        changed = []

        def replace(body):
            if body['action'] == 'upload' and not changed:
                changed.append(True)
                original = self.folder / 'original.jsonl'
                self.path.rename(original)
                self.path.symlink_to(original)

        self.client.hook = replace
        with self.assertRaisesRegex(ValueError, 'fingerprint_path_unsafe'):
            self.save()
        self.no_commit()

    def test_parent_replacement_cannot_pass_by_hardlinking_same_source_inode(self):
        parent = self.folder / 'parent'
        parent.mkdir(mode=0o700)
        self.path = parent / self.path.name
        self.path.write_bytes(self.data)
        self.path.chmod(0o600)
        self.expected = self.fingerprint()
        changed = []

        def replace(body):
            if body['action'] == 'upload' and not changed:
                changed.append(True)
                moved = self.folder / 'old-parent'
                parent.rename(moved)
                parent.mkdir(mode=0o700)
                os.link(str(moved / self.path.name), str(self.path))

        self.client.hook = replace
        with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
            self.save()
        self.no_commit()

    def test_unrelated_directory_ctime_change_is_not_a_false_file_conflict(self):
        self.client.hook = lambda body: (self.folder / 'unrelated-file').write_bytes(b'other')
        self.save()
        self.assertEqual(1, len(self.client.actions('commit')))

    def test_mode_change_during_upload_is_rejected_without_chmod_repair(self):
        self.client.hook = lambda body: self.path.chmod(0o644) if body['action'] == 'upload' else None
        with self.assertRaisesRegex(ValueError, 'fingerprint_changed'):
            self.save()
        self.assertEqual(0o644, stat.S_IMODE(self.path.stat().st_mode))
        self.no_commit()

    def test_source_deletion_during_upload_is_rejected(self):
        self.client.hook = lambda body: self.path.unlink() if self.path.exists() else None
        with self.assertRaisesRegex(ValueError, 'fingerprint_path_unsafe'):
            self.save()
        self.no_commit()

    def test_lease_failure_before_compression_never_uploads_or_commits(self):
        def expired():
            raise ValueError('stale_task_lease')
        with self.assertRaisesRegex(ValueError, 'stale_task_lease'):
            self.save(tick=expired)
        self.assertEqual([], self.client.calls)

    def test_lease_failure_after_upload_never_commits(self):
        def expired():
            if self.client.actions('upload'):
                raise ValueError('stale_task_lease')
        with self.assertRaisesRegex(ValueError, 'stale_task_lease'):
            self.save(tick=expired)
        self.assertEqual(1, len(self.client.actions('upload')))
        self.no_commit()

    def test_lease_failure_during_final_complete_hash_never_commits(self):
        original, phase = sessions._verify_fingerprint, {'checks': 0, 'ticks': 0}

        def verify(*args):
            phase['checks'] += 1
            return original(*args)

        def expired():
            if phase['checks'] == 3:
                phase['ticks'] += 1
                if phase['ticks'] == 2:
                    raise ValueError('stale_task_lease')

        with patch('assistant_mesh.sessions._verify_fingerprint', side_effect=verify):
            with self.assertRaisesRegex(ValueError, 'stale_task_lease'):
                self.save(tick=expired)
        self.assertGreater(len(self.client.actions('upload')), 0)
        self.no_commit()

    def test_upload_failure_is_not_retried_or_committed(self):
        def fail(body):
            if body['action'] == 'upload':
                raise OSError('ambiguous_upload_receipt')
        self.client.hook = fail
        with self.assertRaisesRegex(OSError, 'ambiguous_upload_receipt'):
            self.save()
        self.assertEqual(1, len(self.client.actions('upload')))
        self.no_commit()

    def test_default_save_still_accepts_followed_symlink_and_state_only_commit(self):
        alias = self.folder / 'legacy-alias.jsonl'
        alias.symlink_to(self.path)
        sessions.save(self.client, self.task, 'fixture-node', 'codex', self.state, alias)
        sessions.save(self.client, self.task, 'fixture-node', 'codex', self.state)
        self.assertEqual(2, len(self.client.actions('commit')))
        self.assertEqual(0, self.client.actions('commit')[-1]['payload']['parts'])

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'descriptor accounting is Linux-only')
    def test_rejected_paths_do_not_leak_directory_descriptors(self):
        alias = self.folder / 'rejected-alias.jsonl'
        alias.symlink_to(self.path)
        before = len(list(Path('/proc/self/fd').iterdir()))
        for attempt in range(20):
            with self.assertRaisesRegex(ValueError, 'fingerprint_path_unsafe'):
                self.save(path=alias)
        self.assertEqual(before, len(list(Path('/proc/self/fd').iterdir())))


if __name__ == '__main__':
    unittest.main()
