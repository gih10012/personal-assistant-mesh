"""Node-owned networking state, not a second global Leader.

Two deliberately different channels: durable agent messages (mesh-a2a/1),
and journaled remote execution. This is NOT an A2A-standard implementation.
Peer identities and privileges come from private deployment configuration.
"""
import hashlib
import json
import re
import shlex
import subprocess
import tempfile

from .store import Conflict


PROTOCOL = 'mesh-a2a/1'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf8')).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError('invalid_mesh_identifier')
    return value


def native_scope(node, project, agent, principal=None):
    # Names containing ':' cannot alias another project/agent pair. Offline
    # work never resumes the global leader:owner thread under a local lease.
    return 'node:' + digest(node)[:24] + ':project:' + digest([principal, project, agent])


class Network:
    def __init__(self, store, node_id):
        self.store, self.node_id = store, identifier(node_id)
        with store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS mesh_links(
                    peer TEXT NOT NULL, kind TEXT NOT NULL, failures INTEGER NOT NULL,
                    retry_at REAL NOT NULL, seen REAL, deadline REAL NOT NULL,
                    state TEXT NOT NULL, leader TEXT, authority TEXT,
                    leader_epoch INTEGER NOT NULL DEFAULT 0, latency_ms REAL,
                    PRIMARY KEY(peer,kind));
                CREATE TABLE IF NOT EXISTS mesh_messages(
                    sender TEXT NOT NULL, message_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, task_id TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(sender,message_id));
                CREATE TABLE IF NOT EXISTS mesh_deliveries(
                    peer TEXT NOT NULL, message_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, body TEXT NOT NULL,
                    state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                    deadline REAL NOT NULL DEFAULT 0, response TEXT,
                    created REAL NOT NULL, PRIMARY KEY(peer,message_id));
                CREATE TABLE IF NOT EXISTS mesh_executions(
                    peer TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                    result TEXT, created REAL NOT NULL,
                    PRIMARY KEY(peer,request_id));
                CREATE TABLE IF NOT EXISTS mesh_reports(
                    sender TEXT NOT NULL, report_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, body TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(sender,report_id));
            ''')

    def probe_due(self, peer, kind='a2a'):
        self._link_key(peer, kind)
        with self.store.transaction() as db:
            row = db.execute('SELECT retry_at FROM mesh_links WHERE peer=? AND kind=?', (peer, kind)).fetchone()
            return row is None or row['retry_at'] <= self.store.clock()

    @staticmethod
    def _link_key(peer, kind):
        identifier(peer)
        if kind not in ('a2a', 'execution'):
            raise ValueError('invalid_link_kind')

    def observe_link(self, peer, kind='a2a', ok=False, hello=None, latency_ms=None):
        """Call only after authenticated transport verification, never a prompt.

        Convert a remote relative lease to the local clock. Do not compare
        wall-clock deadlines between machines or elect a global offline Leader.
        Backoff and epoch fences survive process restarts.
        """
        self._link_key(peer, kind)
        hello = hello or {}
        if ok and (not isinstance(hello, dict) or hello.get('node') != peer):
            raise ValueError('peer_identity_mismatch')
        if ok and kind == 'a2a' and hello.get('protocol') != PROTOCOL:
            raise ValueError('peer_protocol_mismatch')
        if latency_ms is not None and (isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float))
                                      or not 0 <= latency_ms <= 3600000):
            raise ValueError('invalid_latency')
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM mesh_links WHERE peer=? AND kind=?', (peer, kind)).fetchone()
            now = self.store.clock()
            failures = 0 if ok else (row['failures'] if row else 0) + 1
            authority = identifier(hello.get('authority', peer)) if ok else (row['authority'] if row else None)
            leader = hello.get('leader') if ok else None
            epoch, remaining = 0, 0
            if leader is not None:
                if not isinstance(leader, dict):
                    raise ValueError('invalid_leader_claim')
                identifier(leader.get('node'))
                epoch, remaining = leader.get('epoch'), leader.get('remaining_seconds')
                if (not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1 or
                        not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or not 0 < remaining <= 90):
                    raise ValueError('invalid_leader_claim')
                previous = db.execute('SELECT MAX(leader_epoch) FROM mesh_links WHERE authority=?', (authority,)).fetchone()[0] or 0
                if epoch < previous:
                    raise Conflict('stale_leader_announcement')
                for other in db.execute('SELECT leader FROM mesh_links WHERE authority=? AND leader_epoch=? AND leader IS NOT NULL', (authority, epoch)):
                    if json.loads(other['leader'])['node'] != leader['node']:
                        raise Conflict('conflicting_leader_announcement')
            retained_epoch = max(epoch, row['leader_epoch'] if row and row['authority'] == authority else 0)
            delay = 15 if ok else min(300, 2 ** min(failures, 8))
            # Deterministic jitter avoids synchronised retry storms after boot.
            jitter = (int(digest([self.node_id, peer, kind, failures])[:4], 16) % 1000) / 1000.0
            db.execute('INSERT OR REPLACE INTO mesh_links VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (peer, kind, failures, now + delay + jitter, now if ok else row['seen'] if row else None,
                        now + remaining if leader else now + 30 if ok else 0,
                        'connected' if ok else 'unreachable', canonical(leader) if leader else None,
                        authority, retained_epoch, latency_ms if ok else None))
        return self.links()

    def links(self):
        with self.store.transaction() as db:
            now = self.store.clock()
            rows = [dict(row) for row in db.execute('SELECT * FROM mesh_links ORDER BY peer,kind')]
            maxima = {}
            for row in rows:
                maxima[row['authority']] = max(maxima.get(row['authority'], 0), row['leader_epoch'])
            for row in rows:
                row['reachable'] = row['state'] == 'connected' and row['deadline'] > now
                row['leader'] = json.loads(row['leader']) if row['leader'] else None
                row['leader_available'] = bool(row['reachable'] and row['leader'] and
                                               row['leader_epoch'] == maxima[row['authority']])
                if row['leader']:
                    row['leader']['remaining_seconds'] = max(0, row['deadline'] - now)
            return {'node': self.node_id, 'links': rows,
                    'mode': 'connected' if any(r['leader_available'] for r in rows) else 'autonomous',
                    'local_work_allowed': True, 'global_takeover_allowed': False}

    def hello(self):
        status = self.store.status()
        leader = status['leader']
        remaining = max(0, leader['deadline'] - self.store.clock())
        return {'protocol': PROTOCOL, 'node': self.node_id, 'authority': self.node_id,
                'leader': {'node': leader['node'], 'epoch': leader['epoch'], 'remaining_seconds': remaining}
                if leader['node'] and remaining else None,
                'channels': ['a2a', 'execution'], 'a2a_idempotent_receive': True}

    def _message(self, message):
        if not isinstance(message, dict) or set(message) - {'protocol', 'id', 'to', 'input', 'project', 'agent', 'parent_ref'}:
            raise ValueError('invalid_agent_message')
        if message.get('protocol') != PROTOCOL:
            raise ValueError('peer_protocol_mismatch')
        for key in ('id', 'to', 'project', 'agent'):
            identifier(message.get(key))
        text = message.get('input')
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf8')) > 65536:
            raise ValueError('invalid_agent_input')
        if 'parent_ref' in message:
            identifier(message['parent_ref'])
        return canonical(message)

    def receive(self, sender, message):
        """Transport must bind sender and authorize a2a.delegate BEFORE entry.

        Receipt and local runnable task commit atomically. Duplicate delivery
        may be retried ONLY with this exact receiver contract and immutable ID.
        Native execution itself is not declared exactly-once.
        """
        identifier(sender)
        self._message(message)
        if message['to'] != self.node_id:
            raise PermissionError('wrong_destination')
        fingerprint = digest(message)
        identity = digest([self.node_id, sender, message['id']])
        context = {'project_id': message['project'], 'agent_id': message['agent'],
                   'session_scope': native_scope(self.node_id, message['project'], message['agent'], sender),
                   'memory_scope': 'peer:' + digest([self.node_id, sender, message['project'], message['agent']]),
                   'origin': {'kind': 'a2a', 'peer': sender, 'message_id': message['id'],
                              'parent_ref': message.get('parent_ref')},
                   'authority': 'local:' + self.node_id}
        if message['input'].startswith('/plan '):
            context['mode'] = 'plan'
        if message['input'].startswith('/goal '):
            objective = message['input'][6:].strip()
            if not objective or len(objective) > 4000:
                raise ValueError('invalid_goal_objective')
            context['goal'] = {'objective': objective, 'status': 'active'}
        with self.store.transaction() as db:
            previous = db.execute('SELECT * FROM mesh_messages WHERE sender=? AND message_id=?', (sender, message['id'])).fetchone()
            if previous and previous['fingerprint'] != fingerprint:
                raise Conflict('message_id_content_conflict')
            if not previous:
                db.execute('INSERT INTO tasks(id,input,required,status,created,context,scope) VALUES(?,?,?,?,?,?,?)',
                           (identity, message['input'], canonical(['agent', 'mesh.node:' + self.node_id]), 'pending', self.store.clock(),
                            canonical(context), context['session_scope']))
                db.execute('INSERT INTO mesh_messages VALUES(?,?,?,?,?)',
                           (sender, message['id'], fingerprint, identity, self.store.clock()))
        return {'protocol': PROTOCOL, 'id': message['id'], 'fingerprint': fingerprint, 'task_id': identity, 'state': 'accepted'}

    def task_for_sender(self, sender, message_id):
        identifier(sender)
        identifier(message_id)
        with self.store.transaction() as db:
            row = db.execute('SELECT task_id FROM mesh_messages WHERE sender=? AND message_id=?', (sender, message_id)).fetchone()
        if not row:
            raise PermissionError('message_not_owned')
        task = self.store.task_status(row['task_id'])
        return {'id': message_id, 'task_id': row['task_id'], 'status': task['status'], 'result': task['result']}

    def enqueue(self, peer, message):
        identifier(peer)
        body = self._message(message)
        if message['to'] != peer:
            raise ValueError('wrong_destination')
        fingerprint = digest(message)
        with self.store.transaction() as db:
            previous = db.execute('SELECT fingerprint FROM mesh_deliveries WHERE peer=? AND message_id=?', (peer, message['id'])).fetchone()
            if previous and previous['fingerprint'] != fingerprint:
                raise Conflict('message_id_content_conflict')
            if not previous:
                db.execute('INSERT INTO mesh_deliveries(peer,message_id,fingerprint,body,state,created) VALUES(?,?,?,?,?,?)',
                           (peer, message['id'], fingerprint, body, 'pending', self.store.clock()))
        return {'peer': peer, 'id': message['id'], 'queued': True}

    def claim_delivery(self, peer):
        """A2A unknowns retry the SAME receiver-enforced ID, never SSH jobs."""
        identifier(peer)
        with self.store.transaction() as db:
            now = self.store.clock()
            row = db.execute("SELECT * FROM mesh_deliveries WHERE peer=? AND (state IN ('pending','unknown') OR (state='submitting' AND deadline<=?)) ORDER BY created LIMIT 1", (peer, now)).fetchone()
            if row is None:
                return None
            attempt = row['attempt'] + 1
            db.execute("UPDATE mesh_deliveries SET state='submitting',attempt=?,deadline=? WHERE peer=? AND message_id=?",
                       (attempt, now + 30, peer, row['message_id']))
            return {'peer': peer, 'message': json.loads(row['body']), 'attempt': attempt, 'fingerprint': row['fingerprint']}

    def finish_delivery(self, delivery, receipt=None):
        peer, identity, attempt = delivery['peer'], delivery['message']['id'], delivery['attempt']
        if receipt is not None and (not isinstance(receipt, dict) or receipt.get('protocol') != PROTOCOL or
                                    receipt.get('id') != identity or receipt.get('fingerprint') != delivery['fingerprint'] or
                                    receipt.get('state') != 'accepted' or not isinstance(receipt.get('task_id'), str)):
            raise ValueError('invalid_peer_receipt')
        with self.store.transaction() as db:
            row = db.execute('SELECT state,attempt,deadline FROM mesh_deliveries WHERE peer=? AND message_id=?', (peer, identity)).fetchone()
            if not row or row['state'] != 'submitting' or row['attempt'] != attempt or row['deadline'] <= self.store.clock():
                raise Conflict('stale_delivery_attempt')
            db.execute('UPDATE mesh_deliveries SET state=?,response=?,deadline=0 WHERE peer=? AND message_id=?',
                       ('accepted' if receipt else 'unknown', canonical(receipt) if receipt else None, peer, identity))
        return {'state': 'accepted' if receipt else 'unknown'}

    def reject_delivery(self, delivery, reason='authorization_rejected'):
        """Definite refusal is not an uncertain send; never auto replay it."""
        if reason not in ('authorization_rejected', 'peer_delegation_not_authorized'):
            raise ValueError('invalid_delivery_rejection')
        peer, identity, attempt = delivery['peer'], delivery['message']['id'], delivery['attempt']
        with self.store.transaction() as db:
            row = db.execute('SELECT state,attempt,deadline FROM mesh_deliveries WHERE peer=? AND message_id=?', (peer, identity)).fetchone()
            if not row or row['state'] != 'submitting' or row['attempt'] != attempt or row['deadline'] <= self.store.clock():
                raise Conflict('stale_delivery_attempt')
            state = 'denied' if attempt == 1 else 'unknown'
            proof = {'reason': reason, 'delivery_not_accepted': attempt == 1}
            db.execute('UPDATE mesh_deliveries SET state=?,response=?,deadline=0 WHERE peer=? AND message_id=?',
                       (state, canonical(proof), peer, identity))
        return dict(proof, state=state)

    def report(self, sender, report_id, body):
        """Append local evidence after reconnect; never replay global effects."""
        identifier(sender)
        identifier(report_id)
        if (not isinstance(body, dict) or set(body) - {'task_id', 'project', 'status', 'result', 'evidence'} or
                not {'task_id', 'project', 'status'} <= set(body) or len(canonical(body).encode()) > 65536):
            raise ValueError('invalid_local_report')
        for key in ('task_id', 'project', 'status'):
            identifier(body[key])
        fingerprint = digest(body)
        with self.store.transaction() as db:
            previous = db.execute('SELECT fingerprint FROM mesh_reports WHERE sender=? AND report_id=?', (sender, report_id)).fetchone()
            if previous and previous['fingerprint'] != fingerprint:
                raise Conflict('report_id_content_conflict')
            if not previous:
                db.execute('INSERT INTO mesh_reports VALUES(?,?,?,?,?)', (sender, report_id, fingerprint, canonical(body), self.store.clock()))
        return {'id': report_id, 'recorded': True, 'global_task_replayed': False}

    def begin_execution(self, peer, request_id, argv, scope):
        """Journal BEFORE any transport side effect. Identity/grant checked by caller.

        No arbitrary retry for unknown SSH outcome, including process restart.
        Returning an existing request never launches it again.
        """
        identifier(peer)
        identifier(request_id)
        if (not isinstance(argv, list) or not argv or not all(isinstance(a, str) and '\0' not in a for a in argv)
                or not isinstance(scope, dict) or len(canonical([argv, scope]).encode()) > 65536):
            raise ValueError('invalid_remote_execution')
        fingerprint = digest([argv, scope])
        with self.store.transaction() as db:
            previous = db.execute('SELECT * FROM mesh_executions WHERE peer=? AND request_id=?', (peer, request_id)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Conflict('execution_id_content_conflict')
                return {'new': False, 'state': previous['state'], 'result': json.loads(previous['result']) if previous['result'] else None}
            db.execute('INSERT INTO mesh_executions VALUES(?,?,?,?,?,?)',
                       (peer, request_id, fingerprint, 'unknown', None, self.store.clock()))
        return {'new': True, 'state': 'unknown'}

    def finish_execution(self, peer, request_id, result):
        if not isinstance(result, dict) or not isinstance(result.get('exit_code'), int):
            raise ValueError('invalid_execution_result')
        with self.store.transaction() as db:
            row = db.execute('SELECT state FROM mesh_executions WHERE peer=? AND request_id=?', (peer, request_id)).fetchone()
            if not row or row['state'] != 'unknown':
                raise Conflict('execution_not_open')
            db.execute("UPDATE mesh_executions SET state='finished',result=? WHERE peer=? AND request_id=?",
                       (canonical(result), peer, request_id))
        return {'state': 'finished', 'result': result}


class SSHExecution:
    """Remote terminal channel: never impersonates a remote autonomous agent.

    Owner-approved aliases/configuration ONLY. No host-key bypass, implicit
    machine enrolment, shell=True, installations, network scanning, or purchases.
    A remote exit code is observed outcome, not proof of no partial side effects.
    """
    def __init__(self, network, peers, ssh_config=None, authorize=None):
        self.network, self.peers, self.ssh_config = network, peers, ssh_config
        self.authorize = authorize

    def execute(self, peer, request_id, argv, scope, timeout=30):
        config = self.peers.get(peer, {})
        if not config.get('execution_allowed'):
            raise PermissionError('remote_execution_not_authorized')
        if self.authorize is not None:
            if not self.authorize(peer, scope):
                raise PermissionError('remote_execution_scope_not_authorized')
        elif 'execution_scope' not in config or canonical(config['execution_scope']) != canonical(scope):
            raise PermissionError('remote_execution_scope_not_authorized')
        alias = config.get('ssh_alias', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', alias):
            raise ValueError('invalid_ssh_alias')
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 60:
            raise ValueError('invalid_execution_timeout')
        record = self.network.begin_execution(peer, request_id, argv, scope)
        if not record['new']:
            return record
        command = ['ssh'] + (['-F', self.ssh_config] if self.ssh_config else [])
        command += ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
                    '--', alias, ' '.join(shlex.quote(argument) for argument in argv)]
        # File-backed bounded read avoids unbounded PIPE memory consumption.
        with tempfile.TemporaryFile() as output:
            try:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
                try:
                    child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    return {'new': True, 'state': 'unknown', 'reason': 'transport_timeout_remote_outcome_unknown'}
                output.seek(0)
                data = output.read(65537)
                # 255 also includes remote commands deliberately returning 255;
                # conservative uncertainty is preferable to replaying effects.
                if child.returncode == 255:
                    return {'new': True, 'state': 'unknown', 'reason': 'ssh_outcome_unknown'}
                result = {'exit_code': child.returncode, 'output': data[:65536].decode('utf8', errors='replace'),
                          'truncated': len(data) > 65536}
                return self.network.finish_execution(peer, request_id, result)
            except OSError:
                return {'new': True, 'state': 'unknown', 'reason': 'transport_start_failed_no_automatic_replay'}
