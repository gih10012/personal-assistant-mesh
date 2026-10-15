"""Synthetic native bytes exercise the real parser and one-shot DB transition."""
import base64
import collections
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.store import Store


MODULE = importlib.util.spec_from_file_location('review_native_effects',
    str(Path(__file__).resolve().parents[1] / 'scripts' / 'review_native_effects.py'))
review = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(review)


class NativeEffectReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'profile'
        self.home.mkdir(mode=0o700)
        self.auth = self.home / 'auth.json'
        claims = base64.urlsafe_b64encode(json.dumps({'email': 'owner@example.invalid'}).encode()).decode().rstrip('=')
        self.private(self.auth, {'tokens': {'id_token': 'synthetic.' + claims + '.signature',
            'access_token': 'SYNTHETIC_PRIVATE_ACCESS', 'refresh_token': 'SYNTHETIC_PRIVATE_REFRESH',
            'account_id': 'synthetic-authorized-account'}})
        self.thread = '01a11b4b-a008-7670-b172-ee8d02ab3fae'
        self.turn = '01a11b74-271f-7852-bb37-523928cc238d'
        folder = self.home
        for name in ('sessions', '2026', '10', '07'):
            folder = folder / name
            folder.mkdir(mode=0o700)
        self.rollout = folder / ('rollout-2026-10-07T15-30-16-' + self.thread + '.jsonl')
        self.database = self.root / 'ledger.sqlite'
        self.store = Store(self.database)
        self.task = 'a' * 64
        self.scope = 'leader:synthetic-owner'
        self.checkpoint = {'thread_id': self.thread, 'turn_id': self.turn, 'harness': 'codex',
            'codex_auth_home': str(self.home), 'codex_node': 'synthetic-node', 'side_effect_started': True,
            'runtime_failure': {'category': 'server_overloaded', 'outcome': 'unknown'},
            'private_original_checkpoint': {'preserve': 'PRIVATE_BEFORE_STATE'}}
        self.state = {'thread_id': self.thread, 'harness': 'codex', 'codex_auth_home': str(self.home),
                      'codex_node': 'synthetic-node', 'private_original_session': 'PRESERVE_NATIVE_STATE'}
        self.history = [{'type': 'session_meta', 'payload': {'id': self.thread,
            'timestamp': '2026-10-07T15:30:16Z', 'base_instructions': 'PRIVATE_ORIGINAL_HISTORY'}}]
        self.old = 'b' * 64
        self.old_bytes = gzip.compress(self.encode(self.history))
        with self.store.transaction() as db:
            db.execute('''INSERT INTO tasks(id,input,required,status,epoch,checkpoint,result,created,scope)
                VALUES(?,?,?,'needs_review',1,?,?,0,?)''', (self.task, 'PRIVATE_ORIGINAL_INPUT',
                '["leader"]', json.dumps(self.checkpoint), 'PRIVATE_ORIGINAL_FAILURE', self.scope))
            db.execute('INSERT INTO native_sessions VALUES(?,?,?,?,?,?)',
                (self.scope, 'synthetic-node', 'codex', json.dumps(self.state), self.old, 123))
            db.execute('INSERT INTO session_chunks VALUES(?,?,?)', (self.old, 0, self.old_bytes))
            db.execute('CREATE TABLE managed_allocations(operation_id TEXT, task_id TEXT)')
            db.execute('CREATE TABLE remote_delegations(child_id TEXT, parent_id TEXT)')
            db.execute('INSERT INTO tasks(id,input,required,status,created,scope) VALUES(?,?,?,?,?,?)',
                ('c' * 64, 'new independent authorized input', '["leader"]', 'pending', 1, self.scope))
        self.worker_config = self.root / 'worker.json'
        self.server_config = self.root / 'server.json'
        self.private(self.worker_config, {'node_id': 'synthetic-node', 'capabilities': ['agent'],
            'codex': {'auth_home': str(self.home)}, 'codex_accounts': [{'auth_home': str(self.home)}]})
        self.private(self.server_config, {'node_id': 'synthetic-node', 'database': str(self.database),
            'peers': [{'role': 'operator'}, {'role': 'worker', 'node': 'synthetic-node'}]})
        self.review_root = self.root / 'reviews'
        self.review_root.mkdir(mode=0o700)
        self.spec_path = self.root / 'owner-review.json'
        self.commands = self.synthetic_commands()
        self.records, self.command_pins, self.polls = self.synthetic_turn()
        self.original_rollout = self.encode(self.history + self.records)
        self.rollout.write_bytes(self.original_rollout)
        self.rollout.chmod(0o600)
        self.spec = self.make_spec()
        self.private(self.spec_path, self.spec)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def encode(records):
        return b''.join((json.dumps(r) + '\n').encode() for r in records)

    @staticmethod
    def private(path, data):
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def py(self, text):
        return "python3 - <<'PY'\n" + text + '\nPY'

    def synthetic_commands(self):
        query = self.py("import json\nfrom assistant_mesh.codex import Codex\na=Codex({})\ntry:\n "
                        "account=a.account()\n limits=a.rpc('account/rateLimits/read',{})\n "
                        "print(json.dumps({'account':account,'limits':limits}))\nfinally:\n a.close()")
        copy_command = self.py("import pathlib,os,json,subprocess\np=pathlib.Path(" +
            repr(str(self.home)) + ");p.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(str(p),0o700)\n"
            "r=subprocess.run(['ssh','-F','/synthetic/config','-o','BatchMode=yes','gi-local',"
            "'cat /synthetic/source/auth.json'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25)\n"
            "if r.returncode:raise RuntimeError('fixed failure')\n"
            "x=json.loads(r.stdout)\nif not x.get('tokens'):raise RuntimeError('fixed failure')\n"
            "f=p/'auth.json';fd=os.open(str(f),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\n"
            "with os.fdopen(fd,'wb') as h:h.write(r.stdout)\nprint('installed privately')")
        return ["rg public;cat public;ssh gi-local " + self.py("import json\nprint(json.dumps({}))"),
            query, 'rg readonly public-file', self.py("import json\nprint(json.dumps({}))"), copy_command,
            query.replace('import json\n', 'import json\n# authority-side original query\n')]

    def synthetic_turn(self):
        records = [{'timestamp': '2026-10-13T06:55:00Z', 'type': 'event_msg',
            'payload': {'type': 'task_started', 'turn_id': self.turn}}]
        pins, polls = [], []
        for i, (role, command) in enumerate(zip(review.ROLES, self.commands)):
            cid = 'call_synthetic_' + str(i)
            source = 'text(await tools.exec_command({cmd:' + json.dumps(command) + '}));'
            out = 'synthetic-output-' + str(i)
            if i == 5:
                out = json.dumps({'account': {'authenticated': True, 'type': 'chatgpt', 'plan': 'plus'}, 'limits': {}})
            pin = {'role': role, 'call_id': cid, 'input_sha256': review.sha(source.encode()),
                   'command_sha256': review.sha(command.encode()), 'stdout_sha256': review.sha(out.encode()),
                   'stdout_size': len(out.encode()), 'chunk_ids': []}
            records.append({'type': 'response_item', 'payload': {'type': 'custom_tool_call',
                            'name': 'exec', 'call_id': cid, 'input': source}})
            event = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': self.thread,
                'turn_id': self.turn, 'item': {'type': 'CommandExecution', 'command': ['/bin/bash', '-lc', command],
                'id': 'synthetic-' + str(i), 'status': 'completed', 'exit_code': 0,
                'process_id': None, 'stdout': out, 'stderr': ''}}}
            if i in (1, 5):
                handle = 100 + i
                pin['chunk_ids'] = ['first-' + str(i), 'last-' + str(i)]
                records.append(self.output(cid, {'chunk_id': pin['chunk_ids'][0], 'output': '', 'session_id': handle}))
                poll_id = 'poll-' + str(i)
                poll_source = 'text(await tools.write_stdin({session_id:' + str(handle) + '}));'
                polls.append({'call_id': poll_id, 'input_sha256': review.sha(poll_source.encode()), 'session_id': handle})
                records.append({'type': 'response_item', 'payload': {'type': 'custom_tool_call',
                    'name': 'exec', 'call_id': poll_id, 'input': poll_source}})
                records.append(event)
                records.append(self.output(poll_id, {'chunk_id': pin['chunk_ids'][1], 'output': out, 'exit_code': 0}))
            else:
                pin['chunk_ids'] = ['final-' + str(i)]
                records.append(event)
                records.append(self.output(cid, {'chunk_id': pin['chunk_ids'][0], 'output': out, 'exit_code': 0}))
            pins.append(pin)
        records.append({'timestamp': '2026-10-13T06:58:50Z', 'type': 'event_msg', 'payload': {
            'type': 'task_complete', 'turn_id': self.turn, 'last_agent_message': None,
            'error': {'codex_error_info': 'server_overloaded', 'message': 'PRIVATE_PROVIDER_ERROR'}}})
        return records, pins, polls

    @staticmethod
    def output(cid, data):
        return {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': cid,
            'output': [{'type': 'input_text', 'text': 'Script finished'},
                       {'type': 'input_text', 'text': json.dumps(data)}]}}

    def rows(self, table):
        with contextlib.closing(sqlite3.connect(str(self.database))) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM ' + table)]

    def make_spec(self):
        task = [r for r in self.rows('tasks') if r['id'] == self.task][0]
        session = self.rows('native_sessions')[0]
        now = time.time()
        return {'protocol': review.PROTOCOL, 'case': review.CASE, 'issued_at': now - 1, 'expires_at': now + 900,
            'worker_config': str(self.worker_config), 'server_config': str(self.server_config),
            'review_root': str(self.review_root), 'commands': self.command_pins, 'polls': self.polls,
            'record_counts': dict(collections.Counter(r['type'] + ':' + str(r['payload'].get('type') or '')
                                                    for r in self.records)),
            'worker': {'pid': 123, 'uid': os.getuid(), 'pgid': 123, 'start_ticks': 456,
                       'command_sha256': 'd' * 64},
            'authority_sources': {'/synthetic/authority/assistant_mesh/codex.py': {'sha256': 'e' * 64}},
            'local_observer': {'uid': os.getuid(), 'auth_path': str(self.auth),
                'auth_file': review.read_checked(self.auth, True)[1],
                'sources': {'/synthetic/local/assistant_mesh/codex.py': {'sha256': 'f' * 64}}},
            'expected': {'task_id': self.task, 'epoch': 1, 'scope': self.scope,
                'thread_id': self.thread, 'turn_id': self.turn, 'checkpoint_sha256': review.sha(task['checkpoint'].encode()),
                'task_sha256': review.digest(task), 'session_sha256': review.digest(session),
                'session_state_sha256': review.sha(session['state'].encode()), 'rollout_path': str(self.rollout),
                'rollout_file': review.read_checked(self.rollout, True)[1], 'selected_size': len(self.encode(self.records)),
                'selected_sha256': review.sha(self.encode(self.records)), 'auth_path': str(self.auth),
                'auth_file': review.read_checked(self.auth, True)[1],
                'auth_email_binding_sha256': review.sha(b'owner@example.invalid'),
                'auth_account_binding_sha256': review.sha(b'synthetic-authorized-account'),
                'dependency_tables': {'managed_allocations': ['operation_id', 'task_id'],
                    'remote_delegations': ['child_id', 'parent_id'],
                    'task_continuations': [r[1] for r in self.schema('task_continuations')],
                    'interactions': [r[1] for r in self.schema('interactions')]},
                'old_artifact': {'id': self.old, 'parts': 1, 'compressed_size': len(self.old_bytes),
                    'compressed_sha256': review.sha(self.old_bytes), 'raw_size': len(self.encode(self.history)),
                    'raw_sha256': review.sha(self.encode(self.history))}}}

    def schema(self, table):
        with contextlib.closing(sqlite3.connect(str(self.database))) as db:
            return list(db.execute('PRAGMA table_info(' + table + ')'))

    def fake_authority(self, spec, request):
        return {'nonce': request['nonce'], 'observed_at': time.time(), 'uid': os.getuid(),
            'auth': {'file': review.read_checked(self.auth, True)[1], 'email_binding': True, 'account_binding': True},
            'sources': spec['authority_sources'],
            'codex_contracts': {p: {'account_read_refresh_token': False, 'close_reaps_owned_runtime': True,
                 'constructor_launches_native_appserver': True} for p in spec['authority_sources']},
            'processes': {'servers': [], 'worker': spec['worker'], 'original_interval_survivors': 0}}

    def fake_local(self, spec, request):
        value = self.fake_authority(spec, request)
        value['processes']['worker'] = None
        value['sources'] = spec['local_observer']['sources']
        value['codex_contracts'] = {p: {'account_read_refresh_token': False, 'close_reaps_owned_runtime': True,
              'constructor_launches_native_appserver': True} for p in value['sources']}
        return value

    def observed(self):
        return patch.multiple(review, authority_observation=self.fake_authority, local_observation=self.fake_local)

    def refresh_rollout_pins(self):
        raw = self.encode(self.history + self.records)
        self.rollout.write_bytes(raw)
        self.spec['record_counts'] = dict(collections.Counter(r['type'] + ':' + str(r['payload'].get('type') or '')
                                                            for r in self.records))
        self.spec['expected'].update(rollout_file=review.read_checked(self.rollout, True)[1],
            selected_sha256=review.sha(self.encode(self.records)), selected_size=len(self.encode(self.records)))
        self.private(self.spec_path, self.spec)

    def test_default_audit_real_six_command_parser_has_no_mutation_or_secret_output(self):
        before = {t: self.rows(t) for t in ('tasks', 'native_sessions', 'session_chunks')}
        with self.observed():
            context = review.inspect(str(self.spec_path))
            self.assertEqual(6, len(context['native']['commands']))
            self.assertFalse(context['native']['effects_zero'])
            output = io.StringIO()
            with patch('sys.stdout', output):
                self.assertEqual(0, review.main(['--spec-file', str(self.spec_path)]))
        for table in before:
            self.assertEqual(before[table], self.rows(table))
        self.assertEqual(self.original_rollout, self.rollout.read_bytes())
        self.assertEqual([], list(self.review_root.iterdir()))
        for secret in ('SYNTHETIC_PRIVATE_ACCESS', 'PRIVATE_ORIGINAL', 'owner@example.invalid', 'python3 -'):
            self.assertNotIn(secret, output.getvalue())

    def test_apply_fails_original_task_without_requeue_and_retains_all_before_bytes(self):
        before_task = self.rows('tasks')[0]
        before_session = self.rows('native_sessions')[0]
        old_chunks = self.rows('session_chunks')
        approval = review.read_checked(self.spec_path, True)[1]['sha256']
        with self.observed():
            result = review.apply_review(str(self.spec_path), approval)
        self.assertTrue(result['applied'])
        after = [r for r in self.rows('tasks') if r['id'] == self.task][0]
        self.assertEqual('failed', after['status'])
        self.assertEqual(2, after['epoch'])
        self.assertEqual(before_task['input'], after['input'])
        self.assertEqual(before_task['result'], after['result'])
        cp = json.loads(after['checkpoint'])
        self.assertFalse(cp['side_effect_started'])
        self.assertFalse(cp['owner_effect_review']['effects_zero'])
        self.assertEqual(self.checkpoint['runtime_failure'], cp['runtime_failure'])
        session = self.rows('native_sessions')[0]
        self.assertEqual(before_session['state'], session['state'])
        self.assertNotEqual(before_session['artifact'], session['artifact'])
        journal = self.rows('native_effect_reviews')[0]
        self.assertEqual(before_task, json.loads(journal['before_task']))
        self.assertEqual(before_session, json.loads(journal['before_session']))
        self.assertEqual(old_chunks, [r for r in self.rows('session_chunks') if r['id'] == self.old])
        packed = b''.join(r['body'] for r in self.rows('session_chunks') if r['id'] == session['artifact'])
        self.assertEqual(self.original_rollout, gzip.decompress(packed))
        directory = self.review_root / result['id']
        self.assertEqual(self.old_bytes, (directory / '02-original-artifact.gz').read_bytes())
        with gzip.open(str(directory / '04-consistent-ledger.sql.gz'), 'rt') as source:
            dump = source.read()
        restored = sqlite3.connect(':memory:')
        restored.executescript(dump)
        self.assertEqual('needs_review', restored.execute('SELECT status FROM tasks WHERE id=?', (self.task,)).fetchone()[0])
        restored.close()
        self.assertEqual(self.original_rollout, self.rollout.read_bytes())
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'intent_requires_readback'):
            review.apply_review(str(self.spec_path), approval)
        self.assertTrue(review.review_status(str(self.spec_path))['applied'])

    def test_digest_does_not_admit_unknown_command_even_if_spec_repinned(self):
        cid = self.command_pins[4]['call_id']
        malicious = self.commands[4].replace('os.O_EXCL', 'os.O_TRUNC')
        source = 'text(await tools.exec_command({cmd:' + json.dumps(malicious) + '}));'
        for record in self.records:
            p = record['payload']
            if p.get('call_id') == cid and p.get('type') == 'custom_tool_call':
                p['input'] = source
            if p.get('type') == 'item_completed' and p.get('item', {}).get('id') == 'synthetic-4':
                p['item']['command'][2] = malicious
        self.spec['commands'][4].update(input_sha256=review.sha(source.encode()), command_sha256=review.sha(malicious.encode()))
        self.refresh_rollout_pins()
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'not_exclusive'):
            review.inspect(str(self.spec_path))

    def test_unknown_tool_effect_record_rejected_even_if_spec_pins_changed(self):
        self.records.insert(-1, {'type': 'response_item', 'payload': {'type': 'function_call',
                             'name': 'send_message', 'arguments': '{}'}})
        self.refresh_rollout_pins()
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'unsupported_reviewed_record'):
            review.inspect(str(self.spec_path))

    def test_original_output_must_equal_complete_native_event_bytes(self):
        for r in self.records:
            if r['payload'].get('item', {}).get('id') == 'synthetic-3':
                r['payload']['item']['stdout'] += 'hidden-unsettled-output'
        self.refresh_rollout_pins()
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'whole_original_output'):
            review.inspect(str(self.spec_path))

    def test_quota_or_other_error_and_new_turn_are_not_this_case(self):
        self.records[-1]['payload']['error']['codex_error_info'] = 'usage_limit_exceeded'
        self.refresh_rollout_pins()
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'not_reviewed_failed_terminal'):
            review.inspect(str(self.spec_path))
        self.records[-1]['payload']['error']['codex_error_info'] = 'server_overloaded'
        self.records.append({'type': 'event_msg', 'payload': {'type': 'task_started',
             'turn_id': '01a11b74-271f-7852-bb37-523928cc2333'}})
        self.refresh_rollout_pins()
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'not_latest'):
            review.inspect(str(self.spec_path))

    def test_auth_generation_and_authorized_binding_are_not_replaced_by_hash_label(self):
        self.auth.write_text(self.auth.read_text() + ' ')
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'auth_identity_or_generation'):
            review.inspect(str(self.spec_path))

    def test_worker_pid_reuse_or_unclosed_native_process_rejects(self):
        def reused(spec, request):
            value = self.fake_authority(spec, request)
            value['processes']['worker'] = dict(spec['worker'], start_ticks=999)
            return value
        with patch.object(review, 'authority_observation', reused), patch.object(review, 'local_observation', self.fake_local):
            with self.assertRaisesRegex(review.ReviewDenied, 'worker_generation_changed'):
                review.inspect(str(self.spec_path))
        def alive(spec, request):
            value = self.fake_local(spec, request)
            value['processes']['original_interval_survivors'] = 1
            return value
        with patch.object(review, 'authority_observation', self.fake_authority), patch.object(review, 'local_observation', alive):
            with self.assertRaisesRegex(review.ReviewDenied, 'query_runtime_still_present'):
                review.inspect(str(self.spec_path))

    def test_children_execution_allocations_and_pending_unknown_siblings_reject(self):
        with self.store.transaction() as db:
            db.execute('INSERT INTO managed_allocations VALUES(?,?)', ('pending-old-operation', self.task))
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'execution_or_delegation_dependency'):
            review.inspect(str(self.spec_path))
        with self.store.transaction() as db:
            db.execute('DELETE FROM managed_allocations')
            db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', ('{"side_effect_started":true}', 'c' * 64))
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'other_scope_effect_unsettled'):
            review.inspect(str(self.spec_path))

    def test_apply_cas_race_is_unknown_keeps_before_snapshot_and_never_requeues(self):
        def racing():
            with self.store.transaction() as db:
                db.execute('UPDATE tasks SET epoch=epoch+1 WHERE id=?', (self.task,))
        approval = review.read_checked(self.spec_path, True)[1]['sha256']
        with self.observed():
            result = review.apply_review(str(self.spec_path), approval, before_transaction=racing)
        self.assertTrue(result['outcome_unknown'])
        self.assertIsNone(result['applied'])
        self.assertEqual('needs_review', self.rows('tasks')[0]['status'])
        self.assertTrue(json.loads(self.rows('tasks')[0]['checkpoint'])['side_effect_started'])
        self.assertEqual(self.old, self.rows('native_sessions')[0]['artifact'])
        directory = self.review_root / result['id']
        self.assertTrue((directory / '01-before.json').exists())
        self.assertTrue((directory / '04-consistent-ledger.sql.gz').exists())
        self.assertTrue(review.review_status(str(self.spec_path))['outcome_unknown'])

    def test_lost_commit_ack_readback_original_review_without_second_apply(self):
        original = review.write_json
        def lost(path, body):
            if path.name == '07-receipt.json':
                raise OSError('simulated original ACK loss')
            return original(path, body)
        approval = review.read_checked(self.spec_path, True)[1]['sha256']
        with self.observed(), patch.object(review, 'write_json', lost):
            result = review.apply_review(str(self.spec_path), approval)
        self.assertTrue(result['outcome_unknown'])
        self.assertEqual('failed', self.rows('tasks')[0]['status'])
        actual = review.review_status(str(self.spec_path))
        self.assertTrue(actual['applied'])
        self.assertFalse(actual['retry_authorized'])
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'intent_requires_readback'):
            review.apply_review(str(self.spec_path), approval)

    def test_expired_or_edited_spec_and_missing_approval_reject(self):
        self.spec['expires_at'] = time.time() - 1
        self.spec['issued_at'] = self.spec['expires_at'] - 100
        self.private(self.spec_path, self.spec)
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'expired_or_future'):
            review.inspect(str(self.spec_path))
        self.spec['issued_at'] = time.time() - 1
        self.spec['expires_at'] = time.time() + 100
        self.private(self.spec_path, self.spec)
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'digest_approval'):
            review.apply_review(str(self.spec_path), 'f' * 64)

    def test_real_readonly_observer_returns_only_metadata_and_binds_authorized_account(self):
        request = {'nonce': 'test-challenge', 'interval': [1, 2], 'auth_path': str(self.auth),
            'email_hash': self.spec['expected']['auth_email_binding_sha256'],
            'account_hash': self.spec['expected']['auth_account_binding_sha256'], 'sources': [], 'worker_pid': os.getpid()}
        result = subprocess.run(['/usr/bin/python3', '-c', review.OBSERVER_CODE],
            input=review.encoded(request), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(0, result.returncode, result.stderr.decode())
        observed = json.loads(result.stdout)
        self.assertTrue(observed['auth']['email_binding'])
        self.assertTrue(observed['auth']['account_binding'])
        self.assertEqual(os.getpid(), observed['processes']['worker']['pid'])
        for secret in ('SYNTHETIC_PRIVATE_ACCESS', 'owner@example.invalid', 'synthetic-authorized-account'):
            self.assertNotIn(secret, result.stdout.decode())

    def test_shell_quoted_heredoc_is_parsed_not_unescaped_or_executed(self):
        quoted = self.commands[4].replace("<<'PY'", "<<'\"'\"'PY'\"'\"'") + "'"
        review.inspect_copy_command(quoted, str(self.auth))
        for bad in (quoted.replace('PY', 'PY;touch', 1), quoted + ';echo something'):
            with self.assertRaises(review.ReviewDenied):
                review.inspect_copy_command(bad, str(self.auth))

    def test_raw_authenticated_false_even_repinned_cannot_settle(self):
        wrong = json.dumps({'account': {'authenticated': False, 'type': 'chatgpt', 'plan': 'plus'}, 'limits': {}})
        last = self.spec['commands'][-1]
        for row in self.records:
            payload = row['payload']
            if payload.get('item', {}).get('id') == 'synthetic-5':
                payload['item']['stdout'] = wrong
            if payload.get('call_id') == 'poll-5' and payload.get('type') == 'custom_tool_call_output':
                receipt = json.loads(payload['output'][1]['text'])
                receipt['output'] = wrong
                payload['output'][1]['text'] = json.dumps(receipt)
        last.update(stdout_sha256=review.sha(wrong.encode()), stdout_size=len(wrong.encode()))
        self.refresh_rollout_pins()
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'post_copy_account_unconfirmed'):
            review.inspect(str(self.spec_path))

    def test_model_reviewed_boolean_and_unrelated_history_divergence_rejected(self):
        self.spec['reviewed'] = True
        self.private(self.spec_path, self.spec)
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'case_spec_shape'):
            review.inspect(str(self.spec_path))
        del self.spec['reviewed']
        self.history[0]['payload']['base_instructions'] += ' divergent'
        self.refresh_rollout_pins()
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'old_history_prefix'):
            review.inspect(str(self.spec_path))

    def test_failed_status_alone_still_blocks_claim_and_review_does_not_replay_original(self):
        self.store.heartbeat('synthetic-node', ['agent', 'leader'])
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET status=? WHERE id=?', ('failed', self.task))
        self.assertIsNone(self.store.claim('synthetic-node'))
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET status=? WHERE id=?', ('needs_review', self.task))
        approval = review.read_checked(self.spec_path, True)[1]['sha256']
        with self.observed():
            result = review.apply_review(str(self.spec_path), approval)
        self.assertTrue(result['applied'])
        claim = self.store.claim('synthetic-node')
        self.assertEqual('c' * 64, claim['id'])
        self.assertEqual(self.thread, claim['sessions']['codex']['state']['thread_id'])
        self.assertEqual(self.state, claim['sessions']['codex']['state'])
        original = [row for row in self.rows('tasks') if row['id'] == self.task][0]
        self.assertEqual('failed', original['status'])
        self.assertEqual('PRIVATE_ORIGINAL_INPUT', original['input'])
        with self.assertRaises(sqlite3.IntegrityError), self.store.transaction() as db:
            db.execute('DELETE FROM native_effect_reviews')
