# Task-bound managed capability call

`assistant_mesh.capability_call.TaskCapabilityCalls` composes existing authority
RPCs. It removes repeated ledger boilerplate, not the authority's admission checks
or any native agent functionality. An independently running ProviderRuntime
consumes admitted dispatch; this helper never executes its callback.

## Owner-installed integration

```python
from assistant_mesh.capability_call import TaskCapabilityCalls

# All constructor arguments come from fixed deployment/current worker context,
# NOT from model arguments. Client is the existing authenticated Worker Client.
calls = TaskCapabilityCalls(
    client, '/private/owned-0700-directory/capability-calls.db',
    authority_id='actual-authority-node-id', node='current-worker-node',
    task_id=current_task['id'], task_epoch=current_task['epoch'])

receipt = calls.call(
    operation_id='stable-original-operation-id',
    decision_id='stable-original-choice-id',
    selection={
        'capability_id': 'ali-vps-public-https-v1',
        'epoch': 1,
        'action': 'network.https',
        'scope': {'operation': 'get', 'url': 'https://example.org/document'},
        'workload': {'kind': 'https', 'max_bytes': 65536, 'timeout_seconds': 20},
        'observations': [{
            'observation_id': 'actual-independently-verified-observation-id',
            'metric': 'actual-measured-metric', 'unit': 'actual-unit',
            'max_age_seconds': 60}]},
    candidates=[{'capability_id': 'ali-vps-public-https-v1', 'epoch': 1},
                {'capability_id': 'other-actual-candidate', 'epoch': 1}],
    rationale='The model selected this path for this scope and workload.',
    lease_seconds=60)

status = calls.status('stable-original-operation-id')
result = calls.result('stable-original-operation-id')
```

This example is a shape, not an observation, grant or actual invocation. Use the
current task-bound routing context to discover the real capability, epoch,
observation IDs/metric/unit and candidate evidence. Ordinary URLs are chosen by
the model's scope; the helper needs no URL-specific owner configuration. The
installed adapter owner contract still controls HTTPS/range/TTL and private
transport, as described in `EGRESS-ADAPTER.md`.

Journal parent must already be owned `0700`; the SQLite file is an owned regular
single-link `0600` file. It is fixed deployment state, not a model-selectable
path. Preserve it across process/profile/code restarts. One journal is bound to
one authority and authenticated worker node; it can hold multiple tasks. Supply
the currently authenticated task ID/epoch on every entrypoint invocation. The
client must be bounded, authenticated and **must not automatically retry writes**.
The existing Worker Client meets that request-level requirement.

Root's entrypoint integration optionally uses private owner configuration:

```json
{"capability_calls": {
  "authority": "actual-authority-node-id",
  "journal_path": "/private/owned-0700-directory/capability-calls.db"
}}
```

This configuration enables a convenience surface, never a native-network gate.
The CLI/native tool binding itself must reject model overrides of authority,
client, credentials, journal path, node, task ID and task epoch.

The implemented native surface is `mesh(action="capability_call",
arguments={operation_id,decision_id,selection,candidates,rationale,lease_seconds})`;
`capability_status` and `capability_result` accept only `operation_id`. Current
task/node/epoch are injected by Worker. An old native thread can instead use its
native Shell with the independent CLI, without replacing the Leader thread:

```sh
python -m assistant_mesh.cli --config /private/worker.json capability-call \
  --task-id CURRENT_TASK --epoch CURRENT_EPOCH --operation-id STABLE_OPERATION \
  --payload-file /private/model-choice.json
python -m assistant_mesh.cli --config /private/worker.json capability-status \
  --task-id CURRENT_TASK --epoch CURRENT_EPOCH --operation-id STABLE_OPERATION
```

The owned 0600 choice file contains `decision_id`, `selection`, `candidates`,
`rationale` and optionally `lease_seconds`, not task/actor/journal credentials.
Journal and payload must be outside git worktrees. Unresolved/unknown call
reports use a nonzero CLI exit/native-tool failure while preserving the original
report; a zero exit still does not prove provider completion. Without opt-in
configuration only this convenience capability is unavailable; native tools work.

This slice calls capacities owned by the configured authority. A foreign
projection is discovery evidence, not a locally reservable owner pool; remote
placement still uses existing authenticated delegation. Automatic foreign-owner
call/delegate/artifact composition remains future usability work.

## Composition and receipts

The model supplies one concrete allocation selection, stable IDs, declared
candidates and rationale. The helper uses existing pure validators to detach
JSON and makes:

1. An immutable routing proposal whose work is the selected action/scope/workload,
   whose selection contains the single-target plan/original operation ID, and whose
   evidence references contain the actual observation IDs/metric/unit.
2. A single-target allocation `reserve` under the original operation ID, current
   authenticated worker and original task epoch. Route is empty in this thin slice.
3. A routing `link` to that same existing managed allocation.
4. Original-ID inspection for current status and provider-reported settlements.

Actual write payloads are checked against the server's 128 KiB body limit before
any durable attempt. Routing/plan validators retain their stricter existing
limits. Connection handles are closed on success and failure. SQLite FULL-sync
transactions and stage CAS persist an intent before each write RPC; cooperating
concurrent callers get at most one write attempt per stage.

Before any write the helper checks the configured authority's authenticated
`mesh/hello`, and before composition it reads routing context to check current
authority/task/node/epoch. The Core then rechecks the live task/Leader lease,
capability epoch, exact existing grant if cross-principal, independently verified
fresh scope/workload-specific observations, owner pool bindings and shared
capacity. Model evidence references in a routing proposal remain declarations;
only actual allocation admission performs those checks. This helper creates no
observations, grants, pools, credentials or child tasks.

Returned `format=mesh-capability-call/1` includes original/current task epochs,
operation/decision IDs, immutable request and plan SHA-256, per-stage state,
actual allocation/routing receipts and fixed diagnostics. `phase=observed` means
an original allocation was read, **not** that its callback completed.
`operation_state` preserves `reserved/accepted/running/unknown/completed/...`.
An unknown operation can still hold real capacity; this helper never cancels,
settles, releases it, selects fallback or starts a new ID automatically.

`result()` returns existing settlements, each explicitly `provider_reported`,
with an independently computed hash of the settlement JSON. It does not follow
result URLs/paths or fetch the remote private artifact. Actual artifact readback,
body SHA/oracle comparison, authentication and inference are separate evidence.
The report therefore keeps execution/target-content/authentication/inference
verification false even if a provider reports GET 200/TLS success. A 401/403
transport report is not an authenticated capability or successful inference.

## ACK loss, restart and task continuation

Stages are `ready → attempted → confirmed`. A write that loses its ACK, returns
invalid data or crashes after durable intent stays attempted. On the exact same
call after restart the helper only inspects that stage's **original** IDs. A
matching original proposal/reservation/link can confirm the stage and unlock only
an as-yet-unattempted next stage. No attempted stage is resubmitted, including when
the original record is absent or a read fails. The current HTTP API generalizes
ValueError to HTTP 400; that is not absence/non-execution proof.

There is consequently a conservative crash-before-RPC window: an intent may be
attempted even though no authority effect occurred. Do not clear the journal or
change IDs to bypass it. Investigate the original receipts; a future explicit
owner reconciliation protocol would be needed to release that ambiguity.

`status`/`result` are always read-only. After lease expiry the task-fenced routing
read can fail while the principal-bound original allocation remains readable.
After same-node task continuation, the new current epoch can read the old
operation; it cannot reserve or write an incomplete old-epoch call. Node or
authority rebinding of the same local journal is rejected. Cross-node journal
handoff and a historical operator read are deliberately not implemented here.

The current authority API exposes a logical node authority, not a database
incarnation attestation. Restoring/resetting a different ledger under the same
node ID cannot be cryptographically detected by hello. Existing reservation
readbacks expose plan/task/principal identities but not their original internal
fingerprint or requested lease seconds; the helper validates the exposed original
identities and plan, not an unavailable proof of that internal fingerprint.
Owner transport/ledger continuity and journal preservation remain prerequisites.
This does not provide global consensus or protect against malicious same-UID
native processes that already have OS access.

## Validation scope

`tests/test_capability_call.py` exercises real in-process API/Store/Routing/
Allocations with synthetic verified measurements, no external HTTP, credentials,
models, SSH or provider callbacks. It covers successful composition, immutable
identity, restart, lost ACK at all three stages, attempted-but-absent reservation,
unknown hold preservation, forged/scope-mismatched observations, missing foreign
grants, epoch/lease loss, changed authority, original provider settlement/hash,
private journal validation, wire-size preflight and connection closure.

These tests are protocol evidence only, not another real cross-node deployment,
target-content/authentication test or complete "one machine" Mesh acceptance.
