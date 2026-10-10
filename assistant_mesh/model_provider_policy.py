"""Owner-scoped model cost declarations, not billing or access verification.

Only managed model selection uses this contract. Native tools remain available.
No keys, purchases, provider probes or silent paid fallback are performed here.
"""
import math
import time

from .config import private_json


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_provider_cost(config, provider, model, now=None):
    policy = config.get('cost_policy')
    if policy in ('existing_subscription', 'local'):
        return {'policy': policy, 'live_cost_verified': False}
    if policy != 'free_api':
        raise ValueError('model_provider_cost_authorization_required')
    path = config.get('cost_contract')
    if not isinstance(path, str) or not path:
        raise ValueError('model_provider_free_contract_required')
    try:
        contract = private_json(path)
    except (OSError, ValueError, TypeError):
        raise ValueError('model_provider_free_contract_invalid') from None
    if not isinstance(contract, dict):
        raise ValueError('model_provider_free_contract_invalid')
    models = contract.get('models')
    if models is None and isinstance(contract.get('model'), str):
        models = [contract['model']]
    expires = contract.get('expires_at')
    instant = time.time() if now is None else now
    if (not isinstance(provider, str) or not provider
            or not isinstance(model, str) or not model
            or contract.get('provider') != provider
            or not isinstance(models, list) or not models
            or any(not isinstance(item, str) or not item for item in models)
            or model not in models
            or contract.get('zero_cost') is not True
            or contract.get('paid_fallback') is not False
            or contract.get('auto_reload') is not False
            or not _finite_number(expires) or expires <= instant):
        raise ValueError('model_provider_free_contract_invalid')
    return {'policy': policy, 'expires_at': expires, 'live_cost_verified': False}
