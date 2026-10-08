"""Offline native-log fixtures plus real loopback API/Worker ledger integration.

The fixture backend is explicitly NOT a model/native runtime acceptance claim.
No real Codex, account, remote server, systemd unit, or paid API is started.
"""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.server import API, ThreadingHTTPServer
from assistant_mesh.worker import Client, Worker
from scripts import probe_worker_shell as probe


class WorkerShellProbeTests(unittest.TestCase):
    thread_id = '01a11b4b-a008-7670-b172-ee8d02ab3fae'
    turn_id = '01a11b74-271f-7852-bb37-523928cc238d'
    old_turn = '01a11b4b-b440-7de1-a1a1-bffb508ffed4'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / 'roster-one'
        self.home.mkdir(mode=0o700)
        self.second_home = self.root / 'roster-two'
        self.second_home.mkdir(mode=0o700)
        self.operator_token = self.private('operator.token', 'a' * 64, raw=True)
        self.worker_token = self.private('worker.token', 'b' * 64, raw=True)
        self.capabilities = ['agent', 'codex.native', 'mesh.node:fixture']
        self.server_config = {'node_id': 'authority-not-worker-id', 'database': str(self.root / 'ledger.sqlite'),
            'peers': [{'role': 'operator', 'token_file': str(self.operator_token)},
                      {'role': 'worker', 'node': 'fixture', 'capabilities': self.capabilities,
                       'token_file': str(self.worker_token)}]}
        self.api = API(self.server_config)
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path, body))
                peer = owner.api.principal(self.headers.get('Authorization', ''))
                status = 200
                try:
                    if peer is None:
                        raise PermissionError()
                    result = owner.api.dispatch('POST', self.path, body, peer)
                except PermissionError:
                    status, result = 403, {'error': 'denied'}
                except ValueError:
                    status, result = 400, {'error': 'invalid_request'}
                data = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()
        url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.operator = {'control_url': url, 'token_file': str(self.operator_token)}
        self.worker = {'node_id': 'fixture', 'control_url': url, 'token_file': str(self.worker_token),
            'capabilities': self.capabilities, 'codex': {'auth_home': str(self.home),
                'workspace': str(self.root), 'executable': '/fixture/complete-package/bin/codex'},
            'codex_accounts': [{'auth_home': str(self.home)}, {'auth_home': str(self.second_home)}]}
        self.operator_path = self.private('operator.json', self.operator)
        self.worker_path = self.private('worker.json', self.worker)
        self.server_path = self.private('server.json', self.server_config)
        self.state_path = self.root / 'state.json'
        self.client = Client(self.operator)
        self.subject = probe.Probe(self.client, self.worker, self.server_config)
        self.state = self.load(create=True)
        self.rollout = self.home / 'sessions/2026/10/08' / (
            'rollout-2026-10-08T03-04-05-' + self.thread_id + '.jsonl')
        self.records = self.native_records()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(3)
        self.temporary.cleanup()

    def private(self, name, value, raw=False):
        path = self.root / name
        path.write_text(value if raw else json.dumps(value), encoding='utf8')
        path.chmod(0o600)
        return path

    def load(self, create=False, path=None, **changes):
        original_mkdtemp = tempfile.mkdtemp
        def fixture_mkdtemp(*args, **kwargs):
            if not args:
                kwargs.setdefault('dir', str(self.root))
            return original_mkdtemp(*args, **kwargs)
        with patch('scripts.probe_worker_shell.tempfile.mkdtemp',
                   side_effect=fixture_mkdtemp):
            return probe.load_state(path or self.state_path, changes.get('operator', self.operator),
                                    changes.get('worker', self.worker), create=create)

    def native_records(self, shell_path=None, output=None, name='functions.exec'):
        command = 'sha256sum -- ' + (shell_path or self.state['challenge'])
        return [
            {'type': 'session_meta', 'payload': {'id': self.thread_id,
                'timestamp': '2026-10-08T03:04:05.321Z', 'cwd': str(self.root),
                'cli_version': '0.159.2', 'model_provider': 'openai'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': self.turn_id}},
            {'type': 'turn_context', 'payload': {'turn_id': self.turn_id}},
            {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': 'PRIVATE REASONING NEVER OUTPUT'}},
            {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'call_id': 'native-shell',
                'name': name, 'input': 'text(await tools.exec_command({cmd: ' + json.dumps(command) + '}));'}},
            {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': 'native-shell',
                'output': output if output is not None else json.dumps({'exit_code': 0,
                    'output': self.state['challenge_sha256'] + '  ' + self.state['challenge']})}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': self.turn_id,
                'last_agent_message': self.state['challenge_sha256']}},
        ]

    def write_rollout(self, records=None, path=None):
        selected = path or self.rollout
        # parents=True applies the default mode to intermediate directories;
        # an SSH umask of 0002 must not turn this private fixture into 0775.
        directory = self.root
        for component in selected.parent.relative_to(self.root).parts:
            directory = directory / component
            directory.mkdir(mode=0o700, exist_ok=True)
        selected.write_text(''.join(json.dumps(record) + '\n' for record in (records or self.records)), encoding='utf8')
        selected.chmod(0o600)
        return selected

    def complete(self, **changes):
        self.subject.submit(self.state)
        checkpoint = {'thread_id': self.thread_id, 'turn_id': self.turn_id, 'harness': 'codex',
                      'codex_node': 'fixture', 'codex_auth_home': str(self.home)}
        checkpoint.update(changes.pop('checkpoint', {}))
        with self.api.store.transaction() as db:
            db.execute("UPDATE tasks SET status=?,node=?,epoch=1,result=?,checkpoint=? WHERE id=?",
                (changes.get('status', 'completed'), changes.get('node', 'fixture'),
                 changes.get('result', self.state['challenge_sha256']), json.dumps(checkpoint), self.state['task']['id']))
        self.write_rollout()
        return self.subject.read(self.state), checkpoint

    def test_state_is_private_immutable_isolated_and_expected_hash_absent_from_prompt(self):
        original = self.state_path.read_bytes()
        self.assertEqual(self.state, self.load(create=True))
        self.assertEqual(original, self.state_path.read_bytes())
        self.assertNotIn(self.state['challenge_sha256'], self.state['task']['input'])
        self.assertEqual(0o600, self.state_path.stat().st_mode & 0o777)
        challenge = Path(self.state['challenge'])
        self.assertEqual(0o600, challenge.stat().st_mode & 0o777)
        self.assertEqual(0o700, challenge.parent.stat().st_mode & 0o777)
        self.assertIn('mesh.node:fixture', self.state['task']['required'])
        context = self.state['task']['context']
        self.assertFalse(context['session_scope'].startswith('leader:'))
        self.assertIn(self.state['run_id'], context['project_id'])
        self.assertIn(self.state['run_id'], context['agent_id'])

    def test_missing_state_default_read_does_not_create_challenge_or_task(self):
        before = set(self.root.iterdir())
        with self.assertRaisesRegex(probe.ProbeError, 'missing_use_explicit_submit'):
            self.load(path=self.root / 'absent.json')
        self.assertEqual(before, set(self.root.iterdir()))
        self.assertEqual([], self.requests)

    def test_state_task_challenge_and_configuration_changes_are_rejected(self):
        value = dict(self.state, task=dict(self.state['task'], input='changed task'))
        self.state_path.write_text(json.dumps(value))
        with self.assertRaisesRegex(probe.ProbeError, 'immutable_task_changed'):
            self.load(create=True)
        self.state_path.write_text(json.dumps(self.state))
        changed_worker = dict(self.worker, codex=dict(self.worker['codex'], executable='/other/runtime'))
        with self.assertRaisesRegex(probe.ProbeError, 'configuration_changed'):
            self.load(create=True, worker=changed_worker)
        Path(self.state['challenge']).write_bytes(b'changed')
        with self.assertRaisesRegex(probe.ProbeError, 'challenge_changed'):
            self.load(create=True)
        self.assertEqual([], self.requests)

    def test_missing_node_fence_is_not_silently_granted(self):
        changed = dict(self.worker, capabilities=['agent', 'codex.native'])
        with self.assertRaisesRegex(probe.ProbeError, 'capabilities_missing'):
            self.load(worker=changed)
        self.assertEqual([], self.requests)

    def test_submission_timeout_and_resubmission_reuse_exact_id_body(self):
        original = self.state_path.read_bytes()
        self.subject.submit(self.state)
        report, code = self.subject.poll(self.state, timeout=0.001, interval=0.1)
        self.assertEqual(2, code)
        self.assertTrue(report['retry_with_same_state'])
        self.subject.submit(self.load(create=True))
        submissions = [body for path, body in self.requests if path == '/v1/tasks']
        self.assertEqual([self.state['task'], self.state['task']], submissions)
        self.assertEqual(original, self.state_path.read_bytes())
        with self.api.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT attempts FROM tasks').fetchone()[0])

    def test_status_wrong_task_scope_or_node_is_rejected(self):
        observed, _ = self.complete()
        for changes in ({'id': 'different'}, {'scope': 'leader:owner'}, {'node': 'different'}, {'epoch': True}):
            with self.subTest(changes=changes), patch.object(self.client, 'request', return_value=dict(observed, **changes)):
                with self.assertRaises(probe.ProbeError):
                    self.subject.read(self.state)

    def test_real_api_completed_task_checkpoint_and_canonical_native_evidence(self):
        observed, checkpoint = self.complete()
        self.assertNotIn('checkpoint', observed)  # actual API intentionally projects native identity
        expected = probe.read_checkpoint(self.server_config, self.state, observed)
        self.assertEqual(checkpoint, expected)
        report, code = self.subject.poll(self.state)
        self.assertEqual(0, code)
        self.assertTrue(report['execution_verified'])
        self.assertTrue(report['native']['checkpoint_auth_binding_verified'])
        self.assertEqual(0, report['native']['authorized_profile_index'])
        self.assertEqual('0.159.2', report['native']['native_cli_version'])
        self.assertFalse(report['systemd_unit_verified'])
        self.assertFalse(report['native']['runtime_process_executable_verified'])
        self.assertTrue(report['native']['configured_runtime_identity_only'])
        self.assertEqual(hashlib.sha256(self.worker['codex']['executable'].encode()).hexdigest(),
                         report['native']['configured_executable_sha256'])
        self.assertFalse(report['runtime_read_only_enforced'])

    def test_without_server_config_selected_api_uuid_is_located_in_roster_only(self):
        self.complete()
        self.private('roster-one/auth.json', 'PRIVATE AUTH MUST NOT READ', raw=True)
        unrelated = self.rollout.with_name('rollout-2026-10-08T03-04-05-' + self.old_turn + '.jsonl')
        unrelated.write_text('THIS IS NOT JSON AND MUST NOT BE READ')
        subject = probe.Probe(self.client, self.worker)
        report, code = subject.poll(self.state)
        self.assertEqual(0, code)
        self.assertTrue(report['execution_verified'])
        self.assertFalse(report['native']['checkpoint_auth_binding_verified'])
        self.assertTrue(report['native']['auth_roster_verified'])
        duplicate = self.second_home / 'sessions/2026/10/08' / self.rollout.name
        self.write_rollout(path=duplicate)
        with self.assertRaisesRegex(probe.ProbeError, 'ambiguous'):
            subject.poll(self.state)

    def test_selected_api_and_database_identity_must_agree_and_db_is_readonly(self):
        observed, _ = self.complete()
        before = Path(self.server_config['database']).read_bytes()
        probe.read_checkpoint(self.server_config, self.state, observed)
        self.assertEqual(before, Path(self.server_config['database']).read_bytes())
        for changes in ({'epoch': 2}, {'result': 'unrelated'}, {'scope': 'leader:owner'}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(probe.ProbeError, 'api_database_identity_mismatch'):
                    probe.read_checkpoint(self.server_config, self.state, dict(observed, **changes))
        with self.api.store.transaction() as db:
            db.execute('UPDATE tasks SET input=? WHERE id=?', ('different', observed['id']))
        with self.assertRaisesRegex(probe.ProbeError, 'database_task_body_mismatch'):
            probe.read_checkpoint(self.server_config, self.state, observed)

    def test_checkpoint_harness_auth_node_and_native_identity_are_bound(self):
        observed, checkpoint = self.complete()
        for changes in ({'harness': 'pi'}, {'codex_node': 'different'},
                        {'codex_auth_home': '/unconfigured/home'}, {'thread_id': self.old_turn}, {'turn_id': self.old_turn}):
            with self.subTest(changes=changes):
                with self.assertRaises(probe.ProbeError):
                    probe.native_evidence(self.worker, self.state, observed, dict(checkpoint, **changes))

    def test_claims_other_tool_other_file_missing_output_or_wrong_turn_cannot_prove_shell(self):
        observed, checkpoint = self.complete()
        records = self.native_records()
        cases = [self.native_records(name='mesh_shell'), self.native_records(shell_path='/different/challenge.bin'),
                 self.native_records(output='shell host missing'),
                 self.native_records(output=self.state['challenge_sha256']),
                 self.native_records(output=json.dumps({'exit_code': 1,
                     'output': self.state['challenge_sha256'] + '  ' + self.state['challenge']})),
                 [record for record in records if record.get('payload', {}).get('type')
                  not in ('custom_tool_call', 'custom_tool_call_output')]]
        wrong_call = self.native_records()
        wrong_call[5]['payload']['call_id'] = 'unrelated-call'
        cases.append(wrong_call)
        echo_only = self.native_records()
        echo_only[4]['payload']['input'] = 'text(' + json.dumps('sha256sum -- ' + self.state['challenge']) + ');'
        cases.append(echo_only)
        old_only = self.native_records()
        old_only[1]['payload']['turn_id'] = self.old_turn
        old_only[2]['payload']['turn_id'] = self.old_turn
        old_only[6]['payload']['turn_id'] = self.old_turn
        old_only += self.native_records()[1:3] + self.native_records()[-1:]
        cases.append(old_only)
        for rows in cases:
            with self.subTest(rows=rows):
                self.write_rollout(rows)
                result = probe.native_evidence(self.worker, self.state, observed, checkpoint)
                self.assertFalse(result['challenge_seen_in_native_output'])

    def test_rollout_header_path_permissions_and_latest_turn_are_checked(self):
        observed, checkpoint = self.complete()
        rows = self.native_records()
        rows[0]['payload']['id'] = self.old_turn
        self.write_rollout(rows)
        with self.assertRaisesRegex(probe.ProbeError, 'thread_mismatch'):
            probe.native_evidence(self.worker, self.state, observed, checkpoint)
        self.write_rollout()
        self.rollout.chmod(0o644)
        with self.assertRaisesRegex(probe.ProbeError, '0600'):
            probe.native_evidence(self.worker, self.state, observed, checkpoint)
        self.rollout.chmod(0o600)
        rows = self.native_records() + [{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': self.old_turn}}]
        self.write_rollout(rows)
        with self.assertRaisesRegex(probe.ProbeError, 'not_latest'):
            probe.native_evidence(self.worker, self.state, observed, checkpoint)

    def test_default_cli_has_no_submit_and_sanitizes_private_content(self):
        self.complete()
        self.requests = []
        output = io.StringIO()
        argv = ['--operator-config', str(self.operator_path), '--worker-config', str(self.worker_path),
                '--server-config', str(self.server_path), '--state', str(self.state_path)]
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, probe.main(argv))
        value = json.loads(output.getvalue())
        self.assertFalse(value['explicit_submission'])
        self.assertEqual(['/v1/task/status'], [path for path, _ in self.requests])
        for private in (self.state['task']['input'], self.state['challenge_sha256'], 'PRIVATE REASONING', 'a' * 64, 'b' * 64):
            self.assertNotIn(private, output.getvalue())
        self.assertNotIn(self.worker['codex']['executable'], output.getvalue())

    def test_default_absent_task_error_is_not_proof_of_non_submission_or_automatic_retry(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = probe.main(['--operator-config', str(self.operator_path), '--worker-config', str(self.worker_path),
                               '--state', str(self.state_path), '--timeout', '0.1'])
        self.assertEqual(1, code)
        self.assertTrue(json.loads(output.getvalue())['retry_with_same_state'])
        self.assertEqual(['/v1/task/status'], [path for path, _ in self.requests])

    def test_existing_worker_integration_uses_isolated_session_not_global_leader(self):
        owner = self
        global_state = {'thread_id': self.old_turn, 'private': 'GLOBAL LEADER MUST REMAIN UNTOUCHED'}
        with self.api.store.transaction() as db:
            db.execute('INSERT INTO native_sessions VALUES(?,?,?,?,?,?)',
                       ('leader:owner', 'fixture', 'codex', json.dumps(global_state), None, 0))

        class FixtureNative:
            def __init__(self, config, **kwargs):
                self.auth_home = config['auth_home']

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def account(self):
                return {'authenticated': True}

            def rate_limits(self):
                return {'status': 'available'}

            def start(self, prompt, resume):
                owner.assertEqual({}, resume)
                owner.assertIn(owner.state['task']['input'], prompt)
                owner.assertNotIn(owner.state['challenge_sha256'], prompt)
                return {'thread_id': owner.thread_id, 'turn_id': owner.turn_id}

            def finish(self, **kwargs):
                return hashlib.sha256(Path(owner.state['challenge']).read_bytes()).hexdigest()

            def native_rollout(self):
                return owner.write_rollout()

            def goal(self):
                return None

        self.subject.submit(self.state)
        # Real Worker + real API, but explicitly offline fixture backend only.
        worker = Worker(self.worker, backend=FixtureNative)
        self.assertTrue(worker.run_once())
        report, code = self.subject.poll(self.state)
        self.assertEqual(0, code)
        self.assertTrue(report['execution_verified'])
        with self.api.store.transaction() as db:
            row = db.execute('SELECT state FROM native_sessions WHERE scope=? AND harness=?', ('leader:owner', 'codex')).fetchone()
            self.assertEqual(global_state, json.loads(row['state']))
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
