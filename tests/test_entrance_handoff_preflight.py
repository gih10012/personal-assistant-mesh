import json
import unittest
from unittest.mock import Mock

from assistant_mesh.entrance_runtime import EntranceWorker


class EntranceHandoffPreflightTest(unittest.TestCase):
    def tool(self, worker):
        return worker.on_tool({'tool': 'mesh', 'callId': 'original-turn', 'arguments': {
            'action': 'entrance', 'arguments': {'action': 'handoff', 'arguments': {'input': 'exact request'}}}})

    def test_expired_or_offline_leader_leaves_original_handoff_unsubmitted(self):
        worker = object.__new__(EntranceWorker)
        worker.current = {'id': 'original-event', 'epoch': 2}
        worker.client, worker.tick = Mock(), Mock()
        status = {'time': 100, 'leader': {'node': 'cloud', 'epoch': 4, 'deadline': 110},
                  'nodes': [{'id': 'cloud', 'online': False, 'capabilities': ['leader']}]}
        worker.client.request.return_value = status
        output = self.tool(worker)
        self.assertFalse(output['success'])
        self.assertEqual({'state': 'waiting_handoff', 'reason': 'leader_worker_unavailable',
                          'handoff_submitted': False}, json.loads(output['contentItems'][0]['text']))
        worker.tick.assert_not_called()
        worker.client.request.assert_called_once_with('/v1/status')

        worker.client.reset_mock()
        status['nodes'][0]['online'] = True
        worker.client.request.side_effect = [status, {'handoff': {'status': 'pending'}}]
        output = self.tool(worker)
        self.assertTrue(output['success'])
        self.assertEqual({'handoff': {'status': 'pending'}}, json.loads(output['contentItems'][0]['text']))
        worker.client.request.assert_called_with('/v1/agent/action', {
            'task_id': 'original-event', 'epoch': 2, 'call_id': 'original-event:original-turn',
            'action': 'entrance_handoff', 'arguments': {'input': 'exact request'}})
