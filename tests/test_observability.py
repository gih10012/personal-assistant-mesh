import json
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.observability import projects
from assistant_mesh.server import API
from assistant_mesh.store import Store


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db', clock=lambda: 1000)

    def tearDown(self):
        self.temp.cleanup()

    def test_project_observes_real_parent_and_child_without_copying_checkpoint(self):
        parent = self.store.create_task('parent', context={'project_id': 'test-project'})
        child = self.store.create_task('child', parent_id=parent, context={'project_id': 'test-project'})
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET checkpoint=? WHERE id=?',
                (json.dumps({'thread_id': 'native', 'credential_value': 'never-render-this'}), parent))
        result = projects(self.store)
        self.assertEqual('task-ledger', result['source'])
        self.assertEqual(1, len(result['projects']))
        tasks = result['projects'][0]['tasks']
        self.assertEqual(parent, next(t for t in tasks if t['id'] == child)['parent_id'])
        self.assertNotIn('never-render-this', json.dumps(result))

    def test_pagination_is_stable_for_equal_creation_timestamps(self):
        identities = [self.store.create_task(str(n)) for n in range(5)]
        result, seen, after = None, [], 0
        while result is None or result['has_more']:
            result = projects(self.store, limit=2, after=after)
            seen.extend(t['id'] for p in result['projects'] for t in p['tasks'])
            after = result['next_cursor']
        self.assertEqual(identities, seen)

    def test_viewer_can_read_but_never_mutate_tasks_or_notify(self):
        api = API({'database': str(self.root / 'api.db'), 'peers': []})
        identity = api.store.create_task('work')
        viewer = {'role': 'viewer'}
        self.assertEqual(identity, api.dispatch('GET', '/v1/projects?limit=1', {}, viewer)['projects'][0]['tasks'][0]['id'])
        self.assertEqual(identity, api.dispatch('POST', '/v1/task/status', {'id': identity}, viewer)['id'])
        for path, payload in [('/v1/tasks', {'input': 'unauthorized'}),
                              ('/v1/task/control', {'id': identity, 'command': 'pause'}),
                              ('/v1/notify', {'text': 'unauthorized', 'request_id': 'x'})]:
            with self.subTest(path=path), self.assertRaises(PermissionError):
                api.dispatch('POST', path, payload, viewer)


if __name__ == '__main__':
    unittest.main()
