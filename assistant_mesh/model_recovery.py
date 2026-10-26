"""Deterministic model-route admission and failure classification.

No model requests, credential reads, business execution or harness switching.
The governance DB owns observations; the original Worker/provider journals own
task, lease and external-effect authority. All scores/evidence are supplied by
the existing private owner policy or model-advice package, never inferred here.
"""
import errno
import hashlib
import json
import math
import os
import urllib.error

from .codex_provider import NativeRouteAuthorization, provider_metadata, resume_source
from .config import private_json
from .model_provider_policy import validate_provider_cost


CATEGORIES = ('quota', 'capacity', 'auth', 'network', 'provider', 'framework')
RETRY_SECONDS = dict(quota=300, capacity=60, auth=300, network=30,
                     provider=300, framework=60)
PAIR_FIELDS = ('provider', 'endpoint_id', 'model', 'reasoning_effort',
               'reasoning_mode', 'fallback_mode')
REQUIREMENTS = frozenset(('min_intelligence_score', 'min_context_tokens',
    'min_task_quality', 'required_capabilities', 'data_scope',
    'max_latency_seconds', 'max_incremental_spend_usd', 'prefer'))


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode('utf8')).hexdigest()


def requirements(value):
    if not isinstance(value, dict) or set(value) - REQUIREMENTS:
        raise ValueError('governance_task_requirements_invalid')
    for key in REQUIREMENTS - {'required_capabilities', 'data_scope', 'prefer'}:
        if key in value and (not number(value[key]) or value[key] < 0):
            raise ValueError('governance_task_requirements_invalid')
    caps = value.get('required_capabilities', [])
    if (not isinstance(caps, list) or len(caps) > 64
            or any(not isinstance(c, str) or not c or len(c) > 200 for c in caps)
            or not isinstance(value.get('data_scope', 'owner_private'), str)
            or not value.get('data_scope', 'owner_private')
            or value.get('prefer', 'lowest_cost') not in
               ('lowest_cost', 'lowest_latency', 'highest_quality')):
        raise ValueError('governance_task_requirements_invalid')
    # The current contract authorizes no additional cash, even when a task asks.
    if value.get('max_incremental_spend_usd', 0) != 0:
        raise ValueError('governance_new_spend_not_authorized')
    return dict(value)


def startable(task):
    cp = task.get('checkpoint') or {}
    failure = cp.get('runtime_failure') or {}
    intent = cp.get('native_execution_intent') or {}
    lifecycle = cp.get('native_lifecycle') or {}
    return not (task.get('status') in ('paused', 'needs_review', 'completed', 'failed', 'cancelled')
        or cp.get('side_effect_started') is True
        or (cp.get('goal') or {}).get('status') == 'paused'
        or (lifecycle.get('goal') or {}).get('status') == 'paused'
        or (failure.get('native_start_attempted') is True and failure.get('outcome') == 'unknown')
        or (intent and intent.get('settled') is not True)
        or (cp.get('turn_id') and lifecycle and lifecycle.get('settled') is not True
            and intent.get('settled') is not True))


def native_sources(task):
    """Use only claimed authority checkpoints, never task 'approved' fields."""
    states = [task.get('checkpoint') or {}]
    sessions = task.get('sessions') or {}
    session = sessions.get('codex') or task.get('session')
    if isinstance(session, dict) and session.get('harness') == 'codex':
        states.append(session.get('state') or {})
    sources = []
    for cp in states:
        lifecycle, intent = cp.get('native_lifecycle') or {}, cp.get('native_execution_intent') or {}
        if (cp.get('thread_id') and cp.get('codex_provider_identity')
                and cp.get('side_effect_started') is False
                and startable({'checkpoint': cp})
                and (lifecycle.get('settled') is True or intent.get('settled') is True)):
            sources.append(resume_source(cp))
    return sources


def native_authorization(task, binding, attempt_id, policy_sha256, factory=None):
    if not startable(task):
        raise ValueError('governance_original_task_not_safely_startable')
    factory = NativeRouteAuthorization if factory is None else factory
    return factory(task['id'], task['epoch'], attempt_id,
        binding['identity'], native_sources(task), policy_sha256)


def native_resume_compatible(task, binding, config):
    """Match the original Worker's resume selection before starting a backend."""
    cp, native = task.get('checkpoint') or {}, binding['native_config']
    base_home = config['codex'].get('auth_home')
    same_home = cp.get('codex_auth_home', base_home) == native.get('auth_home')
    resume = cp if (same_home and cp.get('codex_node') == config.get('node_id')
                    and cp.get('harness', 'codex') == 'codex') else {}
    session = (task.get('sessions') or {}).get('codex') or task.get('session')
    if not resume and isinstance(session, dict) and session.get('harness') == 'codex':
        resume = session.get('state') or {}
        changed_home = resume.get('codex_auth_home', base_home) != native.get('auth_home')
        if (session.get('node') != config.get('node_id') or changed_home) and not session.get('artifact'):
            return False, 'native_session_migration_unavailable'
    if cp.get('thread_id') and resume.get('thread_id') != cp['thread_id']:
        return False, 'native_original_history_unavailable'
    if not resume.get('thread_id'):
        return True, None
    bound = resume.get('codex_provider_identity')
    target = binding['identity']['provider_identity']
    if bound == target or (bound is None and target == 'openai'):
        return True, None
    if resume_source(resume) not in native_sources(task):
        return False, 'native_provider_transition_safe_source_required'
    return True, None


def classify_failure(error, now, phase='preflight', protocol=None, retry_at=None):
    """Project structured/native errors to bounded public codes, never text.

    429 without evidence of quota exhaustion means temporary capacity. A bad
    provider/auth/network request is never evidence that Codex itself is bad.
    """
    code = str(error).lower()
    protocol = protocol if isinstance(protocol, dict) else {}
    # Only inspect locally retained error fields; do not store their text.
    encoded = json.dumps(protocol, default=lambda _: '', ensure_ascii=True).lower()[:32768]
    details = code + ' ' + encoded
    status = getattr(error, 'code', None)
    data = protocol.get('data') if isinstance(protocol.get('data'), dict) else {}
    for row in (protocol, data):
        for key in ('status', 'status_code', 'http_status', 'httpStatusCode'):
            if type(row.get(key)) is int:
                status = row[key]
    if any(s in details for s in ('usage_limit_exceeded', 'insufficient_quota',
            'quota_exhausted', 'quota_exceeded', 'billing_hard_limit_reached',
            'usagelimitexceeded', "you've hit your usage limit")):
        category = 'quota'
    elif status in (401, 403) or any(s in details for s in
            ('auth_required', 'unauthorized', 'invalid_api_key', 'authentication',
             'token_expired', 'credentials_required')):
        category = 'auth'
    elif status in (429, 503) or any(s in details for s in
            ('rate_limit', 'too_many_requests', 'overloaded', 'server_is_overloaded', 'capacity')):
        category = 'capacity'
    elif (isinstance(error, (ConnectionError, TimeoutError, urllib.error.URLError))
            or getattr(error, 'errno', None) in (errno.ENETUNREACH, errno.EHOSTUNREACH,
                 errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT)
            or any(s in details for s in ('timeout', 'dns', 'tls', 'connection_error'))):
        category = 'network'
    elif ((phase == 'framework_start' and isinstance(error, (FileNotFoundError, PermissionError)))
            or code in ('codex_native_transport_start_failed', 'codex_disconnected',
                        'codex_native_transport_read_failed')
            and phase in ('framework_start', 'framework_runtime')):
        category = 'framework'
    else:
        category = 'provider'
    headers = getattr(error, 'headers', None)
    delay = headers.get('Retry-After') if headers is not None else data.get('retry_after_seconds')
    try:
        delay = float(delay)
    except (ValueError, TypeError):
        delay = None
    if not number(retry_at) or retry_at <= now:
        retry_at = now + (delay if number(delay) and delay > 0 else RETRY_SECONDS[category])
    return dict(category=category, reason='model_route_' + category,
                observed_at=now, retry_at=retry_at,
                rescue_harness_allowed=category == 'framework')


def route_pair(route, identity):
    return dict(provider=identity['provider'], endpoint_id=identity['endpoint_id'],
        model=route['model'], reasoning_effort=route['effort'],
        reasoning_mode=route.get('reasoning_mode', 'standard'),
        fallback_mode='disabled')


def route_bindings(config, route, rejected=None):
    """Expand the existing account pool; configuration never contains secrets.

    Custom provider metadata and expiring cost contracts are local reads. The
    selected child alone loads its provider_env_file via custom_provider().
    A model label never transfers a provider, account or network observation.
    """
    accounts = config.get('codex_accounts')
    if accounts is not None and (not isinstance(accounts, list) or not accounts
            or any(not isinstance(a, dict) for a in accounts)):
        raise ValueError('invalid_codex_account_pool')
    base = config['codex']
    for account in accounts or [{}]:
        native = dict(base, **account)
        provider = native.get('model_provider', 'openai')
        if provider != route.get('provider', 'openai'):
            continue
        try:
            binding = _route_binding(native, route)
        except (OSError, ValueError, KeyError, TypeError):
            if rejected is None:
                raise
            rejected.append(dict(route_id=route['id'], reason='route_configuration_or_cost_invalid'))
            continue
        if binding is not None:
            yield binding


def _route_binding(native, route):
    native.update(model=route['model'], reasoning_effort=route['effort'])
    if not isinstance(native.get('auth_home'), str) or not native['auth_home']:
        raise ValueError('governance_explicit_account_profile_required')
    native['strict_auth_home'] = True
    meta = provider_metadata(native)
    policy = {'included_subscription': 'existing_subscription',
              'free': 'free_api', 'local': 'local'}[route['cost_basis']]
    if meta is None:
        if policy != 'existing_subscription' or native.get('cost_policy', policy) != policy:
            raise ValueError('model_provider_cost_authorization_required')
    else:
        if native.get('cost_policy') != policy:
            raise ValueError('model_provider_cost_authorization_required')
        if policy == 'existing_subscription' and not meta['requires_openai_auth']:
            raise ValueError('model_provider_cost_authorization_required')
        validate_provider_cost(native, meta['id'], route['model'])
    network = {k: os.environ[k] for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
        'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy') if k in os.environ}
    if native.get('network_env_file'):
        network.update(private_json(native['network_env_file']))
    identity = dict(route_id=route['id'], model=route['model'], effort=route['effort'],
        provider=meta['id'] if meta else 'openai',
        provider_identity=meta['identity'] if meta else 'openai',
        endpoint_id=native.get('endpoint_id', meta['identity'] if meta else 'openai'),
        account_id=native.get('account_id') or hashlib.sha256(
            str(native.get('auth_home')).encode('utf8')).hexdigest(),
        egress_id=native.get('egress_id', 'inherited_environment'),
        egress_configuration_sha256=digest(network))
    if any(route.get(k) is not None and route[k] != identity[k]
           for k in ('endpoint_id', 'account_id', 'egress_id', 'provider_identity')):
        return None
    identity['route_key'] = digest(identity)
    return dict(route=dict(route), native_config=native, identity=identity)


def qualified(route, rating, identity, now, advice=None):
    """Retain legacy owner calibration; bind new evidence to the exact variant.

    An assumed same-name/route equivalence from advice remains advice. Neither
    the free label nor a particular provider/model decides task quality.
    """
    needs = requirements(rating.get('requirements', {}))
    if rating['difficulty'] not in route['difficulties']:
        return False, 'task_difficulty_not_qualified'
    if rating['difficulty'] == 'D3' and route['quality'] != 'strong':
        return False, 'advanced_quality_floor'
    if (route.get('authorized', True) is not True
            or route.get('marginal_cost_usd', 0) != 0
            or route.get('paid_fallback', False) is not False
            or route.get('auto_reload', False) is not False):
        return False, 'cost_or_route_not_authorized'
    pair = route_pair(route, identity)
    calibration = route.get('quality_evidence')
    calibrated = (isinstance(calibration, dict) and calibration.get('verified') is True
        and calibration.get('pair') == pair and bool(calibration.get('evidence_ref'))
        and calibration.get('quality') == route['quality']
        and number(calibration.get('observed_at')) and calibration['observed_at'] <= now
        and number(calibration.get('expires_at')) and now < calibration['expires_at'])
    record = None
    if route.get('frontier_candidate_id') and isinstance(advice, dict):
        record = next((r for r in advice.get('records', [])
            if r.get('candidate_id') == route['frontier_candidate_id'] and r.get('pair') == pair), None)
        ignored = {'route_not_currently_available', 'effective_cost_or_authority_unknown',
                   'below_owner_scenario_frontier', 'requested_resource_metric_unknown'}
        calibrated = bool(record and record.get('quality_equivalence') in
            ('exact_measured_pair', 'verified_same_variant_route')
            and not (set(record.get('reasons', [])) - ignored))
    # Old private policies are an explicit owner quality judgment, never AA or
    # independent verification. Explicit provider routes require calibration.
    if not calibrated and ('provider' in route or route.get('quality_evidence') is not None
                           or route.get('frontier_candidate_id') is not None):
        return False, 'route_quality_not_calibrated'
    source = record if record is not None else (calibration if calibrated else route)
    score = source.get('intelligence_score')
    if 'min_intelligence_score' in needs and (not number(score) or score < needs['min_intelligence_score']):
        return False, 'task_intelligence_floor_not_met'
    # Requirements beyond the legacy rating require real route declarations;
    # model/advice records already checked their candidate's capability facts.
    if record is None:
        caps = route.get('capabilities', [])
        if not set(needs.get('required_capabilities', [])).issubset(set(caps)):
            return False, 'task_capability_missing'
        if 'min_context_tokens' in needs and (not number(route.get('context_tokens'))
                or route['context_tokens'] < needs['min_context_tokens']):
            return False, 'task_context_requirement_not_met'
        if 'min_task_quality' in needs and (not calibrated
                or not number(source.get('task_quality'))
                or source['task_quality'] < needs['min_task_quality']):
            return False, 'task_specific_quality_not_verified'
        if 'max_latency_seconds' in needs and (not number(route.get('latency_seconds'))
                or route['latency_seconds'] > needs['max_latency_seconds']):
            return False, 'task_latency_requirement_not_met'
    scope = needs.get('data_scope', 'owner_private')
    if ('provider' in route or 'data_scope' in needs) and scope not in route.get('allowed_scopes', []):
        return False, 'task_data_scope_not_authorized'
    return True, ('model_frontier_calibration' if record is not None else
                  'route_calibration' if calibrated else 'owner_policy')
