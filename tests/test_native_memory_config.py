"""Process configuration fixtures, not native memory-generation acceptance."""
import unittest
from unittest.mock import MagicMock, patch

from assistant_mesh.codex import Codex


class NativeMemoryConfigurationTests(unittest.TestCase):
    def command(self, configuration):
        child = MagicMock()
        child.stdout = []
        with patch('assistant_mesh.codex.discover_codex_auth', return_value='/private/fixture-profile'), \
                patch('assistant_mesh.codex.subprocess.Popen', return_value=child) as launch, \
                patch.object(Codex, 'rpc', return_value={}), patch.object(Codex, 'send'):
            Codex(configuration)
        return launch.call_args[0][0]

    def test_default_keeps_native_memories_enabled(self):
        command = self.command({})
        for setting in ('features.memories=true', 'memories.generate_memories=true', 'memories.use_memories=true'):
            self.assertIn(setting, command)

    def test_explicit_optout_overrides_profile_config_only_for_selected_child(self):
        command = self.command({'native_memories': False})
        for setting in ('features.memories=false', 'memories.generate_memories=false', 'memories.use_memories=false'):
            self.assertIn(setting, command)
        self.assertEqual(['app-server', '--stdio'], command[-2:])
        self.assertFalse(any('sandbox' in setting for setting in command))

    def test_nonboolean_is_rejected_without_launching_native_runtime(self):
        for value in (None, 0, 1, 'false', []):
            with self.subTest(value=value), patch('assistant_mesh.codex.subprocess.Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'invalid_native_memories_flag'):
                    Codex({'native_memories': value})
                launch.assert_not_called()

    def test_native_plan_tool_enabled_in_child_without_setting_a_goal_or_permission(self):
        command = self.command({})
        self.assertIn('tools.update_plan.enabled=true', command)
        self.assertFalse(any('goal' in setting or 'sandbox' in setting or 'approval' in setting for setting in command))

    def test_explicit_native_plan_optout_applies_to_child_only(self):
        command = self.command({'native_plan_tool': False})
        self.assertIn('tools.update_plan.enabled=false', command)
        self.assertNotIn('tools.update_plan.enabled=true', command)
        self.assertEqual(['app-server', '--stdio'], command[-2:])

    def test_nonboolean_native_plan_flag_is_rejected_before_auth_discovery_or_spawn(self):
        for value in (None, 0, 1, 'false', []):
            with self.subTest(value=value), patch('assistant_mesh.codex.discover_codex_auth') as discover, \
                    patch('assistant_mesh.codex.subprocess.Popen') as launch:
                with self.assertRaisesRegex(ValueError, '^invalid_native_plan_tool_flag$'):
                    Codex({'native_plan_tool': value})
                discover.assert_not_called()
                launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
