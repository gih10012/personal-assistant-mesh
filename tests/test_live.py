import json
import queue
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from assistant_mesh.codex import Codex, CodexError
from assistant_mesh.live import CodexLiveTransport, LiveError, LiveSession


BINDING = {'thread_id': 'continuous-native-thread', 'scope': 'leader:owner/default',
           'principal': 'operator', 'source': 'synthetic-fixture', 'peer': 'authorized-owner'}
AUTH_HOME = '/private/synthetic-communication-account'


class FakeTransport:
    def __init__(self):
        self.calls, self.events = [], []
        self.failure = None

    def validate(self, binding):
        self.binding = dict(binding)

    def rpc(self, method, params, timeout=30):
        self.calls.append((method, params))
        if self.failure:
            raise self.failure
        return {}  # Exact start/append/stop response shapes, not connection evidence.

    def poll(self, timeout=1):
        return self.events.pop(0) if self.events else None


class LiveTests(unittest.TestCase):
    def session(self, grants=None, binding=None, **kwargs):
        transport = FakeTransport()
        granted = set(grants if grants is not None else
                      ('connect', 'send_text', 'send_audio', 'receive_audio', 'read_transcript', 'speak_result'))
        session = LiveSession(transport, dict(binding or BINDING),
            authorize=lambda binding, action: action in granted,
            lease_check=lambda binding: True, session_id='live-session', **kwargs)
        return session, transport, granted

    def event(self, method, **params):
        return {'method': 'thread/realtime/' + method,
                'params': dict({'threadId': BINDING['thread_id']}, **params)}

    def started(self, session, output='text'):
        session.start(output_modality=output)
        session.handle(self.event('started', version='v3', realtimeSessionId='live-session'))

    def test_binding_is_copied_and_cannot_be_retargeted_through_callback(self):
        binding = dict(BINDING)
        session, transport, _ = self.session(binding=binding)
        binding['thread_id'] = 'other'
        view = session.binding
        view['principal'] = 'forged'
        self.assertEqual(BINDING, session.binding)
        self.started(session)
        self.assertEqual(BINDING['thread_id'], transport.calls[0][1]['threadId'])

    def test_default_deny_and_missing_lease_never_send(self):
        transport = FakeTransport()
        session = LiveSession(transport, BINDING, lease_check=lambda binding: True)
        with self.assertRaisesRegex(LiveError, 'permission_denied_connect'):
            session.start()
        no_lease = LiveSession(transport, BINDING, authorize=lambda binding, action: True)
        with self.assertRaisesRegex(LiveError, 'lease_lost'):
            no_lease.start()
        self.assertEqual([], transport.calls)

    def test_truthy_permission_object_is_not_authorization(self):
        session = LiveSession(FakeTransport(), BINDING, lease_check=lambda binding: True,
                              authorize=lambda binding, action: {'allowed': False})
        with self.assertRaisesRegex(LiveError, 'permission_denied'):
            session.start()

    def test_start_binds_existing_thread_does_not_create_another_leader(self):
        session, transport, _ = self.session()
        result = session.start(version='v3', transport={'type': 'websocket'})
        self.assertEqual([('thread/realtime/start', {
            'threadId': BINDING['thread_id'], 'outputModality': 'text',
            'version': 'v3', 'realtimeSessionId': 'live-session',
            'transport': {'type': 'websocket'}})], transport.calls)
        self.assertEqual('starting', result['state'])
        self.assertTrue(result['evidence']['request_accepted'])
        self.assertFalse(result['evidence']['native_started'])
        self.assertNotIn('model', transport.calls[0][1])
        self.assertNotIn('includeStartupContext', transport.calls[0][1])

    def test_started_notification_not_audio_or_playback_proof(self):
        session, _, _ = self.session()
        self.started(session)
        result = session.snapshot()
        self.assertEqual('started', result['state'])
        self.assertTrue(result['evidence']['native_started'])
        self.assertEqual(0, result['evidence']['output_audio_chunks'])
        self.assertNotIn('connected', result)
        self.assertNotIn('played', result)

    def test_start_observation_timeout_preserves_same_attempt(self):
        session, transport, _ = self.session()
        session.start()
        with self.assertRaisesRegex(LiveError, 'started_not_observed'):
            session.wait_started(timeout=.005)
        self.assertEqual('starting', session.state)
        transport.events.append(self.event('started', version='v3'))
        self.assertEqual('started', session.wait_started(timeout=.1)['state'])
        self.assertEqual(1, len(transport.calls))

    def test_other_thread_and_foreign_session_events_are_not_accepted(self):
        session, _, _ = self.session()
        session.start()
        self.assertFalse(session.handle(self.event('started', threadId='other', version='v3')))
        self.assertEqual('starting', session.state)
        session.handle(self.event('started', version='v3', realtimeSessionId='old-session'))
        self.assertEqual('error', session.state)
        self.assertFalse(session.evidence['native_started'])

    def test_invalid_start_fields_do_not_send(self):
        for args in ({'version': 'v4'}, {'output_modality': 'image'}, {'voice': 'invalid'},
                     {'transport': {'type': 'websocket', 'sdp': 'extra'}},
                     {'transport': {'type': 'webrtc', 'sdp': ''}}):
            with self.subTest(args=args):
                session, transport, _ = self.session()
                with self.assertRaises(LiveError):
                    session.start(**args)
                self.assertEqual([], transport.calls)

    def test_webrtc_offer_passes_only_schema_keys_and_is_not_in_snapshot(self):
        session, transport, _ = self.session()
        session.start(transport={'type': 'webrtc', 'sdp': 'synthetic-private-offer'})
        self.assertEqual({'type': 'webrtc', 'sdp': 'synthetic-private-offer'}, transport.calls[0][1]['transport'])
        self.assertNotIn('synthetic-private-offer', json.dumps(session.snapshot()))

    def test_webrtc_answer_is_only_sent_to_explicitly_authorized_signaling_sink(self):
        sink = mock.Mock()
        session, _, grants = self.session(on_event=sink)
        session.start(transport={'type': 'webrtc', 'sdp': 'synthetic-offer'})
        session.handle(self.event('sdp', sdp='synthetic-private-answer'))
        sink.assert_not_called()
        grants.add('receive_signaling')
        session.handle(self.event('sdp', sdp='synthetic-private-answer'))
        self.assertEqual('sdp', sink.call_args[0][0]['type'])
        self.assertNotIn('synthetic-private-answer', json.dumps(session.snapshot()))

    def test_existing_call_requires_exact_call_identity_and_separate_grant(self):
        binding = dict(BINDING, call_id='owner-call')
        session, transport, grants = self.session(binding=binding)
        with self.assertRaisesRegex(LiveError, 'permission_denied_attach_call'):
            session.start(transport={'type': 'existingCall', 'callId': 'owner-call'})
        grants.add('attach_call')
        with self.assertRaisesRegex(LiveError, 'call_identity_mismatch'):
            session.start(transport={'type': 'existingCall', 'callId': 'other-call'})
        session.start(transport={'type': 'existingCall', 'callId': 'owner-call'})
        self.assertEqual('owner-call', transport.calls[0][1]['transport']['callId'])

    def test_text_is_user_input_and_duplicate_id_is_not_resubmitted(self):
        session, transport, _ = self.session()
        self.started(session)
        first = session.append_text('synthetic message', 'message-one')
        duplicate = session.append_text('synthetic message', 'message-one')
        self.assertEqual(first, duplicate)
        self.assertEqual('accepted', first['status'])
        self.assertEqual(('thread/realtime/appendText', {'threadId': BINDING['thread_id'],
                          'text': 'synthetic message'}), transport.calls[-1])
        self.assertEqual(2, len(transport.calls))
        with self.assertRaisesRegex(LiveError, 'submission_id_conflict'):
            session.append_text('changed message', 'message-one')
        self.assertNotIn('synthetic message', json.dumps(session.snapshot()))

    def test_unknown_text_submission_can_be_queried_but_not_replayed(self):
        session, transport, _ = self.session()
        self.started(session)
        transport.failure = LiveError('live_rpc_unknown')
        with self.assertRaisesRegex(LiveError, 'rpc_unknown'):
            session.append_text('private message', 'message-one')
        self.assertEqual('unknown', session.append_text('private message', 'message-one')['status'])
        self.assertEqual(2, len(transport.calls))
        with self.assertRaisesRegex(LiveError, 'not_started'):
            session.append_text('another', 'message-two')

    def test_speech_handoff_ids_are_independent_and_do_not_mean_played(self):
        session, transport, _ = self.session()
        self.started(session)
        one = session.append_speech('first result', 'handoff-one')
        two = session.append_speech('second result', 'handoff-two')
        self.assertEqual('accepted', one['status'])
        self.assertEqual('accepted', two['status'])
        self.assertEqual(3, len(transport.calls))
        self.assertEqual(0, session.evidence['output_audio_chunks'])
        self.assertNotIn('played', one)

    def test_parallel_text_dedup_and_distinct_result_ids_do_not_share_active_slot(self):
        session, transport, _ = self.session()
        self.started(session)
        barrier = threading.Barrier(2)

        def same_input(index):
            barrier.wait(timeout=2)
            return session.append_text('same synthetic input', 'same-input')

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(same_input, (1, 2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(2, len(transport.calls))
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(lambda identifier:
                session.append_speech(identifier + ' result', identifier), ('first-result', 'second-result')))
        self.assertEqual({'first-result', 'second-result'}, {output['id'] for output in outputs})
        self.assertEqual(4, len(transport.calls))

    def test_audio_shape_forwards_opaque_data_without_guessing_encoding(self):
        session, transport, _ = self.session()
        self.started(session)
        audio = {'data': 'opaque-synthetic-fixture', 'sampleRate': 24000,
                 'numChannels': 1, 'samplesPerChannel': 2, 'itemId': 'audio-item'}
        accepted = session.append_audio(audio)
        self.assertEqual('accepted', accepted['status'])
        self.assertEqual(('thread/realtime/appendAudio', {'threadId': BINDING['thread_id'],
                          'audio': audio}), transport.calls[-1])
        self.assertNotIn(audio['data'], json.dumps(session.snapshot()))

    def test_invalid_audio_fields_and_boolean_integers_are_rejected(self):
        for audio in ({'data': 'x', 'sampleRate': 24000, 'numChannels': True},
                      {'data': 'x', 'sampleRate': 0, 'numChannels': 1},
                      {'data': 'x', 'sampleRate': 24000, 'numChannels': 1, 'unknown': 5},
                      {'data': 'x', 'sampleRate': 24000, 'numChannels': 1, 'samplesPerChannel': -1}):
            with self.subTest(audio=audio):
                session, transport, _ = self.session()
                self.started(session)
                with self.assertRaisesRegex(LiveError, 'invalid_audio'):
                    session.append_audio(audio)
                self.assertEqual(1, len(transport.calls))

    def test_audio_send_and_receive_require_independent_revocable_grants(self):
        audio_sink = mock.Mock()
        session, transport, grants = self.session(on_audio=audio_sink)
        self.started(session, output='audio')
        grants.remove('send_audio')
        audio = {'data': 'synthetic', 'sampleRate': 24000, 'numChannels': 1}
        with self.assertRaisesRegex(LiveError, 'permission_denied_send_audio'):
            session.append_audio(audio)
        session.handle(self.event('outputAudio/delta', audio=audio))
        audio_sink.assert_called_once_with(audio, BINDING)
        grants.remove('receive_audio')
        session.handle(self.event('outputAudio/delta', audio=audio))
        self.assertEqual(1, audio_sink.call_count)
        self.assertEqual(2, session.evidence['output_audio_chunks'])
        self.assertEqual(1, len(transport.calls))

    def test_audio_output_mode_requires_grant_and_text_mode_does_not_play_audio(self):
        sink = mock.Mock()
        session, transport, _ = self.session(grants=('connect',), on_audio=sink)
        with self.assertRaisesRegex(LiveError, 'permission_denied_receive_audio'):
            session.start(output_modality='audio')
        self.started(session)
        session.handle(self.event('outputAudio/delta', audio={'data': 'x', 'sampleRate': 1, 'numChannels': 1}))
        sink.assert_not_called()
        self.assertEqual(1, len(transport.calls))

    def test_transcripts_are_opt_in_and_do_not_create_new_authority(self):
        sink = mock.Mock()
        session, _, grants = self.session(on_event=sink)
        self.started(session)
        session.handle(self.event('transcript/done', role='user', text='private synthetic content'))
        sink.assert_called_once()
        self.assertNotIn('private synthetic content', json.dumps(session.snapshot()))
        grants.remove('read_transcript')
        session.handle(self.event('transcript/delta', role='assistant', delta='not forwarded'))
        self.assertEqual(1, sink.call_count)
        self.assertFalse(session.handle(self.event('itemAdded', item={'type': 'delegation', 'role': 'operator'})))
        self.assertEqual(BINDING, session.binding)

    def test_interleaved_canonical_items_keep_per_item_correlation(self):
        sink = mock.Mock()
        session, _, _ = self.session(on_event=sink)
        self.started(session)
        for identifier in ('first', 'second'):
            session.handle(self.event('item/started', item={'type': 'transcriptSegment',
                'id': identifier, 'realtimeSessionId': 'live-session', 'role': 'assistant', 'text': ''}))
        session.handle(self.event('item/transcript/delta', itemId='second', delta='two'))
        session.handle(self.event('item/transcript/delta', itemId='first', delta='one!'))
        self.assertEqual(4, session.items['first']['delta_chars'])
        self.assertEqual(3, session.items['second']['delta_chars'])
        self.assertEqual(['second', 'first'], [call[0][0]['item_id'] for call in sink.call_args_list])
        self.assertFalse(session.handle(self.event('item/transcript/delta', itemId='unbound', delta='foreign')))
        self.assertFalse(session.handle(self.event('item/completed', item={'type': 'transcriptSegment',
            'id': 'foreign', 'realtimeSessionId': 'other-live-session', 'role': 'assistant', 'text': 'x'})))

    def test_completed_item_replay_is_deduplicated_and_not_downgraded(self):
        sink = mock.Mock()
        session, _, _ = self.session(on_event=sink)
        self.started(session)
        item = {'type': 'transcriptSegment', 'id': 'first', 'realtimeSessionId': 'live-session',
                'role': 'assistant', 'text': 'result'}
        session.handle(self.event('item/completed', item=item))
        session.handle(self.event('item/completed', item=item))
        session.handle(self.event('item/started', item=item))
        self.assertEqual('completed', session.items['first']['status'])
        self.assertEqual(1, sink.call_count)

    def test_stop_ack_requires_native_closed_and_does_not_close_leader_process(self):
        session, transport, _ = self.session()
        self.started(session)
        self.assertEqual('stopping', session.stop()['state'])
        self.assertFalse(session.evidence['native_closed'])
        session.stop()
        self.assertEqual(2, len(transport.calls))
        self.assertEqual(('thread/realtime/stop', {'threadId': BINDING['thread_id']}), transport.calls[-1])
        session.handle(self.event('closed', reason='normal'))
        self.assertEqual('closed', session.state)
        self.assertTrue(session.evidence['native_closed'])
        self.assertFalse(session.handle(self.event('started', version='v3')))

    def test_unknown_start_is_not_retried_and_can_be_stopped(self):
        session, transport, _ = self.session()
        transport.failure = LiveError('live_rpc_unknown')
        with self.assertRaisesRegex(LiveError, 'rpc_unknown'):
            session.start()
        with self.assertRaisesRegex(LiveError, 'start_already_submitted'):
            session.start()
        self.assertEqual('unknown', session.state)
        transport.failure = None
        session.stop()
        self.assertEqual(2, len(transport.calls))

    def test_lease_loss_detaches_and_never_stops_replacement_owner(self):
        session, transport, _ = self.session()
        self.started(session)
        session.lease_check = lambda binding: False
        self.assertEqual('detached', session.stop()['state'])
        self.assertEqual(1, len(transport.calls))

    def test_native_error_details_and_close_reason_do_not_leak(self):
        session, _, _ = self.session()
        self.started(session)
        session.handle(self.event('error', message='sensitive-auth-url-and-context'))
        self.assertEqual('error', session.state)
        self.assertNotIn('sensitive', json.dumps(session.snapshot()))
        session.handle(self.event('closed', reason='sensitive'))
        self.assertTrue(session.evidence['native_closed'])
        self.assertNotIn('sensitive', json.dumps(session.snapshot()))

    def test_sink_failure_is_unknown_not_playback_success_or_automatic_retry(self):
        sink = mock.Mock(side_effect=OSError('private error'))
        session, _, _ = self.session(on_audio=sink)
        self.started(session, output='audio')
        with self.assertRaisesRegex(LiveError, 'sink_delivery_unknown'):
            session.handle(self.event('outputAudio/delta', audio={'data': 'x', 'sampleRate': 1, 'numChannels': 1}))
        self.assertEqual(1, session.evidence['sink_delivery_unknown'])
        self.assertEqual(1, sink.call_count)


class NativeTransportTests(unittest.TestCase):
    def runtime(self):
        agent = Codex.__new__(Codex)
        agent.thread_id = BINDING['thread_id']
        agent.auth_home = AUTH_HOME
        agent.deferred, agent.events = [], queue.Queue()
        agent.account = mock.Mock(return_value={'authenticated': True, 'type': 'chatgpt', 'plan': 'plus'})
        agent.rpc = mock.Mock(return_value={})
        return agent

    def test_requires_loaded_native_thread_and_single_adapter(self):
        agent = self.runtime()
        with self.assertRaisesRegex(LiveError, 'thread_not_loaded'):
            CodexLiveTransport(agent, 'other', lambda: True, expected_auth_home=AUTH_HOME)
        CodexLiveTransport(agent, agent.thread_id, lambda: True, expected_auth_home=AUTH_HOME)
        with self.assertRaisesRegex(LiveError, 'stream_already_bound'):
            CodexLiveTransport(agent, agent.thread_id, lambda: True, expected_auth_home=AUTH_HOME)

    def test_api_key_authentication_is_not_silently_substituted_for_subscription(self):
        agent = self.runtime()
        agent.account.return_value = {'authenticated': True, 'type': 'apiKey'}
        transport = CodexLiveTransport(agent, agent.thread_id, lambda: True, expected_auth_home=AUTH_HOME)
        with self.assertRaisesRegex(LiveError, 'subscription_required'):
            transport.validate(BINDING)
        agent.rpc.assert_not_called()

    def test_changed_thread_and_lost_lease_are_rejected(self):
        agent = self.runtime()
        alive = [True]
        transport = CodexLiveTransport(agent, agent.thread_id, lambda: alive[0], expected_auth_home=AUTH_HOME)
        agent.thread_id = 'changed'
        with self.assertRaisesRegex(LiveError, 'thread_changed'):
            transport.rpc('thread/realtime/stop', {'threadId': BINDING['thread_id']})
        agent.thread_id = BINDING['thread_id']
        alive[0] = False
        with self.assertRaisesRegex(LiveError, 'lease_lost'):
            transport.poll()
        agent.rpc.assert_not_called()

    def test_poll_reads_deferred_realtime_but_preserves_other_native_events(self):
        agent = self.runtime()
        native = {'method': 'turn/completed', 'params': {'threadId': agent.thread_id}}
        foreign = {'method': 'thread/realtime/closed', 'params': {'threadId': 'other'}}
        live = {'method': 'thread/realtime/started', 'params': {'threadId': agent.thread_id, 'version': 'v3'}}
        agent.deferred = [native, foreign, live]
        transport = CodexLiveTransport(agent, agent.thread_id, lambda: True, expected_auth_home=AUTH_HOME)
        self.assertEqual(live, transport.poll())
        self.assertEqual([native, foreign], agent.deferred)

    def test_poll_handles_async_before_rpc_ack_without_dropping_turn_events(self):
        agent = self.runtime()
        agent.on_tool, agent.on_activity, agent.on_interaction = None, None, None
        agent.send = mock.Mock()
        native = {'method': 'item/completed', 'params': {'threadId': agent.thread_id}}
        live = {'method': 'thread/realtime/closed', 'params': {'threadId': agent.thread_id}}
        agent.events.put(native)
        agent.events.put(live)
        transport = CodexLiveTransport(agent, agent.thread_id, lambda: True, expected_auth_home=AUTH_HOME)
        self.assertEqual(live, transport.poll(timeout=.1))
        self.assertEqual([native], agent.deferred)

    def test_protocol_timeout_and_rejection_errors_are_redacted(self):
        agent = self.runtime()
        transport = CodexLiveTransport(agent, agent.thread_id, lambda: True, expected_auth_home=AUTH_HOME)
        agent.rpc.side_effect = CodexError('codex_rpc_failed_thread_realtime_start')
        with self.assertRaisesRegex(LiveError, '^live_rpc_rejected$'):
            transport.rpc('thread/realtime/start', {'threadId': agent.thread_id})
        agent.rpc.side_effect = OSError('private-token-content')
        with self.assertRaisesRegex(LiveError, '^live_rpc_unknown$'):
            transport.rpc('thread/realtime/start', {'threadId': agent.thread_id})

    def test_poll_timeout_is_nonterminal_but_disconnect_is_unknown(self):
        agent = self.runtime()
        transport = CodexLiveTransport(agent, agent.thread_id, lambda: True, expected_auth_home=AUTH_HOME)
        agent.event = mock.Mock(side_effect=CodexError('codex_timeout'))
        self.assertIsNone(transport.poll(timeout=.01))
        agent.event.side_effect = CodexError('codex_disconnected')
        with self.assertRaisesRegex(LiveError, 'transport_unknown'):
            transport.poll(timeout=.01)

    def test_communication_account_pin_is_required_and_never_falls_back(self):
        agent = self.runtime()
        with self.assertRaisesRegex(LiveError, 'communication_auth_home'):
            CodexLiveTransport(agent, agent.thread_id, lambda: True)
        with self.assertRaisesRegex(LiveError, 'communication_account_mismatch'):
            CodexLiveTransport(agent, agent.thread_id, lambda: True,
                               expected_auth_home='/private/other-inference-account')
        transport = CodexLiveTransport(agent, agent.thread_id, lambda: True,
                                       expected_auth_home=AUTH_HOME)
        agent.auth_home = '/private/other-inference-account'
        with self.assertRaisesRegex(LiveError, 'communication_account_changed'):
            transport.validate(BINDING)
        agent.account.assert_not_called()


if __name__ == '__main__':
    unittest.main()
