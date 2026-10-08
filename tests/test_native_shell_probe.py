import json
import tempfile
import unittest
from pathlib import Path

from scripts.probe_native_shell import evidence


class NativeShellProbeTests(unittest.TestCase):
    def check(self, records):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'probe.jsonl'
            path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf8')
            return evidence(path, 'expected-private-digest')

    def item(self, **payload):
        return {'type': 'response_item', 'payload': payload}

    def test_matching_shell_call_and_actual_output(self):
        result = self.check([
            self.item(type='function_call', call_id='native', name='functions.exec',
                      arguments='await tools.exec_command({cmd: "sha256sum -- /private/challenge.bin"})'),
            self.item(type='function_call_output', call_id='native', output='expected-private-digest  challenge.bin')])
        self.assertTrue(result['challenge_seen_in_native_output'])
        self.assertEqual(1, result['shell_challenge_calls'])

    def test_model_claim_or_other_tool_cannot_verify_shell(self):
        for records in ([self.item(type='message', content='expected-private-digest')],
                        [self.item(type='function_call', call_id='read', name='read_file', arguments='sha256sum'),
                         self.item(type='function_call_output', call_id='read', output='expected-private-digest')],
                        [self.item(type='function_call', call_id='shell', name='exec_command', arguments='sha256sum'),
                         self.item(type='function_call_output', call_id='different', output='expected-private-digest')]):
            with self.subTest(records=records):
                self.assertFalse(self.check(records)['challenge_seen_in_native_output'])

    def test_failed_shell_does_not_pass(self):
        result = self.check([self.item(type='custom_tool_call', call_id='shell', name='shell', input='sha256sum'),
                             self.item(type='custom_tool_call_output', call_id='shell', output='host missing')])
        self.assertEqual(1, result['shell_challenge_calls'])
        self.assertFalse(result['challenge_seen_in_native_output'])
