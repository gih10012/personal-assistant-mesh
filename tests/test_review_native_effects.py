"""Synthetic native bytes exercise the real parser and one-shot DB transition."""
import base64
import ast
import collections
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
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
        # Synthetic tests replace the CODE-owned fixture pins, never a CLI or
        # owner-spec override. The installed script admits only its real case.
        self.closed_patch = patch.object(review, 'CLOSED_CASE', self.closed_manifest())
        self.closed_patch.start()

    def tearDown(self):
        self.closed_patch.stop()
        self.temp.cleanup()

    def closed_manifest(self):
        return {'bundle_sha256': review.digest(review.closed_case_bundle(self.spec)),
                'bindings_sha256': review.digest(review.closed_case_bindings(self.spec)),
                'commands': {item['role']: item['command_sha256'] for item in self.spec['commands']},
                'inputs': frozenset(item['input_sha256'] for item in self.spec['commands']),
                'poll_inputs': frozenset(item['input_sha256'] for item in self.spec['polls'])}

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
        # Preserve the original semantic tests below while explicitly binding
        # their synthetic bytes as TEST code, not accepting private repinning.
        review.CLOSED_CASE.update(self.closed_manifest())

    def prepared_after_old_size_failure(self):
        approval = review.read_checked(self.spec_path, True)[1]['sha256']
        # Reproduce the actual failure AFTER the complete gzip has been closed
        # and fsynced, but BEFORE 05/06/any ledger mutation. No huge fixture.
        with self.observed(), patch.object(review, 'digest_checked_backup',
                side_effect=review.ReviewDenied('file_size_bound')):
            with self.assertRaisesRegex(review.ReviewDenied, 'file_size_bound'):
                review.apply_review(str(self.spec_path), approval)
        context_case = review.digest([review.NAMESPACE, self.task, 1,
                                     self.spec['expected']['rollout_file']['sha256']])
        directory = self.review_root / context_case
        self.assertEqual({'00-intent.json', '01-before.json', '02-original-artifact.gz',
            '03-full-rollout.gz', '04-consistent-ledger.sql.gz'}, {p.name for p in directory.iterdir()})
        return approval, directory

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
        self.assertEqual(ast.dump(review.python_body(self.commands[4])), ast.dump(review.python_body(quoted)))
        with self.assertRaisesRegex(review.ReviewDenied, 'closed_case_command_mismatch'):
            review.inspect_copy_command(quoted, str(self.auth))
        for bad in (quoted.replace('PY', 'PY;touch', 1), quoted + ';echo something'):
            with self.assertRaises(review.ReviewDenied):
                review.inspect_copy_command(bad, str(self.auth))

    def test_complete_code_pins_reject_all_five_previously_admitted_effect_paths(self):
        cases = (
            lambda: review.inspect_copy_command('printf unsettled > /synthetic/unreviewed; ' + self.commands[4], str(self.auth)),
            lambda: review.inspect_query_command('printf unsettled > /synthetic/unreviewed; ' + self.commands[1]),
            lambda: review.inspect_copy_command(self.commands[4].replace('os.chmod(str(p),0o700)',
                "os.chmod('/synthetic/unreviewed',0o700)"), str(self.auth)),
            lambda: review.inspect_read_command("rg readonly public-file && python3 -c 'open(\"/synthetic/unreviewed\",\"w\").write(\"unsettled\")'", 'code_read'),
            lambda: review.command_from_call('text(await tools.exec_command({cmd:"rg read only"})); await fetch("https://synthetic.invalid/effect", {method:"POST"});'),
        )
        for index, check in enumerate(cases):
            with self.subTest(case=index), self.assertRaisesRegex(review.ReviewDenied, 'closed_case_'):
                check()  # Parse only; none of these strings is executed.

    def test_private_spec_repins_cannot_add_another_case_command_or_poll(self):
        original = json.loads(json.dumps(self.spec))
        for key in ('task_id', 'thread_id', 'turn_id', 'selected_sha256'):
            self.spec = json.loads(json.dumps(original))
            value = self.spec['expected'][key]
            self.spec['expected'][key] = ('e' * 64 if key.endswith('sha256') or key == 'task_id'
                                          else '01a11b74-271f-7852-bb37-523928cc2333')
            self.private(self.spec_path, self.spec)
            with self.subTest(identity=key), self.assertRaisesRegex(review.ReviewDenied, 'closed_case_bundle_mismatch'):
                review.load_spec(str(self.spec_path))
        for kind in ('commands', 'polls'):
            self.spec = json.loads(json.dumps(original))
            self.spec[kind][0]['input_sha256'] = 'f' * 64
            self.private(self.spec_path, self.spec)
            with self.subTest(kind=kind), self.assertRaisesRegex(review.ReviewDenied, 'closed_case_bundle_mismatch'):
                review.load_spec(str(self.spec_path))
        for key in ('authority_sources', 'local_observer', 'worker', 'review_root'):
            self.spec = json.loads(json.dumps(original))
            if key == 'authority_sources':
                next(iter(self.spec[key].values()))['sha256'] = 'f' * 64
            elif key == 'local_observer':
                self.spec[key]['uid'] += 1
            elif key == 'worker':
                self.spec[key]['start_ticks'] += 1
            else:
                self.spec[key] += '-another-root'
            self.private(self.spec_path, self.spec)
            with self.subTest(binding=key), self.assertRaisesRegex(review.ReviewDenied, 'closed_case_bindings_mismatch'):
                review.load_spec(str(self.spec_path))

    @unittest.skipUnless(shutil.which('node'), 'V8 JSON precision regression requires Node, not a runtime requirement')
    def test_js_roundtrip_large_ns_never_substitutes_for_exact_python_generation(self):
        exact = 1792039464123456789  # Synthetic metadata, greater than 2**53.
        self.spec['expected']['auth_file']['mtime_ns'] = exact
        review.CLOSED_CASE.update(self.closed_manifest())  # Synthetic code pins only.
        self.private(self.spec_path, self.spec)
        self.assertEqual(exact, review.load_spec(str(self.spec_path))[0]['expected']['auth_file']['mtime_ns'])
        # Exercise real V8 on the COMPLETE spec; int(float(...)) produces a
        # different decimal from JSON.stringify's shortest roundtrip string.
        source = ("process.stdin.setEncoding('utf8');let raw='';"
                  "for await(const part of process.stdin)raw+=part;"
                  "process.stdout.write(JSON.stringify(JSON.parse(raw)));")
        result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', source],
            input=json.dumps(self.spec).encode(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(0, result.returncode)
        lossy = json.loads(result.stdout)
        rounded = lossy['expected']['auth_file']['mtime_ns']
        self.assertEqual(1792039464123456800, rounded)
        self.assertNotEqual(exact, rounded)
        self.assertNotEqual(int(float(exact)), rounded)
        self.assertEqual(float(exact), float(rounded))
        one_ns_changed = json.loads(json.dumps(self.spec))
        one_ns_changed['expected']['auth_file']['mtime_ns'] += 1
        self.assertEqual(float(exact), float(exact + 1))
        for label, changed in (('v8_roundtrip', lossy), ('real_one_ns_generation_change', one_ns_changed)):
            self.private(self.spec_path, changed)
            with self.subTest(kind=label), self.assertRaisesRegex(review.ReviewDenied, 'closed_case_bindings_mismatch'):
                review.load_spec(str(self.spec_path))

    def test_readback_requires_exact_approved_spec_and_actual_settlement_artifact(self):
        approval = review.read_checked(self.spec_path, True)[1]['sha256']
        with self.observed():
            result = review.apply_review(str(self.spec_path), approval)
        self.assertTrue(result['applied'])
        self.spec['expires_at'] += 1  # Even a short new window is not the same approval.
        self.private(self.spec_path, self.spec)
        with self.assertRaisesRegex(review.ReviewDenied, 'review_record_spec_or_before_conflict'):
            review.review_status(str(self.spec_path))
        self.spec['expires_at'] -= 1
        self.private(self.spec_path, self.spec)
        # Restore exact original JSON, including bytes, not a new grant.
        self.assertEqual(approval, review.read_checked(self.spec_path, True)[1]['sha256'])
        self.assertTrue(review.review_status(str(self.spec_path))['applied'])
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET status=? WHERE id=?', ('pending', self.task))
        with self.assertRaisesRegex(review.ReviewDenied, 'review_original_task_not_settled'):
            review.review_status(str(self.spec_path))
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET status=? WHERE id=?', ('failed', self.task))
            db.execute('DELETE FROM session_chunks WHERE id=?', (result['complete_artifact'],))
        with self.assertRaisesRegex(review.ReviewDenied, 'review_complete_artifact_changed'):
            review.review_status(str(self.spec_path))

    def test_readback_checks_preserved_checkpoint_and_old_history_not_just_receipt(self):
        approval = review.read_checked(self.spec_path, True)[1]['sha256']
        with self.observed():
            review.apply_review(str(self.spec_path), approval)
        original = next(row for row in self.rows('tasks') if row['id'] == self.task)['checkpoint']
        changed = dict(json.loads(original), unrelated_new_field='not_original_checkpoint')
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (json.dumps(changed), self.task))
        with self.assertRaisesRegex(review.ReviewDenied, 'review_original_guard_not_settled'):
            review.review_status(str(self.spec_path))
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (original, self.task))
            db.execute('UPDATE session_chunks SET body=? WHERE id=?', (b'changed-old-history', self.old))
        with self.assertRaisesRegex(review.ReviewDenied, 'review_old_artifact_changed'):
            review.review_status(str(self.spec_path))

    def test_expired_same_spec_readback_allows_later_native_session_progress(self):
        approval = review.read_checked(self.spec_path, True)[1]['sha256']
        with self.observed():
            result = review.apply_review(str(self.spec_path), approval)
        # A new authorized task may advance this continuous session; status is
        # about the retained original review, not restoring old live state.
        with self.store.transaction() as db:
            db.execute('UPDATE native_sessions SET state=?,artifact=?,updated=?',
                (json.dumps(dict(self.state, subsequent_task='new-authorized-task')), 'd' * 64, 456))
        with patch.object(review.time, 'time', return_value=self.spec['expires_at'] + 1):
            readback = review.review_status(str(self.spec_path))
        self.assertTrue(readback['applied'])
        self.assertFalse(readback['retry_authorized'])
        self.assertEqual(result['complete_artifact'], readback['complete_artifact'])
        self.assertEqual('d' * 64, self.rows('native_sessions')[0]['artifact'])

    def test_prepared_readonly_audit_and_explicit_completion_keep_all_original_files(self):
        approval, directory = self.prepared_after_old_size_failure()
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        tasks, sessions, chunks = self.rows('tasks'), self.rows('native_sessions'), self.rows('session_chunks')
        backup = directory / '04-consistent-ledger.sql.gz'
        with self.assertRaisesRegex(review.ReviewDenied, 'file_size_bound'):
            review.read_checked(backup, private=True, limit=backup.stat().st_size - 1)
        original_read = review.read_checked
        def no_whole_backup_read(path, *args, **kwargs):
            self.assertNotEqual('04-consistent-ledger.sql.gz', Path(path).name)
            return original_read(path, *args, **kwargs)
        with self.observed(), patch.object(review, 'read_checked', no_whole_backup_read):
            context, staged = review.inspect_prepared(str(self.spec_path))
        self.assertEqual(tasks, self.rows('tasks'))
        self.assertEqual(sessions, self.rows('native_sessions'))
        self.assertEqual(chunks, self.rows('session_chunks'))
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})
        self.assertEqual([], list(self.review_root.glob('.native-effect-backup-check.*')))
        digest = staged['ready']['backup_file']['sha256']
        with self.observed(), patch.object(review, 'read_checked', no_whole_backup_read):
            with self.assertRaisesRegex(review.ReviewDenied, 'complete_backup_digest_approval'):
                review.resume_prepared_review(str(self.spec_path), approval)
            result = review.resume_prepared_review(str(self.spec_path), approval,
                                                   approved_backup_sha256=digest)
        self.assertTrue(result['applied'])
        self.assertEqual(before, {name: (directory / name).read_bytes() for name in before})
        original = next(row for row in self.rows('tasks') if row['id'] == self.task)
        self.assertEqual(('failed', 2, 'PRIVATE_ORIGINAL_INPUT'),
                         (original['status'], original['epoch'], original['input']))
        self.assertTrue(review.review_status(str(self.spec_path))['applied'])
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'requires_status'):
            review.resume_prepared_review(str(self.spec_path), approval, approved_backup_sha256=digest)

    def test_prepared_expired_original_requires_separate_fresh_digest_and_lost_ack_uses_original(self):
        approval, directory = self.prepared_after_old_size_failure()
        original_bytes = self.spec_path.read_bytes()
        fresh = json.loads(json.dumps(self.spec))
        future = self.spec['expires_at'] + 1
        fresh.update(issued_at=future - 1, expires_at=future + 600)
        fresh_path = self.root / 'separate-fresh-review.json'
        self.private(fresh_path, fresh)
        fresh_approval = review.read_checked(fresh_path, True)[1]['sha256']
        backup_approval = review.digest_checked_backup(directory / '04-consistent-ledger.sql.gz')['sha256']
        with self.observed(), patch.object(review.time, 'time', return_value=future):
            with self.assertRaisesRegex(review.ReviewDenied, 'expired_or_future'):
                review.inspect_prepared(str(self.spec_path))
            with self.assertRaisesRegex(review.ReviewDenied, 'fresh_prepared_approval_required'):
                review.resume_prepared_review(str(self.spec_path), approval, str(fresh_path), 'f' * 64,
                                              backup_approval)
            context, staged = review.inspect_prepared(str(self.spec_path), str(fresh_path))
            digest = staged['ready']['backup_file']['sha256']
            original_write = review.write_json
            def lost(path, body):
                if path.name == '07-receipt.json':
                    raise OSError('simulated original receipt loss')
                return original_write(path, body)
            with patch.object(review, 'write_json', lost):
                result = review.resume_prepared_review(str(self.spec_path), approval, str(fresh_path),
                                                       fresh_approval, digest)
            self.assertTrue(result['outcome_unknown'])
            self.assertTrue(review.review_status(str(self.spec_path))['applied'])
            with self.assertRaisesRegex(review.ReviewDenied, 'review_record_spec_or_before_conflict'):
                review.review_status(str(fresh_path))
        self.assertEqual(original_bytes, self.spec_path.read_bytes())
        intent = json.loads((directory / '00-intent.json').read_text())
        self.assertEqual(approval, intent['spec_sha256'])
        journal = self.rows('native_effect_reviews')[0]
        proof = json.loads(journal['proof'])
        self.assertEqual(approval, proof['spec_sha256'])
        self.assertEqual(fresh_approval, proof['fresh_approved_spec_sha256'])
        reviews = list(directory.glob('04-resume-review-*.json'))
        self.assertEqual(1, len(reviews))
        self.assertEqual(fresh_approval, json.loads(reviews[0].read_text())['fresh_approved_spec_sha256'])

    def test_prepared_before_cas_local_interruption_can_only_complete_same_files(self):
        approval, directory = self.prepared_after_old_size_failure()
        digest = review.digest_checked_backup(directory / '04-consistent-ledger.sql.gz')['sha256']
        original_write = review.write_json
        def stop_before_cas(path, body):
            if path.name == '06-cas-intent.json':
                raise OSError('known failure before creating any CAS file')
            return original_write(path, body)
        with self.observed(), patch.object(review, 'write_json', stop_before_cas):
            with self.assertRaises(OSError):
                review.resume_prepared_review(str(self.spec_path), approval, approved_backup_sha256=digest)
        self.assertTrue((directory / '05-backup-complete.json').exists())
        self.assertFalse((directory / '06-cas-intent.json').exists())
        ready = (directory / '05-backup-complete.json').read_bytes()
        with self.observed():
            result = review.resume_prepared_review(str(self.spec_path), approval, approved_backup_sha256=digest)
        self.assertTrue(result['applied'])
        self.assertEqual(ready, (directory / '05-backup-complete.json').read_bytes())

    def test_prepared_corrupt_incomplete_or_reduced_backup_never_creates_cas(self):
        approval, directory = self.prepared_after_old_size_failure()
        backup = directory / '04-consistent-ledger.sql.gz'
        original = backup.read_bytes()
        sql = gzip.decompress(original).decode('utf8')
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.executescript(sql)
            db.execute('DROP TABLE nodes')  # Not used by selected task checks.
            reduced = '\n'.join(db.iterdump()).encode()
        cases = (('gzip_truncated', original[:-4]),
                 ('sql_without_commit', gzip.compress(sql.replace('COMMIT;', '').encode())),
                 ('missing_unrelated_table', gzip.compress(reduced)),
                 ('external_database', gzip.compress(sql.replace('COMMIT;',
                    "ATTACH DATABASE '" + str(self.root / 'not-created.sqlite') + "' AS extra;\nCOMMIT;").encode())))
        for name, changed in cases:
            backup.write_bytes(changed)
            with self.subTest(kind=name), self.observed(), self.assertRaises(
                    (review.ReviewDenied, EOFError, OSError, sqlite3.Error)):
                review.resume_prepared_review(str(self.spec_path), approval,
                                              approved_backup_sha256=review.sha(changed))
            self.assertFalse((directory / '05-backup-complete.json').exists())
            self.assertFalse((directory / '06-cas-intent.json').exists())
            self.assertFalse((self.root / 'not-created.sqlite').exists())
        self.assertTrue(json.loads(self.rows('tasks')[0]['checkpoint'])['side_effect_started'])

    def test_prepared_low_space_changed_before_or_existing_review_cannot_resume(self):
        approval, directory = self.prepared_after_old_size_failure()
        with self.observed(), patch.object(review.shutil, 'disk_usage', return_value=type('Space', (), {'free': 0})()):
            with self.assertRaisesRegex(review.ReviewDenied, 'verification_space_required'):
                review.inspect_prepared(str(self.spec_path))
        before_path = directory / '01-before.json'
        previous = before_path.read_bytes()
        changed = json.loads(previous)
        changed['task']['input'] += ' changed'
        self.private(before_path, changed)
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'original_prepared_before_changed'):
            review.inspect_prepared(str(self.spec_path))
        before_path.write_bytes(previous)
        with self.store.transaction() as db:
            db.execute('CREATE TABLE native_effect_reviews(id TEXT)')
        with self.observed(), self.assertRaisesRegex(review.ReviewDenied, 'ledger_review_requires_status'):
            review.inspect_prepared(str(self.spec_path))
        self.assertFalse((directory / '06-cas-intent.json').exists())

    def test_prepared_cli_audit_is_readonly_and_resume_requires_original_and_backup_approval(self):
        approval, directory = self.prepared_after_old_size_failure()
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        output = io.StringIO()
        with self.observed(), patch('sys.stdout', output):
            self.assertEqual(0, review.main(['--spec-file', str(self.spec_path), '--audit-prepared']))
        audit = json.loads(output.getvalue())
        self.assertTrue(audit['read_only'])
        self.assertTrue(audit['complete_saved_backup_verified'])
        self.assertFalse(audit['retry_authorized'])
        self.assertEqual(approval, audit['original_spec_sha256'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})
        for secret in ('SYNTHETIC_PRIVATE_ACCESS', 'PRIVATE_ORIGINAL_INPUT', 'owner@example.invalid'):
            self.assertNotIn(secret, output.getvalue())
        output = io.StringIO()
        with self.observed(), patch('sys.stdout', output):
            self.assertEqual(2, review.main(['--spec-file', str(self.spec_path), '--resume-prepared',
                '--approve-spec-sha256', approval]))
        self.assertEqual('explicit_complete_backup_digest_approval_required', json.loads(output.getvalue())['code'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})
        output = io.StringIO()
        with self.observed(), patch('sys.stdout', output):
            self.assertEqual(0, review.main(['--spec-file', str(self.spec_path), '--resume-prepared',
                '--approve-spec-sha256', approval, '--approve-prepared-backup-sha256',
                audit['backup_compressed_sha256']]))
        self.assertTrue(json.loads(output.getvalue())['applied'])

    def test_prepared_authorizer_keeps_fixed_opcodes_when_python_symbols_are_missing(self):
        approval, directory = self.prepared_after_old_size_failure()
        symbols = ('SQLITE_CREATE_VTABLE', 'SQLITE_DROP_VTABLE', 'SQLITE_FUNCTION')
        previous = {name: getattr(sqlite3, name) for name in symbols if hasattr(sqlite3, name)}
        try:
            for name in previous:
                delattr(sqlite3, name)
            for action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA, 29, 30):
                self.assertEqual(sqlite3.SQLITE_DENY, review.backup_authorizer(action, None, None, None, None))
            for function in ('load_extension', 'LOAD_EXTENSION'):
                self.assertEqual(sqlite3.SQLITE_DENY, review.backup_authorizer(31, None, function, None, None))
            self.assertEqual(sqlite3.SQLITE_OK, review.backup_authorizer(31, None, 'length', None, None))
            with self.observed():
                context, staged = review.inspect_prepared(str(self.spec_path))
            self.assertEqual(self.task, context['selected']['task']['id'])
            self.assertGreater(staged['ready']['backup_uncompressed_size'], 0)
            self.assertFalse((directory / '05-backup-complete.json').exists())
        finally:
            for name, value in previous.items():
                setattr(sqlite3, name, value)

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
