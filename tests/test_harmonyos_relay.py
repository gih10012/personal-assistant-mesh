"""Same TS client protocol + real Python authority; not ArkTS or phone proof."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.enrollment import document_sha256, result_envelope
from assistant_mesh.enrollment_relay import EnrollmentRelay
from tests.test_enrollment_relay import TOKEN, fixture
from tests import test_harmonyos_node as node_suite

APP = node_suite.APP


class HarmonyRelayTests(unittest.TestCase):
    def test_exact_typescript_wire_accepts_original_authority_and_full_canonical_result(self):
        with tempfile.TemporaryDirectory(prefix='mesh-harmony-relay-wire.') as directory:
            bootstrap, config, path, challenge, owner, owner_path, payload = fixture(Path(directory), payload=b'abc')
            relay = EnrollmentRelay(bootstrap, owner_path, live_task_validator=lambda binding: binding == owner['binding'])
            envelope = {'format': 'mesh-enrollment-relay-request/1', 'binding': owner['binding'],
                        'action': 'challenge', 'arguments': {}}
            admitted = relay.handle(TOKEN, envelope)
            result = result_envelope(challenge, payload * 3)
            reported = relay.handle(TOKEN, dict(envelope, action='result', arguments=result))
            input_value = {'enrollment': config['enrollment'], 'config': {
                'format': 'mesh-enrollment-relay-client/1', 'resource_url': owner['resource_url'],
                'binding': owner['binding']}, 'challenge_response': admitted, 'result': result,
                'result_response': reported, 'now': challenge['created_at']}
            run = subprocess.run([node_suite.HarmonyNodeTests().node(), str(APP / 'tools/relay-fixture.mjs')],
                input=json.dumps(input_value).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(0, run.returncode, run.stderr.decode(errors='replace'))
            observed = json.loads(run.stdout)
            canonical = lambda v: json.dumps(v, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
            self.assertEqual(canonical(owner['binding']), observed['canonical_binding'])
            self.assertEqual(canonical(result), observed['canonical_result'])
            self.assertEqual('acknowledged', observed['result_state'])
            self.assertEqual(document_sha256(challenge), observed['request_sha256'])
            self.assertEqual(challenge['operation_id'], observed['operation_id'])

    def test_native_adapter_preserves_intent_order_and_unknown_observe_only_paths(self):
        source = (APP / 'entry/src/main/ets/platform/NativeServices.ets').read_text()
        run = source.split('async runRelay(', 1)[1].split('\n  }', 1)[0]
        self.assertLess(run.index('beginRelayChallenge(row)'), run.index("this.relayRequest(row, scopedToken, 'challenge'"))
        self.assertLess(run.index('await this.persist();'), run.index("this.relayRequest(row, scopedToken, 'challenge'"))
        self.assertLess(run.index('value.may_execute_original_challenge !== true'), run.index('await this.executeChallenge('))
        self.assertIn("row.challenge_state !== 'not_requested'", run)
        self.assertIn('return this.queryRelayStatus(operationId, scopedToken)', run)
        post = source.split('private async relayResult(', 1)[1].split('\n  }', 1)[0]
        self.assertIn("if (row.result_state !== 'not_submitted') { return this.relayStatus(row, scopedToken); }", post)
        self.assertLess(post.index('beginRelayResult(row'), post.index("this.relayRequest(row, scopedToken, 'result'"))
        self.assertLess(post.index('await this.persist();'), post.index("this.relayRequest(row, scopedToken, 'result'"))
        self.assertIn('requireManualChallengeUnclaimed(this.snapshot.relays, challenge.operation_id)', source)
        transport = source.split('private async relayRequest(', 1)[1].split('\n  }', 1)[0]
        self.assertEqual(1, transport.count('await transport.request('))
        self.assertIn('maxRedirects: 0', transport)
        self.assertIn('usingCache: false', transport)
        self.assertIn('transport.destroy()', transport)
        self.assertNotIn('setInterval(', source)
        snapshot = source.split('export class NodeSnapshot {', 1)[1].split('\n}', 1)[0].lower()
        self.assertNotIn('token', snapshot)
        self.assertNotIn('uri', snapshot)


if __name__ == '__main__':
    unittest.main()
