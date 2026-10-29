"""Owner-reviewed local progress -> explicit next turn of the SAME native task.

This is a maintenance authority, not a model tool or a Shell safety classifier.
Byte hashes prove binding/integrity. Effect and process closure claims are the
named maintainer's reviewed evidence, never cryptographic proof of quiescence.
"""
import base64
import contextlib
import gzip
import hashlib
import io
import json
import sqlite3
import uuid
from urllib.parse import quote

from . import operation_isolation as isolation
from .store import Conflict


PROTOCOL = 'owner-native-partial-continuation/1'
PREFIX = '/v1/maintenance/native-partial/'
MAX_HISTORY = 64 * 1024 * 1024
MAX_PACKED = 16 * 1024 * 1024
MAX_LINE = 8 * 1024 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf8')).hexdigest()


def require(value, code):
    if not value:
        raise Conflict('native_partial_' + code)


@contextlib.contextmanager
def _readonly(store):
    db = sqlite3.connect('file:' + quote(str(store.path), safe='/') + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def shape(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields), 'invalid_shape')


def name(value):
    require(isinstance(value, str) and value.strip() and len(value.encode('utf8')) <= 256
            and not any(ord(c) < 32 for c in value), 'invalid_identity')


def sha(value):
    require(isinstance(value, str) and len(value) == 64
            and all(c in '0123456789abcdef' for c in value), 'invalid_sha256')


def integer(value, minimum=1, maximum=(1 << 63) - 1):
    require(type(value) is int and minimum <= value <= maximum, 'invalid_integer')


def native_id(value):
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    require(valid, 'invalid_native_identity')


def maintainer(peer):
    if not isinstance(peer, dict) or peer.get('role') != 'native_maintenance':
        raise PermissionError('native_partial_maintenance_identity_required')
    reviewer = peer.get('reviewer_id')
    name(reviewer)
    return reviewer


def binding(value):
    shape(value, ('acceptance_id', 'task_id', 'expected_epoch', 'thread_id', 'turn_id', 'sha256', 'size'))
    name(value['acceptance_id']); name(value['task_id']); integer(value['expected_epoch'])
    native_id(value['thread_id']); native_id(value['turn_id']); sha(value['sha256'])
    integer(value['size'], maximum=MAX_HISTORY)
    return value


def artifact_id(value):
    return digest([PROTOCOL, 'complete-history', binding(value)])


def tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS native_partial_acceptances(
        acceptance_id TEXT PRIMARY KEY, reviewer TEXT NOT NULL, fingerprint TEXT NOT NULL,
        task_id TEXT NOT NULL, source_epoch INTEGER NOT NULL, before_task TEXT NOT NULL,
        before_session TEXT NOT NULL, before_artifact TEXT NOT NULL,
        specification TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL,
        UNIQUE(task_id,source_epoch))''')
    db.execute('''CREATE TABLE IF NOT EXISTS native_partial_before_chunks(
        acceptance_id TEXT NOT NULL,artifact_id TEXT NOT NULL,part INTEGER NOT NULL,
        body BLOB NOT NULL,PRIMARY KEY(acceptance_id,artifact_id,part))''')
    db.execute('''CREATE TABLE IF NOT EXISTS native_partial_history_parts(
        artifact_id TEXT NOT NULL,part INTEGER NOT NULL,body BLOB NOT NULL,
        PRIMARY KEY(artifact_id,part))''')
    # Dedicated staged parts are immutable too. Ordinary /session uploads can
    # neither overwrite these nor prepopulate this maintenance namespace.
    for table in ('native_partial_acceptances', 'native_partial_before_chunks', 'native_partial_history_parts'):
        for action in ('UPDATE', 'DELETE'):
            db.execute('CREATE TRIGGER IF NOT EXISTS ' + table + '_no_' + action.lower() +
                       ' BEFORE ' + action + ' ON ' + table +
                       " BEGIN SELECT RAISE(ABORT,'native partial receipts are append only'); END")


def _selected(store, db, task_id):
    row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    require(row is not None, 'task_missing')
    checkpoint, context = json.loads(row['checkpoint']), json.loads(row['context'])
    require(isinstance(checkpoint, dict) and checkpoint.get('harness', 'codex') == 'codex', 'codex_required')
    require(not context.get('remote_proxy'), 'remote_proxy_requires_original_protocol')
    native_id(checkpoint.get('thread_id')); native_id(checkpoint.get('turn_id'))
    scope = isolation.native_scope(db, row, Conflict)
    session = db.execute("SELECT * FROM native_sessions WHERE scope=? AND harness='codex'", (scope,)).fetchone()
    require(session is not None, 'original_session_required')
    state = json.loads(session['state'])
    require(state.get('thread_id') == checkpoint['thread_id']
            and state.get('codex_node') == checkpoint.get('codex_node') == session['node']
            and state.get('codex_auth_home') == checkpoint.get('codex_auth_home'), 'original_session_changed')
    name(checkpoint.get('codex_auth_home')); name(checkpoint.get('codex_node'))
    parts = ([dict(part) for part in db.execute('SELECT part,body FROM session_chunks WHERE id=? ORDER BY part',
                                             (session['artifact'],))] if session['artifact'] else [])
    require([part['part'] for part in parts] == list(range(len(parts))), 'old_artifact_incomplete')
    require(not session['artifact'] or parts, 'old_artifact_missing')
    old = {'artifact_id': session['artifact'], 'parts': len(parts),
           'sha256': hashlib.sha256(b''.join(part['body'] for part in parts)).hexdigest()}
    return row, checkpoint, session, state, old, parts


def inspect(store, task_id, peer):
    maintainer(peer); name(task_id)
    with store.transaction() as db:
        row, checkpoint, session, state, old, _ = _selected(store, db, task_id)
        return {'protocol': PROTOCOL, 'task_id': row['id'], 'status': row['status'], 'epoch': row['epoch'],
                'thread_id': checkpoint['thread_id'], 'turn_id': checkpoint['turn_id'],
                'node': checkpoint['codex_node'], 'auth_home': checkpoint['codex_auth_home'],
                'operation_id': checkpoint.get('operation_id') or json.loads(row['context']).get('operation_id') or row['id'],
                'expected': {'task_sha256': digest(dict(row)), 'session_sha256': digest(dict(session)),
                             'artifact': old}, 'goal': checkpoint.get('goal'),
                'ordinary_worker_authorized': False}


def upload(store, payload, peer):
    maintainer(peer)
    shape(payload, ('binding', 'part', 'data'))
    source = binding(payload['binding']); integer(payload['part'], 0, MAX_PACKED // 65536)
    require(isinstance(payload['data'], str), 'invalid_part')
    try:
        data = base64.b64decode(payload['data'], validate=True)
    except (ValueError, TypeError):
        raise Conflict('native_partial_invalid_part') from None
    require(0 < len(data) <= 65536, 'invalid_part')
    artifact = artifact_id(source)
    with store.transaction() as db:
        tables(db)
        old = db.execute('SELECT body FROM native_partial_history_parts WHERE artifact_id=? AND part=?',
                         (artifact, payload['part'])).fetchone()
        require(old is None or old['body'] == data, 'part_content_conflict')
        total = db.execute('SELECT COALESCE(SUM(length(body)),0) FROM native_partial_history_parts WHERE artifact_id=?',
                           (artifact,)).fetchone()[0]
        require(total + (0 if old else len(data)) <= MAX_PACKED, 'packed_history_budget')
        db.execute('INSERT OR IGNORE INTO native_partial_history_parts VALUES(?,?,?)',
                   (artifact, payload['part'], data))
    return {'artifact_id': artifact, 'part': payload['part'], 'retained': True, 'task_changed': False}


def _history(source, count, parts):
    integer(count, maximum=MAX_PACKED // 65536 + 1)
    require(len(parts) == count and [row['part'] for row in parts] == list(range(count)), 'history_parts_incomplete')
    packed = b''.join(row['body'] for row in parts)
    require(len(packed) <= MAX_PACKED, 'packed_history_budget')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as archive:
            raw = archive.read(source['size'] + 1)
    except (OSError, EOFError, ValueError):
        raise Conflict('native_partial_history_invalid') from None
    require(len(raw) == source['size'] and hashlib.sha256(raw).hexdigest() == source['sha256'], 'history_hash_or_size_changed')
    return raw, parts


def _prior_history(raw, old_parts):
    if not old_parts:
        return
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(b''.join(part['body'] for part in old_parts))) as archive:
            prior = archive.read(MAX_HISTORY + 1)
    except (OSError, EOFError, ValueError):
        raise Conflict('native_partial_prior_history_invalid') from None
    require(len(prior) <= MAX_HISTORY and raw.startswith(prior), 'complete_prior_history_required')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_history_json_key')
        result[key] = value
    return result


def history_receipts(raw, thread, turn):
    """Bind complete native bytes and receipt pairing; do not interpret Shell."""
    require(raw.endswith(b'\n'), 'history_incomplete_line')
    active, starts, completes, calls, outputs = False, 0, 0, {}, {}
    started_items = set()
    event_tools = set()
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        require(len(line) <= MAX_LINE, 'history_line_budget')
        try:
            record = json.loads(line.decode('utf8'), object_pairs_hook=_unique)
        except (ValueError, UnicodeError):
            raise Conflict('native_partial_history_json_invalid') from None
        require(isinstance(record, dict) and isinstance(record.get('payload'), dict), 'history_record_invalid')
        kind, item = record.get('type'), record['payload']
        event = item.get('type')
        if index == 0:
            require(kind == 'session_meta' and item.get('id') == thread, 'history_thread_changed')
            continue
        if kind == 'event_msg' and event == 'task_started':
            require(not starts, 'selected_turn_not_latest')
            if item.get('turn_id') == turn:
                starts, active = 1, True
            continue
        if not active:
            continue  # Prior turns are retained in full, never rewritten.
        require(not completes, 'record_after_completion')
        require(item.get('turn_id', turn) == turn and item.get('thread_id', thread) == thread, 'history_binding_changed')
        if kind == 'event_msg':
            if event == 'task_complete':
                require(item.get('turn_id') == turn and not started_items, 'incomplete_native_items')
                completes += 1
            elif event in ('item_started', 'item_completed'):
                native = item.get('item'); require(isinstance(native, dict), 'unknown_native_item')
                key = native.get('id')
                if native.get('type') not in ('UserMessage', 'AgentMessage', 'Reasoning', 'Plan', 'ContextCompaction'):
                    tool = native.get('call_id') or key
                    name(tool); event_tools.add(tool)
                if key is not None:
                    name(key)
                    if event == 'item_started':
                        require(key not in started_items, 'duplicate_native_item'); started_items.add(key)
                    else:
                        started_items.discard(key)
            else:
                require(event in ('token_count', 'agent_reasoning', 'agent_reasoning_raw_content',
                                  'thread_settings_applied', 'agent_message'), 'unknown_native_event')
        elif kind == 'response_item':
            if event in ('custom_tool_call', 'function_call'):
                call = item.get('call_id'); name(call)
                require(call not in calls, 'duplicate_tool_call'); calls[call] = hashlib.sha256(line).hexdigest()
            elif event in ('custom_tool_call_output', 'function_call_output'):
                call = item.get('call_id'); name(call)
                require(call in calls and call not in outputs, 'unmatched_tool_output')
                outputs[call] = hashlib.sha256(line).hexdigest()
            else:
                require(event in ('message', 'reasoning', 'additional_tools'), 'unknown_response_item')
        else:
            require(kind in ('turn_context', 'world_state', 'token_usage_record', 'compacted'), 'unknown_native_record')
    require(starts == completes == 1 and set(calls) == set(outputs)
            and event_tools <= set(calls), 'turn_or_tool_incomplete')
    return {key: {'call_sha256': calls[key], 'output_sha256': outputs[key]} for key in calls}


def _closure(value, source, checkpoint):
    shape(value, ('node', 'auth_home', 'thread_id', 'turn_id', 'runner_id', 'runner_pid', 'runner_birth', 'runtime_work_sha256',
                  'kind', 'native_owner_live', 'owned_children_closed', 'forced_cleanup',
                  'goal_snapshot_sha256', 'goal_controller_active', 'evidence_ref', 'evidence_sha256'))
    for field in ('node', 'auth_home', 'runner_id', 'runner_birth', 'evidence_ref'):
        name(value[field])
    integer(value['runner_pid']); sha(value['runtime_work_sha256']); sha(value['evidence_sha256'])
    require(value['node'] == checkpoint['codex_node'] and value['auth_home'] == checkpoint['codex_auth_home'], 'closure_owner_changed')
    require(value['thread_id'] == source['thread_id'] and value['turn_id'] == source['turn_id'], 'closure_native_changed')
    require(value['goal_controller_active'] is False and value['goal_snapshot_sha256'] == digest(checkpoint.get('goal')),
            'closure_goal_changed')
    require(value['kind'] == 'owner-observed-native-runtime-closed' and value['native_owner_live'] is False
            and value['owned_children_closed'] is True and value['forced_cleanup'] is False, 'runtime_not_proven_closed')


def _progress(value, operation, receipts):
    require(isinstance(value, list) and len(value) <= 512, 'progress_invalid')
    reviewed = set()
    for item in value:
        shape(item, ('tool_call_id', 'operation_id', 'call_sha256', 'output_sha256', 'effect_kind',
                     'unresolved_external_effects', 'resource', 'evidence_ref', 'observation'))
        call = item['tool_call_id']; name(call); name(item['resource']); name(item['evidence_ref'])
        require(call not in reviewed and call in receipts and item['operation_id'] == operation, 'progress_operation_changed')
        require(item['call_sha256'] == receipts[call]['call_sha256'] and item['output_sha256'] == receipts[call]['output_sha256'], 'original_tool_receipt_changed')
        require(item['effect_kind'] in ('owner-reviewed-read-only', 'owner-reviewed-local-progress')
                and item['unresolved_external_effects'] is False, 'external_effect_requires_original_reconciliation')
        require(isinstance(item['observation'], dict) and len(encoded(item['observation']).encode()) <= 8192, 'observation_invalid')
        reviewed.add(call)
    require(reviewed == set(receipts), 'all_tool_receipts_must_be_reviewed')


def acceptance(store, payload, peer):
    reviewer = maintainer(peer)
    shape(payload, ('protocol', 'binding', 'expected', 'parts', 'operation_id', 'closure', 'known_local_progress',
                    'disposition', 'instruction', 'result'))
    require(payload['protocol'] == PROTOCOL, 'protocol_mismatch')
    source = binding(payload['binding']); name(payload['operation_id'])
    expected = payload['expected']; shape(expected, ('task_sha256', 'session_sha256', 'artifact'))
    sha(expected['task_sha256']); sha(expected['session_sha256'])
    disposition, instruction, result = payload['disposition'], payload['instruction'], payload['result']
    require(disposition in ('continue_remaining', 'owner_reviewed_completion'), 'disposition_invalid')
    require(isinstance(instruction, str) and len(instruction.encode('utf8')) <= 32768
            and not any(ord(c) < 32 and c not in '\n\t' for c in instruction), 'instruction_invalid')
    if disposition == 'continue_remaining':
        require(instruction.strip() and result is None, 'remaining_instruction_required')
    else:
        require(not instruction and isinstance(result, str) and result.strip()
                and len(result.encode('utf8')) <= 65536, 'completion_result_required')
    fingerprint = digest([reviewer, payload])
    # Materialize immutable staged bytes in a read snapshot; parsing, gzip and
    # per-tool review validation never hold the authority's write transaction.
    with _readonly(store) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_partial_acceptances'").fetchone()
        previous = db.execute('SELECT * FROM native_partial_acceptances WHERE acceptance_id=?', (source['acceptance_id'],)).fetchone() if exists else None
        if previous:
            require(previous['fingerprint'] == fingerprint, 'acceptance_id_content_conflict')
            return json.loads(previous['receipt'])
        require(exists, 'history_not_staged')
        prepared_task, _, prepared_session, _, prepared_artifact, prior_parts = _selected(store, db, source['task_id'])
        require(digest(dict(prepared_task)) == expected['task_sha256']
                and digest(dict(prepared_session)) == expected['session_sha256']
                and prepared_artifact == expected['artifact'], 'task_session_or_artifact_cas_changed')
        prepared = [dict(row) for row in db.execute('SELECT part,body FROM native_partial_history_parts WHERE artifact_id=? ORDER BY part',
                                                  (artifact_id(source),))]
    raw, parts = _history(source, payload['parts'], prepared)
    _prior_history(raw, prior_parts)
    receipts = history_receipts(raw, source['thread_id'], source['turn_id'])
    _progress(payload['known_local_progress'], payload['operation_id'], receipts)
    with store.transaction() as db:
        tables(db)
        previous = db.execute('SELECT * FROM native_partial_acceptances WHERE acceptance_id=?', (source['acceptance_id'],)).fetchone()
        if previous:
            require(previous['fingerprint'] == fingerprint, 'acceptance_id_content_conflict')
            return json.loads(previous['receipt'])  # Never reset a later attempt.
        row, checkpoint, session, state, old, old_parts = _selected(store, db, source['task_id'])
        require(row['status'] == 'needs_review' and row['epoch'] == source['expected_epoch']
                and row['paused_status'] is None and row['paused_deadline'] is None, 'task_not_reviewable')
        require(digest(dict(row)) == expected['task_sha256'] and digest(dict(session)) == expected['session_sha256']
                and old == expected['artifact'], 'task_session_or_artifact_cas_changed')
        require(checkpoint['thread_id'] == source['thread_id'] and checkpoint['turn_id'] == source['turn_id'], 'original_native_binding_changed')
        operation = checkpoint.get('operation_id') or json.loads(row['context']).get('operation_id') or row['id']
        require(operation == payload['operation_id'] and isolation.native_work_unsettled(checkpoint), 'original_operation_not_unsettled')
        require(row['node'] in (None, checkpoint['codex_node']), 'task_owner_changed')
        # An active/paused/blocked native Goal owns its own scheduling. Do not
        # synthesize a goal clear, reset, unpause or second autonomous owner.
        goal = checkpoint.get('goal')
        require(goal == state.get('goal'), 'goal_source_changed')
        require(not isinstance(goal, dict) or goal.get('status') not in ('active', 'paused', 'blocked'), 'goal_requires_original_controller')
        _closure(payload['closure'], source, checkpoint)
        # Dedicated parts are append-only. An extra late part also invalidates
        # acceptance; live task/session/old artifact are CAS checked above.
        require(db.execute('SELECT COUNT(*) FROM native_partial_history_parts WHERE artifact_id=?',
                           (artifact_id(source),)).fetchone()[0] == len(parts), 'staged_history_changed')
        store._assert_continuation_quiescent(db, row)
        require(not db.execute('SELECT 1 FROM interactions WHERE task_id=? AND epoch=? AND answer IS NULL LIMIT 1',
                               (row['id'], row['epoch'])).fetchone(), 'unresolved_interaction')
        receipt = {'protocol': PROTOCOL, 'acceptance_id': source['acceptance_id'], 'task_id': row['id'],
                   'source_epoch': row['epoch'], 'source_thread_id': source['thread_id'], 'source_turn_id': source['turn_id'],
                   'operation_id': operation, 'accepted': True, 'reviewer_id': reviewer,
                   'goal_sha256': digest(goal),
                   'evidence_basis': 'trusted-owner-reviewed-local-effects-and-runtime-closure',
                   'independent_execution_verification': False, 'effects_zero': False,
                   'history_sha256': source['sha256'], 'history_size': source['size'],
                   'complete_artifact': artifact_id(source), 'closure': payload['closure'],
                   'disposition': disposition,
                   'task_state_at_acceptance': 'pending' if disposition == 'continue_remaining' else 'completed',
                   'original_input_replayed': False, 'native_turn_started': False, 'goal_reset': False,
                   'runtime_retired': False, 'automatic_outbound_replay': False,
                   'result_available_on_original_task': disposition == 'owner_reviewed_completion'}
        for part in old_parts:
            db.execute('INSERT INTO native_partial_before_chunks VALUES(?,?,?,?)',
                       (source['acceptance_id'], old['artifact_id'], part['part'], part['body']))
        # Every original byte/state survives independently of the live pointers.
        db.execute('INSERT INTO native_partial_acceptances VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (source['acceptance_id'], reviewer, fingerprint, row['id'], row['epoch'], encoded(dict(row)),
                    encoded(dict(session)), encoded(old), encoded(payload), encoded(receipt), store.clock()))
        artifact = receipt['complete_artifact']
        require(not db.execute('SELECT 1 FROM session_chunks WHERE id=? LIMIT 1', (artifact,)).fetchone(), 'artifact_namespace_conflict')
        for part in parts:
            db.execute('INSERT INTO session_chunks VALUES(?,?,?)', (artifact, part['part'], part['body']))
        resumed = dict(state)
        resumed.update(checkpoint)
        resumed['side_effect_started'] = False
        failure = resumed.get('runtime_failure')
        if isinstance(failure, dict):
            resumed['runtime_failure'] = dict(failure, outcome='owner_reviewed_local_progress',
                                             automatic_retry_authorized=False)
        resumed['owner_partial_continuation'] = receipt
        # Preserve all Goal/accounting/provider/native identities. The next
        # normal claim increments epoch; Codex.start resumes this exact thread.
        text = ('[owner-reviewed partial continuation; same task/thread. Do not replay the original input or '
                'completed operations below. Read the preserved native history. Perform ONLY the explicit remaining work.]\n'
                'Owner-reviewed progress (not an independent safety attestation):\n' + encoded(payload['known_local_progress']) +
                '\nRemaining work:\n' + instruction)
        require(disposition != 'continue_remaining' or len(text.encode('utf8')) <= 65536, 'continuation_input_budget')
        updated = db.execute('UPDATE native_sessions SET state=?,artifact=?,updated=? WHERE scope=? AND harness=? AND state=? AND artifact IS ? AND updated=?',
                             (encoded(resumed), artifact, store.clock(), session['scope'], session['harness'],
                              session['state'], session['artifact'], session['updated'])).rowcount
        require(updated == 1, 'native_session_cas_failed')
        updated = db.execute("UPDATE tasks SET input=?,checkpoint=?,status=?,node=NULL,deadline=NULL,result=? WHERE id=? AND epoch=? AND status='needs_review' AND checkpoint=?",
                             (text if disposition == 'continue_remaining' else row['input'], encoded(resumed),
                              receipt['task_state_at_acceptance'], result, row['id'], row['epoch'], row['checkpoint'])).rowcount
        require(updated == 1, 'task_cas_failed')
        return receipt


def validate_resume_goal(checkpoint, actual_goal):
    """The real native read must agree before servicing resumed host requests."""
    accepted = (checkpoint or {}).get('owner_partial_continuation')
    if (not isinstance(accepted, dict) or accepted.get('protocol') != PROTOCOL
            or accepted.get('disposition') != 'continue_remaining'
            or checkpoint.get('turn_id') != accepted.get('source_turn_id')):
        return
    require(checkpoint.get('thread_id') == accepted.get('source_thread_id')
            and digest(actual_goal) == accepted.get('goal_sha256')
            and (not isinstance(actual_goal, dict) or actual_goal.get('status') not in ('active', 'paused', 'blocked')),
            'actual_native_goal_changed')


def receipt(store, acceptance_id, peer):
    reviewer = maintainer(peer); name(acceptance_id)
    with store.transaction() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_partial_acceptances'").fetchone()
        row = db.execute('SELECT reviewer,receipt FROM native_partial_acceptances WHERE acceptance_id=?', (acceptance_id,)).fetchone() if exists else None
        require(row is not None and row['reviewer'] == reviewer, 'receipt_not_visible')
        return json.loads(row['receipt'])


def dispatch(api, method, path, payload, peer):
    if peer.get('role') != 'native_maintenance' and not path.startswith(PREFIX):
        return NotImplemented
    maintainer(peer)
    if method != 'POST' or path not in (PREFIX + 'inspect', PREFIX + 'history', PREFIX + 'accept', PREFIX + 'receipt'):
        raise PermissionError('native_partial_route_not_authorized')
    if path == PREFIX + 'inspect':
        shape(payload, ('task_id',)); return inspect(api.store, payload['task_id'], peer)
    if path == PREFIX + 'history':
        return upload(api.store, payload, peer)
    if path == PREFIX + 'accept':
        return acceptance(api.store, payload, peer)
    shape(payload, ('acceptance_id',)); return receipt(api.store, payload['acceptance_id'], peer)
