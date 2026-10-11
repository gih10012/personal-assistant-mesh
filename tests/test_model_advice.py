import copy
import json
import os
import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.model_advice import model_advice, advice_summary, main
from assistant_mesh.worker import Worker
from tests.test_model_frontier import candidate, point, snapshot, NOW


class NoNetwork:
    def __init__(self):
        self.calls = []

    def request(self, *args):
        self.calls.append(args)
        raise OSError('private authority error')


class ModelAdviceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.path = self.root / 'evidence.json'
        self.evidence = dict(schema=1, snapshot=snapshot(point()), candidates=[candidate()],
                             requirements={}, policy={})
        self.write()
        self.config = {'model_advice_file': str(self.path)}

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.evidence))
        self.path.chmod(0o600)

    def test_valid_private_route_and_zero_cost_reach_advice_not_dispatch(self):
        before = self.path.read_bytes()
        advice = model_advice(self.config, now=NOW)
        self.assertEqual('a', advice['recommendation'])
        self.assertEqual(0, advice['records'][0]['effective_cost_usd'])
        self.assertFalse(advice['live_dispatch_authorized'])
        self.assertFalse(advice['native_features_restricted'])
        self.assertEqual(before, self.path.read_bytes())

    def test_no_config_does_not_create_files_or_block_native(self):
        self.assertEqual('not_configured', model_advice({})['status'])
        self.assertEqual(['evidence.json'], [p.name for p in self.root.iterdir()])

    def test_unknown_operation_holds_original_no_model_or_node_fallback(self):
        advice = model_advice(self.config, {'operation_state': 'unknown'}, now=NOW)
        self.assertEqual('hold_original_operation', advice['status'])
        self.assertIsNone(advice['recommendation'])
        self.assertFalse(advice['operation_replayed'])
        self.assertFalse(advice['automatic_fallback_started'])

    def test_task_cannot_supply_catalog_auth_or_override_evidence(self):
        for key in ('snapshot', 'candidates', 'policy', 'auth', 'model', 'endpoint'):
            self.assertEqual('invalid_requirements',
                             model_advice(self.config, {key: 'private'}, now=NOW)['status'])

    def test_expired_route_does_not_become_free_available_from_benchmark(self):
        self.evidence['candidates'][0]['availability']['expires_at'] = NOW
        self.write()
        advice = model_advice(self.config, now=NOW)
        self.assertIsNone(advice['recommendation'])
        self.assertIn('route_not_currently_available', advice['records'][0]['reasons'])

    def test_owner_private_file_missing_public_symlink_and_oversized_fail_safe(self):
        for kind in ('missing', 'public', 'symlink', 'oversized'):
            with self.subTest(kind=kind):
                target = self.root / ('bad-' + kind)
                if kind == 'public':
                    target.write_bytes(self.path.read_bytes())
                    target.chmod(0o644)
                elif kind == 'symlink':
                    target.symlink_to(self.path)
                elif kind == 'oversized':
                    target.write_bytes(b'x' * (1024 * 1024 + 1))
                    target.chmod(0o600)
                advice = model_advice({'model_advice_file': str(target)}, now=NOW)
                self.assertEqual('evidence_unavailable_or_invalid', advice['status'])
                self.assertNotIn(str(target), json.dumps(advice))
                self.assertFalse(advice['native_features_restricted'])

    def test_duplicate_keys_and_unbounded_list_rejected(self):
        self.path.write_text('{"schema":1,"schema":2}')
        self.assertEqual('evidence_unavailable_or_invalid', model_advice(self.config)['status'])
        self.evidence['candidates'] = [candidate()] * 257
        self.write()
        self.assertEqual('evidence_unavailable_or_invalid', model_advice(self.config)['status'])

    def test_task_requirement_does_not_change_installed_evidence(self):
        old = copy.deepcopy(self.evidence)
        advice = model_advice(self.config, {'min_intelligence_score': 99}, now=NOW)
        self.assertIsNone(advice['recommendation'])
        self.assertEqual(old, json.loads(self.path.read_text()))

    def test_worker_local_tool_needs_no_heartbeat_and_does_not_change_backend(self):
        client = NoNetwork()
        worker = Worker(self.config, client=client)
        with patch('assistant_mesh.model_advice.time.time', return_value=NOW):
            output = worker.on_tool({'tool': 'mesh', 'arguments': {
                'action': 'model_advice', 'arguments': {'operation_state': 'unknown'}}})
        self.assertIn('hold_original_operation', json.dumps(output))
        self.assertEqual([], client.calls)
        self.assertIsNone(worker.current)
        self.assertIsNone(worker.agent)

    def test_worker_reference_contains_real_read_only_advice_not_private_path(self):
        worker = Worker(self.config, client=NoNetwork())
        with patch('assistant_mesh.model_advice.time.time', return_value=NOW):
            reference = worker.resource_reference()
        self.assertEqual('a', reference['model_advice']['snapshot']['recommendation'])
        self.assertNotIn(str(self.path), json.dumps(reference))
        self.assertEqual('unavailable', reference['discovery_status'])

    def test_summary_is_bounded_and_does_not_claim_complete_hash_of_projection(self):
        output = dict(records=[dict(candidate_id=str(i)) for i in range(20)],
                      owner_scenario_frontier_candidate_ids=['19'], recommendation='19')
        projected = advice_summary(output)
        self.assertEqual('19', projected['records'][0]['candidate_id'])
        self.assertEqual(8, len(projected['records']))
        self.assertEqual(20, projected['records_total'])
        self.assertTrue(projected['records_truncated'])
        self.assertIn('unprojected', projected['decision_sha256_scope'])

    def test_cli_uses_same_advice_no_credentials_client_or_config_changes(self):
        before = self.path.read_bytes()
        with patch('assistant_mesh.model_advice.time.time', return_value=NOW), patch('sys.stdout', new_callable=io.StringIO) as out:
            code = main(['--evidence-file', str(self.path)])
        self.assertEqual(0, code)
        self.assertEqual('a', json.loads(out.getvalue())['recommendation'])
        self.assertEqual(before, self.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
