"""Explicit private conversation <-> actual Mesh wiring; no automatic daemon.

The public side remains the existing OAuth/MCP app. This owner-installed helper
uses its existing ingress credential, never an operator token or Chat bearer.
"""
import argparse
import json
import threading
import urllib.error

from .conversation import ConversationService, digest, utc
from .conversation_authority import ROUTE, FORMAT
from .ingress import validate_peer
from .mcp_http import RemoteIngressBackend
from .mcp_http_auth import SOURCE
from .native_messaging import _private_bytes, _unique, _nonfinite, client_from_config, project_response
from .store import Conflict


def target(question):
    return {key: question[key] for key in ('task_id', 'task_epoch', 'interaction_id', 'params_sha256')}


class ConversationBridge:
    """One owner/source/subject, existing journal, bounded explicit step only."""
    def __init__(self, service, principal, client):
        if not isinstance(service, ConversationService) or not callable(getattr(client, 'request', None)):
            raise ValueError('conversation_bridge_owner_context_required')
        self.service, self.client = service, client
        self.principal = validate_peer({'role': 'ingress', 'ingress': principal}, service.owner)
        if self.principal['source'] != self.service.source:
            raise PermissionError('conversation_bridge_source_rejected')
        self.lock, self.requests = threading.RLock(), {}
        with service.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_bridge_cursors(
                identity TEXT PRIMARY KEY,cursor INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_bridge_publications(
                identity TEXT NOT NULL,message_id TEXT NOT NULL,task_epoch INTEGER NOT NULL,
                PRIMARY KEY(identity,message_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_bridge_intents(
                identity TEXT NOT NULL,message_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                arguments TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT,
                PRIMARY KEY(identity,message_id))''')
            self.requests.update((row['task_id'], row['request_id'])
                                 for row in db.execute('SELECT task_id,request_id FROM conversations'))
        if service.validator is None:
            # Owner code installation, not a posted boolean. The callback does
            # not reopen this journal while _append holds its write transaction.
            service.validator = self
        service.runtime = self  # explicit owner code; not client/tool JSON

    def _principal(self, principal, write=False):
        current = validate_peer({'role': 'ingress', 'ingress': principal}, self.service.owner)
        if (any(current[key] != self.principal[key] for key in ('owner_id', 'source', 'subject'))
                or ('tasks:submit' if write else 'tasks:read') not in current['scopes']):
            raise PermissionError('conversation_bridge_principal_rejected')

    def _context(self, binding):
        identity, task = self.service._binding(self.principal, binding)
        with self.service.transaction() as db:
            self.service._row(db, identity, binding, task)
        self.requests[task] = binding['request_id']
        return identity, task

    def before_receive(self, principal, binding):
        """Publish actual projections ONLY; a read token never drains replies."""
        self._principal(principal)
        with self.lock:
            identity, task = self._context(binding)
            report = self._outbound(identity, binding, self.observe(binding))
            projection = getattr(self, '_conversation_emit_projection', None)
            if projection is not None:  # Explicit owner hook; no producer write on receive.
                report.update(projection(identity, binding))
            return report

    def after_reply(self, principal, binding, message_id):
        """Only the original message just recorded by this authorized call."""
        self._principal(principal, write=True)
        with self.lock:
            identity, task = self._context(binding)
            original = self.service.request(self.principal, 'receipt', dict(binding, message_id=message_id))
            if not original['found'] or original['message']['direction'] != 'to_mesh':
                raise Conflict('conversation_bridge_original_client_message_missing')
            return self._deliver(identity, binding, self.observe(binding), original['message'])

    def original_receipt(self, principal, binding, message_id):
        """Read-only after unknown: not even a first delivery is attempted."""
        self._principal(principal)
        identity, task = self._context(binding)
        with self.service.transaction() as db:
            old = db.execute('SELECT 1 FROM conversation_bridge_intents WHERE identity=? AND message_id=?',
                             (identity, message_id)).fetchone()
        if old is None:
            return {'found': False, 'state': 'not_admitted_to_native', 'post_attempted': False}
        return self._delivery_receipt(binding, message_id)

    def _request(self, action, arguments):
        reply = self.client.request(ROUTE, {'action': action, 'arguments': arguments})
        if not isinstance(reply, dict) or reply.get('format') != FORMAT:
            raise ValueError('conversation_bridge_authority_response_unknown')
        return reply

    def observe(self, binding):
        value = self._request('observe', {'request_id': binding['request_id']})
        if (value.get('request_id') != binding['request_id'] or value.get('task_id') not in self.requests
                or self.requests[value['task_id']] != binding['request_id']):
            raise Conflict('conversation_bridge_original_task_changed')
        return value

    def __call__(self, interaction):
        return self.question_live(interaction)

    def validate_original_question(self, interaction, request_id):
        # The request ID comes from the journal's immutable bound row, not a
        # model claim or this process's possibly stale startup cache. The ASGI
        # app and an independently invoked private bridge CLI share this wire.
        return self.question_live(interaction, request_id=request_id)

    def question_live(self, interaction, request_id=None):
        request_id = request_id or self.requests.get(interaction.get('task_id'))
        if request_id is None:
            return False
        try:
            observed = self._request('observe', {'request_id': request_id})
            return (observed.get('live') is True and observed.get('task_id') == interaction['task_id']
                    and any(target(item) == interaction for item in observed.get('questions', [])))
        except Exception:
            return False

    def _publish(self, identity, binding, observed, message_id, text, kind, interaction=None, original_time=None):
        payload = dict(binding, message_id=message_id, text=text, kind=kind, original_time=original_time)
        if interaction is not None:
            payload['interaction'] = interaction
        existing = self.service.request(self.principal, 'receipt', dict(binding, message_id=message_id))
        if not existing['found']:
            self.service.publish(self.principal, payload)
        elif (existing['message']['text'] != text or existing['message']['kind'] != kind
              or existing['message']['interaction'] != interaction):
            raise Conflict('conversation_bridge_original_publication_changed')
        with self.service.transaction() as db:
            db.execute('INSERT OR IGNORE INTO conversation_bridge_publications VALUES(?,?,?)',
                       (identity, message_id, observed['task_epoch']))

    def _outbound(self, identity, binding, observed):
        task, epoch = observed['task_id'], observed['task_epoch']
        message_id = 'mesh-status-' + digest([task, epoch, observed['status']])
        self._publish(identity, binding, observed, message_id,
            json.dumps({'task_id': task, 'task_epoch': epoch, 'status': observed['status'],
                        'live': observed['live'], 'execution_verified': False}, sort_keys=True), 'progress')
        questions = 0
        for question in observed.get('questions', []):
            body = []
            for item in question['questions']:
                body.append(str(item.get('id', '')) + ': ' + str(item.get('question', '')))
                for option in item.get('options') or []:
                    body.append(str(option.get('label', '')) + ': ' + str(option.get('description', '')))
            self._publish(identity, binding, observed, 'mesh-question-' + digest(target(question)),
                '\n'.join(body), 'question', target(question), utc(question['created_at']))
            questions += 1
        # Only the already owner-approved publication, NEVER raw task.result.
        approved = False
        if observed['status'] == 'completed':
            try:
                raw = self.service.backend.request(self.principal, 'result', {'request_id': binding['request_id']})
                published = project_response('result', binding['request_id'], raw)['data']
                if published.get('published') and published.get('task_id') == task:
                    self._publish(identity, binding, observed, 'mesh-result-' + digest(published['publication_id']),
                        json.dumps(published['result'], ensure_ascii=False, sort_keys=True), 'result')
                    approved = True
            except (PermissionError, urllib.error.HTTPError) as error:
                if isinstance(error, urllib.error.HTTPError):
                    status = error.code
                    error.close()
                    if status not in (403, 404):
                        raise
                # No publication yet; never replace it with raw results.
        return {'questions_published': questions, 'approved_result_available': approved}

    def _delivery_receipt(self, binding, message_id):
        return self._request('receipt', dict(binding, message_id=message_id))

    def _save_receipt(self, identity, message_id, receipt):
        with self.service.transaction() as db:
            db.execute('UPDATE conversation_bridge_intents SET state=?,receipt=? WHERE identity=? AND message_id=?',
                ('recorded' if receipt.get('found') else 'unknown', json.dumps(receipt, sort_keys=True), identity, message_id))

    def _deliver(self, identity, binding, observed, message):
        with self.service.transaction() as db:
            old = db.execute('SELECT * FROM conversation_bridge_intents WHERE identity=? AND message_id=?',
                             (identity, message['message_id'])).fetchone()
            original_epoch = db.execute('SELECT task_epoch FROM conversation_bridge_publications WHERE identity=? AND message_id=?',
                                       (identity, message.get('reply_to'))).fetchone()
        if old is not None:
            # Constructor/restart/another model can never get another POST.
            receipt = self._delivery_receipt(binding, message['message_id'])
            self._save_receipt(identity, message['message_id'], receipt)
            return receipt
        if not observed['live']:
            return {'found': False, 'state': 'awaiting_original_live_task', 'post_attempted': False}
        interaction = message.get('interaction')
        epoch = (interaction['task_epoch'] if interaction else
                 original_epoch[0] if original_epoch and original_epoch[0] > 0 else observed['task_epoch'])
        arguments = dict(binding, message_id=message['message_id'], task_epoch=epoch,
                         text=message['text'], interaction=interaction)
        fingerprint = digest(arguments)
        with self.service.transaction() as db:
            # Durable BEFORE HTTP. Absence of a later receipt is never a retry
            # permit, even if the process died before its first socket write.
            inserted = db.execute('INSERT OR IGNORE INTO conversation_bridge_intents VALUES(?,?,?,?,?,NULL)',
                (identity, message['message_id'], fingerprint, json.dumps(arguments, sort_keys=True), 'attempted')).rowcount
            old = db.execute('SELECT fingerprint FROM conversation_bridge_intents WHERE identity=? AND message_id=?',
                             (identity, message['message_id'])).fetchone()
            if old['fingerprint'] != fingerprint:
                raise Conflict('conversation_bridge_original_reply_changed')
        if not inserted:
            return self._delivery_receipt(binding, message['message_id'])
        try:
            receipt = self._request('deliver', arguments)
        except Exception:
            try:
                receipt = self._delivery_receipt(binding, message['message_id'])
            except Exception:
                receipt = {'format': FORMAT, 'found': False, 'state': 'delivery_unknown', 'automatic_retry': False}
        self._save_receipt(identity, message['message_id'], receipt)
        return receipt

    def step(self, binding):
        """One bounded owner invocation; no loop, wake, model call or retry."""
        if not isinstance(binding, dict) or set(binding) != {'request_id', 'conversation_id'}:
            raise ValueError('conversation_bridge_original_binding_required')
        with self.lock:
            identity, task = self.service._binding(self.principal, binding)
            with self.service.transaction() as db:
                self.service._row(db, identity, binding, task)
                db.execute('INSERT OR IGNORE INTO conversation_bridge_cursors VALUES(?,0)', (identity,))
                cursor = db.execute('SELECT cursor FROM conversation_bridge_cursors WHERE identity=?', (identity,)).fetchone()[0]
                self.requests[task] = binding['request_id']
            observed = self.observe(binding)
            output = self._outbound(identity, binding, observed)
            page = self.service.request(self.principal, 'receive', dict(binding, after=cursor, limit=20))
            deliveries = []
            for message in page['messages']:
                if message['direction'] == 'to_mesh':
                    receipt = self._deliver(identity, binding, observed, message)
                    deliveries.append({'message_id': message['message_id'], 'receipt': receipt})
                    if receipt.get('state') == 'awaiting_original_live_task':
                        break
                cursor = message['sequence']
            # Refresh existing unknown/queued receipts without another delivery.
            with self.service.transaction() as db:
                db.execute('UPDATE conversation_bridge_cursors SET cursor=? WHERE identity=?', (cursor, identity))
                previous = db.execute('SELECT message_id FROM conversation_bridge_intents WHERE identity=? LIMIT 20', (identity,)).fetchall()
            seen = {item['message_id'] for item in deliveries}
            for row in previous:
                if row['message_id'] in seen:
                    continue
                try:
                    receipt = self._delivery_receipt(binding, row['message_id'])
                    self._save_receipt(identity, row['message_id'], receipt)
                    deliveries.append({'message_id': row['message_id'], 'receipt': receipt})
                except Exception:
                    pass  # Original durable attempted intent remains unknown.
            return dict(output, format='mesh-conversation-runtime/1', request_id=binding['request_id'],
                        task_id=task, cursor=cursor, deliveries=deliveries,
                        background_wake_supported=False, automatic_retry=False)


def load_context(config):
    """Private owner config only; instantiate this SAME context in OAuth factory."""
    if (not isinstance(config, dict) or set(config) != {'format', 'directory', 'owner_id', 'subject', 'ingress_client'}
            or config['format'] != 'mesh-conversation-runtime/1'):
        raise ValueError('conversation_bridge_owner_configuration_required')
    client = client_from_config(config['ingress_client'])
    backend = RemoteIngressBackend({config['subject']: client}, config['owner_id'])
    service = ConversationService(config['directory'], config['owner_id'], backend)
    principal = {'owner_id': config['owner_id'], 'source': SOURCE, 'subject': config['subject'],
                 'scopes': ['tasks:read', 'tasks:submit']}
    return backend, service, ConversationBridge(service, principal, client)


def main(argv=None):
    parser = argparse.ArgumentParser(description='One private Mesh conversation bridge step; never deploys')
    parser.add_argument('--config', required=True)
    parser.add_argument('--conversation-id', required=True)
    parser.add_argument('--request-id', required=True)
    args = parser.parse_args(argv)
    try:
        config = json.loads(_private_bytes(args.config).decode('utf8'), object_pairs_hook=_unique, parse_constant=_nonfinite)
        backend, service, bridge = load_context(config)
        print(json.dumps(bridge.step({'conversation_id': args.conversation_id, 'request_id': args.request_id}), sort_keys=True))
    except Exception:
        print(json.dumps({'ok': False, 'error': 'conversation_bridge_unconfirmed', 'automatic_retry': False}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
