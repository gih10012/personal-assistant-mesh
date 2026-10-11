"""Pure, owner-scenario model advice; no model calls, credentials or native gates.

Published benchmark cost and this owner's effective route cost are different
axes. Inputs are evidence supplied by a trusted caller, not self-attestation
verification. This module neither grants dispatch nor retries an operation.
"""
import hashlib
import json
import math


PAIR_FIELDS = ('provider', 'endpoint_id', 'model', 'reasoning_effort',
               'reasoning_mode', 'fallback_mode')
VARIANT_FIELDS = ('model', 'reasoning_effort', 'reasoning_mode', 'fallback_mode')
MAX_BENCHMARK_AGE = 3 * 24 * 60 * 60


def _number(value, minimum=0):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= minimum
    except OverflowError:
        return False


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512


def _pair(value):
    if not isinstance(value, dict) or any(not _text(value.get(k)) for k in PAIR_FIELDS):
        raise ValueError('model_frontier_pair_invalid')
    return {key: value[key] for key in PAIR_FIELDS}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode('utf8')).hexdigest()


def _live(evidence, now):
    return (isinstance(evidence, dict) and evidence.get('verified') is True
            and _text(evidence.get('evidence_ref'))
            and _number(evidence.get('observed_at'))
            and _number(evidence.get('expires_at'))
            and evidence['observed_at'] <= now < evidence['expires_at'])


def _snapshot(snapshot, now, max_age):
    """One explicit comparable set; different suites cannot be silently pooled."""
    if (not isinstance(snapshot, dict) or snapshot.get('schema') != 1
            or not all(_text(snapshot.get(k)) for k in
                       ('snapshot_id', 'source_url', 'methodology_version',
                        'comparison_set', 'cost_basis', 'evidence_ref'))
            or not snapshot['source_url'].startswith('https://')
            or not isinstance(snapshot.get('points'), list)):
        raise ValueError('model_frontier_snapshot_invalid')
    source = {key: snapshot[key] for key in
              ('snapshot_id', 'source_url', 'methodology_version', 'comparison_set',
               'cost_basis', 'evidence_ref')}
    observed = snapshot.get('observed_at')
    if not _number(observed) or observed > now:
        confidence = 'unknown'
    elif now - observed >= max_age:
        confidence = 'stale_last_known'
    else:
        confidence = 'current_snapshot'
    source.update(observed_at=observed if _number(observed) else None,
                  confidence=confidence, input_sha256=_hash(snapshot))
    points = {}
    for point in snapshot['points']:
        if not isinstance(point, dict) or not _text(point.get('point_id')):
            raise ValueError('model_frontier_point_invalid')
        if point['point_id'] in points:
            raise ValueError('model_frontier_duplicate_point')
        pair = _pair(point.get('pair'))
        comparable = all(point.get(key) == snapshot[key] for key in
                         ('methodology_version', 'comparison_set', 'cost_basis'))
        known = (point.get('evaluated') is True and comparable
                 and _number(point.get('intelligence_score'))
                 and _number(point.get('reference_cost_per_task_usd'))
                 and _text(point.get('evidence_ref')))
        points[point['point_id']] = dict(point, pair=pair, benchmark_known=known)
    return source, points


def _dominates(left, right, axes):
    """Higher quality and no greater declared comparable resource expenditure."""
    if left['intelligence_score'] < right['intelligence_score']:
        return False
    strict = left['intelligence_score'] > right['intelligence_score']
    for axis in axes:
        a, b = left.get(axis), right.get(axis)
        if not _number(a) or not _number(b):
            return False  # Unknown is not zero and is not a dominance proof.
        if axis == 'quota_units' and left.get('quota_unit') != right.get('quota_unit'):
            return False
        if a > b:
            return False
        strict = strict or a < b
    return strict


def advise_models(snapshot, candidates, requirements=None, policy=None, now=None):
    """Return an auditable shortlist for NEW managed work, without dispatching.

    `now` is explicit so an owner observer can bind all evidence to one instant.
    Availability/cost observations must be current; stale benchmark metadata can
    remain last-known advice. Exact zero prices are valid, never epsilon prices.
    """
    requirements = {} if requirements is None else requirements
    policy = {} if policy is None else policy
    if (not _number(now) or not isinstance(candidates, list)
            or not isinstance(requirements, dict) or not isinstance(policy, dict)):
        raise ValueError('model_frontier_request_invalid')
    max_age = policy.get('benchmark_max_age_seconds', MAX_BENCHMARK_AGE)
    budget = requirements.get('max_incremental_spend_usd', 0)
    minimum = requirements.get('min_intelligence_score', 0)
    axes = policy.get('resource_axes', ['effective_cost_usd'])
    if (not _number(max_age) or max_age <= 0 or not _number(budget)
            or not _number(minimum) or not isinstance(axes, list)
            or not axes or axes[0] != 'effective_cost_usd'
            or len(axes) != len(set(axes))
            or any(k not in ('effective_cost_usd', 'latency_seconds', 'quota_units') for k in axes)
            or type(policy.get('allow_stale_benchmark', True)) is not bool):
        raise ValueError('model_frontier_policy_invalid')
    for name in ('max_latency_seconds', 'min_context_tokens', 'min_task_quality'):
        if name in requirements and not _number(requirements[name]):
            raise ValueError('model_frontier_requirements_invalid')
    capabilities = requirements.get('required_capabilities', [])
    if not isinstance(capabilities, list) or any(not _text(k) for k in capabilities):
        raise ValueError('model_frontier_requirements_invalid')
    sort_by = requirements.get('prefer', 'lowest_cost')
    if sort_by not in ('lowest_cost', 'lowest_latency', 'highest_quality'):
        raise ValueError('model_frontier_requirements_invalid')
    data_scope = requirements.get('data_scope', 'public_synthetic')
    if not _text(data_scope):
        raise ValueError('model_frontier_requirements_invalid')
    green = policy.get('green_region')
    if green is not None and (not isinstance(green, dict)
            or not _number(green.get('max_effective_cost_usd'))
            or not _number(green.get('min_intelligence_score'))
            or not _text(green.get('evidence_ref'))):
        raise ValueError('model_frontier_green_region_invalid')
    source, points = _snapshot(snapshot, now, max_age)
    records, seen = [], set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or not _text(candidate.get('candidate_id')):
            raise ValueError('model_frontier_candidate_invalid')
        identity = candidate['candidate_id']
        if identity in seen:
            raise ValueError('model_frontier_duplicate_candidate')
        seen.add(identity)
        pair = _pair(candidate.get('pair'))
        point = points.get(candidate.get('benchmark_point_id'))
        reasons = []
        record = {'candidate_id': identity, 'pair': pair, 'reasons': reasons,
                  'benchmark_point_id': candidate.get('benchmark_point_id'),
                  'benchmark_confidence': source['confidence'],
                  'quality_equivalence': 'unknown', 'eligible': False,
                  'green_preferred': False, 'dominated_by': []}
        if point is None or not point['benchmark_known']:
            reasons.append('benchmark_unknown_or_incomparable')
        else:
            exact = pair == point['pair']
            equivalence = candidate.get('equivalence', {})
            same_variant = all(pair[k] == point['pair'][k] for k in VARIANT_FIELDS)
            if exact:
                record['quality_equivalence'] = 'exact_measured_pair'
            elif (same_variant and isinstance(equivalence, dict)
                  and equivalence.get('state') in ('assumed', 'verified')
                  and _text(equivalence.get('evidence_ref'))):
                record['quality_equivalence'] = equivalence['state'] + '_same_variant_route'
                record['equivalence_evidence_ref'] = equivalence['evidence_ref']
            else:
                reasons.append('physical_variant_or_route_equivalence_unknown')
            record.update(intelligence_score=point['intelligence_score'],
                          reference_cost_per_task_usd=point['reference_cost_per_task_usd'],
                          benchmark_evidence_ref=point['evidence_ref'])
            if point['intelligence_score'] < minimum:
                reasons.append('task_quality_floor_not_met')
        if source['confidence'] == 'unknown':
            reasons.append('benchmark_time_unknown')
        elif source['confidence'] == 'stale_last_known' and not policy.get('allow_stale_benchmark', True):
            reasons.append('stale_benchmark_not_enabled')
        availability = candidate.get('availability')
        if not _live(availability, now) or availability.get('available') is not True:
            reasons.append('route_not_currently_available')
        else:
            record['availability_evidence_ref'] = availability['evidence_ref']
            record['valid_until'] = availability['expires_at']
        declared_caps = candidate.get('capabilities')
        if (not isinstance(declared_caps, list)
                or any(not _text(k) for k in declared_caps)
                or not set(capabilities).issubset(set(declared_caps))):
            reasons.append('task_capability_missing')
        if ('min_context_tokens' in requirements
                and (not _number(candidate.get('context_tokens'))
                     or candidate['context_tokens'] < requirements['min_context_tokens'])):
            reasons.append('task_context_requirement_not_met')
        privacy = candidate.get('privacy')
        if (not _live(privacy, now) or not isinstance(privacy.get('allowed_scopes'), list)
                or data_scope not in privacy['allowed_scopes']):
            reasons.append('task_data_scope_not_authorized')
        else:
            record['privacy_evidence_ref'] = privacy['evidence_ref']
            record['valid_until'] = min(record.get('valid_until', privacy['expires_at']), privacy['expires_at'])
        latency = candidate.get('latency')
        if _live(latency, now) and _number(latency.get('seconds')):
            record['latency_seconds'] = latency['seconds']
            record['latency_evidence_ref'] = latency['evidence_ref']
            record['valid_until'] = min(record.get('valid_until', latency['expires_at']), latency['expires_at'])
        if ('max_latency_seconds' in requirements
                and (not _number(record.get('latency_seconds'))
                     or record['latency_seconds'] > requirements['max_latency_seconds'])):
            reasons.append('task_latency_requirement_not_met')
        quality = candidate.get('task_quality')
        if 'min_task_quality' in requirements and (not _live(quality, now)
                or not _number(quality.get('score'))
                or quality['score'] < requirements['min_task_quality']):
            reasons.append('task_specific_quality_not_verified')
        elif _live(quality, now) and _number(quality.get('score')):
            record['task_quality_evidence_ref'] = quality['evidence_ref']
            record['valid_until'] = min(record.get('valid_until', quality['expires_at']), quality['expires_at'])
        cost = candidate.get('cost')
        if (not _live(cost, now) or not _number(cost.get('effective_cost_usd'))
                or cost.get('policy') not in ('existing_subscription', 'local', 'free_api', 'paid_api')
                or cost.get('authorized') is not True or cost.get('quota_available') is not True
                or cost.get('paid_fallback') is not False
                or cost.get('auto_reload') is not False):
            reasons.append('effective_cost_or_authority_unknown')
        else:
            record.update(effective_cost_usd=cost['effective_cost_usd'],
                          cost_policy=cost['policy'], cost_evidence_ref=cost['evidence_ref'])
            record['valid_until'] = min(record.get('valid_until', cost['expires_at']), cost['expires_at'])
            if cost['policy'] == 'free_api' and cost['effective_cost_usd'] != 0:
                reasons.append('free_api_not_zero_incremental_cost')
            if cost['effective_cost_usd'] > budget:
                reasons.append('owner_incremental_budget_exceeded')
            if _number(cost.get('quota_units')) and _text(cost.get('quota_unit')):
                record.update(quota_units=cost['quota_units'], quota_unit=cost['quota_unit'])
        if any(not _number(record.get(axis)) for axis in axes):
            reasons.append('requested_resource_metric_unknown')
        if not reasons:
            record['eligible'] = True
            if green:
                record['green_preferred'] = (record['effective_cost_usd'] <= green['max_effective_cost_usd']
                    and record['intelligence_score'] >= green['min_intelligence_score'])
        records.append(record)

    # Reference plot describes the published set, including inaccessible points.
    reference = []
    for point in points.values():
        if point['benchmark_known']:
            reference.append({'point_id': point['point_id'], 'intelligence_score': point['intelligence_score'],
                              'effective_cost_usd': point['reference_cost_per_task_usd']})
    reference_frontier = sorted(p['point_id'] for p in reference if not any(
        _dominates(other, p, ['effective_cost_usd']) for other in reference))
    for row in records:
        row['on_published_reference_frontier'] = row['benchmark_point_id'] in reference_frontier
        original = next((p for p in reference if p['point_id'] == row['benchmark_point_id']), None)
        row['published_reference_dominated_by'] = [] if original is None else sorted(
            p['point_id'] for p in reference if _dominates(p, original, ['effective_cost_usd']))
    # The owner's actual plot compares task-feasible routes at their actual cost.
    viable = [row for row in records if row['eligible']]
    for row in viable:
        row['dominated_by'] = sorted(other['candidate_id'] for other in viable
                                      if _dominates(other, row, axes))
        if row['dominated_by']:
            row['eligible'] = False
            row['reasons'].append('below_owner_scenario_frontier')
    shortlist = [row for row in records if row['eligible']]
    def rank(row):
        latency = row.get('latency_seconds', float('inf'))
        ordering = {'lowest_cost': (row['effective_cost_usd'], -row['intelligence_score'], latency),
                    'lowest_latency': (latency, row['effective_cost_usd'], -row['intelligence_score']),
                    'highest_quality': (-row['intelligence_score'], row['effective_cost_usd'], latency)}[sort_by]
        return (not row['green_preferred'],) + ordering + (row['candidate_id'],)
    shortlist.sort(key=rank)
    operation_state = requirements.get('operation_state', 'new')
    held = operation_state not in ('new', 'confirmed_no_effect')
    recommendation = None if held or not shortlist else shortlist[0]
    warnings = []
    if source['confidence'] != 'current_snapshot':
        warnings.append('benchmark_' + source['confidence'])
    if recommendation and recommendation['quality_equivalence'] == 'assumed_same_variant_route':
        warnings.append('quality_route_equivalence_assumed')
    if recommendation and not recommendation['on_published_reference_frontier']:
        warnings.append('owner_scenario_differs_from_published_reference_frontier')
    if not shortlist:
        warnings.append('task_requirements_not_relaxed')
    output = {'schema': 1, 'observed_at': now, 'source': source,
              'cost_scenario': 'owner_incremental_usd', 'resource_axes': list(axes),
              'published_reference_frontier_point_ids': reference_frontier,
              'owner_scenario_frontier_candidate_ids': [row['candidate_id'] for row in shortlist],
              'records': records,
              'recommendation': recommendation['candidate_id'] if recommendation else None,
              'warnings': warnings,
              'status': 'hold_original_operation' if held else ('candidates_available' if shortlist else 'no_qualified_available_route'),
              'operation_state': operation_state,
              'requirements_sha256': _hash(requirements), 'policy_sha256': _hash(policy),
              'input_candidates_sha256': _hash(candidates),
              'green_region_evidence_ref': green.get('evidence_ref') if green else None,
              'live_dispatch_authorized': False, 'native_features_restricted': False,
              'operation_replayed': False, 'automatic_fallback_started': False}
    output['decision_sha256'] = _hash(output)
    return output
