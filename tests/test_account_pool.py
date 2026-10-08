import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from assistant_mesh.config import discover_codex_auth
from assistant_mesh.codex import CodexError
from assistant_mesh.worker import Worker


class AccountPoolTests(unittest.TestCase):
    def config(self):
        return {'node_id': 'n', 'codex': {'workspace': '/private/workspace'},
                'codex_accounts': [{'auth_home': '/private/main'}, {'auth_home': '/private/other'}]}

    def test_pinned_auth_does_not_silently_select_another_account(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                discover_codex_auth(str(Path(directory) / 'missing'), strict=True)

    def test_startup_auth_failure_selects_next_authorized_account(self):
        first, second = Mock(), Mock()
        first.account.return_value = {'authenticated': False}
        second.account.return_value = {'authenticated': True}
        backend = Mock(side_effect=[first, second])
        worker = Worker(self.config(), client=Mock(), backend=backend)
        worker.current = {'context': {}}
        agent = worker.open_backend()
        self.assertIs(second, agent)
        first.close.assert_called_once()
        self.assertEqual(['/private/main', '/private/other'], [call.args[0]['auth_home'] for call in backend.call_args_list])
        self.assertTrue(all(call.args[0]['strict_auth_home'] for call in backend.call_args_list))

    def test_auth_startup_unavailability_does_not_mutate_parent_environment(self):
        initial = dict(os.environ)
        worker = Worker(self.config(), client=Mock(), backend=Mock(side_effect=CodexError('codex_auth_required')))
        worker.current = {'context': {}}
        with self.assertRaises(CodexError):
            worker.open_backend()
        self.assertEqual(initial, dict(os.environ))

    def test_fallback_never_replays_a_turn_that_already_started(self):
        first, second = Mock(), Mock()
        first.account.return_value = {'authenticated': True}
        first.start.side_effect = CodexError('codex_turn_failed')
        backend = Mock(side_effect=[first, second])
        worker = Worker(self.config(), client=Mock(), backend=backend)
        worker.current = {'context': {}}
        chosen = worker.open_backend()
        with self.assertRaises(CodexError):
            chosen.start('effectful task')
        self.assertEqual(1, backend.call_count)
        second.account.assert_not_called()


if __name__ == '__main__':
    unittest.main()
