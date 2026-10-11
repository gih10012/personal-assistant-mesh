"""Synthetic model evidence only; no current score claims, keys or paid calls."""
import copy
import hashlib
import json
import unittest

from assistant_mesh.model_frontier import advise_models


NOW = 1000


def pair(model='model-a', effort='max', provider='creator', endpoint='first-party'):
    return dict(provider=provider, endpoint_id=endpoint, model=model,
                reasoning_effort=effort, reasoning_mode='standard', fallback_mode='disabled')


def live(**values):
    result = dict(verified=True, evidence_ref='owner-observer:fixture', observed_at=990, expires_at=1100)
    result.update(values)
    return result


def point(identity='a', score=40, cost=1, identity_pair=None):
    return dict(point_id=identity, pair=identity_pair or pair('model-' + identity),
                evaluated=True, intelligence_score=score, reference_cost_per_task_usd=cost,
                methodology_version='fixture-v1', comparison_set='fixture-common-suite',
                cost_basis='weighted-benchmark-usd', evidence_ref='primary:fixture-' + identity)


def snapshot(*points):
    return dict(schema=1, snapshot_id='synthetic-1', source_url='https://example.invalid/benchmark',
                methodology_version='fixture-v1', comparison_set='fixture-common-suite',
                cost_basis='weighted-benchmark-usd', evidence_ref='primary:complete-fixture',
                observed_at=990, points=list(points or (point(),)))


def candidate(identity='a', price=0, identity_pair=None, latency=5, policy='existing_subscription'):
    return dict(candidate_id=identity, benchmark_point_id=identity,
                pair=identity_pair or pair('model-' + identity),
                availability=live(available=True), capabilities=['text', 'tools'], context_tokens=10000,
                privacy=live(allowed_scopes=['public_synthetic']), latency=live(seconds=latency),
                cost=live(effective_cost_usd=price, policy=policy, authorized=True,
                          quota_available=True, paid_fallback=False, auto_reload=False))


class ModelFrontierTests(unittest.TestCase):
    def advise(self, points=None, candidates=None, requirements=None, policy=None):
        return advise_models(snapshot(*(points or [point()])), candidates or [candidate()],
                             requirements, policy, now=NOW)

    def reasons(self, result, index=0):
        return result['records'][index]['reasons']

    def test_pure_deterministic_digest_and_no_dispatch_or_native_restrictions(self):
        source, rows = snapshot(point()), [candidate()]
        previous = copy.deepcopy((source, rows))
        result = advise_models(source, rows, now=NOW)
        self.assertEqual(previous, (source, rows))
        self.assertEqual(result, advise_models(source, rows, now=NOW))
        digest = result.pop('decision_sha256')
        self.assertEqual(digest, hashlib.sha256(json.dumps(result, sort_keys=True,
            separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest())
        for key in ('live_dispatch_authorized', 'native_features_restricted',
                    'operation_replayed', 'automatic_fallback_started'):
            self.assertIs(result[key], False)

    def test_same_capability_free_route_reprices_actual_x_to_exact_zero(self):
        p = point(cost=12)
        c = candidate(identity_pair=pair('model-a', provider='free-provider', endpoint='free-route'), policy='free_api')
        c['equivalence'] = {'state': 'assumed', 'evidence_ref': 'owner:same-physical-model-observation'}
        result = self.advise([p], [c])
        row = result['records'][0]
        self.assertEqual('a', result['recommendation'])
        self.assertEqual(0, row['effective_cost_usd'])
        self.assertEqual(12, row['reference_cost_per_task_usd'])
        self.assertEqual('assumed_same_variant_route', row['quality_equivalence'])
        self.assertEqual('owner_incremental_usd', result['cost_scenario'])
        self.assertIn('quality_route_equivalence_assumed', result['warnings'])

    def test_verified_route_equivalence_preserved_not_overclaimed(self):
        c = candidate(identity_pair=pair('model-a', endpoint='other'))
        c['equivalence'] = {'state': 'verified', 'evidence_ref': 'observer:actual-equivalence'}
        self.assertEqual('verified_same_variant_route', self.advise(candidates=[c])['records'][0]['quality_equivalence'])

    def test_different_effort_mode_model_or_fallback_never_inherits_quality(self):
        for field in ('model', 'reasoning_effort', 'reasoning_mode', 'fallback_mode'):
            c = candidate()
            c['pair'][field] = 'different'
            c['equivalence'] = {'state': 'verified', 'evidence_ref': 'fixture:not-an-override'}
            self.assertIn('physical_variant_or_route_equivalence_unknown', self.reasons(self.advise(candidates=[c])))

    def test_route_identity_mismatch_requires_explicit_equivalence(self):
        c = candidate(identity_pair=pair('model-a', endpoint='another'))
        result = self.advise(candidates=[c])
        self.assertIsNone(result['recommendation'])
        self.assertIn('physical_variant_or_route_equivalence_unknown', self.reasons(result))

    def test_unevaluated_missing_score_or_incomparable_points_not_qualified(self):
        for changes in ({'evaluated': False}, {'intelligence_score': None},
                        {'reference_cost_per_task_usd': None}, {'methodology_version': 'v2'},
                        {'comparison_set': 'different'}, {'cost_basis': 'blended-token-price'}):
            p = point()
            p.update(changes)
            result = self.advise([p])
            self.assertIsNone(result['recommendation'])
            self.assertIn('benchmark_unknown_or_incomparable', self.reasons(result))

    def test_stale_benchmark_is_explicit_usable_last_known_advice(self):
        source = snapshot(point())
        source['observed_at'] = 0
        result = advise_models(source, [candidate()], policy={'benchmark_max_age_seconds': 100}, now=NOW)
        self.assertEqual('a', result['recommendation'])
        self.assertEqual('stale_last_known', result['source']['confidence'])
        self.assertEqual('stale_last_known', result['records'][0]['benchmark_confidence'])

    def test_owner_can_require_fresh_benchmark_without_native_gate(self):
        source = snapshot(point())
        source['observed_at'] = 0
        result = advise_models(source, [candidate()], policy={'benchmark_max_age_seconds': 100,
                              'allow_stale_benchmark': False}, now=NOW)
        self.assertIn('stale_benchmark_not_enabled', self.reasons(result))
        self.assertFalse(result['native_features_restricted'])

    def test_exact_age_boundary_stale_and_future_unknown(self):
        for sampled, confidence in ((900, 'stale_last_known'), (1001, 'unknown'), (None, 'unknown')):
            source = snapshot(point())
            source['observed_at'] = sampled
            result = advise_models(source, [candidate()], policy={'benchmark_max_age_seconds': 100}, now=NOW)
            self.assertEqual(confidence, result['source']['confidence'])
            if confidence == 'unknown':
                self.assertIsNone(result['recommendation'])

    def test_subscription_not_assumed_free_api_and_paid_route_needs_budget(self):
        c = candidate(price=0.5, policy='paid_api')
        result = self.advise(candidates=[c])
        self.assertIn('owner_incremental_budget_exceeded', self.reasons(result))
        self.assertEqual('a', self.advise(candidates=[c], requirements={'max_incremental_spend_usd': 0.5})['recommendation'])

    def test_actual_free_price_not_advertised_reference_price_drives_frontier(self):
        result = self.advise([point('a', 50, 1), point('b', 45, 2)],
            [candidate('a', 0.2, policy='paid_api'), candidate('b', 0, policy='free_api')],
            {'max_incremental_spend_usd': 1})
        self.assertEqual(['a'], result['published_reference_frontier_point_ids'])
        self.assertEqual(['b', 'a'], result['owner_scenario_frontier_candidate_ids'])
        self.assertEqual('b', result['recommendation'])
        self.assertFalse(result['records'][1]['on_published_reference_frontier'])
        self.assertEqual(['a'], result['records'][1]['published_reference_dominated_by'])
        self.assertIn('owner_scenario_differs_from_published_reference_frontier', result['warnings'])

    def test_dominated_actual_route_rejected_with_comparator_provenance(self):
        result = self.advise([point('a', 50), point('b', 40)], [candidate('a'), candidate('b')])
        self.assertEqual(['a'], result['owner_scenario_frontier_candidate_ids'])
        self.assertEqual(['a'], result['records'][1]['dominated_by'])
        self.assertIn('below_owner_scenario_frontier', self.reasons(result, 1))

    def test_inaccessible_points_not_repriced_as_available_and_constraint_recorded(self):
        a = candidate('a')
        a['availability']['available'] = False
        result = self.advise([point('a', 50, 1), point('b', 40, 2)], [a, candidate('b')])
        self.assertEqual('b', result['recommendation'])
        self.assertIn('route_not_currently_available', self.reasons(result))
        self.assertFalse(result['records'][1]['on_published_reference_frontier'])

    def test_latency_constraint_allows_fast_lower_scoring_model_for_task(self):
        result = self.advise([point('a', 50), point('b', 40)],
            [candidate('a', latency=30), candidate('b', latency=5)], {'max_latency_seconds': 10})
        self.assertEqual('b', result['recommendation'])
        self.assertIn('task_latency_requirement_not_met', self.reasons(result))

    def test_optional_latency_axis_preserves_real_tradeoff_even_at_zero_cash(self):
        result = self.advise([point('a', 50), point('b', 40)],
            [candidate('a', latency=30), candidate('b', latency=5)], {'prefer': 'lowest_latency'},
            {'resource_axes': ['effective_cost_usd', 'latency_seconds']})
        self.assertEqual(['b', 'a'], result['owner_scenario_frontier_candidate_ids'])
        self.assertEqual('b', result['recommendation'])

    def test_quota_axis_preserves_effort_tradeoff_and_units_not_merged(self):
        rows = [candidate('a'), candidate('b')]
        for c, units in zip(rows, (10, 1)):
            c['cost'].update(quota_units=units, quota_unit='account-credit')
        result = self.advise([point('a', 50), point('b', 40)], rows,
            policy={'resource_axes': ['effective_cost_usd', 'quota_units']})
        self.assertEqual(2, len(result['owner_scenario_frontier_candidate_ids']))
        rows[1]['cost']['quota_unit'] = 'different-account-budget'
        self.assertEqual(2, len(self.advise([point('a', 50), point('b', 40)], rows,
            policy={'resource_axes': ['effective_cost_usd', 'quota_units']})['owner_scenario_frontier_candidate_ids']))

    def test_unknown_requested_resource_metric_not_zero(self):
        c = candidate()
        del c['latency']
        result = self.advise(candidates=[c], policy={'resource_axes': ['effective_cost_usd', 'latency_seconds']})
        self.assertIn('requested_resource_metric_unknown', self.reasons(result))

    def test_green_region_is_owner_supplied_preference_not_frozen_cut(self):
        result = self.advise([point('a', 45), point('b', 60)],
            [candidate('a'), candidate('b', 0.5, policy='paid_api')],
            {'max_incremental_spend_usd': 1, 'prefer': 'highest_quality'},
            {'green_region': {'max_effective_cost_usd': 0.1, 'min_intelligence_score': 40,
                              'evidence_ref': 'owner:current-green-preference'}})
        self.assertEqual('a', result['recommendation'])
        self.assertEqual(2, len(result['owner_scenario_frontier_candidate_ids']))
        self.assertTrue(result['records'][0]['green_preferred'])

    def test_quality_floor_can_require_outside_green_without_silent_downgrade(self):
        result = self.advise([point('a', 45), point('b', 60)],
            [candidate('a'), candidate('b', 0.5, policy='paid_api')],
            {'max_incremental_spend_usd': 1, 'min_intelligence_score': 55},
            {'green_region': {'max_effective_cost_usd': 0.1, 'min_intelligence_score': 40,
                              'evidence_ref': 'owner:fixture'}})
        self.assertEqual('b', result['recommendation'])
        self.assertIn('task_quality_floor_not_met', self.reasons(result))

    def test_no_viable_quality_route_does_not_lower_requirement_or_call_model(self):
        result = self.advise(requirements={'min_intelligence_score': 90})
        self.assertEqual('no_qualified_available_route', result['status'])
        self.assertIsNone(result['recommendation'])
        self.assertFalse(result['automatic_fallback_started'])
        self.assertIn('task_requirements_not_relaxed', result['warnings'])

    def test_unknown_inflight_and_completed_operations_hold_original_identity(self):
        for state in ('unknown', 'inflight', 'started', 'completed', 'selection_unknown'):
            result = self.advise(requirements={'operation_state': state})
            self.assertEqual('hold_original_operation', result['status'])
            self.assertIsNone(result['recommendation'])
            self.assertFalse(result['operation_replayed'])
        self.assertEqual('a', self.advise(requirements={'operation_state': 'confirmed_no_effect'})['recommendation'])

    def test_current_cost_authority_quota_privacy_each_independent(self):
        for field, value in (('verified', False), ('authorized', False), ('quota_available', False),
                             ('paid_fallback', True), ('auto_reload', True), ('expires_at', NOW),
                             ('observed_at', NOW + 1), ('effective_cost_usd', None)):
            c = candidate()
            c['cost'][field] = value
            self.assertIn('effective_cost_or_authority_unknown', self.reasons(self.advise(candidates=[c])))
        c = candidate()
        c['cost']['policy'] = 'free_api'
        c['cost']['effective_cost_usd'] = 1
        self.assertIn('free_api_not_zero_incremental_cost', self.reasons(self.advise(candidates=[c])))

    def test_private_history_not_authorized_by_public_synthetic_trial(self):
        result = self.advise(requirements={'data_scope': 'owner_private'})
        self.assertIn('task_data_scope_not_authorized', self.reasons(result))
        c = candidate()
        c['privacy']['allowed_scopes'].append('owner_private')
        self.assertEqual('a', self.advise(candidates=[c], requirements={'data_scope': 'owner_private'})['recommendation'])

    def test_capability_context_and_specific_quality_requirements(self):
        for requirements, reason in (({'required_capabilities': ['vision']}, 'task_capability_missing'),
                ({'min_context_tokens': 20000}, 'task_context_requirement_not_met'),
                ({'min_task_quality': 0.8}, 'task_specific_quality_not_verified')):
            self.assertIn(reason, self.reasons(self.advise(requirements=requirements)))
        c = candidate()
        c['task_quality'] = live(score=0.9)
        self.assertEqual('a', self.advise(candidates=[c], requirements={'min_task_quality': 0.8})['recommendation'])

    def test_selection_valid_until_is_earliest_current_evidence_expiry(self):
        c = candidate()
        c['latency']['expires_at'] = 1010
        self.assertEqual(1010, self.advise(candidates=[c])['records'][0]['valid_until'])

    def test_equal_points_are_not_strictly_dominated(self):
        result = self.advise([point('a', 40), point('b', 40)], [candidate('a'), candidate('b')])
        self.assertEqual(2, len(result['owner_scenario_frontier_candidate_ids']))

    def test_duplicate_and_malformed_identity_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate_point'):
            self.advise([point(), point()])
        with self.assertRaisesRegex(ValueError, 'duplicate_candidate'):
            self.advise(candidates=[candidate(), candidate()])
        c = candidate()
        c['pair']['reasoning_effort'] = None
        with self.assertRaisesRegex(ValueError, 'pair_invalid'):
            self.advise(candidates=[c])

    def test_invalid_numbers_types_and_policy_rejected(self):
        for now in (True, float('nan'), float('inf'), -1, None):
            with self.assertRaises(ValueError):
                advise_models(snapshot(), [candidate()], now=now)
        for policy in ({'benchmark_max_age_seconds': 0}, {'resource_axes': []},
                       {'resource_axes': ['latency_seconds']}, {'allow_stale_benchmark': 'yes'},
                       {'green_region': {'max_effective_cost_usd': 1}}):
            with self.assertRaises(ValueError):
                self.advise(policy=policy)
        for request in ({'max_incremental_spend_usd': True}, {'min_intelligence_score': -1},
                        {'prefer': 'fixed-ranking'}, {'required_capabilities': 'tools'},
                        {'max_latency_seconds': float('inf')}):
            with self.assertRaises(ValueError):
                self.advise(requirements=request)
        with self.assertRaises(ValueError):
            advise_models(snapshot(), [candidate()], requirements=[], now=NOW)


if __name__ == '__main__':
    unittest.main()
