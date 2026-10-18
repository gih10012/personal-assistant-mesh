"""Full authorized Claw entrance CLI over its existing task/authority, not iLink.

No second receiver, account copy, direct sender, native tool gate or operator key.
Only owner-installed entrance Store composition exposes entrance_claw actions.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from .config import private_json
from .entrance import canonical, digest
from .store import Conflict

MAX_BYTES = 32 * 1024 * 1024
COMMANDS = ('status', 'history', 'context', 'send-status', 'send-text', 'send-media',
            'download', 'recover-context')


def source_file(path, maximum):
    """Same-execution-node adapter; bytes/name/hash bind a later authority read."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('claw_absolute_source_file_required')
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or not 0 < before.st_size <= maximum):
            raise ValueError('claw_owned_source_file_required')
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())
    version = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_ctime_ns)
    if len(data) > maximum or version(before) != version(after) or version(path.lstat()) != version(before):
        raise ValueError('claw_source_file_changed')
    return data, {'path': str(path), 'filename': path.name, 'size': len(data),
                  'sha256': hashlib.sha256(data).hexdigest()}


def _request_id(value):
    if not isinstance(value, str) or re.fullmatch('[A-Za-z0-9_-]{1,80}', value) is None:
        raise ValueError('claw_original_request_id_required')
    return value


def claw_store_class(base, uploader=None, max_bytes=MAX_BYTES, recovery_configured=False):
    if (not callable(getattr(base, '_entrance_task', None)) or type(max_bytes) is not int
            or not 0 < max_bytes <= 100 * 1024 * 1024):
        raise ValueError('claw_authority_configuration_required')

    class ClawStore(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            with self.transaction() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS claw_uploads(
                    request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,item TEXT,task_id TEXT NOT NULL,created REAL NOT NULL)''')
                db.execute('''CREATE TABLE IF NOT EXISTS claw_calls(
                    call_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,request_id TEXT NOT NULL)''')

        def _claw_status(self, db, request_id):
            row = db.execute('SELECT id,status,created FROM outbox WHERE id=?', (request_id,)).fetchone()
            upload = db.execute('SELECT state FROM claw_uploads WHERE request_id=?', (request_id,)).fetchone()
            value = {'request_id': request_id, 'status': row['status'] if row else 'not_enqueued',
                'delivery_verified': False, 'automatic_retry': False,
                'upload_state': upload['state'] if upload else None,
                'outcome_unknown': bool(row and row['status'] in ('submitting', 'unknown'))
                    or bool(upload and upload['state'] in ('reserved', 'unknown'))}
            if row:
                # Fixed receipt projection, never historical private transport detail.
                value.update(self._entrance_send_status(db, request_id), enqueued_at=row['created'])
            return value

        def _claw_context(self, db):
            context = self._meta(db, 'owner_context', {})
            reference = context.get('version')
            row = db.execute('SELECT body,created FROM inbox WHERE id=?', (reference,)).fetchone()
            original = json.loads(row['body']) if row else {}
            return {'reply_context_present': bool(context.get('token')), 'source_ref': reference,
                'source_message_id': str(original['message_id']) if original.get('message_id') is not None else None,
                'original_created_at_ms': str(original.get('create_time_ms', context.get('created')))
                    if original.get('create_time_ms', context.get('created')) is not None else None,
                'received_at': row['created'] if row else None,
                'last_poll_at': self._meta(db, 'last_poll'), 'reply_context_validity': 'unknown',
                'native_refresh_configured_not_live_verified': bool(recovery_configured)}

        def agent_action(self, task_id, node, epoch, action_id, action, arguments):
            if action != 'entrance_claw':
                return super().agent_action(task_id, node, epoch, action_id, action, arguments)
            if (not isinstance(arguments, dict) or set(arguments) != {'command', 'arguments'}
                    or arguments['command'] not in COMMANDS or not isinstance(arguments['arguments'], dict)):
                raise ValueError('claw_arguments_invalid')
            if not isinstance(action_id, str) or not 0 < len(action_id.encode()) <= 1000 or '\0' in action_id:
                raise ValueError('claw_original_call_id_required')
            command, args = arguments['command'], arguments['arguments']
            if command == 'download':
                if set(args) != {'attachment_id'}:
                    raise ValueError('claw_arguments_invalid')
                return super().agent_action(task_id, node, epoch, action_id, 'entrance_media', args)
            with self.transaction() as db:
                self._entrance_task(db, task_id, node, epoch, True)
                if command == 'history':
                    return self._entrance_history(db, args)
                if command in ('context', 'status'):
                    if args:
                        raise ValueError('claw_arguments_invalid')
                    context = self._claw_context(db)
                    if command == 'context':
                        return context
                    return {'context': context, 'channel_error': self._meta(db, 'channel_error'),
                        'outbox': {row[0]: row[1] for row in db.execute('SELECT status,COUNT(*) FROM outbox GROUP BY status')},
                        'single_authority': True, 'new_receiver_started': False,
                        'media_upload_configured': uploader is not None}
                request_id = _request_id(args.get('request_id'))
                if command in ('send-status', 'recover-context'):
                    if set(args) != {'request_id'}:
                        raise ValueError('claw_arguments_invalid')
                    value = self._claw_status(db, request_id)
                    if command == 'recover-context':
                        version = self._meta(db, 'owner_context', {}).get('version', 'none')
                        identity = hashlib.sha256(version.encode()).hexdigest()
                        renewal = db.execute('SELECT id,node,status,created FROM renewals WHERE id=?', (identity,)).fetchone()
                        value.update(recovery_owner='existing_unique_native_refresh_worker',
                            context=self._claw_context(db), renewal=dict(renewal) if renewal else None,
                            state='awaiting_existing_refresher_or_new_owner_context'
                                if value['status'] == 'rejected' else 'original_send_not_context_recovery_eligible',
                            new_refresh_submitted=False, new_ilink_poll_started=False)
                    return value
                call_fingerprint = digest([task_id, command, args])
                previous = db.execute('SELECT * FROM claw_calls WHERE call_id=?', (action_id,)).fetchone()
                if previous and previous['fingerprint'] != call_fingerprint:
                    raise Conflict('claw_original_call_changed')
                if not previous:
                    db.execute('INSERT INTO claw_calls VALUES(?,?,?)', (action_id, call_fingerprint, request_id))
                if command == 'send-text':
                    if set(args) != {'request_id', 'text'} or not isinstance(args['text'], str) or not 0 < len(args['text'].encode()) <= 16000:
                        raise ValueError('claw_text_invalid')
                    if db.execute('SELECT 1 FROM claw_uploads WHERE request_id=?', (request_id,)).fetchone():
                        raise Conflict('claw_original_media_changed')
                    old_send = db.execute('SELECT media_items FROM outbox WHERE id=?', (request_id,)).fetchone()
                    if old_send and old_send['media_items'] is not None:
                        raise Conflict('claw_original_media_changed')
                    # Existing outbox request identity/content check, never POSTs iLink.
                    self._enqueue(db, request_id, args['text'], explicit=True)
                    return self._claw_status(db, request_id)
                if set(args) != {'request_id', 'kind', 'source'} or args['kind'] not in ('file', 'image', 'video'):
                    raise ValueError('claw_media_arguments_invalid')
                source = args['source']
                if not isinstance(source, dict) or set(source) != {'path', 'filename', 'size', 'sha256'}:
                    raise ValueError('claw_source_invalid')
                media_fingerprint = digest([args['kind'], source['filename'], source['size'], source['sha256']])
                operation = db.execute('SELECT * FROM claw_uploads WHERE request_id=?', (request_id,)).fetchone()
                if operation and operation['fingerprint'] != media_fingerprint:
                    raise Conflict('claw_original_media_changed')
                exists = db.execute('SELECT id,media_items FROM outbox WHERE id=?', (request_id,)).fetchone()
                if exists:
                    if exists['media_items'] is None:
                        raise Conflict('claw_original_media_changed')
                    # Legacy media may lack this new upload receipt: do not
                    # reupload to test whether the old bytes were identical.
                    return dict(self._claw_status(db, request_id), replayed=True,
                                new_upload_attempted=False, original_content_revalidated=operation is not None)
                if operation and operation['state'] in ('reserved', 'unknown'):
                    return dict(self._claw_status(db, request_id), replayed=True, new_upload_attempted=False)
                if uploader is None and operation is None:
                    return {'request_id': request_id, 'status': 'media_uploader_not_configured',
                            'new_upload_attempted': False, 'outcome_unknown': False}
                if operation:
                    item = json.loads(operation['item'])
                else:
                    db.execute('INSERT INTO claw_uploads VALUES(?,?,?,NULL,?,?)',
                        (request_id, media_fingerprint, 'reserved', task_id, self.clock()))
                    item = None
            # No SQLite writer is held while reading a file or calling SDK/CDN.
            if item is None:
                try:
                    data, actual = source_file(source['path'], max_bytes)
                    if actual != source:
                        raise ValueError('claw_source_changed')
                    item = uploader(data, args['kind'], source['filename'])
                    serialized = canonical([item])
                    if len(serialized.encode()) > 65536:
                        raise ValueError('claw_media_reference_invalid')
                except Exception:
                    with self.transaction() as db:
                        db.execute("UPDATE claw_uploads SET state='unknown' WHERE request_id=? AND state='reserved'", (request_id,))
                    return {'request_id': request_id, 'status': 'upload_unknown', 'outcome_unknown': True,
                            'automatic_retry': False, 'new_send_submitted': False}
                # Preserve the completed upload even if the task lease expired;
                # only the next transaction may authorize the original enqueue.
                with self.transaction() as db:
                    changed = db.execute("UPDATE claw_uploads SET state='uploaded',item=? WHERE request_id=? AND state='reserved' AND fingerprint=?",
                        (canonical(item), request_id, media_fingerprint)).rowcount
                    if changed != 1:
                        raise Conflict('claw_upload_receipt_changed')
            with self.transaction() as db:
                self._entrance_task(db, task_id, node, epoch, True)
                old = db.execute('SELECT state,fingerprint FROM claw_uploads WHERE request_id=?', (request_id,)).fetchone()
                if old['fingerprint'] != media_fingerprint or old['state'] not in ('uploaded', 'enqueued'):
                    raise Conflict('claw_upload_receipt_changed')
                serialized = canonical([item])
                prior = db.execute('SELECT id FROM outbox WHERE id=?', (request_id,)).fetchone()
                self._enqueue(db, request_id, serialized, explicit=True)
                if not prior:
                    db.execute('UPDATE outbox SET media_items=? WHERE id=?', (serialized, request_id))
                db.execute("UPDATE claw_uploads SET state='enqueued' WHERE request_id=?", (request_id,))
                return self._claw_status(db, request_id)

    return ClawStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'task-id', 'call-id'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--epoch', required=True, type=int)
    commands = parser.add_subparsers(dest='command')
    for name in COMMANDS:
        command = commands.add_parser(name)
        if name in ('send-status', 'send-text', 'send-media', 'recover-context'):
            command.add_argument('--request-id', required=True)
        if name == 'history':
            command.add_argument('--source', choices=('events', 'inbox', 'outbox'), default='events')
            command.add_argument('--after', type=int, default=0); command.add_argument('--limit', type=int, default=50)
        if name == 'send-text':
            command.add_argument('--text-file', required=True)
        if name == 'send-media':
            command.add_argument('--file', required=True); command.add_argument('--kind', choices=('file', 'image', 'video'), default='file')
        if name == 'download':
            command.add_argument('--attachment-id', required=True)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('command required')
    try:
        config = private_json(args.config)
        value = {}
        if args.command in ('send-status', 'send-text', 'send-media', 'recover-context'):
            value['request_id'] = args.request_id
        if args.command == 'history':
            value.update(source=args.source, after=args.after, limit=args.limit)
        elif args.command == 'send-text':
            value['text'] = source_file(args.text_file, 16000)[0].decode('utf8')
        elif args.command == 'send-media':
            value.update(kind=args.kind, source=source_file(args.file, MAX_BYTES)[1])
        elif args.command == 'download':
            value['attachment_id'] = args.attachment_id
        from .worker import Client
        result = Client(config).request('/v1/agent/action', {'task_id': args.task_id, 'epoch': args.epoch,
            'call_id': args.call_id, 'action': 'entrance_claw', 'arguments': {'command': args.command, 'arguments': value}})
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({'ok': False, 'code': 'claw_operation_unconfirmed', 'automatic_retry': False,
                          'original_request_id': getattr(args, 'request_id', None)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
