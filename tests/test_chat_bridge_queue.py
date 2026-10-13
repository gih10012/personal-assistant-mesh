"""Private fixture/IPC/controlled DOM tests; no real Chat/browser or inference."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

from assistant_mesh import chat_bridge_queue as bridge
from assistant_mesh.store import Conflict


ROOT = Path(__file__).resolve().parents[1]
URL = 'https://chatgpt.com/c/fixture-conversation-1'


def config(path):
    return {'schema': 1, 'enabled': True, 'owner_id': 'fixture-owner', 'state_dir': str(path),
            'expected_account': 'fixture-owner@163.com', 'projects': {'fixture-project': URL},
            'dom': {'account_selector': '#fixture-account', 'account_attribute': 'textContent',
                    'completion_selector': '.fixture-completed'}}


def account_menu():
    return {'steps': [{'control_selector': '#fixture-profile-menu', 'menu_selector': '#fixture-account-menu', 'menu_role': 'menu'}],
            'current_selector': '[role="menuitemradio"][aria-checked="true"]',
            'selected_attribute': 'aria-checked', 'selected_value': 'true'}


def owner_profile():
    return {'control_selector': '#fixture-profile-menu', 'label_selector': '.fixture-profile-name',
            'expected_label': 'Fixture Owner',
            'account_sha256': hashlib.sha256(b'fixture-owner@163.com').hexdigest()}


def request(number=1, project='fixture-project', text='  合成无秘密任务 🐾\n\n'):
    return {'request_id': 'fixture-request-' + str(number), 'task_id': 'fixture-child-' + str(number),
            'project_id': project, 'input': text}


def observed(job, state='completed', text='  完整可见结果\n代码\n\n'):
    return {'state': state, 'reason': 'visible_terminal' if state == 'completed' else 'terminal_not_observed',
            'text': text, 'conversation_url': job['conversation_url'], 'account_sha256': job['account_sha256'],
            'user_message_id': 'fixture-user', 'assistant_message_id': 'fixture-assistant',
            'terminal_visible': state == 'completed', 'scope': 'visible_dom_text'}


class ChatQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-chat-queue.')
        self.path = Path(self.temp.name) / 'private'
        self.options = config(self.path)
        self.journal = bridge.ChatBridgeQueue(self.options, clock=lambda: 1234.5)

    def tearDown(self):
        self.temp.cleanup()

    def test_durable_original_prompt_before_any_send_and_private_permissions(self):
        receipt = self.journal.enqueue(request())
        self.assertFalse(receipt['send_intent'])
        reopened = bridge.ChatBridgeQueue(self.options)
        job = reopened.offers()[0]
        self.assertEqual('prepare', job['mode'])
        self.assertTrue(job['prompt'].endswith(request()['input']))
        self.assertNotIn('expected_account', job)
        self.assertEqual(0o700, self.path.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.journal.database.stat().st_mode & 0o777)

    def test_same_original_request_retry_and_content_conflict(self):
        self.assertEqual(self.journal.enqueue(request()), self.journal.enqueue(request()))
        for key, value in (('input', 'changed'), ('task_id', 'different'), ('request_id', 'new-for-same-child')):
            changed = dict(request(), **{key: value})
            with self.subTest(key=key), self.assertRaises(Conflict):
                self.journal.enqueue(changed)

    def test_one_grant_and_reopen_observation_only_after_lost_ack(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        first = self.journal.begin_send(job['request_id'], job['content_sha256'])
        self.assertTrue(first['send_granted'])
        reopened = bridge.ChatBridgeQueue(self.options)
        second = reopened.begin_send(job['request_id'], job['content_sha256'])
        self.assertFalse(second['send_granted'])
        self.assertEqual('observe', reopened.offers()[0]['mode'])
        self.assertEqual('unknown', reopened.status(job['request_id'])['state'])

    def test_concurrent_instances_only_one_grant(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        other = bridge.ChatBridgeQueue(self.options)
        start, results = threading.Barrier(3), []
        def attempt(owner):
            start.wait()
            results.append(owner.begin_send(job['request_id'], job['content_sha256'])['send_granted'])
        threads = [threading.Thread(target=attempt, args=(owner,)) for owner in (self.journal, other)]
        for item in threads:
            item.start()
        start.wait()
        for item in threads:
            item.join(5)
        self.assertCountEqual([True, False], results)

    def test_unknown_same_project_does_not_block_independent_project(self):
        options = dict(self.options, projects={'fixture-project': URL, 'another': 'https://chatgpt.com/c/fixture-conversation-2'})
        journal = bridge.ChatBridgeQueue(options)
        journal.enqueue(request())
        journal.enqueue(request(2))
        journal.enqueue(request(3, project='another'))
        self.assertEqual(['fixture-request-1', 'fixture-request-3'], [job['request_id'] for job in journal.offers()])
        first = journal.offers()[0]
        journal.begin_send(first['request_id'], first['content_sha256'])
        second_hash = journal.status('fixture-request-2')['content_sha256']
        with self.assertRaisesRegex(Conflict, 'project_turn_unresolved'):
            journal.begin_send('fixture-request-2', second_hash)

    def test_completed_opens_next_same_project_and_full_bytes_survive_reopen(self):
        self.journal.enqueue(request())
        self.journal.enqueue(request(2))
        job = self.journal.offers()[0]
        value = observed(job)
        self.journal.record(job['request_id'], job['content_sha256'], value)
        reopened = bridge.ChatBridgeQueue(self.options)
        self.assertEqual('fixture-request-2', reopened.offers()[0]['request_id'])
        result = reopened.result(job['request_id'])['result']
        self.assertEqual(value['text'], result['text'])
        self.assertEqual(hashlib.sha256(value['text'].encode()).hexdigest(), result['text_sha256'])
        self.assertEqual(len(value['text'].encode()), result['text_bytes'])
        self.assertFalse(reopened.result(job['request_id'])['execution_verified'])

    def test_completed_result_immutable_same_repeated_receipt_allowed(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        value = observed(job)
        self.journal.record(job['request_id'], job['content_sha256'], value)
        self.journal.record(job['request_id'], job['content_sha256'], value)
        for changed in (dict(value, text=value['text'].strip()), dict(value, state='unknown', terminal_visible=False)):
            with self.assertRaises(Conflict):
                self.journal.record(job['request_id'], job['content_sha256'], changed)

    def test_partial_is_not_completed_and_later_full_result_can_settle(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        receipt = self.journal.record(job['request_id'], job['content_sha256'], observed(job, state='partial', text='前半'))
        self.assertEqual('partial', receipt['state'])
        self.assertEqual('observe', self.journal.offers()[0]['mode'])
        self.assertFalse(self.journal.result(job['request_id'])['result']['terminal_visible'])
        self.journal.record(job['request_id'], job['content_sha256'], observed(job))
        self.assertEqual('completed', self.journal.status(job['request_id'])['state'])

    def test_known_preflight_block_keeps_unsent_original_request(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        value = observed(job, state='blocked', text='')
        receipt = self.journal.record(job['request_id'], job['content_sha256'], value)
        self.assertEqual('queued', receipt['state'])
        self.assertFalse(receipt['send_intent'])

    def test_browser_local_intent_lost_ack_converges_unknown_without_new_grant(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        self.journal.record(job['request_id'], job['content_sha256'], observed(job, state='unknown', text=''))
        self.assertFalse(self.journal.begin_send(job['request_id'], job['content_sha256'])['send_granted'])

    def test_completed_requires_terminal_and_exact_ids(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        for changed in (dict(observed(job), terminal_visible=False), dict(observed(job), user_message_id=''),
                        dict(observed(job), assistant_message_id=''), dict(observed(job), text='')):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.journal.record(job['request_id'], job['content_sha256'], changed)
        self.assertFalse(self.journal.status(job['request_id'])['send_intent'])

    def test_mismatched_account_conversation_hash_scope_and_extra_fields_rejected(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        for key, value in (('account_sha256', 'a' * 64), ('conversation_url', 'https://chatgpt.com/c/other-fixture'),
                           ('scope', 'full_hidden_transcript'), ('token', 'NOT_REAL_SECRET')):
            with self.subTest(key=key), self.assertRaises((ValueError, Conflict)):
                self.journal.record(job['request_id'], job['content_sha256'], dict(observed(job), **{key: value}))
        with self.assertRaises(Conflict):
            self.journal.begin_send(job['request_id'], 'f' * 64)

    def test_oversized_result_rejected_not_cropped(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        with self.assertRaises(ValueError):
            self.journal.record(job['request_id'], job['content_sha256'], observed(job, text='x' * (bridge.MAX_RESULT + 1)))
        self.assertFalse(self.journal.status(job['request_id'])['result_available'])

    def test_private_result_tamper_detected(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        self.journal.record(job['request_id'], job['content_sha256'], observed(job))
        with contextlib.closing(sqlite3.connect(str(self.journal.database))) as db:
            saved = json.loads(db.execute('SELECT result FROM chat_requests').fetchone()[0])
            saved['text'] += 'tamper'
            db.execute('UPDATE chat_requests SET result=?', (json.dumps(saved),))
            db.commit()
        with self.assertRaisesRegex(Conflict, 'saved_result_changed'):
            self.journal.result(job['request_id'])

    def test_namespace_changed_account_owner_or_conversation_is_not_adopted(self):
        for replacement in (dict(self.options, owner_id='another'),
                            dict(self.options, expected_account='another@163.com'),
                            dict(self.options, projects={'fixture-project': 'https://chatgpt.com/c/another-conversation'})):
            with self.subTest(config=replacement), self.assertRaises(Conflict):
                bridge.ChatBridgeQueue(replacement)

    def test_invalid_config_no_new_chat_wrong_account_and_override_fields(self):
        for replacement in (dict(self.options, expected_account='fixture@example.com'),
                            dict(self.options, projects={'fixture-project': 'https://chatgpt.com/'}),
                            dict(self.options, projects={'fixture-project': URL + '?temporary=true'}),
                            dict(self.options, dom={}), dict(self.options, token='NOT_REAL')):
            with self.subTest(config=replacement), self.assertRaises(ValueError):
                bridge.configuration(replacement)
        for field in ('conversation_url', 'account', 'model', 'shell', 'token', 'url',
                      'owner_confirmed_profile', 'expected_label', 'dom'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.journal.enqueue(dict(request(), **{field: 'not allowed'}))

    def test_disabled_enrollment_never_submits_but_saved_result_still_readable(self):
        self.journal.enqueue(request())
        job = self.journal.offers()[0]
        self.journal.record(job['request_id'], job['content_sha256'], observed(job))
        disabled = bridge.ChatBridgeQueue(dict(self.options, enabled=False))
        self.assertEqual([], disabled.offers())
        self.assertEqual('completed', disabled.result(job['request_id'])['state'])
        with self.assertRaises(PermissionError):
            disabled.enqueue(request(2))

    def test_one_conversation_cannot_create_competing_project_queues(self):
        with self.assertRaisesRegex(ValueError, 'single_project'):
            bridge.configuration(dict(self.options, projects={'one': URL, 'two': URL}))

    def test_account_menu_positive_contract_forwarded_not_inferred_from_email(self):
        options = dict(self.options, dom=dict(self.options['dom'], account_menu=account_menu()))
        journal = bridge.ChatBridgeQueue(options)
        journal.enqueue(request())
        self.assertEqual(account_menu(), journal.offers()[0]['dom']['account_menu'])
        self.assertIsNone(bridge.configuration(self.options)['dom']['account_menu'])

    def test_account_menu_guessing_email_text_or_general_clicks_rejected(self):
        for changed in (dict(account_menu(), selected_attribute='textContent', selected_value='Owner'),
                        dict(account_menu(), selected_attribute='aria-checked', selected_value='false'),
                        dict(account_menu(), steps=[]), dict(account_menu(), steps=[{'selector': 'a'}]),
                        dict(account_menu(), javascript='not allowed')):
            with self.subTest(contract=changed), self.assertRaises(ValueError):
                bridge.configuration(dict(self.options, dom=dict(self.options['dom'], account_menu=changed)))

    def test_owner_profile_only_config_reuses_original_namespace_and_request(self):
        self.journal.enqueue(request())
        original = self.journal.offers()[0]
        options = dict(self.options, dom={'completion_selector': '.fixture-completed',
                                         'owner_confirmed_profile': owner_profile()})
        journal = bridge.ChatBridgeQueue(options)
        offered = journal.offers()[0]
        self.assertEqual(self.journal.namespace, journal.namespace)
        self.assertEqual(original['content_sha256'], offered['content_sha256'])
        self.assertEqual(original['conversation_url'], offered['conversation_url'])
        self.assertEqual(owner_profile(), offered['dom']['owner_confirmed_profile'])
        self.assertNotIn('account_selector', offered['dom'])
        self.assertNotIn('expected_account', offered)
        self.assertIsNone(offered['dom']['account_menu'])
        self.assertFalse(offered['send_intent'])

    def test_owner_profile_and_legacy_menu_both_forwarded_without_namespace_change(self):
        options = dict(self.options, dom=dict(self.options['dom'], account_menu=account_menu(),
                                              owner_confirmed_profile=owner_profile()))
        journal = bridge.ChatBridgeQueue(options)
        journal.enqueue(request())
        offered = journal.offers()[0]
        self.assertEqual(account_menu(), offered['dom']['account_menu'])
        self.assertEqual(owner_profile(), offered['dom']['owner_confirmed_profile'])
        self.assertEqual(self.journal.namespace, journal.namespace)

    def test_owner_profile_invalid_or_unbound_private_contract_rejected(self):
        for changed in (dict(owner_profile(), account_sha256='a' * 64),
                        dict(owner_profile(), expected_label=' Fixture Owner'),
                        dict(owner_profile(), expected_label='Fixture\nOwner'),
                        dict(owner_profile(), expected_label=''),
                        dict(owner_profile(), control_selector=False),
                        dict(owner_profile(), label_selector='x' * 257),
                        dict(owner_profile(), confirmed=True),
                        {'expected_label': 'Fixture Owner'}):
            with self.subTest(contract=changed), self.assertRaises(ValueError):
                bridge.configuration(dict(self.options, dom=dict(self.options['dom'],
                                                                  owner_confirmed_profile=changed)))
        with self.assertRaisesRegex(ValueError, 'owner_profile_namespace_conflict'):
            bridge.configuration(dict(self.options, expected_account='another@163.com',
                                      dom=dict(self.options['dom'], owner_confirmed_profile=owner_profile())))

    def test_owner_profile_installation_never_regrants_original_unknown_send(self):
        self.journal.enqueue(request())
        original = self.journal.offers()[0]
        self.journal.begin_send(original['request_id'], original['content_sha256'])
        journal = bridge.ChatBridgeQueue(dict(self.options, dom={
            'completion_selector': '.fixture-completed', 'owner_confirmed_profile': owner_profile()}))
        offered = journal.offers()[0]
        self.assertEqual('observe', offered['mode'])
        self.assertEqual('unknown', offered['state'])
        self.assertFalse(journal.begin_send(original['request_id'], original['content_sha256'])['send_granted'])

    def test_world_readable_or_symlink_database_rejected_not_repaired(self):
        self.journal.database.chmod(0o644)
        with self.assertRaises(ValueError):
            self.journal.status('not-there')
        self.assertEqual(0o644, self.journal.database.stat().st_mode & 0o777)

    def test_no_browser_wake_is_false_not_task_failure(self):
        self.assertFalse(self.journal.wake())
        self.assertEqual('queued', self.journal.enqueue(request())['state'])


class FramesAndHostTests(unittest.TestCase):
    def test_native_frames_multiple_unicode_and_exact_bytes(self):
        output = io.BytesIO()
        values = [{'schema': 1, 'text': '  原文 🐾\n'}, {'schema': 1, 'event': 'another'}]
        for value in values:
            bridge.write_frame(output, value)
        incoming = io.BytesIO(output.getvalue())
        for value in values:
            self.assertEqual(value, bridge.read_frame(incoming))
        self.assertIsNone(bridge.read_frame(incoming))

    def test_bad_or_oversize_frames_do_not_get_decoded(self):
        for raw in (b'\x01', struct.pack('=I', 0), struct.pack('=I', bridge.MAX_FRAME + 1),
                    struct.pack('=I', 3) + b'{}', struct.pack('=I', 13) + b'{"a":1,"a":2}'):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                bridge.read_frame(io.BytesIO(raw))

    def test_real_private_unix_wake_long_port_one_grant_result_and_clean_exit(self):
        class Sink:
            def __init__(self):
                self.raw = bytearray()
                self.messages = queue.Queue()
            def write(self, raw):
                self.raw.extend(raw)
                return len(raw)
            def flush(self):
                incoming = io.BytesIO(bytes(self.raw))
                self.messages.put(bridge.read_frame(incoming))
                self.raw.clear()
        with tempfile.TemporaryDirectory(prefix='chat-event-host.') as folder:
            journal = bridge.ChatBridgeQueue(config(Path(folder) / 'private'))
            reader_fd, writer_fd = os.pipe()
            incoming, caller = os.fdopen(reader_fd, 'rb', buffering=0), os.fdopen(writer_fd, 'wb', buffering=0)
            sink, errors = Sink(), []
            def serve():
                try:
                    bridge.native_host(journal, incoming, sink)
                except BaseException as error:
                    errors.append(error)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            def reply(call):
                # Work offers may be repeated around startup/wake, but grants
                # and results must correlate exactly to their original call.
                for _ in range(8):
                    value = sink.messages.get(timeout=3)
                    if value.get('event') == 'reply' and value.get('call_id') == call:
                        return value
                    self.assertEqual('work', value['event'])
                    self.assertEqual('fixture-request-1', value['job']['request_id'])
                self.fail('original correlated reply not observed')
            try:
                self.assertEqual('hello', sink.messages.get(timeout=3)['event'])
                self.assertEqual(0o600, (journal.root / 'wake.sock').stat().st_mode & 0o777)
                journal.enqueue(request())
                offer = sink.messages.get(timeout=3)
                self.assertEqual('work', offer['event'])
                job = offer['job']
                frame = {'schema': 1, 'action': 'begin_send', 'call_id': 'first',
                         'request_id': job['request_id'], 'content_sha256': job['content_sha256']}
                bridge.write_frame(caller, frame)
                self.assertTrue(reply('first')['data']['send_granted'])
                bridge.write_frame(caller, dict(frame, call_id='again'))
                self.assertFalse(reply('again')['data']['send_granted'])
                bridge.write_frame(caller, dict(frame, action='report', call_id='result', observation=observed(job)))
                self.assertEqual('completed', reply('result')['data']['state'])
                self.assertEqual(observed(job)['text'], journal.result(job['request_id'])['result']['text'])
            finally:
                caller.close()
                thread.join(3)
                incoming.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertFalse((journal.root / 'wake.sock').exists())

    def test_package_only_owned_js_manifest_license_never_existing_manual_files(self):
        path = ROOT / 'browser/firefox-mesh/chat_bridge_package.py'
        spec = importlib.util.spec_from_file_location('chat_bridge_package', str(path))
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'collaborator.xpi'
            receipt = package.build(output)
            self.assertFalse(receipt['installed'])
            with zipfile.ZipFile(output) as archive:
                self.assertCountEqual(['manifest.json', 'chat_bridge_dom.js', 'chat_bridge_background.js', 'LICENSE'], archive.namelist())
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(bridge.EXTENSION_ID, manifest['browser_specific_settings']['gecko']['id'])
                self.assertNotIn('cookies', manifest['permissions'])
                self.assertEqual("script-src 'self'; object-src 'none'; connect-src 'none'", manifest['content_security_policy'])
            with self.assertRaises(FileExistsError):
                package.build(output)

    def test_example_native_host_rejects_wrong_extension_before_private_runtime_import(self):
        for arguments in ([], ['/fixture/native-manifest.json', 'foreign-extension@fixture']):
            result = subprocess.run([sys.executable, str(ROOT / 'browser/firefox-mesh/chat_bridge_host.py.example')] + arguments,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            self.assertEqual(1, result.returncode)
            self.assertEqual(b'', result.stdout)
            self.assertEqual(b'Chat native caller not allowed\n', result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'controlled DOM runner requires Node; no real Firefox test')
    def test_controlled_dom_and_owned_tab_cases(self):
        result = subprocess.run(['node', str(ROOT / 'browser/firefox-mesh/chat_bridge_test.js')],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertIn(b'controlled DOM/port cases passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
