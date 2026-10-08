import json
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.store import Store, Conflict


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.store = Store(Path(self.temp.name) / 'db', clock=lambda: self.now)
        self.store.heartbeat('n', ['leader'])
        self.store.create_task('plan')
        self.task = self.store.claim('n')
        self.params = {'questions': [{'id': 'choice', 'question': 'Which project?', 'header': 'Project'}]}

    def tearDown(self):
        self.temp.cleanup()

    def message(self, identifier, text):
        return {'message_id': identifier, 'from_user_id': 'owner', 'to_user_id': 'bot', 'message_type': 1,
                'item_list': [{'type': 1, 'text_item': {'text': text}}]}

    def test_question_reply_returns_native_answer_not_chat_input(self):
        self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'question-id', 'question', self.params)
        message = {'message_id': 'reply', 'from_user_id': 'owner', 'to_user_id': 'bot', 'message_type': 1,
                   'item_list': [{'type': 1, 'text_item': {'text': '/answer question-id security study'}}]}
        self.store.ingest([message], 'cursor', 'owner', 'bot')
        answer = self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'question-id')['answer']
        self.assertEqual({'answers': {'choice': {'answers': ['security study']}}}, answer)
        self.assertEqual({'running': 1}, self.store.status()['tasks'])

    def test_stale_request_cannot_accept_approval(self):
        self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'approve-id', 'approval', {'command': 'do something'})
        self.now += 91
        with self.assertRaises(Conflict):
            self.store.resolve_interaction('approve-id', {'decision': 'accept'})

    def test_approval_never_grants_future_session_permission(self):
        self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'approve-id', 'approval', {})
        with self.assertRaises(ValueError):
            self.store.resolve_interaction('approve-id', {'decision': 'acceptForSession'})
        self.store.resolve_interaction('approve-id', {'decision': 'decline'})
        with self.assertRaises(Conflict):
            self.store.resolve_interaction('approve-id', {'decision': 'accept'})

    def test_secret_question_does_not_enter_message_outbox(self):
        self.params['questions'][0]['isSecret'] = True
        with self.assertRaises(ValueError):
            self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'secret', 'question', self.params)
        self.assertEqual({}, self.store.status()['outbox'])

    def test_wrong_question_id_rejected(self):
        self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'question-id', 'question', self.params)
        with self.assertRaises(ValueError):
            self.store.resolve_interaction('question-id', {'answers': {'wrong': {'answers': ['yes']}}})

    def test_malformed_answers_do_not_poison_poll_batch_or_advance_question(self):
        params = {'questions': [{'id': 'project', 'question': 'Which project?'},
                                {'id': 'method', 'question': 'Which method?'}]}
        self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'question-id', 'question', params)
        invalid_answers = [[], {'answers': {'wrong': {'answers': ['yes']}}},
                           {'answers': {'project': {'answers': []}, 'method': {'answers': ['yes']}}}]
        messages = [self.message('typo-' + str(i), '/answer question-id ' + json.dumps(answer))
                    for i, answer in enumerate(invalid_answers)]
        messages.append(self.message('next-task', 'Following independent task'))
        self.store.ingest(messages, 'after-typos', 'owner', 'bot')
        self.assertEqual('after-typos', self.store.get('cursor'))
        self.assertEqual(4, len(self.store.inbox()['items']))
        self.assertEqual({'running': 1, 'pending': 1}, self.store.status()['tasks'])
        self.assertIsNone(self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'question-id')['answer'])
        # The invalid inputs commit once; polling the same batch is a no-op.
        original_outbox = self.store.status()['outbox']
        self.store.ingest(messages, 'after-replay', 'owner', 'bot')
        self.assertEqual(original_outbox, self.store.status()['outbox'])
        valid = {'answers': {'project': {'answers': ['security']}, 'method': {'answers': ['research']}}}
        self.store.ingest([self.message('correction', '/answer question-id ' + json.dumps(valid))],
                          'after-correction', 'owner', 'bot')
        self.assertEqual(valid, self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'question-id')['answer'])

    def test_broken_answers_json_does_not_discard_later_answer(self):
        params = {'questions': [{'id': 'one', 'question': 'First?'}, {'id': 'two', 'question': 'Second?'}]}
        self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'question-id', 'question', params)
        answer = {'answers': {'one': {'answers': ['a']}, 'two': {'answers': ['b']}}}
        messages = [self.message('bad-json', '/answer question-id {broken'),
                    self.message('valid-json', '/answer question-id ' + json.dumps(answer))]
        self.store.ingest(messages, 'after-both', 'owner', 'bot')
        self.assertEqual(answer, self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'question-id')['answer'])
        self.assertEqual(2, len(self.store.inbox()['items']))

    def test_expired_leader_cannot_receive_approval_with_live_task_deadline(self):
        self.store.interaction(self.task['id'], 'n', self.task['epoch'], 'approve-id', 'approval', {'command': 'one operation'})
        self.now += 80
        # Task progress alone can extend its lease but cannot elect/renew Leader.
        self.store.update_task(self.task['id'], 'n', self.task['epoch'])
        self.now += 11
        with self.assertRaises(Conflict):
            self.store.resolve_interaction('approve-id', {'decision': 'accept'})
        self.store.ingest([self.message('old-approval', '/approve approve-id')], 'after-expiry', 'owner', 'bot')
        with self.store.transaction() as db:
            self.assertIsNone(db.execute('SELECT answer FROM interactions WHERE id=?', ('approve-id',)).fetchone()['answer'])
        self.assertEqual('after-expiry', self.store.get('cursor'))


if __name__ == '__main__':
    unittest.main()
