"""Owner-bound phone access to the existing Claw entrance, native tasks and media.

The stream contains references to original events and task transitions. Looking
at a receipt or importing an already executed branch never admits a task.
"""
import base64
import hashlib
import json
import mimetypes
import re
from pathlib import Path

from .entrance import canonical, digest
from .entrance_media import _directory, _write_once, message_metadata
from .store import Conflict

ACTIONS = frozenset('mesh_chat_' + name for name in (
    'status', 'send', 'events', 'approve', 'upload', 'attachment', 'merge'))
CHUNK = 48 * 1024
MAX_MEDIA = 32 * 1024 * 1024


def _id(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value) is None:
        raise ValueError('phone_chat_id_invalid')
    return value


def _int(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('phone_chat_number_invalid')
    return value


def _text(value, limit):
    if not isinstance(value, str) or '\0' in value or len(value.encode('utf8')) > limit:
        raise ValueError('phone_chat_text_invalid')
    return value


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) <= set(required) | set(optional):
        raise ValueError('phone_chat_arguments_invalid')


def _now(store):
    return int(store.clock() * 1000)


class PhoneChat:
    def __init__(self, store):
        self.store, self.policy = store, store.entrance_policy
        with store.transaction() as db:
            for statement in (
                '''CREATE TABLE IF NOT EXISTS phone_chat_inputs(
                    event_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,branch TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,history_only INTEGER NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS phone_chat_context_delivery(
                    event_id TEXT NOT NULL,task_id TEXT NOT NULL,PRIMARY KEY(event_id,task_id))''',
                '''CREATE TABLE IF NOT EXISTS claw_chat_branches(
                    event_id TEXT PRIMARY KEY,scope TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS phone_chat_media(
                    attachment_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,name TEXT NOT NULL,
                    mime TEXT NOT NULL,total INTEGER NOT NULL,received INTEGER NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS phone_chat_chunks(
                    attachment_id TEXT NOT NULL,start INTEGER NOT NULL,body BLOB NOT NULL,
                    PRIMARY KEY(attachment_id,start))''',
                '''CREATE TABLE IF NOT EXISTS phone_chat_feed(
                    sequence INTEGER PRIMARY KEY,kind TEXT NOT NULL,ref TEXT NOT NULL,
                    parent TEXT,body TEXT,observed_ms INTEGER)''',
                '''CREATE UNIQUE INDEX IF NOT EXISTS phone_chat_source_event
                    ON phone_chat_feed(ref) WHERE kind='event' ''',
                '''CREATE INDEX IF NOT EXISTS phone_chat_event_task ON entrance_events(task_id)''',
                '''CREATE INDEX IF NOT EXISTS phone_chat_event_source ON entrance_events(source_ref)''',
                '''CREATE TRIGGER IF NOT EXISTS phone_chat_event AFTER INSERT ON entrance_events
                    BEGIN INSERT INTO phone_chat_feed(kind,ref) VALUES('event',NEW.event_id); END''',
                '''CREATE TRIGGER IF NOT EXISTS phone_chat_task AFTER UPDATE OF status ON tasks
                    WHEN NEW.status<>OLD.status BEGIN
                    INSERT INTO phone_chat_feed(kind,ref,parent,body,observed_ms)
                    SELECT 'receipt',NEW.id,e.event_id,NEW.status,
                      CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)
                    FROM entrance_events e WHERE e.direction='inbound' AND
                      (e.task_id=NEW.id OR (e.source_ref=NEW.id AND e.kind='message')); END''',
                '''CREATE TRIGGER IF NOT EXISTS phone_chat_delivery AFTER UPDATE OF status ON outbox
                    WHEN NEW.status<>OLD.status BEGIN
                    INSERT INTO phone_chat_feed(kind,ref,parent,body,observed_ms)
                    SELECT 'delivery',NEW.id,e.event_id,NEW.status,
                      CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)
                    FROM entrance_events e WHERE e.outbound_request_id=NEW.id AND e.direction='outbound'; END'''):
                db.execute(statement)
            db.execute("""INSERT OR IGNORE INTO phone_chat_feed(kind,ref)
                SELECT 'event',event_id FROM entrance_events ORDER BY rowid""")
        self._install_actions()

    def _install_actions(self):
        store = self.store
        previous_task, previous_action = store._entrance_task, store._entrance_action_in_db
        previous_status = store._entrance_send_status

        previous_record = store._entrance_record

        def record(db, event_id, kind, source_ref, text, metadata, *args, **kwargs):
            existing = db.execute('SELECT 1 FROM entrance_events WHERE event_id=?', (event_id,)).fetchone()
            task_id = previous_record(db, event_id, kind, source_ref, text, metadata, *args, **kwargs)
            if existing is None and kind == 'message' and not metadata.get('branch_id'):
                if metadata.get('channel') != 'phone' and self._select_claw_branch(db, event_id, task_id):
                    return task_id
                self._include_branch_history(db, task_id)
            return task_id

        store._entrance_record = record

        def entrance_task(db, task_id, node, epoch, require_entrance=True):
            # The membership row and task are committed together. No client can
            # select a native scope, a different task, epoch or worker here.
            if require_entrance:
                bound = db.execute("""SELECT 1 FROM entrance_events e JOIN tasks t ON t.id=e.task_id
                    WHERE e.task_id=? AND (EXISTS (
                        SELECT 1 FROM phone_chat_inputs p WHERE p.event_id=e.event_id AND p.history_only=0)
                      OR EXISTS (
                        SELECT 1 FROM claw_chat_branches b WHERE b.event_id=e.event_id AND b.scope=t.scope))
                    """, (task_id,)).fetchone()
                require_entrance = bound is None
            return previous_task(db, task_id, node, epoch, require_entrance)

        def send_status(db, request_id):
            row = db.execute("""SELECT e.event_id FROM entrance_events e JOIN phone_chat_inputs p
                ON e.reply_to=p.event_id WHERE e.outbound_request_id=? AND e.direction='outbound'""",
                (request_id,)).fetchone()
            if row:
                return {'request_id': request_id, 'status': 'recorded', 'event_id': row[0],
                        'automatic_retry': False, 'delivery_verified': False}
            return previous_status(db, request_id)

        def entrance_action(db, task, action_id, action, args):
            phone = db.execute("""SELECT e.* FROM entrance_events e JOIN phone_chat_inputs p
                ON e.event_id=p.event_id WHERE e.task_id=? AND p.history_only=0""", (task['id'],)).fetchone()
            if phone is not None and action == 'reply':
                return self._reply(db, phone, args)
            if action == 'preview':
                if phone is None:
                    raise PermissionError('phone_chat_task_required')
                return self._preview(db, phone, args)
            return previous_action(db, task, action_id, action, args)

        store._entrance_task, store._entrance_action_in_db = entrance_task, entrance_action
        store._entrance_send_status = send_status
        media = getattr(store, 'entrance_media', None)
        if media is not None:
            original = media.materialize

            def materialize(identity):
                with store.transaction() as db:
                    row = db.execute('SELECT * FROM phone_chat_media WHERE attachment_id=?', (identity,)).fetchone()
                    if row is not None:
                        if row['received'] != row['total']:
                            raise Conflict('phone_chat_upload_incomplete')
                        raw = b''.join(bytes(r[0]) for r in db.execute(
                            'SELECT body FROM phone_chat_chunks WHERE attachment_id=? ORDER BY start', (identity,)))
                if row is None:
                    return original(identity)
                directory = _directory(media.root / 'phone-chat', create=True)
                suffix = Path(row['name']).suffix.lower()
                suffix = suffix if re.fullmatch(r'\.[a-z0-9]{1,10}', suffix) else '.bin'
                sha = hashlib.sha256(raw).hexdigest()
                path = directory / (sha + suffix)
                _write_once(path, raw)
                return {'attachment_id': identity, 'state': 'available', 'file_name': row['name'],
                        'mime': row['mime'], 'size': len(raw), 'sha256': sha, 'path': str(path),
                        'understanding_verified': False}

            media.materialize = materialize

    def _include_branch_history(self, db, task_id):
        """Supply branch messages as reference material to a shared entrance turn."""
        task = db.execute('SELECT context,status FROM tasks WHERE id=?', (task_id,)).fetchone()
        if task is None or task['status'] != 'pending':
            return
        context = json.loads(task['context'])
        if 'phone_branch_history' in context:
            return
        rows = db.execute("""SELECT e.* FROM entrance_events e
            WHERE e.conversation_id=? AND (EXISTS (
              SELECT 1 FROM phone_chat_inputs p WHERE p.branch<>'' AND
              (p.event_id=e.event_id OR p.event_id=e.reply_to))
              OR EXISTS (SELECT 1 FROM claw_chat_branches b WHERE b.event_id=e.event_id OR b.event_id=e.reply_to))
            AND NOT EXISTS (SELECT 1 FROM phone_chat_context_delivery d JOIN tasks t ON t.id=d.task_id
              WHERE d.event_id=e.event_id AND t.status='completed')
            ORDER BY e.rowid LIMIT 32""", (self.policy['conversation_id'],))
        history = []
        for event in rows:
            metadata = json.loads(event['metadata'])
            history.append({'event_id':event['event_id'], 'reply_to':event['reply_to'],
                'role':metadata.get('original_role') or ('assistant' if event['direction']=='outbound' else 'user'),
                'time':metadata.get('original_time') or metadata.get('created_at_ms'),
                'text':event['text'][:1200], 'full_text_in_history':len(event['text'])>1200})
        if history:
            context['phone_branch_history'] = [event['event_id'] for event in history]
            reference = '\n独立入口会话历史（仅供理解当前消息，原操作保持原状态）：\n' + canonical(history)
            db.execute('UPDATE tasks SET input=input||?,context=? WHERE id=?',
                       (reference, canonical(context), task_id))
            db.executemany('INSERT INTO phone_chat_context_delivery VALUES(?,?)',
                           [(event['event_id'],task_id) for event in history])

    @staticmethod
    def _unresolved_session(db, scope):
        rows = db.execute("""SELECT id,epoch,checkpoint FROM tasks t WHERE scope=? AND
            status IN ('needs_review','failed','waiting_backend','waiting_auth') AND
            NOT EXISTS (SELECT 1 FROM entrance_closed_failures f WHERE f.task_id=t.id AND f.epoch=t.epoch)
            ORDER BY created,id""", (scope,))
        for row in rows:
            if json.loads(row['checkpoint']).get('side_effect_started') is True:
                return [row['id'], row['epoch']]
        return None

    def _select_claw_branch(self, db, event_id, task_id):
        # A persisted unresolved effect is definitive. Preserve every old
        # task and put only the newly admitted message in the derived scope.
        scope = self.policy['scope']
        blocked = self._unresolved_session(db, scope)
        if blocked is None:
            return False
        while blocked is not None:
            scope = 'entrance:claw-branch:' + digest([scope, blocked])
            blocked = self._unresolved_session(db, scope)
        task = db.execute('SELECT context FROM tasks WHERE id=?', (task_id,)).fetchone()
        context = json.loads(task['context'])
        context['session_scope'] = scope
        db.execute('UPDATE tasks SET context=?,scope=? WHERE id=?',
                   (canonical(context), scope, task_id))
        db.execute('INSERT INTO claw_chat_branches VALUES(?,?)', (event_id, scope))
        return True

    def _mode(self, db):
        workers = db.execute('SELECT capabilities FROM nodes WHERE seen>?', (self.store.clock()-60,))
        if not any(self.policy['capability'] in json.loads(row[0]) for row in workers):
            return 'independent'
        return 'independent' if self._unresolved_session(db, self.policy['scope']) else 'shared'

    def request(self, node, action, value):
        if not isinstance(value, dict) or value.get('session_id') != node['session_id']:
            raise PermissionError('phone_chat_original_session_required')
        args = dict(value)
        del args['session_id']
        if action == 'mesh_chat_attachment':
            return self._download(node, args)
        with self.store.transaction() as db:
            if action == 'mesh_chat_status':
                _fields(args, ())
                mode = self._mode(db)
                return {'mode': mode, 'available': mode == 'shared'}
            if action == 'mesh_chat_send':
                return self._input(db, node, args)
            if action == 'mesh_chat_events':
                return self._events(db, args)
            if action == 'mesh_chat_approve':
                return self._approve(db, args)
            if action == 'mesh_chat_upload':
                return self._upload(db, node, args)
            if action == 'mesh_chat_merge':
                _fields(args, ('branch_id','events'))
                branch = _id(args['branch_id'])
                if not isinstance(args['events'], list) or not 1 <= len(args['events']) <= 32:
                    raise ValueError('phone_chat_merge_bounds')
                merged = []
                for item in args['events']:
                    _fields(item, ('event_id','created_at_ms','role','text'), ('attachment_ids','reply_to'))
                    merged.append(self._input(db, node, dict(item, branch_id=branch), history=True))
                return {'events': merged, 'mode': self._mode(db)}
        raise ValueError('phone_chat_action_invalid')

    def _attachments(self, db, node, items):
        if not isinstance(items, list) or len(items) > 8 or len(set(_id(i) for i in items)) != len(items):
            raise ValueError('phone_chat_attachments_invalid')
        result = []
        for identity in items:
            row = db.execute('SELECT * FROM phone_chat_media WHERE attachment_id=? AND node_id=?',
                             (identity, node['node_id'])).fetchone()
            if row is None or row['received'] != row['total']:
                raise Conflict('phone_chat_upload_incomplete')
            result.append({'attachment_id': identity, 'name': row['name'], 'bytes': row['total'], 'mime': row['mime'],
                           'kind': row['mime'].split('/')[0] if row['mime'].startswith(('image/','video/','audio/')) else 'file'})
        return result

    def _input(self, db, node, args, history=False):
        _fields(args, ('event_id','text','created_at_ms'), ('branch_id','role','reply_to','attachment_ids')
                if history else ('attachment_ids','branch_id'))
        identity, created = _id(args['event_id']), _int(args['created_at_ms'], 253402300799999)
        text = _text(args['text'], 16384)
        role = args.get('role','user')
        if role not in ('user','assistant') or not created:
            raise ValueError('phone_chat_event_invalid')
        parent = _id(args['reply_to']) if args.get('reply_to') else None
        branch = _id(args['branch_id']) if 'branch_id' in args else ''
        items = self._attachments(db, node, args.get('attachment_ids', []))
        if not text.strip() and not items:
            raise ValueError('phone_chat_empty_input')
        fingerprint = digest([node['node_id'],created,text,items,role,parent])
        old = db.execute('SELECT * FROM phone_chat_inputs WHERE event_id=?', (identity,)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint or (not history and old['history_only']):
                raise Conflict('phone_chat_original_event_changed')
            return {'event_id': identity, 'task_id': None if old['history_only'] else 'entrance-'+identity,
                    'mode': 'independent' if old['branch'] else 'shared'}
        if db.execute('SELECT 1 FROM entrance_events WHERE event_id=?', (identity,)).fetchone():
            raise Conflict('phone_chat_original_event_changed')
        metadata = message_metadata({'message_id':identity,'create_time_ms':created}, identity,
            self.store.clock(), request_id=identity, conversation_id=self.policy['conversation_id'], reply_to=parent)
        metadata.update(channel='phone', branch_id=branch, history_only=history)
        db.execute('INSERT INTO phone_chat_inputs VALUES(?,?,?,?,?)',
                   (identity,node['node_id'],branch,fingerprint,int(history)))
        if history:
            # Historical imports must not replace the live reply/approval cursor.
            metadata['original_role'] = role
            kind, direction = 'history', 'internal'
            db.execute('''INSERT INTO entrance_events(event_id,conversation_id,direction,kind,source_ref,
                fingerprint,metadata,text,attachments,received_at,reply_to,decision)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(identity,self.policy['conversation_id'],direction,kind,identity,
                fingerprint,canonical(metadata),text,canonical(items),self.store.clock(),parent,
                canonical({'kind':'imported_history','effects_replayed':False})))
            task_id = None
        else:
            task_id = self.store._entrance_record(db,identity,'message',identity,text,metadata,items)
            task = db.execute('SELECT context FROM tasks WHERE id=?', (task_id,)).fetchone()
            context = json.loads(task['context'])
            context['entrance']['channel'] = 'phone'
            context['entrance']['reply_instructions'] = (
                '当前本人使用手机聊天。entrance reply 直接送达手机；勿另发微信。公开回复草稿可发手机，'
                '所有入口历史可通过 entrance history 查询，已并入独立分支的历史只读，不重复执行。')
            if branch:
                context['session_scope'] = 'entrance:phone-branch:' + digest([self.policy['scope'],node['node_id'],branch])
            db.execute('UPDATE tasks SET context=?,scope=? WHERE id=?',
                       (canonical(context),context['session_scope'],task_id))
        return {'event_id':identity,'task_id':task_id,'mode':'independent' if branch else 'shared'}

    def _reply(self, db, event, args):
        _fields(args, ('text','observed_latest_message_id'), ('reply_key',))
        text = _text(args['text'],16000)
        key = _id(args.get('reply_key','primary'))
        if not text.strip():
            raise ValueError('phone_chat_empty_reply')
        identity = 'entrance-reply-' + digest([event['event_id'],key])
        fingerprint = digest([event['event_id'],key,text])
        existing = db.execute('SELECT fingerprint FROM entrance_replies WHERE request_id=?',(identity,)).fetchone()
        if existing is not None:
            if existing[0] != fingerprint:
                raise Conflict('entrance_original_reply_changed')
            return self.store._entrance_send_status(db,identity)
        current = self.store._entrance_current(db)
        if current['latest_inbound_event_id'] != args['observed_latest_message_id']:
            raise Conflict('entrance_newer_message_requires_context_read')
        now = self.store.clock()
        outgoing = 'outbound-' + digest(identity)
        db.execute('INSERT INTO entrance_replies VALUES(?,?,?,?,?)',(event['event_id'],key,fingerprint,identity,now))
        metadata = {'channel':'phone','request_id':identity,'reply_to':event['event_id'],'created_at_ms':int(now*1000)}
        db.execute('''INSERT INTO entrance_events(event_id,conversation_id,direction,kind,source_ref,fingerprint,
            metadata,text,attachments,received_at,processed_at,reply_to,outbound_request_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(outgoing,self.policy['conversation_id'],'outbound','reply',identity,
            fingerprint,canonical(metadata),text,'[]',now,now,event['event_id'],identity))
        decision = {'kind':'reply','request_id':identity,'processed_at':now}
        db.execute('UPDATE entrance_events SET decision=?,processed_at=?,outbound_request_id=? WHERE event_id=?',
                   (canonical(decision),now,identity,event['event_id']))
        return self.store._entrance_send_status(db,identity)

    def _preview(self, db, event, args):
        _fields(args, ('item_id','text'))
        item, text = _id(args['item_id']), _text(args['text'], 32768)
        db.execute("INSERT INTO phone_chat_feed(kind,ref,parent,body,observed_ms) VALUES('delta',?,?,?,?)",
                   (item,event['event_id'],text,_now(self.store)))
        return {'item_id':item,'recorded':True}

    def _upload(self, db, node, args):
        _fields(args, ('attachment_id','name','mime','offset','total','base64'))
        identity, offset, total = _id(args['attachment_id']),_int(args['offset'],MAX_MEDIA),_int(args['total'],MAX_MEDIA)
        name, mime = _text(args['name'],255),_text(args['mime'],128)
        if not name or '/' in name or '\\' in name or not re.fullmatch(r'[\w.+-]+/[\w.+-]+',mime):
            raise ValueError('phone_chat_media_descriptor_invalid')
        raw = base64.b64decode(_text(args['base64'],CHUNK*4//3),validate=True)
        if len(raw)>CHUNK or offset+len(raw)>total or (not raw and total):
            raise ValueError('phone_chat_chunk_invalid')
        row = db.execute('SELECT * FROM phone_chat_media WHERE attachment_id=?',(identity,)).fetchone()
        if row is None:
            if offset or db.execute('SELECT 1 FROM entrance_attachments WHERE attachment_id=?',(identity,)).fetchone():
                raise Conflict('phone_chat_attachment_id_or_offset_conflict')
            db.execute('INSERT INTO phone_chat_media VALUES(?,?,?,?,?,0)',(identity,node['node_id'],name,mime,total))
            received = 0
        else:
            if (row['node_id'],row['name'],row['mime'],row['total']) != (node['node_id'],name,mime,total):
                raise Conflict('phone_chat_attachment_changed')
            received = row['received']
        prior = db.execute('SELECT body FROM phone_chat_chunks WHERE attachment_id=? AND start=?',(identity,offset)).fetchone()
        if prior:
            if bytes(prior[0]) != raw:
                raise Conflict('phone_chat_chunk_changed')
        else:
            if offset != received:
                raise Conflict('phone_chat_upload_offset')
            db.execute('INSERT INTO phone_chat_chunks VALUES(?,?,?)',(identity,offset,raw))
            received += len(raw)
            db.execute('UPDATE phone_chat_media SET received=? WHERE attachment_id=?',(received,identity))
        return {'attachment_id':identity,'next_offset':received,'complete':received == total}

    def _download(self,node,args):
        _fields(args, ('attachment_id','offset'))
        identity,offset = _id(args['attachment_id']),_int(args['offset'],MAX_MEDIA)
        with self.store.transaction() as db:
            media = db.execute('SELECT * FROM phone_chat_media WHERE attachment_id=?',(identity,)).fetchone()
            if media:
                if media['node_id'] != node['node_id'] and not db.execute(
                        'SELECT 1 FROM entrance_attachments WHERE attachment_id=?',(identity,)).fetchone():
                    raise PermissionError('phone_chat_media_owner_required')
                total = media['total']
                if media['received'] != total or offset>total:
                    raise Conflict('phone_chat_download_offset')
                parts = db.execute('''SELECT start,body FROM phone_chat_chunks WHERE attachment_id=?
                    AND start+length(body)>? AND start<? ORDER BY start''',(identity,offset,offset+CHUNK))
                raw = b''.join(bytes(r['body'])[max(0,offset-r['start']):] for r in parts)[:CHUNK]
            elif not db.execute('SELECT 1 FROM entrance_attachments WHERE attachment_id=?',(identity,)).fetchone():
                raise PermissionError('phone_chat_media_not_visible')
        if media is None:
            adapter = getattr(self.store,'entrance_media',None)
            receipt = adapter.materialize(identity) if adapter else {}
            if receipt.get('state') != 'available':
                raise Conflict('phone_chat_attachment_unavailable')
            total = receipt['size']
            if offset>total or total>MAX_MEDIA:
                raise ValueError('phone_chat_download_offset')
            with Path(receipt['path']).open('rb') as stream:
                stream.seek(offset)
                raw = stream.read(CHUNK)
        return {'attachment_id':identity,'next_offset':offset+len(raw),'total':total,
                'base64':base64.b64encode(raw).decode('ascii')}

    def _events(self, db, args):
        _fields(args, ('after',), ('limit',))
        after,limit = _int(args['after'],(1<<63)-1),_int(args.get('limit',50),100)
        if limit<1:
            raise ValueError('phone_chat_page_invalid')
        events = []
        for row in db.execute('SELECT * FROM phone_chat_feed WHERE sequence>? ORDER BY sequence LIMIT ?', (after,limit)):
            after = row['sequence']
            if row['kind']=='event':
                e = db.execute('SELECT * FROM entrance_events WHERE event_id=? AND conversation_id=?',
                               (row['ref'],self.policy['conversation_id'])).fetchone()
                if e is None:
                    continue
                meta = json.loads(e['metadata'])
                original = meta.get('original_time') or {}
                millis = (int(original['raw']) if original.get('known') else meta.get('created_at_ms'))
                if millis is None and e['direction']!='inbound':
                    millis = int(e['received_at']*1000)
                attachments=[]
                for a in json.loads(e['attachments']):
                    if a.get('attachment_id'):
                        name=a.get('name') or a.get('file_name') or a.get('kind') or 'attachment'
                        mime=a.get('mime') or mimetypes.guess_type(name)[0] or {'image':'image/*','video':'video/*','voice':'audio/*'}.get(a.get('kind'),'application/octet-stream')
                        attachments.append({'attachment_id':a['attachment_id'],'name':name,'mime':mime,'bytes':a.get('bytes',a.get('size'))})
                role = {'inbound': 'user', 'outbound': 'assistant', 'internal': 'system'}[e['direction']]
                if e['kind'] == 'history' and meta.get('history_only'):
                    role = meta['original_role']
                event = {'event_id':e['event_id'],'kind':e['kind'],'role':role,
                         'text':e['text'],'created_at_ms':millis,'task_id':e['task_id'],'reply_to':e['reply_to'],'attachments':attachments}
            else:
                delta = row['kind']=='delta'
                event={'event_id':'feed-'+str(after),'kind':'delta' if delta else 'receipt',
                       'role':'assistant' if delta else 'system','text':row['body'] if delta else '',
                       'created_at_ms':row['observed_ms'],'reply_to':row['parent'],'attachments':[],'task_id':None}
                if delta:
                    event['item_id']=row['ref']
                else:
                    event['state']=row['body']
                    event['task_id' if row['kind']=='receipt' else 'request_id']=row['ref']
            event['sequence']=after
            events.append(event)
        approvals = []
        for row in db.execute("""SELECT i.* FROM interactions i WHERE i.kind='approval' AND i.answer IS NULL
                AND EXISTS (SELECT 1 FROM entrance_events e WHERE e.kind='message' AND e.conversation_id=?
                AND (e.task_id=i.task_id OR e.source_ref=i.task_id)) ORDER BY i.created""",(self.policy['conversation_id'],)):
            task = db.execute('SELECT * FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
            if self.store._interaction_is_live(db,task,row):
                params=json.loads(row['params'])
                prompt='\n'.join(str(params[k]) for k in ('reason','command','cwd','grantRoot') if k in params)
                approvals.append({'interaction_id':row['id'],'task_id':row['task_id'],'prompt':prompt or row['id']})
        return {'events':events,'next_cursor':after,'approvals':approvals,'mode':self._mode(db)}

    def _approve(self, db, args):
        _fields(args, ('interaction_id','decision'))
        identity, decision = _text(args['interaction_id'],1024),args['decision']
        if decision not in ('accept','decline'):
            raise ValueError('phone_chat_decision_invalid')
        row = db.execute("""SELECT i.* FROM interactions i WHERE i.id=? AND i.kind='approval'
            AND EXISTS (SELECT 1 FROM entrance_events e WHERE e.kind='message' AND e.conversation_id=?
            AND (e.task_id=i.task_id OR e.source_ref=i.task_id))""",(identity,self.policy['conversation_id'])).fetchone()
        if row is None:
            raise PermissionError('phone_chat_interaction_not_visible')
        answer={'decision':decision}
        if row['answer']:
            if json.loads(row['answer'])!=answer:
                raise Conflict('interaction_already_answered')
        else:
            task=db.execute('SELECT * FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
            if not self.store._interaction_is_live(db,task,row):
                raise Conflict('interaction_not_live')
            self.store._validate_interaction_answer(row,answer)
            db.execute('UPDATE interactions SET answer=? WHERE id=?',(canonical(answer),identity))
        return {'interaction_id':identity,'task_id':row['task_id'],'decision':decision}
