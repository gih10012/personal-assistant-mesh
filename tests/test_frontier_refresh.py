"""Risk boundaries and recovery; synthetic fixtures make no current model claims."""
import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.frontier_refresh import (DAY, MAX_SOURCE_BYTES, FrontierRefresh,
    Redirects, configuration, digest, latest_advice, main, metadata_observation, public_url)
from assistant_mesh.frontier_refresh import summarize
from assistant_mesh.model_advice import model_advice, advice_summary
from tests.test_model_frontier import candidate, point, snapshot, NOW


class FrontierRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.now = [NOW]
        self.calls = []
        self.unavailable_sources = set()
        self.interrupt = None
        self.config = dict(schema=1, schedule_id='mesh-frontier-test',
            task_context=dict(goal_ref='original-goal', task_id='PAM-007e', project_id='mesh'),
            state_dir=str(self.root / 'state'), interval_seconds=4 * DAY, retry_seconds=60,
            timeout_seconds=1,
            sources=[dict(id=category, category=category, url='https://source.invalid/' + category,
                          required=True) for category in ('models', 'harness', 'skills', 'protocol')])

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, source, config):
        self.calls.append(source['id'])
        if source['id'] == self.interrupt:
            raise KeyboardInterrupt()
        if source['id'] in self.unavailable_sources:
            raise OSError('secret path and personal credential must not enter diagnostics')
        return json.dumps({'name': source['id'], 'tag_name': 'fixture'}).encode(), {
            'http_status': 200, 'final_url': source['url'], 'content_type': 'application/json'}

    def runtime(self, config=None):
        return FrontierRefresh(config or self.config, fetch=self.fetch, clock=lambda: self.now[0])

    def baseline(self):
        path = self.root / 'baseline.json'
        value = dict(schema=1, snapshot=snapshot(point()), candidates=[candidate()], requirements={}, policy={})
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        self.config['baseline_evidence_file'] = str(path)
        return path, value

    def test_due_is_readonly_then_complete_receipt_has_original_context_and_next_due(self):
        runtime = self.runtime()
        self.assertTrue(runtime.status()['due'])
        self.assertFalse(Path(self.config['state_dir']).exists())
        receipt = runtime.refresh('original-check-v1')
        self.assertEqual('completed', receipt['status'])
        self.assertEqual(self.config['task_context'], receipt['task_context'])
        self.assertEqual(NOW + 4 * DAY, receipt['next_due_at'])
        self.assertEqual(4, len(self.calls))
        self.assertFalse(runtime.status()['due'])
        self.assertEqual(receipt, runtime.receipt('original-check-v1'))
        for flag in ('new_spend_authorized', 'live_dispatch_authorized', 'operation_replayed',
                     'native_features_restricted', 'automatic_upgrade_started', 'original_native_goal_or_plan_mutated'):
            self.assertFalse(receipt[flag])
        self.assertEqual(0, receipt['model_calls'])
        self.assertFalse(receipt['timer_installation_performed'])
        self.assertNotIn('secret path and personal credential', json.dumps(receipt))

    def test_original_id_cached_read_never_refetches_and_changed_request_fails(self):
        first = self.runtime().refresh('original-check-v1')
        self.now[0] += 10 * DAY
        self.assertEqual(first, self.runtime().refresh('original-check-v1'))
        self.assertEqual(4, len(self.calls))
        changed = copy.deepcopy(self.config)
        changed['interval_seconds'] = 5 * DAY
        with self.assertRaisesRegex(ValueError, 'content_conflict'):
            self.runtime(changed).refresh('original-check-v1')
        changed['task_context']['goal_ref'] = 'replacement-goal'
        with self.assertRaisesRegex(ValueError, 'context_conflict'):
            self.runtime(changed).status()

    def test_run_due_stable_schedule_and_only_due_fetches(self):
        first = self.runtime().run_due()
        self.assertEqual('mesh-frontier-test:initial', first['request_id'])
        self.runtime().run_due()
        self.assertEqual(4, len(self.calls))
        self.now[0] = first['next_due_at']
        second = self.runtime().run_due()
        self.assertEqual('mesh-frontier-test:' + str(int(first['next_due_at'])), second['request_id'])
        self.assertEqual(8, len(self.calls))
        self.assertTrue(all(s['content_changed'] is False for s in second['sources']))
        self.assertEqual(first['sources'][0]['sha256'], second['sources'][0]['previous_sha256'])

    def test_partial_source_retry_keeps_original_id_failure_and_success_times(self):
        self.unavailable_sources.add('models')
        first = self.runtime().refresh('original-check-v1')
        self.assertEqual('partial', first['status'])
        self.assertIsNone(first['next_due_at'])
        self.assertEqual('public_source_unavailable', first['sources'][0]['error_category'])
        self.assertEqual('models', first['sources'][0]['category'])
        self.unavailable_sources.clear()
        self.now[0] += 60
        second = self.runtime().run_due()
        self.assertEqual(first['request_id'], second['request_id'])
        self.assertEqual('completed', second['status'])
        self.assertEqual(5, len(self.calls))
        self.assertEqual(NOW, second['sources'][1]['observed_at'])
        with self.runtime().db() as db:
            old = json.loads(db.execute('SELECT value FROM observations WHERE source=\'models\' AND attempt=1').fetchone()[0])
        self.assertEqual('unavailable', old['status'])
        self.assertTrue((self.root / 'state' / 'blobs' / (first['receipt_sha256'] + '.receipt')).exists())

    def test_process_interruption_resumes_same_readonly_intent_not_successful_sources(self):
        self.interrupt = 'skills'
        with self.assertRaises(KeyboardInterrupt):
            self.runtime().refresh('original-check-v1')
        self.assertEqual('collecting', self.runtime().status()['status'])
        self.interrupt = None
        second = self.runtime().run_due()
        self.assertEqual('original-check-v1', second['request_id'])
        self.assertEqual(['models', 'harness', 'skills', 'skills', 'protocol'], self.calls)
        with self.runtime().db() as db:
            old = json.loads(db.execute('SELECT value FROM observations WHERE source=\'skills\' AND attempt=1').fetchone()[0])
        self.assertEqual('fetching', old['status'])  # Original interrupted observation retained.

    def test_baseline_cost_quality_availability_times_never_renew_from_new_docs(self):
        path, value = self.baseline()
        old = path.read_bytes()
        self.runtime().refresh('original-check-v1')
        evidence, metadata = latest_advice(self.config['state_dir'], now=NOW)
        self.assertEqual(value, evidence)
        self.assertEqual(old, path.read_bytes())
        advice = model_advice({'model_refresh_dir': self.config['state_dir']}, now=NOW)
        self.assertEqual('a', advice['recommendation'])
        self.assertEqual(0, advice['records'][0]['effective_cost_usd'])
        self.assertFalse(advice['frontier_refresh']['due'])
        later = model_advice({'model_refresh_dir': self.config['state_dir']}, now=1100)
        self.assertIsNone(later['recommendation'])
        self.assertIn('route_not_currently_available', later['records'][0]['reasons'])

    def test_unknown_operation_and_stale_update_never_dispatch(self):
        self.baseline()
        self.runtime().refresh('original-check-v1')
        advice = model_advice({'model_refresh_dir': self.config['state_dir']},
                             {'operation_state': 'unknown'}, now=NOW + 5 * DAY)
        self.assertEqual('hold_original_operation', advice['status'])
        self.assertIsNone(advice['recommendation'])
        self.assertTrue(advice['frontier_refresh']['due'])
        self.assertEqual(0, sum(s['fresh'] for s in advice['frontier_refresh']['sources']))
        self.assertFalse(advice['automatic_fallback_started'])
        self.assertEqual(4, len(self.calls))

    def test_opaque_current_catalog_keeps_real_efforts_no_identity_or_auth_projection(self):
        catalog = self.root / 'models_cache.json'
        catalog.write_text(json.dumps({'identity': 'private-owner', 'fetched_at': 'fixture-time',
            'client_version': 'fixture', 'models': [{'slug': 'new-dynamic-model',
            'supported_reasoning_levels': [{'effort': 'changing-effort'}], 'base_instructions': 'private prompt',
            'env_key': 'SECRET_KEY'}]}))
        catalog.chmod(0o644)
        self.config['local_metadata'] = [dict(id='owner-route', kind='codex_catalog',
                                             path=str(catalog), cash_policy='existing_subscription')]
        receipt = self.runtime().refresh('original-check-v1')
        text = json.dumps(receipt)
        self.assertNotIn('private-owner', text)
        self.assertNotIn('private prompt', text)
        self.assertNotIn('SECRET_KEY', text)
        self.assertNotIn(str(catalog), text)
        advice = model_advice({'model_refresh_dir': self.config['state_dir']}, now=NOW)
        self.assertEqual('new-dynamic-model', advice['records'][0]['pair']['model'])
        self.assertEqual('changing-effort', advice['records'][0]['pair']['reasoning_effort'])
        self.assertIn('benchmark_unknown_or_incomparable', advice['records'][0]['reasons'])
        self.assertIn('effective_cost_or_authority_unknown', advice['records'][0]['reasons'])
        self.assertIsNone(advice['recommendation'])

    def test_owned_nonsecret_config_reads_only_model_scalars_never_executes_wrapper(self):
        path = self.root / 'config.toml'
        path.write_text('model = "changing-model"\nmodel_reasoning_effort = "new-effort"\n'
                        'env_key = "PRIVATE_KEY"\nbase_url = "secret-endpoint"\n')
        result = metadata_observation(dict(id='native-config', kind='codex_config', path=str(path)), NOW)
        self.assertEqual({'model': 'changing-model', 'model_reasoning_effort': 'new-effort'}, result['configured'])
        self.assertNotIn('PRIVATE_KEY', json.dumps(result))
        with self.assertRaisesRegex(ValueError, 'credential'):
            bad = copy.deepcopy(self.config)
            bad['local_metadata'] = [dict(id='auth', kind='file_digest', path=str(self.root / 'auth.json'))]
            configuration(bad)

    def test_source_size_and_unsafe_redirect_never_publish_a_complete_result(self):
        runtime = self.runtime()
        runtime.fetch = lambda source, cfg: (b'x' * (MAX_SOURCE_BYTES + 1), {})
        receipt = runtime.refresh('original-check-v1')
        self.assertEqual('partial', receipt['status'])
        self.assertTrue(all(s['status'] == 'unavailable' for s in receipt['sources']))
        for url in ('http://example.org/', 'https://127.0.0.1/', 'https://foo:secret@example.org/',
                    'https://example.org/?token=secret', 'https://example.org/#secret'):
            with self.assertRaises(ValueError):
                public_url(url)
        with self.assertRaises(ValueError):
            Redirects({'source.invalid'}).redirect_request(None, None, 302, '', {}, 'https://other.invalid/')

    def test_actual_document_projection_does_not_fill_with_navigation_or_execute_html(self):
        data = b'<!doctype html><title>Reference</title>' + b'<h2>Navigation</h2>' * 20 + b'<h1>Changelog</h1><h2>Current month</h2><h3>Current change</h3><script>fake()</script>'
        result = summarize(data)
        self.assertEqual(['Reference', 'Changelog', 'Current month', 'Current change'], result['headings'])

    def test_advice_hash_scope_summary_and_corrupt_blob_fixed_safe_failure(self):
        self.baseline()
        receipt = self.runtime().refresh('original-check-v1')
        advice = model_advice({'model_refresh_dir': self.config['state_dir']}, now=NOW)
        expected = advice.pop('advice_sha256')
        self.assertEqual(expected, digest(advice))
        summary = advice_summary(advice)
        self.assertEqual(4, summary['frontier_refresh']['source_counts']['fresh'])
        self.assertIn('refresh metadata excluded', summary['decision_sha256_scope'])
        blob = self.root / 'state' / 'blobs' / (receipt['model_evidence_sha256'] + '.json')
        blob.write_bytes(b'{"private":"corruption"}')
        result = model_advice({'model_refresh_dir': self.config['state_dir']}, now=NOW)
        self.assertEqual('evidence_unavailable_or_invalid', result['status'])
        self.assertNotIn('corruption', json.dumps(result))

    def test_missing_refresh_does_not_disable_existing_model_advice_or_native_tools(self):
        path, unused = self.baseline()
        advice = model_advice({'model_advice_file': str(path), 'model_refresh_dir': str(self.root / 'missing')}, now=NOW)
        self.assertEqual('a', advice['recommendation'])
        self.assertEqual('unavailable', advice['frontier_refresh']['status'])
        self.assertFalse(advice['native_features_restricted'])

    def test_private_state_symlink_or_file_modes_rejected(self):
        self.runtime().refresh('original-check-v1')
        database = self.root / 'state' / 'frontier.sqlite'
        self.assertEqual(0o600, database.stat().st_mode & 0o777)
        link = self.root / 'alias'
        link.symlink_to(self.root / 'state')
        with self.assertRaises(ValueError):
            latest_advice(str(link), now=NOW)
        database.chmod(0o644)
        with self.assertRaises(ValueError):
            self.runtime().status()

    def test_local_process_lock_observes_existing_refresh_no_second_job(self):
        import fcntl
        self.runtime().refresh('original-check-v1')
        fd = os.open(str(self.root / 'state' / 'refresh.lock'), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            output = self.runtime().refresh('different-explicit-readonly-check')
            self.assertEqual('local_refresh_running', output['status'])
            self.assertEqual(4, len(self.calls))
        finally:
            os.close(fd)

    def test_three_to_five_day_contract_and_complete_category_coverage(self):
        for duration in (2 * DAY, 6 * DAY, False, float('inf')):
            bad = dict(self.config, interval_seconds=duration)
            with self.assertRaises(ValueError):
                configuration(bad)
        bad = copy.deepcopy(self.config)
        bad['sources'] = bad['sources'][:3]
        with self.assertRaisesRegex(ValueError, 'four_source_categories'):
            configuration(bad)

    def test_cli_status_safe_private_config_and_receipt_no_network(self):
        config = self.root / 'owner.json'
        config.write_text(json.dumps(self.config))
        config.chmod(0o600)
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            code = main(['--config', str(config), 'status'])
        self.assertEqual(0, code)
        self.assertEqual('not_started', json.loads(output.getvalue())['status'])
        config.chmod(0o644)
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            code = main(['--config', str(config), 'status'])
        self.assertEqual(1, code)
        self.assertEqual('frontier_unavailable_or_invalid', json.loads(output.getvalue())['status'])


if __name__ == '__main__':
    unittest.main()
