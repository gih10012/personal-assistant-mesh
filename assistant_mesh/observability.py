"""Read models from the ledger, not a second dashboard-owned task system."""
import json


def projects(store, limit=50, after=0):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('invalid_limit')
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise ValueError('invalid_cursor')
    with store.transaction() as db:
        rows = db.execute('SELECT rowid AS cursor,* FROM tasks WHERE rowid>? ORDER BY rowid LIMIT ?',
                          (after, limit + 1)).fetchall()
        page = rows[:limit]
        grouped = {}
        for row in page:
            context, checkpoint = json.loads(row['context']), json.loads(row['checkpoint'])
            identity = str(context.get('project_id') or context.get('dot_id') or 'personal-assistant')
            if identity not in grouped:
                grouped[identity] = {'id': identity,
                    'title': context.get('project_title') or context.get('dot_name') or identity,
                    'status_counts': {}, 'tasks': []}
            project = grouped[identity]
            project['status_counts'][row['status']] = project['status_counts'].get(row['status'], 0) + 1
            project['tasks'].append({'id': row['id'], 'parent_id': row['parent_id'],
                'input': row['input'][:2000], 'status': row['status'], 'node': row['node'],
                'created': row['created'], 'result': (row['result'] or '')[:4000],
                'scope': row['scope'], 'native': {k: checkpoint[k] for k in
                    ('thread_id', 'turn_id', 'harness', 'mode', 'plan', 'goal') if k in checkpoint}})
        return {'projects': list(grouped.values()), 'next_cursor': page[-1]['cursor'] if page else after,
                'has_more': len(rows) > limit, 'counts_scope': 'page', 'source': 'task-ledger'}
