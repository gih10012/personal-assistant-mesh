"""One optional task-bound entrance to local calls and foreign-authority work.

This composes existing admission/delegation ledgers, not a new scheduler or
consensus service. Models select the provider and evidence; authenticated owner
configuration supplies transport, credentials and artifact readers. Native tools
never pass through this module. Preserve this private journal on every upgrade.
"""
import functools
import hashlib
import io
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .artifact_transport import MAX_ARTIFACT_BYTES, _digest as artifact_digest, fetch_artifact
from .capability_call import TaskCapabilityCalls, _journal_path
from .egress_adapter import _root
from .networking import digest
from .resources import _epoch, _name
from .routing import _decision


class WorkspaceError(ValueError):
    """Fixed diagnostics, never transport exceptions, secrets or raw output."""


def _safe(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except WorkspaceError:
            raise
        except Exception:
            raise WorkspaceError('workspace_unavailable_or_invalid') from None
    return wrapped


def _check(condition, code):
    if not condition:
        raise WorkspaceError(code)


class MeshWorkspace:
    """Owner-installed reference adapter; Linux private storage, neutral wire.

    Readers are trusted, bounded, read-only objects installed by the owner,
    keyed by issuer. ``manifest(message_id, operation_id, capability_id)`` and
    ``read(...)`` return independently authenticated metadata and full bytes.
    They must not issue the original HTTP/model/tool request. Never accept a
    model-supplied URL, file path, executable, manifest or reader here.
    """

    @_safe
    def __init__(self, client, journal_path, authority, node, task_id, epoch,
                 artifact_dir=None, artifact_readers=None):
        _check(callable(getattr(client, 'request', None)), 'workspace_client_required')
        self.client = client
        self.authority = _name(authority, 'authority')
        self.node = _name(node, 'node')
        self.task_id = _name(task_id, 'task_id')
        self.epoch = _epoch(epoch)
        self.path = _journal_path(journal_path)
        self.artifact_dir = _root(artifact_dir) if artifact_dir is not None else None
        self.readers = dict(artifact_readers or {})
        _check(all(isinstance(key, str) and callable(getattr(reader, 'manifest', None))
                   and callable(getattr(reader, 'read', None))
                   for key, reader in self.readers.items()), 'workspace_reader_invalid')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS workspace_identity(authority TEXT,node TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS workspace_calls(
                operation_id TEXT PRIMARY KEY,decision_id TEXT UNIQUE NOT NULL,
                task_id TEXT NOT NULL,epoch INTEGER NOT NULL,request TEXT NOT NULL,
                fingerprint TEXT NOT NULL,propose TEXT NOT NULL,
                delegate TEXT NOT NULL,link TEXT NOT NULL)''')
            identity = db.execute('SELECT authority,node FROM workspace_identity').fetchall()
            if not identity:
                _check(not db.execute('SELECT 1 FROM workspace_calls LIMIT 1').fetchone(),
                       'workspace_identity_missing')
                db.execute('INSERT INTO workspace_identity VALUES(?,?)', (self.authority, self.node))
            else:
                _check(len(identity) == 1 and tuple(identity[0]) == (self.authority, self.node),
                       'workspace_identity_mismatch')
            db.commit()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    def _row(self, operation_id):
        _name(operation_id, 'operation_id')
        with self._db() as db:
            row = db.execute('SELECT * FROM workspace_calls WHERE operation_id=?',
                             (operation_id,)).fetchone()
        _check(row is not None, 'workspace_call_not_found')
        _check(row['task_id'] == self.task_id, 'workspace_task_mismatch')
        _check(hashlib.sha256(row['request'].encode('utf8')).hexdigest() == row['fingerprint'],
               'workspace_original_intent_changed')
        return dict(row)

    def _remember(self, request):
        encoded = json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False)
        fingerprint = hashlib.sha256(encoded.encode('utf8')).hexdigest()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM workspace_calls WHERE operation_id=?',
                             (request['operation_id'],)).fetchone()
            if row:
                original = json.loads(row['request'])
                comparable = lambda value: {key: item for key, item in value.items() if key != 'wire_message'}
                _check(row['task_id'] == self.task_id and comparable(original) == comparable(request)
                       and row['fingerprint'] == hashlib.sha256(row['request'].encode()).hexdigest(),
                       'workspace_intent_conflict')
            else:
                try:
                    db.execute('INSERT INTO workspace_calls VALUES(?,?,?,?,?,?,?,?,?)',
                        (request['operation_id'], request['decision_id'], self.task_id,
                         self.epoch, encoded, fingerprint, 'ready', 'ready', 'ready'))
                except sqlite3.IntegrityError:
                    raise WorkspaceError('workspace_decision_conflict') from None
            db.commit()
        return self._row(request['operation_id'])

    def _stage(self, row, name, before, after):
        _check(name in ('propose', 'delegate', 'link'), 'workspace_stage_invalid')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE workspace_calls SET ' + name +
                '=? WHERE operation_id=? AND ' + name + '=?',
                (after, row['operation_id'], before)).rowcount
            db.commit()
        return bool(changed)

    def _route(self, action, arguments):
        return self.client.request('/v1/routing/action', {'task_id': self.task_id,
            'epoch': self.epoch, 'action': action, 'arguments': arguments})

    def _hello(self):
        value = self.client.request('/v1/mesh/hello')
        _check(isinstance(value, dict) and value.get('protocol') == 'mesh-a2a/1'
               and value.get('authority') == self.authority and value.get('node') == self.authority,
               'workspace_authority_changed')

    def _context(self, **filters):
        self._hello()
        value = self._route('context', filters)
        _check(isinstance(value, dict) and value.get('format') == 'mesh-routing-context/1'
               and value.get('authority') == self.authority
               and isinstance(value.get('task'), dict)
               and value['task'].get('id') == self.task_id
               and value['task'].get('node') == self.node
               and type(value['task'].get('epoch')) is int and value['task']['epoch'] == self.epoch,
               'workspace_current_binding_invalid')
        return value

    @_safe
    def discover(self, **filters):
        """Keep complete scope/units/pool/TTL and explicit truncated sections."""
        _check(not set(filters) - {'kind', 'issuer', 'include_unavailable', 'limit',
                                   'observation_max_age_seconds'}, 'workspace_filters_invalid')
        return self._context(**filters)

    def _local(self):
        return TaskCapabilityCalls(self.client, str(self.path), self.authority,
                                   self.node, self.task_id, self.epoch)

    def _remote_identity(self, row):
        call_id = 'workspace-' + digest([self.authority, self.node, self.task_id, row['operation_id']])
        return {'call_id': call_id,
                'child_id': digest(['remote-child', self.authority, self.task_id, call_id]),
                'message_id': digest(['remote-message', self.authority, self.task_id, call_id])}

    def _remote_message(self, row):
        request = json.loads(row['request'])
        message = request.get('wire_message')
        identity = self._remote_identity(row)
        _check(isinstance(message, dict) and set(message) ==
               {'protocol', 'id', 'to', 'input', 'project', 'agent', 'parent_ref'}
               and message['protocol'] == 'mesh-a2a/1'
               and message['id'] == identity['message_id'] and message['parent_ref'] == identity['child_id']
               and message['to'] == request['issuer'] and message['project'] == request['project_id']
               and message['agent'] == request['agent_id'] and isinstance(message['input'], str),
               'workspace_original_wire_missing_or_changed')
        return message

    def _render_message(self, request):
        # Only used before FIRST durable intent, never on observation or read.
        identity = self._remote_identity({'operation_id': request['operation_id']})
        prompt = ('Execute the immutable local_call below using your local Mesh '
                  'workspace/capability_call entrance, within current owner permissions. '
                  'Do not replace operation IDs, fabricate observations or replay unknown '
                  'effects. Preserve full private artifact for the owner-installed reader. '
                  'Return original receipts and a truthful status. Mesh failure does not '
                  'restrict native tools. Request JSON:\n' + json.dumps(request, sort_keys=True))
        _check(len(prompt.encode('utf8')) <= 65536, 'workspace_delegate_too_large')
        return {'protocol': 'mesh-a2a/1', 'id': identity['message_id'], 'to': request['issuer'],
                'input': prompt, 'project': request['project_id'], 'agent': request['agent_id'],
                'parent_ref': identity['child_id']}

    def _delegation_fingerprint(self, row):
        message = self._remote_message(row)
        return digest([self.task_id, 'remote_delegate', message['to'],
                       {'input': message['input'], 'project_id': message['project'],
                        'agent_id': message['agent']}])

    def _proposal(self, row):
        request = json.loads(row['request'])
        local = request['local_call']
        selection = local['selection']
        return _decision({'decision_id': row['decision_id'],
            'work': {'action': selection['action'], 'scope': selection['scope'],
                     'workload': selection['workload']},
            'candidates': local['candidates'],
            'selection': {'kind': 'remote_delegation', 'peer': request['issuer'],
                          'local_call': local},
            'rationale': local['rationale'],
            'evidence_refs': [{'source': 'projection', 'issuer': request['issuer'],
                               'capability_id': selection['capability_id'],
                               'capability_epoch': selection['epoch']}]})

    def _view(self, row, value, linked=False):
        proposal, encoded = self._proposal(row)
        identity = self._remote_identity(row)
        _check(isinstance(value, dict) and value.get('authority') == self.authority
               and value.get('task_id') == self.task_id and value.get('original_node') == self.node
               and type(value.get('original_epoch')) is int and value['original_epoch'] == row['epoch']
               and value.get('decision_id') == row['decision_id'] and value.get('decision') == proposal
               and value.get('content_sha256') == hashlib.sha256(encoded.encode()).hexdigest(),
               'workspace_route_receipt_invalid')
        reference = value.get('linked_execution')
        if linked or reference is not None:
            _check(isinstance(reference, dict) and reference.get('kind') == 'remote_delegation'
                   and reference.get('reference_id') == identity['child_id']
                   and reference.get('peer') == json.loads(row['request'])['issuer']
                   and reference.get('message_id') == identity['message_id']
                   and reference.get('delegation_fingerprint') == self._delegation_fingerprint(row)
                   and reference.get('message_fingerprint') == digest(self._remote_message(row)),
                   'workspace_original_child_mismatch')
        return value

    def _observe_delegate(self, row):
        identity = self._remote_identity(row)
        request = json.loads(row['request'])
        context = self._context(limit=100)
        section = context.get('sections', {}).get('remote_execution_candidates', {})
        entries = [item for item in section.get('records', []) if isinstance(item, dict)
                   and item.get('child_id') == identity['child_id']]
        _check(len(entries) == 1 and entries[0].get('caller_node') == self.node
               and entries[0].get('parent_epoch') == row['epoch']
               and entries[0].get('peer') == request['issuer']
               and entries[0].get('message_id') == identity['message_id']
               and entries[0].get('delegation_fingerprint') == self._delegation_fingerprint(row)
               and entries[0].get('message_fingerprint') == digest(self._remote_message(row)),
               'workspace_original_delegate_unobserved')
        return entries[0]

    def _report(self, row, phase, routing=None, diagnostic=None):
        request = json.loads(row['request'])
        return {'format': 'mesh-workspace-call/1', 'authority': self.authority,
                'task_id': self.task_id, 'original_epoch': row['epoch'], 'current_epoch': self.epoch,
                'operation_id': row['operation_id'], 'decision_id': row['decision_id'],
                'issuer': request['issuer'], 'request_sha256': row['fingerprint'], 'phase': phase,
                'stages': {name: row[name] for name in ('propose', 'delegate', 'link')},
                'remote': self._remote_identity(row) if request['issuer'] != self.authority else None,
                'routing': routing, 'diagnostics': [diagnostic] if diagnostic else [],
                'automatic_invocation_replay': False, 'retry_with_new_id': False,
                'native_tools_intercepted': False, 'execution_verified': False}

    @_safe
    def call(self, issuer, operation_id, decision_id, selection, candidates, rationale,
             lease_seconds=60, project_id='general', agent_id='capability-specialist'):
        _name(issuer, 'issuer')
        _name(project_id, 'project_id')
        _name(agent_id, 'agent_id')
        validated = TaskCapabilityCalls._request(operation_id, decision_id, selection,
                                                 candidates, rationale, lease_seconds)
        local = {'operation_id': operation_id, 'decision_id': decision_id,
                 'selection': validated['plan']['selections'][0], 'candidates': candidates,
                 'rationale': rationale, 'lease_seconds': validated['lease_seconds']}
        request = {'issuer': issuer, 'operation_id': operation_id, 'decision_id': decision_id,
                   'local_call': local, 'project_id': project_id, 'agent_id': agent_id}
        request['wire_message'] = self._render_message(request) if issuer != self.authority else None
        row = self._remember(request)
        if issuer == self.authority:
            if row['epoch'] != self.epoch:
                # A pre-local-call crash may leave no TaskCapabilityCalls row.
                # Never create its first write under a replacement task epoch.
                return self.status(operation_id)
            return self._local().call(**local)
        try:
            self._context()
            proposal, unused = self._proposal(row)
            identity = self._remote_identity(row)
            message = self._remote_message(row)
            for stage in ('propose', 'delegate', 'link'):
                row = self._row(operation_id)
                if row[stage] == 'ready':
                    if row['epoch'] != self.epoch:
                        return self._report(row, 'original_epoch_read_only')
                    if self._stage(row, stage, 'ready', 'attempted'):
                        self._hello()
                        if stage == 'propose':
                            self._view(row, self._route('propose', {'decision': proposal}))
                        elif stage == 'delegate':
                            value = self.client.request('/v1/mesh/delegate', {
                                'task_id': self.task_id, 'epoch': self.epoch,
                                'call_id': identity['call_id'], 'peer': issuer,
                                'arguments': {'input': message['input'], 'project_id': project_id, 'agent_id': agent_id}})
                            _check(isinstance(value, dict) and value.get('parent_id') == self.task_id
                                   and value.get('id') == identity['child_id']
                                   and value.get('peer') == issuer
                                   and value.get('message_id') == identity['message_id'],
                                   'workspace_delegate_ack_invalid')
                        else:
                            self._view(row, self._route('link', {'decision_id': decision_id,
                                'kind': 'remote_delegation', 'reference_id': identity['child_id']}), True)
                        self._stage(row, stage, 'attempted', 'confirmed')
                        continue
                if stage == 'delegate':
                    self._observe_delegate(row)
                else:
                    self._view(row, self._route('inspect', {'decision_id': decision_id}), stage == 'link')
                self._stage(row, stage, 'attempted', 'confirmed')
            return self.status(operation_id)
        except Exception:
            return self._report(self._row(operation_id), 'unresolved',
                                diagnostic='original_receipt_unavailable_no_replay')

    @_safe
    def status(self, operation_id):
        row = self._row(operation_id)
        if json.loads(row['request'])['issuer'] == self.authority:
            try:
                return self._local().result(operation_id)
            except Exception:
                return self._report(row, 'unresolved', diagnostic='original_local_call_unobserved_no_replay')
        try:
            self._hello()
            value = self._view(row, self._route('inspect', {'decision_id': row['decision_id']}),
                               row['link'] == 'confirmed')
            return self._report(row, 'observed', value)
        except Exception:
            return self._report(row, 'unresolved', diagnostic='original_status_unavailable_no_replay')

    @_safe
    def artifact(self, operation_id):
        """Automatically retrieve only original bytes through an installed reader.

        Byte verification is not independent attestation of execution/accuracy.
        No reader/expired lease/status failure must never trigger a new call.
        """
        row = self._row(operation_id)
        request = json.loads(row['request'])
        report = self._report(row, 'artifact_unavailable')
        reader = self.readers.get(request['issuer'])
        if reader is None or self.artifact_dir is None or request['issuer'] == self.authority:
            return dict(report, diagnostics=['owner_original_artifact_reader_not_configured'])
        selection = request['local_call']['selection']
        identity = self._remote_identity(row)
        try:
            _check(row['delegate'] != 'ready', 'workspace_original_delegate_not_attempted')
            manifest = reader.manifest(identity['message_id'], operation_id, selection['capability_id'])
            _check(isinstance(manifest, dict) and manifest.get('authority') == request['issuer']
                   and manifest.get('sender') == self.authority
                   and manifest.get('message_id') == identity['message_id']
                   and manifest.get('message_fingerprint') == digest(self._remote_message(row))
                   and manifest.get('task_id') == digest([request['issuer'], self.authority, identity['message_id']])
                   and manifest.get('operation_id') == operation_id
                   and manifest.get('capability_id') == selection['capability_id']
                   and type(manifest.get('capability_epoch')) is int
                   and manifest['capability_epoch'] == selection['epoch']
                   and manifest.get('scope_sha256') == artifact_digest(selection['scope'])
                   and manifest.get('workload_sha256') == artifact_digest(selection['workload']),
                   'workspace_artifact_binding_invalid')
            raw = reader.read(identity['message_id'], operation_id, selection['capability_id'])
            _check(isinstance(raw, bytes) and len(raw) <= MAX_ARTIFACT_BYTES,
                   'workspace_artifact_body_invalid')
            output = self.artifact_dir / (digest([self.authority, self.task_id, operation_id]) + '.json')
            result = fetch_artifact(io.BytesIO(raw), manifest, str(output))
            return dict(report, phase='artifact_verified', artifact=result, artifact_path=str(output))
        except Exception:
            return dict(report, diagnostics=['original_artifact_unavailable_or_invalid_no_replay'])
