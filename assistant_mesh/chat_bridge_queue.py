"""Owner-private event queue for ordinary Chat in an owned Firefox tab.

This is not a Chat API, OAuth connector, worker enrollment or native sandbox.
Only the separate installed WebExtension may perform DOM interaction. No
cookies, OpenAI credentials, HTTP inference calls or automatic resubmission.
The native host is a POSIX example; other nodes adapt the same wire contract.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue as queues
import re
import socket
import sqlite3
import stat
import struct
import sys
import threading
import time
from urllib.parse import urlsplit

from .cloud_jobs import _private_dir, _private_file
from .native_messaging import _nonfinite, _private_bytes, _unique
from .store import Conflict


FORMAT = 'mesh-chat-browser/1'
EXTENSION_ID = 'personal-assistant-mesh-chat@gih10012.local'
HOST_NAME = 'personal_assistant_mesh_chat'
MAX_FRAME = 786432
MAX_TEXT = 16384
MAX_RESULT = 524288
DOM_DEFAULTS = {
    'composer_selector': '#prompt-textarea',
    'send_selector': 'button[data-testid="send-button"]',
    'generating_selector': 'button[data-testid="stop-button"]',
    'message_selector': '[data-message-author-role]',
    'role_attribute': 'data-message-author-role',
    'message_id_attribute': 'data-message-id',
}


def account_menu_config(value):
    """Explicit normal UI evidence contract, not a guessed profile selector."""
    if value is None:
        return None  # old config may read results, but DOM account gate blocks
    if (not isinstance(value, dict) or set(value) != {'steps', 'current_selector', 'selected_attribute', 'selected_value'}
            or not isinstance(value['steps'], list) or not 1 <= len(value['steps']) <= 3):
        raise ValueError('chat_selected_account_contract_invalid')
    pairs = {('aria-checked', 'true'), ('aria-selected', 'true'), ('aria-current', 'true'),
             ('aria-current', 'page'), ('data-selected', 'true'), ('data-state', 'checked'),
             ('data-state', 'selected'), ('data-state', 'active')}
    for text in (value['current_selector'], value['selected_attribute'], value['selected_value']):
        if not isinstance(text, str) or not 1 <= len(text) <= 256 or re.search(r'[\x00-\x1f\x7f]', text):
            raise ValueError('chat_selected_account_contract_invalid')
    if (value['selected_attribute'], value['selected_value']) not in pairs:
        raise ValueError('chat_selected_account_contract_invalid')
    steps = []
    for item in value['steps']:
        if (not isinstance(item, dict) or set(item) != {'control_selector', 'menu_selector', 'menu_role'}
                or item['menu_role'] not in ('menu', 'listbox', 'dialog')
                or any(not isinstance(item[key], str) or not 1 <= len(item[key]) <= 256
                       or re.search(r'[\x00-\x1f\x7f]', item[key]) for key in ('control_selector', 'menu_selector'))):
            raise ValueError('chat_selected_account_contract_invalid')
        steps.append(dict(item))
    return dict(value, steps=steps)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def identifier(value):
    if (not isinstance(value, str) or not 1 <= len(value.encode('utf8')) <= 200
            or value != value.strip() or re.search(r'[\x00-\x1f\x7f]', value)):
        raise ValueError('chat_identifier_invalid')
    return value


def conversation(value):
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError('chat_original_conversation_required')
    item = urlsplit(value)
    if (item.scheme != 'https' or item.netloc != 'chatgpt.com'
            or item.query or item.fragment
            or not re.fullmatch(r'/(?:g/[A-Za-z0-9_-]+/)?c/[A-Za-z0-9-]{10,}', item.path)):
        raise ValueError('chat_original_conversation_required')
    return value


def configuration(value):
    fields = {'schema', 'enabled', 'owner_id', 'state_dir', 'expected_account', 'projects', 'dom'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['enabled']) is not bool):
        raise ValueError('chat_configuration_invalid')
    account = value['expected_account']
    if (not isinstance(account, str) or not re.fullmatch(r'[^\s@]+@163\.com', account)
            or len(account.encode('utf8')) > 254):
        raise ValueError('chat_primary_163_account_required')
    projects, dom = value['projects'], value['dom']
    if (not isinstance(projects, dict) or not 1 <= len(projects) <= 64
            or not isinstance(dom, dict)
            or not {'account_selector', 'account_attribute', 'completion_selector'} <= set(dom)
            or set(dom) - set(DOM_DEFAULTS) - {'account_selector', 'account_attribute', 'completion_selector', 'account_menu'}):
        raise ValueError('chat_bindings_invalid')
    dom = dict(DOM_DEFAULTS, **dom)
    if any(not isinstance(item, str) or not 1 <= len(item) <= 256
           or re.search(r'[\x00-\x1f\x7f]', item) for key, item in dom.items() if key != 'account_menu'):
        raise ValueError('chat_dom_contract_invalid')
    if dom['account_attribute'] != 'textContent' and not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', dom['account_attribute']):
        raise ValueError('chat_dom_contract_invalid')
    dom['account_menu'] = account_menu_config(dom.get('account_menu'))
    root = Path(value['state_dir'])
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('chat_private_directory_required')
    bindings = {identifier(key): conversation(url) for key, url in projects.items()}
    if len(set(bindings.values())) != len(bindings):
        raise ValueError('chat_shared_conversation_requires_single_project')
    return {'owner_id': identifier(value['owner_id']), 'state_dir': str(root),
            'enabled': value['enabled'], 'account_sha256': hashlib.sha256(account.lower().encode()).hexdigest(),
            'projects': bindings, 'dom': dom}


class ChatBridgeQueue:
    """One immutable request per Mesh child task, private durable send intent."""
    def __init__(self, config, clock=time.time):
        self.config, self.clock = configuration(config), clock
        self.root = _private_dir(self.config['state_dir'], create=True)
        self.database = self.root / 'queue.sqlite'
        _private_file(self.database, create=True)
        self.namespace = {'format': FORMAT, 'owner': self.config['owner_id'],
                          'account': self.config['account_sha256']}
        with self.transaction(initial=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS chat_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS chat_projects(id TEXT PRIMARY KEY,url TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS chat_requests(
                request_id TEXT PRIMARY KEY,task_id TEXT NOT NULL UNIQUE,project_id TEXT NOT NULL,
                body TEXT NOT NULL,content_sha256 TEXT NOT NULL,state TEXT NOT NULL,
                send_intent INTEGER NOT NULL DEFAULT 0,result TEXT,created REAL NOT NULL,updated REAL NOT NULL)''')
            old = dict(db.execute('SELECT key,value FROM chat_metadata'))
            if old and old != self.namespace:
                raise Conflict('chat_owner_namespace_conflict')
            if not old:
                if db.execute('SELECT 1 FROM chat_requests LIMIT 1').fetchone():
                    raise Conflict('chat_owner_namespace_missing')
                db.executemany('INSERT INTO chat_metadata VALUES(?,?)', self.namespace.items())
            for project, url in self.config['projects'].items():
                prior = db.execute('SELECT url FROM chat_projects WHERE id=?', (project,)).fetchone()
                if prior and prior[0] != url:
                    raise Conflict('chat_project_conversation_conflict')
                db.execute('INSERT OR IGNORE INTO chat_projects VALUES(?,?)', (project, url))

    @contextlib.contextmanager
    def transaction(self, initial=False):
        _private_dir(self.root)
        _private_file(self.database)
        db = sqlite3.connect(str(self.database), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            if not initial and dict(db.execute('SELECT key,value FROM chat_metadata')) != self.namespace:
                raise Conflict('chat_owner_namespace_conflict')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _row(self, db, request_id):
        row = db.execute('SELECT * FROM chat_requests WHERE request_id=?', (identifier(request_id),)).fetchone()
        if row is None:
            raise PermissionError('chat_request_not_found')
        body = json.loads(row['body'])
        if digest(body) != row['content_sha256'] or body['request_id'] != row['request_id']:
            raise Conflict('chat_original_content_changed')
        return row

    @staticmethod
    def _view(row):
        return {'format': FORMAT, 'request_id': row['request_id'], 'task_id': row['task_id'],
                'project_id': row['project_id'], 'content_sha256': row['content_sha256'],
                'state': row['state'], 'send_intent': bool(row['send_intent']),
                'result_available': row['result'] is not None,
                'retry_with_new_id': False, 'execution_verified': False,
                'account_verified': False, 'native_tools_intercepted': False}

    def enqueue(self, payload):
        if not self.config['enabled']:
            raise PermissionError('chat_bridge_not_enabled')
        if not isinstance(payload, dict) or set(payload) != {'request_id', 'task_id', 'project_id', 'input'}:
            raise ValueError('chat_request_invalid')
        body = {key: identifier(payload[key]) for key in ('request_id', 'task_id', 'project_id')}
        text = payload['input']
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf8')) > MAX_TEXT:
            raise ValueError('chat_task_text_invalid')
        project = body['project_id']
        if project not in self.config['projects']:
            raise PermissionError('chat_project_not_configured')
        body.update(input=text, conversation_url=self.config['projects'][project],
                    account_sha256=self.config['account_sha256'])
        fingerprint = digest(body)
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM chat_requests WHERE request_id=? OR task_id=?',
                               (body['request_id'], body['task_id'])).fetchone()
            if prior:
                if prior['content_sha256'] != fingerprint:
                    raise Conflict('chat_original_request_conflict')
                result = self._view(self._row(db, body['request_id']))
            else:
                now = self.clock()
                db.execute('''INSERT INTO chat_requests(request_id,task_id,project_id,body,content_sha256,
                    state,created,updated) VALUES(?,?,?,?,?,'queued',?,?)''',
                           (body['request_id'], body['task_id'], project, encoded(body).decode(), fingerprint, now, now))
                result = self._view(self._row(db, body['request_id']))
        self.wake()
        return result

    def status(self, request_id):
        with self.transaction() as db:
            return self._view(self._row(db, request_id))

    def offers(self):
        if not self.config['enabled']:
            return []
        with self.transaction() as db:
            result, seen = [], set()
            for row in db.execute("SELECT * FROM chat_requests WHERE state!='completed' ORDER BY created,rowid"):
                project = row['project_id']
                if project in seen or project not in self.config['projects']:
                    continue
                seen.add(project)
                checked = self._row(db, row['request_id'])
                body = json.loads(checked['body'])
                prompt = 'Mesh request: {}\nMesh content SHA-256: {}\n\n{}'.format(
                    row['request_id'], row['content_sha256'], body['input'])
                result.append(dict(self._view(row), conversation_url=body['conversation_url'],
                                   account_sha256=body['account_sha256'], prompt=prompt,
                                   dom=dict(self.config['dom']),
                                   mode='observe' if row['send_intent'] else 'prepare'))
            return result

    def begin_send(self, request_id, content_sha256):
        """The ONLY send grant: commit before replying; repeat grants are false."""
        with self.transaction() as db:
            row = self._row(db, request_id)
            if row['content_sha256'] != content_sha256:
                raise Conflict('chat_original_request_conflict')
            if not self.config['enabled'] or row['project_id'] not in self.config['projects']:
                raise PermissionError('chat_bridge_not_enabled')
            if row['send_intent'] or row['state'] == 'completed':
                return dict(self._view(row), send_granted=False)
            # A later task may not bypass an unresolved turn in this project.
            first = db.execute("SELECT request_id FROM chat_requests WHERE project_id=? AND state!='completed' ORDER BY created,rowid LIMIT 1",
                               (row['project_id'],)).fetchone()
            if not first or first[0] != request_id:
                raise Conflict('chat_project_turn_unresolved')
            db.execute("UPDATE chat_requests SET send_intent=1,state='unknown',updated=? WHERE request_id=?",
                       (self.clock(), request_id))
            return dict(self._view(self._row(db, request_id)), send_granted=True)

    def record(self, request_id, content_sha256, observation):
        allowed = {'state', 'reason', 'text', 'conversation_url', 'account_sha256',
                   'user_message_id', 'assistant_message_id', 'terminal_visible', 'scope'}
        if not isinstance(observation, dict) or set(observation) != allowed:
            raise ValueError('chat_observation_invalid')
        value = dict(observation)
        if (value['state'] not in ('partial', 'unknown', 'blocked', 'completed')
                or not isinstance(value['reason'], str) or not re.fullmatch(r'[a-z_]{1,80}', value['reason'])
                or not isinstance(value['text'], str) or len(value['text'].encode('utf8')) > MAX_RESULT
                or type(value['terminal_visible']) is not bool or value['scope'] != 'visible_dom_text'
                or any(not isinstance(value[key], str) or len(value[key]) > 200
                       for key in ('user_message_id', 'assistant_message_id'))):
            raise ValueError('chat_observation_invalid')
        with self.transaction() as db:
            row = self._row(db, request_id)
            body = json.loads(row['body'])
            if (row['content_sha256'] != content_sha256
                    or value['conversation_url'] != body['conversation_url']
                    or value['account_sha256'] != body['account_sha256']):
                raise Conflict('chat_observation_binding_conflict')
            if value['state'] == 'completed' and (not value['terminal_visible'] or not value['text']
                                                 or not value['user_message_id'] or not value['assistant_message_id']):
                raise ValueError('chat_terminal_evidence_required')
            if value['state'] != 'completed' and value['terminal_visible']:
                raise ValueError('chat_terminal_evidence_mismatch')
            value.update(text_sha256=hashlib.sha256(value['text'].encode('utf8')).hexdigest(),
                         text_bytes=len(value['text'].encode('utf8')))
            serialized = encoded(value).decode()
            if row['state'] == 'completed':
                if row['result'] != serialized:
                    raise Conflict('chat_completed_result_changed')
                return self._view(row)
            # Even a lost local grant ACK is now observation-only. Blocked
            # before any send remains queued and is retried only on a wake.
            sent = row['send_intent'] or value['state'] in ('completed', 'partial', 'unknown')
            state = value['state'] if sent else 'queued'
            db.execute('UPDATE chat_requests SET send_intent=?,state=?,result=?,updated=? WHERE request_id=?',
                       (int(sent), state, serialized, self.clock(), request_id))
            return self._view(self._row(db, request_id))

    def result(self, request_id):
        with self.transaction() as db:
            row = self._row(db, request_id)
            output = self._view(row)
            if row['result'] is not None:
                value = json.loads(row['result'])
                raw = value['text'].encode('utf8')
                if hashlib.sha256(raw).hexdigest() != value['text_sha256'] or len(raw) != value['text_bytes']:
                    raise Conflict('chat_saved_result_changed')
                output['result'] = value
            return output

    def wake(self):
        """Event notification, not a timer or proof that a browser is available."""
        path = self.root / 'wake.sock'
        try:
            item = path.lstat()
            if not stat.S_ISSOCK(item.st_mode) or item.st_uid != os.getuid() or stat.S_IMODE(item.st_mode) != 0o600:
                return False
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.25)
                client.connect(str(path))
                client.sendall(b'wake\n')
            return True
        except OSError:
            return False


def read_frame(stream):
    def exact(size, first=False):
        parts = []
        while size:
            part = stream.read(size)
            if not part and first and not parts:
                return None
            if not part:
                raise ValueError('chat_native_frame_invalid')
            parts.append(part)
            size -= len(part)
        return b''.join(parts)
    header = exact(4, first=True)
    if header is None:
        return None
    size = struct.unpack('=I', header)[0]
    if not 1 <= size <= MAX_FRAME:
        raise ValueError('chat_native_frame_invalid')
    value = json.loads(exact(size).decode('utf8'), object_pairs_hook=_unique, parse_constant=_nonfinite)
    encoded(value)
    return value


def write_frame(stream, value):
    raw = encoded(value)
    if len(raw) > MAX_FRAME:
        raise ValueError('chat_native_frame_invalid')
    remaining = memoryview(struct.pack('=I', len(raw)) + raw)
    while remaining:
        count = stream.write(remaining)
        if not count:
            raise OSError('chat_native_output_closed')
        remaining = remaining[count:]
    stream.flush()


def native_host(journal, incoming, outgoing):
    """Long event port; one owned host, durable offers + owner Unix wake IPC."""
    _private_file(journal.root / 'host.lock', create=True)
    lock = os.open(str(journal.root / 'host.lock'), os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        os.close(lock)
        raise Conflict('chat_host_already_owned') from None
    events, stopping = queues.Queue(), threading.Event()
    server, pathname = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM), journal.root / 'wake.sock'
    bound_inode = None
    try:
        if pathname.exists():
            old = pathname.lstat()
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid():
                raise PermissionError('chat_wake_socket_not_owned')
            pathname.unlink()  # our private stale socket, exclusive host lock
        server.bind(str(pathname))
        os.chmod(str(pathname), 0o600)
        bound_inode = pathname.lstat().st_ino
        server.listen(4)
        server.settimeout(0.25)

        def receive():
            try:
                while not stopping.is_set():
                    item = read_frame(incoming)
                    events.put(('native', item))
                    if item is None:
                        break
            except Exception:
                events.put(('native', None))

        def notify():
            while not stopping.is_set():
                try:
                    client, _ = server.accept()
                    with client:
                        client.settimeout(0.25)
                        if hasattr(socket, 'SO_PEERCRED'):
                            credentials = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                            if struct.unpack('3i', credentials)[1] != os.getuid():
                                continue
                        if client.recv(6) == b'wake\n':
                            events.put(('wake', None))
                except (OSError, socket.timeout):
                    pass

        threading.Thread(target=receive, daemon=True).start()
        thread = threading.Thread(target=notify, daemon=True)
        thread.start()
        write_frame(outgoing, {'schema': 1, 'event': 'hello', 'format': FORMAT,
                               'owner_id': journal.config['owner_id'], 'enabled': journal.config['enabled']})
        delivered = set()

        def offers(force=False):
            for item in journal.offers():
                key = (item['request_id'], item['send_intent'])
                if force or key not in delivered:
                    write_frame(outgoing, {'schema': 1, 'event': 'work', 'job': item})
                    delivered.add(key)
        offers()
        while True:
            kind, message = events.get()
            if kind == 'wake':
                offers(force=True)  # same identity; browser must inspect its intent
                continue
            if message is None:
                break
            if (not isinstance(message, dict) or set(message) - {'schema', 'action', 'call_id', 'request_id', 'content_sha256', 'observation'}
                    or type(message.get('schema')) is not int or message.get('schema') != 1):
                raise ValueError('chat_native_message_invalid')
            call = identifier(message.get('call_id'))
            action = message.get('action')
            if action == 'begin_send' and set(message) == {'schema', 'action', 'call_id', 'request_id', 'content_sha256'}:
                data = journal.begin_send(message['request_id'], message['content_sha256'])
            elif action == 'report' and set(message) == {'schema', 'action', 'call_id', 'request_id', 'content_sha256', 'observation'}:
                data = journal.record(message['request_id'], message['content_sha256'], message['observation'])
            else:
                raise ValueError('chat_native_message_invalid')
            write_frame(outgoing, {'schema': 1, 'event': 'reply', 'call_id': call, 'data': data})
            if action == 'report' and data['state'] == 'completed':
                offers()
        stopping.set()
        thread.join(1)
    finally:
        stopping.set()
        server.close()
        if bound_inode is not None:
            try:
                if pathname.lstat().st_ino == bound_inode:
                    pathname.unlink()
            except FileNotFoundError:
                pass
        os.close(lock)
    return 0


def load_config(path):
    return json.loads(_private_bytes(path).decode('utf8'), object_pairs_hook=_unique, parse_constant=_nonfinite)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('action', choices=('enqueue', 'status', 'result', 'wake', 'native-host'))
    parser.add_argument('--request-file')
    parser.add_argument('--request-id')
    parser.add_argument('--extension-id')
    args = parser.parse_args(argv)
    try:
        journal = ChatBridgeQueue(load_config(args.config))
        if args.action == 'native-host':
            if args.extension_id != EXTENSION_ID:
                raise PermissionError('chat_native_caller_not_allowed')
            return native_host(journal, sys.stdin.buffer, sys.stdout.buffer)
        if args.action == 'enqueue':
            result = journal.enqueue(load_config(args.request_file))
        elif args.action in ('status', 'result'):
            result = getattr(journal, args.action)(args.request_id)
        else:
            result = {'format': FORMAT, 'wake_notified': journal.wake(), 'execution_verified': False}
        print(encoded(result).decode())
        return 0
    except Exception:
        sys.stderr.write('Chat browser bridge unavailable\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
