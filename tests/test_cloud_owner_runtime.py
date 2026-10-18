"""Real isolated Store claims and loopback; no provider/model/production state."""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.request

from assistant_mesh import cloud_owner_runtime as owner
from assistant_mesh.cloud_membership import node_id
from assistant_mesh.cloud_adoption import CloudAdoptions
from assistant_mesh.conversation_authority import attach
from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults
from assistant_mesh.store import Conflict, Store


TOKEN = 'SYNTHETIC_OWNER_CLOUD_' + 'x' * 40
OWNER = 'synthetic-owner'


class CloudOwnerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-cloud-owner-runtime.')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 1000
        self.store = Store(self.root / 'authority.sqlite', clock=lambda: self.now)
        ingress = Ingress(self.store, 'actual-authority', {'owner_id': OWNER})
        results = IngressResults(self.store, 'actual-authority', {'owner_id': OWNER})
        self.api = SimpleNamespace(store=self.store, ingress=ingress, ingress_results=results,
                                   dispatch=lambda *args: {'original': True})
        self.store.create_task('synthetic real enrollment claim', ['enrollment'], task_id='actual-enrollment',
                               context={'session_scope': 'fixture:independent-enrollment-work'})
        self.store.heartbeat('actual-holder', ['enrollment'], 10)
        task = self.store.claim('actual-holder')
        self.assertEqual('actual-enrollment', task['id'])
        # planned_id is pure; the uninitialized object is used ONLY to prepare
        # synthetic fixture IDs without creating the journal before installation.
        identity = SimpleNamespace(owner=OWNER)
        self.job_id = CloudAdoptions.planned_id(identity, task['id'], 'original-owner-join')
        self.config = {'format': owner.FORMAT,
            'adoptions_directory': str(self.root / 'adopted'),
            'conversation_directory': str(self.root / 'cloud-conversation'),
            'loopback_port': 0, 'task_holder': 'actual-holder',
            'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest(),
            'grant': {'format': 'mesh-cloud-membership/1', 'mesh_task_id': task['id'],
                'mesh_epoch': task['epoch'], 'request_id': 'original-owner-join',
                'lease_id': 'private-original-claim-reference', 'expires_at': 2000,
                'callback_url': 'https://mesh.example.test/cloud/node',
                'scopes': ['node:hello', 'task:read', 'tasks:submit', 'artifact:write', 'task:report'],
                'capabilities': [], 'enrollment': {
                    'format': 'mesh-enrollment/1', 'mesh_id': 'synthetic-mesh',
                    'node_id': node_id(self.job_id), 'owner_binding': OWNER,
                    'repo_pin': {'repository': 'https://example.test/mesh', 'commit': 'a' * 40},
                    'runtime': {'kind': 'temporary', 'expires_at': 2000, 'state_ref': 'mesh:durable'},
                    'join': {'peer_id': 'original-peer', 'authority_id': 'actual-authority',
                        'route_ref': 'route:public-https', 'credential_ref': 'secret:scoped-node',
                        'trust_ref': 'trust:original-owner'},
                    'continuation': {'project_id': 'fixed-project', 'session_ref': 'session:original',
                                     'state_ref': 'mesh:original'}}}}

    def runtime(self, config=None):
        runtime = owner.CloudOwnerRuntime(self.api, config or self.config)
        self.addCleanup(runtime.close)
        return runtime

    def installed(self):
        result = owner.install_owner(self.api, self.config, authorized=True)
        self.assertTrue(result['prepared'])
        runtime = self.runtime()
        self.assertTrue(runtime.start()['serving'])
        return runtime

    def envelope(self, action, arguments=None, request_id=None):
        grant = self.config['grant']
        return {'format': 'mesh-cloud-request/1', 'job_id': self.job_id,
            'task_id': grant['mesh_task_id'], 'epoch': grant['mesh_epoch'], 'lease_id': grant['lease_id'],
            'request_id': request_id or action, 'action': action, 'arguments': arguments or {}}

    def post(self, runtime, action, arguments=None, request_id=None, envelope=None, path='/cloud/node'):
        port = runtime.status()['loopback_port']
        request = urllib.request.Request('http://127.0.0.1:{}{}'.format(port, path),
            data=json.dumps(envelope or self.envelope(action, arguments, request_id)).encode(),
            headers={'Host': 'mesh.example.test', 'X-Forwarded-Proto': 'https',
                     'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            return json.load(response)

    def test_explicit_install_and_start_never_create_replacement_work_or_grant(self):
        runtime = self.runtime()
        self.assertEqual('owner_install_missing', runtime.start()['reason'])
        self.assertFalse(Path(self.config['adoptions_directory']).exists())
        with self.assertRaises(PermissionError):
            owner.install_owner(self.api, self.config)
        with self.store.transaction() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM tasks')]
        installed = owner.install_owner(self.api, self.config, authorized=True)
        self.assertTrue(installed['prepared'])
        self.assertFalse(installed['bootstrap']['credentials_included'])
        with mock.patch.object(CloudAdoptions, 'adopt', side_effect=AssertionError('startup adopted')), \
                mock.patch.object(owner.CloudMembership, 'prepare', side_effect=AssertionError('startup prepared')), \
                mock.patch.object(Store, '__init__', side_effect=AssertionError('new Store')):
            self.assertTrue(runtime.start()['serving'])
        with self.store.transaction() as db:
            self.assertEqual(before, [tuple(row) for row in db.execute('SELECT * FROM tasks')])
        self.assertEqual('owner_installed', installed['state'])
        self.assertFalse(runtime.membership.jobs.get(self.job_id)['submission_attempted'])

    def test_missing_claim_wrong_holder_and_epoch_are_unprepared_without_files(self):
        for changes in ({'mesh_task_id': 'PAM-DOCUMENT-ONLY'}, {'mesh_epoch': 99}, {}):
            config = copy.deepcopy(self.config)
            config['grant'].update(changes)
            if not changes:
                config['task_holder'] = 'another-live-pid-is-not-a-claim-holder'
            with self.subTest(changes=changes):
                self.assertEqual('original_task_not_live',
                    owner.install_owner(self.api, config, authorized=True)['reason'])
                self.assertEqual('original_task_not_live', self.runtime(config).start()['reason'])
                self.assertFalse(Path(config['adoptions_directory']).exists())
        self.now = 1091
        self.assertEqual('original_task_not_live',
            owner.install_owner(self.api, self.config, authorized=True)['reason'])
        self.assertFalse(Path(self.config['adoptions_directory']).exists())

    def test_fixed_cloud_principal_real_question_reply_and_close_preserve_chat(self):
        chat = attach(self.api)
        dispatch = self.api.dispatch
        runtime = self.installed()
        port = runtime.status()['loopback_port']
        self.assertEqual(('127.0.0.1', port), runtime._server.server_address)
        self.assertFalse(self.post(runtime, 'hello')['provider_identity_verified'])
        accepted = self.post(runtime, 'task_submit', {'input': 'synthetic owner request'}, 'entry-original')
        context = {'conversation_id': 'original-conversation', 'request_id': 'entry-original'}
        self.post(runtime, 'conversation_open', context)
        self.store.heartbeat('actual-leader', ['leader'], 10)
        task = self.store.claim('actual-leader')
        self.assertEqual(accepted['task_id'], task['id'])
        self.store.interaction(task['id'], 'actual-leader', task['epoch'], 'original-question', 'question',
                               {'questions': [{'id': 'which', 'question': 'Which branch?'}]})
        page = self.post(runtime, 'conversation_receive', context)
        question = next(item for item in page['messages'] if item['kind'] == 'question')
        reply = dict(context, message_id='original-answer', text='same branch', reply_to=question['message_id'])
        answered = self.post(runtime, 'conversation_reply', reply)
        self.assertEqual('native_answer_recorded', answered['runtime']['state'])
        receipt = self.post(runtime, 'conversation_receipt', dict(context, message_id='original-answer'))
        self.assertEqual('native_answer_recorded', receipt['runtime']['state'])
        self.assertEqual({'answers': {'which': {'answers': ['same branch']}}},
            self.store.interaction(task['id'], 'actual-leader', task['epoch'], 'original-question')['answer'])
        with self.store.transaction() as db:
            origin = db.execute('SELECT source,subject FROM ingress_requests WHERE task_id=?', (task['id'],)).fetchone()
        self.assertEqual(('openai-codex-cloud', node_id(self.job_id)), tuple(origin))
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.post(runtime, 'conversation_receive', dict(context, source='chatgpt-chat'))
        self.assertEqual(409, invalid.exception.code)
        invalid.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as management:
            self.post(runtime, 'status', path='/v1/task/status')
        self.assertEqual(404, management.exception.code)
        management.exception.close()
        self.assertIs(dispatch, self.api.dispatch)
        self.assertEqual(frozenset(('chatgpt-chat',)), chat.sources)
        closed = runtime.close()
        self.assertFalse(closed['remote_quiescence_verified'])
        self.assertEqual('owner_runtime_closed', runtime.start()['reason'])
        self.assertEqual('active', runtime.membership.get(self.job_id)['membership_state'])
        with self.assertRaises(urllib.error.URLError):
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                'http://127.0.0.1:{}/cloud/node'.format(port), timeout=1)

    def test_original_private_lease_request_and_credential_cannot_be_rebound_at_start(self):
        runtime = self.installed()
        binding = dict(self.config['grant'], job_id=self.job_id, node_id=node_id(self.job_id))
        self.assertTrue(runtime.membership.lease_validator(binding))
        for key in ('lease_id', 'request_id'):
            self.assertFalse(runtime.membership.lease_validator(dict(binding, **{key: 'different'})))
        changed = copy.deepcopy(self.config)
        changed['grant']['lease_id'] = 'different'
        with self.assertRaises(Conflict):
            self.runtime(changed).start()
        changed = copy.deepcopy(self.config)
        changed['token_sha256'] = 'b' * 64
        with self.assertRaises(Conflict):
            self.runtime(changed).start()
        changed = copy.deepcopy(self.config)
        changed['grant']['request_id'] = 'different'
        self.assertEqual('owner_install_missing', self.runtime(changed).start()['reason'])

    def test_real_epoch_loss_fences_new_http_write_without_consuming_intent(self):
        runtime = self.installed()
        self.post(runtime, 'hello')
        with runtime.membership.jobs._transaction() as db:
            before = db.execute('SELECT COUNT(*) FROM cloud_node_requests').fetchone()[0]
        self.store.control_task('actual-enrollment', 'pause')
        self.assertFalse(runtime.status()['prepared'])
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.post(runtime, 'task_submit', {'input': 'must not create work'}, 'after-original-epoch')
        self.assertEqual(409, rejected.exception.code)
        rejected.exception.close()
        with runtime.membership.jobs._transaction() as db:
            self.assertEqual(before, db.execute('SELECT COUNT(*) FROM cloud_node_requests').fetchone()[0])
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
        self.assertEqual('original_task_not_live', self.runtime().start()['reason'])

    def test_owner_revocation_is_not_reprepared_by_restart(self):
        runtime = self.installed()
        self.post(runtime, 'hello')
        runtime.membership.close(self.job_id, authorized=True)
        self.assertFalse(runtime.status()['prepared'])
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.post(runtime, 'status')
        self.assertEqual(403, rejected.exception.code)
        rejected.exception.close()
        with mock.patch.object(owner.CloudMembership, 'prepare', side_effect=AssertionError('reprepare')):
            self.assertEqual('owner_membership_revoked', self.runtime().start()['reason'])
        self.assertEqual('owner_membership_revoked',
            owner.install_owner(self.api, self.config, authorized=True)['reason'])

    def test_private_config_and_fixed_route_cannot_install_identity_or_callbacks(self):
        for change in ({'source': 'chatgpt-chat'}, {'operator_bearer': TOKEN}, {'lease_validator': True}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                owner.validate_config(dict(self.config, **change))
        changed = copy.deepcopy(self.config)
        changed['grant']['callback_url'] = 'https://mesh.example.test/v1/tasks'
        with self.assertRaises(ValueError):
            owner.validate_config(changed)
        changed = copy.deepcopy(self.config)
        changed['grant']['enrollment']['join']['authority_id'] = 'another-authority'
        with self.assertRaises(PermissionError):
            owner.install_owner(self.api, changed, authorized=True)
        path = self.root / 'private-config.json'
        path.write_text(json.dumps(self.config), encoding='utf8')
        os.chmod(str(path), 0o600)
        self.assertEqual(self.config, owner.load_config(str(path)))
        os.chmod(str(path), 0o644)
        with self.assertRaises(ValueError):
            owner.load_config(str(path))

    def test_runtime_source_uses_python36_grammar(self):
        source = Path(owner.__file__).read_text(encoding='utf8')
        if sys.version_info >= (3, 8):
            ast.parse(source, feature_version=(3, 6))
        else:
            ast.parse(source)


if __name__ == '__main__':
    unittest.main()
