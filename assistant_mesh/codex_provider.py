"""Explicit Codex Responses providers; no credential discovery outside profiles.

Metadata is supplied in private files. Secret values enter only the selected
child environment, not CLI arguments, public diagnostics or native history.
"""
import hashlib
import copy
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .config import private_json
from .model_provider_policy import validate_provider_cost


_FIELDS = frozenset(('name', 'base_url', 'wire_api', 'env_key',
                     'requires_openai_auth', 'env_http_headers'))
_ENV = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_RESERVED_ENV = frozenset(('HOME', 'CODEX_HOME', 'CODEX_HOME_OVERRIDE', 'PATH',
                          'LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH'))


def resume_source(checkpoint):
    """A source fingerprint excludes transient paths and governance advice."""
    fields = ('thread_id', 'codex_provider_identity', 'side_effect_started',
              'native_execution_intent', 'native_lifecycle', 'runtime_failure', 'goal')
    value = {k: checkpoint[k] for k in fields if k in checkpoint}
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    lifecycle = checkpoint.get('native_lifecycle') or {}
    return dict(thread_id=checkpoint.get('thread_id'),
        provider_identity=checkpoint.get('codex_provider_identity'),
        checkpoint_sha256=hashlib.sha256(encoded.encode('utf8')).hexdigest(),
        goal=copy.deepcopy(checkpoint.get('goal') or lifecycle.get('goal')),
        previous_transition=copy.deepcopy(lifecycle.get('model_route_transition')))


class NativeRouteAuthorization:
    """Process-local capability minted after trusted route/claim admission.

    Codex does not deserialize this object from task/config JSON. The owner
    Worker installs it directly on the selected backend, binding its original
    authority task/epoch, safe source checkpoints and exact target route.
    Serialized ``approved`` fields never create this capability.
    """
    def __init__(self, task_id, epoch, attempt_id, target, sources, policy_sha256):
        self._value = copy.deepcopy(dict(task_id=task_id, epoch=epoch, attempt_id=attempt_id,
            target=target, sources=sources, policy_sha256=policy_sha256))
        self._consumed = False

    def admit(self, checkpoint, actual_provider, model, effort):
        value, target = self._value, self._value['target']
        if self._consumed:
            raise ValueError('codex_route_authorization_already_used')
        if (target['provider_identity'] != actual_provider or target['model'] != model
                or target['effort'] != effort):
            raise ValueError('codex_route_authorization_target_mismatch')
        transition = None
        source_provider = checkpoint.get('codex_provider_identity')
        if (checkpoint.get('thread_id') and source_provider != actual_provider
                and not (source_provider is None and actual_provider == 'openai')):
            source = resume_source(checkpoint)
            if source_provider is None or source not in value['sources']:
                raise ValueError('codex_route_authorization_safe_source_required')
            transition = dict(format='amita-native-route-transition-v1',
                task_id=value['task_id'], epoch=value['epoch'], model_attempt_id=value['attempt_id'],
                thread_id=source['thread_id'], source=source, target=copy.deepcopy(target),
                policy_sha256=value['policy_sha256'], status='intent',
                native_configuration_verified=False, effective_inference_verified=False,
                goal_reset=False, operation_replayed=False)
        self._consumed = True
        return dict(model=model, reasoning_effort=effort, provider=target['provider'],
                    transition=transition)


def provider_metadata(config):
    """Read only provider metadata, without credentials, RPC or child launch.

    Recovery uses the same physical-provider fingerprint as native sessions.
    A provider name or model catalog alone is never an access/quality proof.
    """
    identity = config.get('model_provider', 'openai')
    if identity == 'openai':
        if config.get('provider_file') or config.get('provider_env_file'):
            raise ValueError('codex_builtin_provider_override_forbidden')
        return None
    if (not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', identity)
            or identity in ('ollama', 'lmstudio', 'amazon-bedrock')):
        raise ValueError('codex_custom_provider_id_invalid')
    if not isinstance(config.get('model'), str) or not config['model'].strip():
        raise ValueError('codex_custom_provider_model_required')
    try:
        provider = private_json(config['provider_file'])
    except (KeyError, OSError, ValueError, TypeError):
        raise ValueError('codex_custom_provider_configuration_invalid') from None
    if (not isinstance(provider, dict) or set(provider) - _FIELDS
            or not isinstance(provider.get('name'), str) or not provider['name'].strip()
            or provider.get('wire_api', 'responses') != 'responses'
            or not isinstance(provider.get('base_url'), str)):
        raise ValueError('codex_custom_provider_configuration_invalid')
    try:
        endpoint = urlsplit(provider['base_url'])
        endpoint.port  # Validate malformed port/bracket syntax before launch.
    except ValueError:
        raise ValueError('codex_custom_provider_endpoint_invalid') from None
    if (endpoint.scheme not in ('https', 'http') or not endpoint.hostname
            or any(ord(c) < 32 for c in provider['base_url'])
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or (endpoint.scheme == 'http' and endpoint.hostname not in ('localhost', '127.0.0.1', '::1'))):
        raise ValueError('codex_custom_provider_endpoint_invalid')
    provider = dict(provider, wire_api='responses')
    requires_auth = provider.get('requires_openai_auth', False)
    if not isinstance(requires_auth, bool):
        raise ValueError('codex_custom_provider_configuration_invalid')
    provider['requires_openai_auth'] = requires_auth
    headers = provider.get('env_http_headers', {})
    if not isinstance(headers, dict) or not all(
            isinstance(k, str) and re.fullmatch(r'[A-Za-z0-9-]+', k)
            and isinstance(v, str) and _ENV.fullmatch(v) for k, v in headers.items()):
        raise ValueError('codex_custom_provider_configuration_invalid')
    names = set(headers.values())
    key = provider.get('env_key')
    if key is None:
        provider.pop('env_key', None)  # TOML has no null literal.
    if key is not None:
        if not isinstance(key, str) or not _ENV.fullmatch(key) or requires_auth:
            raise ValueError('codex_custom_provider_configuration_invalid')
        names.add(key)
    if names & _RESERVED_ENV:
        raise ValueError('codex_custom_provider_environment_invalid')
    fingerprint = hashlib.sha256(json.dumps(
        {'provider': identity, 'configuration': provider}, sort_keys=True,
        separators=(',', ':')).encode('utf8')).hexdigest()
    return {'id': identity, 'settings': provider, 'identity': fingerprint,
            'requires_openai_auth': requires_auth}


def custom_provider(config, environment):
    metadata = provider_metadata(config)
    if metadata is None:
        return None
    identity, provider = metadata['id'], metadata['settings']
    requires_auth = metadata['requires_openai_auth']
    names = set(provider.get('env_http_headers', {}).values())
    if provider.get('env_key'):
        names.add(provider['env_key'])
    updates = {}
    if config.get('provider_env_file'):
        try:
            updates = private_json(config['provider_env_file'])
        except (OSError, ValueError, TypeError):
            raise ValueError('codex_custom_provider_environment_invalid') from None
        if (not isinstance(updates, dict) or set(updates) - names
                or any(not isinstance(v, str) or not v.strip() for v in updates.values())):
            raise ValueError('codex_custom_provider_environment_invalid')
    merged = dict(environment, **updates)
    if any(not isinstance(merged.get(name), str) or not merged[name].strip() for name in names):
        raise ValueError('codex_custom_provider_credentials_required')
    # A declared local endpoint cannot silently be a remote paid API.
    if (config.get('cost_policy') == 'local'
            and urlsplit(provider['base_url']).hostname not in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('codex_custom_provider_local_endpoint_required')
    if config.get('cost_policy') == 'existing_subscription' and not requires_auth:
        raise ValueError('model_provider_cost_authorization_required')
    validate_provider_cost(config, identity, config['model'])
    return dict(metadata, environment=updates)


def private_profile_home(path):
    if not isinstance(path, str) or not path:
        raise ValueError('codex_custom_provider_profile_required')
    root = Path(path).expanduser()
    try:
        entry = root.lstat()
        if (not root.is_absolute() or not stat.S_ISDIR(entry.st_mode)
                or entry.st_uid != os.getuid() or stat.S_IMODE(entry.st_mode) & 0o077
                or str(root.resolve()) != str(root)):
            raise ValueError('codex_custom_provider_profile_unsafe')
    except OSError:
        raise ValueError('codex_custom_provider_profile_unsafe') from None
    return str(root)


def provider_overrides(provider):
    """Codex -c uses TOML values; JSON strings/maps below contain metadata only."""
    def toml(value):
        if isinstance(value, dict):
            return '{' + ', '.join(json.dumps(k) + ' = ' + toml(v)
                                   for k, v in sorted(value.items())) + '}'
        return json.dumps(value, ensure_ascii=True)
    command = ['-c', 'model_provider=' + toml(provider['id'])]
    for key, value in sorted(provider['settings'].items()):
        command += ['-c', 'model_providers.' + provider['id'] + '.' + key + '=' + toml(value)]
    return command
