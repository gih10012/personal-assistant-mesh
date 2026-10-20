"""Focused native wire checks, no auth, model inference or production state."""
import copy
import queue
import subprocess
import unittest

from assistant_mesh.codex import Codex
from assistant_mesh.reasoning import (
    ReasoningError, make_codex, observed_turn, prepare_turn, validate_selection)


CATALOG = [{'id': 'synthetic', 'model': 'synthetic-model',
            'defaultReasoningEffort': 'medium', 'supportedReasoningEfforts': [
                {'reasoningEffort': 'low'}, {'reasoningEffort': 'max'}]}]


class ReasoningTests(unittest.TestCase):
    def test_catalog_is_exact_and_omission_does_not_call_rpc(self):
        calls = []

        def rpc(method, params):
            calls.append((method, params))
            return {'data': CATALOG, 'nextCursor': None}

        original = {'threadId': 'same', 'input': [{'type': 'text', 'text': 'same input'}],
                    'collaborationMode': {'mode': 'plan', 'settings': {
                        'model': 'synthetic-model', 'developer_instructions': None}}}
        omitted, receipt = prepare_turn(rpc, original, 'synthetic-model')
        self.assertEqual(original, omitted)
        self.assertIsNone(receipt)
        self.assertEqual([], calls)
        selected, receipt = prepare_turn(rpc, original, 'synthetic-model', 'max')
        self.assertEqual('max', selected['effort'])
        self.assertEqual('max', selected['collaborationMode']['settings']['reasoning_effort'])
        self.assertEqual('plan', selected['collaborationMode']['mode'])
        self.assertNotIn('effort', original)
        self.assertEqual('prepared', receipt['status'])
        self.assertFalse(receipt['effective_verified'])
        with self.assertRaisesRegex(ReasoningError, 'effort_unsupported'):
            validate_selection(CATALOG, 'synthetic-model', 'high')
        with self.assertRaisesRegex(ReasoningError, 'model_unavailable'):
            validate_selection(CATALOG, 'other-model', 'max')

    def test_modern_start_sends_mode_and_top_level_and_preserves_input(self):
        agent = Codex.__new__(Codex)
        agent.config = {'workspace': '/synthetic', 'model': 'synthetic-model',
                        'reasoning_effort': 'max'}
        agent.on_activity = None
        agent.tools = []
        calls = []

        def rpc(method, params, **kwargs):
            calls.append((method, copy.deepcopy(params)))
            if method == 'thread/start':
                return {'thread': {'id': 'original', 'model': 'synthetic-model'}}
            if method == 'model/list':
                return {'data': CATALOG}
            if method == 'turn/start':
                return {'turn': {'id': 'actual-turn', 'status': 'inProgress'}}
            return {}

        agent.rpc = rpc
        agent.deferred, agent.events = [], queue.Queue()
        result = agent.start('new authorized input')
        turn = next(params for method, params in calls if method == 'turn/start')
        self.assertEqual('original', turn['threadId'])
        self.assertEqual([{'type': 'text', 'text': 'new authorized input'}], turn['input'])
        self.assertEqual('max', turn['effort'])
        self.assertEqual('max', turn['collaborationMode']['settings']['reasoning_effort'])
        self.assertEqual('accepted', result['native_lifecycle']['reasoning_selection']['status'])
        self.assertEqual('actual-turn', result['native_lifecycle']['reasoning_selection']['turn_id'])

    def test_original_3198_start_with_overlay_updates_only_next_explicit_turn(self):
        source = subprocess.check_output([
            'git', 'show', '3198c877234a8fd0c6912fdbe218f47185effedc:assistant_mesh/codex.py'])
        namespace = {'__name__': 'assistant_mesh._reasoning_legacy_fixture',
                     '__package__': 'assistant_mesh'}
        exec(compile(source, '<exact-original-3198-codex>', 'exec'), namespace)
        legacy = namespace['Codex']
        calls, receipts = [], []
        profile = {'revision': 1, 'reasoning_effort': 'low'}

        def base_rpc(self, method, params, **kwargs):
            calls.append((method, copy.deepcopy(params)))
            if method in ('thread/start', 'thread/resume'):
                return {'thread': {'id': 'preserved', 'path': '/private/original.jsonl'}}
            if method == 'model/list':
                return {'data': CATALOG}
            if method == 'turn/start':
                return {'turn': {'id': 'turn-' + str(profile['revision'])}}
            return {}

        legacy.rpc = base_rpc
        cls = make_codex(legacy, lambda agent: copy.deepcopy(profile),
                         lambda agent, value: receipts.append(value))
        agent = cls.__new__(cls)
        agent.config = {'workspace': '/synthetic', 'model': 'synthetic-model'}
        agent.on_activity = None
        agent.tools = []
        checkpoint = {'thread_id': 'preserved', 'native_rollout_path': '/private/original.jsonl'}
        before = copy.deepcopy(checkpoint)
        agent.start('first input', checkpoint)
        profile.update(revision=2, reasoning_effort='max')
        self.assertEqual('low', agent.reasoning_selection['reasoning_effort'])
        agent.start('second input', checkpoint)
        turns = [params for method, params in calls if method == 'turn/start']
        self.assertEqual(['low', 'max'], [row['effort'] for row in turns])
        self.assertEqual(['preserved', 'preserved'], [row['threadId'] for row in turns])
        self.assertEqual(before, checkpoint)
        self.assertFalse(any(method in ('thread/start', 'thread/goal/set') for method, _ in calls))
        self.assertEqual([1, 1, 2, 2], [row['profile_revision'] for row in receipts])
        self.assertTrue(all(not row['effective_verified'] for row in receipts))

    def test_unknown_send_is_single_and_unsupported_never_sends(self):
        class Native:
            config = {'model': 'synthetic-model'}
            model = 'synthetic-model'

            def __init__(self):
                self.turn_sends = 0

            def rpc(self, method, params, timeout=30):
                if method == 'model/list':
                    return {'data': CATALOG}
                self.turn_sends += 1
                raise OSError('synthetic lost response')

        profile = {'reasoning_effort': 'unsupported'}
        cls = make_codex(Native, lambda agent: profile)
        agent = cls()
        with self.assertRaisesRegex(ReasoningError, 'effort_unsupported'):
            agent.rpc('turn/start', {'threadId': 'same'})
        self.assertEqual(0, agent.turn_sends)
        profile['reasoning_effort'] = 'max'
        with self.assertRaises(OSError):
            agent.rpc('turn/start', {'threadId': 'same'})
        self.assertEqual(1, agent.turn_sends)
        self.assertEqual('unknown', agent.reasoning_selection['status'])

    def test_only_native_matched_metadata_counts_as_observation(self):
        records = [{'type': 'session_meta', 'payload': {'id': 'same'}},
                   {'type': 'turn_context', 'payload': {
                       'turn_id': 'old', 'model': 'synthetic-model', 'effort': 'max'}},
                   {'type': 'response_item', 'payload': {
                       'turn_id': 'new', 'model': 'synthetic-model', 'effort': 'max'}}]
        self.assertFalse(observed_turn(records, 'same', 'new')['observed'])
        records.append({'type': 'turn_context', 'payload': {
            'turn_id': 'new', 'model': 'synthetic-model', 'effort': 'low',
            'collaboration_mode': {'settings': {'model': 'synthetic-model',
                                               'reasoning_effort': 'low'}}}})
        observation = observed_turn(records, 'same', 'new')
        self.assertEqual('low', observation['reasoning_effort'])
        self.assertEqual('low', observation['collaboration_reasoning_effort'])
        with self.assertRaisesRegex(ReasoningError, 'identity_mismatch'):
            observed_turn(records, 'different', 'new')


if __name__ == '__main__':
    unittest.main()
