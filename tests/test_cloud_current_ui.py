"""Synthetic trusted-UI evidence tests, not real OpenAI tasks or GUI proof."""
import base64
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from assistant_mesh.cloud_jobs import CloudJobs, CommandFailure
from assistant_mesh.cloud_membership import CloudMembership, node_id
from assistant_mesh.cloud_ui import PROTOCOL, RECEIPT_SOURCE
from assistant_mesh.store import Conflict


class CurrentCloudUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-current-cloud-ui.')
        self.path = Path(self.temp.name) / 'private'
        self.now = 1000
        self.cli_calls, self.verifications = [], []

        def runner(*args, **kwargs):
            self.cli_calls.append((args, kwargs))
            raise CommandFailure('cli_timeout')

        def verifier(binding, receipt):
            self.verifications.append((copy.deepcopy(binding), copy.deepcopy(receipt)))
            return True  # Explicitly synthetic normal-UI authority fixture.

        self.options = {'executable': '/fixture/native-codex', 'codex_home': '/fixture/owner-home',
                        'runner': runner, 'clock': lambda: self.now, 'ui_receipt_verifier': verifier}
        self.jobs = self.reopen()
        self.payload = {'mesh_task_id': 'original-task', 'mesh_epoch': 3,
            'request_id': 'original-request', 'environment_id': 'env-fixture',
            'branch': 'main', 'prompt': 'PRIVATE_PROMPT_NOT_A_PROVIDER_ID'}
        self.job_id = self.jobs.intent(self.payload)['job_id']

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self, **changes):
        return CloudJobs(self.path, 'fixture-owner', {'env-fixture': ['main']},
                         **dict(self.options, **changes))

    def receipt(self, job_id=None):
        identity = '00000000-0000-4000-8000-000000000001'
        return dict(self.jobs.current_ui_binding(job_id or self.job_id), format=PROTOCOL,
            provider_task_id=identity, provider_url='https://chatgpt.com/local/' + identity,
            workflow='published_environment_task', execution_location='openai_cloud',
            launch={'evidence_sha256': 'a' * 64, 'observed_at': 1001},
            reopened={'evidence_sha256': 'b' * 64, 'observed_at': 1002})

    def bind(self):
        self.jobs.begin_ui_submission(self.job_id, authorized=True)
        return self.jobs.record_current_ui_receipt(self.job_id, self.receipt(), owner_confirmed=True)

    def test_default_does_not_broaden_legacy_url_or_claim_current_ui_authority(self):
        self.jobs.begin_ui_submission(self.job_id, authorized=True)
        with self.assertRaisesRegex(PermissionError, 'verifier_not_configured'):
            self.reopen(ui_receipt_verifier=None).record_current_ui_receipt(
                self.job_id, self.receipt(), owner_confirmed=True)
        with self.assertRaises(ValueError):
            self.jobs.record_ui_receipt(self.job_id, self.receipt()['provider_url'], owner_confirmed=True)
        with self.assertRaises(ValueError):
            self.reopen(ui_receipt_verifier=True)
        self.assertEqual([], self.cli_calls)
        self.assertEqual('submission_unknown', self.jobs.get(self.job_id)['submission_state'])

    def test_owner_pinned_ui_environment_cannot_be_mistaken_for_cli_environment_id(self):
        ui_jobs = self.reopen(ui_environments=['env-fixture'])
        with self.assertRaisesRegex(PermissionError, 'requires_current_ui'):
            ui_jobs.submit(self.job_id, authorized=True)
        self.assertFalse(ui_jobs.get(self.job_id)['submission_attempted'])
        self.assertEqual([], self.cli_calls)
        self.assertTrue(ui_jobs.begin_ui_submission(self.job_id, authorized=True)['submission_started'])
        ui_jobs.record_current_ui_receipt(self.job_id, self.receipt(), owner_confirmed=True)
        self.assertEqual('submitted', ui_jobs.submit(self.job_id, authorized=True)['submission_state'])
        with self.assertRaises(ValueError):
            self.reopen(ui_environments=['unconfigured-ref'])

    def test_current_ui_receipt_is_durable_same_intent_and_offline_without_reverification(self):
        before = dict(self.jobs.namespace)
        result = self.bind()
        self.assertEqual(RECEIPT_SOURCE, result['receipt_source'])
        self.assertEqual('official_ui', result['submission_channel'])
        self.assertFalse(result['cli_attempted'])
        self.assertFalse(result['execution_verified'])
        self.assertFalse(result['cloud_node_enrolled'])
        self.assertEqual(before, self.jobs.namespace)
        self.assertEqual(result, self.reopen(ui_receipt_verifier=None).get(self.job_id))
        self.assertEqual(result, self.reopen().record_current_ui_receipt(
            self.job_id, self.receipt(), owner_confirmed=True))
        self.assertEqual(1, len(self.verifications))
        self.assertEqual([], self.cli_calls)
        self.assertNotIn('PRIVATE_PROMPT', json.dumps(result))
        self.assertFalse(self.jobs.begin_ui_submission(self.job_id, authorized=True)['submission_started'])
        self.jobs.submit(self.job_id, authorized=True)
        self.assertEqual([], self.cli_calls)

    def test_missing_reopen_setup_local_runtime_or_changed_intent_rejected_before_verifier(self):
        self.jobs.begin_ui_submission(self.job_id, authorized=True)
        valid = self.receipt()
        changes = [{'workflow': 'environment_setup'}, {'execution_location': 'local'},
            {'mesh_epoch': 4}, {'mesh_epoch': True}, {'environment_id': 'wrong-env'},
            {'intent_sha256': 'c' * 64}, {'owner_binding_sha256': 'd' * 64},
            {'request_id': 'wrong-request'}, {'job_id': 'other-job'},
            {'reopened': {'evidence_sha256': 'b' * 64, 'observed_at': 1001}},
            {'launch': {'evidence_sha256': 'a' * 64, 'observed_at': 999}},
            {'reopened': {'evidence_sha256': 'a' * 64, 'observed_at': 1002}},
            {'reopened': {'evidence_sha256': 'b' * 64, 'observed_at': float('nan')}},
            {'provider_task_id': 'client-new-thread-123'}, {'provider_task_id': 'setup-123'}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.jobs.record_current_ui_receipt(self.job_id, dict(valid, **change), owner_confirmed=True)
        missing = dict(valid)
        del missing['reopened']
        with self.assertRaises(ValueError):
            self.jobs.record_current_ui_receipt(self.job_id, missing, owner_confirmed=True)
        self.assertEqual([], self.verifications)
        self.assertIsNone(self.jobs.get(self.job_id)['provider_task_id'])

    def test_unsafe_urls_and_unassociated_task_ids_are_not_receipts(self):
        self.jobs.begin_ui_submission(self.job_id, authorized=True)
        valid = self.receipt()
        url = valid['provider_url']
        urls = [url.replace('https:', 'http:'), url.replace('chatgpt.com', 'evil.example'),
                url + '?token=PRIVATE', url + '#', url + '/', url + '/other',
                url.replace('chatgpt.com', 'chatgpt.com:443'),
                url.replace('/local/', '/setup/'), url.replace('/local/', '/settings/'),
                url.replace('/local/', '/client-new-thread/'),
                url.replace('/local/', '/local/../'), url.replace('/local/', '/local%2f/'),
                url.replace('/local/', '/local\\/'), 'file:///private/local/task']
        for value in urls:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.jobs.record_current_ui_receipt(self.job_id, dict(valid, provider_url=value), owner_confirmed=True)
        self.assertEqual([], self.verifications)
        self.assertEqual([], self.cli_calls)

    def test_trusted_verifier_requires_strict_true_and_cannot_rewrite_receipt(self):
        self.jobs.begin_ui_submission(self.job_id, authorized=True)
        for result in (False, None, 1, 'yes'):
            jobs = self.reopen(ui_receipt_verifier=lambda *args: result)
            with self.subTest(result=result), self.assertRaises(PermissionError):
                jobs.record_current_ui_receipt(self.job_id, self.receipt(), owner_confirmed=True)
        with self.assertRaises(PermissionError):
            self.jobs.record_current_ui_receipt(self.job_id, self.receipt())
        original = self.receipt()

        def mutate(binding, receipt):
            binding['environment_id'] = 'other'
            receipt['provider_url'] = 'https://evil.example'
            return True

        self.reopen(ui_receipt_verifier=mutate).record_current_ui_receipt(
            self.job_id, original, owner_confirmed=True)
        self.assertEqual(original['provider_url'], self.jobs.get(self.job_id)['provider_url'])

    def test_original_cli_unknown_cannot_be_adopted_by_ui_or_replayed(self):
        self.jobs.submit(self.job_id, authorized=True)
        with self.assertRaises(Conflict):
            self.jobs.record_current_ui_receipt(self.job_id, self.receipt(), owner_confirmed=True)
        self.assertFalse(self.jobs.begin_ui_submission(self.job_id, authorized=True)['submission_started'])
        self.jobs.submit(self.job_id, authorized=True)
        self.assertEqual(1, len(self.cli_calls))
        self.assertEqual('submission_unknown', self.reopen().get(self.job_id)['submission_state'])
        with self.assertRaises(Conflict):
            self.jobs.intent(dict(self.payload, request_id='replacement'))

    def test_new_independent_ui_task_keeps_complete_old_unknown_row_and_namespace(self):
        self.jobs.submit(self.job_id, authorized=True)
        with self.jobs._transaction() as db:
            original = tuple(db.execute('SELECT * FROM cloud_jobs WHERE id=?', (self.job_id,)).fetchone())
            namespace = list(db.execute('SELECT * FROM cloud_job_metadata ORDER BY key'))
        other = self.jobs.intent(dict(self.payload, mesh_task_id='independent-new-task',
                                      request_id='independent-new-request'))['job_id']
        self.jobs.begin_ui_submission(other, authorized=True)
        self.jobs.record_current_ui_receipt(other, self.receipt(other), owner_confirmed=True)
        with self.jobs._transaction() as db:
            self.assertEqual(original, tuple(db.execute('SELECT * FROM cloud_jobs WHERE id=?',
                                                       (self.job_id,)).fetchone()))
            self.assertEqual(namespace, list(db.execute('SELECT * FROM cloud_job_metadata ORDER BY key')))
        self.assertEqual(1, len(self.cli_calls))

    def test_ui_verifier_raw_private_failure_does_not_leak_or_reopen_launch(self):
        self.jobs.begin_ui_submission(self.job_id, authorized=True)

        def failing(*args):
            raise RuntimeError('PRIVATE_ACCOUNT PRIVATE_CHAT PRIVATE_CREDENTIAL')

        with self.assertRaises(PermissionError) as caught:
            self.reopen(ui_receipt_verifier=failing).record_current_ui_receipt(
                self.job_id, self.receipt(), owner_confirmed=True)
        self.assertEqual('current_cloud_ui_verifier_unavailable', str(caught.exception))
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertFalse(self.reopen().begin_ui_submission(self.job_id, authorized=True)['submission_started'])
        self.assertIsNone(self.jobs.get(self.job_id)['provider_task_id'])
        self.assertEqual([], self.cli_calls)

    def test_current_receipt_cannot_be_bound_before_once_boundary_or_replace_saved_proof(self):
        with self.assertRaises(Conflict):
            self.jobs.record_current_ui_receipt(self.job_id, self.receipt(), owner_confirmed=True)
        self.bind()
        altered = self.receipt()
        altered['reopened']['evidence_sha256'] = 'c' * 64
        with self.assertRaises(Conflict):
            self.jobs.record_current_ui_receipt(self.job_id, altered, owner_confirmed=True)
        self.assertEqual(1, len(self.verifications))

    def test_two_instances_racing_keep_one_receipt_and_no_second_cloud_launch(self):
        self.jobs.begin_ui_submission(self.job_id, authorized=True)
        barrier = threading.Barrier(2)

        def verifier(*args):
            barrier.wait(timeout=5)
            return True

        first, second = self.reopen(ui_receipt_verifier=verifier), self.reopen(ui_receipt_verifier=verifier)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(jobs.record_current_ui_receipt, self.job_id, self.receipt(),
                                   owner_confirmed=True) for jobs in (first, second)]
            self.assertEqual(futures[0].result(5), futures[1].result(5))
        with self.jobs._transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM cloud_job_ui_receipts').fetchone()[0])
        self.assertEqual([], self.cli_calls)

    def test_current_observation_persists_evidence_and_never_calls_legacy_cli(self):
        self.bind()
        first = self.receipt()
        value = self.jobs.record_current_ui_observation(self.job_id, first, 'running', owner_confirmed=True)
        self.assertEqual('running', value['provider_status'])
        self.assertEqual('running', self.verifications[-1][0]['expected_provider_status'])
        newer = copy.deepcopy(first)
        newer['reopened'] = {'observed_at': 1004, 'evidence_sha256': 'c' * 64}
        complete = self.jobs.record_current_ui_observation(self.job_id, newer, 'completed', owner_confirmed=True)
        self.assertFalse(complete['execution_verified'])
        self.assertEqual('completed', self.reopen(ui_receipt_verifier=None).get(self.job_id)['provider_status'])
        with self.assertRaises(Conflict):
            self.jobs.record_current_ui_observation(self.job_id, first, 'running', owner_confirmed=True)
        with self.assertRaises(Conflict):
            self.jobs.record_current_ui_observation(self.job_id, newer, 'error', owner_confirmed=True)
        with self.assertRaises(ValueError):
            self.jobs.record_current_ui_observation(self.job_id, newer, 'published', owner_confirmed=True)
        self.assertEqual('current_cloud_ui_observation_required', self.jobs.observe(self.job_id)['observation_error'])
        self.assertEqual('current_cloud_ui_observation_required', self.jobs.status_diagnostic(self.job_id)['error'])
        with self.assertRaisesRegex(Conflict, 'artifact_callback_required'):
            self.jobs.fetch_diff(self.job_id)
        with self.jobs._transaction() as db:
            self.assertEqual(2, db.execute('SELECT COUNT(*) FROM cloud_job_ui_observations').fetchone()[0])
        self.assertEqual([], self.cli_calls)

    def test_changed_receipt_or_status_proof_is_rejected_on_reopen(self):
        self.bind()
        self.jobs.record_current_ui_observation(self.job_id, self.receipt(), 'running', owner_confirmed=True)
        with self.jobs._transaction() as db:
            db.execute("UPDATE cloud_job_ui_observations SET fingerprint=?", ('d' * 64,))
        with self.assertRaises(Conflict):
            self.reopen().get(self.job_id)
        self.assertEqual([], self.cli_calls)

    def test_current_identity_supports_scoped_node_and_full_offline_artifact_not_execution_attestation(self):
        membership = CloudMembership(self.jobs, lease_validator=lambda binding: True)
        token = 'SYNTHETIC_CURRENT_UI_NODE_' + 'a' * 40
        grant = {'format': 'mesh-cloud-membership/1', 'mesh_task_id': 'original-task',
            'mesh_epoch': 3, 'lease_id': 'original-lease', 'request_id': 'original-request',
            'expires_at': 2000, 'callback_url': 'https://mesh.example.test/cloud/node',
            'scopes': ['node:hello', 'task:read', 'artifact:write'], 'capabilities': [],
            'enrollment': {'format': 'mesh-enrollment/1', 'mesh_id': 'fixture-mesh',
                'node_id': node_id(self.job_id), 'owner_binding': 'fixture-owner',
                'repo_pin': {'repository': 'https://github.com/example/mesh', 'commit': 'a' * 40},
                'runtime': {'kind': 'temporary', 'expires_at': 2000, 'state_ref': 'mesh:persistent'},
                'join': {'peer_id': 'owner-authority', 'authority_id': 'fixture-authority',
                         'route_ref': 'fixture-route', 'credential_ref': 'fixture-token', 'trust_ref': 'fixture-root'},
                'continuation': {'project_id': 'fixture-project', 'session_ref': 'fixture-session',
                                 'state_ref': 'fixture-state'}}}
        membership.prepare(self.job_id, grant, token_sha256=hashlib.sha256(token.encode()).hexdigest(), authorized=True)
        self.bind()
        envelope = {'format': 'mesh-cloud-request/1', 'job_id': self.job_id,
            'task_id': 'original-task', 'epoch': 3, 'lease_id': 'original-lease',
            'request_id': 'hello', 'action': 'hello', 'arguments': {}}
        hello = membership.handle(token, envelope)
        self.assertEqual('openai-codex-cloud', hello['display_id'])
        self.assertTrue(hello['authenticated_hello'])
        self.assertFalse(hello['execution_verified'])
        raw = bytes(range(256)) * 600
        args = {'name': 'full.bin', 'encoding': 'base64', 'content_b64': base64.b64encode(raw).decode(),
                'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'media_type': 'application/octet-stream'}
        membership.handle(token, dict(envelope, request_id='artifact', action='artifact', arguments=args))
        self.now = 3000
        membership.close(self.job_id, authorized=True)
        self.assertEqual(raw, CloudMembership(self.reopen(ui_receipt_verifier=None)).read_artifact(self.job_id, 'full.bin'))
        self.assertEqual([], self.cli_calls)


if __name__ == '__main__':
    unittest.main()
