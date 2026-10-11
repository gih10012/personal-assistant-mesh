"""Optional local model advice, not dispatch or a native-feature gate.

Only a privately installed evidence file supplies benchmark and route facts.
Task arguments can express requirements, never credentials or evidence claims.
No provider is contacted and no existing operation is changed by this module.
"""
import json
import time

from .model_frontier import advise_models
from .provider_runtime import _owner_bytes, _unique_object


REQUIREMENTS = frozenset(('max_incremental_spend_usd', 'min_intelligence_score',
    'max_latency_seconds', 'min_context_tokens', 'min_task_quality',
    'required_capabilities', 'prefer', 'data_scope', 'operation_state'))


def model_advice(config, requirements=None, now=None):
    """Read bounded owner evidence; return fixed failures without private text.

    ``operation_state`` is an advisory claim, not an authority observation.
    Even ``new`` and a recommendation cannot authorize execution/replay. The
    existing task/provider journals remain the execution authority.
    """
    base = dict(native_features_restricted=False, live_dispatch_authorized=False,
                operation_replayed=False, automatic_fallback_started=False)
    path = config.get('model_advice_file')
    if path is None:
        return dict(base, status='not_configured', recommendation=None)
    if (requirements is not None and (not isinstance(requirements, dict)
            or set(requirements) - REQUIREMENTS)):
        return dict(base, status='invalid_requirements', recommendation=None)
    try:
        raw = _owner_bytes(path, 1024 * 1024, 'model_evidence_unavailable')
        value = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object)
        if (not isinstance(value, dict)
                or set(value) != {'schema', 'snapshot', 'candidates', 'requirements', 'policy'}
                or value['schema'] != 1 or type(value['schema']) is not int
                or not isinstance(value['snapshot'], dict)
                or not isinstance(value['snapshot'].get('points'), list)
                or len(value['snapshot']['points']) > 256
                or not isinstance(value['candidates'], list) or len(value['candidates']) > 256
                or not isinstance(value['requirements'], dict)
                or set(value['requirements']) - REQUIREMENTS):
            raise ValueError('invalid_owner_model_evidence')
        selected = dict(value['requirements'])
        selected.update(requirements or {})
        advice = advise_models(value['snapshot'], value['candidates'], selected,
                               value['policy'], time.time() if now is None else now)
        return advice
    except Exception:
        # Missing/stale/unusable evidence never blocks Shell or opens a model
        # call, changes auth/provider, or exposes exception paths/source text.
        return dict(base, status='evidence_unavailable_or_invalid', recommendation=None)


def advice_summary(advice):
    """Small prompt projection; the full immutable decision remains queryable."""
    keys = ('status', 'recommendation', 'observed_at', 'source', 'cost_scenario',
            'warnings', 'decision_sha256', 'live_dispatch_authorized',
            'native_features_restricted', 'operation_replayed', 'automatic_fallback_started')
    summary = {key: advice[key] for key in keys if key in advice}
    records = advice.get('records', [])
    shortlist = advice.get('owner_scenario_frontier_candidate_ids', [])
    ordered = sorted(records, key=lambda row: (row['candidate_id'] not in shortlist,
                     row['candidate_id'] != advice.get('recommendation'), row['candidate_id']))
    summary.update(records=ordered[:8], records_total=len(records), records_truncated=len(records) > 8,
                   decision_sha256_scope='complete unprojected advice; summary is not the hashed decision')
    return summary


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Read-only local model advice; no dispatch')
    parser.add_argument('--evidence-file', required=True)
    parser.add_argument('--requirements-file')
    args = parser.parse_args(argv)
    try:
        requirements = None
        if args.requirements_file:
            raw = _owner_bytes(args.requirements_file, 65536, 'requirements_unavailable')
            requirements = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object)
        output = model_advice({'model_advice_file': args.evidence_file}, requirements)
    except Exception:
        output = dict(status='invalid_requirements', recommendation=None,
                      native_features_restricted=False, live_dispatch_authorized=False,
                      operation_replayed=False, automatic_fallback_started=False)
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    return 1 if output['status'] in ('invalid_requirements', 'evidence_unavailable_or_invalid') else 0


if __name__ == '__main__':
    raise SystemExit(main())
