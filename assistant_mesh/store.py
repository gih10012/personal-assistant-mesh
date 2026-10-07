"""Single-authority SQLite ledger. All lease checks use the authority's clock."""
import contextlib
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path, clock=time.time):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path, self.clock = str(path), clock
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, parent_id TEXT, input TEXT NOT NULL, required TEXT NOT NULL,
                    status TEXT NOT NULL, node TEXT, epoch INTEGER NOT NULL DEFAULT 0,
                    deadline REAL, checkpoint TEXT NOT NULL DEFAULT '{}', result TEXT,
                    created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS nodes(
                    id TEXT PRIMARY KEY, capabilities TEXT NOT NULL, score REAL NOT NULL,
                    seen REAL NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leader(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), node TEXT, epoch INTEGER NOT NULL,
                    deadline REAL NOT NULL);
                INSERT OR IGNORE INTO leader VALUES(1,NULL,0,0);
                CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL, client_id TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
                    created REAL NOT NULL, context_version TEXT);
                CREATE TABLE IF NOT EXISTS reservations(
                    id TEXT PRIMARY KEY, amount INTEGER NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, fingerprint TEXT NOT NULL);
            ''')
            # A crash during network submission is NEVER treated as permission to retry.
            db.execute("UPDATE outbox SET status='unknown' WHERE status='submitting'")
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('BEGIN IMMEDIATE')
        try:
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

    def create_task(self, text, required=None, parent_id=None, task_id=None):
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf8')) > 65536:
            raise ValueError('invalid_task_input')
        required = required or ['leader']
        if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
            raise ValueError('invalid_required')
        with self.transaction() as db:
            task_id = task_id or uuid.uuid4().hex
            if parent_id and not db.execute('SELECT 1 FROM tasks WHERE id=?', (parent_id,)).fetchone():
                raise ValueError('parent_not_found')
            db.execute('INSERT INTO tasks(id,parent_id,input,required,status,created) VALUES(?,?,?,?,?,?)',
                       (task_id, parent_id, text, json.dumps(required), 'pending', self.clock()))
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
                    continue
                if text in ('/status', '状态', '助理状态'):
                    self._enqueue(db, 'status-' + message_id, self._status_text(db))
                elif text.startswith('/pause ') or text.startswith('/resume '):
                    command, target = text.split(' ', 1)
                    row = db.execute('SELECT status FROM tasks WHERE id=?', (target.strip(),)).fetchone()
                    if row and row['status'] in ('pending', 'running', 'paused'):
                        db.execute('UPDATE tasks SET status=?,node=NULL,epoch=epoch+1,deadline=NULL WHERE id=?',
                                   ('paused' if command == '/pause' else 'pending', target.strip()))
                        self._enqueue(db, 'control-' + message_id, '已' + ('暂停' if command == '/pause' else '恢复') + '任务 ' + target.strip())
                    else:
                        self._enqueue(db, 'control-' + message_id, '任务不存在或已经结束。')
                elif text:
                    db.execute('INSERT INTO tasks(id,input,required,status,created) VALUES(?,?,?,?,?)',
                               (message_id, text, '["leader"]', 'pending', self.clock()))
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
            rows = db.execute("SELECT * FROM tasks WHERE status='pending' OR (status='running' AND deadline<=?) ORDER BY created", (now,)).fetchall()
            for row in rows:
                required = set(json.loads(row['required']))
                if not required <= caps or ('leader' in required and (leader['node'] != node or leader['deadline'] <= now)):
                    continue
                # Native effects are not blindly replayed after losing a worker.
                if row['status'] == 'running' and json.loads(row['checkpoint']).get('side_effect_started'):
                    db.execute("UPDATE tasks SET status='needs_review',node=NULL WHERE id=?", (row['id'],))
                    self._enqueue(db, 'review-' + row['id'], '任务 ' + row['id'][:8] + ' 执行中断，可能已产生外部效果，等待核对后续接。')
                    continue
                db.execute("UPDATE tasks SET status='running',node=?,epoch=epoch+1,deadline=?,attempts=attempts+1 WHERE id=?", (node, now + 90, row['id']))
                result = dict(db.execute('SELECT * FROM tasks WHERE id=?', (row['id'],)).fetchone())
                result['checkpoint'] = json.loads(result['checkpoint'])
                return result
        return None

    def update_task(self, task_id, node, epoch, checkpoint=None, result=None, status=None):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            now = self.clock()
            leader = db.execute('SELECT * FROM leader').fetchone()
            if (not row or row['status'] != 'running' or row['node'] != node or row['epoch'] != epoch
                    or row['deadline'] <= now or ('leader' in json.loads(row['required'])
                    and (leader['node'] != node or leader['deadline'] <= now))):
                raise Conflict('stale_task_lease')
            if status is not None and status not in ('completed', 'failed', 'waiting_auth', 'needs_review'):
                raise ValueError('invalid_terminal_status')
            next_checkpoint = json.loads(row['checkpoint'])
            if checkpoint:
                next_checkpoint.update(checkpoint)
            db.execute('UPDATE tasks SET checkpoint=?,deadline=?,result=?,status=? WHERE id=?',
                       (json.dumps(next_checkpoint), now + 90, result, status or 'running', task_id))
            if status:
                self._enqueue(db, 'result-' + task_id + '-' + str(epoch), result or '任务结束：' + status)
            return {'ok': True}

    def _enqueue(self, db, request_id, text):
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        previous = db.execute('SELECT fingerprint FROM outbox WHERE id=?', (request_id,)).fetchone()
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise Conflict('request_id_content_conflict')
            return request_id
        db.execute('INSERT INTO outbox(id,fingerprint,body,status,client_id,created) VALUES(?,?,?,?,?,?)',
                   (request_id, fingerprint, text, 'pending', 'mesh-' + uuid.uuid4().hex, self.clock()))
        return request_id

    def enqueue(self, request_id, text):
        if not isinstance(text, str) or not text or len(text.encode()) > 16000:
            raise ValueError('invalid_outbound_text')
        with self.transaction() as db:
            return self._enqueue(db, request_id, text)

    def next_send(self):
        with self.transaction() as db:
            context = self._meta(db, 'owner_context', {})
            row = db.execute("SELECT * FROM outbox WHERE status='pending' OR (status='rejected' AND COALESCE(context_version,'')!=?) ORDER BY created LIMIT 1", (context.get('version', ''),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE outbox SET status='submitting',context_version=? WHERE id=?", (context.get('version', ''), row['id']))
            result = dict(row)
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
            row = db.execute('SELECT id,status,detail FROM outbox WHERE id=?', (request_id,)).fetchone()
            if not row:
                raise ValueError('send_not_found')
            value = dict(row)
            value['detail'] = json.loads(value['detail'])
            return value

    def task_status(self, task_id):
        with self.transaction() as db:
            row = db.execute('SELECT id,status,node,epoch,result FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise ValueError('task_not_found')
            return dict(row)
