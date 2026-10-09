"""Offline fake CLI tests. No login, provider job, enrollment or API call."""
import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from assistant_mesh.cloud_jobs import CloudJobs, CommandFailure, CommandResult, run_cli
from assistant_mesh.store import Conflict


URL = 'https://chatgpt.com/codex/tasks/task_fixture'


def page(status='pending', provider='task_fixture', environment='env-fixture', cursor=None):
    return CommandResult(0, json.dumps({'tasks': [{'id': provider,
                         'url': 'https://chatgpt.com/codex/tasks/' + provider,
                         'environment_id': environment, 'status': status,
                         'title': 'PRIVATE_PROVIDER_TITLE', 'summary': 'PRIVATE_SUMMARY'}],
                         'cursor': cursor}).encode(), b'')


class FakeCLI:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.on_call = None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.on_call:
            self.on_call(argv, kwargs)
        if not self.responses:
            raise AssertionError('unexpected fake CLI call')
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class CloudJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-cloud-jobs.')
        self.path = Path(self.temp.name) / 'private'
        self.fake = FakeCLI()
        self.options = {'executable': '/fixture/native-codex', 'codex_home': '/fixture/native-home',
                        'runner': self.fake, 'clock': lambda: 1234.0}
        self.environments = {'env-fixture': ['main', 'feature/mesh']}
        self.jobs = CloudJobs(self.path, 'owner-profile-fixture', self.environments, **self.options)
        self.payload = {'mesh_task_id': 'mesh-task-1', 'mesh_epoch': 3, 'request_id': 'request-1',
                        'environment_id': 'env-fixture', 'branch': 'main',
                        'prompt': 'PRIVATE_PROMPT: 自主研究能力；不要复制本机凭据。'}

    def tearDown(self):
        self.temp.cleanup()

    def intent(self, payload=None):
        return self.jobs.intent(self.payload if payload is None else payload)['job_id']

    def reopen(self, **changes):
        options = dict(self.options, **changes)
        return CloudJobs(self.path, 'owner-profile-fixture', self.environments, **options)

    def bind(self):
        job_id = self.intent()
        self.fake.responses.append(CommandResult(0, (URL + '\n').encode(), b''))
        self.jobs.submit(job_id, authorized=True)
        return job_id

    def count(self):
        with contextlib.closing(sqlite3.connect(str(self.jobs.database))) as db, db:
            return db.execute('SELECT COUNT(*) FROM cloud_jobs').fetchone()[0]

    def test_normal_flow_is_one_exec_observe_and_private_exact_diff(self):
        job_id = self.bind()
        self.fake.responses += [page('Pending'), CommandResult(1, b'[PENDING] PRIVATE_TITLE\n', b''),
                                page('Ready'), CommandResult(0, b'diff --git a/a b/a\nPRIVATE_DIFF\n', b'')]
        self.assertEqual('pending', self.jobs.observe(job_id)['provider_status'])
        diagnostic = self.jobs.status_diagnostic(job_id)
        self.assertTrue(diagnostic['diagnostic_recognized'])
        self.assertEqual(1, diagnostic['cli_exit_code'])
        self.assertEqual('pending', diagnostic['reported_status'])
        self.assertFalse(diagnostic['may_resubmit'])
        self.assertNotIn('PRIVATE_', json.dumps(diagnostic))
        self.assertEqual('ready', self.jobs.observe(job_id)['provider_status'])
        artifact = self.jobs.fetch_diff(job_id)
        self.assertEqual(b'diff --git a/a b/a\nPRIVATE_DIFF\n', Path(artifact['private_path']).read_bytes())
        self.assertEqual(hashlib.sha256(Path(artifact['private_path']).read_bytes()).hexdigest(), artifact['sha256'])
        self.assertEqual(0o600, stat.S_IMODE(Path(artifact['private_path']).stat().st_mode))
        self.assertFalse(artifact['applied'])
        self.assertFalse(artifact['artifact_content_verified'])
        self.assertFalse(artifact['cloud_node_enrolled'])
        self.assertEqual([artifact], self.reopen().saved_diffs(job_id))
        self.assertEqual('submitted', self.reopen().submit(job_id, authorized=True)['submission_state'])
        self.assertEqual(['exec', 'list', 'status', 'list', 'diff'], [argv[2] for argv, _ in self.fake.calls])
        self.assertEqual(['/fixture/native-codex', 'cloud', 'exec', '--env', 'env-fixture',
                          '--branch', 'main', '--attempts', '1', '-'], self.fake.calls[0][0])
        self.assertEqual(self.payload['prompt'].encode(), self.fake.calls[0][1]['input'])
        self.assertNotIn('PRIVATE_PROMPT', repr(self.fake.calls[0][0]))

    def test_constructor_and_intent_are_offline_and_do_not_claim_enrollment(self):
        job_id = self.intent()
        view = self.jobs.get(job_id)
        self.assertEqual('intent', view['submission_state'])
        self.assertTrue(view['can_submit'])
        self.assertFalse(view['cli_attempted'])
        self.assertFalse(view['execution_verified'])
        self.assertFalse(view['cloud_node_enrolled'])
        self.assertFalse(view['native_tools_intercepted'])
        self.assertEqual([], self.fake.calls)
        self.assertNotIn('PRIVATE_', json.dumps(view))

    def test_identical_intent_survives_reopen_and_returns_same_id(self):
        first = self.jobs.intent(self.payload)
        second = self.reopen().intent(dict(reversed(list(self.payload.items()))))
        self.assertTrue(first['intent_created'])
        self.assertFalse(second['intent_created'])
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(1, self.count())

    def test_same_task_cannot_create_second_job_using_new_request_or_epoch(self):
        self.intent()
        for change in ({'request_id': 'new-request'}, {'mesh_epoch': 4}, {'prompt': 'changed'},
                       {'branch': 'feature/mesh'}, {'mesh_task_id': 'other-task'}):
            with self.subTest(change=change), self.assertRaises(Conflict):
                self.intent(dict(self.payload, **change))
        self.assertEqual(1, self.count())

    def test_unknown_ids_and_unconfigured_environment_branch_are_rejected(self):
        with self.assertRaises(PermissionError):
            self.jobs.get('missing')
        for change in ({'environment_id': 'other-env'}, {'branch': 'other-branch'}):
            with self.subTest(change=change), self.assertRaises(PermissionError):
                self.intent(dict(self.payload, **change))
        self.assertEqual(0, self.count())

    def test_intent_schema_type_and_utf8_boundaries(self):
        bad = [dict(self.payload, actor='operator'), dict(self.payload, mesh_epoch=True),
               dict(self.payload, mesh_epoch=0), dict(self.payload, mesh_epoch='1'),
               dict(self.payload, prompt=''), dict(self.payload, prompt='a\x00b'),
               dict(self.payload, prompt='é' * 32769), dict(self.payload, prompt='\ud800'),
               dict(self.payload, request_id='request\n'), dict(self.payload, request_id='\ud800')]
        bad.append({key: value for key, value in self.payload.items() if key != 'mesh_task_id'})
        for payload in bad:
            with self.subTest(payload_keys=tuple(payload)), self.assertRaises(ValueError):
                self.intent(payload)
        self.assertEqual(0, self.count())

    def test_effectful_submit_requires_explicit_true_from_trusted_caller(self):
        job_id = self.intent()
        for value in (False, None, 1, 'yes'):
            with self.subTest(value=value), self.assertRaises(PermissionError):
                self.jobs.submit(job_id, authorized=value)
        self.assertEqual([], self.fake.calls)
        self.assertFalse(self.jobs.get(job_id)['cli_attempted'])

    def test_submission_boundary_is_durable_before_cli_launch(self):
        job_id = self.intent()

        def inspect(argv, kwargs):
            with contextlib.closing(sqlite3.connect(str(self.jobs.database))) as db, db:
                self.assertEqual(('submitting', 1), db.execute(
                    'SELECT state,attempted FROM cloud_jobs WHERE id=?', (job_id,)).fetchone())
            self.assertFalse(self.reopen().get(job_id)['can_submit'])

        self.fake.on_call = inspect
        self.fake.responses.append(CommandResult(0, URL.encode(), b''))
        self.assertEqual('submitted', self.jobs.submit(job_id, authorized=True)['submission_state'])

    def test_timeout_or_lost_reply_never_resubmits_after_restart(self):
        job_id = self.intent()
        self.fake.responses.append(CommandFailure('cli_timeout'))
        first = self.jobs.submit(job_id, authorized=True)
        self.assertEqual('submission_unknown', first['submission_state'])
        self.assertEqual('cli_timeout', first['submission_error'])
        self.assertFalse(first['can_submit'])
        self.assertTrue(first['cli_attempted'])
        self.assertEqual(first, self.reopen().submit(job_id, authorized=True))
        self.assertEqual(1, len(self.fake.calls))
        self.assertEqual('provider_identity_unknown', self.jobs.observe(job_id)['observation_error'])
        self.assertEqual(1, len(self.fake.calls))

    def test_process_crash_after_boundary_is_unknown_even_without_failure_update(self):
        job_id = self.intent()
        self.fake.responses.append(RuntimeError('PRIVATE_FAILURE'))
        with self.assertRaises(RuntimeError):
            self.jobs.submit(job_id, authorized=True)
        self.assertEqual('submission_unknown', self.reopen().submit(job_id, authorized=True)['submission_state'])
        self.assertEqual(1, len(self.fake.calls))

    def test_launch_failure_is_conservatively_unknown_not_automatic_retry(self):
        job_id = self.intent()
        self.fake.responses.append(FileNotFoundError('PRIVATE_PATH'))
        view = self.jobs.submit(job_id, authorized=True)
        self.assertEqual('cli_runtime_failure', view['submission_error'])
        self.assertEqual('submission_unknown', view['submission_state'])
        self.jobs.submit(job_id, authorized=True)
        self.assertEqual(1, len(self.fake.calls))
        self.assertNotIn('PRIVATE_', json.dumps(view))

    def test_nonzero_exec_stderr_is_not_exposed_or_retried(self):
        job_id = self.intent()
        self.fake.responses.append(CommandResult(1, b'', b'PRIVATE_TOKEN PRIVATE_ACCOUNT PRIVATE_PROMPT'))
        view = self.jobs.submit(job_id, authorized=True)
        self.assertEqual('cli_exec_nonzero', view['submission_error'])
        self.assertNotIn('PRIVATE_', json.dumps(view))
        self.jobs.submit(job_id, authorized=True)
        self.assertEqual(1, len(self.fake.calls))

    def test_bad_receipts_never_bind_provider_or_enable_retry(self):
        bad = [b'', b'\xff', b'https://evil.example/codex/tasks/task_fixture',
               b'https://chatgpt.com/codex/tasks/task_fixture?token=PRIVATE',
               b'https://chatgpt.com:443/codex/tasks/task_fixture',
               b'https://chatgpt.com/codex/tasks/task_fixture\nhttps://chatgpt.com/codex/tasks/other',
               b'https://chatgpt.com/codex/tasks/../../file']
        for number, stdout in enumerate(bad):
            payload = dict(self.payload, mesh_task_id='task-' + str(number), request_id='req-' + str(number))
            job_id = self.intent(payload)
            self.fake.responses.append(CommandResult(0, stdout, b''))
            with self.subTest(number=number):
                view = self.jobs.submit(job_id, authorized=True)
                self.assertEqual('cli_exec_invalid_receipt', view['submission_error'])
                self.assertIsNone(view['provider_task_id'])
                self.assertFalse(view['can_submit'])

    def test_two_instances_racing_can_launch_only_once(self):
        job_id = self.intent()
        other = self.reopen()
        started, finish = threading.Event(), threading.Event()

        def block(argv, kwargs):
            started.set()
            self.assertTrue(finish.wait(5))

        self.fake.on_call = block
        self.fake.responses.append(CommandResult(0, URL.encode(), b''))
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.jobs.submit, job_id, authorized=True)
            self.assertTrue(started.wait(5))
            second = executor.submit(other.submit, job_id, authorized=True).result(5)
            self.assertEqual('submission_unknown', second['submission_state'])
            finish.set()
            self.assertEqual('submitted', first.result(5)['submission_state'])
        self.assertEqual(1, len(self.fake.calls))

    def test_environment_revocation_prevents_unstarted_exec_but_allows_observation(self):
        job_id = self.intent()
        restricted = CloudJobs(self.path, 'owner-profile-fixture', {'env-other': ['main']}, **self.options)
        with self.assertRaises(PermissionError):
            restricted.submit(job_id, authorized=True)
        self.assertFalse(restricted.get(job_id)['cli_attempted'])
        self.assertEqual([], self.fake.calls)

    def test_runtime_home_executable_and_owner_namespace_cannot_change(self):
        self.intent()
        for change in ({'codex_home': '/fixture/other-home'}, {'executable': '/fixture/other-codex'}):
            with self.subTest(change=change), self.assertRaises(Conflict):
                self.reopen(**change)
        with self.assertRaises(Conflict):
            CloudJobs(self.path, 'other-owner', self.environments, **self.options)

    def test_managed_runner_environment_excludes_api_keys_and_custom_backend(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'PRIVATE_KEY', 'CODEX_API_KEY': 'PRIVATE_KEY',
                                        'OPENAI_BASE_URL': 'https://private.example',
                                        'CODEX_CLOUD_TASKS_BASE_URL': 'https://private.example'}):
            self.jobs = self.reopen()
        self.bind()
        env = self.fake.calls[0][1]['env']
        for name in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'CODEX_CLOUD_TASKS_BASE_URL'):
            self.assertNotIn(name, env)
        self.assertEqual('/fixture/native-home', env['CODEX_HOME'])

    def test_pagination_uses_exact_id_without_exposing_other_tasks(self):
        job_id = self.bind()
        self.fake.responses += [page(provider='other', cursor='next'), page('ready')]
        view = self.jobs.observe(job_id)
        self.assertEqual('ready', view['provider_status'])
        self.assertEqual('next', self.fake.calls[-1][0][-1])
        self.assertNotIn('PRIVATE_', json.dumps(view))

    def test_not_observed_and_read_failure_keep_original_provider_and_no_resubmit(self):
        job_id = self.bind()
        self.fake.responses += [CommandResult(0, b'{"tasks":[]}', b''),
                                CommandResult(1, b'', b'PRIVATE_AUTH_ERROR')]
        view = self.jobs.observe(job_id)
        self.assertEqual('provider_not_observed', view['observation_error'])
        self.assertEqual('provider_observation_unavailable', self.jobs.observe(job_id)['observation_error'])
        self.assertEqual('task_fixture', self.jobs.submit(job_id, authorized=True)['provider_task_id'])
        self.assertEqual(['exec', 'list', 'list'], [argv[2] for argv, _ in self.fake.calls])

    def test_invalid_json_cursor_or_environment_cannot_set_ready(self):
        job_id = self.bind()
        invalid = [CommandResult(0, b'not json', b''), page('ready', environment='other-env'),
                   CommandResult(0, b'{"tasks":[],"cursor":"x"}', b''),
                   CommandResult(0, b'{"tasks":[],"cursor":"x"}', b'')]
        self.fake.responses += invalid
        for _ in range(3):
            self.assertEqual('provider_observation_unavailable', self.jobs.observe(job_id)['observation_error'])
        self.assertIsNone(self.jobs.get(job_id)['provider_status'])

    def test_unknown_future_provider_status_remains_unrecognized_not_completion(self):
        job_id = self.bind()
        self.fake.responses.append(page('PRIVATE_FUTURE_STATE'))
        view = self.jobs.observe(job_id)
        self.assertEqual('unrecognized', view['provider_status'])
        self.assertNotIn('PRIVATE_', json.dumps(view))
        with self.assertRaises(Conflict):
            self.jobs.fetch_diff(job_id)

    def test_status_exit_one_is_valid_for_pending_applied_error_not_resubmit(self):
        job_id = self.bind()
        for state, code in [('PENDING', 1), ('READY', 0), ('APPLIED', 1), ('ERROR', 1)]:
            self.fake.responses.append(CommandResult(code, ('[' + state + '] PRIVATE_TITLE\n').encode(), b''))
            value = self.jobs.status_diagnostic(job_id)
            self.assertTrue(value['diagnostic_recognized'])
            self.assertEqual(state.lower(), value['reported_status'])
            self.assertFalse(value['may_resubmit'])
        self.fake.responses.append(CommandResult(1, b'login PRIVATE_ACCOUNT failed', b''))
        self.assertFalse(self.jobs.status_diagnostic(job_id)['diagnostic_recognized'])
        self.assertIsNone(self.jobs.get(job_id)['provider_status'])

    def test_reconciliation_requires_owner_and_exact_current_profile_environment(self):
        job_id = self.intent()
        self.fake.responses.append(CommandFailure('cli_timeout'))
        self.jobs.submit(job_id, authorized=True)
        with self.assertRaises(PermissionError):
            self.jobs.reconcile(job_id, URL)
        self.fake.responses.append(page(environment='other-env'))
        with self.assertRaises(Conflict):
            self.jobs.reconcile(job_id, URL, owner_confirmed=True)
        self.fake.responses.append(page('ready'))
        view = self.jobs.reconcile(job_id, URL, owner_confirmed=True)
        self.assertEqual('submitted', view['submission_state'])
        self.assertEqual('owner_reconciliation', view['receipt_source'])
        self.assertFalse(view['execution_verified'])
        self.jobs.submit(job_id, authorized=True)
        self.assertEqual(['exec', 'list', 'list'], [argv[2] for argv, _ in self.fake.calls])
        with self.assertRaises(Conflict):
            self.jobs.reconcile(job_id, URL + '_other', owner_confirmed=True)

    def test_unique_provider_id_cannot_bind_second_mesh_task(self):
        self.bind()
        job_id = self.intent(dict(self.payload, mesh_task_id='second-task', request_id='second-request'))
        self.fake.responses.append(CommandResult(0, URL.encode(), b''))
        with self.assertRaises(Conflict):
            self.jobs.submit(job_id, authorized=True)
        self.assertEqual('submission_unknown', self.jobs.get(job_id)['submission_state'])
        self.jobs.submit(job_id, authorized=True)
        self.assertEqual(2, len(self.fake.calls))

    def test_diff_not_ready_nonzero_and_content_addressed_retrieval_no_apply(self):
        job_id = self.bind()
        with self.assertRaises(Conflict):
            self.jobs.fetch_diff(job_id)
        self.fake.responses += [page('ready'), CommandResult(1, b'', b'PRIVATE_DIFF_ERROR')]
        self.jobs.observe(job_id)
        with self.assertRaises(CommandFailure):
            self.jobs.fetch_diff(job_id)
        self.fake.responses += [CommandResult(0, b'PATCH', b''), CommandResult(0, b'PATCH', b'')]
        first = self.jobs.fetch_diff(job_id)
        second = self.jobs.fetch_diff(job_id)
        self.assertEqual(first, second)
        self.assertEqual(1, len(list(self.jobs.artifacts.glob('*.patch'))))
        self.assertNotIn('apply', [argv[2] for argv, _ in self.fake.calls])

    def test_diff_conflict_does_not_overwrite_existing_bytes(self):
        job_id = self.bind()
        self.fake.responses += [page('ready'), CommandResult(0, b'PATCH', b'')]
        self.jobs.observe(job_id)
        artifact = self.jobs.fetch_diff(job_id)
        destination = Path(artifact['private_path'])
        destination.write_bytes(b'OWNER_EXISTING_BYTES')
        self.fake.responses.append(CommandResult(0, b'PATCH', b''))
        with self.assertRaises(Conflict):
            self.jobs.fetch_diff(job_id)
        self.assertEqual(b'OWNER_EXISTING_BYTES', destination.read_bytes())
        with self.assertRaises(Conflict):
            self.reopen().saved_diffs(job_id)

    def test_private_modes_and_unsafe_paths_rejected_without_chmod(self):
        self.assertEqual(0o700, stat.S_IMODE(self.path.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.jobs.database.stat().st_mode))
        self.path.chmod(0o755)
        with self.assertRaises(ValueError):
            self.reopen()
        self.assertEqual(0o755, stat.S_IMODE(self.path.stat().st_mode))
        self.path.chmod(0o700)
        self.jobs.database.chmod(0o644)
        with self.assertRaises(ValueError):
            self.reopen()
        self.jobs.database.chmod(0o600)
        link = Path(self.temp.name) / 'linked'
        link.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(ValueError):
            CloudJobs(link, 'owner-profile-fixture', self.environments, **self.options)

    def test_database_hardlink_or_changed_intent_is_rejected(self):
        job_id = self.intent()
        duplicate = Path(self.temp.name) / 'duplicate.sqlite'
        os.link(str(self.jobs.database), str(duplicate))
        with self.assertRaises(ValueError):
            self.jobs.get(job_id)
        duplicate.unlink()
        with contextlib.closing(sqlite3.connect(str(self.jobs.database))) as db, db:
            db.execute('UPDATE cloud_jobs SET body=? WHERE id=?', ('{}', job_id))
        with self.assertRaises(Conflict):
            self.jobs.get(job_id)

    def test_configuration_does_not_allow_argv_injection_or_implicit_branch(self):
        for environments in ({}, {'env': []}, {'--env': ['main']}, {'env': ['--branch']},
                             {'env': ['../bad']}, {'env': ['main\nother']}):
            with self.subTest(environments=environments), self.assertRaises(ValueError):
                CloudJobs(self.path, 'owner-profile-fixture', environments, **self.options)
        with self.assertRaises(ValueError):
            self.reopen(executable='codex')

    def test_output_limit_and_invalid_result_are_unknown_no_raw_diagnostic(self):
        job_id = self.intent()
        self.jobs.max_bytes = 1024
        self.fake.responses.append(CommandResult(0, b'PRIVATE_' * 200, b''))
        self.assertEqual('cli_output_limit', self.jobs.submit(job_id, authorized=True)['submission_error'])
        next_job = self.intent(dict(self.payload, mesh_task_id='task-two', request_id='request-two'))
        self.fake.responses.append(CommandResult(0, 'not-bytes', b''))
        self.assertEqual('cli_invalid_result', self.jobs.submit(next_job, authorized=True)['submission_error'])

    def test_default_runner_uses_real_local_subprocess_without_shell_or_network(self):
        result = run_cli([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.write("local")'],
                         input=b'FIXTURE_ONLY', cwd=self.path, env=dict(os.environ), timeout=5, max_bytes=1024)
        self.assertEqual(CommandResult(0, b'FIXTURE_ONLY', b'local'), result)

    def test_default_runner_timeout_and_output_limit_are_fixed_categories(self):
        with self.assertRaisesRegex(CommandFailure, '^cli_timeout$'):
            run_cli([sys.executable, '-c', 'import time; time.sleep(1)'],
                    input=None, cwd=self.path, env=dict(os.environ), timeout=0.05, max_bytes=1024)
        with self.assertRaisesRegex(CommandFailure, '^cli_output_limit$'):
            run_cli([sys.executable, '-c', 'print("x" * 2048)'],
                    input=None, cwd=self.path, env=dict(os.environ), timeout=5, max_bytes=1024)


if __name__ == '__main__':
    unittest.main()
