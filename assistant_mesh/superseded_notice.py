"""One reviewed stale control notice may stop reserving its conversation.

The original task's true effect guard and UNKNOWN remain readable. Nothing
can retry that row. Only the claim-time reservation lookup may exclude it,
and only after the fixed owner review receipt matches the complete row.
"""
import contextlib
import hashlib
import json
import sqlite3
import threading

PROTOCOL = 'owner-superseded-aa-notice/1'
TABLE = 'superseded_notice_reviews'
EPOCH = 1
CLOSED_CASE = {
    'task': '232ee1c0ba6faefffa60888485daa65d0ef714c0f649c71d43714bdad067daa2',
    'input': '900e46cfcebe15f892e856d545ee99d268fed9b677ffde1ffa0b058d345b311d',
    'thread': 'bd3481ccb666c8d8942e42de42417413b8657fa54543c01f4b8014606845369f',
    'turn': 'e7121acb04bf08c4dc9a157a88d8ab79b90492385f07be66c721de64ddc2ce45',
    'auth_home': 'cfdc1771bf01582ff5d9709da8ea5ab6870a9edea36831cf1855b6bd787f20fd'}
RESERVATION_QUERIES = frozenset((
    "SELECT checkpoint FROM tasks WHERE scope=? AND id<>? AND status<>'completed'",
    "SELECT status,checkpoint FROM tasks WHERE scope=? AND id<>? AND status<>'completed'"))


def _digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(raw.encode('utf8')).hexdigest()


def _pin(name, value):
    return isinstance(value, str) and hashlib.sha256(value.encode()).hexdigest() == CLOSED_CASE[name]


def reviewed_task(db):
    """Return the sole reviewed ID, or preserve the existing reservation."""
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return None
        records = db.execute('SELECT * FROM superseded_notice_reviews').fetchall()
        if len(records) != 1:
            return None
        record = dict(records[0])
        task_id = record['task_id']
        if not _pin('task', task_id):
            return None
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            return None
        task = dict(row)
        before = json.loads(record['before_task'])
        receipt = json.loads(record['receipt'])
        proof = json.loads(record['proof'])
        checkpoint = json.loads(before['checkpoint'])
        if (record['source_epoch'] != EPOCH or record['closed_epoch'] != EPOCH + 1
                or before['id'] != task_id or before['scope'] != 'leader:owner'
                or before['status'] != 'needs_review' or before['epoch'] != EPOCH
                or before['parent_id'] is not None
                or not _pin('input', before['input'])
                or checkpoint.get('side_effect_started') is not True
                or any(not _pin(name, checkpoint.get(key)) for name, key in (
                    ('thread', 'thread_id'), ('turn', 'turn_id'), ('auth_home', 'codex_auth_home')))
                or proof.get('protocol') != PROTOCOL or proof.get('task') != before
                or proof.get('session') != json.loads(record['before_session'])
                or proof.get('native', {}).get('goal', 'missing') is not None
                or _digest(proof) != record['review_sha256']
                or receipt.get('review_digest') != record['review_sha256']
                or receipt.get('protocol') != PROTOCOL or receipt.get('task_id') != task_id
                or receipt.get('source_epoch') != EPOCH or receipt.get('closed_epoch') != EPOCH + 1
                or receipt.get('native_outcome') != 'unknown'
                or receipt.get('effects_zero_verified') is not False
                or receipt.get('original_input_requeued') is not False
                or receipt.get('retry_authorized') is not False
                or receipt.get('notice_delivery_verified') is not False
                or receipt.get('side_effect_started') is not True
                or receipt.get('status') != 'failed' or receipt.get('disposition') != 'deferred'
                or receipt.get('resolution') != 'superseded'
                or task['result'] != record['receipt']):
            return None
        expected = dict(before, status='failed', epoch=EPOCH + 1, result=record['receipt'])
        return task_id if task == expected else None
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return None


class _ClaimConnection:
    def __init__(self, original):
        self.original = original

    def execute(self, sql, parameters=()):
        if sql in RESERVATION_QUERIES and len(parameters) == 2 and parameters[0] == 'leader:owner':
            reviewed = reviewed_task(self.original)
            if reviewed is not None:
                return self.original.execute(sql + ' AND id<>?', tuple(parameters) + (reviewed,))
        return self.original.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.original, name)


def make_store_class(original):
    """Preserve the exact Store/core and every non-claim query and guard."""
    if getattr(original, '_superseded_notice_claim_version', None) == 1:
        return original

    class ReviewedNoticeStore(original):
        _superseded_notice_claim_version = 1

        def __init__(self, *args, **kwargs):
            self._notice_claim = threading.local()
            super(ReviewedNoticeStore, self).__init__(*args, **kwargs)

        @contextlib.contextmanager
        def transaction(self):
            with super(ReviewedNoticeStore, self).transaction() as db:
                yield _ClaimConnection(db) if getattr(self._notice_claim, 'active', False) else db

        def claim(self, node):
            previous = getattr(self._notice_claim, 'active', False)
            self._notice_claim.active = True
            try:
                return super(ReviewedNoticeStore, self).claim(node)
            finally:
                self._notice_claim.active = previous

    return ReviewedNoticeStore
