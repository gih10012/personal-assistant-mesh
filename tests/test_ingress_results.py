"""Private local ledger fixtures, not an OAuth or public-artifact verification."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from assistant_mesh.ingress import Ingress
from assistant_mesh.ingress_results import IngressResults, validate_publication
from assistant_mesh.store import Conflict, Store


class IngressResultsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-ingress-results.')
        self.store = Store(Path(self.temporary.name) / 'authority.sqlite', clock=lambda: 1000.0)
        self.config = {'owner_id': 'owner-fixture'}
        self.ingress = Ingress(self.store, 'authority-fixture', self.config)
        self.results = IngressResults(self.store, 'authority-fixture', self.config)
        self.peer = {'role': 'ingress', 'ingress': {
            'owner_id': 'owner-fixture', 'source': 'fixture-chat', 'subject': 'subject-a',
            'scopes': ['tasks:submit', 'tasks:read']}}
        self.operator = {'role': 'operator'}
        receipt = self.ingress.submit(self.peer, {'request_id': 'message-1', 'input': '开放任务'})
        self.task_id = receipt['task_id']
        self.store.heartbeat('fixture-worker', ['leader'])
        claimed = self.store.claim('fixture-worker')
        self.assertEqual(self.task_id, claimed['id'])
        self.raw_result = 'PRIVATE_RAW_RESULT: bearer PRIVATE_ACCOUNT_TOKEN'
        self.store.update_task(self.task_id, 'fixture-worker', claimed['epoch'],
            result=self.raw_result, status='completed', checkpoint={'thread_id': 'PRIVATE_NATIVE_THREAD'})
        self.payload = {'publication_id': 'publication-1', 'source': 'fixture-chat',
            'subject': 'subject-a', 'request_id': 'message-1', 'task_id': self.task_id,
            'task_epoch': claimed['epoch'],
            'task_result_sha256': hashlib.sha256(self.raw_result.encode()).hexdigest(),
            'reviewed': True, 'result': {'summary': '已完成目标，以下是经 owner 审查的有限摘要。',
                'artifacts': [{'url': 'https://github.com/example-owner/example-repo/blob/fixed/report.md',
                               'label': '公开成果引用', 'declared_sha256': '0' * 64}]}}

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, payload=None, peer=None):
        return self.results.publish(self.operator if peer is None else peer,
                                    self.payload if payload is None else payload)

    def read(self, peer=None, payload=None):
        return self.results.read(self.peer if peer is None else peer,
                                 {'request_id': 'message-1'} if payload is None else payload)

    def snapshot(self, exclude_publications=False):
        with self.store.transaction() as db:
            names = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
                    for name in names if not exclude_publications or name != 'ingress_result_publications'}

    def change_task(self, field, value):
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET ' + field + '=? WHERE id=?', (value, self.task_id))

    def test_publish_separate_owner_reviewed_summary_not_private_source_result(self):
        before = self.snapshot(exclude_publications=True)
        receipt = self.publish()
        self.assertTrue(receipt['publication_created'])
        self.assertTrue(receipt['published'])
        self.assertEqual(self.payload['result'], receipt['result'])
        self.assertTrue(receipt['task_binding_verified'])
        self.assertEqual('authenticated_owner_approval', receipt['review_verification'])
        self.assertFalse(receipt['execution_verified'])
        self.assertFalse(receipt['artifact_content_verified'])
        self.assertFalse(receipt['native_tools_intercepted'])
        self.assertNotIn('PRIVATE_', json.dumps(receipt))
        for key in ('subject', 'source', 'task_epoch', 'task_result_sha256', 'node', 'checkpoint', 'native'):
            self.assertNotIn(key, receipt)
        self.assertEqual(before, self.snapshot(exclude_publications=True))
        self.assertEqual(receipt['result'], self.read()['result'])

    def test_plain_sha_is_only_declared_and_never_causes_http_or_file_lookup(self):
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('must not fetch')):
            with mock.patch('builtins.open', side_effect=AssertionError('must not open')):
                receipt = self.publish()
                value = self.read()
        self.assertEqual('0' * 64, value['result']['artifacts'][0]['declared_sha256'])
        self.assertFalse(receipt['artifact_content_verified'])
        self.assertFalse(value['artifact_content_verified'])

    def test_same_publication_content_retry_readonly_and_no_effect_replay(self):
        first = self.publish()
        before = self.snapshot()
        retry = self.publish(dict(reversed(list(self.payload.items()))))
        self.assertFalse(retry['publication_created'])
        self.assertEqual(first['publication_id'], retry['publication_id'])
        self.assertEqual(before, self.snapshot())
        self.read()
        self.assertEqual(before, self.snapshot())

    def test_optional_empty_artifact_list_has_same_meaning(self):
        payload = copy.deepcopy(self.payload)
        payload['result'] = {'summary': 'summary only'}
        self.publish(payload)
        retry = copy.deepcopy(payload)
        retry['result']['artifacts'] = []
        self.assertFalse(self.publish(retry)['publication_created'])

    def test_same_publication_id_changed_body_ref_label_or_digest_conflicts(self):
        self.publish()
        before = self.snapshot()
        for field, value in (('summary', 'different summary'), ('url', 'https://github.com/another/file'),
                             ('label', 'different label'), ('declared_sha256', '1' * 64)):
            changed = copy.deepcopy(self.payload)
            if field == 'summary':
                changed['result']['summary'] = value
            else:
                changed['result']['artifacts'][0][field] = value
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.publish(changed)
        self.assertEqual(before, self.snapshot())

    def test_different_publication_id_cannot_replace_existing_request_result(self):
        self.publish()
        before = self.snapshot()
        with self.assertRaisesRegex(Conflict, '^ingress_request_already_published$'):
            self.publish(dict(self.payload, publication_id='replacement-id'))
        self.assertEqual(before, self.snapshot())

    def test_operator_only_publish_not_client_reviewed_true_authority(self):
        before = self.snapshot()
        for peer in (self.peer, {'role': 'viewer'}, {'role': 'worker', 'node': 'fixture-worker'},
                     {'role': 'agent_peer', 'node': 'fixture-worker'}, None, {},
                     dict(self.peer, role='ingress', reviewed=True)):
            with self.subTest(peer=peer), self.assertRaises(PermissionError):
                self.results.publish(peer, self.payload)
        self.assertEqual(before, self.snapshot())
        for reviewed in (False, None, 1, 'true'):
            with self.subTest(reviewed=reviewed), self.assertRaises(PermissionError):
                self.publish(dict(self.payload, reviewed=reviewed))

    def test_payload_cannot_add_private_fields_or_verification_assertions(self):
        for key in ('owner_id', 'actor', 'role', 'token', 'local_path', 'checkpoint', 'verified'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.publish(dict(self.payload, **{key: 'forbidden'}))
        for key in ('raw_result', 'credential', 'path', 'native', 'execution_verified'):
            payload = copy.deepcopy(self.payload)
            payload['result'][key] = 'forbidden'
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.publish(payload)
        for key in ('path', 'token', 'sha256', 'verified', 'content'):
            payload = copy.deepcopy(self.payload)
            payload['result']['artifacts'][0][key] = 'forbidden'
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.publish(payload)

    def test_actual_task_and_result_digest_must_match_not_declared_sha_only(self):
        before = self.snapshot()
        for change in ({'task_id': 'unrelated-global-task'}, {'task_epoch': 2},
                       {'task_result_sha256': '0' * 64}):
            with self.subTest(change=change), self.assertRaises(Conflict):
                self.publish(dict(self.payload, **change))
        self.assertEqual(before, self.snapshot())

    def test_wrong_subject_source_request_does_not_select_global_task(self):
        before = self.snapshot()
        for field, value in (('source', 'other-source'), ('subject', 'other-subject'), ('request_id', 'missing')):
            with self.subTest(field=field), self.assertRaises(PermissionError):
                self.publish(dict(self.payload, **{field: value}))
        self.assertEqual(before, self.snapshot())

    def test_pending_running_failed_needs_review_paused_task_cannot_publish(self):
        for status in ('pending', 'running', 'failed', 'needs_review', 'paused', 'waiting_remote'):
            self.change_task('status', status)
            before = self.snapshot()
            with self.subTest(status=status), self.assertRaises(Conflict):
                self.publish()
            self.assertEqual(before, self.snapshot())

    def test_completed_but_never_claimed_missing_node_or_result_cannot_publish(self):
        for field, value in (('attempts', 0), ('epoch', 0), ('node', None), ('result', None), ('result', '')):
            with self.store.transaction() as db:
                db.execute('''UPDATE tasks SET attempts=1,epoch=1,node='fixture-worker',result=?
                    WHERE id=?''', (self.raw_result, self.task_id))
            self.change_task(field, value)
            before = self.snapshot()
            with self.subTest(field=field, value=value), self.assertRaises(Conflict):
                self.publish()
            self.assertEqual(before, self.snapshot())

    def test_missing_task_or_ingress_mapping_does_not_recreate_anything(self):
        with self.store.transaction() as db:
            db.execute('DELETE FROM tasks WHERE id=?', (self.task_id,))
        before = self.snapshot()
        with self.assertRaises(Conflict):
            self.publish()
        self.assertEqual(before, self.snapshot())
        with self.store.transaction() as db:
            db.execute('DELETE FROM ingress_requests')
        before = self.snapshot()
        with self.assertRaises(PermissionError):
            self.publish()
        self.assertEqual(before, self.snapshot())

    def test_changed_task_status_after_publish_denies_read_and_same_id_retry(self):
        self.publish()
        self.change_task('status', 'needs_review')
        before = self.snapshot()
        with self.assertRaises(Conflict):
            self.read()
        with self.assertRaises(Conflict):
            self.publish()
        self.assertEqual(before, self.snapshot())

    def test_changed_source_result_epoch_attempts_or_node_after_publish_denies_read(self):
        self.publish()
        for field, value in (('result', 'different private source'), ('epoch', 2),
                             ('attempts', 2), ('node', 'other-worker')):
            with self.store.transaction() as db:
                db.execute('''UPDATE tasks SET attempts=1,epoch=1,node='fixture-worker',result=?
                    WHERE id=?''', (self.raw_result, self.task_id))
            self.change_task(field, value)
            before = self.snapshot()
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.read()
            self.assertEqual(before, self.snapshot())

    def test_read_requires_own_subject_source_and_read_scope_no_global_lookup(self):
        self.publish()
        for field, value in (('subject', 'other-subject'), ('source', 'other-source'),
                             ('owner_id', 'other-owner')):
            peer = copy.deepcopy(self.peer)
            peer['ingress'][field] = value
            with self.subTest(field=field), self.assertRaises(PermissionError):
                self.read(peer=peer)
        for scopes in ([], ['tasks:submit']):
            peer = copy.deepcopy(self.peer)
            peer['ingress']['scopes'] = scopes
            with self.subTest(scopes=scopes), self.assertRaises(PermissionError):
                self.read(peer=peer)
        for payload in ({'task_id': self.task_id}, {'publication_id': 'publication-1'},
                        {'request_id': 'message-1', 'subject': 'subject-a'}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.read(payload=payload)
        for peer in (self.operator, {'role': 'worker', 'node': 'fixture-worker'}):
            with self.subTest(peer=peer), self.assertRaises(PermissionError):
                self.read(peer=peer)

    def test_absent_unpublished_and_foreign_read_same_permission_verdict(self):
        with self.assertRaisesRegex(PermissionError, '^ingress_result_not_visible$'):
            self.read()
        with self.assertRaisesRegex(PermissionError, '^ingress_result_not_visible$'):
            self.read(payload={'request_id': 'missing'})
        self.publish()
        peer = copy.deepcopy(self.peer)
        peer['ingress']['subject'] = 'foreign'
        with self.assertRaisesRegex(PermissionError, '^ingress_result_not_visible$'):
            self.read(peer=peer)

    def test_token_rotation_same_identity_readonly_can_read_approved_summary(self):
        self.publish()
        peer = copy.deepcopy(self.peer)
        peer['token_file'] = '/fixture/private/rotated-token'
        peer['ingress']['scopes'] = ['tasks:read']
        self.assertEqual(self.payload['result'], self.read(peer=peer)['result'])

    def test_publication_sql_failure_leaves_task_result_effects_and_rows_unchanged(self):
        with self.store.transaction() as db:
            db.execute('''CREATE TRIGGER reject_result BEFORE INSERT ON ingress_result_publications
                BEGIN SELECT RAISE(ABORT,'fixture publish failure'); END''')
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.publish()
        self.assertEqual(before, self.snapshot())

    def test_second_readback_failure_rolls_back_publication_insert(self):
        before = self.snapshot()
        with mock.patch.object(self.results, '_view', side_effect=Conflict('fixture anchor change')):
            with self.assertRaises(Conflict):
                self.publish()
        self.assertEqual(before, self.snapshot())

    def test_concurrent_duplicate_two_instances_commit_exactly_one_publication(self):
        other_store = Store(self.store.path, clock=lambda: 1000.0, recover_inflight=False)
        other = IngressResults(other_store, 'authority-fixture', self.config)
        barrier = threading.Barrier(2)
        def publish(results):
            barrier.wait(timeout=10)
            return results.publish(dict(self.operator), copy.deepcopy(self.payload))
        with ThreadPoolExecutor(max_workers=2) as executor:
            rows = list(executor.map(publish, (self.results, other)))
        self.assertEqual([False, True], sorted(row['publication_created'] for row in rows))
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM ingress_result_publications').fetchone()[0])

    def test_restart_preserves_published_record_without_model_or_effect_replay(self):
        self.publish()
        before = self.snapshot()
        reopened = IngressResults(Store(self.store.path, recover_inflight=False), 'authority-fixture', self.config)
        self.assertFalse(reopened.publish(self.operator, self.payload)['publication_created'])
        self.assertEqual(self.payload['result'], reopened.read(self.peer, {'request_id': 'message-1'})['result'])
        self.assertEqual(before, self.snapshot())

    def test_namespace_missing_or_changed_rejected_before_result_table_creation(self):
        for authority, config in (('other-authority', self.config),
                                  ('authority-fixture', {'owner_id': 'other-owner'})):
            before = self.snapshot()
            with self.subTest(authority=authority), self.assertRaises(Conflict):
                IngressResults(self.store, authority, config)
            self.assertEqual(before, self.snapshot())
        other_store = Store(Path(self.temporary.name) / 'without-ingress.sqlite', recover_inflight=False)
        with self.assertRaises(Conflict):
            IngressResults(other_store, 'authority-fixture', self.config)
        with other_store.transaction() as db:
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name='ingress_result_publications'").fetchone())

    def test_namespace_rechecked_on_publish_and_read_not_only_constructor(self):
        self.publish()
        for table in ('ingress_metadata', 'ingress_result_metadata'):
            with self.store.transaction() as db:
                db.execute("UPDATE ingress_metadata SET value='owner-fixture' WHERE key='owner'")
                db.execute("UPDATE " + table + " SET value='other-owner' WHERE key='owner'")
            before = self.snapshot()
            with self.subTest(table=table), self.assertRaises(Conflict):
                self.read()
            with self.subTest(table=table), self.assertRaises(Conflict):
                self.publish()
            self.assertEqual(before, self.snapshot())

    def test_corrupt_immutable_body_fingerprint_or_anchor_fails_closed(self):
        self.publish()
        with self.store.transaction() as db:
            original = dict(db.execute('SELECT * FROM ingress_result_publications').fetchone())
        for field, value in (('body', '{}'), ('fingerprint', '0' * 64), ('anchor', '{}')):
            with self.store.transaction() as db:
                db.execute('UPDATE ingress_result_publications SET body=?,fingerprint=?,anchor=?',
                           (original['body'], original['fingerprint'], original['anchor']))
                db.execute('UPDATE ingress_result_publications SET ' + field + '=?', (value,))
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.read()

    def test_reference_rejects_local_schemes_credentials_queries_private_ips_and_hosts(self):
        urls = ['file:///home/owner/private.json', '/home/owner/private.json', 'C:\\private\\result',
                'http://github.com/file', 'data:text/plain,private', 'https://user:secret@github.com/file',
                'https://github.com/file?token=secret', 'https://github.com/file#secret',
                'https://localhost/file', 'https://localhost./file', 'https://node.internal/file',
                'https://127.0.0.1/file', 'https://127.1/file', 'https://10.0.0.1/file',
                'https://0x7f.0.0.1/file', 'https://0177.0.0.1/file',
                'https://169.254.169.254/file', 'https://[::1]/file', 'https://[fe80::1]/file',
                'https://[::ffff:10.0.0.1]/file',
                'https://224.0.0.1/file', 'https://github.com:8080/file',
                'https://github.com/a%0ab', 'https://github.com/with space', 'https://github.com\\private']
        before = self.snapshot()
        for url in urls:
            payload = copy.deepcopy(self.payload)
            payload['result']['artifacts'][0]['url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.publish(payload)
        self.assertEqual(before, self.snapshot())

    def test_obvious_bearer_privatekey_or_api_key_not_publishable_text(self):
        for secret in ('Bearer abcdefghijklmnop', '-----BEGIN RSA PRIVATE KEY-----',
                       'sk-proj-' + 'a' * 40, 'sk-' + 'b' * 40):
            for field in ('summary', 'label'):
                payload = copy.deepcopy(self.payload)
                if field == 'summary':
                    payload['result']['summary'] = secret
                else:
                    payload['result']['artifacts'][0]['label'] = secret
                with self.subTest(secret_type=field), self.assertRaises(ValueError):
                    self.publish(payload)
        with self.assertRaises(ValueError):
            self.publish(dict(self.payload, publication_id='Bearer abcdefghijklmnop'))

    def test_source_size_bound_refuses_publication_without_truncating_or_rewriting(self):
        before = self.snapshot()
        with mock.patch('assistant_mesh.ingress_results.MAX_SOURCE_BYTES', 4):
            with self.assertRaises(Conflict):
                self.publish()
        self.assertEqual(before, self.snapshot())

    def test_bounded_strict_publication_types_unicode_and_unknown_nested_keys(self):
        invalid = [dict(self.payload, task_epoch=True), dict(self.payload, task_epoch=0),
                   dict(self.payload, task_result_sha256='not-a-hash'),
                   dict(self.payload, publication_id='\ud800')]
        for result in ({'summary': ''}, {'summary': '\ud800'}, {'summary': 'x' * 4097},
                       {'summary': 'valid', 'artifacts': [self.payload['result']['artifacts'][0]] * 9},
                       {'summary': 'valid', 'artifacts': None}, {'summary': 'valid', 'verified': True}):
            invalid.append(dict(self.payload, result=result))
        before = self.snapshot()
        for payload in invalid:
            with self.subTest(result_type=type(payload['result']).__name__), self.assertRaises(ValueError):
                self.publish(payload)
        self.assertEqual(before, self.snapshot())
        exact = copy.deepcopy(self.payload)
        exact['result']['summary'] = 'x' * 4096
        self.assertTrue(self.publish(exact)['publication_created'])

    def test_content_validation_is_pure_detached_and_does_not_claim_independent_review(self):
        payload = copy.deepcopy(self.payload)
        result = validate_publication(payload)
        result['result']['artifacts'].clear()
        self.assertEqual(1, len(payload['result']['artifacts']))
        self.assertTrue(result['reviewed'])
        self.assertNotIn('artifact_content_verified', result)

    def test_unauthorized_role_rejected_before_payload_or_database_access(self):
        with mock.patch.object(self.store, 'transaction', side_effect=AssertionError('must not open')):
            for peer in ({'role': 'ingress'}, {'role': 'worker'}, None):
                with self.subTest(peer=peer), self.assertRaises(PermissionError):
                    self.results.publish(peer, None)
            with self.assertRaises(PermissionError):
                self.results.read(self.operator, None)


if __name__ == '__main__':
    unittest.main()
