import errno
import json
from pathlib import Path
import tempfile
import types
import unittest
import urllib.error

from assistant_mesh.metadata_diagnostics import (
    MetadataDiagnostics, install_save_observer, observe_backend, observe_client, observe_worker)


TASK = {'id': 'original-task', 'epoch': 7}


class MetadataDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.log = Path(self.temporary.name) / 'metadata.jsonl'
        self.recorder = MetadataDiagnostics(str(self.log), enabled=True)

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_client_failure_is_once_same_exception_and_metadata_only(self):
        failure = urllib.error.HTTPError('https://secret.invalid/TOKEN', 409, 'SECRET PATH', {}, None)
        class Client:
            calls = 0
            def request(self, path, body):
                self.calls += 1
                raise failure
        client = Client()
        observed = observe_client(client, self.recorder)
        body = {'task_id': TASK['id'], 'epoch': 7, 'action': 'upload',
                'payload': {'part': 3, 'data': 'PRIVATE_BODY_TOKEN'}}
        with self.recorder.bind(lambda: TASK):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                observed.request('/v1/session?private-query=SECRET', body)
        self.assertIs(failure, caught.exception)
        self.assertEqual(1, client.calls)
        value = self.records()[0]
        self.assertEqual(('original-task', 7, 409, 'upload', 3),
                         (value['task_id'], value['epoch'], value['http_status'], value['action'], value['part']))
        self.assertEqual(len(json.dumps(body).encode()), value['payload_bytes'])
        self.assertEqual(('control.session', 'POST'), (value['method'], value['http_method']))
        for private in ('SECRET', 'TOKEN', 'PRIVATE_BODY', 'secret.invalid', 'private-query'):
            self.assertNotIn(private, self.log.read_text())
        self.assertEqual(0o600, self.log.stat().st_mode & 0o777)

    def test_rpc_save_and_original_backend_hooks_keep_single_call_and_failure(self):
        failure = OSError(errno.ENOSPC, 'SECRET /private/source.jsonl')
        class Backend:
            original_failure_hook = True
            calls = 0
            def rpc(self, method, params):
                self.calls += 1
                self.last_protocol_error = {'code': -32000, 'message': 'SECRET RAW ERROR',
                    'data': {'httpStatusCode': 503, 'path': '/private/native'}}
                raise failure
        native = observe_backend(Backend, self.recorder)()
        self.assertTrue(native.original_failure_hook)
        with self.recorder.bind(lambda: TASK):
            with self.assertRaises(OSError) as caught:
                native.rpc('thread/read', {'threadId': 'private-thread'})
        self.assertIs(failure, caught.exception)
        self.assertEqual(1, native.calls)

        calls, ticks = [], []
        class Client:
            def request(self, path, body):
                calls.append((path, body))
                raise failure
        def save(client, task, node, harness, state, rollout=None, tick=None):
            tick()
            return client.request('/v1/session', {'action': 'commit', 'payload': {'state': state}})
        module = types.SimpleNamespace(save=save)
        self.assertFalse(install_save_observer(module))
        self.assertIs(save, module.save)
        self.assertTrue(install_save_observer(module, enabled=True))
        installed = module.save
        self.assertTrue(install_save_observer(module, enabled=True))
        self.assertIs(installed, module.save)
        with self.recorder.bind(lambda: TASK):
            with self.assertRaises(OSError) as caught:
                module.save(observe_client(Client(), self.recorder), TASK, 'node', 'codex',
                    {'secret': 'PRIVATE_BODY'}, '/private/source.jsonl', lambda: ticks.append(1))
        self.assertIs(failure, caught.exception)
        self.assertEqual((1, 1), (len(calls), len(ticks)))
        records = self.records()
        self.assertEqual('native_rpc', records[0]['kind'])
        self.assertEqual(errno.ENOSPC, records[0]['errno'])
        self.assertEqual((-32000, 503), (records[0]['native_error_code'], records[0]['http_status']))
        self.assertEqual(['session_pack_tick', 'session_commit', 'session_commit'],
                         [r['substep'] for r in records[1:]])
        self.assertNotIn('/private/', self.log.read_text())
        self.assertNotIn('PRIVATE_BODY', self.log.read_text())
        self.assertNotIn('SECRET', self.log.read_text())

    def test_disabled_and_failed_logging_do_not_change_worker_result_or_hooks(self):
        class Client:
            calls = 0
            def request(self, path, body):
                self.calls += 1
                return {'unchanged': True}
        class Backend:
            original_hook = True
        class Worker:
            def __init__(self):
                self.client, self.backend, self.current = Client(), Backend, TASK
            def run_once(self):
                return self.client.request('/v1/task/update', {'private': 'BODY'})
        disabled = MetadataDiagnostics(str(self.log))
        self.assertIs(Worker, observe_worker(Worker, disabled))
        client = Client()
        self.assertIs(client, observe_client(client, disabled))
        self.assertIs(Backend, observe_backend(Backend, disabled))
        self.assertFalse(self.log.exists())
        # Missing directory: no implicit directory creation or error propagation.
        recorder = MetadataDiagnostics(str(self.log.parent / 'missing' / 'log'), enabled=True)
        worker = observe_worker(Worker, recorder)()
        self.assertTrue(worker.backend.original_hook)
        self.assertEqual({'unchanged': True}, worker.run_once())
        self.assertEqual(1, worker.client.calls)
        self.assertEqual(1, recorder.dropped)
        self.assertFalse((self.log.parent / 'missing').exists())


if __name__ == '__main__':
    unittest.main()
