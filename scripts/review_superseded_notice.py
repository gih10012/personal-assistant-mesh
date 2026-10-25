"""Owner-only closure of ONE superseded AA relay notice, retaining UNKNOWN.

Default: read-only task/session review and zero-turn native metadata queries.
--apply requires the exact review digest and epoch from that review. This does
not deliver a notice, resume a Goal, requeue input, or establish zero effects.
Python 3.6 compatible. All beforeimages stay in the private authority ledger.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from assistant_mesh.native_goal_observer import NativeGoalObserver
from assistant_mesh.store import Store
from assistant_mesh.superseded_notice import CLOSED_CASE, EPOCH, PROTOCOL, TABLE


_spec = importlib.util.spec_from_file_location(
    '_superseded_notice_effect_helpers', str(Path(__file__).with_name('review_native_effects.py')))
review = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review)
quota = review.quota
require = review.require

SERVICE = 'assistant-mesh-cloud-worker.service'
# Opaque pins bind the independently reviewed original case. Private CLI
# arguments cannot widen this into a general unknown-effect/guard cleaner.
REASON = ('This old AA model/effort relay notice was superseded by the later '
          'owner-authorized complete Goal and actual governance/role synchronization. '
          'The original native_finish disconnect has no verified natural completion. '
          'Its native outcome and possible effects remain UNKNOWN. No successful '
          'delivery or GUI input is claimed; no retry or old-input replay is authorized.')


def pinned(field, value):
    require(isinstance(value, str) and review.sha(value.encode('utf8')) == CLOSED_CASE[field],
            'closed_notice_' + field + '_mismatch')


def stopped_worker():
    """Fixed local service only; never stop/restart it or accept a command hook."""
    result = subprocess.run(['/usr/bin/systemctl', '--user', 'show', SERVICE, '--no-pager',
                             '--property=ActiveState,SubState,MainPID'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            universal_newlines=True, timeout=10)
    require(result.returncode == 0, 'worker_service_observation_failed')
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition('=')
        require(separator and key not in values, 'worker_service_observation_invalid')
        values[key] = value
    require(set(values) == {'ActiveState', 'SubState', 'MainPID'}
            and values['ActiveState'] == 'inactive' and values['MainPID'] == '0'
            and values['SubState'] == 'dead', 'worker_service_must_be_inactive_pid0')
    return values


class NoticeMetadataObserver(NativeGoalObserver):
    # Restrict this owned observer, never the owner's native session or tools.
    READ_METHODS = frozenset(('initialize', 'thread/read', 'thread/goal/get'))


def native_metadata(executable, auth_home, thread_id, rollout_path):
    """Only initialize + original thread/read + goal/get. No resume/start/model."""
    observer = NoticeMetadataObserver({'executable': str(executable),
        'auth_home': auth_home, 'native_plan_tool': False}, thread_id,
        tick=None, deadline=time.monotonic() + 30)
    try:
        thread = observer.read_thread(str(rollout_path), include_turns=False)
        response = observer.rpc('thread/goal/get', {'threadId': thread_id})
        require(isinstance(response, dict) and 'goal' in response
                and response['goal'] is None, 'original_native_goal_must_be_absent')
        return {'thread_id': thread['id'], 'path': thread['path'],
                'status': thread['status'], 'goal': None}
    finally:
        observer.close()  # Reap only this newly owned metadata observer.


def selected_state(db, auth, task_id):
    require(not db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                           ('table', TABLE)).fetchone(), 'notice_already_reviewed_readback_required')
    selected = quota.snapshot(db, task_id, EPOCH, auth)
    task, checkpoint = selected['task'], selected['checkpoint']
    pinned('input', task['input'])
    pinned('thread', selected['thread'])
    pinned('turn', selected['turn'])
    pinned('auth_home', checkpoint['codex_auth_home'])
    require(task['scope'] == 'leader:owner' and task['parent_id'] is None
            and review.parse(task['required']) == ['leader'], 'closed_notice_scope_or_parent_mismatch')
    context = review.parse(task['context'])
    origin = context.get('origin') if isinstance(context, dict) else None
    require(isinstance(origin, dict) and origin.get('kind') == 'entrance-handoff'
            and origin.get('message_id') == task_id and origin.get('event_id') == task_id,
            'closed_notice_origin_mismatch')
    failure = checkpoint.get('runtime_failure')
    require(isinstance(failure, dict) and failure.get('phase') == 'native_finish'
            and failure.get('category') == 'codex_disconnected'
            and failure.get('outcome') == 'unknown' and failure.get('attempt_epoch') == EPOCH,
            'closed_notice_unknown_failure_required')
    require(not db.execute("SELECT 1 FROM tasks WHERE scope=? AND status='running' LIMIT 1",
                           (task['scope'],)).fetchone(), 'scope_has_running_task')
    require(not db.execute('SELECT 1 FROM tasks WHERE parent_id=? LIMIT 1',
                           (task_id,)).fetchone(), 'closed_notice_has_children')
    require(not db.execute('SELECT 1 FROM agent_actions WHERE substr(id,1,?)=? LIMIT 1',
                           (len(task_id), task_id)).fetchone(), 'closed_notice_has_agent_actions')
    return selected


def source_snapshot(worker_config, server_config, rollout_path, selected):
    """Bind original profile and local source generations, without exporting data."""
    checkpoint = selected['checkpoint']
    home = Path(checkpoint['codex_auth_home'])
    path = quota.canonical_rollout(rollout_path, selected)
    rollout = review.file_metadata(path, private=True)
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        raw = source.readline(quota.MAX_LINE + 1)
    require(len(raw) <= quota.MAX_LINE, 'rollout_metadata_size_bound')
    first = review.parse(raw)
    require(isinstance(first, dict) and first.get('type') == 'session_meta'
            and isinstance(first.get('payload'), dict), 'original_rollout_metadata_required')
    require(path in quota.metadata_paths(home, first['payload'], selected['thread']),
            'original_rollout_path_mismatch')
    require(review.file_metadata(path, private=True) == rollout, 'rollout_generation_changed')
    worker = quota.private_config(worker_config)
    executable = (worker.get('codex') or {}).get('executable')
    require(isinstance(executable, str) and Path(executable).is_absolute(),
            'explicit_worker_native_executable_required')
    executable = quota.checked_path(executable, file=True)
    # Only source/profile files relevant to this single-row operation. No
    # full DB/history backup, restoration, or new verification framework.
    package = Path(__file__).resolve().parents[1] / 'assistant_mesh'
    public_sources = [Path(__file__).absolute(), Path(review.__file__).absolute(),
        Path(quota.__file__).absolute()] + [package / name for name in
        ('store.py', 'native_goal_observer.py', 'codex.py', 'config.py',
         'codex_provider.py', 'native_ws.py', 'superseded_notice.py')]
    private_sources = [Path(worker_config), Path(server_config), home / 'auth.json', home / 'config.toml']
    files = {}
    for source in public_sources + private_sources:
        unused, metadata = review.read_checked(source, private=source in private_sources)
        files[str(source)] = metadata
    metadata = review.file_metadata(executable)
    require(metadata['size'] <= 512 * 1024 * 1024, 'native_executable_size_bound')
    checksum = hashlib.sha256()
    descriptor = os.open(str(executable), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            checksum.update(chunk)
    require(review.file_metadata(executable) == metadata, 'native_executable_generation_changed')
    files[str(executable)] = dict(metadata, sha256=checksum.hexdigest())
    return {'files': files, 'rollout_path': str(path), 'rollout': rollout,
            'executable': str(executable)}


def inspect_notice(worker_config, server_config, task_id):
    auth = quota.authority(worker_config, server_config)
    service = stopped_worker()  # Required BEFORE even the metadata process.
    with contextlib.closing(quota.readonly(auth['database'])) as db:
        selected = selected_state(db, auth, task_id)
    return auth, service, selected


def review_context(worker_config, server_config, task_id, rollout_path):
    auth, service, selected = inspect_notice(worker_config, server_config, task_id)
    sources = source_snapshot(worker_config, server_config, rollout_path, selected)
    native = native_metadata(sources['executable'], selected['checkpoint']['codex_auth_home'],
                             selected['thread'], sources['rollout_path'])
    require(native == {'thread_id': selected['thread'], 'path': sources['rollout_path'],
                       'status': {'type': 'notLoaded'}, 'goal': None},
            'original_native_metadata_changed')
    require(source_snapshot(worker_config, server_config, rollout_path, selected) == sources,
            'review_source_generation_changed')
    database = review.file_metadata(auth['database'], private=True)
    proof = {'protocol': PROTOCOL, 'task': selected['task'], 'session': selected['session'],
             'service': service, 'native': native, 'sources': sources,
             'database_identity': {k: database[k] for k in ('dev', 'ino', 'uid', 'mode')},
             'reason': REASON, 'input_sha256': CLOSED_CASE['input']}
    return auth, selected, proof, review.digest(proof)


def create_review_table(db):
    # This is the one case's private append-only beforeimage and receipt, not a
    # general review framework. Namespace reuse requires readback, never CAS.
    db.execute('''CREATE TABLE superseded_notice_reviews(
        task_id TEXT PRIMARY KEY, source_epoch INTEGER NOT NULL, closed_epoch INTEGER NOT NULL,
        review_sha256 TEXT NOT NULL, before_task TEXT NOT NULL, before_session TEXT NOT NULL,
        proof TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL)''')
    for action in ('UPDATE', 'DELETE'):
        db.execute('CREATE TRIGGER superseded_notice_reviews_no_' + action.lower() +
                   ' BEFORE ' + action + ' ON superseded_notice_reviews '
                   "BEGIN SELECT RAISE(ABORT,'notice review is append only'); END")


def close_notice(worker_config, server_config, task_id, rollout_path, expected_epoch=None,
                 expected_review_digest=None, apply=False):
    pinned('task', task_id)
    require(expected_epoch is None or (type(expected_epoch) is int and expected_epoch == EPOCH),
            'closed_notice_expected_epoch_mismatch')
    require(not apply or expected_epoch == EPOCH, 'apply_explicit_epoch_required')
    require(not apply or (isinstance(expected_review_digest, str)
            and re.fullmatch('[0-9a-f]{64}', expected_review_digest)), 'apply_explicit_review_digest_required')
    auth, selected, proof, checksum = review_context(worker_config, server_config, task_id, rollout_path)
    answer = {'protocol': PROTOCOL, 'task_id': task_id, 'source_epoch': EPOCH,
              'review_digest': checksum, 'native_outcome': 'unknown',
              'effects_zero_verified': False, 'original_input_requeued': False,
              'retry_authorized': False, 'notice_delivery_verified': False,
              'checkpoint_preserved': True, 'side_effect_started': True,
              'native_session_changed': False, 'history_changed': False}
    if not apply:
        return dict(answer, applied=False, eligible=True, reason=REASON)
    require(expected_review_digest == checksum, 'review_digest_changed')
    receipt = dict(answer, status='failed', disposition='deferred', resolution='superseded',
                   closed_epoch=EPOCH + 1, reason=REASON)
    store = Store.__new__(Store)  # Never migrate or recover unrelated inflight work.
    store.path, store.clock = str(auth['database']), time.time
    with store.transaction() as db:
        current = selected_state(db, auth, task_id)
        require(current == selected, 'task_or_session_cas_changed')
        require(stopped_worker() == proof['service'], 'worker_service_generation_changed')
        require(source_snapshot(worker_config, server_config, rollout_path, selected) == proof['sources'],
                'review_source_generation_changed')
        database = review.file_metadata(auth['database'], private=True)
        require({k: database[k] for k in ('dev', 'ino', 'uid', 'mode')} == proof['database_identity'],
                'authority_database_identity_changed')
        create_review_table(db)
        db.execute('INSERT INTO superseded_notice_reviews VALUES(?,?,?,?,?,?,?,?,?)',
                   (task_id, EPOCH, EPOCH + 1, checksum, json.dumps(selected['task']),
                    json.dumps(selected['session']), json.dumps(proof), json.dumps(receipt), time.time()))
        task = selected['task']
        changed = db.execute('''UPDATE tasks SET status='failed',epoch=epoch+1,result=?
            WHERE id=? AND epoch=? AND status='needs_review' AND node IS ? AND scope=?
            AND checkpoint=? AND input=? AND context=? AND result IS ?''',
            (json.dumps(receipt), task_id, EPOCH, task['node'], task['scope'],
             task['checkpoint'], task['input'], task['context'], task['result'])).rowcount
        require(changed == 1, 'original_task_cas_failed')
        # checkpoint (including the true guard), input/context, session/state,
        # artifacts and every history chunk remain byte-for-byte unchanged.
    return dict(receipt, applied=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-config', required=True)
    parser.add_argument('--server-config', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--rollout-path', required=True)
    parser.add_argument('--expected-epoch', type=int)
    parser.add_argument('--expected-review-digest')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        answer = close_notice(args.worker_config, args.server_config, args.task_id,
            args.rollout_path, args.expected_epoch, args.expected_review_digest, args.apply)
    except (review.ReviewDenied, quota.AuditDenied) as exc:
        print(json.dumps({'applied': False, 'eligible': False, 'code': str(exc)}))
        return 2
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
        print(json.dumps({'applied': False, 'eligible': False, 'code': 'notice_review_input_or_observation_invalid'}))
        return 2
    print(json.dumps(answer, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
