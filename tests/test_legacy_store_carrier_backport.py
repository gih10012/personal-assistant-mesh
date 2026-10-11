"""Exact original-3198 Store plus the opt-in channel facade, never production.

No current Store is substituted when the original source is unavailable. A
source archive may explicitly provide PAM_LEGACY_BASE_ROOT; a shallow checkout
without that source reports a skip rather than a false compatibility pass.
"""
import ast
import hashlib
import os
import re
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import test_channel_state as state_tests


BASE_COMMIT = '3198c877234a8fd0c6912fdbe218f47185effedc'
BASE_SHA256 = '7d8f750e8dfe10045ca97b52ac0ff853d645ea66b4b01f9a9aaf60fb48df5440'
PATCHED_SHA256 = '081633994459ffd2fb253e5514ea398d011b0eb8adcf059b251bbde10a705f08'
REPOSITORY = Path(__file__).resolve().parents[1]
PATCH_PATH = REPOSITORY / 'deploy' / 'legacy-store-carrier.patch'


def _original_source():
    provided = os.environ.get('PAM_LEGACY_BASE_ROOT')
    if provided:
        # Explicitly supplied immutable source, not a production ledger path.
        data = (Path(provided) / 'assistant_mesh' / 'store.py').read_bytes()
    else:
        try:
            result = subprocess.run(['git', 'show', BASE_COMMIT + ':assistant_mesh/store.py'],
                cwd=str(REPOSITORY), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise unittest.SkipTest('exact original3198 source unavailable; set PAM_LEGACY_BASE_ROOT')
        if result.returncode:
            raise unittest.SkipTest('original3198 git object unavailable; fetch history or set PAM_LEGACY_BASE_ROOT')
        data = result.stdout
    if hashlib.sha256(data).hexdigest() != BASE_SHA256:
        raise AssertionError('original3198 Store source hash mismatch; no substitute allowed')
    return data.decode('utf8')


def _patched_source(source):
    """Apply the published unified patch with exact line/context checks."""
    original = source.splitlines(True)
    lines = PATCH_PATH.read_text().splitlines(True)
    if lines[:3] != [
            'diff --git a/assistant_mesh/store.py b/assistant_mesh/store.py\n',
            '--- a/assistant_mesh/store.py\n', '+++ b/assistant_mesh/store.py\n']:
        raise AssertionError('patch target changed')
    output, cursor, index, hunks = [], 0, 3, 0
    while index < len(lines):
        header = re.fullmatch(r'@@ -(\d+),(\d+) \+(\d+),(\d+) @@\n', lines[index])
        if header is None:
            raise AssertionError('unexpected patch hunk')
        before, before_count, after, after_count = [int(x) for x in header.groups()]
        if before - 1 < cursor:
            raise AssertionError('overlapping patch hunk')
        output.extend(original[cursor:before - 1])
        cursor, index, removed, added = before - 1, index + 1, 0, 0
        if len(output) != after - 1:
            raise AssertionError('patch new-source offset mismatch')
        while index < len(lines) and not lines[index].startswith('@@ '):
            line = lines[index]
            if line[:1] not in (' ', '+', '-'):
                raise AssertionError('patch line invalid')
            if line[0] in (' ', '-'):
                if cursor >= len(original) or original[cursor] != line[1:]:
                    raise AssertionError('patch original-source context mismatch')
                cursor, removed = cursor + 1, removed + 1
            if line[0] in (' ', '+'):
                output.append(line[1:])
                added += 1
            index += 1
        if (removed, added) != (before_count, after_count):
            raise AssertionError('patch line-count mismatch')
        hunks += 1
    if hunks != 1:
        raise AssertionError('expected one ingestion-only hunk')
    output.extend(original[cursor:])
    source = ''.join(output)
    if hashlib.sha256(source.encode('utf8')).hexdigest() != PATCHED_SHA256:
        raise AssertionError('patched Store source hash mismatch')
    return source


def _load_store(source):
    module = types.ModuleType('assistant_mesh._original3198_store_fixture')
    module.__package__ = 'assistant_mesh'
    exec(compile(source, '<verified-original3198-store>', 'exec'), module.__dict__)
    return module.Store


class LegacySourceFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_source = _original_source()
        cls.patched_source = _patched_source(cls.original_source)
        cls.LegacyStore = _load_store(cls.patched_source)
        cls.OriginalStore = _load_store(cls.original_source)


class LegacyChannelStateBackportTests(state_tests.ChannelStateTests, LegacySourceFixture):
    """All 30 original canonical-state assertions, on the real old Store body."""
    def setUp(self):
        self.original_store_patch = mock.patch.object(state_tests, 'Store', self.LegacyStore)
        self.original_store_patch.start()
        self.addCleanup(self.original_store_patch.stop)
        super().setUp()

    def test_two_concurrent_poll_prepares_keep_one_canonical_lane(self):
        start, prepared, errors = threading.Event(), [], []
        def prepare():
            start.wait(3)
            try:
                prepared.append(self.state.channel_prepare_poll(self.term, 'original-cursor'))
            except state_tests.CarrierError as error:
                errors.append(str(error))
        threads = [threading.Thread(target=prepare) for unused in range(2)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(12)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(1, len(prepared))
        self.assertEqual(['channel_original_intent_unsettled'], errors)
        self.assertEqual(1, len(self.rows('channel_canonical_intents')))
        self.assertEqual([], self.rows('inbox'))

    def test_two_concurrent_commit_ack_lookups_do_not_reingest(self):
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        outcome = self.poll_outcome()
        ticket = self.authorize(prepared, 'poll', outcome)
        start, receipts, errors = threading.Event(), [], []
        def commit():
            start.wait(3)
            try:
                receipts.append(self.commit_poll(prepared, ticket, outcome))
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=commit) for unused in range(2)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(12)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(2, len(receipts))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(1, receipts[0]['accepted'])
        self.assertEqual(1, len(self.rows('inbox')))
        self.assertEqual(1, len(self.rows('tasks')))

    def test_legacy_public_ingest_concurrent_with_fenced_commit_keeps_guard(self):
        # A legacy unfenced poller MUST NOT be run alongside the new carrier.
        # This race proves detection/retention, not dual-poller authorization.
        prepared = self.state.channel_prepare_poll(self.term, 'original-cursor')
        outcome = self.poll_outcome()
        ticket = self.authorize(prepared, 'poll', outcome)
        started, errors = threading.Event(), []
        def commit():
            started.set()
            try:
                self.commit_poll(prepared, ticket, outcome)
            except BaseException as error:
                errors.append(error)
        with self.store.transaction() as db:
            self.store._ingest_in_db(db, [], 'legacy-advanced-cursor',
                self.account['ilink_user_id'], self.account['ilink_bot_id'])
            thread = threading.Thread(target=commit)
            thread.start()
            self.assertTrue(started.wait(3))
        thread.join(12)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(errors))
        self.assertEqual('channel_original_cursor_changed', str(errors[0]))
        self.assertEqual('legacy-advanced-cursor', self.store.get('cursor'))
        self.assertEqual([], self.rows('inbox'))
        self.assertEqual(outcome, state_tests.json.loads(self.canonical(prepared['operation_id'])['outcome']))
        self.assertIsNone(self.state.receipt(prepared['operation_id'])['persistence_sha256'])
        with self.assertRaisesRegex(state_tests.CarrierError, '^channel_original_intent_unsettled$'):
            self.state.channel_prepare_poll(self.term, 'legacy-advanced-cursor')


class LegacyStoreExtractionTests(LegacySourceFixture):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = self.LegacyStore(Path(self.tmp.name) / 'candidate.db',
                                     clock=lambda: 1000, recover_inflight=False)

    @staticmethod
    def message(identity='one', text='synthetic original task'):
        return {'message_id': identity, 'from_user_id': 'owner', 'to_user_id': 'bot',
            'message_type': 1, 'create_time_ms': 2000, 'context_token': 'synthetic-context',
            'item_list': [{'type': 1, 'text_item': {'text': text}}]}

    @staticmethod
    def snapshot(store):
        with store.transaction() as db:
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                    for table in tables}

    def test_no_method_or_schema_change_except_ingestion_extraction(self):
        before, after = ast.parse(self.original_source), ast.parse(self.patched_source)
        old_class = next(n for n in before.body if isinstance(n, ast.ClassDef) and n.name == 'Store')
        new_class = next(n for n in after.body if isinstance(n, ast.ClassDef) and n.name == 'Store')
        old_nodes = {n.name: ast.dump(n) for n in old_class.body if isinstance(n, ast.FunctionDef)}
        new_nodes = {n.name: ast.dump(n) for n in new_class.body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(set(old_nodes) | {'_ingest_in_db'}, set(new_nodes))
        for name in old_nodes:
            if name != 'ingest':
                self.assertEqual(old_nodes[name], new_nodes[name], name)
        old_class.body = [n for n in old_class.body if getattr(n, 'name', None) != 'ingest']
        new_class.body = [n for n in new_class.body if getattr(n, 'name', None) not in ('ingest', '_ingest_in_db')]
        self.assertEqual(ast.dump(before), ast.dump(after))

    def test_extracted_body_is_exact_original_ingest_ast(self):
        old = next(n for n in ast.walk(ast.parse(self.original_source))
                   if isinstance(n, ast.FunctionDef) and n.name == 'ingest')
        new = next(n for n in ast.walk(ast.parse(self.patched_source))
                   if isinstance(n, ast.FunctionDef) and n.name == '_ingest_in_db')
        # Ignore docstrings; unwrap only the original caller's transaction.
        expected = old.body[1:3] + old.body[3].body + old.body[4:]
        self.assertEqual([ast.dump(n) for n in expected], [ast.dump(n) for n in new.body[1:]])

    def test_public_ingest_matches_original_context_commands_and_archive(self):
        original = self.OriginalStore(Path(self.tmp.name) / 'original.db', clock=lambda: 1000)
        cases = [self.message('normal'), self.message('goal', '/goal retain original goal'),
                 self.message('plan', '/plan original planning'), self.message('status', '/status'),
                 self.message('bad-answer', '/answer nonexistent typo')]
        foreign = self.message('foreign')
        foreign['from_user_id'] = 'not-owner'
        cases.append(foreign)
        fixed_uuid = types.SimpleNamespace(hex='f' * 32)
        with mock.patch('uuid.uuid4', return_value=fixed_uuid):
            self.assertEqual(original.ingest(cases, 'next', 'owner', 'bot'),
                             self.store.ingest(cases, 'next', 'owner', 'bot'))
        self.assertEqual(self.snapshot(original), self.snapshot(self.store))

    def test_caller_owned_transaction_rolls_back_ingest_and_own_write_together(self):
        before = self.snapshot(self.store)
        with self.assertRaisesRegex(RuntimeError, 'synthetic receipt failure'):
            with self.store.transaction() as db:
                self.store._set(db, 'synthetic-receipt', 'not-yet-committed')
                self.store._ingest_in_db(db, [self.message()], 'next', 'owner', 'bot')
                raise RuntimeError('synthetic receipt failure')
        self.assertEqual(before, self.snapshot(self.store))

    def test_partial_invalid_batch_keeps_all_old_state(self):
        before = self.snapshot(self.store)
        invalid = self.message('invalid')
        invalid['item_list'] = 'invalid'
        with self.assertRaisesRegex(ValueError, '^invalid_items$'):
            self.store.ingest([self.message(), invalid], 'next', 'owner', 'bot')
        self.assertEqual(before, self.snapshot(self.store))

    def test_public_ingest_still_deduplicates_messages(self):
        message = self.message()
        self.assertEqual(1, self.store.ingest([message, message], 'cursor-one', 'owner', 'bot'))
        self.assertEqual(0, self.store.ingest([message], 'cursor-two', 'owner', 'bot'))
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
        self.assertEqual('cursor-two', self.store.get('cursor'))

    def test_concurrent_public_ingest_retains_single_original_message(self):
        start, errors, accepted = threading.Event(), [], []
        def ingest():
            try:
                start.wait(3)
                accepted.append(self.store.ingest([self.message()], 'next', 'owner', 'bot'))
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=ingest) for unused in range(8)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(12)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(1, sum(accepted))
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])

    def test_no_nested_transaction_from_extracted_ingest(self):
        calls = []
        transaction = self.store.transaction
        def recording():
            calls.append(True)
            return transaction()
        with mock.patch.object(self.store, 'transaction', recording):
            self.store.ingest([self.message()], 'next', 'owner', 'bot')
        self.assertEqual(1, len(calls))

    def test_original_constructor_recover_false_preserves_submitting_rows(self):
        task = self.store.create_task('synthetic task', task_id='original-task')
        self.store.enqueue('original-send', 'synthetic reply')
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET status='submitting'")
            db.execute("INSERT INTO steering(id,task_id,epoch,text,state) VALUES(?,?,?,?,?)",
                       ('original-steer', task, 1, 'synthetic steering', 'submitting'))
        before = self.snapshot(self.store)
        reopened = self.LegacyStore(self.store.path, clock=lambda: 1000, recover_inflight=False)
        self.assertEqual(before, self.snapshot(reopened))

    def test_channel_schema_is_opt_in_and_existing_native_full_rows_unchanged(self):
        from assistant_mesh.channel_carrier import CarrierWitness
        from assistant_mesh.channel_state import ChannelState
        self.store.create_task('native original task', task_id='native-task')
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='running',node='native-worker',epoch=3,"
                       "checkpoint=? WHERE id='native-task'",
                       ('{"thread_id":"original-thread","goal":{"status":"active"},"side_effect_started":true}',))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)',
                       ('private-scope', 'native-worker', 'codex', '{"thread_id":"original-thread"}', 'original-artifact', 42))
            db.execute('INSERT INTO native_sessions VALUES(?,?,?,?,?,?)',
                       ('private-scope', 'native-worker', 'codex', '{"thread_id":"original-thread"}', 'original-artifact', 42))
            db.execute('INSERT INTO session_chunks VALUES(?,?,?)',
                       ('original-artifact', 0, b'{"native":"original-bytes"}\n'))
            db.execute('INSERT INTO agent_actions VALUES(?,?,?)',
                       ('original-action', 'original-fingerprint', '{"result":"original"}'))
            db.execute('INSERT INTO interactions VALUES(?,?,?,?,?,?,?)',
                       ('original-question', 'native-task', 3, 'question', '{}', None, 42))
            db.execute('INSERT INTO steering VALUES(?,?,?,?,?)',
                       ('original-steer', 'native-task', 3, 'original text', 'submitting'))
        before = self.snapshot(self.store)
        self.assertFalse(any(name.startswith('channel_') for name in before))
        witness = CarrierWitness(self.store, 'synthetic-witness-no-grant')
        witness.enroll('a' * 64, 'synthetic-owner-ref', 'cloud')
        ChannelState(self.store, witness)
        after = self.snapshot(self.store)
        added = {'channel_carrier_witness', 'channel_carrier_accounts',
                 'channel_carrier_candidates', 'channel_carrier_intents', 'channel_canonical_intents'}
        self.assertEqual(added, set(after) - set(before))
        self.assertEqual(before, {table: after[table] for table in before})
        # No verifier means the opt-in schema still cannot grant any carrier.
        self.assertIsNone(witness.inspect('a' * 64)['holder'])
        self.assertTrue(witness.inspect('a' * 64)['frozen'])


if __name__ == '__main__':
    unittest.main()
