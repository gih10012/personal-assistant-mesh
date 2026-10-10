"""Synthetic native JSONL fixtures, not runtime or model acceptance evidence."""
import copy
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh import native_history as history


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf8')


def event(kind, identity, **fields):
    return {'type': 'event_msg', 'payload': dict(fields, type=kind, turn_id=identity)}


@unittest.skipUnless(hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY')
                     and os.open in getattr(os, 'supports_dir_fd', ()),
                     'owned no-follow history descriptors are required')
class NativeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'synthetic-native.jsonl'
        self.thread = 'original-thread'
        self.turn = 'current-turn'
        self.meta = {'type': 'session_meta', 'payload': {'id': self.thread, 'opaque': 'kept'}}
        self.old = [event('task_started', 'older-turn'),
                    event('turn_aborted', 'older-turn', reason='interrupted'),
                    {'type': 'compacted', 'payload': {'message': 'native compaction kept'}},
                    {'type': 'future_native_type', 'payload': {'text': 'alpha'}}]
        self.write([self.meta] + self.old + [event('task_started', self.turn)])

    def write(self, records, tail=b''):
        self.path.write_bytes(b''.join(encoded(value) for value in records) + tail)
        self.path.chmod(0o600)

    def append(self, *records):
        with self.path.open('ab') as source:
            for value in records:
                source.write(encoded(value))

    def capture(self, tick=None, path=None):
        return history.capture_prefix(path or self.path, self.thread, tick=tick)

    def verify(self, prefix, closed=None, tick=None, path=None):
        return history.verify_history(path or self.path, self.thread, prefix,
                                      [self.turn] if closed is None else closed, tick=tick)

    def close(self, **fields):
        self.append(event('task_complete', self.turn, **fields))

    def mutate_prefix(self):
        data = self.path.read_bytes()
        self.assertIn(b'alpha', data)
        self.path.write_bytes(data.replace(b'alpha', b'bravo', 1))

    def test_capture_returns_only_original_fixed_byte_prefix_and_s_imode(self):
        original = self.path.read_bytes()
        metadata = self.path.stat()
        prefix = self.capture()
        self.assertEqual({'path': str(self.path), 'fingerprint': {
            'dev': metadata.st_dev, 'ino': metadata.st_ino, 'uid': os.getuid(),
            'mode': 0o600, 'size': len(original), 'sha256': hashlib.sha256(original).hexdigest()}}, prefix)
        self.assertEqual(original, self.path.read_bytes())

    def test_capture_allows_tail_appends_during_every_tick_and_finishes_finitely(self):
        original, ticks = self.path.read_bytes(), []

        def append_tick():
            ticks.append(True)
            self.append({'type': 'future_native_type', 'payload': {'appended': len(ticks)}})

        prefix = self.capture(tick=append_tick)
        self.assertEqual(len(original), prefix['fingerprint']['size'])
        self.assertEqual(hashlib.sha256(original).hexdigest(), prefix['fingerprint']['sha256'])
        self.assertTrue(self.path.read_bytes().startswith(original))
        self.assertGreater(self.path.stat().st_size, len(original))
        self.assertGreaterEqual(len(ticks), 3)
        self.assertLess(len(ticks), 10)

    def test_capture_allows_partial_later_line_and_verify_accepts_completed_tail(self):
        partial = encoded(event('task_started', self.turn))
        self.write([self.meta] + self.old, tail=partial[:23])
        prefix = self.capture()
        with self.path.open('ab') as source:
            source.write(partial[23:])
        self.close()
        result = self.verify(prefix)
        self.assertEqual(self.path.stat().st_size, result['fingerprint']['size'])

    def test_partial_start_crossing_baseline_is_new_and_must_be_in_closed_set(self):
        partial = encoded(event('task_started', 'untracked-current'))
        self.write([self.meta], tail=partial[:23])
        prefix = self.capture()
        with self.path.open('ab') as source:
            source.write(partial[23:])
        self.append(event('task_complete', 'untracked-current'))
        with self.assertRaisesRegex(ValueError, 'native_history_turn_unsettled'):
            self.verify(prefix, closed=[])

    def test_capture_requires_complete_first_metadata_line_without_chasing_append(self):
        header = encoded(self.meta)
        self.path.write_bytes(header[:-1])
        appended = []

        def finish_header():
            if not appended:
                appended.append(True)
                with self.path.open('ab') as source:
                    source.write(b'\n')

        with self.assertRaisesRegex(ValueError, 'native_history_metadata_incomplete'):
            self.capture(tick=finish_header)

    def test_capture_rejects_empty_or_malformed_or_wrong_thread_metadata(self):
        cases = [[], [{'type': 'session_meta', 'payload': {'id': 'another-thread'}}],
                 [{'type': 'not_metadata', 'payload': {'id': self.thread}}],
                 [{'type': 'session_meta', 'payload': None}]]
        for records in cases:
            with self.subTest(records=records):
                self.write(records)
                with self.assertRaisesRegex(ValueError, 'native_history_metadata_'):
                    self.capture()

    def test_capture_rejects_same_size_change_before_or_between_prefix_reads(self):
        original = self.path.read_bytes()
        for when in (1, 2, 3):
            self.path.write_bytes(original)
            count = []

            def mutate():
                count.append(True)
                if len(count) == when:
                    self.mutate_prefix()

            with self.subTest(tick=when):
                with self.assertRaisesRegex(ValueError, 'native_history_prefix_changed'):
                    self.capture(tick=mutate)

    def test_capture_rejects_prefix_change_even_with_additional_tail_append(self):
        # First fixed read has not seen any change; second read must disagree.
        original = history._prefix_read
        calls = []

        def observed(source, size, tick, metadata_thread=None):
            result = original(source, size, None, metadata_thread)
            calls.append(True)
            if len(calls) == 1:
                self.mutate_prefix()
                self.append({'type': 'future_native_type', 'payload': {'more': True}})
            return result

        with patch('assistant_mesh.native_history._prefix_read', side_effect=observed):
            with self.assertRaisesRegex(ValueError, 'native_history_prefix_changed'):
                self.capture()

    def test_capture_rejects_path_replacement_even_if_bytes_are_identical(self):
        replaced = []

        def replace():
            if not replaced:
                replaced.append(True)
                incoming = self.folder / 'replacement.jsonl'
                incoming.write_bytes(self.path.read_bytes())
                incoming.chmod(0o600)
                os.replace(str(incoming), str(self.path))

        with self.assertRaisesRegex(ValueError, 'native_history_identity_changed'):
            self.capture(tick=replace)

    def test_capture_rejects_truncation_before_reading_selected_prefix(self):
        changed = []

        def shrink():
            if not changed:
                changed.append(True)
                self.path.write_bytes(encoded(self.meta))

        with self.assertRaisesRegex(ValueError, 'native_history_prefix_changed'):
            self.capture(tick=shrink)

    def test_verified_full_history_is_stable_and_compatible_with_strict_upload(self):
        prefix = self.capture()
        self.close(error=None, last_agent_message='synthetic owner text is not returned')
        data, original_prefix = self.path.read_bytes(), copy.deepcopy(prefix)
        metadata = self.path.stat()
        result = self.verify(prefix)
        expected = {'dev': metadata.st_dev, 'ino': metadata.st_ino, 'uid': metadata.st_uid,
                    'mode': stat.S_IMODE(metadata.st_mode), 'size': len(data),
                    'sha256': hashlib.sha256(data).hexdigest(),
                    'mtime_ns': metadata.st_mtime_ns, 'ctime_ns': metadata.st_ctime_ns}
        self.assertEqual({'path': str(self.path), 'fingerprint': expected}, result)
        self.assertEqual(original_prefix, prefix)
        self.assertEqual(expected, history.sessions._expected_fingerprint(result['fingerprint']))
        self.assertNotIn('synthetic owner text', str(result))
        self.assertEqual(data, self.path.read_bytes())

    def test_official_task_and_turn_wire_aliases_are_supported(self):
        for start in ('task_started', 'turn_started'):
            for complete in ('task_complete', 'turn_complete'):
                with self.subTest(start=start, complete=complete):
                    self.write([self.meta, event(start, self.turn)])
                    prefix = self.capture()
                    self.append(event(complete, self.turn))
                    self.verify(prefix)

    def test_old_aborts_and_error_completions_are_preserved_not_current_evidence(self):
        self.write([self.meta, event('task_started', 'old-failed'),
                    event('task_complete', 'old-failed', error={'codex_error_info': 'usage_limit_exceeded'}),
                    event('turn_aborted', None, reason='interrupted'),
                    event('task_started', self.turn)])
        prefix = self.capture()
        self.close()
        self.verify(prefix)

    def test_compaction_and_unknown_nested_events_do_not_manufacture_turn_evidence(self):
        self.write([self.meta, {'type': 'compacted', 'payload': {'replacement_history': [
            event('task_started', self.turn), event('task_complete', self.turn)]}},
            {'type': 'future_native_type', 'payload': {'native': 'kept'}}])
        prefix = self.capture()
        with self.assertRaisesRegex(ValueError, 'native_history_turn_unsettled'):
            self.verify(prefix)

    def test_missing_selected_start_or_completion_is_unsettled(self):
        self.write([self.meta])
        prefix = self.capture()
        with self.assertRaisesRegex(ValueError, 'native_history_turn_unsettled'):
            self.verify(prefix)
        self.append(event('task_complete', self.turn))
        with self.assertRaisesRegex(ValueError, 'native_history_turns_invalid'):
            self.verify(prefix)
        self.write([self.meta, event('task_started', self.turn)])
        prefix = self.capture()
        with self.assertRaisesRegex(ValueError, 'native_history_turn_unsettled'):
            self.verify(prefix)

    def test_every_selected_turn_must_have_natural_start_and_complete(self):
        prefix = self.capture()
        self.close()
        self.append(event('task_started', 'second-current'))
        with self.assertRaisesRegex(ValueError, 'native_history_turn_unsettled'):
            self.verify(prefix, closed=[self.turn, 'second-current'])
        self.append(event('task_complete', 'second-current'))
        self.verify(prefix, closed=[self.turn, 'second-current'])

    def test_new_untracked_start_or_complete_after_baseline_is_unsettled(self):
        for extra in (event('task_started', 'unexpected-current'),
                      event('turn_started', 'unexpected-current'),
                      event('task_complete', 'unexpected-current')):
            with self.subTest(extra=extra['payload']['type']):
                self.write([self.meta, event('task_started', self.turn)])
                prefix = self.capture()
                self.close()
                self.append(extra)
                with self.assertRaisesRegex(ValueError, 'native_history_turn_unsettled'):
                    self.verify(prefix)

    def test_aborted_selected_turn_can_never_count_as_natural_completion(self):
        for reason in ('interrupted', 'replaced', 'review_ended', 'budget_limited'):
            with self.subTest(reason=reason):
                self.write([self.meta, event('task_started', self.turn)])
                prefix = self.capture()
                self.append(event('turn_aborted', self.turn, reason=reason))
                self.close()
                with self.assertRaisesRegex(ValueError, 'native_history_turn_aborted'):
                    self.verify(prefix)

    def test_unattributable_abort_after_baseline_is_unknown(self):
        prefix = self.capture()
        self.close()
        self.append(event('turn_aborted', None, reason='interrupted'))
        with self.assertRaisesRegex(ValueError, 'native_history_turns_invalid'):
            self.verify(prefix)

    def test_anonymous_abort_inside_selected_prefix_interval_is_not_old_completion(self):
        self.append(event('turn_aborted', None, reason='interrupted'))
        prefix = self.capture()
        self.close()
        with self.assertRaisesRegex(ValueError, 'native_history_turn_aborted'):
            self.verify(prefix)

    def test_non_none_completion_error_is_never_natural_even_if_empty_or_false(self):
        for error in ({}, False, '', {'codex_error_info': 'usage_limit_exceeded'}):
            with self.subTest(error=error):
                self.write([self.meta, event('task_started', self.turn)])
                prefix = self.capture()
                self.close(error=error)
                with self.assertRaisesRegex(ValueError, 'native_history_turn_aborted'):
                    self.verify(prefix)

    def test_historical_abort_of_selected_id_is_not_repaired_by_later_complete(self):
        self.append(event('turn_aborted', self.turn, reason='interrupted'))
        prefix = self.capture()
        self.close()
        with self.assertRaisesRegex(ValueError, 'native_history_turn_aborted'):
            self.verify(prefix)

    def test_duplicate_selected_start_or_completion_is_rejected(self):
        for extra in (event('task_started', self.turn), event('task_complete', self.turn)):
            with self.subTest(extra=extra['payload']['type']):
                self.write([self.meta, event('task_started', self.turn)])
                prefix = self.capture()
                self.close()
                self.append(extra)
                with self.assertRaisesRegex(ValueError, 'native_history_turns_invalid'):
                    self.verify(prefix)

    def test_malformed_new_turn_identity_is_rejected(self):
        for identity in (None, '', False, [], {}):
            with self.subTest(identity=identity):
                self.write([self.meta, event('task_started', self.turn)])
                prefix = self.capture()
                self.close()
                self.append(event('task_started', identity))
                with self.assertRaisesRegex(ValueError, 'native_history_turns_invalid'):
                    self.verify(prefix)

    def test_prefix_bytes_changed_with_same_size_or_appended_tail_is_rejected(self):
        prefix = self.capture()
        self.mutate_prefix()
        self.close()
        with self.assertRaisesRegex(ValueError, 'native_history_prefix_changed'):
            self.verify(prefix)

    def test_final_file_replacement_or_alternate_hardlink_path_cannot_change_original_identity(self):
        prefix = self.capture()
        self.close()
        alternate = self.folder / 'alternate.jsonl'
        os.link(str(self.path), str(alternate))
        with self.assertRaisesRegex(ValueError, 'native_history_prefix_invalid'):
            self.verify(prefix, path=alternate)
        replacement = self.folder / 'replacement.jsonl'
        replacement.write_bytes(self.path.read_bytes())
        replacement.chmod(0o600)
        os.replace(str(replacement), str(self.path))
        with self.assertRaisesRegex(ValueError, 'native_history_identity_changed'):
            self.verify(prefix)

    def test_capture_or_verify_rejects_final_symlink_without_following_it(self):
        alias = self.folder / 'linked-history.jsonl'
        alias.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'native_history_path_unsafe'):
            self.capture(path=alias)
        prefix = self.capture()
        self.close()
        original = self.folder / 'original.jsonl'
        self.path.rename(original)
        self.path.symlink_to(original)
        with self.assertRaisesRegex(ValueError, 'native_history_path_unsafe'):
            self.verify(prefix)

    def test_unsafe_ancestor_or_relative_path_is_rejected_without_chmod(self):
        for path in (Path('relative.jsonl'), self.folder / '..' / self.folder.name / self.path.name):
            with self.assertRaisesRegex(ValueError, 'native_history_path_unsafe'):
                self.capture(path=path)
        self.folder.chmod(0o777)
        try:
            with self.assertRaisesRegex(ValueError, 'native_history_path_unsafe'):
                self.capture()
            self.assertEqual(0o777, stat.S_IMODE(self.folder.stat().st_mode))
        finally:
            self.folder.chmod(0o700)

    def test_closed_history_requires_complete_newline_terminated_jsonl_tail(self):
        prefix = self.capture()
        self.close()
        with self.path.open('ab') as source:
            source.write(b'{"type":"future_native_type"')
        with self.assertRaisesRegex(ValueError, 'native_history_jsonl_incomplete'):
            self.verify(prefix)

    def test_prefix_truncation_is_rejected(self):
        prefix = self.capture()
        self.path.write_bytes(encoded(self.meta))
        with self.assertRaisesRegex(ValueError, 'native_history_prefix_changed'):
            self.verify(prefix)

    def test_malformed_utf8_json_scalar_duplicate_keys_and_nan_fail_without_text_leak(self):
        tails = [b'not-json-private-owner-text\n', b'\xff\n', b'17\n',
                 b'{"type":"event_msg","type":"compacted"}\n',
                 b'{"type":"future_native_type","payload":{"a":NaN}}\n']
        for tail in tails:
            with self.subTest(tail_kind=tails.index(tail)):
                self.write([self.meta, event('task_started', self.turn)])
                prefix = self.capture()
                self.close()
                with self.path.open('ab') as source:
                    source.write(tail)
                with self.assertRaisesRegex(ValueError, '^native_history_jsonl_invalid$'):
                    self.verify(prefix)

    def test_oversized_record_is_unsupported_not_truncated_or_rewritten(self):
        self.write([self.meta, {'type': 'compacted', 'payload': {'text': 'x' * 300}},
                    event('task_started', self.turn)])
        prefix = self.capture()
        self.close()
        original = self.path.read_bytes()
        with patch('assistant_mesh.native_history.MAX_RECORD_BYTES', 256):
            with self.assertRaisesRegex(ValueError, '^native_history_record_oversized$'):
                self.verify(prefix)
        self.assertEqual(original, self.path.read_bytes())

    def test_closed_metadata_change_or_append_during_verify_is_unstable(self):
        for change in ('append', 'same_size', 'mode'):
            self.write([self.meta] + self.old + [event('task_started', self.turn)])
            prefix = self.capture()
            self.close()
            changed = []

            def mutate():
                if not changed:
                    changed.append(True)
                    if change == 'append':
                        self.append({'type': 'future_native_type', 'payload': {}})
                    elif change == 'same_size':
                        self.mutate_prefix()
                    else:
                        self.path.chmod(0o644)

            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, 'native_history_(unstable|identity_changed)'):
                    self.verify(prefix, tick=mutate)

    def test_verify_path_replacement_during_final_block_tick_is_rejected(self):
        prefix = self.capture()
        self.close()
        ticks = []

        def replace():
            ticks.append(True)
            if len(ticks) == 2:
                incoming = self.folder / 'after-read-replacement.jsonl'
                incoming.write_bytes(self.path.read_bytes())
                incoming.chmod(0o600)
                os.replace(str(incoming), str(self.path))

        with self.assertRaisesRegex(ValueError, 'native_history_identity_changed'):
            self.verify(prefix, tick=replace)

    def test_unrelated_directory_changes_do_not_fake_file_instability(self):
        prefix = self.capture()
        self.close()
        result = self.verify(prefix, tick=lambda: (self.folder / 'unrelated').write_bytes(b'other'))
        self.assertEqual(self.path.stat().st_size, result['fingerprint']['size'])

    def test_multiblock_reads_keep_ticking_without_returning_owner_content(self):
        self.write([self.meta, {'type': 'future_native_type', 'payload': {'text': 'a' * (65536 * 3)}},
                    event('task_started', self.turn)])
        ticks = []
        prefix = self.capture(tick=lambda: ticks.append(True))
        self.assertGreaterEqual(len(ticks), 9)
        self.close()
        ticks[:] = []
        result = self.verify(prefix, tick=lambda: ticks.append(True))
        self.assertGreaterEqual(len(ticks), 5)
        self.assertEqual({'path', 'fingerprint'}, set(result))

    def test_lease_failure_during_capture_or_verify_propagates_without_mutation(self):
        original = self.path.read_bytes()

        def expired():
            raise ValueError('stale_task_lease')

        with self.assertRaisesRegex(ValueError, '^stale_task_lease$'):
            self.capture(tick=expired)
        self.assertEqual(original, self.path.read_bytes())
        prefix = self.capture()
        self.close()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '^stale_task_lease$'):
            self.verify(prefix, tick=expired)
        self.assertEqual(original, self.path.read_bytes())

    def test_invalid_thread_prefix_and_closed_id_inputs_fail_closed(self):
        for thread in (None, False, [], '', ' '):
            with self.assertRaisesRegex(ValueError, 'native_history_thread_invalid'):
                history.capture_prefix(self.path, thread)
        prefix = self.capture()
        self.close()
        malformed = [None, {}, dict(prefix, extra=True),
                     dict(prefix, path='another-path'),
                     dict(prefix, fingerprint=dict(prefix['fingerprint'], size=True)),
                     dict(prefix, fingerprint=dict(prefix['fingerprint'], size=0)),
                     dict(prefix, fingerprint=dict(prefix['fingerprint'], sha256='A' * 64))]
        for value in malformed:
            with self.assertRaisesRegex(ValueError, 'native_history_prefix_invalid'):
                self.verify(value)
        for turns in (None, 'current-turn', [self.turn, self.turn], [None], [False], [[]]):
            with self.assertRaisesRegex(ValueError, 'native_history_turns_invalid'):
                history.verify_history(self.path, self.thread, prefix, turns)

    def test_missing_nofollow_support_is_an_explicit_history_unsupported_error(self):
        with patch('assistant_mesh.sessions.os.supports_dir_fd', set()):
            with self.assertRaisesRegex(ValueError, '^native_history_unsupported$'):
                self.capture()

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'descriptor accounting is Linux-only')
    def test_failed_metadata_validation_closes_the_source_and_directory_descriptors(self):
        self.write([{'type': 'session_meta', 'payload': {'id': 'wrong-thread'}}])
        before = len(list(Path('/proc/self/fd').iterdir()))
        for attempt in range(20):
            with self.assertRaisesRegex(ValueError, '^native_history_metadata_invalid$'):
                self.capture()
        self.assertEqual(before, len(list(Path('/proc/self/fd').iterdir())))


if __name__ == '__main__':
    unittest.main()
