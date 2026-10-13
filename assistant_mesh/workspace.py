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
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from .artifact_transport import LOCAL_FORMAT, MAX_ARTIFACT_BYTES, _digest as artifact_digest, fetch_artifact
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


def _existing_journal(path):
    path = Path(path)
    _check(path.is_absolute() and '..' not in path.parts, 'workspace_existing_private_journal_required')
    _root(path.parent)
    info = path.lstat()
    _check(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
           and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
           'workspace_existing_private_journal_required')
    return path


class _LocalCallReadback(TaskCapabilityCalls):
    """Use original readback validators/RPCs without running the write constructor."""

    def __init__(self, workspace):
        self.client, self.path = workspace.client, _existing_journal(workspace.path)
        self.authority, self.node = workspace.authority, workspace.node
        self.task_id, self.task_epoch = workspace.task_id, workspace.epoch

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            yield db
        finally:
            db.close()

    def call(self, *args, **kwargs):
        raise WorkspaceError('workspace_read_only')


class MeshWorkspace:
    """Owner-installed reference adapter; Linux private storage, neutral wire.

    Readers are trusted, bounded, read-only objects installed by the owner,
    keyed by issuer. ``manifest(reference_id, operation_id, capability_id)`` and
    ``read(...)`` return owner-authenticated metadata and full bytes. The first
    reference is the original A2A message ID for a foreign issuer, or the original
    task ID for this authority's local-allocation reader. Local metadata never
    borrows a remote message/fingerprint to satisfy the transport contract.
    They must not issue the original HTTP/model/tool request. Never accept a
    model-supplied URL, file path, executable, manifest or reader here.
    """

    @_safe
    def __init__(self, client, journal_path, authority, node, task_id, epoch,
                 artifact_dir=None, artifact_readers=None, read_only=False):
        _check(callable(getattr(client, 'request', None)), 'workspace_client_required')
        self.client = client
        self.authority = _name(authority, 'authority')
        self.node = _name(node, 'node')
        self.task_id = _name(task_id, 'task_id')
        self.epoch = _epoch(epoch)
        _check(type(read_only) is bool, 'workspace_read_only_invalid')
        self.read_only = read_only
        self.path = _existing_journal(journal_path) if read_only else _journal_path(journal_path)
        self.artifact_dir = _root(artifact_dir) if artifact_dir is not None else None
        self.readers = dict(artifact_readers or {})
        _check(all(isinstance(key, str) and callable(getattr(reader, 'manifest', None))
                   and callable(getattr(reader, 'read', None))
                   for key, reader in self.readers.items()), 'workspace_reader_invalid')
        with self._db() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self._legacy_call_journal = not tables & {'workspace_identity', 'workspace_calls'} and {
                'call_identity', 'capability_calls'} <= tables
            if self._legacy_call_journal:
                self._local_journal_identity(db)
            if self.read_only and not self._legacy_call_journal:
                _check({'workspace_identity', 'workspace_calls'} <= tables, 'workspace_original_journal_missing')
                identity = db.execute('SELECT authority,node FROM workspace_identity').fetchmany(2)
                _check(len(identity) == 1 and tuple(identity[0]) == (self.authority, self.node),
                       'workspace_identity_mismatch')
        if not self.read_only and not self._legacy_call_journal:
            self._initialize_workspace()

    def _initialize_workspace(self):
        # A pre-existing thin-call journal needs no new tables/history merely to
        # read its original local artifact. New Workspace writes initialize only
        # when call() durably creates a genuine Workspace intent.
        _check(not self.read_only, 'workspace_read_only')
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
        self._legacy_call_journal = False

    @contextmanager
    def _db(self):
        path = self.path.as_uri() + '?mode=ro' if self.read_only else str(self.path)
        db = sqlite3.connect(path, uri=self.read_only, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON' if self.read_only else 'PRAGMA synchronous=FULL')
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    def _row(self, operation_id):
        _name(operation_id, 'operation_id')
        _check(not self._legacy_call_journal, 'workspace_call_not_found')
        with self._db() as db:
            row = db.execute('SELECT * FROM workspace_calls WHERE operation_id=?',
                             (operation_id,)).fetchone()
        _check(row is not None, 'workspace_call_not_found')
        _check(row['task_id'] == self.task_id, 'workspace_task_mismatch')
        _check(hashlib.sha256(row['request'].encode('utf8')).hexdigest() == row['fingerprint'],
               'workspace_original_intent_changed')
        return dict(row)

    def _remember(self, request):
        _check(not self.read_only, 'workspace_read_only')
        encoded = json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False)
        fingerprint = hashlib.sha256(encoded.encode('utf8')).hexdigest()
        if self._legacy_call_journal:
            self._initialize_workspace()
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
        _check(not self.read_only, 'workspace_read_only')
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
        if issuer == self.authority:
            with self._db() as db:
                has_thin_calls = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                            "AND name='capability_calls'").fetchone()
                old_local = has_thin_calls and db.execute('SELECT 1 FROM capability_calls WHERE operation_id=?',
                                                          (operation_id,)).fetchone()
                old_workspace = not self._legacy_call_journal and db.execute(
                    'SELECT 1 FROM workspace_calls WHERE operation_id=?', (operation_id,)).fetchone()
            if old_local and not old_workspace:
                # Preserve the actual thin intent even when an owner explicitly
                # calls it again: its own validator reconciles original IDs and
                # epochs. Do not retroactively stamp a new Workspace history.
                return self._local().call(**local)
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
        try:
            row = self._row(operation_id)
        except WorkspaceError as error:
            if str(error) != 'workspace_call_not_found':
                raise
            original, unused_request, unused_stages = self._existing_local_call(operation_id)
            return dict(_LocalCallReadback(self).result(operation_id), workspace_intent_present=False,
                        intent_source='task_capability_calls', local_call_request_sha256=original['fingerprint'])
        if json.loads(row['request'])['issuer'] == self.authority:
            try:
                self._local_artifact_binding(row)
                return _LocalCallReadback(self).result(operation_id)
            except Exception:
                return self._report(row, 'unresolved', diagnostic='original_local_call_unobserved_no_replay')
        try:
            self._hello()
            value = self._view(row, self._route('inspect', {'decision_id': row['decision_id']}),
                               row['link'] == 'confirmed')
            return self._report(row, 'observed', value)
        except Exception:
            return self._report(row, 'unresolved', diagnostic='original_status_unavailable_no_replay')

    def _local_journal_identity(self, db):
        identities = db.execute('SELECT authority,node FROM call_identity').fetchmany(2)
        _check(len(identities) == 1 and tuple(identities[0]) == (self.authority, self.node),
               'workspace_original_local_journal_identity_changed')

    def _existing_local_call(self, operation_id):
        """Strict original task+operation lookup, not an op-only adoption/write."""
        _name(operation_id, 'operation_id')
        with self._db() as db:
            db.execute('BEGIN')
            self._local_journal_identity(db)
            entries = db.execute('SELECT * FROM capability_calls WHERE operation_id=? AND task_id=? '
                'AND length(request)<=196608', (operation_id, self.task_id)).fetchmany(2)
        _check(len(entries) == 1, 'workspace_original_local_call_missing')
        original = dict(entries[0])
        request = json.loads(original['request'])
        _check(isinstance(request, dict) and set(request) == {'operation_id', 'decision', 'plan', 'lease_seconds'}
               and isinstance(request['plan'], dict) and isinstance(request['plan'].get('selections'), list)
               and len(request['plan']['selections']) == 1, 'workspace_original_local_call_changed')
        decision = request['decision']
        expected = TaskCapabilityCalls._request(request['operation_id'], decision['decision_id'],
            request['plan']['selections'][0], decision['candidates'], decision['rationale'], request['lease_seconds'])
        encoded = json.dumps(expected, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        _check(original['operation_id'] == expected['operation_id'] == operation_id
               and original['task_id'] == self.task_id and type(original['task_epoch']) is int
               and original['task_epoch'] == _epoch(original['task_epoch'])
               and original['decision_id'] == expected['decision']['decision_id']
               and original['request'] == encoded
               and original['fingerprint'] == hashlib.sha256(encoded.encode('utf8')).hexdigest(),
               'workspace_original_local_call_changed')
        stages = {name: original[name] for name in ('propose', 'reserve', 'link')}
        _check(all(value in ('ready', 'attempted', 'confirmed') for value in stages.values())
               and stages['reserve'] != 'ready', 'workspace_original_local_reserve_not_attempted')
        return original, expected, stages

    def _local_artifact_binding(self, row):
        """Read an existing local-call intent without constructing/mutating it.

        A Workspace intent alone may precede the first local call. Never create
        a TaskCapabilityCalls row, repair stages or issue RPCs while reading an
        artifact. The original normalized plan and epoch must agree in both
        private journals; completed provider evidence comes from the reader.
        """
        local = json.loads(row['request'])['local_call']
        expected = TaskCapabilityCalls._request(**local)
        original, original_request, stages = self._existing_local_call(row['operation_id'])
        _check(original['task_epoch'] == row['epoch'] and original_request == expected,
               'workspace_original_local_call_changed')
        return expected['plan'], stages, original['fingerprint']

    @_safe
    def artifact(self, operation_id):
        """Automatically retrieve only original bytes through an installed reader.

        Byte verification is not independent attestation of execution/accuracy.
        No reader/expired lease/status failure must never trigger a new call.
        """
        try:
            row = self._row(operation_id)
        except WorkspaceError as error:
            if str(error) != 'workspace_call_not_found':
                raise
            # Artifact-only compatibility: retain the actual thin-call intent
            # and its complete hash. Do not synthesize/persist Workspace intent
            # or remote message history for a previously completed operation.
            original, original_request, stages = self._existing_local_call(operation_id)
            row = None
            request = {'issuer': self.authority}
            selection = original_request['plan']['selections'][0]
            report = {'format': 'mesh-workspace-call/1', 'authority': self.authority,
                'task_id': original['task_id'], 'original_epoch': original['task_epoch'],
                'current_epoch': self.epoch, 'operation_id': original['operation_id'],
                'decision_id': original['decision_id'], 'issuer': self.authority,
                'request_sha256': original['fingerprint'], 'phase': 'artifact_unavailable',
                'stages': stages, 'remote': None, 'routing': None, 'diagnostics': [],
                'workspace_intent_present': False, 'intent_source': 'task_capability_calls',
                'local_call_request_sha256': original['fingerprint'],
                'artifact_source_kind': 'local_allocation', 'automatic_invocation_replay': False,
                'retry_with_new_id': False, 'native_tools_intercepted': False, 'execution_verified': False}
        else:
            request = json.loads(row['request'])
            selection = request['local_call']['selection']
            report = dict(self._report(row, 'artifact_unavailable'), workspace_intent_present=True,
                          intent_source='workspace')
        reader = self.readers.get(request['issuer'])
        if reader is None or self.artifact_dir is None:
            return dict(report, diagnostics=['owner_original_artifact_reader_not_configured'])
        try:
            local = request['issuer'] == self.authority
            if local:
                if row is None:
                    plan = original_request['plan']
                else:
                    plan, stages, fingerprint = self._local_artifact_binding(row)
                    report = dict(report, local_call_request_sha256=fingerprint)
                reference = self.task_id
                report = dict(report, stages=stages, artifact_source_kind='local_allocation')
                manifest = reader.manifest(reference, operation_id, selection['capability_id'])
                _check(isinstance(manifest, dict) and manifest.get('format') == LOCAL_FORMAT
                       and manifest.get('source_kind') == 'local_allocation'
                       and manifest.get('task_id') == self.task_id
                       and manifest.get('task_node') == self.node
                       and type(manifest.get('task_epoch')) is int
                       and manifest['task_epoch'] == report['original_epoch']
                       and manifest.get('allocation_plan_sha256') == artifact_digest(plan),
                       'workspace_local_artifact_binding_invalid')
            else:
                identity = self._remote_identity(row)
                reference = identity['message_id']
                _check(row['delegate'] != 'ready', 'workspace_original_delegate_not_attempted')
                manifest = reader.manifest(reference, operation_id, selection['capability_id'])
                _check(isinstance(manifest, dict) and manifest.get('sender') == self.authority
                       and manifest.get('message_id') == reference
                       and manifest.get('message_fingerprint') == digest(self._remote_message(row))
                       and manifest.get('task_id') == digest([request['issuer'], self.authority, reference]),
                       'workspace_artifact_origin_invalid')
            _check(isinstance(manifest, dict) and manifest.get('authority') == request['issuer']
                   and manifest.get('operation_id') == operation_id
                   and manifest.get('capability_id') == selection['capability_id']
                   and type(manifest.get('capability_epoch')) is int
                   and manifest['capability_epoch'] == selection['epoch']
                   and manifest.get('scope_sha256') == artifact_digest(selection['scope'])
                   and manifest.get('workload_sha256') == artifact_digest(selection['workload']),
                   'workspace_artifact_binding_invalid')
            raw = reader.read(reference, operation_id, selection['capability_id'])
            _check(isinstance(raw, bytes) and len(raw) <= MAX_ARTIFACT_BYTES,
                   'workspace_artifact_body_invalid')
            output = self.artifact_dir / (digest([self.authority, self.task_id, operation_id]) + '.json')
            result = fetch_artifact(io.BytesIO(raw), manifest, str(output))
            return dict(report, phase='artifact_verified', artifact=result, artifact_path=str(output))
        except Exception:
            return dict(report, diagnostics=['original_artifact_unavailable_or_invalid_no_replay'])
