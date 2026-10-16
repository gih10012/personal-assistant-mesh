"""New entrance on exact incumbent 3198 Store, real SQLite, synthetic IO only."""
import base64
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from assistant_mesh.entrance import (CONTRACT_SHA256, entrance_store_class,
    message_identity, validate_policy)
from assistant_mesh.entrance_failure import (PROTOCOL as FAILURE_PROTOCOL,
    closed_failure_backend, history_snapshot)
from assistant_mesh.entrance_media import EntranceMedia
from assistant_mesh.entrance_runtime import EntranceWorker, media_from_config
from assistant_mesh.store import Store, Conflict
from tests.test_legacy_store_carrier_backport import _original_source, _patched_source, _load_store


POLICY = {'owner_ref': 'owner', 'conversation_id': 'owner-claw', 'agent_id': 'claw-entrance'}
THREAD = '01000000-0000-7000-8000-000000000001'


class EntranceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = _load_store(_patched_source(_original_source()))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 1792191000.125
        self.media = EntranceMedia(self.root / 'media', downloader=lambda record, maximum: b'complete-original-media')
        self.store_class = entrance_store_class(self.original, POLICY, self.media)
        self.store = self.store_class(self.root / 'ledger.sqlite', clock=lambda: self.now)
        self.policy = validate_policy(POLICY)
        self.store.heartbeat('entry', [self.policy['capability']])
        self.store.heartbeat('leader', ['leader'])

    def message(self, identity='message-1', text='在吗', items=None):
        return {'message_id': identity, 'create_time_ms': 1792191000000,
            'from_user_id': 'owner', 'to_user_id': 'bot', 'message_type': 1,
            'context_token': 'private-context-never-to-model',
            'item_list': items if items is not None else [{'type': 1, 'text_item': {'text': text}}]}

    def receive(self, message=None, cursor='original-cursor'):
        message = message or self.message()
        self.store.ingest([message], cursor, 'owner', 'bot')
        return message_identity(message)

    def rows(self, table):
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table)]

    def action(self, task, name, arguments, identity=None):
        return self.store.agent_action(task['id'], 'entry', task['epoch'],
            identity or task['id'] + ':' + name, 'entrance_' + name, arguments)

    def finish(self, task):
        self.store.update_task(task['id'], 'entry', task['epoch'],
            {'side_effect_started': False}, 'private native final', 'completed')

    def test_new_ordinary_and_media_only_do_not_make_leader_or_template_ack(self):
        for message in (self.message(), self.message('media', items=[{'type': 2, 'image_item': {
                'media': {'aes_key': 'private-aes', 'encrypt_query_param': 'private-cdn'}}}])):
            self.receive(message)
        tasks = self.rows('tasks')
        self.assertEqual(2, len(tasks))
        self.assertEqual([self.policy['scope']] * 2, [task['scope'] for task in tasks])
        self.assertTrue(all(json.loads(task['required']) == [self.policy['capability']] for task in tasks))
        self.assertEqual([], self.rows('outbox'))
        self.assertIsNone(self.store.claim('leader'))
        self.assertEqual('original-cursor', self.store.get('cursor'))
        for secret in ('private-context', 'private-aes', 'private-cdn'):
            self.assertNotIn(secret, json.dumps(tasks))

    def test_old_leader_unknown_does_not_block_new_entrance_or_get_replayed(self):
        self.store.create_task('old input', task_id='old-task')
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='needs_review',checkpoint=? WHERE id='old-task'",
                       (json.dumps({'side_effect_started': True, 'old-operation': 'unknown'}),))
        before = self.rows('tasks')[0]
        identity = self.receive()
        task = self.store.claim('entry')
        self.assertEqual('entrance-' + identity, task['id'])
        self.action(task, 'silent', {'reason': 'already answered in the current conversation'})
        self.finish(task)
        self.assertEqual(before, self.rows('tasks')[0])
        self.assertEqual([], self.rows('outbox'))

    def test_duplicate_original_input_and_old_outbox_are_never_backfilled_or_recovered(self):
        identity = self.receive()
        self.store.enqueue('old-unknown', 'original text')
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET status='submitting' WHERE id='old-unknown'")
        old = self.rows('outbox')
        second = self.store_class(self.store.path, clock=lambda: self.now)
        self.assertEqual(0, second.ingest([self.message()], 'later-cursor', 'owner', 'bot'))
        self.assertEqual(1, len(self.rows('tasks')))
        self.assertEqual(old, self.rows('outbox'))
        self.assertEqual(identity, self.rows('inbox')[0]['id'])
        with self.assertRaisesRegex(Conflict, 'original_message_changed'):
            second.ingest([self.message(text='changed same message ID')], 'bad', 'owner', 'bot')

    def test_explicit_model_reply_lossless_times_and_unknown_cannot_get_new_send_identity(self):
        original = self.message(900719925474099312345)
        event_id = self.receive(original)
        task = self.store.claim('entry')
        history = self.action(task, 'history', {})
        metadata = history['items'][0]['metadata']
        self.assertEqual(str(original['message_id']), metadata['message_id'])
        self.assertEqual('1792191000000', metadata['original_time']['raw'])
        self.assertNotEqual(metadata['received_at'], metadata['original_time']['utc'])
        reply = self.action(task, 'reply', {'text': '在，怎么啦？', 'observed_latest_message_id': event_id})
        self.assertEqual('pending', reply['status'])
        row = self.store.next_send()
        self.store.finish_send(row['id'], 'unknown', {'delivery_verified': False})
        before = self.rows('outbox')
        with self.assertRaisesRegex(Conflict, 'original_reply_unknown'):
            self.action(task, 'reply', {'text': 'retry?', 'reply_key': 'another',
                'observed_latest_message_id': event_id}, identity='different-call')
        self.assertEqual(before, self.rows('outbox'))
        status = self.action(task, 'status', {'event_id': event_id})
        self.assertEqual('unknown', status['send']['status'])
        self.assertIsNone(status['send']['sent_at'])
        self.assertFalse(status['send']['delivery_verified'])
        self.assertEqual(self.now, status['send']['send_observation']['attempted_at'])
        self.finish(task)
        self.assertEqual(1, len(self.rows('outbox')))  # no native-final echo

    def test_newer_message_forces_context_refresh_not_stale_previous_reply(self):
        first = self.receive()
        task = self.store.claim('entry')
        self.now += 1  # the original turn's live lease still belongs to entry
        newer = self.receive(dict(self.message('tomorrow', '现在呢'), create_time_ms=int(self.now * 1000)))
        with self.assertRaisesRegex(Conflict, 'newer_message_requires_context_read'):
            self.action(task, 'reply', {'text': 'old response', 'observed_latest_message_id': first})
        history = self.action(task, 'history', {}, identity='fresh-history')
        self.assertEqual(newer, history['latest_inbound_event_id'])
        self.action(task, 'reply', {'text': '在，看到了你今天的新消息。',
            'observed_latest_message_id': newer}, identity='current-reply')
        self.assertEqual(1, len(self.rows('outbox')))

    def test_handoff_is_explicit_same_original_id_verified_pending_not_execution(self):
        event_id = self.receive()
        task = self.store.claim('entry')
        self.assertEqual(1, len(self.rows('tasks')))
        result = self.action(task, 'handoff', {'input': 'owner-understood authorized project'})
        self.assertEqual(event_id, result['handoff']['id'])
        self.assertEqual('pending', result['handoff']['status'])
        self.assertFalse(result['execution_verified'])
        again = self.action(task, 'handoff', {'input': 'owner-understood authorized project'})
        self.assertEqual(result, again)
        self.assertEqual(2, len(self.rows('tasks')))
        leader = self.store.claim('leader')
        self.assertEqual(event_id, leader['id'])
        self.assertEqual('leader:owner', leader['scope'])
        self.assertIsNone(leader['session'])
        self.assertEqual(task['id'], leader['context']['origin']['entrance_task_id'])

    def test_automatic_old_task_result_routes_to_agent_direct_leader_claw_still_available(self):
        self.store.create_task('project', task_id='work')
        work = self.store.claim('leader')
        self.store.update_task('work', 'leader', work['epoch'], result='yesterday project finished', status='completed')
        self.assertEqual([], self.rows('outbox'))
        notice = self.store.claim('entry')
        self.assertIn('notification', notice['input'])
        self.action(notice, 'silent', {'reason': 'old result is unrelated to the new greeting'})
        self.finish(notice)
        self.assertEqual(1, len(self.rows('entrance_events')))
        self.assertEqual([], self.rows('outbox'))
        self.store.enqueue('authorized-direct', 'Leader direct owner communication')
        self.assertEqual('authorized-direct', self.rows('outbox')[0]['id'])

    def test_media_materializes_shared_adapter_bytes_without_exposing_raw_reference(self):
        identity = self.receive(self.message('image', items=[{'type': 2, 'image_item': {
            'media': {'aes_key': 'private-aes', 'encrypt_query_param': 'private-cdn'}}}]))
        task = self.store.claim('entry')
        history = self.action(task, 'history', {})
        attachment = history['items'][0]['attachments'][0]['attachment_id']
        receipt = self.action(task, 'media', {'attachment_id': attachment})
        self.assertEqual(b'complete-original-media', Path(receipt['path']).read_bytes())
        self.assertFalse(receipt['understanding_verified'])
        self.assertEqual(identity, receipt['message']['source_ref'])
        self.assertNotIn('private-aes', json.dumps(history))
        self.assertNotIn('private-cdn', json.dumps(receipt))

    def test_history_reuses_new_attachment_id_and_exposes_old_authorized_media_without_reingest(self):
        old_message = self.message('old-image', items=[{'type': 2, 'image_item': {
            'media': {'aes_key': 'private-aes', 'encrypt_query_param': 'private-cdn'}}}])
        with self.store.transaction() as db:
            db.execute('INSERT INTO inbox VALUES(?,?,?)',
                (message_identity(old_message), json.dumps(old_message), self.now - 86400))
        self.receive(self.message('new-image', items=old_message['item_list']))
        task = self.store.claim('entry')
        current_id = self.rows('entrance_attachments')[0]['attachment_id']
        history = self.action(task, 'history', {'source': 'inbox'})
        self.assertEqual(current_id, history['items'][1]['attachments'][0]['attachment_id'])
        historical_id = history['items'][0]['attachments'][0]['attachment_id']
        receipt = self.action(task, 'media', {'attachment_id': historical_id})
        self.assertEqual(b'complete-original-media', Path(receipt['path']).read_bytes())
        self.assertEqual(1, len(self.rows('tasks')))  # old media was read, never resubmitted

    def test_same_scope_next_message_carries_same_complete_native_session(self):
        first = self.receive()
        task = self.store.claim('entry')
        state = {'thread_id': THREAD, 'harness': 'codex', 'side_effect_started': False}
        complete = b'original native complete bytes\n'
        self.store.session_action(task['id'], 'entry', task['epoch'], 'upload', {
            'harness': 'codex', 'part': 0, 'data': base64.b64encode(gzip.compress(complete)).decode()})
        self.store.session_action(task['id'], 'entry', task['epoch'], 'commit',
            {'harness': 'codex', 'state': state, 'parts': 1})
        self.action(task, 'silent', {'reason': 'no useful response'})
        self.finish(task)
        self.receive(self.message('next', '继续我们刚才的话题'))
        second = self.store.claim('entry')
        self.assertNotEqual(task['id'], second['id'])
        self.assertEqual(task['scope'], second['scope'])
        self.assertEqual(state, second['sessions']['codex']['state'])
        part = self.store.session_action(second['id'], 'entry', second['epoch'], 'download', {'part': 0})
        self.assertEqual(complete, gzip.decompress(base64.b64decode(part['data'])))
        self.assertEqual(1, len(self.rows('native_sessions')))

    def failed_task(self, save_receipt=True):
        identity = self.receive()
        task = self.store.claim('entry')
        proof = {'protocol': FAILURE_PROTOCOL, 'thread_id': THREAD, 'turn_id': 'failed-turn',
            'terminal_turn_status': 'failed', 'owned_process_reaped': True, 'owned_reader_eof': True,
            'runtime_pid': 12345, 'history': {'size': 50, 'sha256': 'b' * 64, 'records': 2,
            'thread_id': THREAD}, 'external_effects_settled': False}
        checkpoint = {'thread_id': THREAD, 'turn_id': 'failed-turn', 'side_effect_started': True}
        self.store.update_task(task['id'], 'entry', task['epoch'], checkpoint=checkpoint)
        state = dict(checkpoint, harness='codex', codex_auth_home='/private/original163',
                     entrance_closed_failure=proof)
        self.store.session_action(task['id'], 'entry', task['epoch'], 'upload', {
            'harness': 'codex', 'part': 0, 'data': base64.b64encode(gzip.compress(b'complete failed history')).decode()})
        self.store.session_action(task['id'], 'entry', task['epoch'], 'commit',
            {'harness': 'codex', 'state': state, 'parts': 1})
        if save_receipt:
            self.action(task, 'record_failure', {'proof': proof})
        self.store.update_task(task['id'], 'entry', task['epoch'], result='native failure', status='waiting_backend')
        return task

    def test_guarded_failed_entry_preserved_but_fresh_input_after_saved_closed_boundary_uses_same_thread(self):
        old = self.failed_task()
        self.now += 100
        self.store.heartbeat('entry', [self.policy['capability']])
        fresh = self.receive(self.message('fresh', '先别重做刚才的发送，告诉我现在怎样'))
        current = self.store.claim('entry')
        self.assertEqual('entrance-' + fresh, current['id'])
        self.assertEqual(THREAD, current['session']['state']['thread_id'])
        old_row = next(row for row in self.rows('tasks') if row['id'] == old['id'])
        self.assertEqual('needs_review', old_row['status'])
        self.assertTrue(json.loads(old_row['checkpoint'])['side_effect_started'])
        self.assertEqual(1, old_row['attempts'])
        self.assertFalse(current['context']['unsettled_previous_operations'][0]['external_effects_settled'])

    def test_lost_terminal_eof_or_history_receipt_never_permits_unknown_retry(self):
        old = self.failed_task(save_receipt=False)
        self.now += 100
        self.store.heartbeat('entry', [self.policy['capability']])
        self.receive(self.message('fresh', 'new input remains safely archived'))
        self.assertIsNone(self.store.claim('entry'))
        self.assertEqual(2, len(self.rows('inbox')))
        self.assertEqual(1, next(row for row in self.rows('tasks') if row['id'] == old['id'])['attempts'])
        self.assertEqual([], self.rows('outbox'))

    def test_current_store_adapter_has_same_no_direct_leader_behavior(self):
        current = entrance_store_class(Store, POLICY)(self.root / 'current.sqlite')
        self.assertEqual(1, current.ingest([self.message()], 'cursor', 'owner', 'bot'))
        with current.transaction() as db:
            self.assertEqual([self.policy['capability']], json.loads(db.execute('SELECT required FROM tasks').fetchone()[0]))
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])


class EntranceFailureAndRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rollout = self.root / 'original.jsonl'
        self.prefix = (json.dumps({'type': 'session_meta', 'payload': {'id': THREAD}}) + '\n').encode()
        self.failed = (json.dumps({'type': 'event_msg', 'payload': {'type': 'turn_failed', 'id': 'original-turn'}}) + '\n').encode()
        self.rollout.write_bytes(self.prefix + self.failed)
        self.rollout.chmod(0o600)

    def test_exact_full_failed_history_and_prefix_reject_partial_changed_or_wrong_thread(self):
        history = history_snapshot(self.rollout, THREAD)
        self.assertEqual(hashlib.sha256(self.prefix + self.failed).hexdigest(), history['sha256'])
        self.assertEqual(2, history['records'])
        self.rollout.write_bytes(self.prefix + self.failed + b'{"type":"later"}\n')
        grown = history_snapshot(self.rollout, THREAD, [history])
        self.assertGreater(grown['size'], history['size'])
        self.rollout.write_bytes(self.prefix + self.failed.replace(b'failed', b'BROKEN'))
        with self.assertRaisesRegex(ValueError, 'prefix_changed'):
            history_snapshot(self.rollout, THREAD, [history])
        self.rollout.write_bytes(self.prefix + b'{"type":"partial"}')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            history_snapshot(self.rollout, THREAD)
        with self.assertRaisesRegex(ValueError, 'thread_changed'):
            history_snapshot(self.rollout, 'different-thread')

    def test_closed_adapter_needs_native_terminal_owned_reap_and_actual_reader_return(self):
        class Native:
            def _read(inner):
                return None
            def rpc(inner, method, params):
                return {'thread': {'id': THREAD, 'path': str(self.rollout)}}
            def goal(inner):
                return None
            def native_rollout(inner):
                return self.rollout
            def __exit__(inner, *args):
                inner.process.poll.return_value = 0
        reports = []
        subclass = closed_failure_backend(Native, lambda *args: reports.append(args), lambda: [])
        for error, terminal, eof in (('codex_turn_timeout', True, True),
                ('codex_turn_failed', False, True), ('codex_turn_failed', True, False),
                ('codex_turn_failed', True, True)):
            runtime = subclass.__new__(subclass)
            runtime._entrance_reader_eof = mock.Mock()
            runtime._entrance_reader_eof.wait.return_value = eof
            runtime._entrance_failure_saved = False
            runtime.process = mock.Mock(pid=12345)
            runtime.thread_id, runtime.turn_id = THREAD, 'original-turn'
            runtime.native_observations = {'turn_completed': {'threadId': THREAD, 'turn': {
                'id': 'original-turn', 'status': 'failed'}}} if terminal else {}
            runtime.__exit__(ValueError, ValueError(error), None)
        self.assertEqual(1, len(reports))
        self.assertEqual(hashlib.sha256(self.rollout.read_bytes()).hexdigest(), reports[0][2]['history']['sha256'])
        self.assertFalse(reports[0][2]['external_effects_settled'])

    def test_native_resume_full_failed_prefix_checked_before_any_new_turn_and_no_fork(self):
        history = history_snapshot(self.rollout, THREAD)
        calls = []
        class Native:
            def rpc(inner, method, params):
                calls.append(method)
                return {'thread': {'id': THREAD, 'path': str(self.rollout)}}
        adapter = closed_failure_backend(Native, mock.Mock(), lambda: [{'thread_id': THREAD, 'history': history}])
        runtime = adapter.__new__(adapter)
        runtime.rpc('thread/resume', {})
        self.assertEqual(['thread/resume'], calls)
        with self.assertRaisesRegex(ValueError, 'replacement_forbidden'):
            runtime.rpc('thread/start', {})
        self.rollout.write_bytes(self.prefix)
        with self.assertRaisesRegex(ValueError, 'prefix_invalid'):
            runtime.rpc('thread/resume', {})
        self.assertNotIn('turn/start', calls)

    def test_worker_has_distinct_native_role_config_and_existing_mesh_tool_route(self):
        client = mock.Mock()
        config = {'node_id': 'entry', 'entrance': POLICY,
            'capabilities': [validate_policy(POLICY)['capability']],
            'codex': {'auth_home': '/private/explicit163', 'strict_auth_home': True,
                      'workspace': str(self.root), 'model': 'gpt-6-luna'}}
        worker = EntranceWorker(config, client=client)
        self.assertNotIn('instructions', config['codex'])
        self.assertIn('不是Mesh Leader', worker.config['codex']['instructions'])
        worker.current = {'id': 'original-task', 'epoch': 3}
        worker.tick = mock.Mock()
        client.request.return_value = {'status': 'pending', 'delivery_verified': False}
        output = worker.on_tool({'tool': 'mesh', 'callId': 'native-call',
            'arguments': {'action': 'entrance', 'arguments': {'action': 'silent',
                'arguments': {'reason': 'no useful update'}}}})
        self.assertTrue(output['success'])
        client.request.assert_called_once_with('/v1/agent/action', {
            'task_id': 'original-task', 'epoch': 3, 'call_id': 'original-task:native-call',
            'action': 'entrance_silent', 'arguments': {'reason': 'no useful update'}})
        bad = dict(config, capabilities=['leader', validate_policy(POLICY)['capability']])
        with self.assertRaisesRegex(ValueError, 'capabilities_required'):
            EntranceWorker(bad, client=client)
        config['codex']['goal'] = {'objective': 'old Leader goal'}
        with self.assertRaisesRegex(ValueError, 'inherit_leader_goal'):
            EntranceWorker(config, client=client)

    def test_real_shared_media_config_uses_owner_fixed_python_not_model_supplied_module(self):
        # Default is explicitly no network/downloader; configuring the fixed
        # SDK launcher is separately source-pinned by the shared adapter.
        media = media_from_config({'entrance_media': {'root': str(self.root / 'media')}})
        self.assertIsInstance(media, EntranceMedia)
        self.assertIsNone(media.downloader)


if __name__ == '__main__':
    unittest.main()
