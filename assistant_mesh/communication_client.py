"""Opt-in old Claw skill transport, preserving its immutable logical origin.

The original ilink_remote submit/reservation code remains in charge. Existing
requests only inspect the original imported outbox row; this module never
creates a reservation or retries a POST. No host tunnel or native tool changes.
"""
import hashlib
import http.client
import json
import re
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = 'mesh-communication-owner-client/1'
ORIGIN_HEADER = 'X-Mesh-Owner-Origin'
IDENTITY_HEADER = 'X-Mesh-Communication-Id'
_REQUEST = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')


def control_origin(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ValueError('BOT_REMOTE_INVALID_CONFIG')
    value = value.rstrip('/')
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('BOT_REMOTE_UNTRUSTED_ORIGIN') from None
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or port is not None and not 1 <= port <= 65535
            or parsed.scheme != 'https' and not (
                parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1'))):
        raise ValueError('BOT_REMOTE_UNTRUSTED_ORIGIN')
    return value


def validate_route(config, access):
    route = config.get('mesh_communication') if isinstance(config, dict) else None
    if (not isinstance(route, dict) or set(route) != {
            'protocol', 'control_url', 'token_file', 'communication_id', 'legacy_control_url'}
            or route['protocol'] != PROTOCOL
            or not isinstance(route['communication_id'], str)
            or not _SHA.fullmatch(route['communication_id'])):
        raise ValueError('BOT_REMOTE_COMMUNICATION_BINDING_INVALID')
    logical = control_origin(config.get('control_url'))
    if logical != control_origin(route['legacy_control_url']):
        raise ValueError('BOT_REMOTE_ORIGINAL_ORIGIN_CHANGED')
    endpoint = control_origin(route['control_url'])
    if not isinstance(route['token_file'], str) or not route['token_file']:
        raise ValueError('BOT_REMOTE_INVALID_TOKEN')
    token = access.private_read(Path(route['token_file'])).strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError('BOT_REMOTE_INVALID_TOKEN')
    return endpoint, token, logical, route['communication_id']


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('BOT_REMOTE_REDIRECT_BLOCKED')


def owner_request(config, access, endpoint, body=None):
    """New physical endpoint; original config URL/journal fingerprint unaltered."""
    allowed = {'/v1/status'} if body is None else {
        '/v1/inbox', '/v1/notify', '/v1/notify/status'}
    if endpoint not in allowed:
        raise ValueError('BOT_REMOTE_COMMUNICATION_ROUTE_UNSUPPORTED')
    base, token, logical, identity = validate_route(config, access)
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
               ORIGIN_HEADER: logical, IDENTITY_HEADER: identity}
    request = urllib.request.Request(base + endpoint,
        data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(
                request, timeout=15) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('BOT_REMOTE_RESPONSE_TOO_LARGE')
            value = json.loads(raw)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError('BOT_REMOTE_UNAVAILABLE_NO_LOCAL_FALLBACK') from None
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        raise ValueError('BOT_REMOTE_UNAVAILABLE_NO_LOCAL_FALLBACK') from None
    if not isinstance(value, dict):
        raise ValueError('BOT_REMOTE_INVALID_RESPONSE')
    return value


def _status(config, access, identity, attachment_id=None):
    try:
        value = owner_request(config, access, '/v1/notify/status', {'request_id': identity})
        status = value.get('status')
        if (status not in ('pending', 'submitting', 'accepted', 'rejected', 'unknown', 'waiting_auth')
                or value.get('id') != identity):
            raise ValueError('BOT_REMOTE_INVALID_RESPONSE')
    except ValueError:
        return {'ok': False, 'code': 'BOT_REMOTE_SEND_OUTCOME_UNKNOWN',
            'request_id': identity, 'outcome_unknown': True, 'replayed': True,
            'delivery_verified': False, 'automatic_retry': False}
    result = dict(value, ok=status not in ('rejected', 'unknown'),
        code='BOT_REMOTE_SEND_' + status.upper(), request_id=identity,
        outcome_unknown=status in ('unknown', 'submitting'), replayed=True,
        delivery_verified=False, automatic_retry=False)
    if attachment_id:
        result['attachment_id'] = attachment_id
    return result


def legacy_preflight(args, access, root, config):
    """Called BEFORE old upload or submit, not a replacement sender.

    Text replay continues the original module's exact fingerprint check. New
    media is unsupported before reservation/upload/network. Already reserved
    media can only match its original local records and read original status.
    """
    validate_route(config, access)
    media = next((getattr(args, key, None) for key in ('image', 'video', 'file')
                  if getattr(args, key, None)), None)
    if getattr(args, 'operation', None) != 'send' or media is None:
        return None
    identity = getattr(args, 'request_id', None)
    if not isinstance(identity, str) or not _REQUEST.fullmatch(identity):
        raise ValueError('BOT_REQUEST_ID_REQUIRED')
    journal = root / 'remote-sends' / (identity + '.json')
    if not journal.exists():
        return {'ok': False, 'code': 'BOT_REMOTE_MEDIA_UNSUPPORTED', 'request_id': identity,
                'outcome_unknown': False, 'submission_count': 0,
                'delivery_verified': False, 'automatic_retry': False}
    upload = json.loads(access.private_read(root / 'remote-uploads' / (identity + '.json')))
    if not isinstance(upload, dict) or not isinstance(upload.get('item'), dict):
        raise ValueError('BOT_REQUEST_ID_CONFLICT')
    if not media.is_file() or not 0 < media.stat().st_size <= args.max_bytes:
        raise ValueError('BOT_INVALID_MEDIA_FILE_OR_SIZE')
    with media.open('rb') as stream:
        data = stream.read(args.max_bytes + 1)
    if len(data) > args.max_bytes:
        raise ValueError('BOT_MEDIA_TOO_LARGE')
    kind = 'image' if getattr(args, 'image', None) else 'video' if getattr(args, 'video', None) else 'file'
    original_upload = hashlib.sha256(json.dumps(
        [kind, media.name, hashlib.sha256(data).hexdigest()]).encode()).hexdigest()
    original_submit = hashlib.sha256(json.dumps([config['control_url'].rstrip('/'),
        '/v1/notify/media', {'items': [upload['item']]}], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    reserved = json.loads(access.private_read(journal))
    if (upload.get('fingerprint') != original_upload or not isinstance(reserved, dict)
            or reserved.get('fingerprint') != original_submit):
        raise ValueError('BOT_REQUEST_ID_CONFLICT')
    return _status(config, access, identity, upload.get('attachment_id'))
