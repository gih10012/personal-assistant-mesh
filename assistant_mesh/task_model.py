"""Current owner preferences, applied at a safe task admission boundary only."""
from .model_recovery import startable
from .reasoning import read_model_catalog, validate_selection


PREFERENCES = {
    'entrance': ('gpt-6.2-sol', 'low'),
    'entrance_analysis': ('gpt-6.2-sol', 'medium'),
    'coordination': ('gpt-6.2-sol', 'high'),
    'routine_coding': ('gpt-6.2-sol', 'high'),
    'deep_repo_maintenance': ('gpt-6.1-sol', 'max'),
    'distributed_complex': ('gpt-6.2-astra', 'xhigh'),
    'computer_use': ('gpt-6.1-terra', 'max'),
}


def worker_role(task, entrance):
    if entrance:
        return 'entrance'
    if task.get('parent_id') or not (task.get('scope') or 'leader:owner').startswith('leader:'):
        return 'specialist'
    return 'leader'


def preference(config, task, entrance=False):
    if config['codex'].get('task_model_routing') is not True:
        return None
    if not startable(task):
        raise ValueError('codex_managed_original_operation_unsettled')
    context = task.get('context') or {}
    governance = context.get('governance') or {}
    difficulty = governance.get('difficulty')
    scopes = config.get('task_model_scopes', {})
    kind = scopes.get(task.get('scope')) or context.get('runtime_task_class')
    required = set(task.get('required') or [])
    required.update((governance.get('requirements') or {}).get('required_capabilities', []))
    if entrance:
        kind = ('entrance_analysis' if kind == 'entrance_analysis'
                or difficulty in ('D2', 'D3') else 'entrance')
    elif not kind:
        if required & {'computer_use', 'computer-use', 'desktop', 'browser', 'phone.gui'}:
            kind = 'computer_use'
        elif difficulty == 'D3' and ((context.get('origin') or {}).get('kind') == 'a2a'
                                      or task.get('children')):
            kind = 'distributed_complex'
        elif (context.get('origin') or {}).get('kind') == 'node-maintenance' and difficulty in ('D2', 'D3'):
            kind = 'deep_repo_maintenance'
        else:
            kind = 'coordination' if worker_role(task, False) == 'leader' else 'routine_coding'
    if kind not in PREFERENCES:
        raise ValueError('codex_task_model_class_unknown')
    model, effort = PREFERENCES[kind]
    return dict(model=model, reasoning_effort=effort, task_class=kind)


def admit_selection(agent, selected, account):
    if (getattr(agent, 'provider', None) or account.get('type') != 'chatgpt'
            or agent.config.get('cost_policy', 'existing_subscription') != 'existing_subscription'):
        raise ValueError('codex_task_model_subscription_required')
    receipt = validate_selection(read_model_catalog(agent.rpc), selected['model'], selected['reasoning_effort'])
    agent.routing_selection = dict(selected, provider='openai', catalog_sha256=receipt['catalog_sha256'])
