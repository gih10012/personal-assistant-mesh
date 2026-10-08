import json
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.networking import Network, PROTOCOL
from assistant_mesh.store import Store


class MachineFencingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'ledger.sqlite')
        self.store.heartbeat('cloud', ['agent', 'mesh.node:cloud'])
        self.store.heartbeat('laptop', ['agent', 'leader'])

    def tearDown(self):
        self.directory.cleanup()

    def test_a2a_local_child_cannot_remove_machine_fence(self):
        network = Network(self.store, 'cloud')
        network.receive('laptop', {'protocol': PROTOCOL, 'id': 'fenced-parent',
            'to': 'cloud', 'input': 'work', 'project': 'project', 'agent': 'parent'})
        parent = self.store.claim('cloud')
        child = self.store.agent_action(parent['id'], 'cloud', parent['epoch'],
            'local-child', 'delegate', {'input': 'local work', 'required': ['agent'], 'agent_id': 'child'})
        with self.store.transaction() as db:
            row = db.execute('SELECT required,context FROM tasks WHERE id=?', (child['id'],)).fetchone()
        self.assertIn('mesh.node:cloud', json.loads(row['required']))
        self.assertEqual('child', json.loads(row['context'])['agent_id'])
        self.assertIsNone(self.store.claim('laptop'))
        self.assertEqual(child['id'], self.store.claim('cloud')['id'])

    def test_maintenance_memory_and_children_are_node_isolated(self):
        scopes = []
        for index, authority in enumerate(('local:cloud', 'local:laptop')):
            context = {'authority': authority, 'project_id': 'node-runtime',
                'agent_id': 'self-maintenance', 'memory_scope': 'maintenance:' + authority,
                'session_scope': 'maintenance-parent-' + str(index),
                'origin': {'kind': 'node-maintenance', 'peer': 'known-peer'}}
            task_id = self.store.create_task('maintenance', ['agent', 'mesh.node:cloud'], context=context)
            task = self.store.claim('cloud')
            self.assertEqual(task_id, task['id'])
            self.assertEqual((context['memory_scope'], context['memory_scope']), self.store.memory_scopes(context))
            child = self.store.agent_action(task_id, 'cloud', task['epoch'], 'maintenance-child-' + str(index),
                'delegate', {'input': 'local diagnosis', 'project_id': 'shared', 'agent_id': 'same-specialist'})
            with self.store.transaction() as db:
                row = db.execute('SELECT context,scope FROM tasks WHERE id=?', (child['id'],)).fetchone()
            scopes.append(row['scope'])
            child_context = json.loads(row['context'])
            self.assertEqual(self.store.memory_scopes(child_context)[0], self.store.memory_scopes(child_context)[1])
            self.store.update_task(task_id, 'cloud', task['epoch'], status='completed')
            claimed = self.store.claim('cloud')
            self.store.update_task(claimed['id'], 'cloud', claimed['epoch'], status='completed')
        self.assertNotEqual(scopes[0], scopes[1])
        self.assertNotEqual('project:shared:same-specialist', scopes[0])


if __name__ == '__main__':
    unittest.main()
