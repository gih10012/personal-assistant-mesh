"""Owner-local complete-backup verification; capture never opens a writer.

No SSH, Store construction, service operation, credential copy or native call.
capture-export writes gzip ONLY to stdout, and an exclusive private snapshot
after the complete stream. A capture is not owner approval or a passed audit.
"""
import argparse
import codecs
import collections
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time


SNAPSHOT = 'owner-entrance-source-snapshot/1'
REQUEST = 'owner-entrance-backup-verification-request/1'
RECEIPT = 'owner-entrance-offhost-backup-verification/1'
CHECKS = ('complete_gzip_crc_eof', 'complete_sql_hash_and_transaction', 'restored_quick_check',
          'complete_schema_matches', 'complete_selected_rows_and_receipts_match',
          'old_artifact_strict_prefix_matches', 'complete_native_source_and_two_execs_match')


def review_module():
    loader = importlib.util.spec_from_file_location('_offhost_entrance_review',
        str(Path(__file__).resolve().with_name('review_entrance_abandon.py')))
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    return module


def decimal_metadata(value):
    if isinstance(value, dict):
        return {key: str(item) if key in ('mtime_ns', 'ctime_ns') else decimal_metadata(item)
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [decimal_metadata(item) for item in value]
    return value


def schema_rows(db):
    return [list(row) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]


class HashingWriter:
    def __init__(self, stream):
        self.stream, self.hashed, self.size = stream, hashlib.sha256(), 0

    def write(self, data):
        view, total = memoryview(data), 0
        while total < len(view):
            written = self.stream.write(view[total:])
            if written is None:
                written = len(view) - total
            if written <= 0:
                raise OSError('offhost_export_short_write')
            self.hashed.update(view[total:total + written])
            total += written
            self.size += written
        return total

    def flush(self):
        self.stream.flush()


def capture_expected(review, db, auth, task_id, epoch, rollout_path):
    """Capture facts for Root's later review, never admit or settle a task."""
    value = review.util.quota.snapshot(db, task_id, epoch, auth)
    task, session = value['task'], value['session']
    event = db.execute('SELECT * FROM entrance_events WHERE task_id=?', (task_id,)).fetchall()
    review.require(len(event) == 1, 'offhost_original_event_required')
    event = dict(event[0])
    actions = [dict(row) for row in db.execute(
        'SELECT * FROM entrance_actions WHERE action_id>=? AND action_id<? ORDER BY action_id',
        (task_id + ':exec-', task_id + ':exec.'))]
    path = review.util.quota.canonical_rollout(rollout_path, value)
    raw, version = review.util.read_checked(path, private=True)
    lines, active, calls, outputs = [], False, [], []
    counts = collections.Counter()
    for line in raw.splitlines(True):
        review.require(line.endswith(b'\n'), 'offhost_incomplete_native_line')
        record = review.parse(line.decode('utf8'))
        payload = record.get('payload') or {}
        if record.get('type') == 'event_msg' and payload.get('type') == 'task_started':
            review.require(not active, 'offhost_later_native_turn')
            active = payload.get('turn_id') == value['turn']
        if not active:
            continue
        lines.append(line)
        counts[record.get('type') + ':' + str(payload.get('type') or '')] += 1
        if record.get('type') == 'response_item' and payload.get('type') == 'custom_tool_call':
            review.require(payload.get('name') == 'exec', 'offhost_two_original_execs_required')
            calls.append({'call_id': payload['call_id'], 'input': payload['input'],
                          'input_sha256': review.util.sha(payload['input'].encode('utf8'))})
        if record.get('type') == 'response_item' and payload.get('type') == 'custom_tool_call_output':
            outputs.append((payload['call_id'], review.digest(payload['output'])))
    review.require(active and len(calls) == len(outputs) == 2
                   and [call['call_id'] for call in calls] == [item[0] for item in outputs],
                   'offhost_two_original_execs_required')
    tools = [dict(call, output_canonical_sha256=output[1]) for call, output in zip(calls, outputs)]
    parts = db.execute('SELECT part,body FROM session_chunks WHERE id=? ORDER BY part',
                       (session['artifact'],)).fetchall()
    packed = io.BytesIO()
    for number, row in enumerate(parts):
        review.require(row['part'] == number and isinstance(row['body'], bytes)
                       and len(row['body']) <= 65536, 'offhost_old_chunks_changed')
        packed.write(row['body'])
        review.require(packed.tell() <= review.util.MAX_FILE, 'offhost_old_artifact_bound')
    packed.seek(0)
    raw_hash, raw_size = hashlib.sha256(), 0
    with gzip.GzipFile(fileobj=packed, mode='rb') as previous:
        for block in iter(lambda: previous.read(65536), b''):
            raw_hash.update(block)
            raw_size += len(block)
            review.require(raw_size <= review.util.MAX_FILE, 'offhost_old_artifact_bound')
    expected = {'task_id': task_id, 'epoch': epoch, 'scope': task['scope'], 'event_id': event['event_id'],
        'thread_id': value['thread'], 'turn_id': value['turn'],
        'worker_sha256': auth['worker_file']['sha256'], 'server_sha256': auth['server_file']['sha256'],
        'task_sha256': review.digest(task), 'session_sha256': review.digest(session),
        'checkpoint_sha256': review.util.sha(task['checkpoint'].encode('utf8')),
        'session_state_sha256': review.util.sha(session['state'].encode('utf8')),
        'event_sha256': review.digest(event),
        'action_row_sha256': {row['action_id']: review.digest(row) for row in actions},
        'rollout_path': str(path), 'rollout_file': decimal_metadata(version),
        'selected_sha256': review.util.sha(b''.join(lines)), 'selected_size': sum(map(len, lines)),
        'record_counts': dict(counts), 'tools': tools,
        'old_artifact': {'id': session['artifact'], 'parts': len(parts),
            'compressed_size': len(packed.getvalue()), 'compressed_sha256': review.util.sha(packed.getvalue()),
            'raw_size': raw_size, 'raw_sha256': raw_hash.hexdigest()}}
    before = review.selected(db, auth, {'expected': expected})
    review.native({'expected': expected}, before)
    return expected, before


def capture_export(worker_config, server_config, task_id, epoch, rollout_path,
                   snapshot_out, output):
    review = review_module()
    target = Path(snapshot_out)
    review.util.private_directory(target.parent)
    review.require(not target.exists() and not target.is_symlink(), 'offhost_snapshot_must_be_new')
    worker = review.util.read_checked(worker_config, private=True, limit=review.util.MAX_SPEC)[1]
    server = review.util.read_checked(server_config, private=True, limit=review.util.MAX_SPEC)[1]
    authority_spec = {'worker_config': worker_config, 'server_config': server_config,
        'expected': {'worker_sha256': worker['sha256'], 'server_sha256': server['sha256']}}
    auth = review.authority(authority_spec)
    writer, sql_hash, sql_size = HashingWriter(output), hashlib.sha256(), 0
    with contextlib.closing(review.util.quota.readonly(auth['database'])) as db:
        expected, selected = capture_expected(review, db, auth, task_id, epoch, rollout_path)
        schema = schema_rows(db)
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
        page_count = db.execute('PRAGMA page_count').fetchone()[0]
        with gzip.GzipFile(filename='', fileobj=writer, mode='wb', mtime=0) as compressed:
            for statement in db.iterdump():
                encoded = (statement + '\n').encode('utf8')
                sql_hash.update(encoded)
                sql_size += len(encoded)
                compressed.write(encoded)
        writer.flush()
        review.require(review.authority(authority_spec) == auth
                       and review.selected(db, auth, {'expected': expected}) == selected
                       and review.native({'expected': expected}, selected)[1] == {
                           key: int(item) if key in ('mtime_ns', 'ctime_ns') else item
                           for key, item in expected['rollout_file'].items()},
                       'offhost_source_changed_during_export')
        snapshot = {'protocol': SNAPSHOT, 'captured_at': time.time(),
            'source': {'worker_config': str(worker_config), 'server_config': str(server_config),
                'worker_file': decimal_metadata(worker), 'server_file': decimal_metadata(server),
                'database': str(auth['database']), 'database_file_size': review.util.file_metadata(
                    auth['database'], private=True)['size'],
                'database_snapshot_size': page_count * page_size,
                'auth': {'node': auth['node'], 'homes': sorted(auth['homes']), 'policy': auth['policy']}},
            'expected': expected, 'selected': selected, 'schema': schema,
            'backup': {'sha256': writer.hashed.hexdigest(), 'size': writer.size,
                       'sql_sha256': sql_hash.hexdigest(), 'sql_size': sql_size}}
    review.require(len(review.util.encoded(snapshot)) <= review.util.MAX_SPEC, 'offhost_snapshot_size_bound')
    review.util.write_json(target, snapshot)
    version = review.util.read_checked(target, private=True, limit=review.util.MAX_SPEC)[1]
    return {'protocol': SNAPSHOT, 'snapshot_path': str(target), 'snapshot_sha256': version['sha256'],
            'backup': snapshot['backup'], 'capture_only': True, 'owner_approved': False,
            'native_terminal_proven': False, 'historical_reader_eof_proven': False,
            'backup_file_written_on_source_host': False}


def load_snapshot(review, path):
    raw, version = review.util.read_checked(path, private=True, limit=review.util.MAX_SPEC)
    value = review.parse(raw.decode('utf8'))
    review.util.exact(value, ('protocol', 'captured_at', 'source', 'expected', 'selected', 'schema', 'backup'),
                      'offhost_snapshot_shape')
    review.require(value['protocol'] == SNAPSHOT, 'offhost_snapshot_protocol')
    source = value['source']
    review.util.exact(source, ('worker_config', 'server_config', 'worker_file', 'server_file', 'database',
                              'database_file_size', 'database_snapshot_size', 'auth'), 'offhost_source_shape')
    review.util.exact(source['auth'], ('node', 'homes', 'policy'), 'offhost_source_auth_shape')
    review.require(type(source['database_snapshot_size']) is int and source['database_snapshot_size'] >= 4096
                   and type(source['database_file_size']) is int and source['database_file_size'] >= 0,
                   'offhost_source_size_invalid')
    review.require(isinstance(source['auth']['homes'], list) and source['auth']['homes']
                   and all(isinstance(home, str) and Path(home).is_absolute() for home in source['auth']['homes']),
                   'offhost_source_homes_invalid')
    review.util.exact(value['backup'], ('sha256', 'size', 'sql_sha256', 'sql_size'), 'offhost_backup_shape')
    for key in ('sha256', 'sql_sha256'):
        review.require(isinstance(value['backup'][key], str)
                       and re.fullmatch('[0-9a-f]{64}', value['backup'][key]), 'offhost_backup_digest_invalid')
    for key in ('size', 'sql_size'):
        review.require(type(value['backup'][key]) is int and value['backup'][key] > 0,
                       'offhost_backup_size_invalid')
    review.util.exact(value['selected'], ('task', 'checkpoint', 'session', 'thread', 'turn', 'event', 'actions'),
                      'offhost_selected_shape')
    review.require(isinstance(value['schema'], list) and value['schema']
                   and all(isinstance(row, list) and len(row) == 4 for row in value['schema']),
                   'offhost_schema_shape')
    exp, before = value['expected'], value['selected']
    review.require(isinstance(exp, dict) and exp['task_id'] == 'entrance-' + exp['event_id']
                   and re.fullmatch('notice-[0-9a-f]{64}', exp['event_id'])
                   and review.digest(before['task']) == exp['task_sha256']
                   and review.digest(before['session']) == exp['session_sha256']
                   and review.digest(before['event']) == exp['event_sha256']
                   and {row['action_id']: review.digest(row) for row in before['actions']} == exp['action_row_sha256']
                   and review.util.sha(before['task']['checkpoint'].encode('utf8')) == exp['checkpoint_sha256']
                   and review.util.sha(before['session']['state'].encode('utf8')) == exp['session_state_sha256'],
                   'offhost_snapshot_original_bindings_changed')
    return value, version


def verification_request(review, snapshot, snapshot_sha256):
    """No final-spec/receipt circularity: the approved spec later pins both files."""
    return {'protocol': REQUEST, 'case_id': review.case_id({'expected': snapshot['expected']}),
        'snapshot_sha256': snapshot_sha256, 'expected_sha256': review.digest(snapshot['expected']),
        'selected_sha256': review.digest(snapshot['selected']), 'schema_sha256': review.digest(snapshot['schema']),
        'backup': snapshot['backup'],
        'native_source': {key: snapshot['expected']['rollout_file'][key] for key in ('sha256', 'size')},
        'restore_page_cap_bytes': snapshot['source']['database_snapshot_size']}


def auditor_sources(review):
    files = (Path(__file__).resolve(), Path(review.__file__).resolve(),
             Path(review.util.__file__).resolve(), Path(review.util.quota.__file__).resolve())
    return {path.name: review.util.read_checked(path, root_ok=True, limit=review.util.MAX_SPEC)[1]['sha256']
            for path in files}


def restore_complete_backup(review, snapshot, backup_path, rollout_path, work_root):
    """Actual full local restore, using the original SQL deny/verification rules."""
    pin = snapshot['backup']
    version = review.util.digest_checked_backup(backup_path)
    review.require(version['sha256'] == pin['sha256'] and version['size'] == pin['size'],
                   'offhost_complete_backup_changed')
    root = review.util.private_directory(work_root)
    bound = snapshot['source']['database_snapshot_size']
    review.require(shutil.disk_usage(str(root)).free >= bound + 32 * 1024 * 1024 + 1024 ** 3,
                   'offhost_restore_space_required')
    local_spec = {'expected': dict(snapshot['expected'], rollout_path=str(rollout_path))}
    auth = dict(snapshot['source']['auth'])
    auth['homes'] = set(auth['homes'])
    sql_hash, sql_size = hashlib.sha256(), 0
    with tempfile.TemporaryDirectory(prefix='.entrance-offhost-backup.', dir=str(root)) as folder:
        target = Path(folder) / 'verification.sqlite'
        with review.util.create_file(target):
            pass
        with contextlib.closing(sqlite3.connect(str(target), isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            db.execute('PRAGMA max_page_count=' + str(max(1, bound // page_size)))
            db.set_authorizer(review.util.backup_authorizer)
            pending, committed, statements = '', False, 0
            decoder = codecs.getincrementaldecoder('utf8')()
            with os.fdopen(os.open(str(backup_path), os.O_RDONLY | os.O_NOFOLLOW), 'rb') as compressed, \
                    gzip.GzipFile(fileobj=compressed, mode='rb') as source:
                for block in iter(lambda: source.readline(65536), b''):
                    sql_hash.update(block)
                    sql_size += len(block)
                    pending += decoder.decode(block)
                    review.require(len(pending.encode('utf8')) <= review.util.MAX_FILE,
                                   'offhost_backup_statement_bound')
                    if not sqlite3.complete_statement(pending):
                        continue
                    statement = pending.strip()
                    review.require(not committed and (statements or statement == 'BEGIN TRANSACTION;'),
                                   'offhost_backup_transaction_incomplete')
                    db.execute(pending)
                    statements += 1
                    committed = statement == 'COMMIT;'
                    pending = ''
                    review.require(shutil.disk_usage(str(root)).free >= 1024 ** 3, 'offhost_restore_space_floor')
                pending += decoder.decode(b'', final=True)
            review.require(committed and not pending.strip() and not db.in_transaction
                           and sql_size == pin['sql_size'] and sql_hash.hexdigest() == pin['sql_sha256'],
                           'offhost_complete_sql_changed')
            def read_authorizer(action, first, second, database, trigger):
                allowed = action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ)
                allowed |= action == sqlite3.SQLITE_PRAGMA and first == 'quick_check' and second is None
                return sqlite3.SQLITE_OK if allowed and trigger is None and database in (None, 'main') else sqlite3.SQLITE_DENY
            db.set_authorizer(read_authorizer)
            review.require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                           and schema_rows(db) == snapshot['schema']
                           and review.selected(db, auth, local_spec) == snapshot['selected'],
                           'offhost_complete_restored_state_changed')
    review.require(review.util.digest_checked_backup(backup_path) == version, 'offhost_backup_generation_changed')


def verify_local(snapshot_path, backup_path, rollout_path, receipt_out, work_root,
                 auditor_label='owner-selected-local-validator'):
    review = review_module()
    started = time.time()
    snapshot, snapshot_file = load_snapshot(review, snapshot_path)
    target = Path(receipt_out)
    review.util.private_directory(target.parent)
    review.require(not target.exists() and not target.is_symlink(), 'offhost_receipt_must_be_new')
    review.require(isinstance(auditor_label, str) and re.fullmatch('[A-Za-z0-9_.:/-]{1,100}', auditor_label),
                   'offhost_auditor_label_invalid')
    sources = auditor_sources(review)
    raw, local_source = review.util.read_checked(rollout_path, private=True)
    expected = snapshot['expected']
    review.require(local_source['sha256'] == expected['rollout_file']['sha256']
                   and local_source['size'] == expected['rollout_file']['size'], 'offhost_complete_native_changed')
    review.native_records(raw, expected, snapshot['selected'])
    restore_complete_backup(review, snapshot, backup_path, rollout_path, work_root)
    review.require(review.util.read_checked(snapshot_path, private=True, limit=review.util.MAX_SPEC)[1] == snapshot_file
                   and review.util.read_checked(rollout_path, private=True)[1] == local_source
                   and auditor_sources(review) == sources, 'offhost_validation_input_changed')
    receipt = {'protocol': RECEIPT, 'request': verification_request(review, snapshot, snapshot_file['sha256']),
        'started_at': started, 'completed_at': time.time(),
        'auditor': {'label': auditor_label, 'tool_sources': sources,
                    'python_version': sys.version.split()[0], 'sqlite_version': sqlite3.sqlite_version},
        'checks': {key: True for key in CHECKS}, 'authority_basis': 'explicit-owner-final-spec-sha256',
        'host_attestation': False, 'native_terminal_proven': False, 'historical_reader_eof_proven': False}
    review.util.write_json(target, receipt)
    version = review.util.read_checked(target, private=True, limit=review.util.MAX_SPEC)[1]
    return {'protocol': RECEIPT, 'receipt_path': str(target), 'receipt_sha256': version['sha256'],
            'snapshot_sha256': snapshot_file['sha256'], 'case_id': receipt['request']['case_id'],
            'request_sha256': review.digest(receipt['request']), 'host_attestation': False,
            'requires_explicit_final_spec_approval': True}


def check_receipt(spec, auth, before):
    """Root-approved private evidence, NOT remote full-backup/host attestation."""
    review = review_module()
    pin = spec['backup_verification']
    snapshot, snapshot_file = load_snapshot(review, pin['snapshot_path'])
    raw, receipt_file = review.util.read_checked(pin['receipt_path'], private=True, limit=review.util.MAX_SPEC)
    review.require(snapshot_file['sha256'] == pin['snapshot_sha256'] and receipt_file['sha256'] == pin['receipt_sha256'],
                   'offhost_approved_evidence_changed')
    review.require(snapshot['expected'] == spec['expected'] and snapshot['selected'] == before
                   and snapshot['backup'] == {key: spec['backup'][key] for key in snapshot['backup']},
                   'offhost_original_case_or_backup_changed')
    source = snapshot['source']
    review.require(source['worker_config'] == spec['worker_config'] and source['server_config'] == spec['server_config']
                   and source['database'] == str(auth['database'])
                   and source['auth'] == {'node': auth['node'], 'homes': sorted(auth['homes']), 'policy': auth['policy']}
                   and source['worker_file'] == decimal_metadata(auth['worker_file'])
                   and source['server_file'] == decimal_metadata(auth['server_file']), 'offhost_current_authority_changed')
    with contextlib.closing(review.util.quota.readonly(auth['database'])) as live:
        review.require(schema_rows(live) == snapshot['schema'], 'offhost_current_schema_changed')
    receipt = review.parse(raw.decode('utf8'))
    review.util.exact(receipt, ('protocol', 'request', 'started_at', 'completed_at', 'auditor', 'checks',
                              'authority_basis', 'host_attestation', 'native_terminal_proven',
                              'historical_reader_eof_proven'), 'offhost_receipt_shape')
    review.require(receipt['protocol'] == RECEIPT and receipt['request'] == verification_request(
                       review, snapshot, snapshot_file['sha256'])
                   and receipt['authority_basis'] == 'explicit-owner-final-spec-sha256'
                   and all(receipt[key] is False for key in ('host_attestation', 'native_terminal_proven',
                                                            'historical_reader_eof_proven')),
                   'offhost_receipt_original_request_changed')
    review.require(set(receipt['checks']) == set(CHECKS) and all(value is True for value in receipt['checks'].values()),
                   'offhost_complete_checks_required')
    review.util.exact(receipt['auditor'], ('label', 'tool_sources', 'python_version', 'sqlite_version'),
                      'offhost_auditor_shape')
    review.require(receipt['auditor']['tool_sources'] == auditor_sources(review)
                   and all(isinstance(receipt['auditor'][key], str) and 0 < len(receipt['auditor'][key]) <= 100
                           for key in ('label', 'python_version', 'sqlite_version'))
                   and type(receipt['started_at']) in (int, float) and type(receipt['completed_at']) in (int, float)
                   and 0 < receipt['started_at'] <= receipt['completed_at'] < float('inf'), 'offhost_auditor_changed')
    return {'mode': 'owner-off-host', 'snapshot_file': snapshot_file, 'receipt_file': receipt_file,
            'request': receipt['request'], 'receipt': receipt, 'owner_approval_required': True,
            'host_attestation': False, 'complete_backup_verified_on_apply_host': False}


def evidence_unchanged(spec, expected):
    """Post-COMMIT: recheck evidence files, not the deliberately extended schema."""
    review = review_module()
    pin = spec['backup_verification']
    return (review.util.read_checked(pin['snapshot_path'], private=True, limit=review.util.MAX_SPEC)[1]
            == expected['snapshot_file']
            and review.util.read_checked(pin['receipt_path'], private=True, limit=review.util.MAX_SPEC)[1]
            == expected['receipt_file']
            and auditor_sources(review) == expected['receipt']['auditor']['tool_sources'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    capture = sub.add_parser('capture-export')
    for name in ('worker-config', 'server-config', 'task-id', 'rollout', 'snapshot-out'):
        capture.add_argument('--' + name, required=True)
    capture.add_argument('--epoch', type=int, required=True)
    verify = sub.add_parser('verify')
    for name in ('snapshot', 'backup', 'rollout', 'receipt-out', 'work-root'):
        verify.add_argument('--' + name, required=True)
    verify.add_argument('--auditor-label', default='owner-selected-local-validator')
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('command required')
    try:
        if args.command == 'capture-export':
            result = capture_export(args.worker_config, args.server_config, args.task_id,
                args.epoch, args.rollout, args.snapshot_out, sys.stdout.buffer)
            print(json.dumps(result, sort_keys=True), file=sys.stderr)
        else:
            result = verify_local(args.snapshot, args.backup, args.rollout, args.receipt_out,
                                  args.work_root, args.auditor_label)
            print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        # stdout may contain an incomplete gzip. Only a matching snapshot plus
        # the later complete local verification can admit the received backup.
        code = str(error)
        if re.fullmatch('[a-z][a-z0-9_]{0,100}', code) is None:
            code = ('offhost_restore_page_cap_exceeded' if str(error) == 'database or disk is full'
                    else 'offhost_verification_unconfirmed')
        print(json.dumps({'protocol': SNAPSHOT if args.command == 'capture-export' else RECEIPT,
            'stage': args.command, 'complete': False, 'code': code,
            'owner_approved': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
