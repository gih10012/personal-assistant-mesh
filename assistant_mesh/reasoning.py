"""Small native reasoning-selection contract; no model calls or credentials.

Catalog validation proves a setting is supported, not that inference used it.
The latter requires the matching native turn_context, not an agent assertion.
"""
import copy
import hashlib
import json


class ReasoningError(ValueError):
    pass


def read_model_catalog(rpc):
    """Read this initialized native app-server's catalog, including hidden rows."""
    rows, cursor, seen = [], None, set()
    for _ in range(32):
        params = {'includeHidden': True, 'limit': 100}
        if cursor is not None:
            params['cursor'] = cursor
        value = rpc('model/list', params)
        if not isinstance(value, dict) or not isinstance(value.get('data'), list):
            raise ReasoningError('codex_reasoning_catalog_invalid')
        rows.extend(value['data'])
        if len(rows) > 3200:
            break
        cursor = value.get('nextCursor')
        if cursor is None:
            return rows
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ReasoningError('codex_reasoning_catalog_invalid')
        seen.add(cursor)
    raise ReasoningError('codex_reasoning_catalog_limit')


def validate_selection(catalog, model, reasoning_effort):
    """Pure exact model/effort validation; never translate or silently downgrade."""
    if not isinstance(model, str) or not model or len(model) > 256:
        raise ReasoningError('codex_reasoning_model_required')
    if (not isinstance(reasoning_effort, str) or not reasoning_effort
            or len(reasoning_effort) > 64):
        raise ReasoningError('codex_reasoning_effort_invalid')
    if not isinstance(catalog, list):
        raise ReasoningError('codex_reasoning_catalog_invalid')
    matches = [row for row in catalog if isinstance(row, dict)
               and model in (row.get('id'), row.get('model'))]
    if len(matches) != 1:
        raise ReasoningError('codex_reasoning_model_unavailable')
    row = matches[0]
    options = row.get('supportedReasoningEfforts')
    if (not isinstance(options, list) or not options or len(options) > 64
            or any(not isinstance(item, dict)
                   or not isinstance(item.get('reasoningEffort'), str)
                   or not item['reasoningEffort'] or len(item['reasoningEffort']) > 64
                   for item in options)):
        raise ReasoningError('codex_reasoning_options_unavailable')
    efforts = sorted(set(item['reasoningEffort'] for item in options))
    if reasoning_effort not in efforts:
        raise ReasoningError('codex_reasoning_effort_unsupported')
    snapshot = {'id': row.get('id'), 'model': row.get('model'),
                'supported_reasoning_efforts': efforts,
                'default_reasoning_effort': row.get('defaultReasoningEffort')}
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode('utf8')).hexdigest()
    return {'model': model, 'reasoning_effort': reasoning_effort,
            'catalog_model': row.get('model') or row.get('id'),
            'supported_reasoning_efforts': efforts, 'catalog_sha256': digest,
            'validation': 'native_model_catalog'}


def prepare_turn(rpc, params, model, reasoning_effort=None):
    """Set native turn/start effort AND the higher-priority mode setting.

    None is omission: no catalog request, no injected setting/default or mode.
    Every input/history/permissions/goal field remains unchanged.
    """
    selected = copy.deepcopy(params)
    if reasoning_effort is None:
        return selected, None
    receipt = validate_selection(read_model_catalog(rpc), model, reasoning_effort)
    selected['model'] = model
    selected['effort'] = reasoning_effort
    mode = selected.get('collaborationMode')
    if mode is not None:
        if not isinstance(mode, dict) or not isinstance(mode.get('settings'), dict):
            raise ReasoningError('codex_reasoning_mode_invalid')
        mode['settings']['model'] = model
        mode['settings']['reasoning_effort'] = reasoning_effort
    receipt.update(status='prepared', thread_id=selected.get('threadId'),
                   turn_id=None, effective_verified=False)
    return selected, receipt


def observed_turn(records, thread_id, turn_id):
    """Extract only matched native metadata from caller-supplied owned rollout.

    The caller must select and authenticate the original rollout path. Nothing
    here opens files, loads/resumes a thread or trusts model output as metadata.
    """
    if not isinstance(records, list) or not thread_id or not turn_id:
        raise ReasoningError('codex_reasoning_observation_invalid')
    identities = [row.get('payload', {}).get('id') for row in records
                  if isinstance(row, dict) and row.get('type') == 'session_meta'
                  and isinstance(row.get('payload'), dict)]
    if identities != [thread_id]:
        raise ReasoningError('codex_reasoning_observation_identity_mismatch')
    contexts = [row['payload'] for row in records if isinstance(row, dict)
                and row.get('type') == 'turn_context'
                and isinstance(row.get('payload'), dict)
                and row['payload'].get('turn_id') == turn_id]
    if not contexts:
        return {'thread_id': thread_id, 'turn_id': turn_id, 'observed': False}
    context = contexts[-1]
    result = {'thread_id': thread_id, 'turn_id': turn_id, 'observed': True,
              'model': context.get('model'), 'reasoning_effort': context.get('effort')}
    mode = context.get('collaboration_mode')
    if isinstance(mode, dict) and isinstance(mode.get('settings'), dict):
        result['collaboration_model'] = mode['settings'].get('model')
        result['collaboration_reasoning_effort'] = mode['settings'].get('reasoning_effort')
    return result


def make_codex(base_class, profile_loader, receipt_callback=None):
    """Overlay ONLY explicit turn/start on an unchanged frozen Codex class.

    profile_loader(agent) supplies an owner-verified per-agent snapshot (or
    None). The loader owns file/identity policy; this module never finds files
    or guesses agent identities. Changes apply to the next explicit turn, not
    the active turn or an autonomous native-goal continuation.
    """
    if not callable(profile_loader):
        raise ReasoningError('codex_reasoning_profile_loader_required')

    class ProfiledCodex(base_class):
        def rpc(self, method, params, timeout=30):
            if method != 'turn/start':
                return super(ProfiledCodex, self).rpc(method, params, timeout=timeout)
            profile = profile_loader(self)
            if profile is None:
                self.reasoning_selection = None
                return super(ProfiledCodex, self).rpc(method, params, timeout=timeout)
            if not isinstance(profile, dict):
                raise ReasoningError('codex_reasoning_profile_invalid')
            effort = profile.get('reasoning_effort')
            if effort is None:
                self.reasoning_selection = None
                return super(ProfiledCodex, self).rpc(method, params, timeout=timeout)
            model = profile.get('model') or getattr(self, 'model', None) or self.config.get('model')
            # A custom provider's existing model-scoped cost policy must not
            # be bypassed by an overlay selecting a different model.
            if getattr(self, 'provider', None) and model != self.config.get('model'):
                raise ReasoningError('codex_reasoning_profile_provider_model_mismatch')
            request, receipt = prepare_turn(self.rpc, params, model, effort)
            receipt['profile_revision'] = profile.get('revision')
            self.reasoning_selection = receipt
            if receipt_callback:
                receipt_callback(self, copy.deepcopy(receipt))
            try:
                result = super(ProfiledCodex, self).rpc(method, request, timeout=timeout)
                turn = result.get('turn') if isinstance(result, dict) else None
                if (not isinstance(turn, dict) or not isinstance(turn.get('id'), str)
                        or not turn['id']):
                    raise ReasoningError('codex_reasoning_turn_receipt_invalid')
            except BaseException:
                receipt['status'] = 'unknown'
                if receipt_callback:
                    receipt_callback(self, copy.deepcopy(receipt))
                raise
            receipt.update(status='accepted', turn_id=turn['id'])
            self.model = model
            if receipt_callback:
                receipt_callback(self, copy.deepcopy(receipt))
            return result

    return ProfiledCodex
