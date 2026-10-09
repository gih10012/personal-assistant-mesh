"""Single-authority SQLite ledger. All lease checks use the authority's clock."""
import contextlib
import base64
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

_UNSPECIFIED = object()


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path, clock=time.time, recover_inflight=True, notification_policy=None, node_id=None):
        # Only this extra managed notification route is configured here.
        # Native communication, Shell and network tools are unaffected.
        policy = {'mode': 'channel'} if notification_policy is None else notification_policy
        if (not isinstance(policy, dict) or set(policy) - {'mode', 'owner_relay'}
                or policy.get('mode') not in ('channel', 'private')):
            raise ValueError('invalid_notification_policy')
        policy = json.loads(json.dumps(policy))
        relay = policy.get('owner_relay')
        if relay is not None:
            from .networking import identifier
            if policy['mode'] != 'private' or not isinstance(relay, dict) or set(relay) != {'peer', 'authority'}:
                raise ValueError('invalid_owner_notification_relay')
            identifier(node_id)
            identifier(relay['peer'])
            identifier(relay['authority'])
            if relay['peer'] == node_id:
                raise ValueError('invalid_owner_notification_relay')
        self.notification_policy, self.notification_node = policy, node_id
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path, self.clock = str(path), clock
        with self.transaction() as db:
            # executescript implicitly commits BEGIN IMMEDIATE. Keep schema
            # creation, check-then-ALTER migrations and recovery in ONE writer
            # transaction, including when several node services start at once.
            statements = (
                'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL)',
                '''CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, parent_id TEXT, input TEXT NOT NULL, required TEXT NOT NULL,
                    status TEXT NOT NULL, node TEXT, epoch INTEGER NOT NULL DEFAULT 0,
                    deadline REAL, checkpoint TEXT NOT NULL DEFAULT '{}', result TEXT,
                    created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)''',
                '''CREATE TABLE IF NOT EXISTS nodes(
                    id TEXT PRIMARY KEY, capabilities TEXT NOT NULL, score REAL NOT NULL,
                    seen REAL NOT NULL, details TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS leader(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), node TEXT, epoch INTEGER NOT NULL,
                    deadline REAL NOT NULL)''',
                'INSERT OR IGNORE INTO leader VALUES(1,NULL,0,0)',
                '''CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL, client_id TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
                    created REAL NOT NULL, context_version TEXT)''',
                '''CREATE TABLE IF NOT EXISTS reservations(
                    id TEXT PRIMARY KEY, amount INTEGER NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, fingerprint TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS renewals(
                    id TEXT PRIMARY KEY, node TEXT NOT NULL, marker TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS memories(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, text TEXT NOT NULL, updated REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS agent_actions(
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS sessions(
                    scope TEXT PRIMARY KEY, node TEXT NOT NULL, harness TEXT NOT NULL,
                    state TEXT NOT NULL, artifact TEXT, updated REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS native_sessions(
                    scope TEXT NOT NULL, node TEXT NOT NULL, harness TEXT NOT NULL,
                    state TEXT NOT NULL, artifact TEXT, updated REAL NOT NULL,
                    PRIMARY KEY(scope,harness))''',
                '''CREATE TABLE IF NOT EXISTS session_chunks(
                    id TEXT NOT NULL, part INTEGER NOT NULL, body BLOB NOT NULL,
                    PRIMARY KEY(id,part))''',
                '''CREATE TABLE IF NOT EXISTS interactions(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                    kind TEXT NOT NULL, params TEXT NOT NULL, answer TEXT, created REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS steering(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                    text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending')''',
                '''CREATE TABLE IF NOT EXISTS notification_relays(
                    request_id TEXT PRIMARY KEY, source_node TEXT NOT NULL,
                    peer TEXT NOT NULL, authority TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempt INTEGER NOT NULL DEFAULT 0,
                    post_attempted INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0, receipt TEXT, error TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL)''',
            )
            for statement in statements:
                db.execute(statement)
            if 'media_items' not in [r[1] for r in db.execute('PRAGMA table_info(outbox)')]:
                db.execute('ALTER TABLE outbox ADD COLUMN media_items TEXT')
            if 'retry_count' not in [r[1] for r in db.execute('PRAGMA table_info(outbox)')]:
                db.execute('ALTER TABLE outbox ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0')
            if 'delivery_route' not in [r[1] for r in db.execute('PRAGMA table_info(outbox)')]:
                # Historical pending messages retain their original route;
                # installing a relay never opts old messages into replay.
                db.execute("ALTER TABLE outbox ADD COLUMN delivery_route TEXT NOT NULL DEFAULT 'channel'")
            if 'post_attempted' not in [r[1] for r in db.execute('PRAGMA table_info(notification_relays)')]:
                # Existing ambiguous attempts are not permission to create a
                # replacement delivery after an upgrade or receipt loss.
                db.execute('ALTER TABLE notification_relays ADD COLUMN post_attempted INTEGER NOT NULL DEFAULT 0')
                db.execute('UPDATE notification_relays SET post_attempted=1 WHERE attempt>0')
            if 'context' not in [r[1] for r in db.execute('PRAGMA table_info(tasks)')]:
                db.execute("ALTER TABLE tasks ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
            if 'scope' not in [r[1] for r in db.execute('PRAGMA table_info(tasks)')]:
                db.execute("ALTER TABLE tasks ADD COLUMN scope TEXT NOT NULL DEFAULT 'leader:owner'")
            if 'paused_status' not in [r[1] for r in db.execute('PRAGMA table_info(tasks)')]:
                db.execute('ALTER TABLE tasks ADD COLUMN paused_status TEXT')
            if 'paused_deadline' not in [r[1] for r in db.execute('PRAGMA table_info(tasks)')]:
                db.execute('ALTER TABLE tasks ADD COLUMN paused_deadline REAL')
            if 'leader_epoch' not in [r[1] for r in db.execute('PRAGMA table_info(tasks)')]:
                # Do not invent a term for legacy active work. Unknown Leader
                # bindings fail closed for that Mesh task, not native tools.
                db.execute('ALTER TABLE tasks ADD COLUMN leader_epoch INTEGER')
            db.execute('INSERT OR IGNORE INTO native_sessions SELECT * FROM sessions')
            # A crash during network submission is NEVER treated as permission to retry.
            if recover_inflight:
                db.execute("UPDATE outbox SET status='unknown' WHERE status='submitting'")
                db.execute("UPDATE steering SET state='unknown' WHERE state='submitting'")
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            # The explicit WAL setup loop owns a single ten-second wait budget,
            # rather than stacking SQLite's busy timeout on every retry.
            db.execute('PRAGMA busy_timeout=0')
            # Concurrent first opens can race on rollback->WAL conversion.
            # SQLite may return BUSY without invoking its busy handler. Retry
            # ONLY this setup statement, before BEGIN/yield; never replay the
            # caller's task writes or hide a non-lock database error.
            expires = time.monotonic() + 10
            while True:
                try:
                    db.execute('PRAGMA journal_mode=WAL')
                    break
                except sqlite3.OperationalError as error:
                    remaining = expires - time.monotonic()
                    if str(error) not in ('database is locked', 'database is busy') or remaining <= 0:
                        raise
                    time.sleep(min(0.05, remaining))
            db.execute('PRAGMA busy_timeout=10000')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _meta(db, key, default=None):
        row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(db, key, value):
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))

    def get(self, key, default=None):
        with self.transaction() as db:
            return self._meta(db, key, default)

    def set(self, key, value):
        with self.transaction() as db:
            self._set(db, key, value)

    def heartbeat(self, node, capabilities, score=0, details=None):
        if not isinstance(node, str) or not node or len(node) > 100:
            raise ValueError('invalid_node')
        if not isinstance(capabilities, list) or not all(isinstance(x, str) for x in capabilities):
            raise ValueError('invalid_capabilities')
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?)',
                       (node, json.dumps(capabilities), float(score), self.clock(), json.dumps(details or {})))
            leader = db.execute('SELECT * FROM leader').fetchone()
            if leader['node'] == node and leader['deadline'] > self.clock():
                db.execute('UPDATE leader SET deadline=?', (self.clock() + 90,))
            return dict(db.execute('SELECT * FROM leader').fetchone())

    def elect(self):
        with self.transaction() as db:
            now = self.clock()
            current = dict(db.execute('SELECT * FROM leader').fetchone())
            if current['deadline'] > now:
                return current
            nodes = db.execute('SELECT * FROM nodes WHERE seen>? ORDER BY score DESC,id', (now - 60,)).fetchall()
            candidates = [n for n in nodes if 'leader' in json.loads(n['capabilities'])]
            node = candidates[0]['id'] if candidates else None
            db.execute('UPDATE leader SET node=?,epoch=epoch+1,deadline=?', (node, now + 90 if node else 0))
            return dict(db.execute('SELECT * FROM leader').fetchone())

    def create_task(self, text, required=None, parent_id=None, task_id=None, context=None):
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf8')) > 65536:
            raise ValueError('invalid_task_input')
        required = required or ['leader']
        if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
            raise ValueError('invalid_required')
        context = context or {}
        if not isinstance(context, dict) or len(json.dumps(context).encode()) > 32768:
            raise ValueError('invalid_task_context')
        context = dict(context)
        if text.startswith('/plan '):
            context.setdefault('mode', 'plan')
        elif text.startswith('/goal '):
            objective = text[6:].strip()
            if not objective or len(objective) > 4000:
                raise ValueError('invalid_goal_objective')
            context.setdefault('goal', {'objective': objective, 'status': 'active'})
        with self.transaction() as db:
            task_id = task_id or uuid.uuid4().hex
            previous = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if previous:
                if (previous['input'] != text or json.loads(previous['required']) != required
                        or previous['parent_id'] != parent_id or json.loads(previous['context']) != context):
                    raise Conflict('task_id_content_conflict')
                return task_id
            if parent_id and not db.execute('SELECT 1 FROM tasks WHERE id=?', (parent_id,)).fetchone():
                raise ValueError('parent_not_found')
            scope = self.session_scope(context)
            db.execute('INSERT INTO tasks(id,parent_id,input,required,status,created,context,scope) VALUES(?,?,?,?,?,?,?,?)',
                       (task_id, parent_id, text, json.dumps(required), 'pending', self.clock(), json.dumps(context), scope))
            return task_id

    def ingest(self, messages, cursor, owner, bot):
        """Persist whole batch, owner context, business tasks and cursor in ONE transaction."""
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages) or not isinstance(cursor, str):
            raise ValueError('invalid_updates')
        accepted = 0
        with self.transaction() as db:
            for msg in messages:
                if msg.get('from_user_id') != owner or msg.get('to_user_id') != bot or msg.get('group_id') or msg.get('message_type') != 1:
                    continue
                raw_id = msg.get('message_id')
                message_id = hashlib.sha256(json.dumps([bot, raw_id, msg if raw_id is None else None], sort_keys=True).encode()).hexdigest()
                if db.execute('SELECT 1 FROM inbox WHERE id=?', (message_id,)).fetchone():
                    continue
                db.execute('INSERT INTO inbox VALUES(?,?,?)', (message_id, json.dumps(msg), self.clock()))
                context = msg.get('context_token')
                if context:
                    previous = self._meta(db, 'owner_context', {})
                    created = msg.get('create_time_ms', 0)
                    if created >= previous.get('created', 0):
                        self._set(db, 'owner_context', {'token': context, 'version': message_id, 'created': created})
                # First activation may replay older server-held input. Archive it,
                # never treat an old command as fresh execution authorization.
                cutoff = self._meta(db, 'activate_after_ms', 0)
                if cutoff and msg.get('create_time_ms', 0) < cutoff:
                    continue
                items = msg.get('item_list', [])
                if not isinstance(items, list):
                    raise ValueError('invalid_items')
                text = '\n'.join(str(i.get('text_item', {}).get('text', '')) for i in items if isinstance(i, dict) and i.get('type') == 1).strip()
                if text.startswith('ClawBot 自动刷新'):
                    db.execute("UPDATE renewals SET status='context_observed' WHERE marker=? AND status='reserved'", (text,))
                    continue
                if text in ('/status', '状态', '助理状态'):
                    self._enqueue(db, 'status-' + message_id, self._status_text(db))
                elif text.startswith('/answer ') or text.startswith('/approve ') or text.startswith('/deny '):
                    parts = text.split(' ', 2)
                    identity = parts[1]
                    interaction = db.execute('SELECT * FROM interactions WHERE id=?', (identity,)).fetchone()
                    task = db.execute('SELECT * FROM tasks WHERE id=?', (interaction['task_id'],)).fetchone() if interaction else None
                    if not self._interaction_is_live(db, task, interaction) or interaction['answer'] is not None:
                        self._enqueue(db, 'control-' + message_id, '请求不存在、已回答或原会话已经中断。')
                        continue
                    if parts[0] == '/answer' and interaction['kind'] == 'question' and len(parts) == 3:
                        questions = json.loads(interaction['params']).get('questions', [])
                        if len(questions) == 1:
                            answer = {'answers': {questions[0]['id']: {'answers': [parts[2]]}}}
                        else:
                            try:
                                answer = json.loads(parts[2])
                            except ValueError:
                                self._enqueue(db, 'control-' + message_id, '多个问题请使用 answers JSON 按问题 ID 回答。')
                                continue
                    elif parts[0] in ('/approve', '/deny') and interaction['kind'] == 'approval':
                        answer = {'decision': 'accept' if parts[0] == '/approve' else 'decline'}
                    else:
                        self._enqueue(db, 'control-' + message_id, '回答类型不匹配。')
                        continue
                    try:
                        self._validate_interaction_answer(interaction, answer)
                    except ValueError:
                        # A user typo is an archived invalid command, not a bad
                        # transport batch. Keep its inbox entry and continue so
                        # following commands and the cursor still commit.
                        self._enqueue(db, 'control-' + message_id, '回答格式无效；请按原问题 ID 提交完整的 answers JSON。')
                        continue
                    db.execute('UPDATE interactions SET answer=? WHERE id=?', (json.dumps(answer), identity))
                    self._enqueue(db, 'control-' + message_id, '回答已交给原生 Codex 会话。')
                elif text.startswith('/steer '):
                    parts = text.split(' ', 2)
                    target = db.execute('SELECT epoch,status FROM tasks WHERE id=?', (parts[1],)).fetchone() if len(parts) == 3 else None
                    if target and target['status'] == 'running':
                        db.execute('INSERT OR IGNORE INTO steering(id,task_id,epoch,text) VALUES(?,?,?,?)', (message_id, parts[1], target['epoch'], parts[2]))
                        self._enqueue(db, 'control-' + message_id, '补充要求已保存，将插入当前运行轮次。')
                    else:
                        self._enqueue(db, 'control-' + message_id, '仅运行中的任务可接收 /steer；请指定完整任务 ID。')
                elif text.startswith('/pause ') or text.startswith('/resume '):
                    command, target = text.split(' ', 1)
                    try:
                        control = self._control_task(db, target.strip(), command[1:])
                    except Conflict:
                        self._enqueue(db, 'control-' + message_id, '任务可能已产生外部效果，需要先核对实际执行状态；未重新运行。')
                    except ValueError:
                        self._enqueue(db, 'control-' + message_id, '任务不存在或已经结束。')
                    else:
                        self._enqueue(db, 'control-' + message_id, '任务 ' + target.strip() + ' 当前状态：' + control['status'])
                elif text:
                    context = {}
                    if text.startswith('/goal '):
                        objective = text[len('/goal '):].strip()
                        if not objective or len(objective) > 4000:
                            self._enqueue(db, 'control-' + message_id, '目标不能为空且最多 4000 字。')
                            continue
                        context['goal'] = {'objective': objective, 'status': 'active'}
                    elif text.startswith('/plan '):
                        context['mode'] = 'plan'
                    db.execute('INSERT INTO tasks(id,input,required,status,created,context) VALUES(?,?,?,?,?,?)',
                               (message_id, text, '["leader"]', 'pending', self.clock(), json.dumps(context)))
                    self._enqueue(db, 'ack-' + message_id, '已收到并保存任务 ' + message_id[:8] + '。正在交给可用的 Leader。')
                else:
                    # Retain media raw references privately; don't silently pretend understood.
                    self._enqueue(db, 'media-' + message_id, '附件已保存，当前网关尚未接通附件理解；请补充文字说明。')
                accepted += 1
            self._set(db, 'cursor', cursor)
            self._set(db, 'last_poll', self.clock())
        return accepted

    def claim(self, node):
        self.elect()
        with self.transaction() as db:
            now = self.clock()
            n = db.execute('SELECT * FROM nodes WHERE id=? AND seen>?', (node, now - 60)).fetchone()
            if not n:
                raise Conflict('node_not_live')
            caps = set(json.loads(n['capabilities']))
            leader = db.execute('SELECT * FROM leader').fetchone()
            rows = db.execute("SELECT * FROM tasks WHERE status='pending' OR (status IN ('running','waiting_backend','waiting_auth','continuing') AND deadline<=?) ORDER BY created", (now,)).fetchall()
            for row in rows:
                required = set(json.loads(row['required']))
                if not required <= caps or ('leader' in required and (leader['node'] != node or leader['deadline'] <= now)):
                    continue
                if db.execute("SELECT 1 FROM tasks WHERE scope=? AND id<>? AND status='running' AND deadline>? LIMIT 1", (row['scope'], row['id'], now)).fetchone():
                    continue  # one native thread has one active turn, across all nodes
                # Native effects are not blindly replayed after losing a worker.
                if json.loads(row['checkpoint']).get('side_effect_started'):
                    db.execute("UPDATE tasks SET status='needs_review',node=NULL,deadline=NULL WHERE id=?", (row['id'],))
                    self._enqueue(db, 'review-' + row['id'], '任务 ' + row['id'][:8] + ' 执行中断，可能已产生外部效果，等待核对后续接。')
                    continue
                unsettled = db.execute("SELECT checkpoint FROM tasks WHERE scope=? AND id<>? AND status<>'completed'", (row['scope'], row['id']))
                if any(json.loads(other['checkpoint']).get('side_effect_started') for other in unsettled):
                    # A new task must not sidestep review by resuming the same
                    # native conversation under a different task ID or node.
                    continue
                db.execute("UPDATE tasks SET status='running',node=?,epoch=epoch+1,deadline=?,attempts=attempts+1,leader_epoch=? WHERE id=?",
                           (node, now + 90, leader['epoch'] if 'leader' in required else None, row['id']))
                checkpoint = json.loads(row['checkpoint'])
                checkpoint['wait_children_requested'] = False
                db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (json.dumps(checkpoint), row['id']))
                result = dict(db.execute('SELECT * FROM tasks WHERE id=?', (row['id'],)).fetchone())
                result['checkpoint'] = json.loads(result['checkpoint'])
                result['context'] = json.loads(result['context'])
                result['sessions'] = {}
                for session in db.execute('SELECT * FROM native_sessions WHERE scope=?', (row['scope'],)):
                    native = dict(session)
                    native['state'] = json.loads(native['state'])
                    result['sessions'][native['harness']] = native
                result['session'] = result['sessions'].get('codex') or result['sessions'].get('pi')
                scopes = self.memory_scopes(result['context'])
                result['memories'] = [dict(m) for m in db.execute('SELECT id,text FROM memories WHERE scope IN (?,?) ORDER BY updated DESC LIMIT 100', scopes)]
                result['children'] = [dict(c) for c in db.execute('SELECT id,status,result FROM tasks WHERE parent_id=? ORDER BY created', (row['id'],))]
                return result
        return None

    def _task_is_live(self, db, task, node=_UNSPECIFIED, epoch=_UNSPECIFIED):
        """One authority transaction checks both task and Leader generations.

        This is only a managed task lease, never a native Shell/network gate.
        A same-name re-elected Leader cannot revive the previous term's task.
        Callers with an authenticated worker must also pass its node/epoch.
        """
        now = self.clock()
        if (not task or task['status'] != 'running' or not task['node']
                or task['deadline'] is None or task['deadline'] <= now
                or (node is not _UNSPECIFIED and task['node'] != node)
                or (epoch is not _UNSPECIFIED and (not isinstance(epoch, int) or isinstance(epoch, bool)
                                           or task['epoch'] != epoch))):
            return False
        if 'leader' in json.loads(task['required']):
            leader = db.execute('SELECT * FROM leader').fetchone()
            if (task['leader_epoch'] is None or leader['node'] != task['node']
                    or leader['deadline'] <= now or leader['epoch'] != task['leader_epoch']):
                return False
        return True

    def update_task(self, task_id, node, epoch, checkpoint=None, result=None, status=None):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            now = self.clock()
            if not self._task_is_live(db, row, node, epoch):
                raise Conflict('stale_task_lease')
            if status is not None and status not in ('completed', 'failed', 'waiting_auth', 'waiting_backend', 'waiting_children', 'continuing', 'needs_review'):
                raise ValueError('invalid_terminal_status')
            next_checkpoint = json.loads(row['checkpoint'])
            if checkpoint:
                next_checkpoint.update(checkpoint)
            delay = 300 if status == 'waiting_auth' else 60 if status == 'waiting_backend' else 30 if status == 'continuing' else 90
            if status == 'waiting_children':
                active = db.execute("SELECT 1 FROM tasks WHERE parent_id=? AND status NOT IN ('completed','failed','needs_review') LIMIT 1", (task_id,)).fetchone()
                if not active:
                    status = 'pending'  # children may finish before the parent yields
                next_checkpoint['side_effect_started'] = False  # all native work has settled
            db.execute('UPDATE tasks SET checkpoint=?,deadline=?,result=?,status=? WHERE id=?',
                       (json.dumps(next_checkpoint), now + delay, result, status or 'running', task_id))
            if status and not row['parent_id'] and status not in ('waiting_children', 'continuing', 'pending') and (status not in ('waiting_auth', 'waiting_backend') or not json.loads(row['checkpoint']).get('backend_wait_notified')):
                self._enqueue(db, 'result-' + task_id + '-' + str(epoch), result or '任务结束：' + status)
            if status in ('waiting_auth', 'waiting_backend'):
                next_checkpoint['backend_wait_notified'] = True
                db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (json.dumps(next_checkpoint), task_id))
            if row['parent_id'] and status in ('completed', 'failed', 'needs_review'):
                active = db.execute("SELECT 1 FROM tasks WHERE parent_id=? AND status NOT IN ('completed','failed','needs_review') LIMIT 1", (row['parent_id'],)).fetchone()
                if not active:
                    db.execute("UPDATE tasks SET status='pending',node=NULL,deadline=NULL WHERE id=? AND status='waiting_children'", (row['parent_id'],))
            return {'ok': True}

    def agent_action(self, task_id, node, epoch, action_id, action, arguments):
        """Model-selected coordination tools; mutation and fencing are ONE transaction.

        These tools supplement native shell/MCP, not replace them with an allowlist.
        Durable call IDs protect task creation and notifications from replay.
        """
        if not isinstance(arguments, dict) or not isinstance(action_id, str) or not action_id:
            raise ValueError('invalid_agent_action')
        fingerprint = hashlib.sha256(json.dumps([task_id, action, arguments], sort_keys=True).encode()).hexdigest()
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not self._task_is_live(db, row, node, epoch):
                raise Conflict('stale_task_lease')
            previous = db.execute('SELECT * FROM agent_actions WHERE id=?', (action_id,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Conflict('action_id_content_conflict')
                return json.loads(previous['response'])
            context = json.loads(row['context'])
            scope, shared_scope = self.memory_scopes(context)
            if action == 'remember':
                text = arguments['text']
                if not isinstance(text, str) or not text.strip() or len(text.encode()) > 16000:
                    raise ValueError('invalid_memory')
                identity = hashlib.sha256((scope + '\0' + text).encode()).hexdigest()
                db.execute('INSERT OR REPLACE INTO memories VALUES(?,?,?,?)', (identity, scope, text, self.clock()))
                output = {'id': identity, 'saved': True}
            elif action == 'recall':
                query = arguments.get('query', '')
                if not isinstance(query, str):
                    raise ValueError('invalid_query')
                output = {'memories': [dict(r) for r in db.execute('SELECT id,text FROM memories WHERE scope IN (?,?) AND instr(lower(text),lower(?))>0 ORDER BY updated DESC LIMIT 100', (scope, shared_scope, query))]}
            elif action == 'delegate':
                text, required = arguments['input'], arguments.get('required') or ['agent']
                if not isinstance(text, str) or not text.strip() or len(text.encode()) > 65536 or not isinstance(required, list) or not all(isinstance(c, str) for c in required):
                    raise ValueError('invalid_child_task')
                # Local children inherit machine fences even when the model
                # supplies its own capability requirements. Crossing machines
                # requires the explicit authenticated remote delegate route.
                fences = [cap for cap in json.loads(row['required']) if cap.startswith('mesh.node:')]
                required = sorted(set(required + fences))
                child = hashlib.sha256(('child-' + action_id).encode()).hexdigest()
                child_context = dict(context)
                child_context['role'] = arguments.get('role', 'specialist')
                child_context['project_id'] = arguments.get('project_id', context.get('project_id', 'general'))
                identity = str(arguments.get('agent_id', child_context['role']))
                child_context['agent_id'] = identity
                if context.get('origin', {}).get('kind') in ('a2a', 'node-maintenance'):
                    binding = [context.get('authority'), context.get('origin', {}).get('peer'), child_context['project_id'], identity]
                    isolated = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
                    child_context['session_scope'] = 'peer-project:' + isolated
                    child_context['memory_scope'] = 'peer:' + isolated
                else:
                    child_context['session_scope'] = 'project:' + str(child_context['project_id']) + ':' + identity
                db.execute('INSERT INTO tasks(id,parent_id,input,required,status,created,context,scope) VALUES(?,?,?,?,?,?,?,?)', (child, task_id, text, json.dumps(required), 'pending', self.clock(), json.dumps(child_context), self.session_scope(child_context)))
                output = {'id': child, 'parent_id': task_id, 'status': 'pending'}
            elif action == 'children':
                output = {'tasks': [dict(c) for c in db.execute('SELECT id,status,result FROM tasks WHERE parent_id=? ORDER BY created', (task_id,))],
                          'wait_requested': bool(json.loads(row['checkpoint']).get('wait_children_requested'))}
            elif action == 'wait_children':
                checkpoint = json.loads(row['checkpoint'])
                checkpoint['wait_children_requested'] = True
                db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (json.dumps(checkpoint), task_id))
                output = {'continue_after_children': True, 'instruction': '结束本轮，父任务等待子任务后继续。'}
            elif action == 'notify':
                text = arguments['text']
                if not isinstance(text, str) or not text or len(text.encode()) > 16000:
                    raise ValueError('invalid_outbound_text')
                identity = self._enqueue(db, 'agent-' + action_id, text, explicit=True)
                output = self._notification_status(db, identity)
            else:
                raise ValueError('unknown_agent_action')
            db.execute('INSERT INTO agent_actions VALUES(?,?,?)', (action_id, fingerprint, json.dumps(output)))
            return output

    def control_task(self, task_id, command):
        with self.transaction() as db:
            return self._control_task(db, task_id, command)

    def _control_task(self, db, task_id, command):
        """One control transition for owner chat and operator API.

        Pausing fences ledger mutations immediately; the worker stops its native
        process when it observes the fence. It is NOT an undo of existing effects.
        """
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row or command not in ('pause', 'resume') or row['status'] == 'completed':
            raise ValueError('invalid_task_control')
        if json.loads(row['context']).get('remote_proxy'):
            raise Conflict('remote_proxy_requires_remote_control_protocol')
        checkpoint = json.loads(row['checkpoint'])
        if command == 'pause':
            if row['status'] != 'paused':
                db.execute("UPDATE tasks SET paused_status=status,paused_deadline=deadline,status='paused',node=NULL,epoch=epoch+1,deadline=NULL WHERE id=?", (task_id,))
            return {'id': task_id, 'status': 'paused'}
        if (checkpoint.get('side_effect_started') or row['status'] == 'needs_review'
                or row['paused_status'] == 'needs_review'):
            raise Conflict('effectful_task_requires_review')
        if row['status'] in ('running', 'pending', 'waiting_children', 'continuing'):
            # Resume is idempotent, not permission to start a second native turn
            # or skip an already active dependency/wake interval.
            return {'id': task_id, 'status': row['status']}
        status, deadline = 'pending', None
        if row['status'] == 'paused' and row['paused_status'] == 'waiting_children':
            active = db.execute("SELECT 1 FROM tasks WHERE parent_id=? AND status NOT IN ('completed','failed','needs_review') LIMIT 1", (task_id,)).fetchone()
            if active:
                status = 'waiting_children'
        elif row['status'] == 'paused' and row['paused_status'] == 'continuing':
            if row['paused_deadline'] and row['paused_deadline'] > self.clock():
                status, deadline = 'continuing', row['paused_deadline']
        db.execute('UPDATE tasks SET status=?,node=NULL,epoch=epoch+1,deadline=?,paused_status=NULL,paused_deadline=NULL WHERE id=?', (status, deadline, task_id))
        return {'id': task_id, 'status': status}

    @staticmethod
    def memory_scopes(context):
        if context.get('origin', {}).get('kind') in ('a2a', 'node-maintenance'):
            scope = context.get('memory_scope') or 'peer:' + hashlib.sha256(json.dumps(
                [context.get('authority'), context.get('origin', {}).get('peer'), context.get('project_id'), context.get('agent_id')], sort_keys=True).encode()).hexdigest()
            return (scope, scope)  # no implicit personal owner-memory sharing
        return (context.get('agent_id', 'owner'), 'owner')

    @staticmethod
    def session_scope(context):
        scope = context.get('session_scope') or 'leader:' + str(context.get('agent_id', 'owner'))
        if not isinstance(scope, str) or not scope or len(scope) > 256:
            raise ValueError('invalid_session_scope')
        return scope

    def session_action(self, task_id, node, epoch, action, payload):
        """Fenced native session storage. Rollouts are private opaque artifacts;
        no prompt rewriting or application-authored context compression.
        """
        if not isinstance(payload, dict):
            raise ValueError('invalid_session_payload')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not self._task_is_live(db, row, node, epoch):
                raise Conflict('stale_task_lease')
            harness = payload.get('harness', 'codex')
            if harness not in ('codex', 'pi'):
                raise ValueError('invalid_session_harness')
            session = db.execute('SELECT * FROM native_sessions WHERE scope=? AND harness=?', (row['scope'], harness)).fetchone()
            artifact_id = hashlib.sha256(json.dumps([task_id, epoch, harness], separators=(',', ':')).encode()).hexdigest()
            if action == 'upload':
                part = payload['part']
                if not isinstance(part, int) or isinstance(part, bool) or part < 0:
                    raise ValueError('invalid_artifact_part')
                data = base64.b64decode(payload['data'], validate=True)
                if len(data) > 65536:
                    raise ValueError('artifact_part_too_large')
                existing = db.execute('SELECT body FROM session_chunks WHERE id=? AND part=?', (artifact_id, part)).fetchone()
                if existing and existing['body'] != data:
                    raise Conflict('artifact_part_content_conflict')
                db.execute('INSERT OR IGNORE INTO session_chunks VALUES(?,?,?)', (artifact_id, part, data))
                return {'ok': True}
            if action == 'commit':
                state, harness, count = payload['state'], payload['harness'], payload.get('parts', 0)
                if not isinstance(state, dict) or not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise ValueError('invalid_session_state')
                actual = db.execute('SELECT COUNT(*),MIN(part),MAX(part) FROM session_chunks WHERE id=?', (artifact_id,)).fetchone()
                if count and (actual[0] != count or actual[1] != 0 or actual[2] != count - 1):
                    raise Conflict('incomplete_session_artifact')
                # Atomic promotion: an interrupted upload never replaces the last
                # known complete native rollout. Retain former artifacts for now.
                artifact = artifact_id if count else session['artifact'] if session else None
                if session and not count:
                    previous = json.loads(session['state'])
                    if previous.get('thread_id') != state.get('thread_id'):
                        # Never attach the former native conversation's rollout
                        # to a newly created session just because no upload came.
                        artifact = None
                db.execute('INSERT OR REPLACE INTO native_sessions VALUES(?,?,?,?,?,?)',
                    (row['scope'], node, harness, json.dumps(state), artifact, self.clock()))
                return {'scope': row['scope'], 'saved': True, 'artifact_saved': bool(count)}
            if action == 'download':
                if not session or not session['artifact']:
                    raise ValueError('native_session_artifact_unavailable')
                part = payload['part']
                if not isinstance(part, int) or isinstance(part, bool) or part < 0:
                    raise ValueError('invalid_artifact_part')
                data = db.execute('SELECT body FROM session_chunks WHERE id=? AND part=?', (session['artifact'], part)).fetchone()
                return {'data': base64.b64encode(data['body']).decode() if data else None}
            raise ValueError('unknown_session_action')

    @staticmethod
    def _validate_interaction_answer(row, answer):
        if row['kind'] == 'approval':
            if answer not in ({'decision': 'accept'}, {'decision': 'decline'}):
                raise ValueError('invalid_approval_answer')
        else:
            expected = {q['id'] for q in json.loads(row['params']).get('questions', [])}
            answers = answer.get('answers') if isinstance(answer, dict) else None
            if (not isinstance(answers, dict) or set(answers) != expected or
                    any(not isinstance(a, dict) or not isinstance(a.get('answers'), list)
                        or not a['answers'] or not all(isinstance(s, str) for s in a['answers']) for a in answers.values())):
                raise ValueError('invalid_question_answer')

    def _interaction_is_live(self, db, task, interaction):
        return bool(interaction and self._task_is_live(db, task, epoch=interaction['epoch']))

    def interaction(self, task_id, node, epoch, identity, kind=None, params=None):
        with self.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not self._task_is_live(db, task, node, epoch):
                raise Conflict('stale_task_lease')
            row = db.execute('SELECT * FROM interactions WHERE id=?', (identity,)).fetchone()
            if kind:
                if kind not in ('question', 'approval') or not isinstance(params, dict):
                    raise ValueError('invalid_interaction')
                # Questions flagged secret must never be requested via chat.
                if any(q.get('isSecret') for q in params.get('questions', [])):
                    raise ValueError('secret_input_requires_private_credential_flow')
                if row:
                    if row['task_id'] != task_id or row['epoch'] != epoch or json.loads(row['params']) != params:
                        raise Conflict('interaction_id_conflict')
                else:
                    db.execute('INSERT INTO interactions VALUES(?,?,?,?,?,NULL,?)', (identity, task_id, epoch, kind, json.dumps(params), self.clock()))
                    if kind == 'question':
                        detail = '\n'.join(q['id'] + ': ' + q['question'] + '\n' + '\n'.join(o['label'] + ': ' + o['description'] for o in q.get('options') or []) for q in params.get('questions', []))
                        hint = '/answer ' + identity + ' 你的回答'
                    else:
                        detail = json.dumps({k: params[k] for k in ('command', 'cwd', 'reason', 'itemId', 'grantRoot') if k in params}, ensure_ascii=False)
                        hint = '/approve ' + identity + ' 或 /deny ' + identity
                    self._enqueue(db, 'interaction-' + identity, ('Codex 需要本人回答：\n' if kind == 'question' else 'Codex 需要本人批准这一次操作：\n') + detail[:10000] + '\n回复 ' + hint)
            row = db.execute('SELECT * FROM interactions WHERE id=?', (identity,)).fetchone()
            if not row or row['task_id'] != task_id or row['epoch'] != epoch:
                raise ValueError('interaction_not_found')
            return {'id': identity, 'answer': json.loads(row['answer']) if row['answer'] else None}

    def resolve_interaction(self, identity, answer):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM interactions WHERE id=?', (identity,)).fetchone()
            task = db.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone() if row else None
            if not self._interaction_is_live(db, task, row):
                raise Conflict('interaction_not_live')
            self._validate_interaction_answer(row, answer)
            if row['answer'] and json.loads(row['answer']) != answer:
                raise Conflict('interaction_already_answered')
            db.execute('UPDATE interactions SET answer=? WHERE id=?', (json.dumps(answer), identity))
            return {'id': identity, 'answered': True}

    def steer(self, task_id, text, identity):
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 32768:
            raise ValueError('invalid_steering')
        with self.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not self._task_is_live(db, task):
                raise Conflict('task_not_running')
            previous = db.execute('SELECT * FROM steering WHERE id=?', (identity,)).fetchone()
            if previous and (previous['task_id'] != task_id or previous['text'] != text or previous['epoch'] != task['epoch']):
                raise Conflict('steer_id_conflict')
            db.execute('INSERT OR IGNORE INTO steering(id,task_id,epoch,text) VALUES(?,?,?,?)', (identity, task_id, task['epoch'], text))
            return {'id': identity, 'state': 'queued'}

    def poll_steering(self, task_id, node, epoch, identity=None, state=None):
        with self.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not self._task_is_live(db, task, node, epoch):
                raise Conflict('stale_task_lease')
            if identity:
                if state not in ('submitted', 'unknown'):
                    raise ValueError('invalid_steer_state')
                db.execute('UPDATE steering SET state=? WHERE id=? AND task_id=? AND epoch=?', (state, identity, task_id, epoch))
                return {'ok': True}
            row = db.execute("SELECT * FROM steering WHERE task_id=? AND epoch=? AND state='pending' ORDER BY rowid LIMIT 1", (task_id, epoch)).fetchone()
            if row:
                db.execute("UPDATE steering SET state='submitting' WHERE id=?", (row['id'],))
            return {'steering': dict(row) if row else None}

    def _enqueue(self, db, request_id, text, delivery_route=None, explicit=False):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise ValueError('invalid_notification_request_id')
        relay = self.notification_policy.get('owner_relay')
        route = delivery_route or ('relay' if explicit and relay else self.notification_policy['mode'])
        if route not in ('channel', 'private', 'relay') or (route == 'relay' and not relay):
            raise ValueError('invalid_notification_route')
        if route == 'relay':
            import re
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}', request_id):
                raise ValueError('invalid_notification_request_id')
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        previous = db.execute('SELECT fingerprint FROM outbox WHERE id=?', (request_id,)).fetchone()
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise Conflict('request_id_content_conflict')
            return request_id
        db.execute('INSERT INTO outbox(id,fingerprint,body,status,client_id,created,delivery_route) VALUES(?,?,?,?,?,?,?)',
                   (request_id, fingerprint, text, 'pending', 'mesh-' + uuid.uuid4().hex, self.clock(), route))
        if route == 'relay':
            now = self.clock()
            db.execute('INSERT INTO notification_relays(request_id,source_node,peer,authority,fingerprint,created,updated) VALUES(?,?,?,?,?,?,?)',
                       (request_id, self.notification_node, relay['peer'], relay['authority'], fingerprint, now, now))
        return request_id

    def enqueue(self, request_id, text):
        if not isinstance(text, str) or not text or len(text.encode()) > 16000:
            raise ValueError('invalid_outbound_text')
        with self.transaction() as db:
            return self._enqueue(db, request_id, text, explicit=True)

    @staticmethod
    def _notification_status(db, request_id):
        row = db.execute('SELECT delivery_route FROM outbox WHERE id=?', (request_id,)).fetchone()
        if not row:
            raise ValueError('send_not_found')
        route = row['delivery_route']
        return {'id': request_id, 'status': 'recorded_private' if route == 'private' else 'queued',
                'delivery_route': route, 'delivery_verified': False}

    def enqueue_media(self, request_id, items):
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or items[0].get('type') not in (2, 4, 5):
            raise ValueError('invalid_media_items')
        serialized = json.dumps(items, sort_keys=True, separators=(',', ':'))
        if len(serialized.encode()) > 65536:
            raise ValueError('media_reference_too_large')
        with self.transaction() as db:
            self._enqueue(db, request_id, serialized)
            db.execute('UPDATE outbox SET media_items=? WHERE id=?', (serialized, request_id))
        return request_id

    def next_send(self):
        with self.transaction() as db:
            context = self._meta(db, 'owner_context', {})
            row = db.execute("SELECT * FROM outbox WHERE delivery_route='channel' AND (status='pending' OR (status='rejected' AND retry_count<1 AND COALESCE(context_version,'')!=?)) ORDER BY created LIMIT 1", (context.get('version', ''),)).fetchone()
            if not row:
                return None
            client_id = 'mesh-' + uuid.uuid4().hex if row['status'] == 'rejected' else row['client_id']
            db.execute("UPDATE outbox SET status='submitting',client_id=?,context_version=?,retry_count=retry_count+? WHERE id=?", (client_id, context.get('version', ''), 1 if row['status'] == 'rejected' else 0, row['id']))
            result = dict(row)
            result['client_id'] = client_id
            result['context_token'] = context.get('token')
            return result

    def finish_send(self, request_id, status, detail=None):
        if status not in ('accepted', 'rejected', 'unknown', 'waiting_auth'):
            raise ValueError('invalid_send_status')
        with self.transaction() as db:
            db.execute('UPDATE outbox SET status=?,detail=? WHERE id=? AND status=\'submitting\'', (status, json.dumps(detail or {}), request_id))

    def budget_reserve(self, reservation_id, amount, policy):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError('amount_must_be_positive_minor_units')
        ceiling = policy.get('monthly_minor', 0)
        per_action = policy.get('automatic_minor', 0)
        fingerprint = hashlib.sha256(str(amount).encode()).hexdigest()
        with self.transaction() as db:
            old = db.execute('SELECT * FROM reservations WHERE id=?', (reservation_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('reservation_content_conflict')
                return dict(old)
            month_start = time.mktime(time.strptime(time.strftime('%Y-%m-01', time.localtime(self.clock())), '%Y-%m-%d'))
            spent = db.execute("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE status IN ('reserved','spent','unknown') AND created>=?", (month_start,)).fetchone()[0]
            if amount > per_action or spent + amount > ceiling:
                raise Conflict('budget_approval_required')
            db.execute('INSERT INTO reservations VALUES(?,?,?,?,?)', (reservation_id, amount, 'reserved', self.clock(), fingerprint))
            return dict(db.execute('SELECT * FROM reservations WHERE id=?', (reservation_id,)).fetchone())

    def _status_text(self, db):
        leader = db.execute('SELECT * FROM leader').fetchone()
        rows = db.execute('SELECT status,COUNT(*) n FROM tasks GROUP BY status').fetchall()
        return 'Leader：' + (leader['node'] or '暂不可用') + '\n任务：' + '，'.join(r['status'] + '=' + str(r['n']) for r in rows)

    def status(self):
        with self.transaction() as db:
            nodes = [dict(r) for r in db.execute('SELECT id,capabilities,score,seen FROM nodes')]
            for node in nodes:
                node['capabilities'] = json.loads(node['capabilities'])
                node['online'] = node['seen'] > self.clock() - 60
            return {'leader': dict(db.execute('SELECT * FROM leader').fetchone()), 'nodes': nodes,
                    'tasks': {r['status']: r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM tasks GROUP BY status')},
                    'outbox': {r['status']: r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM outbox GROUP BY status')},
                    'last_poll': self._meta(db, 'last_poll'), 'time': self.clock()}

    def inbox(self, after=0, limit=20):
        if not isinstance(after, int) or after < 0 or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('invalid_page')
        with self.transaction() as db:
            rows = db.execute('SELECT rowid,body FROM inbox WHERE rowid>? ORDER BY rowid LIMIT ?', (after, limit)).fetchall()
            output = []
            for row in rows:
                raw = json.loads(row['body'])
                msg = {k: raw.get(k) for k in ('message_id', 'from_user_id', 'to_user_id', 'create_time_ms', 'message_type')}
                msg['items'] = []
                for item in raw.get('item_list', []):
                    if item.get('type') == 1:
                        msg['items'].append({'type': 1, 'text': item.get('text_item', {}).get('text', '')})
                    else:
                        msg['items'].append({'type': item.get('type'), 'content_downloaded': False})
                output.append(msg)
            return {'items': output, 'next_cursor': rows[-1]['rowid'] if rows else after, 'source': 'mesh_archive', 'desktop_required': False}

    def send_status(self, request_id):
        with self.transaction() as db:
            row = db.execute('SELECT id,status,detail,delivery_route FROM outbox WHERE id=?', (request_id,)).fetchone()
            if not row:
                raise ValueError('send_not_found')
            value = dict(row)
            value['detail'] = json.loads(value['detail'])
            if row['delivery_route'] == 'private':
                value['status'] = 'recorded_private'
                value['delivery_verified'] = False
            elif row['delivery_route'] == 'relay':
                relay = db.execute('SELECT state,receipt,error FROM notification_relays WHERE request_id=?', (request_id,)).fetchone()
                if not relay:
                    raise ValueError('notification_relay_record_missing')
                value['status'] = relay['state']
                value['detail'] = {'receipt': json.loads(relay['receipt']) if relay['receipt'] else None,
                                   'error': relay['error'], 'delivery_verified': False}
                value['delivery_verified'] = False
            return value

    def task_status(self, task_id):
        with self.transaction() as db:
            row = db.execute('SELECT id,status,node,epoch,leader_epoch,result,checkpoint,scope FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise ValueError('task_not_found')
            value = dict(row)
            checkpoint = json.loads(value.pop('checkpoint'))
            value['native'] = {k: checkpoint.get(k) for k in ('thread_id', 'turn_id', 'harness', 'mode', 'goal', 'plan')}
            value['interactions'] = [dict(q) for q in db.execute('SELECT id,kind,params FROM interactions WHERE task_id=? AND epoch=? AND answer IS NULL', (task_id, row['epoch']))]
            for question in value['interactions']:
                question['params'] = json.loads(question['params'])
            return value

    def claim_renewal(self, node):
        """One native refresh per reply-context version, journaled BEFORE any effect."""
        with self.transaction() as db:
            row = db.execute("SELECT id FROM outbox WHERE status='rejected' AND retry_count<1 ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            version = self._meta(db, 'owner_context', {}).get('version', 'none')
            identity = hashlib.sha256(version.encode()).hexdigest()
            if db.execute('SELECT 1 FROM renewals WHERE id=?', (identity,)).fetchone():
                return None
            marker = 'ClawBot 自动刷新 mesh-' + identity[:24]
            db.execute('INSERT INTO renewals VALUES(?,?,?,?,?)', (identity, node, marker, 'reserved', self.clock()))
            return {'id': identity, 'marker': marker, 'request_id': 'mesh-renew-' + identity[:40]}

    def retry_waiting(self):
        """Explicit operator retry after fixing a backend, no effectful replays."""
        with self.transaction() as db:
            rows = db.execute("SELECT id,checkpoint FROM tasks WHERE status IN ('failed','waiting_backend','waiting_auth')").fetchall()
            count = 0
            for row in rows:
                if not json.loads(row['checkpoint']).get('side_effect_started'):
                    db.execute("UPDATE tasks SET status='pending',node=NULL,epoch=epoch+1,deadline=NULL WHERE id=?", (row['id'],))
                    count += 1
            return {'requeued': count}
