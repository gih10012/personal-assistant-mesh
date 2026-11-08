# Owner-scoped local repair seam

This is a local action consumer for an original completed maintenance task.
The real cognition worker still owns that task; the device owns its journal,
permissions and fixed action implementation. No model Shell, URL, filesystem
path, native HAP, permission change or local control grant is executable here.
Nothing in this leaf activates a new cognitive provider or changes the existing
SelfMaintenanceClient, 110-second callback supervisor or service task status.

The worker result must be only this exact JSON (placeholders shown):

```json
{
  "format": "amita-phone-maintenance-repair/1",
  "incident_id": "ORIGINAL_INCIDENT",
  "task_id": "ORIGINAL_TASK",
  "task_epoch": 2,
  "operation_id": "ORIGINAL_OPERATION",
  "action": "rollback_signed_data",
  "target_version": 7
}
```

The actual IDs/epoch must match the authenticated original completed task result,
not the examples. Allowed actions are `sync_signed_data`, `observe_receipts` and
`rollback_signed_data`; only rollback has a positive target_version, others
require zero. Extra fields and Markdown/free-form instructions are rejected.
This narrow plan does not qualify an incapable model to write advanced code.
The real worker/route must still meet the task's cognition requirements.

`noLocalRepairs()` denies all actions. Root's local owner UI may explicitly
configure `LocalRepairPolicy` with booleans for sync/read-only receipt queries
and an exact allowed rollback_versions list. Keep this policy in local owner
state (the existing self-maintenance state or owner-only preferences), serialize
changes with the original lease and re-read it at each OS opportunity. Never
derive it from the model result, runtime route config, server `automatic_repair`,
or a caller-provided owner=true. User stop must cancel the running helper.

## Root integration without another journal

After SelfMaintenanceSupervisor has stored a real completed result and its
observed task_epoch, select that SAME IncidentRecord. The optional `repair`
field is serialized by the existing `phone_self_maintenance.state` through
the original `SelfMaintenanceClient.save`; no table, task, operation ID or
replacement business journal is created. Old records lacking a genuinely
observed task epoch remain non-executable. Query their original incident status
and validate the original task/operation binding before populating that metadata;
never take the epoch from the proposed model plan itself.

Construct `SafeRepairActions(context, updates, this.owner, savedWorkSnapshot,
originalNodeCredential, remaining)` while still holding the original UpdateDB
lease. These are trusted in-process objects, not model parameters. The `remaining`
closure must return zero when stopped, otherwise the actual remaining client/OS
window (minimum of both deadlines). Add helper.stop() to SelfMaintenanceClient's
existing stop path. Do not start a second timer/worker or extend the 110s window.

Give LocalRepairController a LocalRepairHost forwarding:

- `remaining` to that same budget closure;
- `save` to the existing bound client.save;
- `capture/execute/observe` to the SafeRepairActions instance.

Call `advance(originalIncident)` with the locally read owner policy. A null
result means less than two seconds remain: no intent/effect was created, and
the same incident/plan waits for a later OS opportunity. The caller does not
create another maintenance task. Root should expose the actual repair state in
its existing maintenance status instead of presenting a suggestion as execution.

`UpdateCoordinator.syncSmall(heldDB, heldOwner, budgetMs)` is the only shared
dependency change: defaults preserve the existing acquire/release/90s behavior.
The trusted held-lease path checks the original owner, never reacquires, and
never releases/closes the caller's DB. budgetMs is 1..90000; the action passes
the actual remaining window minus 500ms for journal completion. Its request
timeouts and cancellation use that reduced deadline. Original signed-byte,
anti-downgrade, numeric SDK-code and finally behavior remain in place. A prior
90s sync cannot acquire a fresh extra 90s repair allowance.

## Fixed effects and unknown handling

Before any effect, the controller durably saves intent, then attempted, under
the original incident. No successful save means no effect. A cold intent or
attempt is conservatively unknown. Every later advance of an existing unknown
only calls observe, never execute again.

Sync uses the real existing signed updater. Missing/incomplete active data is
unconfirmed, not success. An unknown sync is observed without automatically
retrying that repair attempt; regular OS data maintenance remains its existing
separate idempotent responsibility.

Receipt inspection only reads IDs from the original node WorkSnapshot. It uses
the original `/phone/node` provider inspect action, checks allocation/task/epoch,
provider/capability and the original receipt, and makes at most four bounded
requests. It never accepts, starts, executes, settles, clears a work record or
uploads fabricated results. More outstanding records remain unconfirmed for
later normal reconciliation; this helper does not claim all receipts settled.

Rollback accepts only the currently retained **previous** version, explicitly
allowed locally. It verifies the original pinned envelope and every complete
artifact, captures the exact envelope SHA256, then uses the existing fenced
UpdateDB.rollback on that same owner lease. The highest watermark is retained.
If completion is lost, only matching active target version + original signed
envelope hash + complete artifacts can confirm convergence. Otherwise it stays
unknown; it never toggles rollback a second time or chooses a new target.

Pure TypeScript fixtures exercise strict plan/owner/binding rejection, durable
intent before effects, exhausted-budget deferral and lost/cold rollback
observation without repeat execution. They are not SDK/device/network evidence.
Root still imports these modules in its single real SDK integration build and
tests one genuinely owner-authorized action before describing repair as active.
