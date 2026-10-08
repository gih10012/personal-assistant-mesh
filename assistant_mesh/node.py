"""A node-owned authority/worker and reconnecting, authenticated A2A supervisor.

Transport health never proves model availability. Local tasks remain local;
this daemon does not elect a replacement global Leader, scan hosts, grant
permissions, replay SSH, install models, or spend money.
"""
import json
import hmac
import fcntl
import os
import signal
import stat
import tempfile
import threading
import time
import traceback
import urllib.error
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from .config import private_json, read_secret
from .networking import PROTOCOL, Network, canonical, digest, identifier, native_scope
from .runtime_health import diagnose as diagnose_runtime
from .server import serve
from .store import Conflict, Store
from .worker import Client, Worker


# The component label is stored in a separate table. Mesh identifiers accept
# any non-control text, so no printable prefix can reserve a safe namespace.
LOCAL_RUNTIME = '@local-runtime'
LOCAL_WORKER = 'local-worker'


class PeerContractError(ValueError):
    pass


def _failure(exc):
    if isinstance(exc, PeerContractError):
        return str(exc)  # only fixed codes created in this module
    if isinstance(exc, urllib.error.HTTPError):
        return {401: 'authentication_rejected', 403: 'authorization_rejected',
                404: 'protocol_route_missing', 409: 'remote_conflict'}.get(exc.code, 'peer_http_error')
    if isinstance(exc, Conflict):
        return 'peer_state_conflict'
    if isinstance(exc, PermissionError):
        return 'authorization_rejected'
    if isinstance(exc, ValueError):
        return 'peer_contract_invalid'
    return 'connection_unavailable'


class Node:
    def __init__(self, config, config_path=None, client_factory=Client,
                 worker_factory=Worker, server_runner=serve, clock=time.time):
        if not isinstance(config, dict):
            raise ValueError('invalid_node_config')
        if config_path is not None:
            private_json(config_path)
        self.config, self.node_id = config, identifier(config['node_id'])
        self.client_factory, self.worker_factory, self.server_runner = client_factory, worker_factory, server_runner
        self.server_config = private_json(config['local_server_config'])
        self.worker_config = private_json(config['local_worker_config'])
        if not isinstance(self.server_config, dict) or not isinstance(self.worker_config, dict):
            raise ValueError('invalid_local_node_config')
        if self.server_config.get('node_id') != self.node_id or self.worker_config.get('node_id') != self.node_id:
            raise ValueError('local_node_identity_mismatch')
        self.start_server = config.get('start_server', True)
        self.start_worker = config.get('start_worker', True)
        if not isinstance(self.start_server, bool) or not isinstance(self.start_worker, bool):
            raise ValueError('invalid_node_start_flags')
        if self.start_server and self.server_config.get('ilink_account'):
            raise ValueError('embedded_node_must_not_start_second_channel_receiver')
        if not isinstance(config.get('auto_maintenance', True), bool):
            raise ValueError('invalid_node_maintenance_flag')
        if not isinstance(config.get('reconcile_remote_children', False), bool):
            raise ValueError('invalid_remote_reconciliation_flag')
        self.routing_capability = 'mesh.node:' + self.node_id
        capabilities = self.worker_config.get('capabilities', [])
        if (not isinstance(capabilities, list) or not all(isinstance(cap, str) for cap in capabilities)
                or not {'agent', self.routing_capability} <= set(capabilities)):
            raise ValueError('local_worker_requires_node_routing_capability')
        # The node-owned authority is not a replacement for the global Leader.
        # A companion of an already deployed central authority may reference
        # its existing Leader worker, but this daemon does not create/elect one.
        if self.start_server and 'leader' in capabilities:
            raise ValueError('embedded_node_worker_must_not_be_global_leader')
        configured_peers = self.server_config.get('peers', [])
        if not isinstance(configured_peers, list) or not all(isinstance(peer, dict) for peer in configured_peers):
            raise ValueError('invalid_local_server_peers')
        worker_secret = read_secret(self.worker_config['token_file'])
        matching = [peer for peer in configured_peers
                    if hmac.compare_digest(read_secret(peer['token_file']), worker_secret)]
        if (len(matching) != 1 or matching[0].get('role') != 'worker'
                or matching[0].get('node') != self.node_id
                or not isinstance(matching[0].get('capabilities', []), list)
                or not set(capabilities) <= set(matching[0]['capabilities'])):
            raise ValueError('local_worker_peer_identity_or_capability_mismatch')
        if self.start_server and 'leader' in matching[0].get('capabilities', []):
            raise ValueError('embedded_node_peer_must_not_grant_global_leader')
        local = urlsplit(self.worker_config['control_url'])
        if (local.hostname not in ('127.0.0.1', 'localhost', '::1') or local.username or local.password
                or local.query or local.fragment or local.scheme not in ('http', 'https')):
            raise ValueError('local_worker_requires_loopback_authority')
        if self.start_server and (local.scheme != 'http' or local.port != self.server_config.get('port', 17680)):
            raise ValueError('local_worker_server_port_mismatch')
        if self.start_server and (local.hostname not in ('127.0.0.1', 'localhost') or local.path not in ('', '/')):
            raise ValueError('local_worker_server_address_mismatch')
        self.interval = config.get('poll_interval', 1)
        if isinstance(self.interval, bool) or not isinstance(self.interval, (int, float)) or not 0.1 <= self.interval <= 60:
            raise ValueError('invalid_node_poll_interval')
        self.peers = {}
        if not isinstance(config.get('peers', []), list):
            raise ValueError('invalid_node_peers')
        for peer in config.get('peers', []):
            if not isinstance(peer, dict):
                raise ValueError('invalid_node_peer')
            name = identifier(peer.get('node'))
            if name == self.node_id or name in self.peers:
                raise ValueError('invalid_node_peer_identity')
            authority = identifier(peer.get('authority', name))
            if not isinstance(peer.get('report_results', False), bool):
                raise ValueError('invalid_peer_report_flag')
            client_config = private_json(peer['client_config']) if peer.get('client_config') else {
                key: peer[key] for key in ('control_url', 'token_file')}
            # Validate URL and owned token now, without issuing network traffic.
            self.client_factory(client_config)
            self.peers[name] = dict(peer, authority=authority, client=client_config)
        self.store = Store(self.server_config['database'], clock=clock, recover_inflight=self.start_server)
        self.network = Network(self.store, self.node_id)
        with self.store.transaction() as db:
            for statement in (
                '''CREATE TABLE IF NOT EXISTS node_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, peer TEXT,
                    operation TEXT NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS node_incidents(
                    peer TEXT PRIMARY KEY, episode INTEGER NOT NULL, active INTEGER NOT NULL,
                    code TEXT NOT NULL, task_id TEXT, updated REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS node_remote_tasks(
                    peer TEXT NOT NULL, message_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, terminal INTEGER NOT NULL, observed REAL NOT NULL,
                    PRIMARY KEY(peer,message_id))''',
                '''CREATE TABLE IF NOT EXISTS node_report_receipts(
                    peer TEXT NOT NULL, report_id TEXT NOT NULL, body TEXT NOT NULL,
                    accepted INTEGER NOT NULL, PRIMARY KEY(peer,report_id))''',
                '''CREATE TABLE IF NOT EXISTS node_peer_backoff(
                    peer TEXT PRIMARY KEY, failures INTEGER NOT NULL, retry_at REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS node_report_denials(
                    peer TEXT NOT NULL, report_id TEXT NOT NULL, code TEXT NOT NULL,
                    PRIMARY KEY(peer,report_id))''',
            ):
                db.execute(statement)
        self.runtime_legacy_unknown = False
        self._initialize_runtime_observer()
        self.worker_legacy_unknown = False
        self._initialize_worker_observer()
        self.stop_event, self.server_ready = threading.Event(), threading.Event()
        self.server, self.server_thread, self.worker, self.worker_thread = None, None, None, None
        self.server_error, self.runtime_config_path = None, None
        self.lock_file = None
        self.worker_started_at, self.worker_started_clock, self.worker_health_recorded = None, None, False
        self.worker_state_lock = threading.Lock()
        self.worker_failure_generation, self.worker_started_generation = 0, None
        self.runtime_observation, self.runtime_next_observation = None, 0
        self.runtime_observation_persisted = False
        self.verified = set()
        self.started = False

    def _initialize_runtime_observer(self):
        """Preserve legacy attention without replaying or retyping peer work.

        Old printable component keys could alias legitimate peers. Only a
        fixed-code component event preserves its historical task reference;
        ambiguous data is unknown, never silent recovery. No tasks are made.
        This optional schema/migration must not stop the native Worker either.
        """
        self.runtime_legacy_unknown = True
        try:
            with self.store.transaction() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS node_runtime_incidents(
                    peer TEXT PRIMARY KEY, episode INTEGER NOT NULL, active INTEGER NOT NULL,
                    code TEXT NOT NULL, task_id TEXT, updated REAL NOT NULL)''')
                current = db.execute('SELECT * FROM node_runtime_incidents WHERE peer=?', (LOCAL_RUNTIME,)).fetchone()
                if current is None:
                    event = db.execute('''SELECT detail,created FROM node_events WHERE peer=?
                        AND operation=? AND code=? ORDER BY sequence DESC LIMIT 1''',
                        (LOCAL_RUNTIME, 'runtime_layout_degraded', 'native_runtime_components_missing')).fetchone()
                    if event:
                        detail = json.loads(event['detail'])
                        episode, task = detail.get('episode'), detail.get('maintenance_task')
                        if (not isinstance(episode, int) or isinstance(episode, bool) or episode < 1
                                or (task is not None and task != 'maintenance-' + digest([self.node_id, LOCAL_RUNTIME, episode]))):
                            raise ValueError('runtime_legacy_evidence_ambiguous')
                        db.execute('INSERT INTO node_runtime_incidents VALUES(?,?,?,?,?,?)',
                                   (LOCAL_RUNTIME, episode, 1, 'native_runtime_components_missing', task, event['created']))
                    elif db.execute('SELECT 1 FROM node_incidents WHERE peer=? AND code=?',
                                    (LOCAL_RUNTIME, 'native_runtime_components_missing')).fetchone():
                        raise ValueError('runtime_legacy_evidence_ambiguous')
            self.runtime_legacy_unknown = False
        except Exception:
            pass  # fixed unknown flag only; never private exception bodies

    def _event(self, peer, operation, code, detail=None):
        with self.store.transaction() as db:
            db.execute('INSERT INTO node_events(peer,operation,code,detail,created) VALUES(?,?,?,?,?)',
                       (peer, operation, code, canonical(detail or {}), self.store.clock()))

    def _initialize_worker_observer(self):
        """Migrate only evidenced owned-Worker failures, never same-name peers.

        Legacy reconnect events could refer to either a peer or the Worker;
        they cannot prove recovery. Preserve the original maintenance task
        reference without creating, changing or replaying that task.
        """
        self.worker_legacy_unknown = True
        try:
            with self.store.transaction() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS node_runtime_incidents(
                    peer TEXT PRIMARY KEY, episode INTEGER NOT NULL, active INTEGER NOT NULL,
                    code TEXT NOT NULL, task_id TEXT, updated REAL NOT NULL)''')
                current = db.execute('SELECT * FROM node_runtime_incidents WHERE peer=?', (LOCAL_WORKER,)).fetchone()
                if current is None:
                    event = db.execute('''SELECT sequence,detail,created FROM node_events WHERE peer=?
                        AND operation=? AND code=? ORDER BY sequence DESC LIMIT 1''',
                        (LOCAL_WORKER, 'link_degraded', 'local_worker_runtime_failed')).fetchone()
                    if event:
                        detail = json.loads(event['detail'])
                        episode, task = detail.get('episode'), detail.get('maintenance_task')
                        failed = db.execute('''SELECT 1 FROM node_events WHERE peer IS NULL
                            AND operation=? AND code=? AND sequence<? LIMIT 1''',
                            ('worker_failed', 'local_worker_runtime_failed', event['sequence'])).fetchone()
                        if (not failed or not isinstance(episode, int) or isinstance(episode, bool) or episode < 1
                                or (task is not None and task != 'maintenance-' + digest([self.node_id, LOCAL_WORKER, episode]))):
                            raise ValueError('worker_legacy_evidence_ambiguous')
                        db.execute('INSERT INTO node_runtime_incidents VALUES(?,?,?,?,?,?)',
                                   (LOCAL_WORKER, episode, 1, 'local_worker_runtime_failed', task, event['created']))
                    elif db.execute('SELECT 1 FROM node_incidents WHERE peer=? AND code=?',
                                    (LOCAL_WORKER, 'local_worker_runtime_failed')).fetchone():
                        raise ValueError('worker_legacy_evidence_ambiguous')
            self.worker_legacy_unknown = False
        except Exception:
            pass  # optional attention bookkeeping must not block native work

    def _incident(self, peer, code, runtime=False):
        """One local maintenance task per failure episode, not each retry."""
        table = 'node_runtime_incidents' if runtime else 'node_incidents'
        owned_worker = runtime and peer == LOCAL_WORKER
        with self.store.transaction() as db:
            previous = db.execute('SELECT * FROM ' + table + ' WHERE peer=?', (peer,)).fetchone()
            first = previous is None or not previous['active']
            episode = (previous['episode'] if previous else 0) + (1 if first else 0)
            task_id = previous['task_id'] if previous and not first else None
            if first and self.config.get('auto_maintenance', True):
                task_id = 'maintenance-' + digest(
                    ['native-runtime', self.node_id, peer, episode] if runtime else [self.node_id, peer, episode])
                context = {'project_id': 'node-runtime', 'agent_id': 'self-maintenance',
                           'session_scope': native_scope(self.node_id, 'node-runtime', 'self-maintenance'),
                           'memory_scope': 'node-runtime:' + digest(self.node_id),
                           'authority': 'local:' + self.node_id,
                           'origin': dict({'kind': 'node-maintenance', 'episode': episode},
                                          **({'component': peer} if runtime else {'peer': peer}))}
                subject = ('诊断本节点自有 Worker 启动失败或意外退出（' + code + '）。'
                           '先核对实际执行器、启动配置和日志；自有 Worker 稳定运行与认证心跳仅证明进程恢复，'
                           '不等于原生 Shell、模型或联网能力已恢复，相关能力须真实执行验收。'
                           if owned_worker else
                           '诊断本节点原生运行包组件缺失（' + code + '）。'
                           '先使用本节点 runtime_diagnose/运行包维护 CLI 核对实际布局与可用执行器；'
                           '坏 Shell 不能冒称执行了修复。有其它本人已授权的可用节点/原生路径时可请求其协助，'
                           '没有可用执行路径则保留故障证据并联系本人。修复布局不等于执行恢复，须真实执行验收。'
                           if runtime else
                           '诊断本节点与已授权 peer ' + peer + ' 的连接故障（' + code + '），')
                text = (subject + '检查本节点服务、'
                        '私有配置引用、日志、已配置模型及已授权连接；有可用模型时自主分析并验证修复。'
                        'Mesh 是额外的受管能力入口，不拦截任何原生 Shell、网络或其他功能；'
                        '可自主使用原生能力排障，并尝试本人已授权范围内的不同联网路径重新连接已授权 peer。'
                        '不要求原生工具或新路线先注册；成功验证后可把路线登记为推荐的 Mesh 能力。'
                        '没有离线模型不得声称脱网推理成功。不要探索未授权主机、擅自信任新 peer、'
                        '自动审批、购买算力或输出凭据。'
                        '只维护本节点的任务和环境，不接管全球 Leader，不复制或重放全球任务。'
                        '故障与重连记录在本节点 private ledger 的 node_events / mesh_links。')
                db.execute('INSERT OR IGNORE INTO tasks(id,input,required,status,created,context,scope) VALUES(?,?,?,?,?,?,?)',
                           (task_id, text, canonical(['agent', self.routing_capability]), 'pending',
                            self.store.clock(), canonical(context), context['session_scope']))
            if first or previous['code'] != code:
                db.execute('INSERT INTO node_events(peer,operation,code,detail,created) VALUES(?,?,?,?,?)',
                       (None if runtime else peer, ('worker_degraded' if owned_worker else 'runtime_layout_degraded')
                        if runtime else 'link_degraded',
                        code, canonical(dict({'episode': episode, 'maintenance_task': task_id},
                                             **({'component': peer} if runtime else {}))), self.store.clock()))
            db.execute('INSERT OR REPLACE INTO ' + table + ' VALUES(?,?,?,?,?,?)',
                       (peer, episode, 1, code, task_id, self.store.clock()))

    def _connected(self, peer):
        with self.store.transaction() as db:
            row = db.execute('SELECT active FROM node_incidents WHERE peer=?', (peer,)).fetchone()
            if row and row['active']:
                db.execute('UPDATE node_incidents SET active=0,updated=? WHERE peer=?', (self.store.clock(), peer))
                db.execute('INSERT INTO node_events(peer,operation,code,detail,created) VALUES(?,?,?,?,?)',
                           (peer, 'link_reconnected', 'identity_verified', '{}', self.store.clock()))

    def _degrade(self, peer, exc):
        self.verified.discard(peer)
        self.network.observe_link(peer, ok=False)
        # Hello may succeed while delegation/report authorization still fails.
        # Do not reset this durable operation backoff merely on hello success.
        with self.store.transaction() as db:
            row = db.execute('SELECT failures FROM node_peer_backoff WHERE peer=?', (peer,)).fetchone()
            failures = (row['failures'] if row else 0) + 1
            jitter = (int(digest([self.node_id, peer, failures])[:4], 16) % 1000) / 1000.0
            db.execute('INSERT OR REPLACE INTO node_peer_backoff VALUES(?,?,?)',
                       (peer, failures, self.store.clock() + min(300, 2 ** min(failures, 8)) + jitter))
        self._incident(peer, _failure(exc))

    def _probe_due(self, peer):
        with self.store.transaction() as db:
            row = db.execute('SELECT retry_at FROM node_peer_backoff WHERE peer=?', (peer,)).fetchone()
        return (row is None or row['retry_at'] <= self.store.clock()) and self.network.probe_due(peer)

    def _settled_peer(self, peer):
        with self.store.transaction() as db:
            db.execute('INSERT OR REPLACE INTO node_peer_backoff VALUES(?,0,0)', (peer,))
        self._connected(peer)

    def probe(self, peer):
        config = self.peers[peer]
        start = time.monotonic()
        try:
            # Refresh credentials from their protected file after owner rotation.
            client = self.client_factory(config['client'])
            hello = client.request('/v1/mesh/hello')
            if not isinstance(hello, dict) or hello.get('node') != peer:
                raise PeerContractError('peer_identity_mismatch')
            if hello.get('authority') != config['authority']:
                raise PeerContractError('peer_authority_mismatch')
            if hello.get('protocol') != PROTOCOL:
                raise PeerContractError('peer_protocol_mismatch')
            if hello.get('a2a_idempotent_receive') is not True:
                raise PeerContractError('receiver_idempotence_not_confirmed')
            self.network.observe_link(peer, ok=True, hello=hello, latency_ms=(time.monotonic() - start) * 1000)
            self.verified.add(peer)
            return True
        except (ValueError, OSError, urllib.error.URLError) as exc:
            self._degrade(peer, exc)
            return False

    def deliver(self, peer):
        # Revocation also applies to messages queued before this daemon's
        # restart. Merely having a transport/token is not a delegation grant.
        allowed = self.server_config.get('network_peers', {}).get(peer, {}).get('send_allowed')
        if allowed is not True:
            with self.store.transaction() as db:
                old = db.execute("SELECT state,response FROM mesh_deliveries WHERE peer=? AND state IN ('pending','unknown','submitting') ORDER BY created LIMIT 1", (peer,)).fetchone()
            if (old and old['state'] == 'unknown' and old['response']
                    and json.loads(old['response']).get('reason') == 'peer_delegation_not_authorized'):
                return False  # already blocked; do not create fake attempts
            delivery = self.network.claim_delivery(peer)
            if delivery is not None:
                rejected = self.network.reject_delivery(delivery, 'peer_delegation_not_authorized')
                self._event(peer, 'delivery_refused', 'peer_delegation_not_authorized',
                            dict(rejected, message_id=delivery['message']['id']))
            return False
        if peer not in self.verified:
            return False  # never retry unknowns before receiver contract proof
        delivery = self.network.claim_delivery(peer)
        if delivery is None:
            return False
        try:
            client = self.client_factory(self.peers[peer]['client'])
            receipt = client.request('/v1/mesh/send', {'message': delivery['message']})
            self.network.finish_delivery(delivery, receipt)
            return True
        except (ValueError, OSError, urllib.error.URLError) as exc:
            try:
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 403:
                    rejected = self.network.reject_delivery(delivery)
                    self._event(peer, 'delivery_refused', 'authorization_rejected',
                                dict(rejected, message_id=delivery['message']['id']))
                else:
                    self.network.finish_delivery(delivery, None)
            except Conflict:
                pass  # a newer attempt/expired lease owns recovery
            self._degrade(peer, exc)
            return False

    def _reconcile_denials(self, peer):
        if not self.config.get('reconcile_remote_children', False):
            return
        from .remote import Remote
        remote = Remote(self.store, self.network)
        # Recover a crash between definite local refusal and child settlement.
        # An unknown prior attempt is never treated as a non-delivery proof.
        with self.store.transaction() as db:
            rows = db.execute('''SELECT d.message_id FROM mesh_deliveries d
                JOIN remote_delegations r ON r.peer=d.peer AND r.message_id=d.message_id
                WHERE d.peer=? AND d.state='denied' AND r.terminal=0
                ORDER BY d.created LIMIT 100''', (peer,)).fetchall()
        for row in rows:
            remote.reject(peer, row['message_id'])

    def poll_result(self, peer):
        with self.store.transaction() as db:
            row = db.execute('''SELECT d.message_id,d.body,d.response FROM mesh_deliveries d
                LEFT JOIN node_remote_tasks t ON t.peer=d.peer AND t.message_id=d.message_id
                WHERE d.peer=? AND d.state='accepted' AND (t.terminal IS NULL OR t.terminal=0)
                ORDER BY COALESCE(t.observed,0),d.created LIMIT 1''', (peer,)).fetchone()
        if row is None:
            return
        message, receipt = json.loads(row['body']), json.loads(row['response'])
        try:
            answer = self.client_factory(self.peers[peer]['client']).request('/v1/mesh/task?' + urlencode({'id': row['message_id']}))
            if (not isinstance(answer, dict) or answer.get('id') != row['message_id']
                    or answer.get('task_id') != receipt['task_id']):
                raise PeerContractError('task_receipt_identity_mismatch')
            status = identifier(answer.get('status'))
            if status not in ('pending', 'running', 'paused', 'waiting_auth', 'waiting_backend',
                              'waiting_children', 'waiting_remote', 'unknown', 'continuing',
                              'completed', 'failed', 'needs_review'):
                raise PeerContractError('peer_task_status_invalid')
            result = answer.get('result')
            body = {'task_id': answer['task_id'], 'project': message['project'], 'status': status, 'result': result,
                    'evidence': {'source': 'authenticated-ownerbound-task-read', 'message_id': row['message_id']}}
            if len(canonical(body).encode('utf8')) > 65536:
                raise PeerContractError('peer_result_too_large')
            self.network.report(peer, 'observed-' + digest(body), body)
            if self.config.get('reconcile_remote_children', False):
                # Authenticated task reads have already matched the immutable
                # message receipt. Unsolicited reports cannot settle a proxy.
                from .remote import Remote
                Remote(self.store, self.network).reconcile(peer, row['message_id'],
                    answer['task_id'], status, result)
            with self.store.transaction() as db:
                db.execute('INSERT OR REPLACE INTO node_remote_tasks VALUES(?,?,?,?,?,?,?)',
                           (peer, row['message_id'], answer['task_id'], status, canonical(result),
                            int(status in ('completed', 'failed', 'needs_review')), self.store.clock()))
        except (ValueError, OSError, urllib.error.URLError) as exc:
            self._degrade(peer, exc)

    def report_results(self, peer):
        if not self.peers[peer].get('report_results', False):
            return
        with self.store.transaction() as db:
            rows = db.execute("SELECT id,context,status,result FROM tasks WHERE status IN ('completed','failed','needs_review') ORDER BY created").fetchall()
        for row in rows:
            context = json.loads(row['context'])
            if context.get('origin', {}).get('kind') != 'a2a' or context['origin'].get('peer') != peer:
                continue  # private autonomous work is not broadcast to any Leader
            body = {'task_id': row['id'], 'project': context['project_id'], 'status': row['status'], 'result': row['result'],
                    'evidence': {'source': 'local-authority-task-ledger', 'node': self.node_id}}
            report_id = 'local-' + digest(body)
            with self.store.transaction() as db:
                known = db.execute('SELECT accepted FROM node_report_receipts WHERE peer=? AND report_id=?', (peer, report_id)).fetchone()
                denied = db.execute('SELECT 1 FROM node_report_denials WHERE peer=? AND report_id=?', (peer, report_id)).fetchone()
                if (known and known['accepted']) or denied:
                    continue
                db.execute('INSERT OR IGNORE INTO node_report_receipts VALUES(?,?,?,0)', (peer, report_id, canonical(body)))
            try:
                answer = self.client_factory(self.peers[peer]['client']).request('/v1/mesh/report', {'id': report_id, 'body': body})
                if not isinstance(answer, dict) or answer.get('id') != report_id or answer.get('recorded') is not True:
                    raise PeerContractError('report_receipt_invalid')
                with self.store.transaction() as db:
                    db.execute('UPDATE node_report_receipts SET accepted=1 WHERE peer=? AND report_id=?', (peer, report_id))
            except (ValueError, OSError, urllib.error.URLError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 403:
                    with self.store.transaction() as db:
                        db.execute('INSERT OR IGNORE INTO node_report_denials VALUES(?,?,?)',
                                   (peer, report_id, 'authorization_rejected'))
                    self._event(peer, 'report_denied', 'authorization_rejected', {'report_id': report_id})
                self._degrade(peer, exc)
                return

    def _server_main(self):
        try:
            self.server_runner(self.server_config, self._server_ready)
        except Exception as exc:
            self.server_error = exc
            self.server_ready.set()

    def _server_ready(self, server, api, channel):
        self.server = server
        api.store.clock = self.store.clock
        port = server.server_address[1]
        if self.server_config.get('port') == 0:
            parsed = urlsplit(self.worker_config['control_url'])
            self.worker_config = dict(self.worker_config, control_url=urlunsplit((parsed.scheme, '127.0.0.1:' + str(port), parsed.path, '', '')))
            directory = Path(self.store.path).parent / 'node-runtime'
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
                raise ValueError('runtime_config_requires_private_owned_directory')
            descriptor, path = tempfile.mkstemp(prefix='worker-live-', suffix='.json', dir=str(directory))
            with os.fdopen(descriptor, 'w') as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(self.worker_config, handle)
            self.runtime_config_path = path
        self.server_ready.set()

    def _worker_main(self):
        try:
            self.worker.run()
            if not self.stop_event.is_set():
                raise RuntimeError('owned_worker_exited')
        except Exception as exc:
            self._worker_failed(exc)

    def _observe_runtime(self, force=False):
        """Advisory host observation, including externally managed Workers.

        No native operation, quota/model request, runtime switch or task replay.
        Heartbeats/layout repairs cannot clear an execution incident. Unknown
        wrappers are not treated as broken and do not block the Worker.
        """
        if not force and time.monotonic() < self.runtime_next_observation:
            return
        self.runtime_next_observation = time.monotonic() + 30
        if self.runtime_legacy_unknown:
            self._initialize_runtime_observer()
        if self.worker_legacy_unknown:
            self._initialize_worker_observer()
        try:
            path = self.runtime_config_path or self.config['local_worker_config']
            configured = private_json(path)
            if (not isinstance(configured, dict) or configured.get('node_id') != self.node_id
                    or configured.get('token_file') != self.worker_config.get('token_file')
                    or configured.get('control_url') != self.worker_config.get('control_url')
                    or not isinstance(configured.get('capabilities'), list)
                    or not all(isinstance(cap, str) for cap in configured['capabilities'])
                    or not set(configured['capabilities']) <= set(self.worker_config['capabilities'])):
                raise ValueError('runtime_configuration_binding_changed')
            report = diagnose_runtime(configured)
        except Exception:
            report = {'schema': 'runtime-health/1', 'read_only': True,
                      'observation': 'configured_layout_only', 'active_process_verified': False,
                      'native_tools_intercepted': False, 'execution_verified': False,
                      'error': 'runtime_observation_unavailable'}
        self.runtime_observation = report
        self.runtime_observation_persisted = False
        missing = (isinstance(report, dict) and any(
            isinstance(runtime, dict) and runtime.get('layout') == 'missing'
            for runtime in report.get('runtimes', [])))
        try:
            self.store.set('node_runtime_observation', {'node': self.node_id, 'report': report,
                                                       'observed': self.store.clock()})
            if missing:
                self._incident(LOCAL_RUNTIME, 'native_runtime_components_missing', runtime=True)
            self.runtime_observation_persisted = True
        except Exception:
            # Optional telemetry must not stop a healthy native Worker. Do not
            # expose database exception bodies, or claim durable bookkeeping.
            self.runtime_observation = dict(report, persistence_error='runtime_observation_record_failed')

    def _worker_failed(self, exc):
        with self.worker_state_lock:
            self._record_worker_failure(exc)

    def _record_worker_failure(self, exc):
        if self.stop_event.is_set():
            return
        self.worker_failure_generation += 1
        # Exception strings can contain credential-bearing URLs. Store only
        # typed codes and source locations, never repr(exc) or argument values.
        with self.store.transaction() as db:
            frames = [{'file': frame.filename, 'line': frame.lineno, 'function': frame.name}
                      for frame in traceback.extract_tb(exc.__traceback__)[-8:]]
            db.execute('INSERT INTO node_events(peer,operation,code,detail,created) VALUES(?,?,?,?,?)',
                       (None, 'worker_failed', 'local_worker_runtime_failed',
                        canonical({'exception_type': type(exc).__name__, 'frames': frames}), self.store.clock()))
            previous = self.store._meta(db, 'node_worker_failure', {'failures': 0})
            failures = previous['failures'] + 1
            self.store._set(db, 'node_worker_failure', {'failures': failures,
                            'retry_at': self.store.clock() + min(300, 2 ** min(failures, 8))})
        try:
            self._incident(LOCAL_WORKER, 'local_worker_runtime_failed', runtime=True)
        except Exception:
            self.worker_legacy_unknown = True

    def _ensure_worker(self):
        if not self.start_worker or self.stop_event.is_set():
            return
        if self.worker_thread and self.worker_thread.is_alive():
            self._worker_healthy()
            return
        failure = self.store.get('node_worker_failure', {})
        if failure.get('retry_at', 0) > self.store.clock():
            return
        path = self.runtime_config_path or self.config['local_worker_config']
        try:
            self.worker = self.worker_factory(self.worker_config, config_path=path)
        except Exception as exc:
            self._worker_failed(exc)
            return
        with self.worker_state_lock:
            self.worker_thread = threading.Thread(target=self._worker_main)
            self.worker_thread.daemon = True
            self.worker_started_at, self.worker_started_clock = time.monotonic(), self.store.clock()
            self.worker_started_generation = self.worker_failure_generation
            self.worker_health_recorded = False
            self.worker_thread.start()

    def _worker_healthy(self):
        with self.worker_state_lock:
            self._record_worker_health()

    def _record_worker_health(self):
        if (self.worker_health_recorded or not self.worker_thread or not self.worker_thread.is_alive()
                or self.worker_started_generation != self.worker_failure_generation
                or time.monotonic() - self.worker_started_at < 30):
            return
        with self.store.transaction() as db:
            node = db.execute('SELECT seen FROM nodes WHERE id=?', (self.node_id,)).fetchone()
            now = self.store.clock()
            if (not node or node['seen'] < self.worker_started_clock
                    or node['seen'] <= now - 60 or node['seen'] > now):
                return
            self.store._set(db, 'node_worker_failure', {'failures': 0, 'retry_at': 0})
        try:
            with self.store.transaction() as db:
                incident = db.execute('SELECT * FROM node_runtime_incidents WHERE peer=?', (LOCAL_WORKER,)).fetchone()
                if incident and incident['active']:
                    db.execute('UPDATE node_runtime_incidents SET active=0,updated=? WHERE peer=?',
                               (self.store.clock(), LOCAL_WORKER))
                    db.execute('INSERT INTO node_events(peer,operation,code,detail,created) VALUES(?,?,?,?,?)',
                               (None, 'worker_recovered', 'owned_worker_heartbeat',
                                canonical({'component': LOCAL_WORKER, 'episode': incident['episode'],
                                           'execution_verified': False}), self.store.clock()))
            self.worker_health_recorded = True
            if incident is not None:
                self.worker_legacy_unknown = False
        except Exception:
            self.worker_legacy_unknown = True
            # Keep retrying optional recovery bookkeeping while this owned
            # Worker stays healthy. Never settle a peer or a Shell incident.

    def start(self):
        if self.started:
            return
        if self.stop_event.is_set():
            raise ValueError('stopped_node_cannot_restart')
        # Linux user-service deployment: one owned supervisor per private
        # ledger. Locks disappear on process death; no stale PID-file guessing.
        path = self.store.path + '.node-supervisor.lock'
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o077:
                raise ValueError('node_lock_requires_private_regular_owned_file')
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_file = os.fdopen(descriptor, 'r+')
            descriptor = None
        except BlockingIOError:
            raise ValueError('node_supervisor_already_running')
        finally:
            if descriptor is not None:
                os.close(descriptor)
        self.started = True
        if self.start_server:
            self.server_thread = threading.Thread(target=self._server_main)
            self.server_thread.daemon = True
            self.server_thread.start()
            if not self.server_ready.wait(5) or self.server_error is not None:
                self.stop()
                raise ValueError('local_node_server_start_failed')
        self._observe_runtime()
        self._ensure_worker()

    def step(self):
        if not self.started or self.stop_event.is_set():
            raise ValueError('node_supervisor_not_running')
        if self.start_server and self.started and (self.server_error is not None
                or self.server_thread is None or not self.server_thread.is_alive()):
            self._event(None, 'server_failed', 'local_authority_runtime_failed')
            raise ValueError('local_node_server_stopped')
        self._observe_runtime()
        self._ensure_worker()
        for peer in self.peers:
            if self.stop_event.is_set():
                break
            if self.server_config.get('network_peers', {}).get(peer, {}).get('send_allowed') is not True:
                self.deliver(peer)  # local revocation needs no remote contact
            self._reconcile_denials(peer)
            if self._probe_due(peer):
                self.probe(peer)
            link = next((row for row in self.network.links()['links'] if row['peer'] == peer and row['kind'] == 'a2a'), None)
            if peer not in self.verified or not link or not link['reachable']:
                continue
            if self.deliver(peer) or peer in self.verified:
                self.poll_result(peer)
            self._reconcile_denials(peer)
            if peer in self.verified:
                self.report_results(peer)
            if peer in self.verified:
                self._settled_peer(peer)
        snapshot = self.status()
        self.store.set('node_runtime', snapshot)
        return snapshot

    def status(self):
        links = self.network.links()
        incident, incident_unknown = None, self.runtime_legacy_unknown
        worker_incident, worker_unknown = None, self.worker_legacy_unknown
        try:
            with self.store.transaction() as db:
                incident = db.execute('SELECT active FROM node_runtime_incidents WHERE peer=?', (LOCAL_RUNTIME,)).fetchone()
                worker_incident = db.execute('SELECT active FROM node_runtime_incidents WHERE peer=?', (LOCAL_WORKER,)).fetchone()
        except Exception:
            # This optional telemetry read must not stop the owned native
            # child. Core authority/lease errors elsewhere remain failures.
            incident_unknown = True
            worker_unknown = True
        try:
            worker_failures = self.store.get('node_worker_failure', {}).get('failures', 0)
        except Exception:
            worker_failures, worker_unknown = 0, True
        if worker_failures and (worker_incident is None or not worker_incident['active']):
            worker_unknown = True  # a failed optional write is not recovery
        return {'node': self.node_id, 'mode': links['mode'], 'links': links['links'],
                'a2a_reachable': any(link['kind'] == 'a2a' and link['reachable'] for link in links['links']),
                'leader_reachable': any(link['leader_available'] for link in links['links']),
                'local_work_allowed': True, 'global_takeover_allowed': False,
                'local_server_owned': self.server is not None,
                'local_server_alive': bool(self.server_thread and self.server_thread.is_alive()),
                'local_worker_alive': bool(self.worker_thread and self.worker_thread.is_alive()),
                'worker_management': 'embedded' if self.start_worker else 'external_or_not_started',
                'runtime_observation': self.runtime_observation,
                'runtime_observation_persisted': self.runtime_observation_persisted,
                'runtime_incident_status': 'unknown' if incident_unknown else (
                    'active' if incident and incident['active'] else 'clear'),
                'runtime_attention_required': bool(incident_unknown or (incident and incident['active']) or any(
                    runtime.get('layout') == 'missing'
                    for runtime in (self.runtime_observation or {}).get('runtimes', [])
                    if isinstance(runtime, dict))),
                'runtime_execution_verified': False,
                'worker_incident_status': 'unknown' if worker_unknown else (
                    'active' if worker_incident and worker_incident['active'] else 'clear'),
                'worker_attention_required': bool(worker_unknown or (worker_incident and worker_incident['active'])
                    or worker_failures),
                'model_availability': 'not_verified_by_transport', 'observed': self.store.clock()}

    def run_forever(self):
        try:
            self.start()
            while not self.stop_event.is_set():
                self.step()
                self.stop_event.wait(self.interval)
        finally:
            self.stop()

    def stop(self):
        self.stop_event.set()
        if self.worker:
            self.worker.stop.set()
            agent = getattr(self.worker, 'agent', None)
            if agent is not None:
                agent.close()  # only this daemon's native child, never other units
        if self.worker_thread and self.worker_thread is not threading.current_thread():
            self.worker_thread.join(timeout=5)
        if self.server:
            if self.server_thread and self.server_thread.is_alive():
                self.server.shutdown()
            else:
                self.server.server_close()
        if self.server_thread and self.server_thread is not threading.current_thread():
            self.server_thread.join(timeout=5)
        if self.runtime_config_path:
            try:
                os.unlink(self.runtime_config_path)
            except FileNotFoundError:
                pass
            self.runtime_config_path = None
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None


def run(config, config_path):
    """CLI entry: owned private configuration, clean signal handling, no logging."""
    checked = private_json(config_path)
    if checked != config:
        raise ValueError('node_config_changed_before_start')
    node = Node(config, config_path=config_path)
    handlers = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGTERM, signal.SIGINT):
            handlers[number] = signal.signal(number, lambda signum, frame: node.stop_event.set())
    try:
        return node.run_forever()
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
