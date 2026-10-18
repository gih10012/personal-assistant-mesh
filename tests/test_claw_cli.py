"""Exact old Store + new entrance, real SQLite; no iLink/SDK/model business IO."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from assistant_mesh import claw_cli
from assistant_mesh.claw_upload import SkillMediaUploader
from assistant_mesh.store import Conflict
from tests import test_entrance as entrance_fixtures


class ClawCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        entrance_fixtures.EntranceTests.setUpClass()

    def setUp(self):
        f = self.fixture = entrance_fixtures.EntranceTests()
        f.setUp(); self.addCleanup(f.doCleanups)
        self.uploads = []
        self.cls = claw_cli.claw_store_class(f.store_class, self.upload)
        f.store = self.store = self.cls(f.root / 'ledger.sqlite', clock=lambda: f.now)
        f.receive()
        self.task = self.store.claim('entry')
        self.calls = 0
        self.path = f.root / 'owned-file.bin'; self.path.write_bytes(b'complete-upload-bytes\x00\xff')

    def upload(self, data, kind, filename):
        self.uploads.append((data, kind, filename))
        return {'type': {'file': 4, 'image': 2, 'video': 5}[kind], kind + '_item': {
            'file_name': filename, 'len': str(len(data)), 'md5': hashlib.md5(data).hexdigest(),
            'media': {'aes_key': 'private-upload-key', 'encrypt_query_param': 'private-upload-reference'}}}

    def command(self, name, args=None, call_id=None, node='entry', epoch=None):
        self.calls += 1
        return self.store.agent_action(self.task['id'], node, self.task['epoch'] if epoch is None else epoch,
            call_id or 'original-call-' + str(self.calls), 'entrance_claw',
            {'command': name, 'arguments': args or {}})

    def media_args(self, request_id='original-media'):
        return {'request_id': request_id, 'kind': 'file', 'source': claw_cli.source_file(self.path, 1024)[1]}

    def test_scope_lease_and_commands_are_not_operator_or_native_feature_changes(self):
        before = self.fixture.rows('outbox')
        with self.assertRaises(ValueError):
            self.command('send-text', {'request_id': 'wrong-node', 'text': 'must not enqueue'}, node='leader')
        with self.assertRaises(ValueError):
            self.command('send-text', {'request_id': 'wrong-epoch', 'text': 'must not enqueue'}, epoch=999)
        self.assertEqual(before, self.fixture.rows('outbox'))
        self.assertEqual('original-cursor', self.store.get('cursor'))

    def test_text_goes_to_original_outbox_not_notice_and_unknown_only_reads(self):
        args = {'request_id': 'original-text', 'text': 'natural authorized message'}
        first = self.command('send-text', args)
        self.assertEqual(first['status'], 'pending')
        row = self.fixture.rows('outbox')[0]
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET status='unknown',detail=? WHERE id='original-text'", (json.dumps({
                'private_transport_payload': 'PRIVATE_OLD_RESPONSE_NOT_FOR_MODEL',
                'message_id': 900719925474099312345}),))
        before = self.fixture.rows('outbox')
        second = self.command('send-text', args)
        self.assertTrue(second['outcome_unknown'])
        self.assertEqual(second['platform_message_id'], '900719925474099312345')
        self.assertNotIn('PRIVATE_OLD_RESPONSE', json.dumps(second))
        self.assertEqual(before, self.fixture.rows('outbox'))
        self.assertEqual(row['client_id'], before[0]['client_id'])
        with self.assertRaises(ValueError):
            self.command('send-text', dict(args, text='changed content'))

    def test_media_upload_once_then_original_outbox_complete_reference_not_model(self):
        args = self.media_args()
        result = self.command('send-media', args)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0][0], self.path.read_bytes())
        row = self.fixture.rows('outbox')[0]
        self.assertIsNotNone(row['media_items'])
        self.assertEqual(json.loads(row['media_items'])[0]['type'], 4)
        self.assertIn('private-upload-key', row['media_items'])
        self.assertNotIn('private-upload-key', json.dumps(result))
        self.assertNotIn('private-upload-reference', json.dumps(result))
        self.assertEqual([], self.fixture.rows('entrance_replies'))
        before = self.fixture.rows('outbox')
        self.command('send-media', args)
        self.assertEqual(1, len(self.uploads))
        self.assertEqual(before, self.fixture.rows('outbox'))

    def test_failed_upload_and_cross_kind_identity_never_retry_or_reinterpret(self):
        attempts = []
        def unknown(*args):
            attempts.append(True)
            raise OSError('private SDK error must not escape')
        self.store = claw_cli.claw_store_class(self.fixture.store_class, unknown)(
            self.fixture.root / 'ledger.sqlite', clock=lambda: self.fixture.now)
        args = self.media_args()
        self.assertTrue(self.command('send-media', args)['outcome_unknown'])
        self.assertTrue(self.command('send-media', args)['outcome_unknown'])
        self.assertEqual(1, len(attempts))
        self.assertEqual([], self.fixture.rows('outbox'))
        with self.assertRaises(Conflict):
            self.command('send-text', {'request_id': args['request_id'], 'text': 'do not replace media'})
        self.command('send-text', {'request_id': 'text-id', 'text': 'text'})
        with self.assertRaises(Conflict):
            self.command('send-media', self.media_args('text-id'))

    def test_legacy_media_reads_original_unknown_without_upload_or_row_change(self):
        item = self.upload(b'legacy bytes', 'file', 'legacy.bin')
        serialized = json.dumps([item], sort_keys=True, separators=(',', ':'))
        with self.store.transaction() as db:
            self.store._enqueue(db, 'legacy', serialized, explicit=True)
            db.execute("UPDATE outbox SET media_items=?,status='unknown' WHERE id='legacy'", (serialized,))
        before = self.fixture.rows('outbox'); self.uploads.clear()
        result = self.command('send-media', self.media_args('legacy'))
        self.assertTrue(result['outcome_unknown'])
        self.assertFalse(result['original_content_revalidated'])
        self.assertEqual([], self.uploads)
        self.assertEqual(before, self.fixture.rows('outbox'))

    def test_completed_upload_is_retained_when_lease_is_fenced_before_enqueue(self):
        def fenced_upload(data, kind, filename):
            item = self.upload(data, kind, filename)
            with self.store.transaction() as db:
                db.execute('UPDATE tasks SET epoch=epoch+1 WHERE id=?', (self.task['id'],))
            return item
        self.store = claw_cli.claw_store_class(self.fixture.store_class, fenced_upload)(
            self.fixture.root / 'ledger.sqlite', clock=lambda: self.fixture.now)
        with self.assertRaises(ValueError):
            self.command('send-media', self.media_args())
        with self.store.transaction() as db:
            saved = db.execute('SELECT state,item FROM claw_uploads').fetchone()
        self.assertEqual(saved['state'], 'uploaded')
        self.assertIn('private-upload-key', saved['item'])
        self.assertEqual([], self.fixture.rows('outbox'))
        self.assertEqual(1, len(self.uploads))

    def test_download_uses_original_private_membership_and_never_returns_media_key(self):
        self.fixture.receive(self.fixture.message('image-input', items=[{'type': 2, 'image_item': {
            'media': {'aes_key': 'private-source-key', 'encrypt_query_param': 'private-source-cdn'}}}]))
        history = self.command('history', {'source': 'inbox'})
        attachment = history['items'][-1]['attachments'][0]['attachment_id']
        result = self.command('download', {'attachment_id': attachment})
        self.assertEqual(Path(result['path']).read_bytes(), b'complete-original-media')
        self.assertFalse(result['understanding_verified'])
        self.assertNotIn('private-source-key', json.dumps(result))
        with self.assertRaises(ValueError):
            self.command('download', {'attachment_id': '0' * 64})

    def test_uploader_uses_only_minimal_existing_state_stdin_and_does_not_copy_account(self):
        root = self.fixture.root
        account = root / 'existing-account.json'
        value = {'baseurl': 'https://ilinkai.weixin.qq.com', 'bot_token': 'PRIVATE_SYNTHETIC_TOKEN',
            'ilink_user_id': 'bound-owner', 'ilink_bot_id': 'bound-bot',
            'pending': ['PRIVATE_HISTORY_NOT_COPIED'], 'owner_context': {'token': 'PRIVATE_CONTEXT_NOT_COPIED'}}
        account.write_text(json.dumps(value)); account.chmod(0o600)
        before = account.read_bytes()
        helper = root / 'ilink_media.py'; helper.write_text('# synthetic source pin, no SDK/network test\n')
        uploader = SkillMediaUploader(account, helper, hashlib.sha256(helper.read_bytes()).hexdigest(), 'f' * 64)
        calls = []
        item = {'type': 4, 'file_item': {'media': {'aes_key': 'PRIVATE_UPLOAD_REFERENCE'}}}
        def run(argv, **options):
            calls.append((argv, options))
            return mock.Mock(returncode=0, stdout=json.dumps(item).encode())
        with mock.patch('assistant_mesh.claw_upload.subprocess.run', side_effect=run):
            self.assertEqual(uploader(b'full bytes', 'file', 'original.bin'), item)
        argv, options = calls[0]
        supplied = json.loads(options['input'])
        self.assertEqual(set(supplied['state']), {'baseurl', 'bot_token', 'ilink_user_id'})
        self.assertNotIn('PRIVATE_SYNTHETIC_TOKEN', json.dumps(argv))
        self.assertNotIn('PRIVATE_HISTORY_NOT_COPIED', options['input'].decode())
        self.assertNotIn('PRIVATE_CONTEXT_NOT_COPIED', options['input'].decode())
        self.assertEqual(account.read_bytes(), before)
        account.write_text(json.dumps(dict(value, ilink_user_id='different-owner')))
        with mock.patch('assistant_mesh.claw_upload.subprocess.run') as child, self.assertRaises(ValueError):
            uploader(b'full bytes', 'file', 'original.bin')
        child.assert_not_called()

    def test_context_history_recovery_no_token_cursor_or_native_claim(self):
        context = self.command('context')
        self.assertTrue(context['reply_context_present'])
        self.assertEqual(context['reply_context_validity'], 'unknown')
        self.assertNotIn('private-context-never-to-model', json.dumps(context))
        self.command('send-text', {'request_id': 'needs-context', 'text': 'text'})
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET status='rejected',detail=? WHERE id='needs-context'",
                       (json.dumps({'code': 'business_-2'}),))
        before = self.fixture.rows('renewals')
        result = self.command('recover-context', {'request_id': 'needs-context'})
        self.assertEqual(result['recovery_owner'], 'existing_unique_native_refresh_worker')
        self.assertFalse(result['new_refresh_submitted'])
        self.assertFalse(result['new_ilink_poll_started'])
        self.assertEqual(before, self.fixture.rows('renewals'))
        history = self.command('history', {'source': 'outbox'})
        self.assertEqual(history['items'][0]['request_id'], 'needs-context')
        self.assertEqual('original-cursor', self.store.get('cursor'))

    def test_cli_real_parser_uses_worker_identity_and_original_action_without_token_in_args(self):
        config = self.fixture.root / 'worker-private.json'
        config.write_text(json.dumps({'token_file': '/private/reference-only'})); config.chmod(0o600)
        body = self.fixture.root / 'text.txt'; body.write_text('CLI original body')
        calls = []
        def request(path, value):
            calls.append(value)
            return self.store.agent_action(value['task_id'], 'entry', value['epoch'], value['call_id'], value['action'], value['arguments'])
        with mock.patch('assistant_mesh.worker.Client') as client, contextlib.redirect_stdout(io.StringIO()) as output:
            client.return_value.request.side_effect = request
            code = claw_cli.main(['--config', str(config), '--task-id', self.task['id'],
                '--epoch', str(self.task['epoch']), '--call-id', 'cli-original-call',
                'send-text', '--request-id', 'cli-original-send', '--text-file', str(body)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'pending')
        self.assertEqual(calls[0]['action'], 'entrance_claw')
        self.assertNotIn('token', json.dumps(calls))


if __name__ == '__main__':
    unittest.main()
