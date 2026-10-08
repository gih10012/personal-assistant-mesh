"""Optional Mesh metadata cannot gate native work; core task fences still do."""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from assistant_mesh.codex import CodexError
from assistant_mesh.store import Store
from assistant_mesh.worker import Worker


PRIVATE_DETAIL = 'https://private.invalid/path?token=never-in-model-context'


def http_error(code):
    error = urllib.error.HTTPError(PRIVATE_DETAIL, code, PRIVATE_DETAIL, {}, io.BytesIO())
    error.close()  # real Client rejects and closes private response bodies
    return error


class FixtureClient:
    def __init__(self):
        self.calls = []
        self.failures = {}
        self.resources = {'capabilities': [], 'as_of': 123}
        self.event = None
        self.acks = []
        self.task = {'id': 'task', 'epoch': 1, 'checkpoint': {}, 'input': 'native work'}

    def request(self, path, body=None):
        self.calls.append((path, copy.deepcopy(body)))
        key = 'ack' if path == '/v1/steering' and 'id' in body else path
        failure = self.failures.get(key)
        if failure is not None:
            if callable(failure):
                failure = failure()
            if failure is not None:
                raise failure
        if path == '/v1/resources?limit=10':
            return self.resources
        if path == '/v1/steering':
            if 'id' in body:
                self.acks.append(copy.deepcopy(body))
                return {'ok': True}
            return {'steering': copy.deepcopy(self.event)}
        if path == '/v1/claim':
            task, self.task = self.task, None
            return {'task': task}
        if path == '/v1/agent/action':
            return {'children': []}
        return {'ok': True}


class NativeHarnessFixture:
    """An in-process harness boundary, not a claim of live model execution."""
    def __init__(self, config, **callbacks):
        self.auth_home = '/private/fixture-native-home'
        self.turn_id = None
        self.started = self.finished = self.closed = False
        self.prompts = []
        self.steers = []

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.closed = True

    def account(self):
        return {'authenticated': True}

    def start(self, text, resume):
        self.prompts.append(text)
        self.started = True
        self.turn_id = 'native-turn'
        return {'thread_id': 'continuous-native-thread', 'turn_id': self.turn_id}

    def steer(self, text):
        self.steers.append(text)

    def finish(self, tick, timeout):
        tick(force=True)
        self.finished = True
        return 'fixture native result'

    def native_rollout(self):
        return None

    def goal(self):
        return None


class OptionalMeshFailureTests(unittest.TestCase):
    def setUp(self):
        self.client = FixtureClient()
        self.config = {'node_id': 'node', 'codex': {'workspace': '/private/fixture-workspace',
                       'sandbox': 'danger-full-access', 'approval_policy': 'never'}}
        self.worker = Worker(self.config, client=self.client)
        self.worker.current = copy.deepcopy(self.client.task)
        self.worker.agent = Mock(turn_id='native-turn')

    def status(self):
        return self.worker.current['checkpoint']['mesh_steering_status']

    def event(self, **changes):
        value = {'id': 'steer-one', 'task_id': 'task', 'epoch': 1,
                 'text': 'owner steering', 'state': 'pending'}
        value.update(changes)
        return value

    def assert_private_detail_absent(self, value):
        self.assertNotIn(PRIVATE_DETAIL, json.dumps(value))
        self.assertNotIn('never-in-model-context', json.dumps(value))

    def test_discovery_http_failures_keep_native_cli_and_fixed_status(self):
        self.worker.config_path = '/private/worker.json'
        for code, expected in ((401, 'permission_denied'), (403, 'permission_denied'),
                               (404, 'server_upgrade_required'), (409, 'unavailable'), (500, 'unavailable')):
            with self.subTest(code=code):
                self.client.failures['/v1/resources?limit=10'] = http_error(code)
                reference = self.worker.resource_reference()
                self.assertEqual(expected, reference['discovery_status'])
                self.assertIn('cli', reference)
                self.assertIn('a2a', reference)
                self.assertFalse(reference['gateway']['native_tools_intercepted'])
                self.assertNotIn('summary', reference)
                self.assert_private_detail_absent(reference)

    def test_discovery_transport_and_invalid_payloads_degrade_without_fake_catalogue(self):
        for failure in (urllib.error.URLError(PRIVATE_DETAIL), OSError(PRIVATE_DETAIL), ValueError(PRIVATE_DETAIL)):
            with self.subTest(kind=type(failure).__name__):
                self.client.failures['/v1/resources?limit=10'] = failure
                reference = self.worker.resource_reference()
                self.assertEqual('unavailable', reference['discovery_status'])
                self.assertNotIn('summary', reference)
                self.assert_private_detail_absent(reference)
        self.client.failures.clear()
        for payload in (None, [], {}, {'capabilities': None}, {'capabilities': [{}, None]}):
            with self.subTest(payload=payload):
                self.client.resources = payload
                self.assertEqual('unavailable', self.worker.resource_reference()['discovery_status'])

    def test_discovery_success_keeps_bounded_real_metadata(self):
        self.client.resources = {'capabilities': [{'id': 'cap-' + str(i), 'kind': 'open.kind',
            'description': 'x' * 400, 'token': PRIVATE_DETAIL} for i in range(12)], 'as_of': 123}
        reference = self.worker.resource_reference()
        self.assertEqual('available', reference['discovery_status'])
        self.assertEqual(10, len(reference['summary']))
        self.assertEqual(300, len(reference['summary'][0]['description']))
        self.assertEqual(123, reference['as_of'])
        self.assert_private_detail_absent(reference)

    def test_steering_query_failures_are_optional_but_core_rpcs_still_run(self):
        failures = [(http_error(403), 'permission_denied'), (http_error(404), 'server_upgrade_required'),
                    (http_error(500), 'unavailable'), (urllib.error.URLError(PRIVATE_DETAIL), 'unavailable'),
                    (OSError(PRIVATE_DETAIL), 'unavailable'), (ValueError(PRIVATE_DETAIL), 'unavailable')]
        for failure, expected in failures:
            with self.subTest(kind=type(failure).__name__, expected=expected):
                self.client.failures['/v1/steering'] = failure
                self.worker.tick(force=True)
                self.assertEqual(expected, self.status()['query'])
                self.assertEqual('none', self.status()['submission'])
                self.assertFalse(self.worker.polling_steering)
                self.worker.agent.steer.assert_not_called()
                self.assert_private_detail_absent(self.worker.current['checkpoint'])
        self.assertEqual(len(failures), sum(path == '/v1/heartbeat' for path, _ in self.client.calls))
        self.assertEqual(len(failures), sum(path == '/v1/task/update' for path, _ in self.client.calls))

    def test_invalid_steering_events_never_submit_native_rpc(self):
        for event in (False, [], {}, self.event(id=[]), self.event(text=None), self.event(text=' '),
                      self.event(text='x' * 32769), self.event(task_id='other'), self.event(epoch=2),
                      self.event(state='completed')):
            with self.subTest(event=event):
                self.client.event = event
                self.worker.tick(force=True)
                self.assertEqual('unavailable', self.status()['query'])
                self.worker.agent.steer.assert_not_called()
        self.assertEqual([], self.client.acks)

    def test_success_is_submitted_not_claimed_as_executed_and_duplicate_never_replays(self):
        self.client.event = self.event()
        for _ in range(3):
            self.worker.tick(force=True)
        self.worker.agent.steer.assert_called_once_with('owner steering')
        self.assertEqual(['submitted'], [body['state'] for body in self.client.acks])
        self.assertEqual({'query': 'available', 'submission': 'submitted', 'ack': 'acknowledged'}, self.status())
        self.assertNotIn('completed', json.dumps(self.status()))

    def test_lost_ack_marks_unknown_and_only_retries_bookkeeping_not_native_steer(self):
        self.client.event = self.event()
        self.client.failures['ack'] = http_error(500)
        self.worker.tick(force=True)
        self.assertEqual({'query': 'available', 'submission': 'unknown', 'ack': 'unknown'}, self.status())
        self.assert_private_detail_absent(self.status())
        self.client.failures.clear()
        self.worker.tick(force=True)
        self.worker.tick(force=True)
        self.worker.agent.steer.assert_called_once_with('owner steering')
        self.assertEqual(['unknown'], [body['state'] for body in self.client.acks])
        attempts = [body['state'] for path, body in self.client.calls if path == '/v1/steering' and 'id' in body]
        self.assertEqual(['submitted', 'unknown'], attempts)
        self.assertEqual('unknown', self.status()['submission'])
        self.assertEqual('acknowledged', self.status()['ack'])

    def test_ack_transport_failures_remain_unknown_and_retry_once_per_tick(self):
        self.client.event = self.event()
        for failure in (http_error(403), http_error(404), urllib.error.URLError(PRIVATE_DETAIL),
                        OSError(PRIVATE_DETAIL), ValueError(PRIVATE_DETAIL)):
            with self.subTest(kind=type(failure).__name__):
                self.client.failures['ack'] = failure
                before = len(self.client.calls)
                self.worker.tick(force=True)
                calls = self.client.calls[before:]
                self.assertEqual(1, sum(path == '/v1/steering' and 'id' in body for path, body in calls))
                self.assertEqual('unknown', self.status()['submission'])
                self.assertEqual('unknown', self.status()['ack'])
        self.worker.agent.steer.assert_called_once_with('owner steering')

    def test_uncertain_native_steer_is_unknown_and_same_id_is_not_retried(self):
        self.worker.agent.steer.side_effect = CodexError(PRIVATE_DETAIL)
        self.client.event = self.event()
        self.worker.tick(force=True)
        self.worker.tick(force=True)
        self.worker.agent.steer.assert_called_once_with('owner steering')
        self.assertEqual(['unknown'], [body['state'] for body in self.client.acks])
        self.assertEqual('unknown', self.status()['submission'])
        self.assert_private_detail_absent(self.worker.resource_reference())

    def test_terminal_or_inflight_ledger_states_never_replay_native_steer(self):
        for state in ('submitted', 'unknown', 'submitting'):
            with self.subTest(state=state):
                self.client.event = self.event(id='steer-' + state, state=state)
                self.worker.tick(force=True)
                self.worker.agent.steer.assert_not_called()
        self.assertEqual(['unknown'], [body['state'] for body in self.client.acks])

    def test_new_pending_event_is_not_blocked_by_an_earlier_unknown_ack(self):
        self.client.event = self.event()
        self.client.failures['ack'] = http_error(500)
        self.worker.tick(force=True)
        self.client.event = self.event(id='steer-two', text='new owner detail')
        self.worker.tick(force=True)
        self.assertEqual(2, self.worker.agent.steer.call_count)
        self.assertEqual(['owner steering', 'new owner detail'], [call[0][0] for call in self.worker.agent.steer.call_args_list])

    def test_heartbeat_and_task_fence_failures_stay_strict_and_do_not_poll(self):
        for path in ('/v1/heartbeat', '/v1/task/update'):
            for failure in (http_error(409), http_error(500), OSError(PRIVATE_DETAIL), ValueError(PRIVATE_DETAIL)):
                with self.subTest(path=path, kind=type(failure).__name__):
                    self.client.calls.clear()
                    self.client.failures = {path: failure}
                    with self.assertRaises(type(failure)):
                        self.worker.tick(force=True)
                    self.assertFalse(any(route == '/v1/steering' for route, _ in self.client.calls))
                    self.assertFalse(self.worker.polling_steering)

    def test_steering_query_and_ack_stale_task_409_always_raise(self):
        for key in ('/v1/steering', 'ack'):
            with self.subTest(key=key):
                worker = Worker(self.config, client=self.client)
                worker.current = copy.deepcopy(self.client.task)
                worker.agent = Mock(turn_id='native-turn')
                self.client.event = self.event()
                self.client.failures = {key: http_error(409)}
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    worker.tick(force=True)
                self.assertEqual(409, failure.exception.code)
                self.assertFalse(worker.polling_steering)

    def test_steering_reentrant_tick_does_not_submit_twice(self):
        self.client.event = self.event()
        self.worker.agent.steer.side_effect = lambda text: self.worker.tick(force=True)
        self.worker.tick(force=True)
        self.worker.agent.steer.assert_called_once_with('owner steering')
        self.assertEqual(1, len(self.client.acks))

    def test_status_is_safe_prompt_data_even_when_checkpoint_status_is_untrusted(self):
        self.worker.current['checkpoint']['mesh_steering_status'] = {
            'query': PRIVATE_DETAIL, 'submission': PRIVATE_DETAIL, 'ack': [PRIVATE_DETAIL], 'token': PRIVATE_DETAIL}
        reference = self.worker.resource_reference()
        self.assertEqual({'query': 'not_polled', 'submission': 'none', 'ack': 'none'}, reference['steering_status'])
        self.assert_private_detail_absent(reference)
        self.client.failures['/v1/steering'] = http_error(403)
        self.worker.tick(force=True)
        self.assertEqual('permission_denied', self.worker.resource_reference()['steering_status']['query'])

    def test_optional_failures_allow_native_turn_to_start_finish_and_complete_task(self):
        harnesses = []

        def backend(config, **callbacks):
            agent = NativeHarnessFixture(config, **callbacks)
            harnesses.append(agent)
            return agent

        for failure_kind in ('discovery', 'steering_query', 'steering_ack'):
            with self.subTest(failure_kind=failure_kind):
                client = FixtureClient()
                if failure_kind == 'discovery':
                    client.failures['/v1/resources?limit=10'] = http_error(500)
                elif failure_kind == 'steering_query':
                    client.failures['/v1/steering'] = http_error(404)
                else:
                    client.event = self.event()
                    client.failures['ack'] = OSError(PRIVATE_DETAIL)
                worker = Worker(self.config, client=client, backend=backend)
                with patch('assistant_mesh.worker.sessions.save'):
                    self.assertTrue(worker.run_once())
                agent = harnesses[-1]
                self.assertTrue(agent.started and agent.finished and agent.closed)
                self.assertEqual('completed', [body['status'] for path, body in client.calls
                                               if path == '/v1/task/update' and 'status' in body][-1])
                self.assertEqual(1 if failure_kind == 'steering_ack' else 0, len(agent.steers))
                self.assert_private_detail_absent(agent.prompts)


class SteeringLedgerContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'ledger.sqlite')
        self.store.heartbeat('node', ['leader'])
        self.store.create_task('steering protocol')
        self.task = self.store.claim('node')
        self.store.steer(self.task['id'], 'owner detail', 'event-one')

    def tearDown(self):
        self.directory.cleanup()

    def state(self):
        with self.store.transaction() as db:
            return db.execute("SELECT state FROM steering WHERE id='event-one'").fetchone()[0]

    def poll(self, identity=None, state=None):
        return self.store.poll_steering(self.task['id'], 'node', self.task['epoch'], identity, state)

    def test_claim_is_transactionally_submitting_and_not_polled_again(self):
        event = self.poll()['steering']
        self.assertEqual('pending', event['state'])
        self.assertEqual('submitting', self.state())
        self.assertIsNone(self.poll()['steering'])

    def test_restart_marks_unacknowledged_submission_unknown_without_replay(self):
        self.poll()
        self.store = Store(self.store.path)
        self.assertEqual('unknown', self.state())
        self.assertIsNone(self.poll()['steering'])

    def test_ack_response_lost_after_commit_is_repaired_unknown_without_resteer(self):
        store, task = self.store, self.task
        calls = []
        lose_ack = [True]

        def request(path, body=None):
            calls.append((path, copy.deepcopy(body)))
            if path == '/v1/heartbeat':
                return store.heartbeat('node', ['leader'])
            if path == '/v1/task/update':
                return store.update_task(task['id'], 'node', task['epoch'], body['checkpoint'])
            if path == '/v1/steering':
                response = store.poll_steering(task['id'], 'node', task['epoch'], body.get('id'), body.get('state'))
                if 'id' in body and lose_ack[0]:
                    lose_ack[0] = False
                    raise OSError(PRIVATE_DETAIL)
                return response
            raise AssertionError('unexpected fixture request')

        worker = Worker({'capabilities': ['leader']}, client=Mock(request=request))
        worker.current, worker.agent = task, Mock(turn_id='native-turn')
        worker.tick(force=True)
        self.assertEqual('submitted', self.state())  # committed but receipt lost
        self.assertEqual('unknown', task['checkpoint']['mesh_steering_status']['submission'])
        worker.tick(force=True)
        worker.tick(force=True)
        self.assertEqual('unknown', self.state())
        worker.agent.steer.assert_called_once_with('owner detail')
        self.assertEqual(['submitted', 'unknown'], [body['state'] for path, body in calls
                                                  if path == '/v1/steering' and 'id' in body])


if __name__ == '__main__':
    unittest.main()
