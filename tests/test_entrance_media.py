"""Synthetic private archive bridge; no poller, account, CDN or model calls."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

from assistant_mesh.entrance_media import (EntranceMedia, EntranceMediaError,
    SkillMediaDownloader, message_metadata, public_projection)


class EntranceMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'media'
        self.calls = []
        self.payload = b'\xff\xd8\xffsynthetic-jpeg-full-bytes\x00\xff'
        self.media = EntranceMedia(self.root, downloader=self.download, clock=lambda: 1792108800)

    def download(self, record, maximum):
        self.calls.append((record, maximum))
        return self.payload

    def message(self, kind='image', item_type=2):
        return {'message_id': 900719925474099312345,
                'create_time_ms': 1792108799123,
                'from_user_id': 'synthetic-owner', 'to_user_id': 'synthetic-bot',
                'context_token': 'never-expose-context', 'item_list': [
                    {'type': 1, 'text_item': {'text': 'private caption'}},
                    {'type': item_type, kind + '_item': {'file_name': '../../secret.xlsx',
                        'media': {'aes_key': 'never-expose-aes',
                                  'encrypt_query_param': 'never-expose-cdn'}}}]}

    def stage(self, message=None):
        return self.media.stage(message or self.message(), 'original-inbox-hash',
            1792108801.125, request_id='original-request', task_id='original-task',
            reply_to='900719925474099312344', conversation_id='original-conversation')

    def attachment(self, message=None):
        return self.stage(message)['attachments'][0]['attachment_id']

    def test_exact_platform_id_original_milliseconds_and_distinct_observations(self):
        metadata = self.stage()['message']
        self.assertEqual(metadata['message_id'], '900719925474099312345')
        self.assertEqual(metadata['original_time']['raw'], '1792108799123')
        self.assertTrue(metadata['original_time']['known'])
        self.assertTrue(metadata['original_time']['utc'].endswith('.123Z'))
        self.assertTrue(metadata['received_at'].endswith('.125000Z'))
        self.assertNotEqual(metadata['received_at'], metadata['original_time']['utc'])
        self.assertIsNone(metadata['processed_at'])
        self.assertIsNone(metadata['sent_at'])
        self.assertEqual(metadata['reply_to'], '900719925474099312344')
        self.assertEqual(metadata['task_id'], 'original-task')

    def test_unknown_original_time_does_not_become_receive_or_process_time(self):
        for raw in (None, 0, -1, True, 1792108799123.0, 'not-a-time'):
            message = dict(self.message(), create_time_ms=raw)
            metadata = message_metadata(message, 'inbox', 1792108801,
                                        processed_at=1792108802, sent_at=1792108803)
            self.assertFalse(metadata['original_time']['known'])
            self.assertIsNone(metadata['original_time']['utc'])
            self.assertNotEqual(metadata['received_at'], metadata['processed_at'])
            self.assertNotEqual(metadata['processed_at'], metadata['sent_at'])

    def test_float_boolean_ids_and_missing_source_rejected(self):
        for raw in (1.0, True, {}, ''):
            with self.subTest(raw=raw), self.assertRaises(EntranceMediaError):
                self.stage(dict(self.message(), message_id=raw))
        with self.assertRaises(EntranceMediaError):
            message_metadata(self.message(), None, 1792108801)

    def test_stage_is_private_idempotent_no_download_and_no_secret_projection(self):
        first = self.stage()
        self.assertEqual(first, self.stage())
        self.assertEqual(len(self.calls), 0)
        encoded = json.dumps(first)
        for secret in ('never-expose', 'private caption', 'secret.xlsx'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(len(list(self.media.records.iterdir())), 1)
        record = next(self.media.records.iterdir())
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(record.parent.stat().st_mode), 0o700)
        self.assertIn('never-expose-aes', record.read_text())

    def test_materialize_preserves_refs_full_bytes_and_unverified_understanding(self):
        attachment_id = self.attachment()
        result = self.media.materialize(attachment_id)
        self.assertEqual(result['state'], 'available')
        self.assertFalse(result['understanding_verified'])
        self.assertFalse(result['desktop_required'])
        self.assertEqual(result['message'], self.stage()['message'])
        self.assertEqual(Path(result['path']).read_bytes(), self.payload)
        self.assertEqual(result['sha256'], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(result['size'], len(self.payload))
        self.assertEqual(Path(result['path']).parent, self.media.downloads)
        self.assertTrue(result['path'].endswith('.jpg'))
        self.assertEqual(stat.S_IMODE(Path(result['path']).stat().st_mode), 0o600)
        public = json.dumps(public_projection(result))
        for private in ('path', 'file_name', 'original-task', 'original-conversation', 'never-expose'):
            self.assertNotIn(private, public)

    def test_cached_asset_hashes_all_bytes_and_does_not_redownload_or_rewrite(self):
        attachment_id = self.attachment()
        first = self.media.materialize(attachment_id)
        receipt_path = self.media.receipts / (attachment_id + '.json')
        before = receipt_path.read_bytes()
        second = self.media.materialize(attachment_id)
        self.assertTrue(second['cache_hit'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(before, receipt_path.read_bytes())
        Path(first['path']).write_bytes(self.payload[:-1] + b'x')
        with self.assertRaisesRegex(EntranceMediaError, 'entrance_cached_asset_changed'):
            self.media.materialize(attachment_id)
        self.assertEqual(len(self.calls), 1)

    def test_reference_corruption_fails_without_fetch(self):
        attachment_id = self.attachment()
        path = self.media.records / (attachment_id + '.json')
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(EntranceMediaError, 'entrance_original_record_changed'):
            self.media.materialize(attachment_id)
        self.assertEqual(self.calls, [])

    def test_no_downloader_or_unknown_item_preserves_reference_without_network(self):
        attachment_id = self.attachment()
        self.media.downloader = None
        self.assertEqual(self.media.materialize(attachment_id)['code'], 'entrance_media_not_configured')
        unknown = self.attachment({'message_id': 'opaque-platform-id', 'item_list': [{'type': 97, 'raw': 'private'}]})
        result = self.media.materialize(unknown)
        self.assertEqual(result['state'], 'not_supported')
        self.assertEqual(result['kind'], 'unrecognized')
        self.assertIsNone(result['message']['original_time']['utc'])
        self.assertEqual(self.calls, [])

    def test_unsafe_file_names_never_choose_output_path(self):
        for filename in ('/tmp/evil', '../../escape.exe', 'name.sh;echo x', 'actual.xlsx'):
            message = self.message('file', 4)
            message['item_list'][1]['file_item']['file_name'] = filename
            result = self.media.materialize(self.attachment(message))
            self.assertEqual(Path(result['path']).parent, self.media.downloads)
            self.assertTrue(Path(result['path']).name.startswith(result['attachment_id']))
            self.assertEqual(result['file_name'], filename)

    def test_permissions_symlinks_and_attachment_id_cannot_escape_root(self):
        with self.assertRaises(EntranceMediaError):
            self.media.materialize('../private')
        target = Path(self.temporary.name) / 'target'
        target.mkdir(mode=0o700)
        link = Path(self.temporary.name) / 'link'
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(EntranceMediaError):
            EntranceMedia(link)
        attachment_id = self.attachment()
        record = self.media.records / (attachment_id + '.json')
        record.chmod(0o644)
        with self.assertRaises(EntranceMediaError):
            self.media.materialize(attachment_id)
        self.assertEqual(self.calls, [])

    def test_error_redaction_and_download_limit_do_not_create_success_receipt(self):
        attachment_id = self.attachment()
        def failed(record, maximum):
            raise EntranceMediaError('private AES or CDN credentials')
        self.media.downloader = failed
        result = self.media.materialize(attachment_id)
        self.assertEqual(result['code'], 'entrance_media_download_failed')
        self.assertNotIn('private AES', json.dumps(result))
        self.media.downloader = lambda record, maximum: b'x' * (maximum + 1)
        self.media.max_bytes = 8
        with self.assertRaisesRegex(EntranceMediaError, 'entrance_media_size_invalid'):
            self.media.materialize(attachment_id)
        self.assertEqual(list(self.media.receipts.iterdir()), [])

    def test_download_only_child_delegates_existing_component_with_pins(self):
        # A tiny synthetic dependency, not a fake claim of live CDN/SDK execution.
        helper = Path(self.temporary.name) / 'skill'
        helper.mkdir(mode=0o700)
        ilink = b'class BotError(ValueError): pass\n'
        media = b'''from ilink import BotError
def download_async(record, maximum):
    assert record['content']['media']['aes_key'] == 'never-expose-aes'
    assert maximum > 0
    return b'complete synthetic download'
def transfer(value): return value
'''
        (helper / 'ilink.py').write_bytes(ilink)
        (helper / 'ilink_media.py').write_bytes(media)
        downloader = SkillMediaDownloader(helper / 'ilink_media.py',
            hashlib.sha256(media).hexdigest(), hashlib.sha256(ilink).hexdigest())
        # The isolated child requires an installed real SDK metadata distribution.
        # Use a private Python launcher to inject ONLY a synthetic version lookup;
        # the transfer component remains exactly the owner-pinned files above.
        launcher = Path(self.temporary.name) / 'python-launcher'
        injected = "import importlib.metadata; importlib.metadata.version=lambda name: '0.3.0'; "
        launcher.write_text('#!' + sys.executable + '\nimport os,sys\n'
            + 'os.execv(sys.executable, [sys.executable, "-I", "-c", '
            + repr(injected) + ' + sys.argv[3]] + sys.argv[4:])\n')
        launcher.chmod(0o700)
        downloader.python = str(launcher)
        self.media.downloader = downloader
        result = self.media.materialize(self.attachment())
        self.assertEqual(Path(result['path']).read_bytes(), b'complete synthetic download')
        (helper / 'ilink_media.py').write_bytes(media + b'\n')
        other = self.message(); other['message_id'] = 'different-original-message'
        self.assertEqual(self.media.materialize(self.attachment(other))['code'], 'entrance_media_download_failed')


if __name__ == '__main__':
    unittest.main()
