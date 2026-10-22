# Private metadata diagnostics (opt-in source candidate)

No deployment, model turn, task recovery, archive re-compression, database write
or old-case replay was performed by this slice. Three bounded local units use
fake RPC/save functions and temporary private logs, not platform acceptance.

## Frozen assembly

One new standard-library-only module, `assistant_mesh.metadata_diagnostics`,
composes with the existing 3198 Worker/Codex/sessions and current governance or
EntranceWorker closed-failure backend. No modern core copy or daemon is needed.
Root's serialized launcher can opt in explicitly:

```python
from assistant_mesh import sessions  # The actual original frozen module.
from assistant_mesh.metadata_diagnostics import (
    MetadataDiagnostics, install_save_observer, observe_worker)

# Root prepares an owner-private 0700 parent. No implicit directory creation.
diagnostics = MetadataDiagnostics('/PRIVATE/diagnostics/metadata.jsonl', enabled=True)
install_save_observer(sessions, enabled=True)

# ExistingWorker is the ALREADY assembled governed/scoped Worker, or the
# original EntranceWorker with its role/profile/closed-failure decorators.
ObservedWorker = observe_worker(ExistingWorker, diagnostics)
ObservedWorker(config, config_path=config_path).run()
```

Default `enabled=False` returns original worker/client/backend objects and does
not install the save wrapper or create a log. Disabling/removing the launcher
addon never edits task/history/guard/queue data. No model choice or role profile
is changed. The backend is wrapped only AFTER its original constructor installs
governance and entrance failure hooks. Existing callable backend factories are
left unchanged; native-specific observation requires a backend class.

`install_save_observer` wraps the function once in this process, but only a
thread bound by the opted-in Worker observes it. Other threads call the original
save directly. Compression, upload, tick and commit code is NOT copied or run
twice. The original tick is called exactly where the original save invokes it.

## Recorded / deliberately absent

Recorded: safe task ID/epoch, fixed method/action, save phase, part, JSON byte
count, elapsed time, UTC observation time, returned/raised, bounded HTTP status
and errno, fixed exception family. Raw request/response, token, endpoint URL,
query, filesystem path, arbitrary exception text and native reasoning are never
written. Newly received native JSON-RPC errors may also contribute their numeric
error code and structured upstream HTTP status; old cached errors are not
attributed to a later RPC. Fixed control routes use labels such as
`control.session`, not raw URL paths. Unknown methods/routes/actions become
`other`/null. The HTTP body count
matches the frozen Client's default JSON encoding; native RPC count measures
parameters at this decorator entry, not a promise of exact final wire size after
an inner profile adapter adds fields. A successful return is not independent
execution, artifact, phone delivery or business-completion attestation.

Steps distinguish native RPC, rollout validation, goal, finish/close, save packing,
upload, commit, and the ticks made within packing/upload. Packing combines local
read and gzip: it cannot distinguish those without modifying the original save
implementation. Client failures during ticks retain their own route/status.

The log is an owner-owned regular single-link 0600 file in a private parent.
Writes append one bounded JSON line and fsync. Missing/unsafe/full log storage
only increments the recorder's in-memory `dropped` count; it never masks the
original result/exception or stops native work. There is no fallback log to
stdout/journal/public Git. This is best-effort telemetry, not a recovery journal,
hard real-time guarantee or defense against another malicious same-UID process.

## Original cases remain unchanged

The Oct 22 backend/skill native completed and frontend interrupted cases keep
their original task/epoch/thread/unknown records. Future diagnostics cannot
recreate an HTTP status that the old Client discarded. Do not re-run a model,
clear a guard, resend a save, re-compress a partial archive or create another
task merely to reproduce that old failure. Any original-case settlement still
needs separate owner-controlled evidence, backup and fenced reconciliation.

Native protocol error semantics reference: [official app-server errors](https://learn.chatgpt.com/docs/app-server#errors).
Only protocol metadata is observed; this addon does not alter error handling.
