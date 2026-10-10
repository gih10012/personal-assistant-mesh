"""Synthetic private SQLite only: no credentials, network, receiver or import."""
import copy
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh import channel_checkpoint as module
from assistant_mesh.channel_checkpoint import (CheckpointError, export_checkpoint,
                                                validate_checkpoint, write_checkpoint)
from assistant_mesh.remote_notifications import RemoteNotifications, text_fingerprint
from assistant_mesh.store import Store


class ChannelCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = self.root / 'source.db'
        self.binding = 'a' * 64
        self.store = Store(self.database, clock=lambda: 1000)
        self.notifications = RemoteNotifications(self.store, 'cloud', self.binding)
        self.message = {'message_id': 'owner-message', 'from_user_id': 'fixture-owner',
                        'to_user_id': 'fixture-bot', 'message_type': 1, 'create_time_ms': 1000000,
                        'context_token': 'fixture-private-context',
                        'item_list': [{'type': 1, 'text_item': {'text': 'synthetic work'}}]}
        self.store.ingest([self.message], 'private-cursor-1', 'fixture-owner', 'fixture-bot')
        self.task_id = self.task_rows()[0]['id']
        self.store.create_task('child work', ['agent'], parent_id=self.task_id, task_id='child')
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='needs_review',epoch=3,checkpoint=? WHERE id=?",
                       (json.dumps({'side_effect_started': True, 'thread_id': 'fixture-native-original'}), self.task_id))
            db.execute("INSERT INTO session_chunks VALUES('original-native-artifact',0,?)", (b'original-native-bytes',))
        self.store.enqueue('unknown-send', 'do not replay')
        # The first ack is also pending; consume it and preserve its status.
        while True:
            row = self.store.next_send()
            if row is None:
                break
            self.store.finish_send(row['id'], 'unknown', {'delivery_verified': False, 'code': 'timeout'})

    def tearDown(self):
        self.tmp.cleanup()

    def task_rows(self):
        with self.store.transaction() as db:
            return [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY id')]

    def export(self, **kwargs):
        return export_checkpoint(self.database, 'cloud', self.binding, 'snapshot-one',
                                 clock=lambda: 1234, **kwargs)

    def validate(self, checkpoint, expected=None, authority='cloud', binding=None):
        return validate_checkpoint(checkpoint, authority, binding or self.binding,
                                   checkpoint['sha256'] if expected is None else expected)

    @staticmethod
    def reseal(checkpoint):
        checkpoint['sha256'] = module._digest(checkpoint['payload'])
        return checkpoint

    def test_roundtrip_preserves_private_rows_original_tasks_and_guards(self):
        checkpoint = self.export()
        result = self.validate(checkpoint)
        payload = checkpoint['payload']
        self.assertTrue(result['valid'])
        self.assertIs(False, payload['migration_ready'])
        self.assertIs(False, result['import_or_send_authorized'])
        self.assertEqual(self.notifications.route_id, payload['route_id'])
        self.assertEqual(self.task_rows(), payload['tables']['tasks'])
        original = next(r for r in payload['tables']['tasks'] if r['id'] == self.task_id)
        self.assertTrue(json.loads(original['checkpoint'])['side_effect_started'])
        self.assertEqual('needs_review', original['status'])
        self.assertEqual(3, original['epoch'])
        self.assertEqual('canonical_task', payload['inbox_task_bindings'][0]['binding'])
        self.assertEqual(self.task_id, payload['inbox_task_bindings'][0]['task_id'])
        self.assertEqual({self.task_id, 'child'}, {r['task_id'] for r in payload['task_bindings']})
        self.assertTrue(all(r['ledger_authority'] == 'cloud' for r in payload['task_bindings']))

    def test_unknown_send_preserves_client_id_detail_and_context(self):
        with self.store.transaction() as db:
            before = dict(db.execute("SELECT * FROM outbox WHERE id='unknown-send'").fetchone())
        checkpoint = self.export()
        row = next(r for r in checkpoint['payload']['tables']['outbox'] if r['id'] == 'unknown-send')
        self.assertEqual(before, row)
        self.assertEqual('unknown', row['status'])
        meta = {r['key']: json.loads(r['value']) for r in checkpoint['payload']['tables']['meta']}
        self.assertEqual('private-cursor-1', meta['cursor'])
        self.assertEqual('fixture-private-context', meta['owner_context']['token'])
        self.assertFalse(self.validate(checkpoint)['native_features_restricted'])

    def test_source_unchanged_and_store_constructor_never_called(self):
        with self.store.transaction() as db:
            before = list(db.iterdump())
        with patch.object(Store, '__init__', side_effect=AssertionError('must not open writable Store')):
            self.export()
        with self.store.transaction() as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_inflight_submitting_not_recovered_to_unknown(self):
        self.store.enqueue('inflight', 'pending transport')
        row = self.store.next_send()
        self.assertEqual('inflight', row['id'])
        checkpoint = self.export()
        exported = next(r for r in checkpoint['payload']['tables']['outbox'] if r['id'] == 'inflight')
        self.assertEqual('submitting', exported['status'])
        self.assertEqual('submitting', self.store.send_status('inflight')['status'])

    def test_single_read_snapshot_survives_independent_wal_writer(self):
        original_rows = module._rows
        changed = []

        def rows(db, table, budget):
            if not changed:
                changed.append(True)
                with self.store.transaction() as writer:
                    writer.execute("UPDATE meta SET value=? WHERE key='cursor'", (json.dumps('new-cursor'),))
                    writer.execute("UPDATE outbox SET status='accepted' WHERE id='unknown-send'")
            return original_rows(db, table, budget)

        with patch.object(module, '_rows', side_effect=rows):
            checkpoint = self.export()
        tables = checkpoint['payload']['tables']
        meta = {r['key']: json.loads(r['value']) for r in tables['meta']}
        self.assertEqual('private-cursor-1', meta['cursor'])
        self.assertEqual('unknown', next(r for r in tables['outbox'] if r['id'] == 'unknown-send')['status'])
        self.assertEqual('new-cursor', self.store.get('cursor'))
        self.assertEqual('accepted', self.store.send_status('unknown-send')['status'])

    def test_dependencies_are_anchors_not_native_history_copy(self):
        checkpoint = self.export()
        dependency = next(r for r in checkpoint['payload']['source_dependencies'] if r['table'] == 'session_chunks')
        self.assertEqual(1, dependency['rows'])
        self.assertTrue(dependency['retained_at_source_authority'])
        self.assertNotIn('original-native-bytes', json.dumps(checkpoint))
        self.assertFalse(checkpoint['payload']['safety']['native_history_complete'])

    def test_changed_native_dependency_changes_checkpoint(self):
        first = self.export()
        with self.store.transaction() as db:
            db.execute("UPDATE session_chunks SET body=?", (b'new-native-bytes',))
        second = self.export()
        self.assertNotEqual(first['sha256'], second['sha256'])

    def test_optional_notify_receipt_matches_original_outbox(self):
        text = 'synthetic relay notification'
        self.notifications.receive('laptop', {'request_id': 'notify-one', 'text': text,
            'fingerprint': text_fingerprint(text), 'route_id': self.notifications.route_id})
        checkpoint = self.export()
        self.assertEqual(1, len(checkpoint['payload']['tables']['mesh_notify_receipts']))
        self.assertTrue(self.validate(checkpoint)['valid'])

    def test_missing_optional_receipt_table_is_explicit_not_invented(self):
        with self.store.transaction() as db:
            db.execute('DROP TABLE mesh_notify_receipts')
        checkpoint = self.export()
        self.assertNotIn('mesh_notify_receipts', checkpoint['payload']['tables'])
        self.assertTrue(self.validate(checkpoint)['valid'])

    def test_receipt_without_original_outbox_rejected(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO mesh_notify_receipts VALUES('laptop','bad',?,'missing',1000)", ('b' * 64,))
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_notify_receipt_invalid$'):
            self.export()

    def test_relay_unknown_post_intent_pinned_receipt_preserved(self):
        # An explicit local relay can coexist in this synthetic source ledger;
        # its destination remains original, never changed to checkpoint holder.
        fingerprint = text_fingerprint('relay text')
        receipt = {'protocol': 'mesh-notify/1', 'authority': 'original-peer', 'source_node': 'cloud',
                   'request_id': 'relay-one', 'fingerprint': fingerprint,
                   'outbox_id': 'mesh-notify-' + module._digest(['mesh-notify/1', 'cloud', 'relay-one']),
                   'route_id': 'c' * 64, 'found': False, 'status': 'not_found',
                   'idempotent_receive': True, 'delivery_verified': False}
        with self.store.transaction() as db:
            db.execute("INSERT INTO outbox(id,fingerprint,body,status,client_id,created,delivery_route) VALUES('relay-one',?,'relay text','pending','original-client',1000,'relay')", (fingerprint,))
            db.execute("INSERT INTO notification_relays(request_id,source_node,peer,authority,fingerprint,state,attempt,post_attempted,receipt,created,updated) VALUES('relay-one','cloud','original-peer','original-peer',?,'unknown',2,1,?,1000,1000)", (fingerprint, json.dumps(receipt)))
        checkpoint = self.export()
        relay = checkpoint['payload']['tables']['notification_relays'][0]
        self.assertEqual('unknown', relay['state'])
        self.assertEqual(1, relay['post_attempted'])
        self.assertEqual('original-peer', relay['authority'])
        self.assertEqual(receipt, json.loads(relay['receipt']))
        self.assertTrue(self.validate(checkpoint)['valid'])

    def test_orphan_relay_rejected(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO notification_relays(request_id,source_node,peer,authority,fingerprint,created,updated) VALUES('missing','cloud','peer','peer',?,1000,1000)", ('b' * 64,))
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_relay_binding_invalid$'):
            self.export()

    def test_complete_task_parent_relation_required(self):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET parent_id='missing-parent' WHERE id='child'")
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_task_parent_missing$'):
            self.export()

    def test_task_parent_cycle_rejected(self):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET parent_id='child' WHERE id=?", (self.task_id,))
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_task_parent_cycle$'):
            self.export()

    def test_owner_message_missing_canonical_task_rejected(self):
        with self.store.transaction() as db:
            db.execute("DELETE FROM tasks WHERE id='child'")
            db.execute('DELETE FROM tasks WHERE id=?', (self.task_id,))
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_owner_task_missing$'):
            self.export()

    def test_control_media_and_old_message_need_not_create_tasks(self):
        self.store.set('activate_after_ms', 1000000)
        messages = []
        for identity, text, created in (('status', '/status', 1000001), ('stale', 'old work', 1), ('media', None, 1000001)):
            msg = dict(self.message, message_id=identity, create_time_ms=created)
            msg['item_list'] = [] if text is None else [{'type': 1, 'text_item': {'text': text}}]
            messages.append(msg)
        self.store.ingest(messages, 'cursor-2', 'fixture-owner', 'fixture-bot')
        checkpoint = self.export()
        self.assertEqual(3, sum(r['task_id'] is None for r in checkpoint['payload']['inbox_task_bindings']))

    def test_media_reference_preserved_without_filesystem_reads(self):
        self.store.enqueue_media('media-send', [{'type': 2, 'image_item': {'private_reference': '/nonexistent/attachment'}}])
        checkpoint = self.export()
        media = next(r for r in checkpoint['payload']['tables']['outbox'] if r['id'] == 'media-send')
        self.assertEqual('/nonexistent/attachment', json.loads(media['media_items'])[0]['image_item']['private_reference'])
        self.assertTrue(self.validate(checkpoint)['valid'])

    def test_task_mapping_cannot_silently_change_authority(self):
        checkpoint = self.export()
        checkpoint['payload']['task_bindings'][0]['ledger_authority'] = 'laptop'
        self.reseal(checkpoint)
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_task_binding_mismatch$'):
            self.validate(checkpoint)

    def test_expected_authority_account_and_external_digest_are_required(self):
        checkpoint = self.export()
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_authority_mismatch$'):
            self.validate(checkpoint, authority='laptop')
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_account_binding_mismatch$'):
            self.validate(checkpoint, binding='b' * 64)
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_integrity_mismatch$'):
            self.validate(checkpoint, expected='b' * 64)

    def test_rehashed_unknown_rewrite_rejected_by_retained_digest(self):
        checkpoint = self.export()
        expected = checkpoint['sha256']
        changed = copy.deepcopy(checkpoint)
        next(r for r in changed['payload']['tables']['outbox'] if r['id'] == 'unknown-send')['status'] = 'pending'
        self.reseal(changed)
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_integrity_mismatch$'):
            self.validate(changed, expected=expected)

    def test_migration_ready_is_never_valid_even_with_recomputed_digest(self):
        checkpoint = self.export()
        checkpoint['payload']['migration_ready'] = True
        self.reseal(checkpoint)
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_format_invalid$'):
            self.validate(checkpoint)

    def test_extra_or_changed_schema_rejected(self):
        with self.store.transaction() as db:
            db.execute('ALTER TABLE outbox ADD COLUMN unhandled_intent TEXT')
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_schema_unsupported$'):
            self.export()

    def test_schema_type_or_primary_key_tampering_rejected(self):
        checkpoint = self.export()
        checkpoint['payload']['schemas']['outbox'][0][5] = 0
        self.reseal(checkpoint)
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_schema_unsupported$'):
            self.validate(checkpoint)

    def test_changed_body_fingerprint_rejected(self):
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET body='modified-private-text' WHERE id='unknown-send'")
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_outbox_invalid$'):
            self.export()

    def test_invalid_inbox_identity_rejected(self):
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET id='wrong-identity'")
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_inbox_identity_invalid$'):
            self.export()

    def test_account_change_and_missing_cursor_rejected(self):
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_account_binding_mismatch$'):
            export_checkpoint(self.database, 'cloud', 'b' * 64, 'one')
        self.store.set('cursor', None)
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_cursor_invalid$'):
            self.export()

    def test_rows_bytes_and_invalid_limits_fail_without_partial_artifact(self):
        for kwargs in ({'max_rows': 1}, {'max_bytes': 100}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(CheckpointError, '^checkpoint_limit_exceeded$'):
                self.export(**kwargs)
        for kwargs in ({'max_rows': True}, {'max_bytes': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(CheckpointError, '^checkpoint_limits_invalid$'):
                self.export(**kwargs)

    def test_dependency_blob_bytes_count_against_limit(self):
        with self.store.transaction() as db:
            db.execute('UPDATE session_chunks SET body=?', (b'x' * 1024 * 1024,))
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_limit_exceeded$'):
            self.export(max_bytes=1024 * 100)

    def test_unselected_meta_is_not_exported_as_credentials(self):
        self.store.set('unrelated_private_setting', 'not-channel-state')
        checkpoint = self.export()
        self.assertNotIn('not-channel-state', json.dumps(checkpoint))

    def test_publish_new_private_file_roundtrip_no_overwrite(self):
        checkpoint = self.export()
        destination = self.root / 'checkpoint.json'
        result = write_checkpoint(checkpoint, destination, 'cloud', self.binding, checkpoint['sha256'])
        self.assertTrue(result['written'])
        self.assertEqual(0o600, stat.S_IMODE(destination.stat().st_mode))
        self.assertEqual(1, destination.stat().st_nlink)
        self.assertEqual(checkpoint, json.loads(destination.read_text()))
        before = destination.read_bytes()
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_destination_exists$'):
            write_checkpoint(checkpoint, destination, 'cloud', self.binding, checkpoint['sha256'])
        self.assertEqual(before, destination.read_bytes())
        self.assertEqual([], list(self.root.glob('.mesh-checkpoint-*')))

    def test_nonprivate_source_and_symlinks_rejected(self):
        self.database.chmod(0o644)
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_private_path_required$'):
            self.export()
        self.database.chmod(0o600)
        linked = self.root / 'linked.db'
        linked.symlink_to(self.database)
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_private_path_required$'):
            export_checkpoint(linked, 'cloud', self.binding, 'one')

    def test_nonprivate_destination_and_symlink_rejected(self):
        checkpoint = self.export()
        directory = self.root / 'public'
        directory.mkdir(mode=0o755)
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_private_path_required$'):
            write_checkpoint(checkpoint, directory / 'out.json', 'cloud', self.binding, checkpoint['sha256'])
        linked = self.root / 'out.json'
        linked.symlink_to(self.database)
        with self.assertRaisesRegex(CheckpointError, '^checkpoint_private_path_required$'):
            write_checkpoint(checkpoint, linked, 'cloud', self.binding, checkpoint['sha256'])

    def test_publish_failure_leaves_no_temporary_private_copy(self):
        checkpoint = self.export()
        with patch.object(module.os, 'link', side_effect=OSError('private error must not leak')):
            with self.assertRaisesRegex(CheckpointError, '^checkpoint_publish_failed$'):
                write_checkpoint(checkpoint, self.root / 'out.json', 'cloud', self.binding, checkpoint['sha256'])
        self.assertEqual([], list(self.root.glob('.mesh-checkpoint-*')))

    def test_post_publication_fsync_failure_preserves_existing_artifact(self):
        checkpoint = self.export()
        destination = self.root / 'out.json'
        original_fsync = module.os.fsync
        calls = []

        def fail_directory_fsync(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError('directory fsync failed')
            return original_fsync(descriptor)

        with patch.object(module.os, 'fsync', side_effect=fail_directory_fsync):
            with self.assertRaisesRegex(CheckpointError, '^checkpoint_publish_failed$'):
                write_checkpoint(checkpoint, destination, 'cloud', self.binding, checkpoint['sha256'])
        self.assertEqual(checkpoint, json.loads(destination.read_text()))
        self.assertEqual(1, destination.stat().st_nlink)
        self.assertTrue(self.validate(json.loads(destination.read_text()))['valid'])
        self.assertEqual([], list(self.root.glob('.mesh-checkpoint-*')))

    def test_failed_write_never_opens_source_authority_or_sender(self):
        checkpoint = self.export()
        with patch.object(sqlite3, 'connect', side_effect=AssertionError('must not open DB')):
            write_checkpoint(checkpoint, self.root / 'out.json', 'cloud', self.binding, checkpoint['sha256'])


if __name__ == '__main__':
    unittest.main()
