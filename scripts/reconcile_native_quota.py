"""Owner-local, fail-closed reconciliation of one proven zero-effect quota turn.

Default is a read-only audit. --apply snapshots the complete native rollout,
publishes a NEW immutable artifact, and releases only the fenced original task.
No model, account, network, or tool invocation occurs here. Native history is
opaque for transport; only selected-turn event/type metadata is interpreted.
Python 3.6 compatible. See --help; raw checkpoints remain in the private DB.
"""
import argparse
import contextlib
import datetime
import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from assistant_mesh.store import Store


NAMESPACE = 'native-zero-effect-quota-v1'
MAX_LINE = 8 * 1024 * 1024


class AuditDenied(ValueError):
    """A fixed, non-content-bearing public denial code."""


def require(condition, code):
    if not condition:
        raise AuditDenied(code)


def known_fields(payload, fields):
    require(set(payload) <= set(fields), 'unknown_selected_turn_metadata')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_rollout_json_key')
        result[key] = value
    return result


def checked_path(value, file=False, private=False):
    path = Path(value)
    require(path.is_absolute() and '..' not in path.parts, 'absolute_canonical_path_required')
    for ancestor in reversed((path,) + tuple(path.parents)):
        metadata = ancestor.lstat()
        require(not stat.S_ISLNK(metadata.st_mode), 'symlink_path_denied')
        require(metadata.st_uid in (0, os.getuid()), 'foreign_path_denied')
        if ancestor != path or not file:
            require(stat.S_ISDIR(metadata.st_mode), 'directory_required')
            require(not metadata.st_mode & 0o022 or metadata.st_mode & stat.S_ISVTX,
                    'writable_ancestor_denied')
    metadata = path.lstat()
    if file:
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid(), 'owned_file_required')
        if private:
            require(stat.S_IMODE(metadata.st_mode) == 0o600, 'private_0600_file_required')
    return path


def private_config(path):
    path = checked_path(path, file=True, private=True)
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'r') as source:
        require(os.fstat(source.fileno()).st_ino == path.lstat().st_ino, 'config_changed')
        value = json.load(source)
    require(isinstance(value, dict), 'config_object_required')
    return value


def authority(worker_path, server_path):
    worker, server = private_config(worker_path), private_config(server_path)
    node = worker.get('node_id')
    require(isinstance(node, str) and node and server.get('node_id') == node, 'node_config_mismatch')
    require('agent' in worker.get('capabilities', []), 'agent_worker_required')
    peers = server.get('peers', [])
    require(any(peer.get('role') == 'worker' and peer.get('node') == node for peer in peers),
            'worker_authority_mismatch')
    require(any(peer.get('role') == 'operator' for peer in peers), 'operator_authority_required')
    codex = worker.get('codex') or {}
    roster = worker.get('codex_accounts')
    if roster is None:
        roster = [codex]
    require(isinstance(roster, list) and roster and all(isinstance(row, dict) for row in roster),
            'auth_roster_required')
    homes = set()
    for row in roster:
        value = row.get('auth_home', codex.get('auth_home'))
        require(isinstance(value, str), 'auth_roster_required')
        homes.add(str(checked_path(value)))
    database = checked_path(server['database'], file=True, private=True)
    return {'node': node, 'homes': homes, 'database': database}


def readonly(database):
    connection = sqlite3.connect('file:' + quote(str(database), safe='/') + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('BEGIN')
    return connection


def identity(value):
    try:
        require(isinstance(value, str) and str(uuid.UUID(value)) == value, 'native_identity_required')
    except (ValueError, AttributeError):
        raise AuditDenied('native_identity_required') from None
    return value


def snapshot(db, task_id, expected_epoch, auth):
    row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    require(row is not None, 'task_not_found')
    task = dict(row)
    require(task['status'] == 'needs_review', 'task_not_needs_review')
    require(expected_epoch is None or task['epoch'] == expected_epoch, 'task_epoch_changed')
    checkpoint = json.loads(task['checkpoint'])
    require(isinstance(checkpoint, dict) and checkpoint.get('harness', 'codex') == 'codex',
            'codex_checkpoint_required')
    require(checkpoint.get('codex_node') == auth['node'] and task['node'] in (None, auth['node']),
            'task_node_mismatch')
    require(checkpoint.get('codex_auth_home') in auth['homes'], 'auth_home_not_in_roster')
    require(checkpoint.get('side_effect_started') is True, 'review_reservation_required')
    thread, turn = identity(checkpoint.get('thread_id')), identity(checkpoint.get('turn_id'))
    session = db.execute('SELECT * FROM native_sessions WHERE scope=? AND harness=?',
                         (task['scope'], 'codex')).fetchone()
    require(session is not None, 'scope_native_session_required')
    session = dict(session)
    state = json.loads(session['state'])
    require(session['node'] == auth['node'] and state.get('thread_id') == thread
            and state.get('codex_node') == auth['node'], 'scope_session_identity_mismatch')
    require(state.get('codex_auth_home') == checkpoint['codex_auth_home'], 'scope_session_auth_mismatch')
    require(not db.execute("SELECT 1 FROM tasks WHERE scope=? AND id<>? AND status='running' LIMIT 1",
                           (task['scope'], task_id)).fetchone(), 'scope_has_running_task')
    return {'task': task, 'checkpoint': checkpoint, 'session': session, 'thread': thread, 'turn': turn}


def canonical_rollout(path, selected):
    path = checked_path(path, file=True, private=True)
    home = Path(selected['checkpoint']['codex_auth_home'])
    try:
        relative = path.relative_to(home / 'sessions')
    except ValueError:
        raise AuditDenied('rollout_outside_selected_auth_home') from None
    require(len(relative.parts) == 4, 'canonical_rollout_path_required')
    return path


def file_version(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def metadata_paths(home, payload, thread):
    require(payload.get('id') == thread, 'rollout_thread_mismatch')
    value = payload.get('timestamp')
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)',
                         value if isinstance(value, str) else '')
    require(match is not None, 'rollout_timestamp_invalid')
    zone = '+0000' if match.group(2) == 'Z' else match.group(2).replace(':', '')
    instant = datetime.datetime.strptime(match.group(1) + zone, '%Y-%m-%dT%H:%M:%S%z')
    # Codex's original writer names rollouts in host local time; this project's
    # cross-profile restore names the SAME session in UTC. Both are canonical,
    # not arbitrary locations or inferred new thread identities.
    instants = (instant.astimezone(datetime.timezone.utc), instant.astimezone())
    return {home / 'sessions' / candidate.strftime('%Y/%m/%d') / (
        'rollout-' + candidate.strftime('%Y-%m-%dT%H-%M-%S') + '-' + thread + '.jsonl')
        for candidate in instants}


def inspect_rollout(path, selected, archive=None):
    """Fail closed on unknown selected-turn execution evidence; never return bodies."""
    path = canonical_rollout(path, selected)
    before = path.lstat()
    digest, size, starts, completes, active = hashlib.sha256(), 0, 0, 0, False
    counts = {}
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        require(file_version(os.fstat(source.fileno())) == file_version(before), 'rollout_changed')
        number = 0
        while True:
            line = source.readline(MAX_LINE + 1)
            if not line:
                break
            number += 1
            require(len(line) <= MAX_LINE and line.endswith(b'\n'), 'rollout_incomplete_or_oversized_line')
            digest.update(line)
            size += len(line)
            if archive is not None:
                archive.write(line)  # Entire original bytes, including every previous turn.
            value = json.loads(line.decode('utf8'), object_pairs_hook=unique_object)
            require(isinstance(value, dict), 'rollout_record_invalid')
            kind, payload = value.get('type'), value.get('payload')
            if number == 1:
                require(kind == 'session_meta' and isinstance(payload, dict), 'session_metadata_required')
                require(path in metadata_paths(Path(selected['checkpoint']['codex_auth_home']), payload,
                                               selected['thread']), 'canonical_rollout_path_required')
                continue
            event = payload.get('type') if isinstance(payload, dict) else None
            if kind == 'event_msg' and event == 'task_started':
                if payload.get('turn_id') == selected['turn']:
                    known_fields(payload, ('type', 'turn_id', 'root_turn_id', 'started_at',
                                           'model_context_window', 'collaboration_mode_kind'))
                    require(starts == 0, 'duplicate_selected_turn')
                    starts, active = 1, True
                elif starts:
                    raise AuditDenied('selected_turn_not_latest')
                continue
            if not active:
                # Historical content is transported opaquely, not audited as this turn.
                continue
            require(isinstance(payload, dict), 'unknown_selected_turn_record')
            for key, expected in (('turn_id', selected['turn']), ('thread_id', selected['thread'])):
                require(key not in payload or payload[key] == expected, 'selected_turn_identity_mismatch')
            counts[kind + ':' + str(event or '')] = counts.get(kind + ':' + str(event or ''), 0) + 1
            if kind == 'response_item':
                require(not completes, 'record_after_selected_completion')
                item = payload.get('type')
                if item == 'message':
                    require(payload.get('role') in ('user', 'developer', 'system'), 'unexpected_assistant_output')
                else:
                    # Reasoning is not an execution record; no reasoning fields are inspected.
                    require(item == 'reasoning', 'native_effect_or_unknown_response_item')
            elif kind == 'event_msg':
                if event == 'task_complete':
                    known_fields(payload, ('type', 'turn_id', 'root_turn_id', 'last_agent_message', 'error',
                        'started_at', 'completed_at', 'duration_ms', 'time_to_first_token_ms'))
                    require(not completes and payload.get('turn_id') == selected['turn'], 'completion_identity_invalid')
                    require('last_agent_message' in payload and payload['last_agent_message'] is None,
                            'unexpected_final_answer')
                    error = payload.get('error')
                    require(isinstance(error, dict) and error.get('codex_error_info') == 'usage_limit_exceeded',
                            'not_proven_quota_failure')
                    known_fields(error, ('message', 'codex_error_info', 'additional_details'))
                    require(error.get('additional_details') is None, 'unknown_quota_error_details')
                    completes = 1
                elif event in ('item_started', 'item_completed'):
                    known_fields(payload, ('type', 'thread_id', 'turn_id', 'item', 'started_at_ms', 'completed_at_ms'))
                    require(not completes and isinstance(payload.get('item'), dict), 'item_metadata_invalid')
                    require(payload['item'].get('type') in ('UserMessage', 'Reasoning'),
                            'native_effect_or_unknown_event_item')
                else:
                    require(event in ('token_count', 'agent_reasoning', 'agent_reasoning_raw_content'),
                            'native_effect_or_unknown_event')
                    if event == 'token_count':
                        known_fields(payload, ('type', 'info', 'rate_limits'))
                    else:
                        known_fields(payload, ('type', 'text', 'turn_id', 'thread_id'))
            else:
                require(not completes and kind in ('turn_context', 'world_state', 'token_usage_record'),
                        'native_effect_or_unknown_record')
        require(file_version(os.fstat(source.fileno())) == file_version(before), 'rollout_changed')
    require(file_version(path.lstat()) == file_version(before), 'rollout_changed')
    require(starts == 1 and completes == 1, 'selected_turn_incomplete')
    return {'sha256': digest.hexdigest(), 'size': size, 'version': file_version(before),
            'turn_id': selected['turn'], 'thread_id': selected['thread'],
            'error_category': 'usage_limit_exceeded', 'execution_records': 0, 'counts': counts}


def repeat_digest(path, proof):
    require(file_version(path.lstat()) == proof['version'], 'rollout_changed')
    digest = hashlib.sha256()
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        require(file_version(os.fstat(source.fileno())) == proof['version'], 'rollout_changed')
        while True:
            block = source.read(65536)
            if not block:
                break
            digest.update(block)
        require(file_version(os.fstat(source.fileno())) == proof['version'], 'rollout_changed')
    require(file_version(path.lstat()) == proof['version'] and digest.hexdigest() == proof['sha256'],
            'rollout_changed')


def release(auth, selected, path, proof, before_transaction=None):
    """Stage first; CAS revalidate, publish history, audit and release atomically."""
    task = selected['task']
    with tempfile.TemporaryFile(prefix='native-quota-snapshot-', dir=str(auth['database'].parent)) as packed:
        with gzip.GzipFile(filename='', fileobj=packed, mode='wb', mtime=0) as archive:
            staged = inspect_rollout(path, selected, archive=archive)
        require(staged == proof, 'rollout_changed')
        packed.flush()
        os.fsync(packed.fileno())
        if before_transaction:
            before_transaction()  # Test-only race injection; never a CLI/user hook.
        checked_path(str(auth['database']), file=True, private=True)
        store = Store.__new__(Store)  # No Store.__init__: no migrations or inflight recovery.
        store.path, store.clock = str(auth['database']), time.time
        artifact = hashlib.sha256(json.dumps([NAMESPACE, task['id'], task['epoch'], proof['sha256']],
                                             separators=(',', ':')).encode()).hexdigest()
        with store.transaction() as db:
            current = snapshot(db, task['id'], task['epoch'], auth)
            require(current == selected, 'task_or_session_changed')
            repeat_digest(path, proof)
            require(not db.execute('SELECT 1 FROM session_chunks WHERE id=? LIMIT 1', (artifact,)).fetchone(),
                    'artifact_namespace_conflict')
            db.execute('''CREATE TABLE IF NOT EXISTS native_quota_reconciliations(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, scope TEXT NOT NULL,
                expected_epoch INTEGER NOT NULL, released_epoch INTEGER NOT NULL,
                before_task TEXT NOT NULL, before_session TEXT NOT NULL,
                artifact TEXT NOT NULL, old_artifact TEXT, rollout_sha256 TEXT NOT NULL,
                rollout_size INTEGER NOT NULL, source_path TEXT NOT NULL,
                error_category TEXT NOT NULL, proof TEXT NOT NULL, created REAL NOT NULL)''')
            require(not db.execute('SELECT 1 FROM native_quota_reconciliations WHERE id=?', (artifact,)).fetchone(),
                    'audit_namespace_conflict')
            packed.seek(0)
            parts = 0
            while True:
                block = packed.read(65536)
                if not block:
                    break
                db.execute('INSERT INTO session_chunks(id,part,body) VALUES(?,?,?)', (artifact, parts, block))
                parts += 1
            require(parts > 0, 'snapshot_empty')
            # Recheck after insertion as well, before either session/task promotion.
            repeat_digest(path, proof)
            checkpoint = dict(selected['checkpoint'], side_effect_started=False)
            state = dict(checkpoint)  # Original identity and failed turn metadata survive.
            previous = selected['session']
            changed = db.execute('''UPDATE native_sessions SET state=?,artifact=?,updated=?
                WHERE scope=? AND harness='codex' AND node=? AND state=? AND artifact IS ? AND updated=?''',
                (json.dumps(state), artifact, time.time(), task['scope'], previous['node'],
                 previous['state'], previous['artifact'], previous['updated'])).rowcount
            require(changed == 1, 'session_cas_failed')
            before_task = {key: task[key] for key in ('id', 'scope', 'status', 'node', 'epoch', 'deadline',
                                                      'checkpoint', 'result', 'paused_status', 'paused_deadline')}
            db.execute('INSERT INTO native_quota_reconciliations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (artifact, task['id'], task['scope'], task['epoch'], task['epoch'] + 1,
                 json.dumps(before_task), json.dumps(previous), artifact, previous['artifact'],
                 proof['sha256'], proof['size'], str(path), proof['error_category'],
                 json.dumps(proof), time.time()))
            changed = db.execute('''UPDATE tasks SET status='pending',epoch=epoch+1,node=NULL,deadline=NULL,
                checkpoint=?,result=NULL,paused_status=NULL,paused_deadline=NULL
                WHERE id=? AND status='needs_review' AND epoch=? AND node IS ? AND checkpoint=? AND scope=?''',
                (json.dumps(checkpoint), task['id'], task['epoch'], task['node'], task['checkpoint'], task['scope'])).rowcount
            require(changed == 1, 'task_cas_failed')
    return {'applied': True, 'task_id': task['id'], 'status': 'pending', 'epoch': task['epoch'] + 1,
            'artifact': artifact, 'rollout_sha256': proof['sha256'], 'audit_id': artifact}


def reconcile(worker_config, server_config, task_id, rollout_path, expected_epoch=None, apply=False):
    require(isinstance(task_id, str) and re.fullmatch(r'[0-9a-f]{64}', task_id), 'explicit_task_id_required')
    require(expected_epoch is None or (isinstance(expected_epoch, int) and not isinstance(expected_epoch, bool)
                                      and expected_epoch >= 0), 'invalid_expected_epoch')
    require(not apply or expected_epoch is not None, 'apply_expected_epoch_required')
    auth = authority(worker_config, server_config)
    with contextlib.closing(readonly(auth['database'])) as db:
        selected = snapshot(db, task_id, expected_epoch, auth)
    path = canonical_rollout(rollout_path, selected)
    proof = inspect_rollout(path, selected)
    if apply:
        return release(auth, selected, path, proof)
    return {'applied': False, 'eligible': True, 'task_id': task_id, 'epoch': selected['task']['epoch'],
            'node': auth['node'], 'thread_id': selected['thread'], 'turn_id': selected['turn'],
            'error_category': proof['error_category'], 'execution_records': 0,
            'rollout_sha256': proof['sha256'], 'rollout_size': proof['size'],
            'complete_history_will_be_published': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-config', required=True)
    parser.add_argument('--server-config', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--rollout-path', required=True)
    parser.add_argument('--expected-epoch', type=int)
    parser.add_argument('--apply', action='store_true', help='Publish full history and release ONLY this fenced task.')
    args = parser.parse_args(argv)
    try:
        answer = reconcile(args.worker_config, args.server_config, args.task_id, args.rollout_path,
                           expected_epoch=args.expected_epoch, apply=args.apply)
    except AuditDenied as exc:
        print(json.dumps({'applied': False, 'eligible': False, 'code': str(exc)}))
        return 2
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print(json.dumps({'applied': False, 'eligible': False, 'code': 'audit_input_or_storage_invalid'}))
        return 2
    print(json.dumps(answer, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
