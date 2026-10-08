import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.store import Store, Conflict
from assistant_mesh import sessions


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.store = Store(Path(self.temp.name) / 'db', clock=lambda: self.now)
        self.store.heartbeat('n', ['leader', 'agent'])
        self.store.create_task('first')
        self.task = self.store.claim('n')

    def tearDown(self):
        self.temp.cleanup()

    def request(self, path, value):
        return self.store.session_action(value['task_id'], 'n', value['epoch'], value['action'], value['payload'])

    def test_leader_native_thread_continues_in_next_task(self):
        state = {'thread_id': 'native-id', 'harness': 'codex'}
        sessions.save(self, self.task, 'n', 'codex', state)
        self.store.update_task(self.task['id'], 'n', self.task['epoch'], status='completed')
        self.store.create_task('second')
        second = self.store.claim('n')
        self.assertEqual('native-id', second['session']['state']['thread_id'])
        self.assertEqual('leader:owner', second['scope'])

    def test_scope_does_not_allow_concurrent_native_turns(self):
        self.store.create_task('second')
        self.assertIsNone(self.store.claim('n'))

    def test_child_sessions_reuse_same_project_and_role(self):
        args = {'input': 'child', 'project_id': 'security-study', 'agent_id': 'researcher'}
        for key in ('one', 'two'):
            self.store.agent_action(self.task['id'], 'n', self.task['epoch'], key, 'delegate', args)
        child = self.store.claim('n')
        self.assertEqual('project:security-study:researcher', child['scope'])
        self.assertIsNone(self.store.claim('n'))

    def test_native_artifact_roundtrip_without_prompt_summarization(self):
        original = Path(self.temp.name) / 'native.jsonl'
        identity = '01a116fc-8aae-7001-a0e2-07a1073c5bcb'
        data = self.native_data(identity) + b'{"type":"compacted","native":true}\n' * 20000
        original.write_bytes(data)
        self.task['checkpoint'] = {'thread_id': identity, 'harness': 'codex'}
        sessions.save(self, self.task, 'n', 'codex', {'thread_id': identity}, original)
        restored = sessions.restore(self, self.task, Path(self.temp.name) / 'sessions')
        self.assertEqual(data, Path(restored).read_bytes())
        self.assertEqual(0o600, Path(restored).stat().st_mode & 0o777)
        self.assertEqual('rollout-2026-10-07T12-13-14-' + identity + '.jsonl', Path(restored).name)
        self.assertEqual(data, Path(sessions.restore(self, self.task, Path(self.temp.name) / 'sessions')).read_bytes())

    def native_data(self, identity='01a116fc-8aae-7001-a0e2-07a1073c5bcb'):
        return (json.dumps({'type': 'session_meta', 'payload': {'id': identity,
            'timestamp': '2026-10-07T12:13:14.123Z', 'history_mode': 'paginated'}}) + '\n').encode()

    def prepare_artifact(self, data=None):
        identity = '01a116fc-8aae-7001-a0e2-07a1073c5bcb'
        self.task['checkpoint'] = {'thread_id': identity, 'harness': 'codex'}
        original = Path(self.temp.name) / 'selected.jsonl'
        original.write_bytes(data or self.native_data())
        sessions.save(self, self.task, 'n', 'codex', {'thread_id': identity}, original)
        return Path(self.temp.name) / 'sessions'

    def test_selected_id_and_metadata_must_match(self):
        folder = self.prepare_artifact(self.native_data('01a116fc-8aae-7001-a0e2-07a1073c5bcc'))
        with self.assertRaisesRegex(ValueError, 'thread_mismatch'):
            sessions.restore(self, self.task, folder)
        self.assertEqual([], list(folder.rglob('*.jsonl')))

    def test_metadata_and_selected_identity_fail_closed(self):
        folder = self.prepare_artifact(b'{"type":"session_meta","payload":{"id":"bad"}}\n')
        self.task['checkpoint'] = {'thread_id': 'bad'}
        with self.assertRaisesRegex(ValueError, 'selected_thread_required'):
            sessions.restore(self, self.task, folder)
        self.store.update_task(self.task['id'], 'n', self.task['epoch'], status='completed')
        self.store.create_task('next validation')
        self.task = self.store.claim('n')
        self.task.pop('session', None)
        self.task.pop('sessions', None)
        self.task['checkpoint']['thread_id'] = '01a116fc-8aae-7001-a0e2-07a1073c5bcb'
        folder = self.prepare_artifact(b'{"type":"unexpected"}\n')
        with self.assertRaisesRegex(ValueError, 'metadata_invalid'):
            sessions.restore(self, self.task, folder)

    def test_import_cannot_overwrite_newer_native_history(self):
        folder = self.prepare_artifact()
        destination = Path(sessions.restore(self, self.task, folder))
        newer = destination.read_bytes() + b'{"type":"new_native_turn"}\n'
        destination.write_bytes(newer)
        with self.assertRaisesRegex(ValueError, 'destination_conflict'):
            sessions.restore(self, self.task, folder)
        self.assertEqual(newer, destination.read_bytes())

    def test_import_refuses_symlink_destination_and_wrong_root(self):
        folder = self.prepare_artifact()
        destination = Path(sessions.restore(self, self.task, folder))
        target = Path(self.temp.name) / 'untouched.jsonl'
        target.write_bytes(b'untouched')
        destination.unlink()
        destination.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'destination_unsafe'):
            sessions.restore(self, self.task, folder)
        self.assertEqual(b'untouched', target.read_bytes())
        with self.assertRaisesRegex(ValueError, 'sessions_directory_required'):
            sessions.restore(self, self.task, Path(self.temp.name) / 'random-import')

    def test_restore_requires_absolute_paths_for_both_harnesses(self):
        self.prepare_artifact()
        for harness, folder in (('codex', Path('sessions')), ('pi', Path('pi-native'))):
            with self.assertRaisesRegex(ValueError, 'restore_absolute_path_required'):
                sessions.restore(self, self.task, folder, harness=harness)
        folder = Path(self.temp.name) / '..' / Path(self.temp.name).name / 'sessions'
        with self.assertRaisesRegex(ValueError, 'restore_absolute_path_required'):
            sessions.restore(self, self.task, folder)

    def test_restore_rejects_symlink_auth_home_ancestor_before_writing(self):
        self.prepare_artifact()
        actual = Path(self.temp.name) / 'actual-auth-home'
        actual.mkdir(mode=0o700)
        alias = Path(self.temp.name) / 'linked-auth-home'
        alias.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'restore_ancestor_symlink'):
            sessions.restore(self, self.task, alias / 'sessions')
        self.assertFalse((actual / 'sessions').exists())

    def test_restore_rejects_symlink_date_directory_and_cleans_stage(self):
        folder = self.prepare_artifact()
        folder.mkdir(mode=0o700)
        target = Path(self.temp.name) / 'unrelated-date-directory'
        target.mkdir(mode=0o700)
        (folder / '2026').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'directory_unsafe'):
            sessions.restore(self, self.task, folder)
        self.assertEqual([], list(target.iterdir()))
        self.assertEqual([], list(folder.glob('native-rollout-*')))

    def test_restore_rejects_nonsticky_writable_ancestor_without_chmod(self):
        self.prepare_artifact()
        ancestor = Path(self.temp.name) / 'unsafe-shared'
        ancestor.mkdir(mode=0o700)
        for mode in (0o777, 0o775, 0o770):
            ancestor.chmod(mode)
            try:
                with self.assertRaisesRegex(ValueError, 'restore_ancestor_unsafe'):
                    sessions.restore(self, self.task, ancestor / 'new-auth-home' / 'sessions')
                self.assertEqual(mode, ancestor.stat().st_mode & 0o7777)
                self.assertFalse((ancestor / 'new-auth-home').exists())
            finally:
                ancestor.chmod(0o700)

    def test_restore_accepts_sticky_shared_ancestor_and_private_child(self):
        self.prepare_artifact()
        ancestor = Path(self.temp.name) / 'sticky-shared'
        ancestor.mkdir(mode=0o700)
        ancestor.chmod(0o1777)
        auth_home = ancestor / 'owner-private-auth'
        auth_home.mkdir(mode=0o700)
        restored = Path(sessions.restore(self, self.task, auth_home / 'sessions'))
        self.assertEqual(self.native_data(), restored.read_bytes())
        self.assertEqual(0o1777, ancestor.stat().st_mode & 0o7777)
        self.assertEqual(0o700, auth_home.stat().st_mode & 0o7777)
        self.assertEqual(0o600, restored.stat().st_mode & 0o7777)

    def test_restore_preserves_existing_0755_native_directories(self):
        self.prepare_artifact()
        auth_home = Path(self.temp.name) / 'existing-auth-home'
        folder = auth_home / 'sessions'
        folder.mkdir(mode=0o755, parents=True)
        auth_home.chmod(0o755)
        folder.chmod(0o755)
        restored = Path(sessions.restore(self, self.task, folder))
        self.assertEqual(0o755, auth_home.stat().st_mode & 0o7777)
        self.assertEqual(0o755, folder.stat().st_mode & 0o7777)
        self.assertEqual(0o600, restored.stat().st_mode & 0o7777)

    def test_restore_rejects_foreign_owned_ancestor_even_if_not_writable(self):
        self.prepare_artifact()
        ancestor = Path(self.temp.name) / 'foreign-controlled'
        ancestor.mkdir(mode=0o755)
        original = Path.lstat
        def foreign_owner(path):
            metadata = original(path)
            if path == ancestor:
                fields = list(metadata)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return metadata
        with patch('assistant_mesh.sessions.Path.lstat', foreign_owner):
            with self.assertRaisesRegex(ValueError, 'restore_ancestor_unsafe'):
                sessions.restore(self, self.task, ancestor / 'new-auth-home' / 'sessions')
        self.assertFalse((ancestor / 'new-auth-home').exists())

    def test_pi_opaque_native_format_is_not_rewritten_as_codex(self):
        original = Path(self.temp.name) / 'pi.jsonl'
        original.write_bytes(b'opaque pi native record\n')
        sessions.save(self, self.task, 'n', 'pi', {'thread_id': 'pi-native'}, original)
        restored = sessions.restore(self, self.task, Path(self.temp.name) / 'pi-native', harness='pi')
        self.assertEqual(original.read_bytes(), Path(restored).read_bytes())

    def test_incomplete_upload_does_not_replace_good_native_session(self):
        sessions.save(self, self.task, 'n', 'codex', {'thread_id': 'good'})
        with self.assertRaises(Conflict):
            self.request('/v1/session', {'task_id': self.task['id'], 'epoch': self.task['epoch'], 'action': 'commit',
                'payload': {'harness': 'codex', 'state': {'thread_id': 'bad'}, 'parts': 1}})
        self.store.update_task(self.task['id'], 'n', self.task['epoch'], status='completed')
        self.store.create_task('next')
        self.assertEqual('good', self.store.claim('n')['session']['state']['thread_id'])

    def test_plan_and_goal_are_native_task_context(self):
        message = {'message_id': 'goal', 'from_user_id': 'owner', 'to_user_id': 'bot', 'message_type': 1,
                   'item_list': [{'type': 1, 'text_item': {'text': '/goal finish research'}}]}
        self.store.ingest([message], 'cursor', 'owner', 'bot')
        self.store.update_task(self.task['id'], 'n', self.task['epoch'], status='completed')
        self.assertEqual('finish research', self.store.claim('n')['context']['goal']['objective'])

    def test_codex_and_pi_artifacts_are_isolated_in_same_task_epoch(self):
        values = {'codex': b'Codex native rollout', 'pi': b'Pi native rollout'}
        for harness, data in values.items():
            self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'upload',
                {'harness': harness, 'part': 0, 'data': base64.b64encode(data).decode()})
            self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'commit',
                {'harness': harness, 'state': {'thread_id': harness + '-thread'}, 'parts': 1})
        for harness, data in values.items():
            value = self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'download',
                {'harness': harness, 'part': 0})
            self.assertEqual(data, base64.b64decode(value['data']))
        self.store.update_task(self.task['id'], 'n', self.task['epoch'], status='completed')
        self.store.create_task('next')
        following = self.store.claim('n')
        self.assertEqual({'codex', 'pi'}, set(following['sessions']))
        self.assertNotEqual(following['sessions']['codex']['artifact'], following['sessions']['pi']['artifact'])
        self.assertEqual('codex-thread', following['session']['state']['thread_id'])

    def test_pi_cannot_commit_codex_uploaded_parts(self):
        self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'upload',
            {'harness': 'codex', 'part': 0, 'data': base64.b64encode(b'codex').decode()})
        with self.assertRaises(Conflict):
            self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'commit',
                {'harness': 'pi', 'state': {'thread_id': 'pi-thread'}, 'parts': 1})

    def test_scope_fence_applies_across_nodes_without_leader_requirement(self):
        self.store.heartbeat('cloud', ['agent'])
        context = {'session_scope': 'project:security:researcher'}
        first = self.store.create_task('one', ['agent'], context=context)
        self.store.create_task('two', ['agent'], context=context)
        child = self.store.claim('n')
        self.assertEqual(first, child['id'])
        self.assertIsNone(self.store.claim('cloud'))
        self.store.session_action(child['id'], 'n', child['epoch'], 'commit',
            {'harness': 'codex', 'state': {'thread_id': 'project-native'}, 'parts': 0})
        self.store.update_task(child['id'], 'n', child['epoch'], status='completed')
        following = self.store.claim('cloud')
        self.assertEqual('project-native', following['sessions']['codex']['state']['thread_id'])
        self.assertEqual('n', following['sessions']['codex']['node'])

    def test_native_session_download_cannot_cross_project_scope(self):
        data = b'private leader session'
        self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'upload',
            {'harness': 'codex', 'part': 0, 'data': base64.b64encode(data).decode()})
        self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'commit',
            {'harness': 'codex', 'state': {'thread_id': 'leader-native'}, 'parts': 1})
        self.store.create_task('other project', ['agent'], context={'session_scope': 'project:other:researcher'})
        other = self.store.claim('n')
        self.assertEqual({}, other['sessions'])
        with self.assertRaises(ValueError):
            self.store.session_action(other['id'], 'n', other['epoch'], 'download', {'harness': 'codex', 'part': 0})

    def test_stale_worker_cannot_commit_or_download_after_takeover(self):
        self.now += 91
        self.store.heartbeat('cloud', ['leader', 'agent'])
        taken = self.store.claim('cloud')
        self.assertEqual(self.task['id'], taken['id'])
        self.assertGreater(taken['epoch'], self.task['epoch'])
        for action, payload in [('upload', {'part': 0, 'data': base64.b64encode(b'stale').decode()}),
                                ('commit', {'harness': 'codex', 'state': {'thread_id': 'stale'}}),
                                ('download', {'harness': 'codex', 'part': 0})]:
            with self.assertRaises(Conflict):
                self.store.session_action(self.task['id'], 'n', self.task['epoch'], action, payload)
        self.store.session_action(taken['id'], 'cloud', taken['epoch'], 'commit',
            {'harness': 'codex', 'state': {'thread_id': 'current'}})

    def test_new_native_identity_does_not_inherit_previous_rollout(self):
        for harness in ('codex', 'pi'):
            self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'upload',
                {'harness': harness, 'part': 0, 'data': base64.b64encode(b'previous').decode()})
            self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'commit',
                {'harness': harness, 'state': {'thread_id': 'previous'}, 'parts': 1})
            self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'commit',
                {'harness': harness, 'state': {'thread_id': 'different'}, 'parts': 0})
            with self.assertRaises(ValueError):
                self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'download',
                    {'harness': harness, 'part': 0})

    def test_artifact_harness_and_part_types_are_validated(self):
        for harness in ('unknown', False, []):
            with self.assertRaises(ValueError):
                self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'upload',
                    {'harness': harness, 'part': 0, 'data': base64.b64encode(b'part').decode()})
        self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'upload',
            {'harness': 'codex', 'part': 0, 'data': base64.b64encode(b'part').decode()})
        with self.assertRaises(ValueError):
            self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'commit',
                {'harness': 'codex', 'state': {'thread_id': 'native'}, 'parts': True})
        self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'commit',
            {'harness': 'codex', 'state': {'thread_id': 'native'}, 'parts': 1})
        for part in (-1, True, '0'):
            with self.assertRaises(ValueError):
                self.store.session_action(self.task['id'], 'n', self.task['epoch'], 'download',
                    {'harness': 'codex', 'part': part})


if __name__ == '__main__':
    unittest.main()
