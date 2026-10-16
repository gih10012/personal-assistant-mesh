"""Opt-in continuous owner entrance over an existing task/Claw authority.

This is a Store subclass, not a replacement ledger, poller or LLM harness.
Only NEW input and NEW implicit notices enter its separate native scope.
The original Store owns tasks, native sessions, cursor and outbox transactions.
"""
import hashlib
import json
import math

from .store import Conflict


PROTOCOL = 'mesh-entrance/1'
ROLE_REVISION = 'owner-entrance-20261016-duplex-v1'
CONTRACT_SHA256 = '931ea1ee0f51216092766604d6e91c4f5c3d648c6facab92f5572532bb890401'
_CONTROL = ('/answer ', '/approve ', '/deny ', '/steer ', '/pause ', '/resume ')
_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS entrance_events(
        event_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
        direction TEXT NOT NULL, kind TEXT NOT NULL, source_ref TEXT NOT NULL,
        fingerprint TEXT NOT NULL, metadata TEXT NOT NULL, text TEXT NOT NULL,
        attachments TEXT NOT NULL, received_at REAL NOT NULL, processed_at REAL,
        task_id TEXT, reply_to TEXT, decision TEXT, outbound_request_id TEXT)''',
    '''CREATE TABLE IF NOT EXISTS entrance_actions(
        action_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        receipt TEXT NOT NULL, created REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS entrance_replies(
        event_id TEXT NOT NULL, reply_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
        request_id TEXT NOT NULL UNIQUE, created REAL NOT NULL,
        PRIMARY KEY(event_id,reply_key))''',
    '''CREATE TABLE IF NOT EXISTS entrance_send_observations(
        request_id TEXT PRIMARY KEY, attempted_at REAL, completed_at REAL,
        outcome TEXT, platform_message_id TEXT, platform_sent_at TEXT)''',
    '''CREATE TABLE IF NOT EXISTS entrance_closed_failures(
        task_id TEXT NOT NULL, epoch INTEGER NOT NULL, node TEXT NOT NULL,
        thread_id TEXT NOT NULL, artifact_id TEXT NOT NULL, proof TEXT NOT NULL,
        created REAL NOT NULL, PRIMARY KEY(task_id,epoch))''',
    '''CREATE TABLE IF NOT EXISTS entrance_attachments(
        attachment_id TEXT PRIMARY KEY,source_ref TEXT NOT NULL,descriptor TEXT NOT NULL)''',
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf8')).hexdigest()


def message_identity(message):
    """EXACT incumbent inbox identity, including its default ASCII encoding."""
    raw = message.get('message_id')
    return hashlib.sha256(json.dumps([message.get('to_user_id'), raw,
        message if raw is None else None], sort_keys=True).encode()).hexdigest()


def _text(message):
    items = message.get('item_list', [])
    if not isinstance(items, list):
        raise ValueError('invalid_items')
    return '\n'.join(str(item.get('text_item', {}).get('text', ''))
        for item in items if isinstance(item, dict) and item.get('type') == 1).strip()


def _string(value, code, maximum=200):
    if (not isinstance(value, str) or not value or '\0' in value
            or len(value.encode('utf8')) > maximum):
        raise ValueError(code)
    return value


def validate_policy(policy):
    if (not isinstance(policy, dict)
            or set(policy) != {'owner_ref', 'conversation_id', 'agent_id'}):
        raise ValueError('entrance_owner_policy_required')
    policy = dict(policy)
    for key in policy:
        _string(policy[key], 'entrance_owner_policy_invalid', 100)
    policy['scope'] = 'entrance:clawbot:' + digest(policy)
    policy['capability'] = 'mesh.entrance:' + policy['agent_id']
    return policy


def _metadata(message, **options):
    # One shared time/ID implementation with the media adapter, not a second
    # timestamp guesser. Optional import keeps unrelated old services unchanged.
    from .entrance_media import message_metadata
    return message_metadata(message, **options)


def entrance_store_class(original_store, policy, media=None):
    """Owner-selected class factory; HTTP/task text cannot enable this route.

    media.stage is private local persistence only; materialize is invoked only
    by the model, outside the writer transaction. No account/cursor is cloned.
    """
    selected = validate_policy(policy)
    if not callable(getattr(original_store, '_ingest_in_db', None)):
        raise ValueError('entrance_original_ingest_extraction_required')

    class EntranceStore(original_store):
        entrance_policy = selected
        entrance_media = media

        def __init__(self, *args, **kwargs):
            # Opening an extra API Store is not permission to recover/reset old
            # in-flight outbox/steering rows. The original worker stays running.
            kwargs['recover_inflight'] = False
            super().__init__(*args, **kwargs)
            with self.transaction() as db:
                for statement in _SCHEMA:
                    db.execute(statement)
                binding = self._meta(db, 'entrance_owner_policy')
                if binding is not None and binding != selected:
                    raise Conflict('entrance_owner_policy_changed')
                if binding is None:
                    self._set(db, 'entrance_owner_policy', selected)

        def _entrance_context(self, event_id):
            return {'agent_id': selected['agent_id'], 'role': 'communication-entrance',
                'project_id': 'owner-communication', 'session_scope': selected['scope'],
                'entrance': {'protocol': PROTOCOL, 'role_revision': ROLE_REVISION,
                    'event_id': event_id, 'conversation_id': selected['conversation_id']}}

        def _entrance_record(self, db, event_id, kind, source_ref, text, metadata,
                             attachments=None, direction='inbound', reply_to=None, received_at=None):
            attachments = attachments or []
            fingerprint = digest([kind, source_ref, text, metadata, attachments, direction, reply_to])
            previous = db.execute('SELECT fingerprint,task_id FROM entrance_events WHERE event_id=?',
                                  (event_id,)).fetchone()
            if previous is not None:
                if previous['fingerprint'] != fingerprint:
                    raise Conflict('entrance_original_event_changed')
                return previous['task_id']
            now, task_id = self.clock() if received_at is None else received_at, 'entrance-' + event_id
            db.execute('INSERT INTO entrance_events(event_id,conversation_id,direction,kind,source_ref,'
                'fingerprint,metadata,text,attachments,received_at,task_id,reply_to) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (event_id, selected['conversation_id'], direction,
                kind, source_ref, fingerprint, canonical(metadata), text, canonical(attachments),
                now, task_id, reply_to))
            self._entrance_register_attachments(db, source_ref, attachments)
            envelope = {'protocol': PROTOCOL, 'role_revision': ROLE_REVISION,
                'event_id': event_id, 'kind': kind, 'message': metadata,
                'text': text, 'attachments': attachments}
            # Prefix prevents a user's /goal or /plan text from activating a
            # native goal for the entrance; handing work to Leader is explicit.
            prompt = ('这是沟通入口的一次新事件，不是 Leader 任务。先读取当前时间和入口历史；'
                '根据真实最新上下文决定回复、静默、基础操作或明确转交。不要自动回任务模板。\n'
                + canonical(envelope))
            self._create_task_in_db(db, prompt, [selected['capability']], None,
                                    task_id, self._entrance_context(event_id))
            return task_id

        def _entrance_register_attachments(self, db, source_ref, attachments):
            for item in attachments:
                identity = item.get('attachment_id')
                if identity is not None:
                    descriptor = canonical(item)
                    old = db.execute('SELECT source_ref,descriptor FROM entrance_attachments '
                                     'WHERE attachment_id=?', (identity,)).fetchone()
                    if old is not None and tuple(old) != (source_ref, descriptor):
                        raise Conflict('entrance_original_attachment_changed')
                    db.execute('INSERT OR IGNORE INTO entrance_attachments VALUES(?,?,?)',
                               (identity, source_ref, descriptor))

        def _ingest_in_db(self, db, messages, cursor, owner, bot):
            if (not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages)
                    or not isinstance(cursor, str)):
                raise ValueError('invalid_updates')
            count = 0
            for message in messages:
                if (message.get('from_user_id') != owner or message.get('to_user_id') != bot
                        or message.get('group_id') or message.get('message_type') != 1):
                    continue
                identity = message_identity(message)
                old = db.execute('SELECT body FROM inbox WHERE id=?', (identity,)).fetchone()
                if old is not None:
                    if digest(json.loads(old[0])) != digest(message):
                        raise Conflict('entrance_original_message_changed')
                    continue  # NEVER backfill/replay historical ordinary input
                text = _text(message)
                if text.startswith(_CONTROL) or text.startswith('ClawBot 自动刷新'):
                    continue  # incumbent control, interaction and refresh rules
                now = self.clock()
                db.execute('INSERT INTO inbox VALUES(?,?,?)', (identity, json.dumps(message), now))
                context = message.get('context_token')
                created = message.get('create_time_ms')
                # Missing platform time is unknown, never replaced with now.
                if context and isinstance(created, (int, float)) and not isinstance(created, bool):
                    previous = self._meta(db, 'owner_context', {})
                    if math.isfinite(created) and created >= previous.get('created', 0):
                        self._set(db, 'owner_context', {'token': context, 'version': identity,
                                                       'created': created})
                cutoff = self._meta(db, 'activate_after_ms', 0)
                if cutoff and isinstance(created, (int, float)) and created < cutoff:
                    continue  # first-activation server backlog is archive only
                options = dict(source_ref=identity, received_at=now, processed_at=None,
                    request_id=identity, task_id='entrance-' + identity,
                    reply_to=message.get('reply_to_message_id'),
                    conversation_id=selected['conversation_id'])
                if media is not None:
                    staged = media.stage(message, **options)
                    metadata, attachments = staged['message'], staged['attachments']
                else:
                    metadata = _metadata(message, **options)
                    attachments = [{'item_index': index, 'kind': item.get('type'),
                        'state': 'not_configured', 'understanding_verified': False}
                        for index, item in enumerate(message.get('item_list', []))
                        if isinstance(item, dict) and item.get('type') != 1]
                self._entrance_record(db, identity, 'message', identity, text, metadata,
                                      attachments, received_at=now)
                count += 1
            # Already intercepted ordinary rows are skipped, but the original
            # cursor/control/archive transaction and its accepted count remain.
            return count + super()._ingest_in_db(db, messages, cursor, owner, bot)

        def _entrance_notice(self, db, request_id, text, source_task=None):
            old = db.execute('SELECT * FROM outbox WHERE id=?', (request_id,)).fetchone()
            if old is not None:
                # A new source wrapper must not reinterpret an old send ID.
                if old['fingerprint'] != hashlib.sha256(text.encode('utf8')).hexdigest():
                    raise Conflict('request_id_content_conflict')
                return request_id
            event_id = 'notice-' + digest(request_id)
            previous = db.execute('SELECT text FROM entrance_events WHERE event_id=?', (event_id,)).fetchone()
            if previous:
                if previous[0] != text:
                    raise Conflict('request_id_content_conflict')
                return request_id
            metadata = {'request_id': request_id, 'source_task_id': source_task,
                'original_time': {'raw': None, 'unit': None, 'utc': None, 'known': False},
                'received_at': self.clock(), 'processed_at': None, 'sent_at': None,
                'direction': 'internal', 'conversation_id': selected['conversation_id']}
            self._entrance_record(db, event_id, 'notification', request_id, text,
                                  metadata, direction='internal')
            return request_id

        def _enqueue(self, db, request_id, text, delivery_route=None, explicit=False):
            if explicit or delivery_route is not None:
                # Preserve Leader/native/direct Claw permission and old relay
                # wire. Recommended entrance route is not a host-tool firewall.
                return super()._enqueue(db, request_id, text, delivery_route, explicit)
            own = db.execute('SELECT event_id FROM entrance_events '
                "WHERE ?='result-'||task_id||'-'||(SELECT epoch FROM tasks WHERE id=task_id) "
                "OR ?='review-'||task_id", (request_id, request_id)).fetchone()
            if own is not None:
                # Native final text is private, not an automatic WeChat reply.
                # Especially do not recursively schedule a notice about itself.
                return request_id
            task = db.execute("SELECT id FROM tasks WHERE ?='result-'||id||'-'||epoch "
                              "OR ?='review-'||id", (request_id, request_id)).fetchone()
            return self._entrance_notice(db, request_id, text, task[0] if task else None)

        def _entrance_task(self, db, task_id, node, epoch, require_entrance=True):
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not self._task_is_live(db, task, node, epoch):
                raise Conflict('stale_task_lease')
            if require_entrance and task['scope'] != selected['scope']:
                raise PermissionError('entrance_task_required')
            return task

        def _entrance_current(self, db):
            last = db.execute("SELECT event_id FROM entrance_events WHERE kind='message' "
                'AND conversation_id=? ORDER BY rowid DESC LIMIT 1',
                (selected['conversation_id'],)).fetchone()
            unhandled = db.execute("SELECT event_id FROM entrance_events WHERE direction<>'outbound' "
                'AND decision IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()
            replied = db.execute("SELECT event_id,outbound_request_id FROM entrance_events "
                "WHERE direction='outbound' ORDER BY rowid DESC LIMIT 1").fetchone()
            return {'now_utc_seconds': self.clock(), 'display_timezone': 'Asia/Shanghai',
                'latest_inbound_event_id': last[0] if last else None,
                'latest_unhandled_event_id': unhandled[0] if unhandled else None,
                'last_reply_request_id': replied['outbound_request_id'] if replied else None,
                'role_revision': ROLE_REVISION, 'native_tools_intercepted': False}

        def claim(self, node):
            task = super().claim(node)
            if task is None:
                task = self._entrance_fresh_after_closed_failure(node)
            if task is not None and task['scope'] == selected['scope']:
                with self.transaction() as db:
                    db.execute('UPDATE entrance_events SET processed_at=COALESCE(processed_at,?) '
                               'WHERE task_id=?', (self.clock(), task['id']))
                    task['context']['entrance_current'] = self._entrance_current(db)
            return task

        def _entrance_fresh_after_closed_failure(self, node):
            """Fresh owner input only, never recovery of an unknown old task.

            An actual worker-reported terminal-turn/owned-runtime/full-artifact
            receipt permits a NEW input on that same native thread. It does not
            settle unknown external operations or clear the old task's guard.
            Missing protocol completion/EOF/history ACK keeps this path closed.
            """
            with self.transaction() as db:
                now = self.clock()
                member = db.execute('SELECT * FROM nodes WHERE id=? AND seen>?', (node, now - 60)).fetchone()
                if not member or selected['capability'] not in json.loads(member['capabilities']):
                    return None
                if db.execute("SELECT 1 FROM tasks WHERE scope=? AND status='running' AND deadline>?",
                              (selected['scope'], now)).fetchone():
                    return None
                blockers = db.execute("SELECT * FROM tasks WHERE scope=? AND status<>'completed'",
                                      (selected['scope'],)).fetchall()
                native = db.execute("SELECT * FROM native_sessions WHERE scope=? AND harness='codex'",
                                    (selected['scope'],)).fetchone()
                if native is None or native['node'] != node:
                    return None
                state = json.loads(native['state'])
                closed = []
                for old in blockers:
                    if not json.loads(old['checkpoint']).get('side_effect_started'):
                        continue
                    proof = db.execute('SELECT * FROM entrance_closed_failures WHERE task_id=? AND epoch=?',
                                       (old['id'], old['epoch'])).fetchone()
                    if (proof is None or proof['node'] != node
                            or proof['thread_id'] != state.get('thread_id')
                            or json.loads(proof['proof']).get('auth_home') != state.get('codex_auth_home')
                            or old['status'] not in ('needs_review', 'waiting_backend', 'waiting_auth', 'failed')
                            or not db.execute('SELECT 1 FROM session_chunks WHERE id=? LIMIT 1',
                                              (proof['artifact_id'],)).fetchone()):
                        return None
                    closed.append(proof)
                if not closed:
                    return None
                # No reuse of the old event or tasks belonging to its operation.
                row = db.execute("SELECT t.* FROM tasks t JOIN entrance_events e ON e.task_id=t.id "
                    "WHERE t.scope=? AND t.status='pending' AND t.epoch=0 AND e.kind IN ('message','notification') "
                    'ORDER BY t.created,t.rowid LIMIT 1', (selected['scope'],)).fetchone()
                if row is None:
                    return None
                db.execute("UPDATE tasks SET status='running',node=?,epoch=1,deadline=?,attempts=attempts+1 "
                           'WHERE id=?', (node, now + 90, row['id']))
                task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (row['id'],)).fetchone())
                task['context'], task['checkpoint'] = json.loads(task['context']), json.loads(task['checkpoint'])
                task['context']['unsettled_previous_operations'] = [
                    {'task_id': item['task_id'], 'epoch': item['epoch'],
                     'thread_id': item['thread_id'], 'history': json.loads(item['proof'])['history'],
                     'native_turn_closed': True, 'external_effects_settled': False,
                     'instruction': '仅处理新的输入；原任务/原操作unknown保留，不能重做。'} for item in closed]
                native = dict(native)
                native['state'] = state
                task['sessions'], task['session'] = {'codex': native}, native
                task['memories'], task['children'] = [], []
                return task

        def _entrance_history(self, db, arguments):
            source = arguments.get('source', 'events')
            after, limit = arguments.get('after', 0), arguments.get('limit', 50)
            if (set(arguments) - {'source', 'after', 'limit'} or source not in ('events', 'inbox', 'outbox')
                    or type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100):
                raise ValueError('entrance_history_page_invalid')
            table = {'events': 'entrance_events', 'inbox': 'inbox', 'outbox': 'outbox'}[source]
            rows = db.execute('SELECT rowid AS sequence,* FROM ' + table +
                ' WHERE rowid>? ORDER BY rowid LIMIT ?', (after, limit)).fetchall()
            output = []
            for row in rows:
                if source == 'events':
                    item = dict(row)
                    for key in ('metadata', 'attachments', 'decision'):
                        item[key] = json.loads(item[key]) if item[key] is not None else None
                    if row['outbound_request_id']:
                        item['send'] = self._entrance_send_status(db, row['outbound_request_id'])
                    output.append(item)
                elif source == 'inbox':
                    raw = json.loads(row['body'])
                    opts = dict(source_ref=row['id'], received_at=row['created'],
                        conversation_id=selected['conversation_id'], request_id=row['id'],
                        reply_to=raw.get('reply_to_message_id'))
                    event = db.execute('SELECT metadata,attachments FROM entrance_events WHERE event_id=?',
                                       (row['id'],)).fetchone()
                    if event:
                        stage = {'message': json.loads(event['metadata']),
                                 'attachments': json.loads(event['attachments'])}
                    else:
                        old_task = db.execute('SELECT id FROM tasks WHERE id=?', (row['id'],)).fetchone()
                        opts['task_id'] = old_task[0] if old_task else None
                        stage = media.stage(raw, **opts) if media else None
                        if stage:
                            self._entrance_register_attachments(db, row['id'], stage['attachments'])
                    output.append({'sequence': row['sequence'], 'archive_id': row['id'],
                        'message': stage['message'] if stage else _metadata(raw, **opts),
                        'text': _text(raw), 'attachments': stage['attachments'] if stage else [],
                        'historical_only': True})
                else:
                    output.append({'sequence': row['sequence'], 'request_id': row['id'],
                        'text': row['body'] if row['media_items'] is None else None,
                        'media_present': row['media_items'] is not None,
                        'created_at': row['created'], 'sent_at': None, 'direction': 'outbound',
                        'conversation_id': selected['conversation_id'],
                        'send': self._entrance_send_status(db, row['id'])})
            return dict(self._entrance_current(db), source=source, items=output,
                next_cursor=rows[-1]['sequence'] if rows else after,
                history_scope='all locally archived authorized rows; native WeChat history is separately readable')

        def _entrance_send_status(self, db, request_id):
            row = db.execute('SELECT id,status,detail,created FROM outbox WHERE id=?', (request_id,)).fetchone()
            observation = db.execute('SELECT * FROM entrance_send_observations WHERE request_id=?',
                                     (request_id,)).fetchone()
            if row is None:
                return {'request_id': request_id, 'status': 'not_found', 'outcome': 'unknown',
                        'automatic_retry': False, 'delivery_verified': False}
            # Only fixed receipt fields, not private transport error payloads.
            detail = json.loads(row['detail'])
            return {'request_id': request_id, 'status': row['status'],
                'platform_message_id': (str(detail['message_id'])
                    if detail.get('message_id') is not None else None),
                'sent_at': observation['platform_sent_at'] if observation else None,
                'send_observation': dict(observation) if observation else None,
                'automatic_retry': False, 'delivery_verified': False}

        def _entrance_action_in_db(self, db, task, action_id, action, arguments):
            event = db.execute('SELECT * FROM entrance_events WHERE task_id=?', (task['id'],)).fetchone()
            if action == 'history':
                return self._entrance_history(db, arguments)
            if action == 'status':
                if set(arguments) != {'event_id'}:
                    raise ValueError('entrance_status_arguments_invalid')
                target = db.execute('SELECT * FROM entrance_events WHERE event_id=?',
                                    (arguments['event_id'],)).fetchone()
                if target is None:
                    raise ValueError('entrance_event_not_found')
                handoff = db.execute('SELECT id,status,node,epoch FROM tasks WHERE id=?',
                                    (target['source_ref'],)).fetchone() if target['kind'] == 'message' else None
                return dict(self._entrance_current(db), event_id=target['event_id'],
                    decision=json.loads(target['decision']) if target['decision'] else None,
                    handoff=dict(handoff) if handoff else None,
                    entrance_processing=dict(db.execute('SELECT id,status,node,epoch FROM tasks WHERE id=?',
                        (target['task_id'],)).fetchone()) if target['task_id'] else None,
                    send=self._entrance_send_status(db, target['outbound_request_id'])
                        if target['outbound_request_id'] else None)
            if event is None:
                raise Conflict('entrance_original_event_missing')
            if action == 'silent':
                if set(arguments) != {'reason'}:
                    raise ValueError('entrance_silent_arguments_invalid')
                decision = {'kind': 'silent', 'reason': _string(arguments['reason'],
                    'entrance_reason_invalid', 2000), 'processed_at': self.clock()}
                db.execute('UPDATE entrance_events SET decision=?,processed_at=? WHERE event_id=?',
                           (canonical(decision), self.clock(), event['event_id']))
                return dict(self._entrance_current(db), event_id=event['event_id'], decision=decision)
            if action == 'handoff':
                if (event['kind'] != 'message' or set(arguments) != {'input'}):
                    raise ValueError('entrance_handoff_arguments_invalid')
                text = _string(arguments['input'], 'entrance_handoff_input_invalid', 60000)
                context = {'origin': {'kind': 'entrance-handoff',
                    'message_id': event['source_ref'], 'event_id': event['event_id'],
                    'entrance_task_id': task['id'], 'conversation_id': selected['conversation_id']}}
                # Same original inbox ID, not a new retry identity. No copied
                # entrance session, parent scope, /goal or old effect markers.
                identity = self._create_task_in_db(db, text, ['leader'], None,
                                                 event['source_ref'], context)
                accepted = db.execute('SELECT id,status,node,epoch FROM tasks WHERE id=?',
                                      (identity,)).fetchone()
                decision = {'kind': 'handoff', 'task_id': identity,
                    'accepted_at': self.clock(), 'state_at_acceptance': accepted['status'],
                    'execution_verified': False}
                db.execute('UPDATE entrance_events SET decision=?,processed_at=? WHERE event_id=?',
                           (canonical(decision), self.clock(), event['event_id']))
                return dict(self._entrance_current(db), event_id=event['event_id'],
                    accepted=True, handoff=dict(accepted), execution_verified=False)
            if action != 'reply':
                raise ValueError('entrance_action_unknown')
            if (set(arguments) - {'text', 'reply_key', 'observed_latest_message_id'}
                    or not {'text', 'observed_latest_message_id'} <= set(arguments)):
                raise ValueError('entrance_reply_arguments_invalid')
            text = _string(arguments['text'], 'invalid_outbound_text', 16000)
            reply_key = _string(arguments.get('reply_key', 'primary'), 'entrance_reply_key_invalid', 100)
            reply_fingerprint = digest([event['event_id'], reply_key, text])
            old = db.execute('SELECT * FROM entrance_replies WHERE event_id=? AND reply_key=?',
                             (event['event_id'], reply_key)).fetchone()
            if old is not None:
                if old['fingerprint'] != reply_fingerprint:
                    raise Conflict('entrance_original_reply_changed')
                return self._entrance_send_status(db, old['request_id'])
            uncertain = db.execute("SELECT 1 FROM entrance_replies r JOIN outbox o ON o.id=r.request_id "
                "WHERE r.event_id=? AND o.status IN ('unknown','submitting') LIMIT 1",
                (event['event_id'],)).fetchone()
            if uncertain:
                raise Conflict('entrance_original_reply_unknown')
            current = self._entrance_current(db)
            if arguments['observed_latest_message_id'] != current['latest_inbound_event_id']:
                raise Conflict('entrance_newer_message_requires_context_read')
            identity = 'entrance-reply-' + digest([event['event_id'], reply_key])
            # Direct existing one-outbox route. Never submit network IO here.
            super()._enqueue(db, identity, text, explicit=True)
            db.execute('INSERT INTO entrance_replies VALUES(?,?,?,?,?)',
                (event['event_id'], reply_key, reply_fingerprint, identity, self.clock()))
            outgoing = 'outbound-' + digest(identity)
            metadata = {'request_id': identity, 'reply_to': event['event_id'],
                'platform_reply_to_message_id': json.loads(event['metadata']).get('message_id'),
                'platform_quote_sent': False,
                'received_at': None, 'processed_at': self.clock(), 'sent_at': None,
                'direction': 'outbound', 'conversation_id': selected['conversation_id']}
            db.execute('INSERT INTO entrance_events(event_id,conversation_id,direction,kind,source_ref,'
                'fingerprint,metadata,text,attachments,received_at,processed_at,reply_to,outbound_request_id) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (outgoing, selected['conversation_id'], 'outbound',
                'reply', identity, reply_fingerprint, canonical(metadata), text, '[]', self.clock(),
                self.clock(), event['event_id'], identity))
            decision = {'kind': 'reply', 'request_id': identity, 'processed_at': self.clock()}
            db.execute('UPDATE entrance_events SET decision=?,processed_at=?,outbound_request_id=? WHERE event_id=?',
                       (canonical(decision), self.clock(), identity, event['event_id']))
            return dict(current, event_id=event['event_id'],
                **self._entrance_send_status(db, identity))

        def agent_action(self, task_id, node, epoch, action_id, action, arguments):
            if not isinstance(action, str) or not action.startswith('entrance_'):
                return super().agent_action(task_id, node, epoch, action_id, action, arguments)
            if not isinstance(arguments, dict):
                raise ValueError('invalid_agent_action')
            _string(action_id, 'invalid_agent_action', 1000)
            name = action[len('entrance_'):]
            fingerprint = digest([task_id, action, arguments])
            with self.transaction() as db:
                task = self._entrance_task(db, task_id, node, epoch, name != 'notice')
                old = db.execute('SELECT * FROM entrance_actions WHERE action_id=?', (action_id,)).fetchone()
                if old:
                    if old['fingerprint'] != fingerprint:
                        raise Conflict('action_id_content_conflict')
                    return json.loads(old['receipt'])
                if name == 'record_failure':
                    from .entrance_failure import PROTOCOL as FAILURE_PROTOCOL
                    proof = arguments.get('proof')
                    expected_keys = {'protocol', 'thread_id', 'turn_id', 'terminal_turn_status',
                        'owned_process_reaped', 'owned_reader_eof', 'runtime_pid', 'history',
                        'external_effects_settled'}
                    checkpoint = json.loads(task['checkpoint'])
                    native = db.execute("SELECT * FROM native_sessions WHERE scope=? AND harness='codex'",
                                        (selected['scope'],)).fetchone()
                    state = json.loads(native['state']) if native else {}
                    artifact = hashlib.sha256(json.dumps([task_id, epoch, 'codex'],
                                                          separators=(',', ':')).encode()).hexdigest()
                    if (set(arguments) != {'proof'} or not isinstance(proof, dict)
                            or set(proof) != expected_keys or proof.get('protocol') != FAILURE_PROTOCOL
                            or proof.get('terminal_turn_status') not in ('failed', 'interrupted')
                            or proof.get('owned_process_reaped') is not True
                            or proof.get('owned_reader_eof') is not True
                            or proof.get('external_effects_settled') is not False
                            or proof.get('thread_id') != checkpoint.get('thread_id')
                            or proof.get('turn_id') != checkpoint.get('turn_id')
                            or state.get('entrance_closed_failure') != proof
                            or state.get('side_effect_started') is not True
                            or state.get('thread_id') != proof['thread_id']
                            or native['node'] != node or native['artifact'] != artifact
                            or not db.execute('SELECT 1 FROM session_chunks WHERE id=? LIMIT 1',
                                              (artifact,)).fetchone()):
                        raise Conflict('entrance_failed_native_boundary_unconfirmed')
                    # This is an authenticated actual-worker report, not an
                    # independent external-effects attestation or guard reset.
                    retained = dict(proof, auth_home=state.get('codex_auth_home'))
                    db.execute('INSERT INTO entrance_closed_failures VALUES(?,?,?,?,?,?,?)',
                        (task_id, epoch, node, proof['thread_id'], artifact,
                         canonical(retained), self.clock()))
                    output = {'task_id': task_id, 'epoch': epoch, 'native_thread_id': proof['thread_id'],
                        'status': 'closed_failure_history_saved', 'fresh_input_only': True,
                        'old_input_replayed': False, 'old_effect_guard_cleared': False,
                        'external_effects_settled': False, 'evidence_level': 'worker_reported'}
                elif name == 'notice':
                    if set(arguments) != {'text'}:
                        raise ValueError('entrance_notice_arguments_invalid')
                    text = _string(arguments['text'], 'invalid_outbound_text', 16000)
                    identity = 'entrance-notice-' + digest([task_id, action_id])
                    self._entrance_notice(db, identity, text, task_id)
                    output = {'request_id': identity, 'status': 'queued_for_entrance',
                              'delivery_verified': False}
                elif name == 'media':
                    if set(arguments) != {'attachment_id'} or media is None:
                        raise ValueError('entrance_media_not_configured')
                    attachment = _string(arguments['attachment_id'], 'entrance_attachment_invalid')
                    # Validate membership against privately staged descriptors;
                    # never accept CDN URLs/AES keys from model arguments.
                    found = db.execute('SELECT 1 FROM entrance_attachments WHERE attachment_id=?',
                                       (attachment,)).fetchone()
                    if not found:
                        raise ValueError('entrance_attachment_not_found')
                    output = None
                else:
                    output = self._entrance_action_in_db(db, task, action_id, name, arguments)
                if output is not None:
                    db.execute('INSERT INTO entrance_actions VALUES(?,?,?,?)',
                               (action_id, fingerprint, canonical(output), self.clock()))
                    return output
            # Materialization is a private read/cache operation, not a send or
            # task replay. Never hold the SQLite writer while downloading.
            output = media.materialize(attachment)
            with self.transaction() as db:
                self._entrance_task(db, task_id, node, epoch)
                previous = db.execute('SELECT * FROM entrance_actions WHERE action_id=?', (action_id,)).fetchone()
                if previous:
                    if previous['fingerprint'] != fingerprint:
                        raise Conflict('action_id_content_conflict')
                    return json.loads(previous['receipt'])
                db.execute('INSERT INTO entrance_actions VALUES(?,?,?,?)',
                           (action_id, fingerprint, canonical(output), self.clock()))
            return output

        def next_send(self):
            row = super().next_send()
            if row is not None:
                with self.transaction() as db:
                    db.execute('INSERT OR IGNORE INTO entrance_send_observations(request_id,attempted_at) '
                               'VALUES(?,?)', (row['id'], self.clock()))
            return row

        def finish_send(self, request_id, status, detail=None):
            # Same incumbent state transition; observation is NOT a fabricated
            # platform send timestamp or evidence the owner's phone received it.
            super().finish_send(request_id, status, detail)
            with self.transaction() as db:
                db.execute('INSERT OR IGNORE INTO entrance_send_observations(request_id) VALUES(?)', (request_id,))
                message = (detail or {}).get('message_id')
                db.execute('UPDATE entrance_send_observations SET completed_at=?,outcome=?,platform_message_id=? '
                    'WHERE request_id=?', (self.clock(), status, str(message) if message is not None else None, request_id))

    return EntranceStore
