"""Optional local model advice, not dispatch or a native-feature gate.

Only a privately installed evidence file supplies benchmark and route facts.
Task arguments can express requirements, never credentials or evidence claims.
No provider is contacted and no existing operation is changed by this module.
"""
import json
import hashlib
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
    refresh_dir = config.get('model_refresh_dir')
    if path is None and refresh_dir is None:
        return dict(base, status='not_configured', recommendation=None)
    if (requirements is not None and (not isinstance(requirements, dict)
            or set(requirements) - REQUIREMENTS)):
        return dict(base, status='invalid_requirements', recommendation=None)
    try:
        refresh = None
        if refresh_dir is not None:
            # Local read only. A due/stale package never starts fetching,
            # changes a model or refreshes older cost/quality/route timestamps.
            from .frontier_refresh import latest_advice
            try:
                value, refresh = latest_advice(refresh_dir, now=now)
            except Exception:
                if path is None:
                    raise
                refresh = dict(status='unavailable', sources=[], local_metadata=[],
                               native_features_restricted=False)
        if path is not None:
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
        if refresh is not None:
            advice['frontier_refresh'] = refresh
            advice['decision_sha256_scope'] = 'model_frontier decision only; refresh metadata excluded'
            advice['advice_sha256'] = hashlib.sha256(json.dumps(advice, ensure_ascii=False,
                sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()
        return advice
    except Exception:
        # Missing/stale/unusable evidence never blocks Shell or opens a model
        # call, changes auth/provider, or exposes exception paths/source text.
        return dict(base, status='evidence_unavailable_or_invalid', recommendation=None)


def advice_summary(advice):
    """Small prompt projection; the full immutable decision remains queryable."""
    keys = ('status', 'recommendation', 'observed_at', 'source', 'cost_scenario',
            'warnings', 'decision_sha256', 'live_dispatch_authorized',
            'native_features_restricted', 'operation_replayed', 'automatic_fallback_started',
            'decision_sha256_scope', 'advice_sha256')
    summary = {key: advice[key] for key in keys if key in advice}
    records = advice.get('records', [])
    shortlist = advice.get('owner_scenario_frontier_candidate_ids', [])
    ordered = sorted(records, key=lambda row: (row['candidate_id'] not in shortlist,
                     row['candidate_id'] != advice.get('recommendation'), row['candidate_id']))
    summary.update(records=ordered[:8], records_total=len(records), records_truncated=len(records) > 8,
                   decision_sha256_scope=advice.get('decision_sha256_scope',
                       'complete unprojected advice; summary is not the hashed decision'))
    refresh = advice.get('frontier_refresh')
    if isinstance(refresh, dict):
        summary['frontier_refresh'] = {key: refresh[key] for key in
            ('request_id', 'status', 'observed_at', 'next_due_at', 'retry_at', 'due',
             'receipt_sha256', 'model_evidence_sha256', 'numeric_benchmark_imported') if key in refresh}
        summary['frontier_refresh']['source_counts'] = {
            'total': len(refresh.get('sources', [])),
            'fresh': sum(source.get('fresh') is True for source in refresh.get('sources', []))}
    return summary


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Read-only local model advice; no dispatch')
    parser.add_argument('--evidence-file')
    parser.add_argument('--refresh-dir')
    parser.add_argument('--requirements-file')
    args = parser.parse_args(argv)
    if args.evidence_file is None and args.refresh_dir is None:
        parser.error('one of --evidence-file or --refresh-dir is required')
    try:
        requirements = None
        if args.requirements_file:
            raw = _owner_bytes(args.requirements_file, 65536, 'requirements_unavailable')
            requirements = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object)
        config = {}
        if args.evidence_file is not None:
            config['model_advice_file'] = args.evidence_file
        if args.refresh_dir is not None:
            config['model_refresh_dir'] = args.refresh_dir
        output = model_advice(config, requirements)
    except Exception:
        output = dict(status='invalid_requirements', recommendation=None,
                      native_features_restricted=False, live_dispatch_authorized=False,
                      operation_replayed=False, automatic_fallback_started=False)
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    return 1 if output['status'] in ('invalid_requirements', 'evidence_unavailable_or_invalid') else 0


if __name__ == '__main__':
    raise SystemExit(main())
