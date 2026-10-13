"""Exact frozen old receiver, real IPC/threads/HTTP, synthetic iLink only.

No production ledger/credential is opened. A missing exact ancestor reports a
skip, never substituting today's Store/server. CI fetches full history.
"""
import ast
import copy
import hashlib
import importlib
import importlib.util
import io
import json
import os
import re
import select
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPOSITORY = Path(__file__).resolve().parents[1]
BASE_COMMIT = '3198c877234a8fd0c6912fdbe218f47185effedc'
OVERLAY_COMMIT = '1af08d2100c681e2b2a3b8442764318056b6fa76'


def _git(*arguments):
    result = subprocess.run(['git'] + list(arguments), cwd=str(REPOSITORY),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    if result.returncode:
        raise unittest.SkipTest('exact frozen receiver objects missing; set PAM_FENCED_RECEIVER_BASE_ROOT')
    return result.stdout


def _owner_overlay(source, patch):
    """Exact historical ready/lifecycle patch; verify final pinned bytes."""
    old = source.decode('utf8').splitlines(True)
    lines = [line for line in patch.decode('utf8').splitlines(True) if not line.startswith('index ')]
    if lines[:3] != ['diff --git a/assistant_mesh/server.py b/assistant_mesh/server.py\n',
                    '--- a/assistant_mesh/server.py\n', '+++ b/assistant_mesh/server.py\n']:
        raise AssertionError('original owner overlay target changed')
    cursor, index, output = 0, 3, []
    while index < len(lines):
        match = re.match(r'@@ -(\d+),(\d+) \+(\d+),(\d+) @@', lines[index])
        if match is None:
            raise AssertionError('original owner overlay invalid')
        before, old_count, after, new_count = map(int, match.groups())
        output.extend(old[cursor:before - 1])
        cursor, index, removed, added = before - 1, index + 1, 0, 0
        if len(output) != after - 1:
            raise AssertionError('original owner overlay offset changed')
        while index < len(lines) and not lines[index].startswith('@@ '):
            line = lines[index]
            if line[0] in (' ', '-'):
                if old[cursor] != line[1:]:
                    raise AssertionError('original owner overlay context changed')
                cursor, removed = cursor + 1, removed + 1
            if line[0] in (' ', '+'):
                output.append(line[1:])
                added += 1
            index += 1
        if (removed, added) != (old_count, new_count):
            raise AssertionError('original owner overlay count changed')
    output.extend(old[cursor:])
    return ''.join(output).encode('utf8')


def _source_base():
    provided = os.environ.get('PAM_FENCED_RECEIVER_BASE_ROOT')
    if provided:
        return {path.name: path.read_bytes() for path in
                (Path(provided) / 'assistant_mesh').glob('*.py')}
    with tarfile.open(fileobj=io.BytesIO(_git('archive', BASE_COMMIT, 'assistant_mesh'))) as archive:
        files = {Path(member.name).name: archive.extractfile(member).read()
                 for member in archive.getmembers() if member.isfile() and member.name.endswith('.py')}
    files['server.py'] = _owner_overlay(files['server.py'],
        _git('show', OVERLAY_COMMIT + ':deploy/legacy-owner-server.patch'))
    for name in ('channel.py', 'channel_carrier.py', 'channel_supervisor.py',
                 'legacy_channel_owner.py', 'routing.py', 'routing_context.py'):
        files[name] = _git('show', OVERLAY_COMMIT + ':assistant_mesh/' + name)
    return files


def _builder():
    spec = importlib.util.spec_from_file_location('_fenced_candidate_builder',
        str(REPOSITORY / 'deploy' / 'build-legacy-fenced-receiver.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Client:
    """Real loopback HTTP with no ambient proxy or model call."""
    def __init__(self, url, token):
        self.url, self.token = url, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, payload=None):
        request = urllib.request.Request(self.url + path,
            data=json.dumps(payload).encode('utf8') if payload is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=4) as response:
            return json.loads(response.read())


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(socket, 'SO_PEERCRED'),
                     'Linux owner IPC deployment adapter, not a Mesh protocol requirement')
class LegacyFencedReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_tmp = tempfile.TemporaryDirectory(prefix='mesh-fenced-source-')
        root = Path(cls.source_tmp.name)
        base, destination = root / 'base', root / 'candidate'
        (base / 'assistant_mesh').mkdir(parents=True)
        cls.base_files = _source_base()
        for name, content in cls.base_files.items():
            (base / 'assistant_mesh' / name).write_bytes(content)
        cls.builder = _builder()
        cls.report = cls.builder.build(base, destination)
        cls.destination = destination
        cls.alias = '_mesh_exact_fenced_receiver'
        package = types.ModuleType(cls.alias)
        package.__path__ = [str(destination / 'assistant_mesh')]
        package.__package__ = cls.alias
        sys.modules[cls.alias] = package
        cls.runtime = importlib.import_module(cls.alias + '.legacy_fenced_receiver')
        cls.server_module = importlib.import_module(cls.alias + '.server')
        cls.store_module = importlib.import_module(cls.alias + '.store')
        cls.carrier_module = importlib.import_module(cls.alias + '.channel_carrier')
        cls.state_module = importlib.import_module(cls.alias + '.channel_authority')

    @classmethod
    def tearDownClass(cls):
        for name in tuple(sys.modules):
            if name == cls.alias or name.startswith(cls.alias + '.'):
                sys.modules.pop(name, None)
        cls.source_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mesh-fenced-fixture-')
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.socket = self.root / 'control.sock'
        self.account = {'baseurl': 'https://fixture.weixin.qq.com', 'bot_token': 'synthetic-private-token',
                        'ilink_user_id': 'fixture-owner', 'ilink_bot_id': 'fixture-bot',
                        'pending': [{'DO_NOT_IMPORT': 'candidate-account-pending'}]}
        account = self.root / 'account.json'
        account.write_text(json.dumps(self.account))
        account.chmod(0o600)
        # Obtain the actual protocol constant rather than guessing the version.
        notify = importlib.import_module(self.alias + '.remote_notifications')
        self.binding = self.runtime.digest([notify.PROTOCOL,
            'https://fixture.weixin.qq.com', 'fixture-owner', 'fixture-bot'])
        self.tokens = {name: name + '-' + 'a' * 64 for name in ('cloud', 'laptop', 'operator')}
        peers = []
        for name, token in self.tokens.items():
            path = self.root / (name + '.token')
            path.write_text(token)
            path.chmod(0o600)
            peer = {'role': 'operator', 'token_file': str(path)}
            if name != 'operator':
                peer.update(role='channel_carrier', node=name, channel={
                    'owner_ref': 'fixture-self', 'canonical_authority': 'cloud', 'account_binding': self.binding})
            peers.append(peer)
        self.config = {'node_id': 'cloud', 'port': 0, 'database': str(self.root / 'original.sqlite'),
            'ilink_account': str(account), 'peers': peers, 'channel_carrier': {
                'witness_id': 'fixture-witness', 'owner_ref': 'fixture-self', 'transport_mode': 'disabled'}}
        self.http = self.api = self.channel = self.thread = self.group = None
        self.wire, self.errors, self.release = [], [], threading.Event()
        self.children = []

    def tearDown(self):
        self.release.set()
        for child in self.children:
            if child.poll() is None:
                child.terminate()  # only this synthetic fixture's owned child
                child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        if self.group:
            self.group.stop(3)
        if self.http:
            self.http.shutdown()
        if self.thread:
            self.thread.join(4)
            self.assertFalse(self.thread.is_alive())
        self.tmp.cleanup()

    @staticmethod
    def handoff(before, holder, proof_ref):
        # TRUSTED SYNTHETIC TEST ONLY; never a production verifier.
        return {'protocol': 'mesh-channel-handoff-proof/1',
            'account_binding': before['account_binding'], 'canonical_authority': before['canonical_authority'],
            'from_holder': before['holder'], 'from_epoch': before['epoch'], 'to_holder': holder,
            'proof_ref': proof_ref, 'checkpoint_revision': before['checkpoint_revision'] + 1,
            'checkpoint_sha256': hashlib.sha256(b'synthetic-closed-state').hexdigest(),
            'migration_ready': True, 'original_history_closed': True, 'original_execution_closed': True,
            'previous_carrier_stopped': True, 'external_operations_quiescent': True}

    def candidate(self, node='cloud'):
        return {'protocol': self.carrier_module.PROTOCOL, 'node': node, 'owner_ref': 'fixture-self',
            'canonical_authority': 'cloud', 'account_binding': self.binding,
            'private_credential_ref': 'synthetic-private-ref', 'revision': 1,
            'runtime': {'adapter': 'ilink/0.3.0', 'fence_protocol': self.carrier_module.PROTOCOL,
                        'transport_available': True, 'private_state': True}}

    def start(self, hook=None, handoff=None):
        ready = threading.Event()
        def prepared(api):
            def wire(endpoint, body, timeout):
                self.wire.append((endpoint, body))
                return {'msgs': [], 'get_updates_buf': 'next-cursor'} if endpoint == 'getupdates' else {'message_id': 'fixture'}
            api.ilink.request_override = wire
            if hook:
                hook(api)
        def connected(http, api, channel):
            self.http, self.api, self.channel = http, api, channel
            self.assertIsNot(self.server_module.API, type(api))
            self.assertIs(self.store_module.Store, self.server_module.Store)
            ready.set()
        def run():
            try:
                self.runtime.serve_with_fenced_owner_control(self.config, self.socket,
                    handoff_verifier=handoff, before_channel=prepared, ready=connected)
            except Exception as error:
                self.errors.append(error)
                ready.set()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(5))
        if self.errors:
            raise self.errors[0]
        url = 'http://127.0.0.1:' + str(self.http.server_address[1])
        self.clients = {name: Client(url, token) for name, token in self.tokens.items()}
        return self.runtime.request_owner(self.socket, expected_pid=os.getpid())

    def grant(self, node='laptop'):
        authority = self.api.channel_authority
        authority.witness.candidate(node, self.candidate(node), 300)
        term = authority.witness.inspect(self.binding)
        return authority.witness.grant(self.binding, term['epoch'], node, 60, 'synthetic-proof')

    def test_candidate_changes_only_two_incumbent_modules_and_no_native_ast(self):
        self.assertEqual(38, self.report['unchanged_original_modules'])
        self.assertEqual(['server.py', 'store.py'], self.report['changed_original_modules'])
        for name, content in self.base_files.items():
            actual = (self.destination / 'assistant_mesh' / name).read_bytes()
            if name not in ('store.py', 'server.py'):
                self.assertEqual(content, actual, name)
        old = ast.parse(self.base_files['store.py'].decode('utf8'))
        new = ast.parse((self.destination / 'assistant_mesh' / 'store.py').read_text())
        for tree in (old, new):
            store = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == 'Store')
            store.body = [item for item in store.body if not isinstance(item, ast.FunctionDef)
                          or item.name not in ('ingest', '_ingest_in_db')]
        self.assertEqual(ast.dump(old), ast.dump(new))

    def test_constructor_and_disabled_server_never_import_pending_or_grant(self):
        report = self.start()
        self.assertIsNone(self.channel)
        self.assertEqual([], self.wire)
        self.assertIsNone(self.api.store.get('cursor'))
        self.assertTrue(self.api.channel_authority.witness.inspect(self.binding)['frozen'])
        self.assertFalse(report['lifecycle']['started'])
        self.assertFalse(report['lifecycle']['local_stop_closed'])
        self.assertFalse(report['lifecycle']['handoff_authorized'])
        with self.assertRaisesRegex(self.carrier_module.CarrierError, 'carrier_handoff_verifier_missing'):
            self.grant()

    def test_original_noncarrier_routes_still_work_and_carrier_cannot_escape(self):
        self.start()
        self.assertIsInstance(self.clients['operator'].request('/v1/status'), dict)
        for path in ('/v1/status', '/v1/tasks', '/v1/resource/action', '/v1/claim',
                     '/channel-carrier/grant', '/channel-carrier/freeze', '/channel-carrier/reconcile'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.clients['laptop'].request(path, {})
            self.assertEqual(403, error.exception.code, path)
            error.exception.close()

    def test_carrier_alias_and_untrusted_json_hooks_rejected_before_store(self):
        self.config['peers'][0]['token_file'] = self.config['peers'][-1]['token_file']
        api = self.runtime.make_api_class(self.server_module.API)
        with self.assertRaisesRegex(ValueError, 'ambiguous_channel_carrier_credential'):
            api(self.config)
        self.assertFalse(Path(self.config['database']).exists())
        self.config['channel_carrier']['handoff_verifier'] = 'pretend-module.verify'
        with self.assertRaises(ValueError):
            api(self.config)
        self.assertFalse(Path(self.config['database']).exists())
        with self.assertRaisesRegex(self.runtime.OwnerControlError, 'owner_verifier_invalid'):
            self.runtime.make_api_class(self.server_module.API, handoff_verifier={'verified': True})

    def test_private_credential_change_between_original_reads_fails_closed(self):
        api = self.runtime.make_api_class(self.server_module.API)
        original_reader = self.server_module.read_secret
        with mock.patch.object(self.server_module, 'read_secret',
                side_effect=lambda path: 'different-' + original_reader(path)):
            with self.assertRaisesRegex(ValueError, 'channel_carrier_credential_changed_during_startup'):
                api(self.config)
        # It never reaches witness enrollment, let alone transport or grant.
        import sqlite3
        with sqlite3.connect(self.config['database']) as db:
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='channel_carrier_witness'").fetchone())
        self.assertEqual([], self.wire)

    def test_missing_term_fails_before_transport_and_restores_original_factories(self):
        self.config['channel_carrier']['transport_mode'] = 'fenced'
        originals = (self.server_module.API, self.server_module.Store, self.server_module._start_channel)
        with self.assertRaisesRegex(self.runtime.OwnerControlError, 'owner_local_term_not_granted'):
            self.start()
        self.assertEqual(originals, (self.server_module.API, self.server_module.Store, self.server_module._start_channel))
        self.assertEqual([], self.wire)
        self.assertFalse(self.socket.exists())

    def test_startup_preserves_original_submitting_outbox_and_steering(self):
        store = self.store_module.Store(self.config['database'])
        task = store.create_task('synthetic unrelated leader task', task_id='unrelated-original')
        store.enqueue('original-send', 'synthetic pending send')
        row = store.next_send()
        with store.transaction() as db:
            db.execute('INSERT INTO steering VALUES(?,?,?,?,?)',
                       ('original-steer', task, 1, 'synthetic steer', 'submitting'))
            before = [tuple(item) for item in db.execute('SELECT * FROM steering')]
        self.start()
        with self.api.store.transaction() as db:
            self.assertEqual(before, [tuple(item) for item in db.execute('SELECT * FROM steering')])
            after = dict(db.execute('SELECT * FROM outbox WHERE id=?', ('original-send',)).fetchone())
        self.assertEqual('submitting', after['status'])
        self.assertEqual(row['client_id'], after['client_id'])
        self.assertEqual([], self.wire)

    def test_real_remote_full_outcome_over_128k_commits_exact_original(self):
        self.start(hook=lambda api: api.store.set('cursor', 'original-cursor'), handoff=self.handoff)
        term = self.grant('laptop')
        state = self.state_module.RemoteChannelState(self.clients['laptop'], str(self.root / 'spool.sqlite'))
        message = {'message_id': 'fixture-original-large-batch-message',
            'from_user_id': 'fixture-owner', 'to_user_id': 'fixture-bot', 'message_type': 1,
            'create_time_ms': int(time.time() * 1000) + 1000,
            'context_token': 'synthetic-original-context',
            'item_list': [{'type': 1, 'text_item': {'text': 'synthetic original native task'}}]}
        batch = {'msgs': [message], 'get_updates_buf': 'large-next', 'padding': 'z' * 200000}
        self.api.ilink.request_override = lambda endpoint, body, timeout: batch
        channel = self.runtime.Channel(state, self.api.ilink,
            self.carrier_module.CarrierFence(self.clients['laptop'], term))
        self.assertEqual(1, channel.poll_once())
        self.assertEqual('large-next', self.api.store.get('cursor'))
        with self.api.store.transaction() as db:
            row = dict(db.execute('SELECT * FROM channel_canonical_intents').fetchone())
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0])
            tasks = [dict(item) for item in db.execute('SELECT * FROM tasks')]
        self.assertEqual(1, len(tasks))
        self.assertEqual('synthetic original native task', tasks[0]['input'])
        self.assertEqual(batch, json.loads(row['outcome'])['batch'])
        self.assertEqual(batch, state.spool(row['operation_id'])['outcome']['batch'])
        self.assertTrue(row['persistence_sha256'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.clients['operator'].request('/v1/tasks', {'input': 'x' * 200000})
        self.assertEqual(413, error.exception.code)
        error.exception.close()

    def test_distinct_remote_process_retains_original_authority_and_owned_ipc(self):
        self.start(hook=lambda api: api.store.set('cursor', 'original-cursor'), handoff=self.handoff)
        self.api.store.enqueue('original-remote-send', 'synthetic original body')
        with self.api.store.transaction() as db:
            original_client = db.execute('SELECT client_id FROM outbox WHERE id=?',
                                        ('original-remote-send',)).fetchone()[0]
        self.grant('laptop')
        child_code = '''
import json, os, sys
from assistant_mesh.channel import Channel, ILink
from assistant_mesh.channel_authority import RemoteChannelState
from assistant_mesh.channel_carrier import CarrierFence
from assistant_mesh.legacy_fenced_receiver import run_remote_with_owner_control
from assistant_mesh.worker import Client
client = Client({'control_url': sys.argv[1], 'token_file': sys.argv[2]})
term = client.request('/channel-carrier/inspect', {})
state = RemoteChannelState(client, sys.argv[4])
wire = []
def transport(endpoint, body, timeout):
    wire.append(endpoint)
    if endpoint == 'getupdates':
        sys.stdin.readline()
        return {'msgs': [], 'get_updates_buf': 'remote-original-next'}
    return {'message_id': 'synthetic-remote-accept'}
channel = Channel(state, ILink(sys.argv[3], request=transport), CarrierFence(client, term))
owned = []
def ready(control, group):
    owned.append(group)
    print(json.dumps({'pid': os.getpid(), 'instance_id': control.instance_id}), flush=True)
run_remote_with_owner_control(channel, sys.argv[5], ready=ready, renew_interval=0.05)
print(json.dumps({'lifecycle': owned[0].snapshot(), 'wire': wire}), flush=True)
'''
        environment = dict(os.environ, PYTHONPATH=str(self.destination),
                           HTTP_PROXY='', HTTPS_PROXY='', ALL_PROXY='',
                           NO_PROXY='127.0.0.1,localhost')
        remote_socket = self.root / 'remote.sock'
        child = subprocess.Popen([sys.executable, '-c', child_code,
            self.clients['laptop'].url, str(self.root / 'laptop.token'), str(self.root / 'account.json'),
            str(self.root / 'remote-spool.sqlite'), str(remote_socket)],
            cwd=str(self.root), env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(child)
        self.assertTrue(select.select([child.stdout], [], [], 6)[0], 'remote fixture readiness timed out')
        report = json.loads(child.stdout.readline())
        self.assertEqual(child.pid, report['pid'])
        self.assertNotEqual(os.getpid(), report['pid'])
        # Observe the same held callback from its actual separate socket peer.
        deadline = time.monotonic() + 3
        while True:
            observed = self.runtime.request_owner(remote_socket, expected_pid=child.pid)
            if observed['lifecycle']['loop_calls']['poll']['active']:
                break
            self.assertLess(time.monotonic(), deadline, observed['lifecycle'])
            threading.Event().wait(0.02)
        stopped = self.runtime.request_owner(remote_socket, 'stop', timeout=0,
            instance_id=report['instance_id'], expected_pid=child.pid)
        self.assertFalse(stopped['lifecycle']['local_stop_closed'])
        self.assertFalse(stopped['lifecycle']['handoff_authorized'])
        child.stdin.write(b'finish original synthetic callback\n')
        child.stdin.flush()
        output, error = child.communicate(timeout=7)
        self.assertEqual(0, child.returncode, error.decode('utf8', errors='replace'))
        ended = json.loads(output)
        self.assertTrue(ended['lifecycle']['local_stop_closed'])
        self.assertEqual(1, ended['wire'].count('getupdates'))
        self.assertLessEqual(ended['wire'].count('sendmessage'), 1)
        self.assertEqual('remote-original-next', self.api.store.get('cursor'))
        with self.api.store.transaction() as db:
            row = dict(db.execute('SELECT * FROM outbox WHERE id=?', ('original-remote-send',)).fetchone())
        self.assertEqual(original_client, row['client_id'])
        self.assertIn(row['status'], ('pending', 'accepted'))
        self.assertTrue(self.clients['operator'].request('/v1/status'))

    def test_fenced_real_group_stop_waits_for_full_commit_and_never_grants(self):
        self.config['channel_carrier']['transport_mode'] = 'fenced'
        self.config['channel_carrier']['supervisor'] = {
            'renew_seconds': 60, 'renew_interval': 0.05,
            'candidate_ttl': 60, 'candidate_contract': self.candidate('cloud')}
        entered = threading.Event()
        def prepare(api):
            api.store.set('cursor', 'original-cursor')
            api.store.enqueue('original-send', 'synthetic body')
            authority = api.channel_authority
            authority.witness.candidate('cloud', self.candidate(), 300)
            authority.witness.grant(self.binding, 0, 'cloud', 60, 'synthetic-proof')
            original = authority.state.channel_commit_send
            def held(*args, **kwargs):
                entered.set()
                if not self.release.wait(4):
                    raise RuntimeError('synthetic held commit deadline')
                return original(*args, **kwargs)
            authority.state.channel_commit_send = held
        report = self.start(hook=prepare, handoff=self.handoff)
        self.group = self.channel.loop_lifecycle
        self.assertTrue(entered.wait(3), self.group.snapshot())
        handles = dict(self.group._threads)
        unclosed = self.runtime.request_owner(self.socket, 'stop', instance_id=report['instance_id'],
                                             timeout=0, expected_pid=os.getpid())
        self.assertFalse(unclosed['lifecycle']['local_stop_closed'])
        self.assertTrue(unclosed['lifecycle']['loop_calls']['send']['active'])
        self.release.set()
        ended = self.runtime.request_owner(self.socket, 'stop', instance_id=report['instance_id'],
                                           timeout=2, expected_pid=os.getpid())
        self.assertEqual(handles, self.group._threads)
        self.assertTrue(ended['lifecycle']['actual_owned_thread_handles'])
        self.assertTrue(ended['lifecycle']['local_stop_closed'])
        self.assertEqual(['maintenance', 'poll', 'send'], ended['lifecycle']['loop_returns_observed'])
        for field in ('external_operation_quiescence_verified', 'canonical_channel_state_closed_verified',
                      'handoff_authorized', 'native_features_restricted'):
            self.assertFalse(ended['lifecycle'][field])
        self.assertEqual(1, sum(endpoint == 'sendmessage' for endpoint, body in self.wire))
        self.assertTrue(self.clients['operator'].request('/v1/status'))

    def test_partial_start_and_unstarted_supervisor_never_report_closure(self):
        self.start(hook=lambda api: api.store.set('cursor', 'original-cursor'), handoff=self.handoff)
        term = self.grant('cloud')
        channel = self.runtime.Channel(self.api.channel_authority.state, self.api.ilink,
            self.carrier_module.CarrierFence(self.api.channel_authority.bound_client('cloud'), term))
        group = self.runtime.OwnedFencedSupervisor(channel)
        self.group = group
        self.assertFalse(group.snapshot()['owned_threads_stopped'])
        real_start = threading.Thread.start
        def fail_send(thread):
            if thread.name == 'mesh-channel-send':
                raise RuntimeError('synthetic partial thread start')
            return real_start(thread)
        with mock.patch.object(threading.Thread, 'start', fail_send):
            with self.assertRaisesRegex(self.runtime.OwnerControlError, 'owner_fenced_group_start_failed'):
                group.start()
        group.stop(1)
        self.assertTrue(group.snapshot()['start_failed'])
        self.assertFalse(group.snapshot()['actual_owned_thread_handles'])
        self.assertFalse(group.snapshot()['local_stop_closed'])
        with self.assertRaisesRegex(self.runtime.OwnerControlError, 'owner_fenced_group_already_started'):
            group.start()

    def test_old_legacy_ipc_refuses_fenced_and_stale_instance_cannot_stop(self):
        self.config['channel_carrier']['transport_mode'] = 'fenced'
        def prepare(api):
            api.store.set('cursor', 'original-cursor')
            api.channel_authority.witness.candidate('cloud', self.candidate(), 300)
            api.channel_authority.witness.grant(self.binding, 0, 'cloud', 60, 'synthetic-proof')
        report = self.start(hook=prepare, handoff=self.handoff)
        self.group = self.channel.loop_lifecycle
        with self.assertRaisesRegex(self.runtime.OwnerControlError, 'owner_request_rejected'):
            self.runtime.request_owner(self.socket, 'stop', instance_id='0' * 32, timeout=0)
        self.assertFalse(self.channel.stop.is_set())
        legacy = importlib.import_module(self.alias + '.legacy_channel_owner')
        control = legacy.LegacyChannelOwnerControl(self.root / 'other.sock')
        try:
            with self.assertRaisesRegex(self.runtime.OwnerControlError, 'owner_legacy_group_unavailable'):
                control.attach(self.http, self.api, self.channel)
        finally:
            control.close()
        self.assertFalse(report['lifecycle']['handoff_authorized'])


if __name__ == '__main__':
    unittest.main()
