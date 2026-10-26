"""Bind trusted atomic task helpers onto an existing frozen Store class.

No Store is constructed, source loaded, SQL copied or database opened here.
The caller retains its original constructor, authority database and hooks.
"""
from inspect import getattr_static
from types import FunctionType


def _conflict_class(base):
    # Exact frozen fixtures need not register their module in sys.modules.
    # Find the original Store function's real globals through its lineage.
    for owner in reversed(base.__mro__):
        function = vars(owner).get('update_task')
        if isinstance(function, FunctionType):
            conflict = function.__globals__.get('Conflict')
            if isinstance(conflict, type) and issubclass(conflict, Exception):
                return conflict
    raise TypeError('task_transaction_base_conflict_required')


def _bind(function, conflict):
    if not isinstance(function, FunctionType):
        raise TypeError('task_transaction_trusted_helper_required')
    namespace = dict(function.__globals__)
    namespace['Conflict'] = conflict
    bound = FunctionType(function.__code__, namespace, function.__name__,
                         function.__defaults__, function.__closure__)
    bound.__kwdefaults__ = function.__kwdefaults__
    return bound


def make_store_class(base, methods_class):
    """Trusted classes only; completed seams pass through without wrapping.

    Public legacy claim/update methods, constructor/recovery defaults, media
    behavior and installed final hooks remain inherited. New callers use the
    private helpers inside their own original authority/receipt transaction.
    """
    if not isinstance(base, type):
        raise TypeError('task_transaction_base_class_required')
    names = ('_claim_in_db', '_update_task_in_db')
    if all(callable(getattr(base, name, None)) for name in names):
        return base
    if not isinstance(methods_class, type):
        raise TypeError('task_transaction_methods_class_required')
    active = getattr_static(methods_class, '_yielded_active_goal_checkpoint')
    reserves = getattr_static(methods_class, '_yielded_goal_reserves_scope')
    marker = getattr(methods_class, '_conversation_final_hook_version', None)
    if not isinstance(active, staticmethod) or not isinstance(reserves, classmethod) or marker != 1:
        raise TypeError('task_transaction_helper_contract_required')
    conflict = _conflict_class(base)
    namespace = {name: _bind(getattr_static(methods_class, name), conflict) for name in names}
    namespace.update(__module__=__name__,
        _yielded_active_goal_checkpoint=active, _yielded_goal_reserves_scope=reserves,
        _conversation_final_hook_version=marker)
    return type('TaskTransaction' + base.__name__, (base,), namespace)
