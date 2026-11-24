# Private authenticated phone screen-frame receiver

`PhoneAuthority` installs an in-memory `PhoneScreenShare` over its original API,
Store, Registry and Allocations. No new Store/schema/task/listener/API server,
frame SQLite/WAL, file, public download, history or model execution is created.
Nothing runs on module import. Root owns production packaging and HTTP binding.

Provider transport uses the already authenticated original `/phone/node` POST,
same NodeToken and installed control session. Exact actions:

- `mesh_screen_share_open`
- `mesh_screen_share_frame`
- `mesh_screen_share_status`
- `mesh_screen_share_close`

Envelope is exactly `{action, arguments}`. All four arguments carry this exact
public binding (snake case; source time values are epoch **seconds**):

```json
{
  "primary_session_id": "actual-installed-primary-session",
  "session_id": "actual-installed-control-session",
  "stream_id": "opaque-public-stream-session",
  "task_id": "original-task",
  "task_epoch": 7,
  "operation_id": "original-managed-operation",
  "capability_id": "actual-node:ui-control-v1",
  "binding_digest": "64-lowercase-hex-opaque-original-binding-digest",
  "expires_at": 1234567890
}
```

Use actual values from original SourceWorkRecord/Allocation and installed node
configuration. `expires_at` must be in the future, <=300 seconds away and no
later than the original allocation deadline or dedicated workload duration
measured from the initial open. It never renews an operation or
scope. An exact status/frame/open retry cannot change the binding. At most one
active slot per configured node; replacing an active binding fails. Expired or
closed original slots cannot silently reopen with the same binding.

The frame action adds exactly `frame`:

```json
{
  "revision": 1,
  "capture_timestamp": 1234.5,
  "width": 640,
  "height": 320,
  "bytes": 42000,
  "sha256": "64-lowercase-hex-of-real-JPEG-body",
  "base64": "canonical-base64-of-real-JPEG-body"
}
```

Maximum full JSON envelope is 64 KiB. JPEG body is <=42000 bytes with JPEG
header/tail, positive increasing safe-integer revision, <=640 positive integer
dimensions, finite nonnegative capture monotonic timestamp (never substituted
wall-clock time), exact actual body length, canonical base64 and SHA256. Equal
revision is accepted only for identical prior content; conflicting or reversed
revisions/timestamps fail. The source stores only latest bytes in memory. The
dedicated workload's max_fps limits new frames by source receive time; actual
capture timestamps must remain monotonic. A frame may be captured earlier and
held until the delivery interval. Identical revision retries are not new frames.

ACK format `amita-phone-screen-share-ack/1` echoes only stream_id, task_id,
task_epoch, operation_id, capability_id, binding_digest, expires_at, active,
acknowledged, frame_revision, frame_sha256 and bytes. An open/status without a
stored frame returns acknowledged=false, revision=0, bytes=0. Frame ACK true
means this source retained those actual bytes under current original fences;
it does not prove independent image interpretation, human input monitoring or
permission for unattended control.

**No local approvalId, owner action nonce, owner grant, credential, private
policy field or raw error is a wire field.** The phone's trusted sink adapter
sends only the public binding/frame above. It verifies the source ACK tuple,
frame hash/revision/length, then constructs in-process `ack.scope` from the
captured original LOCAL ScreenShareScope. Never serialize that local scope.

Every admission/read reuses original authenticated node identity, owner-enabled
installed adapter, live selected control presence, Registry capability/provider
and epoch, exact current owner-approved read-only allocation grant/resolution,
running original provider dispatch, and current original task owner/epoch/lease.
The selected outer action must be `inspect` with this exact dedicated workload
JSON inside the already hashed/base64 `workload.input` blob:

```json
{"format":"amita-phone-screen-share/1","command":"screen_share","duration_ms":60000,"max_fps":2}
```

duration_ms is an integer 1000–300000, max_fps is an integer 1–2. Existing
`inspect_ui`/`screenshot` one-shot workloads NEVER authorize continuous sharing.
The original dispatch stays running until explicit stop/final settlement; a
frame ACK never reports the dispatch completed or stores an operation result.
Additionally the owner-installed adapter must explicitly have
`screen_share_enabled: true`; absent/false is denied. This optional boolean
does not change old adapter spec when absent/false. True adds the dedicated
stream declaration to Registry spec and must be installed through the owner's
real capability contract/epoch update; this receiver never upgrades an old
capability, grant or app. Root enables only after the actual new app is ready.
An opaque digest, node-supplied boolean,
metadata receipt, action grant or provider identity is never a new grant.
Primary session ID is bound to installed identity; only control service presence
must remain live, allowing the primary page to background legitimately.

Cache idle TTL is 30 seconds, refreshed only by valid provider open/status/frame
requests, bounded by the fixed original expiry. A static screen stays legitimate
through authenticated status heartbeats (e.g. every 10 seconds), without invented
frames. Reader requests do not renew TTL. Expiry/closure clears pixels; original
task/epoch/grant/capability/owner/presence invalidation deletes them on the next
admission/read. No timer/background daemon is added merely to maintain this
cache; expiration is enforced and lazy eviction occurs on receiver access.

Lifetime is tied to the **original fixed Allocation deadline**, not a snapshot
of a shorter rolling task lease. Allocations.reserve fixes its deadline to the
minimum of requested lease and then-current task/grant/capability/pool/metric
limits; pending dispatch.deadline is that allocation deadline. There is no
Allocation-renew operation. Every frame/status/read still calls the existing
Allocation admission path, which checks the CURRENT original task owner/epoch
and actual deadline. A legitimate caller task renewal can keep the session live
within its original fixed allocation/scope; it never extends that allocation.
The sink/receiver do not issue task, grant, capability or Allocation renewals.

`mesh_screen_share_close` is a separate **delete-only** cleanup operation. It
still requires the exact authenticated producer node, installed primary/control
session identity, historical original allocation/provider and task ID/epoch.
It does not require current task/grant/presence permission to erase an exact
old slot after normal task end or revocation. Its response contains no image,
is always inactive/unacknowledged with zero bytes, and creates/renews no slot,
operation, task, lease or receipt. An already absent old binding returns that
same bounded absence ACK; a late old close never deletes a different replacement
stream. Node token revocation and node identity mismatches remain denied.
Before rejecting a new valid stream as busy, the receiver revalidates an old
slot and evicts it if its original fences are no longer live. Unknown/finished
old work therefore cannot block an unrelated valid original task.

Private reader hook Root attaches to the **existing** authenticated worker/
operator API dispatch, without a new listener or arbitrary bearer endpoint.
Use the actual installer described below, not a new role-to-actor shortcut
around protected runtime task-scoped authentication. `read()` independently requires
actor == original allocation.actor; operator role alone, other workers, phone
provider and other Mesh members cannot read pixels. Existing API authentication
must select that original worker; do not copy a phone token or accept an actor
from JSON. Response `amita-phone-screen-share-frame/1` has public binding fields
and latest `frame` including canonical base64, never prior frames or a URL.
Serve with existing private/no-store handling, and do not log body/response bytes.

The executable private route adapter is now `phone_screen_share_reader.install_dispatch`,
installed by the new copied phone_http.start after phone_owner ready succeeds,
inside its same rollback block and before the phone HTTP server thread starts.
Original callers need no producer-random stream ID/digest: authenticated
`POST /v1/phone/screen-share/frame` accepts exactly operation_id,
capability_id, task_id and integer task_epoch in the JSON body. These
come from the caller's original WorkEngine/Allocation, and must match the exact
allocation.actor/current original task and epoch. A positive canonical decimal
task_epoch is required. The source locates its single matching live private slot
and returns the public binding plus latest frame only after all original fences.
No GET/query route, listing, wildcard operation, viewer or NodeToken reader is
provided. Exact POST also accepts the full old binding for backward
compatibility; full-binding `read()` remains unchanged.

Install only on the original API/PhoneAuthority sharing the same Store. Today's
API supplies its already authenticated peer to this hook, just as its existing
private /v1/phone/result route. Actor is authenticated worker peer.node or
literal operator; operator is not promoted to a task's worker. There is no
guessed `_resource_actor`/`_task_actor` signature or JSON-selected actor fallback. Source
lookup itself independently enforces actor == original allocation.actor, task
ID/epoch, selected provider, live dedicated stream grant and control presence.
The seven-module overlay pins that new phone ready hook without rewriting the
protected API. Root still owns source package build, deployment and actual
acceptance, including existing private/no-store transport behavior.

Root still owns HTTPS phone sink/receiver ACK binding, private worker route,
commercial SDK build/signing, production deployment and actual phone-to-source
frame acceptance. Synthetic unittest evidence proves these software fences only.

Phone adapter is now implemented as `screenshare/PhoneScreenShareSink.ets`:

```arkts
const sink = new PhoneScreenShareSink(pinnedWorkConfig, actualControlSessionId,
  actualOriginalWorkRecord, originalLocalScope, trustedGate);
await sink.open(); // verified original source open, not frame/capture proof
session.start(uiAbilityContext, originalLocalScope, authorizationAdapter, sink);
// On explicit finalization; keep original WorkRecord attempted until cleanup:
await session.stop();
await sink.close(); // one cleanup request, unknown close is never retried
```

The config is the existing validated WorkConfig from the original WorkEngine.
Record must be the actual durable `attempted` record after original provider
start, with its original dispatch_json, receipt and invocation. Sink verifies
the stream workload's bytes/base64/hash, explicit format/duration/max_fps and
exact task/epoch/provider/capability. It accepts positive actual capability
epochs; it does not silently assume the legacy epoch1 contract supports streams.
Root must separately update WorkEngine/runtime stream eligibility when enabling
a new owner-installed Registry epoch. `trustedGate.valid(scope)` checks the
existing original owner authorization/lease; `serviceLive()` reads actual
control-service state. Neither callback may fabricate or renew presence.

Only the pinned HTTPS /phone/node URL is used, with an app Asset NodeCredential
whose node and phone:node scope match. HTTP1.1, system TLS verification, no
redirects/cache, 64KiB body/response limits and an 8-second total operation
deadline apply. No frame method accepts a URL/token or saves credentials/images.
Opaque stream ID and binding digest are derived from the captured internal
scope and original identities; no raw approval, nonce, invocation or local
permission value enters JSON. Each frame body is copied before awaits and is
posted once. Any unknown/mismatched ACK invalidates the sink and notifies the
session, cancelling capture via its existing disconnect hook.

The adapter waits from the last verified frame ACK plus the granted delivery
interval before the next POST. One pending real frame may wait behind a status
request; no dropped frame receives a fabricated ACK. Static scenes use status
every 10 seconds while actually attached, authorized and service-live. This
refreshes only the receiver cache's idle TTL, never the original expiry, source
task, Allocation or control presence. First open/status without a real frame is
not capture/background qualification. detach() immediately cancels local
requests/timers; Root calls and awaits close() once to observe source cleanup.
No automatic reopen/retry occurs after unknown outcome or process restart.

The original worker can save the latest authorized frame to a local private
JPEG for visual inspection:

```sh
python3 -m assistant_mesh.phone_frame_client --config /PRIVATE/worker.json \
  --task-id ORIGINAL_TASK --task-epoch ORIGINAL_EPOCH \
  --operation-id ORIGINAL_OPERATION --capability-id SELECTED_CAPABILITY \
  --directory /PRIVATE/frame-preview
```

Use an existing owner-only 0700 directory outside the worktree. The command
checks the original binding and JPEG hash, saves a 0600 preview, and reports
the file path and frame metadata. Delete the local preview after inspection.

For an initial real request, use the existing worker's live task and the
`assistant_mesh.phone_share_request` command. Request a grant for the task
ID and epoch, approve that exact grant with the existing owner API, and
bind the phone capability to its owner-defined screen capacity pool. The
phone owner enables reception in AMITA Settings.

The caller can then run:

```sh
python3 -m assistant_mesh.phone_share_request --config /PRIVATE/worker.json \
  --task-id ORIGINAL_TASK --task-epoch ORIGINAL_EPOCH \
  --capability-id SELECTED_CAPABILITY reserve --capability-epoch CAPABILITY_EPOCH \
  --grant-id APPROVED_GRANT --operation-id ORIGINAL_OPERATION --duration-ms 30000
```

The Source verifies the latest authenticated control presence on the original
phone and records transport reachability for that request. The reservation
still requires the original live task, grant, capacity and local owner
approval. Capture verification requires the first real acknowledged frame.
Inspect an unconfirmed reservation using the same operation ID before taking
another action.
