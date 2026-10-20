"""Modern sidecar's explicit access / private Client feed / lifecycle wiring."""
import asyncio
import contextlib
import json

from .conversation_event_feed import FORMAT, ROUTE, feed_cursor, project_policies
from .conversation import encoded
from .ingress import validate_peer
from .mcp_events import MCPEvents, EventDeliveryLoop, NAMES
from .mcp_http_auth import NotConfigured, SOURCE
from .store import Conflict


class ProjectEventAccess:
    """Private owner grants + REQUIRED live account/subject revocation evidence.

    A project label or a currently running task is never an authorization grant.
    replace() is owner config reload, not a tool/model/API operation.
    """
    def __init__(self, policy, policies, *, account_active):
        if not callable(account_active):
            raise NotConfigured('events_live_account_access_required')
        self.policy, self.account_active = policy, account_active
        self.replace(policies)

    def replace(self, policies):
        grants = project_policies(policies)
        approved = {entry['mesh_subject'] for entry in self.policy['subjects'] if 'tasks:read' in entry['scopes']}
        if any(source != SOURCE or subject not in approved for source, subject in grants):
            raise NotConfigured('events_project_grant_subject_not_approved')
        self.grants = grants

    def projects(self, principal):
        try:
            binding = validate_peer({'role': 'ingress', 'ingress': principal}, self.policy['owner_binding'])
            current = {entry['mesh_subject'] for entry in self.policy['subjects'] if 'tasks:read' in entry['scopes']}
            if (binding['source'] != SOURCE or 'tasks:read' not in binding['scopes']
                    or binding['subject'] not in current or self.account_active(dict(binding)) is not True):
                return ()
            return tuple(sorted(self.grants.get((binding['source'], binding['subject']), ())))
        except Exception:
            return ()

    def __call__(self, principal, name, args):
        projects = self.projects(principal)
        return bool(projects) if name is None else name in NAMES and args.get('project_id') in projects


class RemoteReceiptSource:
    """Existing Client with a fixed ingress credential, NEVER operator/OAuth bearer.

    Credential role remains the receiver's authentication authority. The source
    sends only the exact private route, cursor, limit and currently granted
    project subset. It never reads the owner's global completion feed.
    """
    def __init__(self, client, principal, access):
        if not callable(getattr(client, 'request', None)) or not isinstance(access, ProjectEventAccess):
            raise NotConfigured('events_private_ingress_client_and_access_required')
        self.principal = validate_peer({'role': 'ingress', 'ingress': principal}, access.policy['owner_binding'])
        if self.principal['source'] != SOURCE or 'tasks:read' not in self.principal['scopes']:
            raise PermissionError('events_private_ingress_source_required')
        self.client, self.access = client, access

    def __call__(self, after, limit):
        projects = self.access.projects(self.principal)
        if not projects:
            raise PermissionError('events_current_account_or_project_access_revoked')
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('event_feed_limit_invalid')
        before = feed_cursor(after)
        page = self.client.request(ROUTE, {'after': before, 'limit': limit, 'projects': list(projects)})
        if (not isinstance(page, dict) or set(page) != {'format', 'events', 'cursor'} or page['format'] != FORMAT
                or not isinstance(page['events'], list) or len(page['events']) > limit):
            raise ValueError('events_private_feed_response_invalid')
        cursor = feed_cursor(page['cursor'])
        if any(cursor[key] < before[key] for key in cursor):
            raise Conflict('events_private_feed_cursor_changed')
        for event in page['events']:
            if (not isinstance(event, dict) or set(event) != {'event_id', 'name', 'owner_id', 'source', 'subject',
                'project_id', 'request_id', 'task_id', 'task_epoch', 'occurred_at', 'message_id'}
                    or any(event[key] != self.principal[key] for key in ('owner_id', 'source', 'subject'))
                    or not self.access(self.principal, event['name'], {'project_id': event['project_id']})):
                raise PermissionError('events_private_feed_original_audience_changed')
        return {'receipts': page['events'], 'next_cursor': cursor}


def attach_lifecycle(app, events, source, *, enabled=False):
    """Wrap the EXISTING ASGI lifespan, preserving all OAuth/SDK startup/close."""
    if type(enabled) is not bool:
        raise ValueError('events_owner_lifecycle_enable_required')
    if not enabled:
        return None
    if (not isinstance(events, MCPEvents) or not isinstance(source, RemoteReceiptSource)
            or events.owner != source.principal['owner_id'] or events.authorize is not source.access):
        raise NotConfigured('events_lifecycle_matching_access_required')
    old = getattr(app, '_mesh_event_delivery', None)
    if old is not None:
        if old.service is not events or old.source is not source:
            raise Conflict('events_installed_lifecycle_changed')
        return old
    binding = encoded({key: source.principal[key] for key in ('owner_id', 'source', 'subject')}).decode()
    with events.transaction() as db:
        recorded = db.execute("SELECT value FROM event_metadata WHERE key='private_feed_binding'").fetchone()
        if recorded is not None and recorded[0] != binding:
            raise Conflict('events_installed_feed_principal_changed')
        if recorded is None:
            # An owner-installed source is fixed for this cursor namespace.
            # Never reinterpret an old in-process int cursor as a remote one.
            cursor = db.execute("SELECT value FROM event_metadata WHERE key='receipt_cursor'").fetchone()
            if cursor is not None:
                feed_cursor(json.loads(cursor[0]))
            db.execute("INSERT INTO event_metadata VALUES('private_feed_binding',?)", (binding,))
    previous = app.router.lifespan_context
    loop = EventDeliveryLoop(events, receipt_source=source)
    @contextlib.asynccontextmanager
    async def lifespan(application):
        async with previous(application) as state:
            loop.start()
            try:
                yield state
            finally:
                await asyncio.to_thread(loop.close)
    app.router.lifespan_context = lifespan
    app._mesh_event_delivery = loop
    return loop
