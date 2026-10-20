"""Default-off private, source-bound event references on an already-open API.

Python3.6 pure module for the sole receiver. No Store construction, new table,
OAuth/MCP import, task mutation, native body export or public listener.
"""
import datetime
import json
from urllib.parse import urlsplit

from .conversation_authority import digest
from .ingress import _identifier
from .store import Conflict


ROUTE = '/v1/ingress/conversation/events'
FORMAT = 'mesh-private-conversation-event-feed/1'
SOURCE = 'chatgpt-chat'


def project_policies(policies):
    if not isinstance(policies, (list, tuple)) or not 1 <= len(policies) <= 32:
        raise ValueError('event_feed_explicit_project_policy_required')
    result = {}
    for value in policies:
        if (not isinstance(value, dict) or set(value) != {'source', 'subject', 'projects'}
                or value['source'] != SOURCE or not isinstance(value['projects'], (list, tuple))
                or not 1 <= len(value['projects']) <= 64):
            raise ValueError('event_feed_project_policy_invalid')
        key = (value['source'], _identifier(value['subject'], 'event_feed_subject', 1024))
        projects = frozenset(_identifier(item, 'event_feed_project') for item in value['projects'])
        if key in result or '*' in projects or len(projects) != len(value['projects']):
            raise ValueError('event_feed_project_policy_invalid')
        result[key] = projects
    return result


def feed_cursor(value):
    if value is None:
        return {'final': 0, 'question': 0}
    if (not isinstance(value, dict) or set(value) != {'final', 'question'}
            or any(type(item) is not int or not 0 <= item < (1 << 63) for item in value.values())):
        raise ValueError('event_feed_cursor_invalid')
    return dict(value)


def _utc(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


class ConversationEventFeed:
    def __init__(self, api, finals, policies):
        bridge = getattr(api, '_conversation_authority', None)
        if (finals is None or getattr(api, '_conversation_finals', None) is not finals or finals.api is not api
                or finals.store is not api.store or finals.ingress is not api.ingress
                or bridge is None or bridge.store is not api.store or bridge.ingress is not api.ingress
                or SOURCE not in bridge.sources or not callable(getattr(finals, '_record', None))):
            raise ValueError('event_feed_existing_final_and_bridge_required')
        self.api, self.finals, self.policies = api, finals, policies
        self.ingress, self.store = api.ingress, api.store

    def read(self, peer, payload):
        principal = self.ingress._principal(peer, 'tasks:read')
        allowed = self.policies.get((principal['source'], principal['subject']))
        if not allowed:
            raise PermissionError('event_feed_principal_not_authorized')
        if (not isinstance(payload, dict) or set(payload) != {'after', 'limit', 'projects'}
                or type(payload['limit']) is not int or not 1 <= payload['limit'] <= 20
                or not isinstance(payload['projects'], list) or not 1 <= len(payload['projects']) <= 64):
            raise ValueError('event_feed_bounds_required')
        projects = frozenset(_identifier(item, 'event_feed_project') for item in payload['projects'])
        if len(projects) != len(payload['projects']) or not projects <= allowed:
            raise PermissionError('event_feed_project_not_authorized')
        cursor, limit = feed_cursor(payload['after']), payload['limit']
        events = []
        with self.store.transaction() as db:
            self.finals.emits._namespace(db)
            # Never call the owner-global completion_events and filter its page.
            # Account/source/subject/project are SQL predicates BEFORE LIMIT.
            sql = '''SELECT f.rowid AS cursor,f.* FROM conversation_task_finals f
                JOIN ingress_requests r ON r.request_key=f.request_key AND r.task_id=f.task_id
                WHERE r.source=? AND r.subject=? AND f.project_id IN ('''
            sql += ','.join('?' for unused in projects) + ') AND f.rowid>? ORDER BY f.rowid LIMIT ?'
            rows = db.execute(sql, (principal['source'], principal['subject']) + tuple(sorted(projects))
                              + (cursor['final'], max(1, limit // 2))).fetchall()
            for row in rows:
                ref = self.finals._record(db, row)['event']
                if (ref['owner_id'] != self.ingress.owner_id or ref['source'] != principal['source']
                        or ref['subject'] != principal['subject'] or ref['project_id'] not in projects):
                    raise Conflict('event_feed_original_final_binding_changed')
                events.append(ref); cursor['final'] = row['cursor']
            # No JSON1 requirement on the legacy host. First scope by original
            # authenticated binding in SQL, then bounded-scan/validate its body
            # and current real lease. Only eligible references consume limit.
            rows = db.execute('''SELECT i.rowid AS cursor,i.id AS interaction_id,
                i.task_id,i.epoch,i.kind,i.params,i.answer,i.created,
                r.request_key,r.request_id,r.body FROM interactions i
                JOIN tasks t ON t.id=i.task_id
                JOIN ingress_requests r ON r.task_id=t.id
                WHERE r.source=? AND r.subject=? AND t.status='running'
                AND i.rowid>? ORDER BY i.rowid LIMIT 100''',
                (principal['source'], principal['subject'], cursor['question'])).fetchall()
            for row in rows:
                if len(events) >= limit:
                    break
                cursor['question'] = row['cursor']
                original = self.ingress._binding(db, row['request_key'], principal, row['request_id'])
                if original is None or original['task_id'] != row['task_id']:
                    raise Conflict('event_feed_original_question_binding_changed')
                project = json.loads(original['body']).get('project_id')
                if project not in projects or row['kind'] != 'question' or row['answer'] is not None:
                    continue
                task = db.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
                if not self.store._task_is_live(db, task, epoch=row['epoch']):
                    continue
                params = json.loads(row['params']); questions = params.get('questions')
                if (not isinstance(questions, list) or not questions
                        or any(not isinstance(item, dict) or item.get('isSecret') for item in questions)):
                    continue
                target = {'interaction_id': row['interaction_id'], 'task_id': row['task_id'],
                          'task_epoch': row['epoch'], 'params_sha256': digest(params)}
                # EXACT existing Bridge projection identity. Work receives only
                # this pointer; its original receive projects the public question.
                events.append({'event_id': 'mesh-attention-event-' + digest([FORMAT,
                    self.ingress.owner_id, principal['source'], principal['subject'], row['request_id'], target]),
                    'name': 'leader.attention_required', 'owner_id': self.ingress.owner_id,
                    'source': principal['source'], 'subject': principal['subject'],
                    'project_id': project, 'request_id': row['request_id'], 'task_id': row['task_id'],
                    'task_epoch': row['epoch'], 'occurred_at': _utc(row['created']),
                    'message_id': 'mesh-question-' + digest(target)})
        return {'format': FORMAT, 'events': events, 'cursor': cursor}


def attach(api, finals, *, enabled=False, policies=()):
    if type(enabled) is not bool:
        raise ValueError('event_feed_owner_enable_required')
    if not enabled:
        return None
    policies = project_policies(policies)
    old = getattr(api, '_conversation_event_feed', None)
    if old is not None:
        if old.finals is not finals or old.policies != policies:
            raise Conflict('event_feed_installed_policy_changed')
        return old
    feed = ConversationEventFeed(api, finals, policies)
    previous = api.dispatch
    def dispatch(method, path, payload, peer):
        if urlsplit(path).path == ROUTE:
            if method != 'POST' or path != ROUTE:
                raise PermissionError('event_feed_private_route_required')
            return feed.read(peer, payload)
        return previous(method, path, payload, peer)
    api.dispatch = dispatch
    api._conversation_event_feed = feed
    return feed
