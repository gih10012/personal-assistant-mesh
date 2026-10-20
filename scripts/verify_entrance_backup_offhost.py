"""Owner-local complete-backup verification; capture never opens a writer.

No SSH, Store construction, service operation, credential copy or native call.
capture-export writes gzip ONLY to stdout, and an exclusive private snapshot
after the complete stream. A capture is not owner approval or a passed audit.
"""
import argparse
import collections
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time


SNAPSHOT = 'owner-entrance-source-snapshot/1'


def review_module():
    loader = importlib.util.spec_from_file_location('_offhost_entrance_review',
        str(Path(__file__).with_name('review_entrance_abandon.py')))
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
            'compressed_size': packed.tell(), 'compressed_sha256': review.util.sha(packed.getvalue()),
            'raw_size': raw_size, 'raw_sha256': raw_hash.hexdigest()}}
    # GzipFile may seek while reading; length is the complete original bytes.
    expected['old_artifact']['compressed_size'] = len(packed.getvalue())
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
    auth = review.authority({'worker_config': worker_config, 'server_config': server_config,
        'expected': {'worker_sha256': worker['sha256'], 'server_sha256': server['sha256']}})
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
        review.require(review.selected(db, auth, {'expected': expected}) == selected
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
    review.util.write_json(target, snapshot)
    version = review.util.read_checked(target, private=True, limit=review.util.MAX_SPEC)[1]
    return {'protocol': SNAPSHOT, 'snapshot_path': str(target), 'snapshot_sha256': version['sha256'],
            'backup': snapshot['backup'], 'capture_only': True, 'owner_approved': False,
            'native_terminal_proven': False, 'historical_reader_eof_proven': False,
            'backup_file_written_on_source_host': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    capture = sub.add_parser('capture-export')
    for name in ('worker-config', 'server-config', 'task-id', 'rollout', 'snapshot-out'):
        capture.add_argument('--' + name, required=True)
    capture.add_argument('--epoch', type=int, required=True)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('command required')
    try:
        result = capture_export(args.worker_config, args.server_config, args.task_id,
            args.epoch, args.rollout, args.snapshot_out, sys.stdout.buffer)
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        return 0
    except Exception:
        # stdout may contain an incomplete gzip. Only a matching snapshot plus
        # the later complete local verification can admit the received backup.
        print(json.dumps({'protocol': SNAPSHOT, 'capture_complete': False,
            'code': 'offhost_capture_unconfirmed', 'owner_approved': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
