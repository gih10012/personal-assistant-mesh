"""Four focused boundaries; real local supervisors, zero Codex/model calls."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from assistant_mesh.agent_manager import AgentError, AgentManager, _command, _write, load_profile


class AgentManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-agents.')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'owner.json'
        _write(self.config, {'format': 'mesh-agent-manager/v1',
            'state_dir': str(self.root / 'state'), 'runtime_dir': str(self.root / 'run'),
            'runtime': {'executable': str(Path(sys.executable).resolve()), 'auth_home': str(self.root / 'existing-auth')},
            'external_agents': {'leader': {'model': 'synthetic-native', 'reasoning_effort': 'low'}}})
        self.validations = []

        def validate(identity, model, effort):
            self.validations.append((identity, model, effort))
            if effort not in ('low', 'max'):
                raise AgentError('unsupported_synthetic_effort')
            return {'validation': 'synthetic_no_native_catalog', 'model': model, 'reasoning_effort': effort}

        self.manager = AgentManager(str(self.config), validate)

    def test_external_profile_is_next_turn_only_persistent_idempotent_and_never_pid_killed(self):
        first = self.manager.configure('configure-one', 'leader', effort='max')
        self.assertEqual('next_explicit_turn', first['effective'])
        self.assertFalse(first['effective_verified'])
        self.assertEqual({'revision': 1, 'model': 'synthetic-native', 'reasoning_effort': 'max'},
                         load_profile(self.manager.profiles_path, 'leader'))
        self.manager.configure('configure-two', 'leader', model='synthetic-other')
        count = len(self.validations)
        same = self.manager.configure('configure-one', 'leader', effort='max')
        self.assertTrue(same['replayed'])
        self.assertEqual(count, len(self.validations))
        self.assertEqual(1, same['profile_revision'])
        with self.assertRaisesRegex(AgentError, 'request_id_conflict'):
            self.manager.configure('configure-one', 'leader', effort='low')
        with self.assertRaisesRegex(AgentError, 'unsupported_synthetic_effort'):
            self.manager.configure('configure-rejected', 'leader', effort='invented')
        self.assertEqual(2, load_profile(self.manager.profiles_path, 'leader')['revision'])
        self.assertEqual(0o600, stat.S_IMODE(self.manager.profiles_path.stat().st_mode))
        with patch('os.kill'), patch('os.killpg') as killpg:
            result = self.manager.stop('leader')
            self.assertFalse(result['stopped'])
            killpg.assert_not_called()

    def test_launch_intent_precedes_one_spawn_and_same_id_never_relaunches_unknown(self):
        class Started:
            pid = 987654

        def inspect_launch(*args, **kwargs):
            value = json.loads(self.manager.profiles_path.read_text())
            request = value['requests']['spawn-one']
            self.assertEqual('launch_intent', request['status'])
            self.assertTrue(request['outcome_unknown'])
            self.assertEqual(b'No model request in this fixture.',
                             (Path(request['request_root']) / 'prompt.txt').read_bytes())
            command = _command(request)
            self.assertIn('model_reasoning_effort="low"', command)
            self.assertIn('--dangerously-bypass-approvals-and-sandbox', command)
            self.assertNotIn('features.shell_tool=false', command)
            return Started()

        with patch('assistant_mesh.agent_manager.subprocess.Popen', side_effect=inspect_launch) as spawned:
            first = self.manager.spawn('spawn-one', 'worker-one', str(self.root),
                'No model request in this fixture.', 'synthetic-native', 'low')
            repeated = self.manager.spawn('spawn-one', 'worker-one', str(self.root),
                'No model request in this fixture.', 'synthetic-native', 'low')
            self.assertTrue(repeated['replayed'])
            self.assertEqual(first['request_id'], repeated['request_id'])
            spawned.assert_called_once()
        self.assertFalse(self.manager.status('worker-one')['supervisor_reachable'])
        with self.assertRaisesRegex(AgentError, 'prior_turn_not_safely_completed'):
            self.manager.resume('resume-unknown-forbidden', 'worker-one', 'Do not replay.')

    def _synthetic_supervisor(self, request_id, body):
        with patch('assistant_mesh.agent_manager.subprocess.Popen') as spawn:
            spawn.return_value.pid = 987654
            self.manager.spawn(request_id, 'worker-one', str(self.root),
                               'Only synthetic local Python fixture.', 'synthetic-native', 'low')
        code = r'''
import os, sys
from assistant_mesh import agent_manager as am
manager = am.AgentManager(sys.argv[1])
manager._update(sys.argv[2], supervisor_pid=os.getpid())
am._child_env = lambda runtime: os.environ.copy()
am._command = lambda record: [sys.executable, '-u', '-c', sys.argv[3]]
sys.exit(am.supervise(manager, sys.argv[2]))
'''
        process = subprocess.Popen([sys.executable, '-c', code, str(self.config), request_id, body],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            status = self.manager.status('worker-one')
            if status['status'] in ('running', 'completed', 'unknown'):
                return process, status
            time.sleep(.02)
        self.fail('synthetic supervisor did not start')

    def test_real_owned_stop_reaps_native_and_same_group_late_child_keeps_unknown_history(self):
        body = r'''
import json, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(json.dumps({'type':'thread.started','thread_id':'synthetic-original-thread'}), flush=True)
print(json.dumps({'type':'fixture-child','pid':child.pid}), flush=True)
while True: time.sleep(1)
'''
        process, status = self._synthetic_supervisor('stop-one', body)
        until = time.monotonic() + 3
        event_path = self.manager.root / __import__('hashlib').sha256(b'stop-one').hexdigest() / 'events.jsonl'
        child_pid = None
        while time.monotonic() < until:
            rows = [json.loads(line) for line in event_path.read_text().splitlines()]
            children = [row['pid'] for row in rows if row.get('type') == 'fixture-child']
            if children:
                child_pid = children[0]
                break
            time.sleep(.02)
        self.assertIsNotNone(child_pid)
        self.assertTrue(self.manager.stop('worker-one')['stop_accepted'])
        output, error = process.communicate(timeout=8)
        self.assertEqual(1, process.returncode, error)
        final = self.manager.status('worker-one')
        self.assertEqual('stopped', final['status'])
        self.assertTrue(final['outcome_unknown'])
        self.assertTrue(final['owned_native_reaped'])
        self.assertTrue(final['owned_group_children_reaped'])
        self.assertFalse(final['detached_or_remote_effects_settled'])
        self.assertEqual('synthetic-original-thread', final['session_id'])
        self.assertTrue(event_path.is_file())
        for pid in (status['native_pid'], child_pid):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        with self.assertRaisesRegex(AgentError, 'prior_turn_not_safely_completed'):
            self.manager.resume('resume-interrupted-forbidden', 'worker-one', 'Do not replay.')

    def test_real_completed_turn_and_new_profile_preserve_original_session_and_receipt(self):
        body = r'''
import json
print(json.dumps({'type':'thread.started','thread_id':'synthetic-original-thread'}))
print(json.dumps({'type':'turn.completed','usage':{}}))
'''
        process, unused = self._synthetic_supervisor('complete-one', body)
        output, error = process.communicate(timeout=5)
        self.assertEqual(0, process.returncode, error)
        final = self.manager.status('worker-one')
        self.assertEqual('completed', final['status'])
        self.assertFalse(final['outcome_unknown'])
        self.assertFalse(final['effective_verified'])
        self.manager.configure('raise-next', 'worker-one', effort='max')
        with patch('assistant_mesh.agent_manager.subprocess.Popen') as spawn:
            spawn.return_value.pid = 987654
            resumed = self.manager.resume('resume-two', 'worker-one', 'New explicit input, not a replay.')
        self.assertEqual('synthetic-original-thread', resumed['session_id'])
        self.assertEqual('max', resumed['selection']['reasoning_effort'])
        self.assertEqual(2, resumed['profile_revision'])
        original = self.manager.status(request_id='complete-one')
        self.assertEqual('complete-one', original['request_id'])
        self.assertEqual('completed', original['status'])
        self.assertEqual('low', original['selection']['reasoning_effort'])

    def test_two_concurrent_resumes_cannot_both_claim_the_same_completed_turn(self):
        with patch('assistant_mesh.agent_manager.subprocess.Popen') as spawn:
            spawn.return_value.pid = 987654
            self.manager.spawn('original-complete', 'worker-one', str(self.root),
                               'No model in concurrent-resume fixture.', 'synthetic-native', 'low')
        self.manager._update('original-complete', status='completed',
                             session_id='synthetic-original-thread', outcome_unknown=False)
        barrier = threading.Barrier(2)
        validated = self.manager.validator

        def concurrent_validate(identity, model, effort):
            receipt = validated(identity, model, effort)
            barrier.wait(timeout=3)
            return receipt

        self.manager.validator = concurrent_validate
        results, errors = [], []

        def resume(request):
            try:
                results.append(self.manager.resume(request, 'worker-one', 'New explicit input: ' + request))
            except Exception as exc:
                errors.append(exc)

        with patch('assistant_mesh.agent_manager.subprocess.Popen') as spawn:
            spawn.return_value.pid = 987654
            threads = [threading.Thread(target=resume, args=(name,)) for name in ('resume-A', 'resume-B')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            spawn.assert_called_once()
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], AgentError)
        self.assertEqual('agent_prior_turn_changed_before_launch', str(errors[0]))
        with self.manager._locked() as value:
            self.assertEqual(results[0]['request_id'], value['agents']['worker-one']['current_request_id'])
            self.assertEqual(2, len(value['requests']))
            self.assertEqual('completed', value['requests']['original-complete']['status'])


if __name__ == '__main__':
    unittest.main()
