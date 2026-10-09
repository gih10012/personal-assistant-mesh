"""Explicit owner-reviewed result publication for an existing ingress task.

This module never publishes raw Store results or executes/fetches a reference.
An authenticated operator approves a bounded separate summary. The actual task
result digest is computed as a compare-and-swap anchor, NOT proof of result
quality or an artifact's bytes. Source/subject reads remain independently bound
to the authenticated ingress peer. Native tools and effect ledgers are untouched.
"""
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

from .ingress import (Ingress, PROTOCOL as INGRESS_PROTOCOL, _digest, _encoded,
                      _identifier, validate_config, validate_peer)
from .store import Conflict


PROTOCOL = 'owner-ingress-result/1'
MAX_SUMMARY_BYTES = 4096
MAX_ARTIFACTS = 8
MAX_BODY_BYTES = 32768
MAX_SOURCE_BYTES = 8 * 1024 * 1024
_FIELDS = frozenset(('publication_id', 'source', 'subject', 'request_id', 'task_id',
                     'task_epoch', 'task_result_sha256', 'reviewed', 'result'))
_COLUMNS = frozenset(('publication_id', 'request_key', 'body', 'fingerprint', 'anchor', 'created'))
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_OBVIOUS_CREDENTIAL = re.compile(
    r'(?i)(\bbearer\s+[a-z0-9._~+/=-]{8,}|-----BEGIN [^-\r\n]*PRIVATE KEY-----'
    r'|\bsk-(?:proj-)?[a-z0-9_-]{20,})')


def _hash(value):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError('invalid_ingress_result_sha256')
    return value


def _text(value, maximum, label):
    try:
        valid = (isinstance(value, str) and bool(value.strip())
                 and len(value.encode('utf8')) <= maximum
                 and all(ord(char) >= 32 or char in '\n\t' for char in value))
    except UnicodeError:
        valid = False
    if not valid:
        raise ValueError('invalid_ingress_result_' + label)
    if _OBVIOUS_CREDENTIAL.search(value):
        raise ValueError('private_ingress_result_not_allowed')
    return value


def _public_reference(value):
    """Check reference syntax only. No DNS, HTTP, file access or public proof."""
    url = _identifier(value, 'result_url', 2048)
    if any(char.isspace() for char in url) or '\\' in url:
        raise ValueError('invalid_ingress_result_url')
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != 'https' or not host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or '?' in url or '#' in url or parsed.port not in (None, 443)):
            raise ValueError('unsafe_reference')
        host = host.rstrip('.').lower()
        if (host in ('localhost', 'localhost.localdomain')
                or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home', '.onion'))):
            raise ValueError('private_reference')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ('.' not in host or not re.fullmatch(r'[a-z0-9.-]+', host)
                    or all(re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', label)
                           for label in host.split('.'))
                    or any(not label or len(label) > 63 or label.startswith('-')
                           or label.endswith('-') for label in host.split('.'))):
                raise ValueError('invalid_reference_host')
        else:
            addresses = [address]
            mapped = getattr(address, 'ipv4_mapped', None)
            if mapped is not None:
                addresses.append(mapped)
            if any(not item.is_global or item.is_multicast or item.is_unspecified
                   or item.is_reserved or item.is_link_local or item.is_loopback for item in addresses):
                raise ValueError('private_reference')
        # Encoded control bytes and nested URL schemes are not useful public
        # artifact paths. Other path bytes remain a declared reference only.
        if re.search(r'%(?:0[0-9a-f]|1[0-9a-f]|7f)', parsed.path, re.IGNORECASE):
            raise ValueError('unsafe_reference_path')
    except (ValueError, UnicodeError):
        raise ValueError('invalid_ingress_result_url') from None
    if _OBVIOUS_CREDENTIAL.search(url):
        raise ValueError('private_ingress_result_not_allowed')
    return url


def validate_publication(payload):
    """Pure bounded content validation; reviewed=true is NOT independent proof."""
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError('invalid_ingress_result_publication')
    value = {}
    for field in ('publication_id', 'source', 'request_id', 'task_id'):
        value[field] = _identifier(payload[field], 'result_' + field)
        if field != 'source' and _OBVIOUS_CREDENTIAL.search(value[field]):
            raise ValueError('private_ingress_result_not_allowed')
    value['subject'] = _identifier(payload['subject'], 'result_subject', 1024)
    epoch = payload['task_epoch']
    if type(epoch) is not int or epoch < 1:
        raise ValueError('invalid_ingress_result_task_epoch')
    value['task_epoch'] = epoch
    value['task_result_sha256'] = _hash(payload['task_result_sha256'])
    if payload['reviewed'] is not True:
        raise PermissionError('ingress_result_owner_review_required')
    value['reviewed'] = True
    result = payload['result']
    if (not isinstance(result, dict) or 'summary' not in result
            or set(result) - {'summary', 'artifacts'}):
        raise ValueError('invalid_ingress_result_body')
    summary = _text(result['summary'], MAX_SUMMARY_BYTES, 'summary')
    artifacts = result.get('artifacts', [])
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
        raise ValueError('invalid_ingress_result_artifacts')
    normalized = []
    for artifact in artifacts:
        if (not isinstance(artifact, dict) or not {'url', 'label'} <= set(artifact)
                or set(artifact) - {'url', 'label', 'declared_sha256'}):
            raise ValueError('invalid_ingress_result_artifact')
        item = {'url': _public_reference(artifact['url']),
                'label': _text(artifact['label'], 200, 'artifact_label')}
        if 'declared_sha256' in artifact:
            item['declared_sha256'] = _hash(artifact['declared_sha256'])
        normalized.append(item)
    value['result'] = {'summary': summary, 'artifacts': normalized}
    if len(_encoded(value).encode('utf8')) > MAX_BODY_BYTES:
        raise ValueError('ingress_result_publication_too_large')
    return value


class IngressResults:
    """One immutable approved publication per ingress request, local Store only.

    Caller authenticates the peer. Operator publication is private integration
    authority, never delegated by a request's reviewed/source/subject fields.
    Read checks current identity, task completion and the original result anchor
    in ONE transaction. No OAuth/MCP/Cloud transport or result revision workflow
    is installed; same-UID code/SQLite writers are outside this trust boundary.
    """
    def __init__(self, store, authority_id, ingress_config):
        self.owner_id = validate_config(ingress_config)['owner_id']
        self.authority = _identifier(authority_id, 'authority_id')
        self.store = store
        expected = {'schema': '1', 'authority': self.authority, 'owner': self.owner_id}
        with store.transaction() as db:
            self._namespace(db)
            db.execute('''CREATE TABLE IF NOT EXISTS ingress_result_metadata(
                key TEXT PRIMARY KEY,value TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS ingress_result_publications(
                publication_id TEXT PRIMARY KEY,request_key TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL,fingerprint TEXT NOT NULL,anchor TEXT NOT NULL,
                created REAL NOT NULL)''')
            if ({row[1] for row in db.execute('PRAGMA table_info(ingress_result_metadata)')}
                    != {'key', 'value'} or
                    {row[1] for row in db.execute('PRAGMA table_info(ingress_result_publications)')}
                    != _COLUMNS):
                raise Conflict('ingress_result_schema_mismatch')
            recorded = dict(db.execute('SELECT key,value FROM ingress_result_metadata'))
            if not recorded:
                if db.execute('SELECT 1 FROM ingress_result_publications LIMIT 1').fetchone():
                    raise Conflict('ingress_result_namespace_missing')
                for key, value in expected.items():
                    db.execute('INSERT INTO ingress_result_metadata VALUES(?,?)', (key, value))
            elif recorded != expected:
                raise Conflict('ingress_result_namespace_mismatch')

    def _namespace(self, db):
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'ingress_metadata', 'ingress_requests'} <= tables:
            raise Conflict('ingress_result_ingress_unavailable')
        if dict(db.execute('SELECT key,value FROM ingress_metadata')) != {
                'schema': '1', 'authority': self.authority, 'owner': self.owner_id}:
            raise Conflict('ingress_namespace_mismatch')

    def _key(self, source, subject, request_id):
        return _digest([INGRESS_PROTOCOL, self.authority, self.owner_id, source, subject, request_id])

    def _publication_namespace(self, db):
        if dict(db.execute('SELECT key,value FROM ingress_result_metadata')) != {
                'schema': '1', 'authority': self.authority, 'owner': self.owner_id}:
            raise Conflict('ingress_result_namespace_mismatch')

    def _binding(self, db, source, subject, request_id):
        key = self._key(source, subject, request_id)
        row = Ingress._binding(db, key, {'source': source, 'subject': subject}, request_id)
        if not row:
            raise PermissionError('ingress_result_not_visible')
        return row

    @staticmethod
    def _anchor(db, binding, value):
        if value['task_id'] != binding['task_id']:
            raise Conflict('ingress_result_task_mismatch')
        task = db.execute('''SELECT id,status,node,epoch,attempts,
            CASE WHEN length(CAST(result AS BLOB))<=? THEN result ELSE NULL END AS result,
            length(CAST(result AS BLOB)) AS result_bytes FROM tasks WHERE id=?''',
                          (MAX_SOURCE_BYTES, binding['task_id'])).fetchone()
        if (not task or task['status'] != 'completed' or not task['node']
                or type(task['epoch']) is not int or task['epoch'] < 1
                or type(task['attempts']) is not int or task['attempts'] < 1
                or not isinstance(task['result'], str) or not task['result'].strip()
                or not isinstance(task['result_bytes'], int) or task['result_bytes'] > MAX_SOURCE_BYTES):
            raise Conflict('ingress_result_task_not_completed')
        try:
            result_hash = hashlib.sha256(task['result'].encode('utf8')).hexdigest()
        except UnicodeError:
            raise Conflict('ingress_result_source_invalid') from None
        if task['epoch'] != value['task_epoch'] or result_hash != value['task_result_sha256']:
            raise Conflict('ingress_result_source_changed')
        return {'task_id': task['id'], 'task_epoch': task['epoch'], 'attempts': task['attempts'],
                'node_sha256': _digest(task['node']), 'result_sha256': result_hash,
                'request_fingerprint': binding['fingerprint']}

    @staticmethod
    def _stored(row):
        try:
            value = validate_publication(json.loads(row['body']))
            encoded = _encoded(value)
            if (encoded != row['body'] or hashlib.sha256(encoded.encode('utf8')).hexdigest()
                    != row['fingerprint'] or value['publication_id'] != row['publication_id']):
                raise ValueError('changed')
            return value
        except (ValueError, TypeError, PermissionError):
            raise Conflict('ingress_result_publication_invalid') from None

    def _view(self, db, row, binding):
        value = self._stored(row)
        if (row['request_key'] != binding['request_key'] or value['source'] != binding['source']
                or value['subject'] != binding['subject'] or value['request_id'] != binding['request_id']):
            raise Conflict('ingress_result_binding_changed')
        if _encoded(self._anchor(db, binding, value)) != row['anchor']:
            raise Conflict('ingress_result_source_changed')
        return {'format': PROTOCOL, 'publication_id': row['publication_id'],
                'request_id': binding['request_id'], 'task_id': binding['task_id'],
                'published': True, 'result': value['result'],
                'review_verification': 'authenticated_owner_approval',
                'task_binding_verified': True, 'execution_verified': False,
                'artifact_content_verified': False, 'native_tools_intercepted': False}

    def publish(self, peer, payload):
        if not isinstance(peer, dict) or peer.get('role') != 'operator':
            raise PermissionError('ingress_result_operator_required')
        value = validate_publication(payload)
        encoded = _encoded(value)
        fingerprint = hashlib.sha256(encoded.encode('utf8')).hexdigest()
        with self.store.transaction() as db:
            self._namespace(db)
            self._publication_namespace(db)
            binding = self._binding(db, value['source'], value['subject'], value['request_id'])
            anchor = _encoded(self._anchor(db, binding, value))
            old = db.execute('SELECT * FROM ingress_result_publications WHERE publication_id=?',
                             (value['publication_id'],)).fetchone()
            if old:
                if old['body'] != encoded or old['fingerprint'] != fingerprint or old['anchor'] != anchor:
                    raise Conflict('ingress_result_publication_content_conflict')
                return dict(self._view(db, old, binding), publication_created=False)
            if db.execute('SELECT 1 FROM ingress_result_publications WHERE request_key=?',
                          (binding['request_key'],)).fetchone():
                raise Conflict('ingress_request_already_published')
            db.execute('''INSERT INTO ingress_result_publications(publication_id,request_key,
                body,fingerprint,anchor,created) VALUES(?,?,?,?,?,?)''',
                       (value['publication_id'], binding['request_key'], encoded, fingerprint,
                        anchor, self.store.clock()))
            row = db.execute('SELECT * FROM ingress_result_publications WHERE publication_id=?',
                             (value['publication_id'],)).fetchone()
            return dict(self._view(db, row, binding), publication_created=True)

    def read(self, peer, payload):
        binding = validate_peer(peer, self.owner_id)
        if 'tasks:read' not in binding['scopes']:
            raise PermissionError('ingress_scope_required')
        if not isinstance(payload, dict) or set(payload) != {'request_id'}:
            raise ValueError('invalid_ingress_result_read')
        request_id = _identifier(payload['request_id'], 'request_id')
        with self.store.transaction() as db:
            self._namespace(db)
            self._publication_namespace(db)
            request = self._binding(db, binding['source'], binding['subject'], request_id)
            row = db.execute('SELECT * FROM ingress_result_publications WHERE request_key=?',
                             (request['request_key'],)).fetchone()
            if not row:
                raise PermissionError('ingress_result_not_visible')
            return self._view(db, row, request)
