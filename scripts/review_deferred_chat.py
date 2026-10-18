"""Owner-only settlement candidate for ONE audited, explicitly deferred Chat turn.

Never queues the original input, invokes Chat/models, changes auth, or rewrites
native state/history. Default audit may restore the EXISTING backup only into a
disposable private DB. The operations owner must independently approve this case.
No admission hook broadens review_native_effects.py's different closed case.
"""
import argparse
import contextlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import quote

_loader = importlib.util.spec_from_file_location('_deferred_effect_utilities',
    str(Path(__file__).with_name('review_native_effects.py')))
util = importlib.util.module_from_spec(_loader)
_loader.loader.exec_module(util)
require, digest, parse = util.require, util.digest, util.parse
PROTOCOL = 'owner-deferred-chat-effects/1'
NAMESPACE = 'owner-deferred-chat-effects-v1'

# Public opaque pins, not private native identities or command/chat contents.
# A new source-reviewed case is required to change any of these. No spec override.
CLOSED = {
    'task': 'e56e1ac07d6b175d3cccbe9d23e64bca999c3bef4153954e93d679ed38a81ba8',
    'session': '8bca0b3997fe6852cf45e25df22738a8380b3d97cc2f36577b415c301842a643',
    'rollout': '9930f864b77089a6308d275be967803026eccc5ad0044ec22535c5a9e7ebeee6',
    'rollout_size': 18632493,
    'selected': 'd1c4a53ba5eee1bc5a2d27f279368e97a48174fd0c9a8cdd4c31e952d6702d11',
    'selected_size': 267291,
    'old_artifact': '1d24a9aa825ed2ff5549f710889f315aa37fc7eb20341eac6c30a9ece78688f8',
    'backup': '844c498c6a34e5f8004c1c31a66ebc25a1ee0b3e8d008afba4f43805658592d8',
    'backup_sql': 'bc7b1d5d4c877338c43f8b90f73b52a69ffb899095435f41a20ed77a952b66c2',
    'backup_size': 298121656, 'backup_sql_size': 528766045,
    'closure_targets': 'e1e6271621bf56ee61ca66c25bf9fa7954ab5c24c5f1e3787320fc46eb23896d',
    'disabled_config': '59410c1abdf38c73fbbee5f21a38aa6ccfa05e70f9a249d81c8bc0c63a6ffeb4',
    'queue_row': '08bdb8eeee3fba01362e6ff097a137d252b55516c0f946338fc520e30b469fe0',
    'payload': '0710bae8e4502e11752dcd94fa0c1b02321c713ad35911ed7382713e0a0fc70d',
}


def load(path, expired=False):
    raw, version = util.read_checked(path, private=True, limit=util.MAX_SPEC)
    spec = parse(raw.decode('utf8'))
    util.exact(spec, ('protocol', 'issued_at', 'expires_at', 'worker_config', 'server_config',
        'task_id', 'epoch', 'rollout_path', 'old_artifact', 'backup_path', 'review_root',
        'local_observer'), 'deferred_spec_shape')
    require(spec['protocol'] == PROTOCOL, 'deferred_spec_protocol')
    start, end = spec['issued_at'], spec['expires_at']
    require(type(start) in (int, float) and type(end) in (int, float)
            and 0 < end - start <= 1800, 'deferred_spec_window')
    require(expired or start <= time.time() <= end, 'deferred_spec_expired')
    require(digest(spec['old_artifact']) == CLOSED['old_artifact'], 'deferred_old_artifact_case')
    local = spec['local_observer']
    util.exact(local, ('ssh_executable', 'ssh_config', 'alias', 'targets'), 'deferred_observer_shape')
    require(digest(local['targets']) == CLOSED['closure_targets'], 'deferred_closure_case')
    require(isinstance(local['alias'], str) and __import__('re').fullmatch(
        '[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', local['alias']), 'deferred_observer_alias')
    return spec, version


def selected(db, auth, spec):
    value = util.quota.snapshot(db, spec['task_id'], spec['epoch'], auth)
    require(digest(value['task']) == CLOSED['task'] and digest(value['session']) == CLOSED['session'],
            'deferred_original_generation_changed')
    require(value['task']['parent_id'] is None and not value['checkpoint'].get('goal')
            and not value['checkpoint'].get('native_lifecycle')
            and value['checkpoint'].get('approval_required') is not True,
            'deferred_unreviewed_lifecycle')
    state = parse(value['session']['state'])
    require(not state.get('goal') and not state.get('native_lifecycle')
            and not state.get('side_effect_started'), 'deferred_unsettled_native_state')
    require(not db.execute('SELECT 1 FROM tasks WHERE parent_id=? LIMIT 1',
                          (spec['task_id'],)).fetchone(), 'deferred_has_child')
    for row in db.execute("SELECT checkpoint FROM tasks WHERE scope=? AND id<>? AND status<>'completed'",
                          (value['task']['scope'], spec['task_id'])):
        require(not parse(row[0]).get('side_effect_started'), 'deferred_other_scope_guard')
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in util.DEPENDENCY_TABLES & tables:
        for row in db.execute('SELECT * FROM ' + name):
            require(not any(isinstance(cell, str) and spec['task_id'] in cell for cell in row),
                    'deferred_has_dependency')
    util.old_artifact(db, value, {'expected': {'old_artifact': spec['old_artifact'],
                                            'rollout_path': spec['rollout_path']}})
    return value


def native(spec, value):
    path = util.quota.canonical_rollout(spec['rollout_path'], value)
    raw, version = util.read_checked(path, private=True)
    require(version['sha256'] == CLOSED['rollout'] and len(raw) == CLOSED['rollout_size'],
            'deferred_full_history_changed')
    active, lines = False, []
    for line in raw.splitlines(True):
        require(line.endswith(b'\n'), 'deferred_incomplete_history')
        record = parse(line.decode('utf8'))
        payload = record.get('payload') or {}
        if record.get('type') == 'event_msg' and payload.get('type') == 'task_started':
            require(not active, 'deferred_later_turn')
            active = payload.get('turn_id') == value['turn']
        if active:
            lines.append(line)
    segment = b''.join(lines)
    require(util.sha(segment) == CLOSED['selected'] and len(segment) == CLOSED['selected_size'],
            'deferred_closed_nine_call_case_changed')
    records = [parse(line.decode('utf8')) for line in lines]
    last = records[-1]['payload']
    require(last.get('type') == 'task_complete' and last.get('last_agent_message') is None
            and last.get('error', {}).get('codex_error_info') == 'usage_limit_exceeded',
            'deferred_failure_not_terminal')
    return raw, version


# Both modes use the original host's actual lock, never PID absence/file=false.
# The default audit is a sample only. APPLY uses one continuously held fd through
# COMMIT and state readback; only an explicit same-nonce release ends that hold.
_PEER_OBSERVER = r'''
import contextlib,fcntl,hashlib,json,os,pathlib,sqlite3,stat,sys,time
from urllib.parse import quote
def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
def read(path):
 p=pathlib.Path(path)
 for q in (p,)+tuple(p.parents):
  m=q.lstat()
  if stat.S_ISLNK(m.st_mode) or m.st_uid not in (0,os.getuid()):raise ValueError()
 m=p.lstat()
 if not stat.S_ISREG(m.st_mode) or m.st_uid!=os.getuid() or stat.S_IMODE(m.st_mode)!=384 or m.st_size>1048576:raise ValueError()
 f=os.open(str(p),os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(f,'rb') as h:raw=h.read(1048577)
 same=lambda s:(s.st_dev,s.st_ino,s.st_uid,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
 if len(raw)>1048576 or same(p.lstat())!=same(m):raise ValueError()
 return raw
def lock_identity(fd):
 m=os.fstat(fd)
 if not stat.S_ISREG(m.st_mode) or m.st_uid!=os.getuid() or stat.S_IMODE(m.st_mode)!=384:raise ValueError()
 return [m.st_dev,m.st_ino,m.st_uid,stat.S_IMODE(m.st_mode)]
def acquire(t):
 p=pathlib.Path(t['queue_path']).parent/'host.lock'
 fd=os.open(str(p),os.O_RDONLY|os.O_NOFOLLOW)
 try:
  identity=lock_identity(fd)
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  m=p.lstat()
  if [m.st_dev,m.st_ino,m.st_uid,stat.S_IMODE(m.st_mode)]!=identity:raise ValueError()
  return fd,identity
 except BaseException:
  os.close(fd);raise
def observe(request,fd,identity):
 t=request['targets']
 p=pathlib.Path(t['queue_path']).parent/'host.lock';m=p.lstat()
 if lock_identity(fd)!=identity or [m.st_dev,m.st_ino,m.st_uid,stat.S_IMODE(m.st_mode)]!=identity:raise ValueError()
 config_raw=read(t['owner_config_path']);config=json.loads(config_raw)
 if config['enabled'] is not False:raise ValueError()
 queue=pathlib.Path(t['queue_path'])
 if str(queue.parent)!=config['state_dir']:raise ValueError()
 q=queue.lstat()
 if not stat.S_ISREG(q.st_mode) or q.st_uid!=os.getuid() or stat.S_IMODE(q.st_mode)!=384:raise ValueError()
 with contextlib.closing(sqlite3.connect('file:'+quote(str(queue),safe='/')+'?mode=ro',uri=True)) as db:
  db.row_factory=sqlite3.Row;db.execute('BEGIN')
  row=dict(db.execute('SELECT * FROM chat_requests WHERE request_id=?',(t['request_id'],)).fetchone())
  if row['state']!='queued' or row['send_intent']!=0:raise ValueError()
 payload=read(t['payload_path'])
 return {'nonce':request['nonce'],'observed_at':time.time(),'targets_sha256':digest(t),'disabled_config_sha256':hashlib.sha256(config_raw).hexdigest(),'queue_row_sha256':digest(row),'payload_sha256':hashlib.sha256(payload).hexdigest(),'host_lock_fenced':True}
'''

OBSERVER = _PEER_OBSERVER + r'''
try:
 request=json.loads(sys.stdin.read(65537));t=request['targets']
 lock,identity=acquire(t)
 print(json.dumps(observe(request,lock,identity),sort_keys=True,separators=(',',':')))
except Exception:
 print(json.dumps({'code':'deferred_peer_closure_not_proven'}));sys.exit(2)
'''


HOLD_OBSERVER = _PEER_OBSERVER + r'''
import signal
def emit(value):
 sys.stdout.write(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n');sys.stdout.flush()
def orphaned():
 # EOF/transport failure is NOT an owner release. The actual lock stays held.
 # Root must inspect the original case/owned guard before deliberate cleanup.
 while True:time.sleep(60)
try:
 raw=sys.stdin.buffer.readline(65537)
 if not raw.endswith(b'\n') or len(raw)>65536:raise ValueError()
 request=json.loads(raw);lock,identity=acquire(request['targets'])
 first=observe(request,lock,identity)
except Exception:
 emit({'code':'deferred_peer_closure_not_proven'});sys.exit(2)
for name in ('SIGHUP','SIGINT','SIGTERM'):
 if hasattr(signal,name):signal.signal(getattr(signal,name),signal.SIG_IGN)
sequence=0
try:
 emit(dict(first,phase='held',sequence=sequence,pid=os.getpid(),lock_identity=identity))
 while True:
  raw=sys.stdin.buffer.readline(65537)
  if not raw or not raw.endswith(b'\n') or len(raw)>65536:orphaned()
  command=json.loads(raw);sequence+=1
  if set(command)!={'nonce','sequence','action'} or command['nonce']!=request['nonce'] or command['sequence']!=sequence:raise ValueError()
  proof=observe(request,lock,identity)
  if command['action']=='check':
   emit(dict(proof,phase='held',sequence=sequence,pid=os.getpid(),lock_identity=identity))
  elif command['action']=='release':
   os.close(lock)
   try:emit({'nonce':request['nonce'],'phase':'released','sequence':sequence,'pid':os.getpid()})
   except Exception:sys.exit(3)  # explicit release happened; its ACK is unknown
   sys.exit(0)
  else:raise ValueError()
except Exception:
 try:emit({'code':'deferred_held_observer_unavailable'})
 except Exception:pass
 orphaned()
'''


def observer_command(spec, code):
    cfg = spec['local_observer']
    util.read_checked(cfg['ssh_executable'], root_ok=True, limit=32 * 1024 * 1024)
    util.read_checked(cfg['ssh_config'], private=True, limit=util.MAX_SPEC)
    command = [cfg['ssh_executable'], '-F', cfg['ssh_config']]
    for option in ('BatchMode=yes', 'ConnectTimeout=10', 'StrictHostKeyChecking=yes',
        'UpdateHostKeys=no', 'RequestTTY=no', 'ClearAllForwardings=yes', 'ControlMaster=no',
        'ControlPath=none', 'PermitLocalCommand=no', 'LocalCommand=none', 'RemoteCommand=none'):
        command += ['-o', option]
    return command + [cfg['alias'], 'python3 -c ' + shlex.quote(code)]


def validate_observation(observed, nonce):
    require(observed.get('nonce') == nonce and 0 <= time.time() - observed.get('observed_at', 0) <= 30
            and observed.get('host_lock_fenced') is True
            and observed.get('targets_sha256') == CLOSED['closure_targets']
            and observed.get('disabled_config_sha256') == CLOSED['disabled_config']
            and observed.get('queue_row_sha256') == CLOSED['queue_row']
            and observed.get('payload_sha256') == CLOSED['payload'], 'deferred_peer_closure_changed')
    return observed


def closure(spec):
    cfg, nonce = spec['local_observer'], util.sha(os.urandom(32))
    command = observer_command(spec, OBSERVER)
    result = subprocess.run(command, input=util.encoded({'nonce': nonce, 'targets': cfg['targets']}),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
    require(result.returncode == 0 and len(result.stdout) <= util.MAX_SPEC, 'deferred_peer_closure_not_proven')
    return validate_observation(parse(result.stdout.decode('utf8')), nonce)


class HeldClosure:
    """One owned SSH process / actual remote lock; no timeout-based release.

    On a protocol/disconnect error we stop admitting COMMIT and retain the
    private original intent. Do not automatically release/kill a held observer
    after an unknown result: EOF itself must not reopen the old send host.
    """
    def __init__(self, spec):
        self.spec, self.nonce = spec, util.sha(os.urandom(32))
        self.sequence, self.process, self.buffer = 0, None, bytearray()
        self.identity, self.pid, self.latest, self.released = None, None, None, False

    def _read(self, timeout=25):
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                require(self.process.poll() is None, 'deferred_held_observer_terminated')
                remaining = deadline - time.monotonic()
                require(remaining > 0 and selector.select(remaining), 'deferred_held_observer_unavailable')
                block = os.read(self.process.stdout.fileno(), 4096)
                require(block, 'deferred_held_observer_disconnected')
                self.buffer.extend(block)
                require(len(self.buffer) <= util.MAX_SPEC, 'deferred_held_observer_response_bound')
        raw, remainder = bytes(self.buffer).split(b'\n', 1)
        self.buffer = bytearray(remainder)
        require(self.process.poll() is None, 'deferred_held_observer_terminated')
        return parse(raw.decode('utf8'))

    def _write(self, value):
        require(self.process.poll() is None, 'deferred_held_observer_terminated')
        self.process.stdin.write(util.encoded(value) + b'\n')
        self.process.stdin.flush()

    def _accept(self, observed):
        validate_observation(observed, self.nonce)
        require(observed.get('phase') == 'held' and observed.get('sequence') == self.sequence
                and type(observed.get('pid')) is int and observed['pid'] > 0
                and isinstance(observed.get('lock_identity'), list) and len(observed['lock_identity']) == 4,
                'deferred_held_observer_identity_changed')
        if self.identity is not None:
            require(observed['pid'] == self.pid and observed['lock_identity'] == self.identity,
                    'deferred_held_observer_identity_changed')
        self.pid, self.identity, self.latest = observed['pid'], observed['lock_identity'], observed
        return observed

    def __enter__(self):
        self.process = subprocess.Popen(observer_command(self.spec, HOLD_OBSERVER),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, bufsize=0)
        self._write({'nonce': self.nonce, 'targets': self.spec['local_observer']['targets']})
        self._accept(self._read())
        return self

    def check(self):
        require(not self.released, 'deferred_held_observer_released')
        self.sequence += 1
        self._write({'nonce': self.nonce, 'sequence': self.sequence, 'action': 'check'})
        return self._accept(self._read())

    def commit_authorizer(self, action, first, second, database, trigger):
        # Runs at SQLite's actual COMMIT admission, not just before earlier SQL.
        # Transport EOF retains the remote lock, even if a link fails after ACK.
        if action == getattr(sqlite3, 'SQLITE_TRANSACTION', 22) and first == 'COMMIT':
            try:
                self.check()
            except Exception:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def release(self):
        self.sequence += 1
        self._write({'nonce': self.nonce, 'sequence': self.sequence, 'action': 'release'})
        # release ACK may race the expected natural exit; read it without the
        # held-state poll gate, then require exact ACK plus native process 0.
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 5
            while b'\n' not in self.buffer:
                remaining = deadline - time.monotonic()
                require(remaining > 0 and selector.select(remaining), 'deferred_guard_release_unknown')
                block = os.read(self.process.stdout.fileno(), 4096)
                require(block, 'deferred_guard_release_unknown')
                self.buffer.extend(block)
                require(len(self.buffer) <= util.MAX_SPEC, 'deferred_guard_release_unknown')
        raw, rest = bytes(self.buffer).split(b'\n', 1)
        answer = parse(raw.decode('utf8'))
        require(not rest and answer == {'nonce': self.nonce, 'sequence': self.sequence,
            'phase': 'released', 'pid': self.pid}, 'deferred_guard_release_unknown')
        require(self.process.wait(timeout=5) == 0, 'deferred_guard_release_unknown')
        self.released = True
        self.process.stdin.close(); self.process.stdout.close()
        return answer

    def __exit__(self, kind, error, traceback):
        # Never interpret exception/GC/EOF/timeout as an authorized release.
        # The caller explicitly releases only AFTER same-case state readback.
        return False


def verify_backup(spec, auth, expected):
    """Reuse all original gzip/SQL bytes; never copy or restore onto the ledger."""
    path = Path(spec['backup_path'])
    version = util.digest_checked_backup(path)
    require(version['sha256'] == CLOSED['backup'] and version['size'] == CLOSED['backup_size'],
            'deferred_backup_changed')
    root = util.private_directory(spec['review_root'])
    bound = util.file_metadata(auth['database'], private=True)['size']
    require(shutil.disk_usage(str(root)).free >= bound + 32 * 1024 * 1024 + 1024 ** 3,
            'deferred_backup_restore_space_required')
    checksum, size = util.hashlib.sha256(), 0
    with tempfile.TemporaryDirectory(prefix='.deferred-backup-check.', dir=str(root)) as folder:
        temporary = Path(folder) / 'verification.sqlite'
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with contextlib.closing(sqlite3.connect(str(temporary), isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            db.execute('PRAGMA max_page_count=' + str(max(1, bound // page_size)))
            db.set_authorizer(util.backup_authorizer)
            pending, committed, count = '', False, 0
            import codecs
            decoder = codecs.getincrementaldecoder('utf8')()
            with os.fdopen(os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW), 'rb') as compressed, \
                    gzip.GzipFile(fileobj=compressed, mode='rb') as stream:
                for block in iter(lambda: stream.readline(65536), b''):
                    checksum.update(block); size += len(block); pending += decoder.decode(block)
                    require(len(pending.encode('utf8')) <= util.MAX_FILE, 'deferred_backup_statement_bound')
                    if not sqlite3.complete_statement(pending):
                        continue
                    statement = pending.strip()
                    require(not committed and (count or statement == 'BEGIN TRANSACTION;'),
                            'deferred_backup_transaction_incomplete')
                    db.execute(pending); count += 1
                    require(shutil.disk_usage(str(root)).free >= 1024 ** 3, 'deferred_backup_space_floor')
                    committed = statement == 'COMMIT;'; pending = ''
                pending += decoder.decode(b'', final=True)
            require(committed and not pending.strip() and not db.in_transaction,
                    'deferred_backup_transaction_incomplete')
            require(size == CLOSED['backup_sql_size'] and checksum.hexdigest() == CLOSED['backup_sql'],
                    'deferred_backup_sql_changed')
            db.set_authorizer(util.backup_verification_authorizer)
            require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'deferred_backup_integrity')
            require(selected(db, auth, spec) == expected, 'deferred_backup_original_state_changed')
    require(util.digest_checked_backup(path) == version, 'deferred_backup_generation_changed')
    return version


def inspect(path):
    spec, version = load(path)
    auth = util.quota.authority(spec['worker_config'], spec['server_config'])
    with contextlib.closing(util.quota.readonly(auth['database'])) as db:
        value = selected(db, auth, spec)
    raw, rollout = native(spec, value)
    local = closure(spec)
    saved = verify_backup(spec, auth, value)
    return {'spec': spec, 'version': version, 'auth': auth, 'selected': value,
            'raw': raw, 'rollout': rollout, 'closure': local, 'backup': saved}


def case_id():
    return digest([NAMESPACE, CLOSED['task'], CLOSED['rollout']])


def receipt_for(spec):
    return {'protocol': PROTOCOL, 'id': case_id(), 'task_id': spec['task_id'],
        'source_epoch': spec['epoch'], 'closed_epoch': spec['epoch'] + 1, 'status': 'failed',
        'owner_decision': 'deferred', 'effects_zero': False, 'original_input_requeued': False,
        'complete_artifact': digest([NAMESPACE, 'full-history', case_id(), CLOSED['rollout']]),
        'full_rollout_sha256': CLOSED['rollout'], 'native_session_state_changed': False,
        'old_chunks_changed': False, 'retry_authorized': False}


def complete_history(packed):
    checksum, size = util.hashlib.sha256(), 0
    with gzip.GzipFile(fileobj=io.BytesIO(packed), mode='rb') as source:
        for block in iter(lambda: source.read(65536), b''):
            size += len(block)
            require(size <= CLOSED['rollout_size'], 'deferred_review_history_bound')
            checksum.update(block)
    require(checksum.hexdigest() == CLOSED['rollout'] and size == CLOSED['rollout_size'],
            'deferred_review_complete_history_changed')


def release_status(spec):
    directory = Path(spec['review_root']) / case_id()
    path = directory / '08-peer-released.json'
    if not path.exists():
        return {'peer_lock_release_verified': False,
                'peer_lock_may_remain_held': (directory / '06-cas-intent.json').exists()}
    released = parse(util.read_checked(path, private=True, limit=util.MAX_SPEC)[0].decode('utf8'))
    intent = parse(util.read_checked(directory / '06-cas-intent.json', private=True,
                                    limit=util.MAX_SPEC)[0].decode('utf8'))
    held = parse(util.read_checked(directory / '06-peer-held.json', private=True,
                                  limit=util.MAX_SPEC)[0].decode('utf8'))
    require(set(released) == {'nonce', 'phase', 'sequence', 'pid'}
            and released['phase'] == 'released'
            and released['nonce'] == intent['peer_lock_nonce'] == held['nonce']
            and released['pid'] == held['pid']
            and type(released['sequence']) is int and released['sequence'] > held['sequence'],
            'deferred_guard_release_receipt_conflict')
    return {'peer_lock_release_verified': True, 'peer_lock_may_remain_held': False}


def status(path):
    spec, version = load(path, expired=True)
    auth = util.quota.authority(spec['worker_config'], spec['server_config'])
    with contextlib.closing(util.quota.readonly(auth['database'])) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_deferred_effect_reviews'").fetchone()
        row = db.execute('SELECT * FROM native_deferred_effect_reviews WHERE id=?', (case_id(),)).fetchone() if exists else None
        if row:
            proof = parse(row['proof']); before = proof['selected']; receipt = parse(row['receipt'])
            require(proof['spec_sha256'] == version['sha256'] and digest(before['task']) == CLOSED['task']
                    and digest(before['session']) == CLOSED['session'], 'deferred_review_identity_conflict')
            expected_receipt = receipt_for(spec)
            require(receipt == expected_receipt and all(type(receipt[k]) is type(v)
                    for k, v in expected_receipt.items()), 'deferred_review_receipt_conflict')
            task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (spec['task_id'],)).fetchone())
            expected = closed_task(before['task'], case_id())
            require(task == expected, 'deferred_review_task_readback_conflict')
            util.old_artifact(db, before, {'expected': {'old_artifact': spec['old_artifact'],
                                                      'rollout_path': spec['rollout_path']}})
            packed = io.BytesIO()
            for number, (part, body) in enumerate(db.execute('SELECT part,body FROM session_chunks WHERE id=? ORDER BY part',
                                                            (receipt['complete_artifact'],))):
                require(part == number and len(body) <= 65536, 'deferred_review_history_parts')
                packed.write(body)
                require(packed.tell() <= util.MAX_FILE, 'deferred_review_history_bound')
            complete_history(packed.getvalue())
            return dict(receipt, applied=True, outcome_unknown=False, read_only=True,
                        **release_status(spec))
    directory = Path(spec['review_root']) / case_id()
    return {'protocol': PROTOCOL, 'id': case_id(), 'read_only': True,
            'applied': False if not (directory / '06-cas-intent.json').exists() else None,
            'outcome_unknown': (directory / '06-cas-intent.json').exists(), 'retry_authorized': False,
            **release_status(spec)}


def closed_task(before, identifier):
    checkpoint = parse(before['checkpoint'])
    checkpoint.update(side_effect_started=False, owner_deferred_effect_review={
        'protocol': PROTOCOL, 'id': identifier, 'owner_decision': 'deferred',
        'effects_zero': False, 'original_input_requeue_authorized': False})
    return dict(before, status='failed', epoch=before['epoch'] + 1, node=None,
                deadline=None, checkpoint=json.dumps(checkpoint))


def apply(path, approved_sha256):
    admitted, admission_version = load(path)
    require(admission_version['sha256'] == approved_sha256, 'deferred_explicit_spec_approval_required')
    require(not (Path(admitted['review_root']) / case_id()).exists(), 'deferred_existing_intent_use_status')
    context = inspect(path)
    require(context['version']['sha256'] == approved_sha256, 'deferred_explicit_spec_approval_required')
    spec, auth, before = context['spec'], context['auth'], context['selected']
    directory = util.private_directory(spec['review_root']) / case_id()
    require(not directory.exists(), 'deferred_existing_intent_use_status')
    directory.mkdir(mode=0o700)
    util.write_json(directory / '00-intent.json', {'protocol': PROTOCOL, 'id': case_id(),
        'approved_spec_sha256': approved_sha256, 'selected': before, 'backup': context['backup']})
    history = directory / '03-full-rollout.gz'
    with util.create_file(history) as target:
        with gzip.GzipFile(filename='', fileobj=target, mode='wb', mtime=0) as packed:
            packed.write(context['raw'])
        target.flush(); os.fsync(target.fileno())
    packed_bytes, packed_version = util.read_checked(history, private=True)
    complete_history(packed_bytes)
    receipt = receipt_for(spec)
    artifact = receipt['complete_artifact']
    held = HeldClosure(spec)
    util.write_json(directory / '06-cas-intent.json', {'protocol': PROTOCOL, 'id': case_id(),
        'approved_spec_sha256': approved_sha256, 'peer_lock_nonce': held.nonce})
    try:
        with held:
            util.write_json(directory / '06-peer-held.json', held.latest)
            with contextlib.closing(sqlite3.connect('file:' + quote(str(auth['database']), safe='/') + '?mode=rw',
                                                  uri=True, isolation_level=None, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                try:
                    db.execute('BEGIN IMMEDIATE')
                    require(load(path)[1] == context['version'], 'deferred_spec_changed')
                    require(selected(db, auth, spec) == before, 'deferred_original_cas_changed')
                    require(native(spec, before)[1] == context['rollout'], 'deferred_rollout_cas_changed')
                    require(util.digest_checked_backup(spec['backup_path']) == context['backup'], 'deferred_backup_cas_changed')
                    require(util.read_checked(history, private=True)[1] == packed_version,
                            'deferred_staged_history_cas_changed')
                    live = held.check()
                    db.execute('''CREATE TABLE IF NOT EXISTS native_deferred_effect_reviews(
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL,source_epoch INTEGER NOT NULL,
                    proof TEXT NOT NULL,receipt TEXT NOT NULL,UNIQUE(task_id,source_epoch))''')
                    for operation in ('UPDATE', 'DELETE'):
                        db.execute('CREATE TRIGGER IF NOT EXISTS native_deferred_no_' + operation.lower()
                            + ' BEFORE ' + operation + " ON native_deferred_effect_reviews BEGIN SELECT RAISE(ABORT,'deferred review is append only'); END")
                    require(not db.execute('SELECT 1 FROM session_chunks WHERE id=? LIMIT 1', (artifact,)).fetchone(),
                            'deferred_artifact_namespace_conflict')
                    with io.BytesIO(packed_bytes) as stream:
                        for part, block in enumerate(iter(lambda: stream.read(65536), b'')):
                            db.execute('INSERT INTO session_chunks VALUES(?,?,?)', (artifact, part, block))
                    old = before['session']
                    changed = db.execute('''UPDATE native_sessions SET artifact=?,updated=? WHERE scope=? AND harness=?
                    AND node=? AND state=? AND artifact IS ? AND updated=?''',
                        (artifact, time.time(), old['scope'], old['harness'], old['node'], old['state'], old['artifact'], old['updated'])).rowcount
                    require(changed == 1, 'deferred_session_cas_failed')
                    after = closed_task(before['task'], case_id())
                    changed = db.execute('''UPDATE tasks SET status=?,epoch=?,node=NULL,deadline=NULL,checkpoint=?
                    WHERE id=? AND epoch=? AND status='needs_review' AND checkpoint=? AND scope=?''',
                        ('failed', after['epoch'], after['checkpoint'], spec['task_id'], spec['epoch'],
                         before['task']['checkpoint'], before['task']['scope'])).rowcount
                    require(changed == 1, 'deferred_task_cas_failed')
                    proof = {'spec_sha256': approved_sha256, 'selected': before,
                             'backup': context['backup'], 'closure': live, 'rollout': context['rollout']}
                    db.execute('INSERT INTO native_deferred_effect_reviews VALUES(?,?,?,?,?)',
                               (case_id(), spec['task_id'], spec['epoch'], json.dumps(proof), json.dumps(receipt)))
                    require(native(spec, before)[1] == context['rollout'], 'deferred_rollout_final_changed')
                    held.check()
                    # The live connection still admits only this fixed trusted
                    # program's SQL; check the owned lock at COMMIT itself.
                    db.set_authorizer(held.commit_authorizer)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            observed = status(path)
            require(observed.get('applied') is True and observed.get('outcome_unknown') is False
                    and observed['id'] == case_id(), 'deferred_same_case_readback_required')
            held.check()
            util.write_json(directory / '07-receipt.json', receipt)
            released = held.release()
            util.write_json(directory / '08-peer-released.json', released)
        return dict(receipt, applied=True, outcome_unknown=False, peer_lock_release_verified=True)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        return {'protocol': PROTOCOL, 'id': case_id(), 'applied': None, 'outcome_unknown': True,
                'retry_authorized': False, 'code': 'deferred_same_case_requires_status'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec-file', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--status', action='store_true')
    parser.add_argument('--approve-spec-sha256')
    args = parser.parse_args(argv)
    try:
        if args.status:
            answer = status(args.spec_file)
        elif args.apply:
            answer = apply(args.spec_file, args.approve_spec_sha256)
        else:
            context = inspect(args.spec_file)
            answer = {'protocol': PROTOCOL, 'id': case_id(), 'eligible': True, 'applied': False,
                      'backup_sha256': context['backup']['sha256'], 'effects_zero': False,
                      'original_input_requeue_authorized': False}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        answer = {'protocol': PROTOCOL, 'eligible': False, 'applied': False,
                  'code': str(error) if isinstance(error, util.ReviewDenied) else 'deferred_review_unavailable'}
        print(json.dumps(answer, sort_keys=True))
        return 2
    print(json.dumps(answer, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
