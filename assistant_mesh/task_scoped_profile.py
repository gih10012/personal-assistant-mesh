"""Bind an owner profile loader to one Worker's currently claimed scope.

No configuration is read until a matching task requests its profile. Model,
effort, catalog validation and receipts remain owned by the existing reasoning
and governance adapters; this helper carries no model policy.
"""


class TaskScopedProfile:
    def __init__(self, profile_loader, scopes):
        if (not callable(profile_loader) or isinstance(scopes, str)
                or not isinstance(scopes, (tuple, list, set, frozenset))
                or not scopes or any(not isinstance(scope, str) or not scope for scope in scopes)):
            raise ValueError('task_scoped_profile_arguments_invalid')
        self.profile_loader = profile_loader
        self.scopes = frozenset(scopes)
        self.worker = None

    def bind(self, worker):
        if worker is None or (self.worker is not None and self.worker is not worker):
            raise ValueError('task_scoped_profile_worker_binding_changed')
        self.worker = worker
        return worker

    def __call__(self, agent):
        task = getattr(self.worker, 'current', None)
        if not isinstance(task, dict) or task.get('scope') not in self.scopes:
            return None
        profile = self.profile_loader(agent)
        if (not isinstance(profile, dict)
                or any(not isinstance(profile.get(key), str) or not profile[key]
                       for key in ('model', 'reasoning_effort'))):
            raise ValueError('task_scoped_profile_explicit_selection_required')
        return profile
