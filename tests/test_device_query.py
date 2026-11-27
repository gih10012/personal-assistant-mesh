import json
from unittest import TestCase
from unittest.mock import Mock, patch

from assistant_mesh.device_query import FORMAT, ROUTE, inspect_device
from assistant_mesh.worker import Worker


class DirectDeviceQueryTest(TestCase):
    def test_entrance_turn_uses_the_target_command_without_creating_work(self):
        worker = Worker({'device_query_peers': {'laptop': '/private/laptop.json'}}, client=Mock())
        worker.current = {'id': 'existing-claw-turn', 'epoch': 1}
        peer = {'role': 'agent_peer', 'node': 'cloud', 'capabilities': ['device.query']}
        with patch('assistant_mesh.worker.private_json', return_value={}), \
                patch('assistant_mesh.worker.Client') as connected, \
                patch('assistant_mesh.device_query.subprocess.check_output', return_value='size used avail\n100 40 58\n'):
            connected.return_value.request.side_effect = lambda route, args: inspect_device('laptop', args, peer)
            response = worker.on_tool({'tool': 'mesh', 'callId': 'original-call', 'arguments': {
                'action': 'device_query', 'arguments': {'peer': 'laptop', 'query': 'disk'}}})
            self.assertTrue(response['success'])
            output = json.loads(response['contentItems'][0]['text'])
            self.assertEqual((FORMAT, 'laptop', 58), (output['format'], output['node_id'], output['value']['available_bytes']))
            connected.return_value.request.assert_called_once_with(ROUTE, {'query': 'disk'})
            worker.client.request.assert_not_called()
        with self.assertRaises(PermissionError):
            inspect_device('laptop', {'query': 'disk'}, dict(peer, capabilities=[]))
        with self.assertRaises(ValueError):
            inspect_device('laptop', {'query': 'disk', 'path': '/tmp'}, peer)
