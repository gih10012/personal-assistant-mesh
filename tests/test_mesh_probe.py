"""Probe contract tests; no external network, models, native execution or spend."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.networking import PROTOCOL, digest
from assistant_mesh.server import serve
from assistant_mesh.worker import Client
from scripts.probe_mesh import Probe, ProbeError, error_code, load_envelope, main


class Endpoint:
    def __init__(self, node, other):
        self.node, self.other = node, other
        self.calls, self.tasks = [], {}
        self.hello_changes, self.links_changes = {}, {}
        self.replay_changes = {}

    def request(self, path, body=None):
        self.calls.append((path, body))
        if path == '/v1/mesh/hello':
            value = {'protocol': PROTOCOL, 'node': self.node, 'authority': self.node,
                     'a2a_idempotent_receive': True, 'token': 'DO_NOT_PRINT_PRIVATE_TOKEN'}
            value.update(self.hello_changes)
            return value
        if path == '/v1/mesh/links':
            value = {'node': self.node, 'links': [{'peer': self.other, 'kind': 'a2a',
                                                  'reachable': True, 'leader_available': False,
                                                  'secret': 'DO_NOT_PRINT_PRIVATE_TOKEN'}]}
            value.update(self.links_changes)
            return value
        if path == '/v1/mesh/queue':
            return {'peer': body['peer'], 'id': body['message']['id'], 'queued': True}
        if path.startswith('/v1/mesh/task?id='):
            identity = path.partition('id=')[2]
            if identity not in self.tasks:
                raise urllib.error.HTTPError('private URL', 403, 'private detail', {}, io.BytesIO())
            return self.tasks[identity]
        if path == '/v1/mesh/send':
            message = body['message']
            value = {'protocol': PROTOCOL, 'id': message['id'], 'fingerprint': digest(message),
                     'state': 'accepted', 'task_id': self.tasks[message['id']]['task_id']}
            value.update(self.replay_changes)
            return value
        raise AssertionError('Probe used an unexpected endpoint')


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_path = self.root / 'smoke.json'
        self.state = load_envelope(self.state_path, 'laptop', 'cloud', create=True)
        self.local, self.peer = Endpoint('laptop', 'cloud'), Endpoint('cloud', 'laptop')
        self.probe = Probe(self.local, self.peer, 'laptop', 'cloud')

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, phase='remember', status='completed', result='ACK'):
        message = self.state['messages'][phase]
        value = {'id': message['id'], 'task_id': digest(['cloud', 'laptop', message['id']]),
                 'status': status, 'result': result}
        self.peer.tasks[message['id']] = value
        return value

    def test_health_only_has_gets_and_does_not_print_server_credentials(self):
        result = self.probe.health()
        self.assertTrue(result['both_endpoint_hellos_authenticated'])
        self.assertTrue(result['bidirectional_links_observed'])
        self.assertFalse(result['native_inference_verified'])
        self.assertNotIn('DO_NOT_PRINT_PRIVATE_TOKEN', json.dumps(result))
        self.assertTrue(all(body is None for path, body in self.local.calls + self.peer.calls))
        self.assertEqual(4, len(self.local.calls + self.peer.calls))

    def test_node_authority_protocol_and_idempotence_must_match(self):
        for change in ({'node': 'impostor'}, {'authority': 'impostor'}, {'protocol': 'A2A'},
                       {'a2a_idempotent_receive': 'true'}):
            self.peer.hello_changes = change
            with self.assertRaises(ProbeError):
                self.probe.health()

    def test_link_observation_not_transport_or_model_availability_claim(self):
        self.peer.links_changes = {'links': []}
        result = self.probe.health()
        self.assertFalse(result['bidirectional_links_observed'])
        self.assertFalse(result['model_availability_verified'])
        self.assertTrue(result['both_endpoint_hellos_authenticated'])

    def test_envelope_private_immutable_and_nonce_only_in_first_input(self):
        self.assertEqual(0o600, self.state_path.stat().st_mode & 0o777)
        same = load_envelope(self.state_path, 'laptop', 'cloud')
        self.assertEqual(self.state, same)
        first, second = self.state['messages']['remember'], self.state['messages']['recall']
        self.assertEqual(first['project'], second['project'])
        self.assertEqual(first['agent'], second['agent'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertIn(self.state['nonce'], first['input'])
        self.assertNotIn(self.state['nonce'], json.dumps(second))
        self.assertNotIn('parent_ref', first)

    def test_changed_envelope_or_origin_rejected_instead_of_new_task(self):
        with self.assertRaises(ProbeError):
            load_envelope(self.state_path, 'cloud', 'laptop')
        with self.assertRaises(ProbeError):
            load_envelope(self.state_path, 'laptop', 'cloud', agent='changed')
        mutated = dict(self.state)
        mutated['messages']['remember']['input'] = 'arbitrary dangerous user text'
        self.state_path.write_text(json.dumps(mutated))
        with self.assertRaises(ProbeError):
            load_envelope(self.state_path, 'laptop', 'cloud')

    def test_public_state_or_symlink_or_git_worktree_state_rejected(self):
        self.state_path.chmod(0o644)
        with self.assertRaises(ValueError):
            load_envelope(self.state_path, 'laptop', 'cloud')
        link = self.root / 'link.json'
        link.symlink_to(self.state_path)
        with self.assertRaises(ProbeError):
            load_envelope(link, 'laptop', 'cloud')
        git = self.root / 'repo'
        git.mkdir()
        (git / '.git').mkdir()
        with self.assertRaises(ProbeError):
            load_envelope(git / 'state.json', 'laptop', 'cloud', create=True)

    def test_poll_missing_state_does_not_create_file_or_task(self):
        missing = self.root / 'does-not-exist.json'
        with self.assertRaises(ProbeError):
            load_envelope(missing, 'laptop', 'cloud')
        self.assertFalse(missing.exists())

    def test_submit_only_queues_fixed_independent_existing_mesh_message(self):
        result = self.probe.submit(self.state, 'remember')
        self.assertTrue(result['queued'])
        self.assertFalse(result['remote_completion_verified'])
        path, body = self.local.calls[-1]
        self.assertEqual('/v1/mesh/queue', path)
        self.assertEqual('cloud', body['peer'])
        self.assertEqual(self.state['messages']['remember'], body['message'])
        self.assertNotIn('/v1/tasks', [call[0] for call in self.local.calls])

    def test_recall_cannot_be_submitted_before_verified_first_ack(self):
        for status, result in (('pending', None), ('completed', 'not ACK'), ('needs_review', 'ACK')):
            self.task(status=status, result=result)
            with self.assertRaises(ProbeError):
                self.probe.submit(self.state, 'recall')
        self.assertEqual([], self.local.calls)
        self.task()
        self.assertTrue(self.probe.submit(self.state, 'recall')['queued'])
        self.assertNotIn(self.state['nonce'], json.dumps(self.local.calls[-1]))

    def test_poll_exact_nonce_proof_does_not_print_nonce_or_model_output(self):
        self.task(phase='recall', result=self.state['nonce'])
        result, code = self.probe.poll(self.state, 'recall')
        self.assertEqual(0, code)
        self.assertTrue(result['nonce_match'])
        self.assertFalse(result['tool_use_verified'])
        self.assertNotIn(self.state['nonce'], json.dumps(result))
        self.assertEqual([], self.local.calls)

    def test_completed_but_wrong_answer_is_not_native_continuity_success(self):
        self.task(phase='recall', result='UNKNOWN')
        result, code = self.probe.poll(self.state, 'recall')
        self.assertEqual(3, code)
        self.assertFalse(result['nonce_match'])
        self.assertTrue(result['terminal_observed'])

    def test_failed_or_review_does_not_pass_even_with_right_text(self):
        for status in ('failed', 'needs_review'):
            self.task(status=status)
            result, code = self.probe.poll(self.state, 'remember')
            self.assertEqual(3, code)
            self.assertFalse(result['expected_reply_match'])

    def test_timeout_is_nonterminal_and_never_resubmits_message(self):
        self.task(status='waiting_auth', result='DO_NOT_PRINT_PRIVATE_TOKEN')
        times = iter((0, 1, 2))
        result, code = self.probe.poll(self.state, 'remember', timeout=1,
                                       clock=lambda: next(times), sleep=lambda duration: None)
        self.assertEqual(2, code)
        self.assertEqual('waiting_auth', result['status'])
        self.assertTrue(result['task_not_restarted'])
        self.assertNotIn('DO_NOT_PRINT_PRIVATE_TOKEN', json.dumps(result))
        self.assertTrue(all(body is None for path, body in self.peer.calls))

    def test_denied_task_read_is_not_proof_of_no_execution(self):
        times = iter((0, 1))
        result, code = self.probe.poll(self.state, 'remember', timeout=1,
                                       clock=lambda: next(times), sleep=lambda duration: None)
        self.assertEqual(2, code)
        self.assertEqual('unobserved_or_read_denied', result['status'])
        self.assertFalse(result['terminal_observed'])

    def test_receiver_replay_uses_exact_same_body_and_returns_same_task(self):
        observed = self.task()
        result, code = self.probe.poll(self.state, 'remember', verify_replay=True)
        self.assertEqual(0, code)
        self.assertTrue(result['same_id_receiver_replay_verified'])
        send = [call for call in self.peer.calls if call[0] == '/v1/mesh/send']
        self.assertEqual([('/v1/mesh/send', {'message': self.state['messages']['remember']})], send)
        self.assertEqual(observed['task_id'], result['task_id'])

    def test_changed_task_id_or_receipt_cannot_pass_replay(self):
        for change in ({'task_id': 'another'}, {'fingerprint': 'different'}, {'state': 'unknown'}):
            self.peer.replay_changes = change
            with self.assertRaises(ProbeError):
                self.probe.replay(self.state['messages']['remember'], self.task())

    def test_foreign_task_or_unknown_status_rejected(self):
        original = self.task()
        for change in ({'task_id': 'foreign'}, {'status': 'fake-done'}, {'result': {}},
                       {'result': 'x' * 65537}):
            self.peer.tasks[original['id']] = dict(original, **change)
            with self.assertRaises(ProbeError):
                self.probe.read(self.state['messages']['remember'])

    def test_exceptions_never_expose_private_transport_or_token_strings(self):
        self.assertEqual('mesh_transport_unavailable_outcome_not_assumed',
                         error_code(urllib.error.URLError('DO_NOT_PRINT_PRIVATE_TOKEN')))
        self.assertEqual('authentication_rejected',
                         error_code(urllib.error.HTTPError('private URL', 401, 'DO_NOT_PRINT_PRIVATE_TOKEN', {}, io.BytesIO())))
        self.assertEqual('private_configuration_or_probe_input_invalid',
                         error_code(ValueError('DO_NOT_PRINT_PRIVATE_TOKEN')))

    def test_bodyless_http_error_is_safe_on_older_python(self):
        # Python 3.6 keeps fp=None here and its close() raises KeyError. The
        # probe must still sanitize and preserve the HTTP permission meaning.
        error = urllib.error.HTTPError('private URL', 403, 'private detail', {}, None)
        self.assertEqual('route_not_authorized', error_code(error))


class ProbeCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local, self.peer = Endpoint('laptop', 'cloud'), Endpoint('cloud', 'laptop')
        self.paths = []
        for kind in ('local', 'peer'):
            path = self.root / (kind + '.json')
            path.write_text(json.dumps({'control_url': kind, 'token_file': 'PRIVATE_TOKEN_PATH'}))
            path.chmod(0o600)
            self.paths.append(path)
        self.args = ['--local-config', str(self.paths[0]), '--peer-config', str(self.paths[1]),
                     '--source', 'laptop', '--destination', 'cloud']

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_cli_prints_only_safe_metadata(self):
        stream = io.StringIO()
        with patch('scripts.probe_mesh.Client', side_effect=lambda config: self.local if config['control_url'] == 'local' else self.peer):
            with contextlib.redirect_stdout(stream):
                code = main(self.args + ['--health'])
        self.assertEqual(0, code)
        self.assertNotIn('PRIVATE_TOKEN', stream.getvalue())
        self.assertFalse(any(body is not None for path, body in self.local.calls + self.peer.calls))

    def test_health_mode_cannot_request_implicit_submission_or_replay(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for extra in (['--health', '--verify-replay'], ['--submit'], ['--poll'], ['--health', '--timeout', 'nan']):
                with self.assertRaises(SystemExit):
                    main(self.args + extra)

    def test_invalid_config_error_does_not_echo_path_contents_or_auth(self):
        stream = io.StringIO()
        with patch('scripts.probe_mesh.Client', side_effect=ValueError('PRIVATE_TOKEN_PATH')):
            with contextlib.redirect_stdout(stream):
                code = main(self.args + ['--health'])
        self.assertEqual(1, code)
        self.assertNotIn('PRIVATE_TOKEN', stream.getvalue())
        self.assertEqual('private_configuration_or_probe_input_invalid', json.loads(stream.getvalue())['error'])


class RealProbeAPITests(unittest.TestCase):
    """Actual loopback auth/receive contracts; deliberately no model/backend."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.services = []
        local_key = self.secret('laptop-owner')
        reverse_key = self.secret('laptop-from-cloud')
        peer_key = self.secret('cloud-from-laptop')
        self.local_api, local_url = self.start({'node_id': 'laptop', 'port': 0,
            'database': str(self.root / 'laptop.db'), 'network_peers': {'cloud': {'send_allowed': True}},
            'peers': [{'role': 'operator', 'token_file': local_key},
                      {'role': 'agent_peer', 'node': 'cloud', 'capabilities': ['a2a.delegate'], 'token_file': reverse_key}]})
        self.peer_api, peer_url = self.start({'node_id': 'cloud', 'port': 0,
            'database': str(self.root / 'cloud.db'), 'network_peers': {},
            'peers': [{'role': 'agent_peer', 'node': 'laptop', 'capabilities': ['a2a.delegate'], 'token_file': peer_key}]})
        self.local = Client({'control_url': local_url, 'token_file': local_key})
        self.peer = Client({'control_url': peer_url, 'token_file': peer_key})
        self.probe = Probe(self.local, self.peer, 'laptop', 'cloud')
        self.state = load_envelope(self.root / 'state.json', 'laptop', 'cloud', create=True)

    def tearDown(self):
        for server, thread in reversed(self.services):
            server.shutdown()
            thread.join(timeout=3)
        self.tmp.cleanup()

    def secret(self, name):
        path = self.root / (name + '.token')
        path.write_text(digest(['test-only', name]))
        path.chmod(0o600)
        return str(path)

    def start(self, config):
        ready, service = threading.Event(), []
        def installed(server, api, channel):
            service.extend([server, api])
            ready.set()
        thread = threading.Thread(target=serve, args=(config, installed))
        thread.daemon = True
        thread.start()
        self.assertTrue(ready.wait(3))
        self.services.append((service[0], thread))
        return service[1], 'http://127.0.0.1:' + str(service[0].server_address[1])

    def deliver(self):
        delivery = self.local_api.network.claim_delivery('cloud')
        receipt = self.peer.request('/v1/mesh/send', {'message': delivery['message']})
        self.local_api.network.finish_delivery(delivery, receipt)
        return receipt

    def test_real_health_and_same_id_receiver_replay_without_model(self):
        health = self.probe.health()
        self.assertTrue(health['both_endpoint_hellos_authenticated'])
        self.assertFalse(health['bidirectional_links_observed'])
        self.assertEqual({}, self.peer_api.store.status()['tasks'])
        self.probe.submit(self.state, 'remember')
        self.assertIsNone(self.probe.read(self.state['messages']['remember']))
        receipt = self.deliver()
        observed = self.probe.read(self.state['messages']['remember'])
        self.assertEqual(receipt['task_id'], observed['task_id'])
        self.assertEqual('pending', observed['status'])
        replay = self.probe.replay(self.state['messages']['remember'], observed)
        self.assertTrue(replay['same_id_receiver_replay_verified'])
        self.assertEqual({'pending': 1}, self.peer_api.store.status()['tasks'])
        changed = dict(self.state['messages']['remember'], input='different body')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.peer.request('/v1/mesh/send', {'message': changed})
        self.assertEqual(409, caught.exception.code)

    def test_real_credentials_required_and_wrong_identity_cannot_claim_health(self):
        wrong = Client({'control_url': self.peer.url, 'token_file': self.secret('wrong-owner')})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            Probe(self.local, wrong, 'laptop', 'cloud').health()
        self.assertEqual(401, caught.exception.code)
        with self.assertRaises(ProbeError):
            Probe(self.local, self.peer, 'laptop', 'impostor').health()
        self.assertEqual({}, self.peer_api.store.status()['tasks'])


if __name__ == '__main__':
    unittest.main()
