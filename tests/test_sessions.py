import base64
import tempfile
import unittest
from pathlib import Path

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
        data = b'{"type":"session_meta","payload":{"id":"native-id"}}\n' + b'{"type":"compacted","native":true}\n' * 20000
        original.write_bytes(data)
        sessions.save(self, self.task, 'n', 'codex', {'thread_id': 'native-id'}, original)
        restored = sessions.restore(self, self.task, Path(self.temp.name) / 'import')
        self.assertEqual(data, Path(restored).read_bytes())
        self.assertEqual(0o600, Path(restored).stat().st_mode & 0o777)

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
