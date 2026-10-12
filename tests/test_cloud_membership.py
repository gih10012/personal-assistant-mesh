"""Isolated scoped protocol/authority tests; not a real Codex Cloud job.

The loopback case exercises actual authenticated JSON HTTP and complete bytes
with a local test listener. OpenAI CLI execution is fake; no account/job/secret
installation, production enrollment or Mesh receiver change occurs.
"""
import base64
import copy
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from assistant_mesh.cloud_jobs import CloudJobs, CommandResult
from assistant_mesh.cloud_membership import CloudMembership, PROTOCOL, REQUEST_PROTOCOL, node_id
from assistant_mesh.ingress import Ingress
from assistant_mesh.store import Conflict, Store


TOKEN = 'SYNTHETIC_CLOUD_NODE_TOKEN_' + 'a' * 40
URL = 'https://chatgpt.com/codex/tasks/task_fixture'


class CloudMembershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-cloud-membership.')
        self.path = Path(self.temp.name) / 'private'
        self.now = 1000
        self.cli_calls = []
        def runner(argv, **kwargs):
            self.cli_calls.append((argv, kwargs))
            return CommandResult(0, URL.encode('utf8'), b'')
        self.jobs = CloudJobs(self.path, 'owner-profile-fixture', {'env-fixture': ['main']},
                              executable='/fixture/native-codex', codex_home='/fixture/native-home',
                              runner=runner, clock=lambda: self.now)
        self.job_id = self.jobs.intent({'mesh_task_id': 'original-mesh-task', 'mesh_epoch': 3,
            'request_id': 'original-cloud-request', 'environment_id': 'env-fixture', 'branch': 'main',
            'prompt': 'SYNTHETIC_PUBLIC_TASK'})['job_id']
        self.store = Store(Path(self.temp.name) / 'isolated.sqlite', clock=lambda: self.now)
        self.ingress = Ingress(self.store, 'authority-fixture', {'owner_id': 'owner-fixture'})
        self.lease_live = True
        self.gateway_calls = []
        def gateway(binding, action, args):
            self.gateway_calls.append((copy.deepcopy(binding), action, copy.deepcopy(args)))
            return {'request_accepted': True, 'capability_id': args.get('capability_id'),
                    'execution_verified': False}
        self.gateway = gateway
        self.membership = self.reopen()
        self.document = {'format': PROTOCOL, 'mesh_task_id': 'original-mesh-task', 'mesh_epoch': 3,
            'lease_id': 'original-cloud-lease', 'expires_at': 2000, 'request_id': 'original-cloud-request',
            'callback_url': 'https://mesh.example.test/cloud/node',
            'scopes': ['node:hello', 'task:read', 'task:report', 'artifact:write',
                       'tasks:submit', 'tasks:continue', 'capabilities:read', 'capabilities:call'],
            'capabilities': ['public-fixture-fetch'],
            'enrollment': {'format': 'mesh-enrollment/1', 'mesh_id': 'mesh-fixture',
                'node_id': node_id(self.job_id), 'owner_binding': 'owner-fixture',
                'repo_pin': {'repository': 'https://github.com/example/mesh', 'commit': 'a' * 40},
                'runtime': {'kind': 'temporary', 'expires_at': 2000, 'state_ref': 'authority:cloud-state'},
                'join': {'peer_id': 'owner-authority', 'authority_id': 'authority-fixture',
                         'route_ref': 'route-fixture', 'credential_ref': 'cloud-token-fixture',
                         'trust_ref': 'owner-root-fixture'},
                'continuation': {'project_id': 'mesh-self-maintenance',
                                 'session_ref': 'project-cloud-session', 'state_ref': 'cloud-result-state'}}}

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self, **changes):
        options = {'ingress': self.ingress, 'capability_gateway': self.gateway,
                   'lease_validator': lambda binding: self.lease_live}
        return CloudMembership(self.jobs, **dict(options, **changes))

    def prepare(self, document=None, token=TOKEN):
        return self.membership.prepare(self.job_id, document or self.document,
                                       token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                                       authorized=True)

    def envelope(self, action, arguments=None, request_id=None):
        return {'format': REQUEST_PROTOCOL, 'job_id': self.job_id,
                'task_id': 'original-mesh-task', 'epoch': 3, 'lease_id': 'original-cloud-lease',
                'request_id': request_id or 'request-' + action, 'action': action,
                'arguments': arguments or {}}

    def call(self, action, arguments=None, request_id=None, token=TOKEN):
        return self.membership.handle(token, self.envelope(action, arguments, request_id))

    def active(self):
        self.prepare()
        self.jobs.submit(self.job_id, authorized=True)
        return self.call('hello')

    @staticmethod
    def artifact(raw=b'PUBLIC_ARTIFACT\n', name='fixture.txt'):
        return {'name': name, 'encoding': 'base64', 'content_b64': base64.b64encode(raw).decode('ascii'),
                'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'media_type': 'text/plain'}

    def request_count(self):
        with self.jobs._transaction() as db:
            return db.execute('SELECT COUNT(*) FROM cloud_node_requests').fetchone()[0]

    def test_prepared_contract_is_offline_separate_from_live_enrollment(self):
        result = self.prepare()
        self.assertTrue(result['prepared'])
        self.assertFalse(result['cloud_node_enrolled'])
        self.assertEqual('openai-codex-cloud', result['display_id'])
        self.assertNotEqual('cloud', result['node_id'])
        self.assertEqual([], self.cli_calls)
        bootstrap = self.membership.bootstrap(self.job_id)
        self.assertEqual('cloud-token-fixture', bootstrap['enrollment']['join']['credential_ref'])
        self.assertFalse(bootstrap['credentials_included'])
        self.assertNotIn(TOKEN, json.dumps(bootstrap))

    def test_prepare_requires_owner_prelaunch_exact_job_and_enrollment_binding(self):
        with self.assertRaises(PermissionError):
            self.membership.prepare(self.job_id, self.document,
                                    token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest())
        for field, value in [('mesh_epoch', 4), ('mesh_task_id', 'other'), ('request_id', 'other')]:
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.prepare(dict(self.document, **{field: value}))
        altered = copy.deepcopy(self.document)
        altered['enrollment']['node_id'] = 'other-node'
        with self.assertRaises(ValueError):
            self.prepare(altered)
        self.jobs.submit(self.job_id, authorized=True)
        with self.assertRaises(Conflict):
            self.prepare()

    def test_prepare_reopen_is_same_immutable_grant_not_new_identity(self):
        first = self.prepare()
        self.membership = self.reopen()
        second = self.prepare()
        self.assertEqual(first['node_id'], second['node_id'])
        self.assertFalse(second['prepared'])
        for change in ({'expires_at': 2500}, {'capabilities': []}, {'callback_url': 'https://other.test/cloud'}):
            document = copy.deepcopy(self.document)
            document.update(change)
            if 'expires_at' in change:
                document['enrollment']['runtime']['expires_at'] = change['expires_at']
            with self.subTest(change=change), self.assertRaises(Conflict):
                self.prepare(document)

    def test_token_cannot_activate_unknown_provider_or_skip_hello(self):
        self.prepare()
        with self.assertRaisesRegex(Conflict, 'provider_identity_unconfirmed'):
            self.call('hello')
        self.jobs.submit(self.job_id, authorized=True)
        with self.assertRaisesRegex(Conflict, 'hello_required'):
            self.call('status')
        active = self.call('hello')
        self.assertTrue(active['cloud_node_enrolled'])
        self.assertTrue(active['authenticated_hello'])
        self.assertFalse(active['execution_verified'])
        self.assertEqual(active, self.call('hello'))

    def test_wrong_token_scope_task_epoch_lease_and_operator_injection_rejected(self):
        self.active()
        for token in ('bad', TOKEN + 'other', 'a' * 40 + '\udfff'):
            with self.subTest(token_length=len(token)), self.assertRaises(PermissionError):
                self.call('status', token=token)
        for change in ({'task_id': 'other'}, {'epoch': 4}, {'epoch': True},
                       {'job_id': 'other'}, {'lease_id': 'other'}):
            with self.subTest(change=change), self.assertRaises(PermissionError):
                self.membership.handle(TOKEN, dict(self.envelope('status'), **change))
        with self.assertRaises(ValueError):
            self.membership.handle(TOKEN, dict(self.envelope('status'), role='operator'))
        with self.assertRaises(PermissionError):
            self.call('operator')
        self.assertEqual(1, self.request_count())

    def test_restricted_node_scope_cannot_submit_or_call_ungranted_capability(self):
        limited = dict(self.document, scopes=['node:hello', 'task:read'])
        self.prepare(limited)
        self.jobs.submit(self.job_id, authorized=True)
        self.call('hello')
        with self.assertRaises(PermissionError):
            self.call('task_submit', {'input': 'outside grant'})
        with self.assertRaises(PermissionError):
            self.call('call', {'capability_id': 'public-fixture-fetch', 'input': {}})
        self.assertEqual([], self.gateway_calls)

    def test_lease_expiry_and_explicit_close_block_node_not_owner_offline_reads(self):
        self.active()
        raw = b'full original bytes\n'
        saved = self.call('artifact', self.artifact(raw))
        self.now = 2001
        with self.assertRaises(PermissionError):
            self.call('status')
        self.assertTrue(self.membership.get(self.job_id)['expired'])
        self.assertEqual(raw, self.membership.read_artifact(self.job_id, 'fixture.txt'))
        self.assertEqual(saved['sha256'], self.membership.saved_artifacts(self.job_id)[0]['sha256'])
        self.now = 1001
        with self.assertRaises(PermissionError):
            self.membership.close(self.job_id)
        self.membership.close(self.job_id, authorized=True)
        with self.assertRaises(PermissionError):
            self.call('hello')
        self.assertEqual(raw, self.reopen().read_artifact(self.job_id, 'fixture.txt'))

    def test_actual_authority_validator_fences_new_work_without_replaying_prior(self):
        self.active()
        args = {'capability_id': 'public-fixture-fetch', 'input': {'url': 'public fixture'}}
        result = self.call('call', args)
        self.lease_live = False
        self.assertEqual(result, self.call('call', args))
        with self.assertRaisesRegex(Conflict, 'stale_cloud_task_lease'):
            self.call('call', args, 'another-new-call')
        self.assertEqual(1, len(self.gateway_calls))

    def test_absent_gateway_or_authority_is_gate_before_consuming_operation(self):
        self.active()
        self.membership = self.reopen(capability_gateway=None)
        with self.assertRaisesRegex(PermissionError, 'gateway_not_configured'):
            self.call('call', {'capability_id': 'public-fixture-fetch', 'input': {}})
        self.membership = self.reopen(lease_validator=None)
        with self.assertRaisesRegex(PermissionError, 'authority_not_configured'):
            self.call('task_submit', {'input': 'new task'})
        self.assertEqual(1, self.request_count())

    def test_complete_artifact_and_terminal_result_persist_outside_vm(self):
        self.active()
        raw = bytes(range(256)) * 600
        artifact = self.call('artifact', self.artifact(raw, 'complete.bin'))
        text = '完整原结果，不进行 trim\n' * 6000
        report = self.call('report', {'status': 'completed', 'result': text,
                                     'artifacts': [artifact['sha256']]})
        self.assertTrue(report['provider_reported'])
        self.assertFalse(report['execution_verified'])
        self.membership.close(self.job_id, authorized=True)
        self.now = 9000
        self.membership = self.reopen()
        self.assertEqual(raw, self.membership.read_artifact(self.job_id, 'complete.bin'))
        self.assertEqual(text, self.membership.saved_result(self.job_id)['result'])
        self.assertEqual(1, len(self.cli_calls))

    def test_report_cannot_reference_missing_or_tampered_artifact(self):
        self.active()
        with self.assertRaisesRegex(Conflict, 'artifact_missing'):
            self.call('report', {'status': 'completed', 'result': 'result', 'artifacts': ['a' * 64]})
        self.assertEqual(1, self.request_count())
        self.call('artifact', self.artifact())
        path = next(self.membership.artifacts.glob('*.bin'))
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(Conflict, 'saved_content_changed'):
            self.membership.saved_artifacts(self.job_id)

    def test_artifact_path_size_hash_and_base64_are_verified_before_intent(self):
        self.active()
        sample = self.artifact()
        for change in ({'name': '../escape'}, {'name': '__terminal_result'}, {'bytes': True},
                       {'bytes': 1}, {'sha256': 'b' * 64}, {'content_b64': 'not base64'},
                       {'encoding': 'utf8'}, {'bytes': self.jobs.max_bytes + 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.call('artifact', dict(sample, **change))
        self.assertEqual(1, self.request_count())

    def test_artifact_name_and_terminal_are_immutable_on_replay(self):
        self.active()
        original = self.call('artifact', self.artifact())
        self.assertEqual(original, self.call('artifact', self.artifact()))
        with self.assertRaises(Conflict):
            self.call('artifact', self.artifact(b'changed'), 'new-artifact-request')
        self.assertEqual(b'PUBLIC_ARTIFACT\n', self.membership.read_artifact(self.job_id, 'fixture.txt'))
        with self.assertRaises(Conflict):
            self.call('artifact', self.artifact(b'changed'))
        report = {'status': 'completed', 'result': 'result', 'artifacts': [original['sha256']]}
        self.call('report', report)
        with self.assertRaises(Conflict):
            self.call('report', dict(report, result='other'), 'new-report')

    def test_effectful_gateway_crash_is_original_unknown_no_retry_on_reopen(self):
        self.active()
        calls = []
        def failure(binding, action, args):
            calls.append(True)
            raise RuntimeError('SYNTHETIC_GONE_AFTER_EFFECT')
        self.membership = self.reopen(capability_gateway=failure)
        args = {'capability_id': 'public-fixture-fetch', 'input': {}}
        with self.assertRaises(RuntimeError):
            self.call('call', args)
        self.membership = self.reopen(capability_gateway=failure)
        reply = self.call('call', args)
        self.assertEqual('unknown', reply['state'])
        self.assertFalse(reply['automatic_retry'])
        self.assertEqual(1, len(calls))

    def test_simultaneous_same_request_dispatches_only_once(self):
        self.active()
        start, release = threading.Event(), threading.Event()
        calls = []
        def gateway(binding, action, args):
            calls.append(True)
            start.set()
            self.assertTrue(release.wait(5))
            return {'accepted': True}
        self.membership = self.reopen(capability_gateway=gateway)
        second = self.reopen(capability_gateway=gateway)
        body = self.envelope('call', {'capability_id': 'public-fixture-fetch', 'input': {}})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.membership.handle, TOKEN, body)
            self.assertTrue(start.wait(5))
            waiting = second.handle(TOKEN, body)
            self.assertEqual('unknown', waiting['state'])
            release.set()
            self.assertTrue(first.result(5)['accepted'])
        self.assertEqual(1, len(calls))

    def test_cloud_is_restricted_entry_with_fixed_project_source_and_owner_scope(self):
        self.active()
        receipt = self.call('task_submit', {'input': 'Self maintenance new work'})
        self.assertTrue(receipt['task_created'])
        self.assertEqual(receipt, self.call('task_submit', {'input': 'Self maintenance new work'}))
        status = self.call('task_status', {'entry_request_id': 'request-task_submit'})
        self.assertEqual(receipt['task_id'], status['task_id'])
        with self.store.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (receipt['task_id'],)).fetchone()
            context = json.loads(task['context'])
            self.assertEqual('mesh-self-maintenance', context['project_id'])
            self.assertEqual('openai-codex-cloud', context['ingress']['source'])
            self.assertEqual('leader:owner', task['scope'])
        with self.assertRaises(ValueError):
            self.call('task_submit', {'input': 'other', 'project_id': 'other'})
        with self.assertRaises(PermissionError):
            self.call('task_status', {'entry_request_id': 'foreign-owner-request'})

    def test_cloud_continue_only_its_own_source_task_with_original_native_identity(self):
        self.active()
        receipt = self.call('task_submit', {'input': 'new scoped task'})
        self.store.heartbeat('fixture-worker', ['leader'])
        task = self.store.claim('fixture-worker')
        self.assertEqual(receipt['task_id'], task['id'])
        checkpoint = {'harness': 'codex', 'thread_id': '11111111-1111-4111-8111-111111111111',
                      'side_effect_started': False}
        self.store.session_action(task['id'], 'fixture-worker', task['epoch'], 'commit',
                                  {'harness': 'codex', 'state': checkpoint, 'parts': 0})
        self.store.update_task(task['id'], 'fixture-worker', task['epoch'], checkpoint, 'full old result', 'completed')
        args = {'entry_request_id': 'request-task_submit', 'expected_epoch': task['epoch'],
                'task_result_sha256': hashlib.sha256(b'full old result').hexdigest(), 'instruction': 'new work only'}
        continuation = self.call('task_continue', args)
        self.assertEqual(task['id'], continuation['task_id'])
        self.assertEqual(checkpoint['thread_id'], continuation['native']['thread_id'])
        self.assertFalse(continuation['effect_markers_changed'])
        self.assertEqual(continuation, self.call('task_continue', args))
        with self.assertRaises(PermissionError):
            self.call('task_continue', dict(args, entry_request_id='foreign'), 'foreign-continue')

    def test_private_artifacts_remain_regular_0600_files(self):
        self.active()
        self.call('artifact', self.artifact())
        path = next(self.membership.artifacts.glob('*.bin'))
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(0o700, self.membership.artifacts.stat().st_mode & 0o777)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.membership.saved_artifacts(self.job_id)

    def test_ordinary_artifact_count_preserves_a_terminal_result_slot(self):
        self.active()
        for index in range(64):
            self.call('artifact', self.artifact(b'x', 'artifact-{}'.format(index)),
                      'artifact-request-{}'.format(index))
        with self.assertRaisesRegex(Conflict, 'capacity_exceeded'):
            self.call('artifact', self.artifact(b'x', 'artifact-over-capacity'), 'artifact-over-capacity')
        result = self.call('report', {'status': 'completed', 'result': 'full terminal', 'artifacts': []})
        self.assertTrue(result['provider_reported'])
        self.assertEqual(65, len(self.membership.saved_artifacts(self.job_id)))
        self.assertEqual('full terminal', self.membership.saved_result(self.job_id)['result'])

    def test_lease_validator_receives_same_original_binding_at_prepare_and_use(self):
        observed = []
        self.membership.lease_validator = lambda binding: observed.append(copy.deepcopy(binding)) or True
        self.active()
        self.assertGreaterEqual(len(observed), 2)
        for binding in observed:
            self.assertEqual(self.job_id, binding['job_id'])
            self.assertEqual(node_id(self.job_id), binding['node_id'])
            self.assertEqual('original-mesh-task', binding['mesh_task_id'])
            self.assertEqual('original-cloud-lease', binding['lease_id'])

    def test_revocation_racing_token_lookup_blocks_new_write_before_intent(self):
        self.active()
        original = self.membership._principal
        def revoke(token, action):
            result = original(token, action)
            self.membership.close(self.job_id, authorized=True)
            return result
        self.membership._principal = revoke
        with self.assertRaises(PermissionError):
            self.call('artifact', self.artifact())
        self.assertEqual(1, self.request_count())
        self.assertEqual([], self.membership.saved_artifacts(self.job_id))

    def test_admitted_report_does_not_revive_credential_closed_in_flight(self):
        self.active()
        save = self.membership._save_artifact
        def revoke_after_save(*args):
            result = save(*args)
            self.membership.close(self.job_id, authorized=True)
            return result
        self.membership._save_artifact = revoke_after_save
        result = self.call('report', {'status': 'completed', 'result': 'full admitted result', 'artifacts': []})
        self.assertEqual('closed', result['membership_state'])
        self.assertEqual('full admitted result', self.reopen().saved_result(self.job_id)['result'])
        with self.assertRaises(PermissionError):
            self.call('status')

    def test_scoped_real_loopback_http_complete_callback_not_real_cloud(self):
        self.prepare()
        self.jobs.submit(self.job_id, authorized=True)
        membership = self.membership
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                header = self.headers.get('Authorization', '')
                token = header[7:] if header.startswith('Bearer ') else ''
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    result = membership.handle(token, body)
                    self.send_response(200)
                except (PermissionError, ValueError, Conflict):
                    self.send_response(403)
                    result = {'error': 'scoped_request_rejected'}
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def post(body, token=TOKEN):
            req = urllib.request.Request('http://127.0.0.1:{}/cloud/node'.format(server.server_port),
                data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + token,
                                                        'Content-Type': 'application/json'})
            with opener.open(req, timeout=5) as response:
                return json.load(response)
        try:
            self.assertTrue(post(self.envelope('hello'))['authenticated_hello'])
            raw = bytes(range(256)) * 600
            saved = post(self.envelope('artifact', self.artifact(raw, 'complete.bin')))
            result = '完整回执\n' * 12000
            post(self.envelope('report', {'status': 'completed', 'result': result,
                                         'artifacts': [saved['sha256']]}))
            with self.assertRaises(urllib.error.HTTPError) as failure:
                post(self.envelope('status'), token=TOKEN + 'wrong')
            failure.exception.close()
            self.assertEqual(raw, membership.read_artifact(self.job_id, 'complete.bin'))
            self.assertEqual(result, self.reopen().saved_result(self.job_id)['result'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == '__main__':
    unittest.main()
