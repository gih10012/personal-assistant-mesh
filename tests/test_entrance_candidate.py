"""Exact frozen source, actual loopback API + owned process EOF; NO model/iLink.

The failed native turn notification is explicitly synthetic. Its original reader
and process reaping, complete history upload, SQLite and HTTP are actual here.
"""
import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_legacy_fenced_receiver import _source_base


SCRIPT = r'''
import base64, gzip, hashlib, json, queue, subprocess, sys, tempfile, threading, time
from pathlib import Path
from assistant_mesh import server
from assistant_mesh.entrance import entrance_store_class, message_identity, validate_policy
from assistant_mesh.entrance_runtime import EntranceWorker

policy = {'owner_ref':'owner','conversation_id':'bound-owner','agent_id':'entrance'}
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    token = root / 'worker.token'
    token.write_text('synthetic-entrance-' + 'x'*64)
    token.chmod(0o600)
    config = {'database': str(root/'test.sqlite'), 'port':0,
        'node_id':'fixture-authority', 'peers':[{'role':'worker','node':'entry',
        'capabilities':[validate_policy(policy)['capability']], 'token_file':str(token)}]}
    original = server.Store
    selected = entrance_store_class(original, policy)
    server.Store = selected
    ready, state, errors = threading.Event(), {}, []
    def bound(http, api, channel):
        assert channel is None
        state.update(http=http,api=api)
        ready.set()
    def run():
        try: server.serve(config, ready=bound)
        except BaseException as error:
            errors.append(type(error).__name__)
            ready.set()
    runner = threading.Thread(target=run)
    runner.start()
    assert ready.wait(5), 'exact API did not bind'
    try:
        assert not errors, errors
        from assistant_mesh.worker import Client
        client_config = {'control_url':'http://127.0.0.1:'+str(state['http'].server_address[1]),
            'token_file':str(token), 'node_id':'entry',
            'capabilities':[validate_policy(policy)['capability']], 'entrance':policy,
            'codex':{'workspace':str(root),'auth_home':str(root/'synthetic-home'),
                     'strict_auth_home':True,'model':'synthetic-no-model'}}
        client = Client(client_config)
        api = state['api']
        ms = int(time.time()*1000)+10
        message = {'message_id':'original-platform-id','from_user_id':'owner','to_user_id':'bot',
            'message_type':1,'create_time_ms':ms,
            'item_list':[{'type':1,'text_item':{'text':'original owner message'}}]}
        assert api.store.ingest([message],'original-cursor','owner','bot') == 1
        client.request('/v1/heartbeat',{'capabilities':client_config['capabilities']})
        task = client.request('/v1/claim',{})['task']
        assert task['scope'] == validate_policy(policy)['scope']
        from assistant_mesh.entrance_failure import history_snapshot
        thread = '01000000-0000-7000-8000-000000000001'
        raw = (json.dumps({'type':'session_meta','payload':{'id':thread}})+'\n'+
               json.dumps({'type':'event_msg','payload':{'type':'failed','private':'complete failed bytes'}})+'\n').encode()
        rollout = root/'full-original.jsonl'
        rollout.write_bytes(raw)
        rollout.chmod(0o600)
        task['checkpoint'].update(thread_id=thread,turn_id='failed-turn',side_effect_started=True)
        client.request('/v1/task/update',{'id':task['id'],'epoch':task['epoch'],'checkpoint':task['checkpoint']})
        observed_errors = []
        class ObservedWorker(EntranceWorker):
            def _save_closed_failure(self,*args):
                try: return super()._save_closed_failure(*args)
                except BaseException as error:
                    import traceback
                    observed_errors.append(traceback.format_exc())
                    raise
        worker = ObservedWorker(client_config,client=client)
        from assistant_mesh.entrance_runtime import run_entrance_worker
        from unittest.mock import patch
        with patch('assistant_mesh.entrance_runtime.EntranceWorker') as constructor:
            run_entrance_worker(client_config)
            constructor.return_value.run.assert_called_once_with()
        worker.current = task
        runtime = worker.backend.__new__(worker.backend)
        runtime._entrance_reader_eof = threading.Event()
        runtime._entrance_failure_saved = False
        runtime.process = subprocess.Popen([sys.executable,'-c','print("{}",flush=True)'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            universal_newlines=True,start_new_session=True)
        runtime.events, runtime.deferred = queue.Queue(), []
        runtime.thread_id, runtime.turn_id = thread,'failed-turn'
        runtime.auth_home = client_config['codex']['auth_home']
        # Controlled protocol fixture, NOT an actual OpenAI completion event.
        runtime.native_observations = {'turn_completed':{'threadId':thread,
            'turn':{'id':'failed-turn','status':'failed'}}}
        runtime.goal = lambda: None
        runtime.native_rollout = lambda: rollout
        reader = threading.Thread(target=runtime._read)
        reader.start()
        runtime.process.wait(timeout=5)
        reader.join(5)
        assert not reader.is_alive() and runtime._entrance_reader_eof.is_set()
        runtime.__exit__(ValueError,ValueError('codex_turn_failed'),None)
        assert not observed_errors, observed_errors
        runtime.process.stdin.close()
        runtime.process.stdout.close()
        client.request('/v1/task/update',{'id':task['id'],'epoch':task['epoch'],
            'result':'known failed turn; external operations remain unknown','status':'waiting_backend'})
        saved_time = api.store.clock()
        api.store.clock = lambda: saved_time+100
        fresh = dict(message,message_id='new-message-not-retry',
            item_list=[{'type':1,'text_item':{'text':'Do not replay the old send; what is happening now?'}}])
        api.store.ingest([fresh],'next-cursor','owner','bot')
        client.request('/v1/heartbeat',{'capabilities':client_config['capabilities']})
        next_task = client.request('/v1/claim',{})['task']
        assert next_task['id'] == 'entrance-'+message_identity(fresh)
        assert next_task['session']['state']['thread_id'] == thread
        body = client.request('/v1/session',{'task_id':next_task['id'],'epoch':next_task['epoch'],
            'action':'download','payload':{'part':0}})['data']
        assert gzip.decompress(base64.b64decode(body)) == raw
        with api.store.transaction() as db:
            old = db.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone()
            assert old['attempts'] == 1 and old['status'] == 'needs_review'
            assert json.loads(old['checkpoint'])['side_effect_started'] is True
            assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM entrance_closed_failures').fetchone()[0] == 1
        print(json.dumps({'original_api_loopback':True,'owned_process_reaped':True,'actual_reader_eof':True,
            'raw_failed_history_full_bytes':len(raw),'raw_sha256':hashlib.sha256(raw).hexdigest(),
            'same_thread_new_message':True,'old_guard_preserved':True,'old_input_replayed':False,
            'model_calls':0,'ilink_calls':0}))
    finally:
        state['http'].shutdown()
        runner.join(5)
        server.Store = original
        assert not runner.is_alive()
'''


class EntranceCandidateTests(unittest.TestCase):
    def test_exact_candidate_keeps_incumbent_worker_and_runs_actual_api_history_path(self):
        repository = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('_entrance_candidate_builder',
            str(repository / 'deploy' / 'build-entrance-candidate.py'))
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / 'base' / 'assistant_mesh'
            base.mkdir(parents=True, mode=0o700)
            original = _source_base()
            for name, data in original.items():
                (base / name).write_bytes(data)
            candidate = root / 'candidate'
            report = builder.build(base.parent, candidate, repository)
            self.assertEqual(38, report['unchanged_original_modules'])
            self.assertEqual(original['worker.py'], (candidate/'assistant_mesh/worker.py').read_bytes())
            self.assertEqual(original['codex.py'], (candidate/'assistant_mesh/codex.py').read_bytes())
            for name in builder.MODULES:
                data = (candidate/'assistant_mesh'/name).read_text()
                ast.parse(data, feature_version=(3, 6))
            result = subprocess.run([sys.executable, '-c', SCRIPT], cwd=str(candidate),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                universal_newlines=True)
            self.assertEqual(0, result.returncode, result.stderr)
            output = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(output['same_thread_new_message'])
            self.assertTrue(output['old_guard_preserved'])
            self.assertFalse(output['old_input_replayed'])
            self.assertEqual(0, output['model_calls'])
            self.assertEqual(0, output['ilink_calls'])


if __name__ == '__main__':
    unittest.main()
