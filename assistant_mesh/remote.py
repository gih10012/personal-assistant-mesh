"""Fenced remote delegation, represented by children in the existing task ledger.

This is the mesh-a2a/1 contract, not an implementation of the A2A standard.
Transport configuration must authorize the destination before delegate() is
called. Only an authenticated result read, matched to the stored acceptance
receipt, may settle a remote child. Unsolicited reports are evidence only.
"""
import json

from .networking import PROTOCOL, canonical, digest, identifier
from .store import Conflict


TERMINAL = ('completed', 'failed', 'needs_review')
ACTIVE = ('pending', 'running', 'waiting_auth', 'waiting_backend',
          'waiting_children', 'waiting_remote', 'continuing', 'paused', 'unknown')


class Remote:
    def __init__(self, store, network):
        if network.store.path != store.path:
            raise ValueError('remote_requires_same_authority_ledger')
        self.store, self.network = store, network
        with store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS remote_delegations(
                child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
                parent_epoch INTEGER NOT NULL, caller_node TEXT NOT NULL,
                call_id TEXT NOT NULL UNIQUE, peer TEXT NOT NULL,
                message_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                remote_task_id TEXT, remote_status TEXT,
                terminal INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                observed REAL, UNIQUE(peer,message_id))''')
            db.execute('CREATE INDEX IF NOT EXISTS remote_delegation_parent ON remote_delegations(parent_id)')

    @staticmethod
    def _arguments(arguments, context):
        if (not isinstance(arguments, dict) or
                set(arguments) - {'input', 'project', 'project_id', 'agent', 'agent_id', 'role'}):
            raise ValueError('invalid_remote_delegate_arguments')
        text = arguments.get('input')
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf8')) > 65536:
            raise ValueError('invalid_remote_delegate_input')
        if ('project' in arguments and 'project_id' in arguments) or ('agent' in arguments and 'agent_id' in arguments):
            raise ValueError('ambiguous_remote_delegate_identity')
        project = arguments.get('project', arguments.get('project_id', context.get('project_id', 'general')))
        agent = arguments.get('agent', arguments.get('agent_id', arguments.get('role', 'specialist')))
        role = arguments.get('role', agent)
        for value in (project, agent, role):
            identifier(value)
        return text, project, agent, role

    def delegate(self, task_id, node, epoch, call_id, peer, arguments):
        """Create child, mapping, outbox and immutable action receipt atomically.

        Checking the live task/Leader lease precedes even an idempotent replay.
        A stale native process cannot submit another remote effectful job. Peer
        grants are deployment policy, not model-supplied fields or this method.
        """
        for value in (task_id, node, call_id, peer):
            identifier(value)
        if peer == self.network.node_id:
            raise ValueError('remote_delegate_requires_other_node')
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ValueError('invalid_task_epoch')
        if not isinstance(arguments, dict):
            raise ValueError('invalid_remote_delegate_arguments')
        fingerprint = digest([task_id, 'remote_delegate', peer, arguments])
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            leader = db.execute('SELECT * FROM leader').fetchone()
            now = self.store.clock()
            if (not row or row['status'] != 'running' or row['node'] != node or
                    row['epoch'] != epoch or row['deadline'] <= now or
                    ('leader' in json.loads(row['required']) and
                     (leader['node'] != node or leader['deadline'] <= now))):
                raise Conflict('stale_task_lease')
            context = json.loads(row['context'])
            text, project, agent, role = self._arguments(arguments, context)
            previous = db.execute('SELECT * FROM agent_actions WHERE id=?', (call_id,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Conflict('action_id_content_conflict')
                return json.loads(previous['response'])
            child = digest(['remote-child', self.network.node_id, task_id, call_id])
            message_id = digest(['remote-message', self.network.node_id, task_id, call_id])
            message = {'protocol': PROTOCOL, 'id': message_id, 'to': peer, 'input': text,
                       'project': project, 'agent': agent, 'parent_ref': child}
            body = self.network._message(message)
            delivery_fingerprint = digest(message)
            existing = db.execute('SELECT fingerprint FROM mesh_deliveries WHERE peer=? AND message_id=?',
                                  (peer, message_id)).fetchone()
            if existing:
                # An independent raw queue must not turn into an authorized child.
                raise Conflict('remote_message_id_already_used')
            proxy_scope = 'remote:' + digest([self.network.node_id, peer, project, agent])
            child_context = {'project_id': project, 'agent_id': agent, 'role': role,
                             'session_scope': proxy_scope, 'authority': 'remote:' + peer,
                             'remote_proxy': True,
                             'origin': {'kind': 'remote-delegation', 'node': self.network.node_id,
                                        'peer': peer, 'parent_id': task_id, 'message_id': message_id}}
            checkpoint = {'remote_peer': peer, 'remote_message_id': message_id,
                          'delegated_parent_epoch': epoch}
            # waiting_remote is deliberately not a local runnable status. The
            # sentinel requirement also prevents accidental local execution.
            db.execute('''INSERT INTO tasks(id,parent_id,input,required,status,created,context,scope,checkpoint)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                       (child, task_id, text, canonical(['mesh.remote.proxy']), 'waiting_remote', now,
                        canonical(child_context), proxy_scope, canonical(checkpoint)))
            db.execute('''INSERT INTO remote_delegations(child_id,parent_id,parent_epoch,caller_node,call_id,
                peer,message_id,fingerprint,created) VALUES(?,?,?,?,?,?,?,?,?)''',
                       (child, task_id, epoch, node, call_id, peer, message_id, fingerprint, now))
            db.execute('''INSERT INTO mesh_deliveries(peer,message_id,fingerprint,body,state,created)
                VALUES(?,?,?,?,?,?)''', (peer, message_id, delivery_fingerprint, body, 'pending', now))
            output = {'id': child, 'parent_id': task_id, 'status': 'waiting_remote',
                      'peer': peer, 'message_id': message_id, 'queued': True,
                      'remote_completion_verified': False}
            db.execute('INSERT INTO agent_actions VALUES(?,?,?)', (call_id, fingerprint, canonical(output)))
            return output

    def reconcile(self, peer, message_id, remote_task_id, status, result):
        """Settle only a receipt-bound result read by the owned Node supervisor.

        The original parent lease may have expired or its Leader moved. The
        child's durable creation authorization remains valid; this observation
        neither starts native execution nor replays the parent's side effects.
        """
        for value in (peer, message_id, remote_task_id, status):
            identifier(value)
        if status not in TERMINAL + ACTIVE:
            raise ValueError('invalid_remote_task_status')
        if result is not None and (not isinstance(result, str) or len(result.encode('utf8')) > 65536):
            raise ValueError('invalid_remote_task_result')
        with self.store.transaction() as db:
            mapping = db.execute('SELECT * FROM remote_delegations WHERE peer=? AND message_id=?',
                                 (peer, message_id)).fetchone()
            if mapping is None:
                return {'reconciled': False, 'reason': 'not_remote_child'}
            delivery = db.execute('SELECT * FROM mesh_deliveries WHERE peer=? AND message_id=?',
                                  (peer, message_id)).fetchone()
            if not delivery or delivery['state'] != 'accepted' or not delivery['response']:
                raise Conflict('remote_acceptance_not_verified')
            receipt = json.loads(delivery['response'])
            if (not isinstance(receipt, dict) or receipt.get('protocol') != PROTOCOL or
                    receipt.get('id') != message_id or receipt.get('state') != 'accepted' or
                    receipt.get('fingerprint') != delivery['fingerprint'] or
                    receipt.get('task_id') != remote_task_id or
                    (mapping['remote_task_id'] is not None and mapping['remote_task_id'] != remote_task_id)):
                raise PermissionError('remote_result_identity_mismatch')
            child = db.execute('SELECT * FROM tasks WHERE id=?', (mapping['child_id'],)).fetchone()
            if not child or child['parent_id'] != mapping['parent_id']:
                raise Conflict('remote_proxy_child_missing')
            checkpoint = json.loads(child['checkpoint'])
            if mapping['terminal']:
                # Terminal results are immutable. A stale poll cannot undo a
                # completion; a different claimed completion needs review.
                if status in TERMINAL and (mapping['remote_status'] != status or child['result'] != result):
                    raise Conflict('remote_terminal_result_conflict')
                return {'reconciled': True, 'id': child['id'], 'status': child['status'],
                        'terminal': True, 'parent_woken': False, 'duplicate': True}
            if child['status'] != 'waiting_remote':
                raise Conflict('remote_proxy_not_waiting')
            now = self.store.clock()
            terminal = status in TERMINAL
            checkpoint.update(remote_task_id=remote_task_id, remote_status=status,
                              remote_observed_at=now, remote_result_verified=True,
                              side_effect_started=False)
            child_status = status if terminal else 'waiting_remote'
            db.execute('UPDATE tasks SET status=?,result=?,checkpoint=?,node=NULL,deadline=NULL WHERE id=?',
                       (child_status, result, canonical(checkpoint), child['id']))
            db.execute('''UPDATE remote_delegations SET remote_task_id=?,remote_status=?,terminal=?,observed=?
                WHERE child_id=?''', (remote_task_id, status, int(terminal), now, child['id']))
            woken = False
            if terminal:
                active = db.execute("SELECT 1 FROM tasks WHERE parent_id=? AND status NOT IN ('completed','failed','needs_review') LIMIT 1",
                                    (mapping['parent_id'],)).fetchone()
                if not active:
                    changed = db.execute("UPDATE tasks SET status='pending',node=NULL,deadline=NULL WHERE id=? AND status='waiting_children'",
                                         (mapping['parent_id'],))
                    woken = bool(changed.rowcount)
            return {'reconciled': True, 'id': child['id'], 'status': child_status,
                    'terminal': terminal, 'parent_woken': woken, 'duplicate': False}

    def reject(self, peer, message_id):
        """Settle an authorized child only after provable non-acceptance.

        A refusal on a retry is NOT proof that a previous uncertain attempt did
        not execute. Require the first attempt's definite denial; never turn an
        unknown/401/timeout, or a later 403, into a claim of non-delivery.
        """
        identifier(peer)
        identifier(message_id)
        with self.store.transaction() as db:
            mapping = db.execute('SELECT * FROM remote_delegations WHERE peer=? AND message_id=?',
                                 (peer, message_id)).fetchone()
            if mapping is None:
                return {'reconciled': False, 'reason': 'not_remote_child'}
            delivery = db.execute('SELECT * FROM mesh_deliveries WHERE peer=? AND message_id=?',
                                  (peer, message_id)).fetchone()
            if not delivery or delivery['state'] != 'denied':
                return {'reconciled': False, 'reason': 'definite_rejection_not_verified'}
            if delivery['attempt'] != 1:
                return {'reconciled': False, 'reason': 'refusal_delivery_uncertain'}
            denial = json.loads(delivery['response']) if delivery['response'] else None
            if (not isinstance(denial, dict) or
                    denial.get('reason') not in ('authorization_rejected', 'peer_delegation_not_authorized') or
                    denial.get('delivery_not_accepted') is not True):
                return {'reconciled': False, 'reason': 'definite_rejection_not_verified'}
            child = db.execute('SELECT * FROM tasks WHERE id=?', (mapping['child_id'],)).fetchone()
            if not child or child['parent_id'] != mapping['parent_id']:
                raise Conflict('remote_proxy_child_missing')
            if mapping['terminal']:
                if mapping['remote_status'] != 'denied':
                    raise Conflict('remote_terminal_result_conflict')
                return {'reconciled': True, 'id': child['id'], 'status': child['status'],
                        'terminal': True, 'parent_woken': False, 'duplicate': True}
            if child['status'] != 'waiting_remote':
                raise Conflict('remote_proxy_not_waiting')
            now = self.store.clock()
            checkpoint = json.loads(child['checkpoint'])
            checkpoint.update(remote_delivery='denied', remote_observed_at=now,
                              remote_non_acceptance_verified=True, side_effect_started=False)
            result = '远端权限拒绝，任务未交付；需本人核对授权'
            db.execute("UPDATE tasks SET status='needs_review',result=?,checkpoint=?,node=NULL,deadline=NULL WHERE id=?",
                       (result, canonical(checkpoint), child['id']))
            db.execute("UPDATE remote_delegations SET remote_status='denied',terminal=1,observed=? WHERE child_id=?",
                       (now, child['id']))
            active = db.execute("SELECT 1 FROM tasks WHERE parent_id=? AND status NOT IN ('completed','failed','needs_review') LIMIT 1",
                                (mapping['parent_id'],)).fetchone()
            woken = False
            if not active:
                changed = db.execute("UPDATE tasks SET status='pending',node=NULL,deadline=NULL WHERE id=? AND status='waiting_children'",
                                     (mapping['parent_id'],))
                woken = bool(changed.rowcount)
            return {'reconciled': True, 'id': child['id'], 'status': 'needs_review',
                    'terminal': True, 'parent_woken': woken, 'duplicate': False}
