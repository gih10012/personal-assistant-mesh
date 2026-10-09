"""Scoped owner entrypoints over real private-token HTTP, not OAuth proof."""
import copy
import contextlib
import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from assistant_mesh.cli import main
from assistant_mesh.server import API, serve
from assistant_mesh.worker import Client


class IngressGatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.server = None
        self.clients = {}
        self.peers = []
        for name, role, scopes in (('operator', 'operator', None), ('viewer', 'viewer', None),
                                  ('worker', 'worker', None), ('ingress', 'ingress', ['tasks:submit', 'tasks:read']),
                                  ('other', 'ingress', ['tasks:submit', 'tasks:read']),
                                  ('read', 'ingress', ['tasks:read']), ('revoked', 'ingress', [])):
            token = self.root / (name + '.token')
            token.write_text(name + '-' + 'i' * 64)
            token.chmod(0o600)
            peer = {'role': role, 'token_file': str(token)}
            if scopes is not None:
                peer['ingress'] = {'owner_id': 'fixture-owner', 'source': 'fixture-chat',
                                   'subject': 'fixture-other' if name == 'other' else 'fixture-self', 'scopes': scopes}
            if role == 'worker':
                peer.update(node='fixture-node', capabilities=['leader'])
            self.peers.append(peer)
        self.config = {'node_id': 'fixture-authority', 'database': str(self.root / 'ledger.sqlite'),
                       'port': 0, 'peers': self.peers, 'ingress': {'owner_id': 'fixture-owner'}}
        ready = threading.Event()

        def started(server, api, channel):
            self.server, self.api = server, api
            ready.set()

        self.thread = threading.Thread(target=serve, args=(self.config, started), daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(10))
        url = 'http://127.0.0.1:' + str(self.server.server_address[1])
        for path in self.root.glob('*.token'):
            self.clients[path.stem] = Client({'control_url': url, 'token_file': str(path)})

    def tearDown(self):
        if self.server:
            self.server.shutdown()
        self.thread.join(timeout=3)
        self.directory.cleanup()

    def submit(self, name='ingress', **extra):
        return self.clients[name].request('/v1/ingress/tasks', dict(
            request_id='original-request', input='Fixture owner task, no model or business execution.', **extra))

    def failure(self, name, path, body, code):
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.clients[name].request(path, body)
        self.assertEqual(code, failure.exception.code)
        failure.exception.close()

    def count_tasks(self):
        with self.api.store.transaction() as db:
            return db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]

    def completed_publication(self):
        receipt = self.submit()
        self.api.store.heartbeat('fixture-node', ['leader'])
        task = self.api.store.claim('fixture-node')
        self.assertEqual(receipt['task_id'], task['id'])
        raw = 'PRIVATE_RAW_RESULT PRIVATE_NATIVE_THREAD'
        self.api.store.update_task(task['id'], 'fixture-node', task['epoch'],
            result=raw, status='completed', checkpoint={'thread_id': 'PRIVATE_NATIVE_THREAD'})
        return {'publication_id': 'owner-approved-result', 'source': 'fixture-chat',
                'subject': 'fixture-self', 'request_id': 'original-request',
                'task_id': task['id'], 'task_epoch': task['epoch'],
                'task_result_sha256': hashlib.sha256(raw.encode('utf8')).hexdigest(),
                'reviewed': True, 'result': {'summary': 'Owner-reviewed fixture result.'}}

    def test_result_publication_and_same_subject_read_use_separate_authority(self):
        payload = self.completed_publication()
        value = self.clients['operator'].request('/v1/ingress-result/publish', payload)
        self.assertTrue(value['publication_created'])
        self.assertFalse(self.clients['operator'].request('/v1/ingress-result/publish', payload)['publication_created'])
        result = self.clients['ingress'].request('/v1/ingress/task/result', {'request_id': 'original-request'})
        self.assertEqual(payload['result']['summary'], result['result']['summary'])
        self.assertTrue(result['task_binding_verified'])
        self.assertFalse(result['execution_verified'])
        self.assertFalse(result['artifact_content_verified'])
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.failure('other', '/v1/ingress/task/result', {'request_id': 'original-request'}, 403)
        self.failure('revoked', '/v1/ingress/task/result', {'request_id': 'original-request'}, 403)

    def test_result_publish_cannot_be_self_approved_or_mapped_to_other_roles(self):
        payload = self.completed_publication()
        for name in ('ingress', 'other', 'viewer', 'worker', 'read', 'revoked'):
            self.failure(name, '/v1/ingress-result/publish', payload, 403)
        self.failure('operator', '/v1/ingress-result/publish', None, 403)
        self.failure('operator', '/v1/ingress-result/publish?subject=forged', payload, 403)
        self.failure('operator', '/v1/ingress/task/result', {'request_id': 'original-request'}, 403)
        self.failure('ingress', '/v1/ingress/task/result', {'request_id': 'original-request'}, 403)

    def test_stale_or_modified_result_anchor_is_not_returned(self):
        payload = self.completed_publication()
        self.clients['operator'].request('/v1/ingress-result/publish', payload)
        with self.api.store.transaction() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?', ('changed-private-result', payload['task_id']))
        self.failure('ingress', '/v1/ingress/task/result', {'request_id': 'original-request'}, 409)

    def test_result_cli_private_payload_respects_operator_publish_and_scoped_read(self):
        payload = self.completed_publication()
        body = self.root / 'publication.json'
        body.write_text(json.dumps(payload))
        body.chmod(0o600)

        def call(name, command):
            config = self.root / (name + '-result-client.json')
            config.write_text(json.dumps({'control_url': self.clients[name].url,
                                          'token_file': str(self.root / (name + '.token'))}))
            config.chmod(0o600)
            out = io.StringIO()
            with mock.patch('sys.argv', ['mesh', '--config', str(config), command,
                                        '--payload-file', str(body)]), contextlib.redirect_stdout(out):
                main()
            return json.loads(out.getvalue())

        self.assertTrue(call('operator', 'ingress-publish')['publication_created'])
        body.write_text(json.dumps({'request_id': 'original-request'}))
        self.assertEqual(payload['result']['summary'], call('ingress', 'ingress-result')['result']['summary'])

    def test_submit_readback_retry_and_original_native_scope(self):
        first = self.submit(project_id='owner-project', agent_id='suggested-specialist')
        self.assertTrue(first['task_created'])
        self.assertFalse(self.submit(project_id='owner-project', agent_id='suggested-specialist')['task_created'])
        result = self.clients['ingress'].request('/v1/ingress/task/status', {'request_id': 'original-request'})
        self.assertEqual(first['task_id'], result['task_id'])
        self.assertEqual('pending', result['status'])
        self.assertFalse(result['execution_verified'])
        self.assertEqual(1, self.count_tasks())
        with self.api.store.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (first['task_id'],)).fetchone()
            context = json.loads(row['context'])
            self.assertEqual('leader:owner', context['session_scope'])
            self.assertNotIn('agent_id', context)
            self.assertEqual(['leader'], json.loads(row['required']))

    def test_payload_cannot_override_identity_policy_context_or_native_session(self):
        for key in ('actor', 'owner_id', 'source', 'subject', 'scopes', 'required', 'context',
                    'session_scope', 'thread_id', 'node', 'task_id', 'id', 'parent_id', 'token'):
            self.failure('ingress', '/v1/ingress/tasks', {
                'request_id': 'original-request', 'input': 'fixture', key: 'forged'}, 400)
        self.assertEqual(0, self.count_tasks())

    def test_ingress_never_falls_through_global_routes_or_authority_roles(self):
        for path, body in (('/v1/status', None), ('/v1/projects', None), ('/v1/resources', None),
                           ('/v1/routing/decisions', None), ('/v1/tasks', {'input': 'forged operator'}),
                           ('/v1/heartbeat', {'capabilities': ['leader']}), ('/v1/claim', {}),
                           ('/v1/notify', {'request_id': 'forged', 'text': 'must not send'}),
                           ('/v1/allocation/action', {'action': 'define_pool', 'arguments': {}})):
            self.failure('ingress', path, body, 403)
        self.assertEqual(0, self.count_tasks())
        for name in ('operator', 'viewer', 'worker'):
            self.failure(name, '/v1/ingress/tasks', {'request_id': 'x', 'input': 'fixture'}, 403)

    def test_scopes_cross_subject_and_no_global_id_lookup(self):
        first = self.submit()
        self.failure('read', '/v1/ingress/tasks', {'request_id': 'x', 'input': 'fixture'}, 403)
        self.failure('revoked', '/v1/ingress/task/status', {'request_id': 'original-request'}, 403)
        self.failure('other', '/v1/ingress/task/status', {'request_id': 'original-request'}, 403)
        self.failure('ingress', '/v1/ingress/task/status', {'task_id': first['task_id']}, 400)
        self.assertEqual(first['task_id'], self.clients['read'].request('/v1/ingress/task/status',
            {'request_id': 'original-request'})['task_id'])
        other = self.submit(name='other')
        self.assertNotEqual(first['task_id'], other['task_id'])

    def test_changed_request_content_conflicts_no_new_task(self):
        first = self.submit()
        self.failure('ingress', '/v1/ingress/tasks', {'request_id': 'original-request', 'input': 'changed'}, 409)
        self.assertEqual(first['task_id'], self.submit()['task_id'])
        self.assertEqual(1, self.count_tasks())

    def test_status_does_not_disclose_private_task_checkpoint_or_result(self):
        receipt = self.submit()
        with self.api.store.transaction() as db:
            db.execute('UPDATE tasks SET result=?,checkpoint=? WHERE id=?',
                       ('fixture-private-result', json.dumps({'secret': 'fixture-secret', 'thread_id': 'private-thread'}), receipt['task_id']))
        value = self.clients['ingress'].request('/v1/ingress/task/status', {'request_id': 'original-request'})
        self.assertTrue(value['result_available'])
        for secret in ('fixture-private-result', 'fixture-secret', 'private-thread', 'checkpoint'):
            self.assertNotIn(secret, json.dumps(value))

    def test_method_query_disabled_and_peer_node_injection_rejected(self):
        self.failure('ingress', '/v1/ingress/tasks', None, 403)
        self.failure('ingress', '/v1/ingress/tasks?owner_id=forged', {'request_id': 'x', 'input': 'fixture'}, 403)
        malformed = copy.deepcopy(self.peers[3])
        malformed['node'] = 'fixture-node'
        with self.assertRaises((PermissionError, ValueError)):
            self.api.dispatch('POST', '/v1/ingress/tasks', {'request_id': 'x', 'input': 'fixture'}, malformed)
        bad = copy.deepcopy(self.config)
        del bad['ingress']
        with self.assertRaises(ValueError):
            API(bad)

    def test_scoped_cli_private_payload_submit_and_readback(self):
        config = self.root / 'ingress-client.json'
        config.write_text(json.dumps({'control_url': self.clients['ingress'].url,
                                      'token_file': str(self.root / 'ingress.token')}))
        config.chmod(0o600)
        payload = self.root / 'ingress-input.json'
        payload.write_text(json.dumps({'request_id': 'cli-request', 'input': 'fixture CLI task'}))
        payload.chmod(0o600)

        def call(command, *extra):
            out = io.StringIO()
            with mock.patch('sys.argv', ['mesh', '--config', str(config), command,
                                        '--payload-file', str(payload)] + list(extra)), contextlib.redirect_stdout(out):
                main()
            return json.loads(out.getvalue())

        first = call('ingress-submit')
        payload.write_text(json.dumps({'request_id': 'cli-request'}))
        self.assertEqual(first['task_id'], call('ingress-status')['task_id'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            call('ingress-status', '--principal', 'operator')

    def test_ambiguous_ingress_bearer_rejected_before_opening_business_state(self):
        for first, second in ((0, 3), (2, 3), (3, 4)):
            config = copy.deepcopy(self.config)
            database = self.root / ('ambiguous-%s-%s.sqlite' % (first, second))
            config['database'] = str(database)
            config['peers'][second]['token_file'] = config['peers'][first]['token_file']
            with self.assertRaisesRegex(ValueError, 'ambiguous_ingress_credential'):
                API(config)
            self.assertFalse(database.exists())


if __name__ == '__main__':
    unittest.main()
