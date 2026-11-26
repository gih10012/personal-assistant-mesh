# Original-task read-only screen-share runtime

Root retains exactly one `ScreenShareRuntime` at UIAbility lifecycle level.
`create(context)` reads the original `mesh_node_v1` identity and
`mesh_work_v1` primary configuration, reuses `TrustedRuntimeConfig`, and loads
the original node credential from `NodeCredentialStore` Asset at request time.
It never creates another physical node, Store, task or allocation.

```ts
// Retain the promise/instance on EntryAbility. Creating it performs private
// configuration reads only, not capture, network registration or authorization.
this.shareRuntime = await ScreenShareRuntime.create(this.context);
// onForeground/onBackground, AFTER updating real meshForeground AppStorage:
this.shareRuntime.foregroundChanged();
// onDestroy, before discarding the runtime:
await this.shareRuntime.destroy();
// PhoneFeatures may present the optional ScreenSharePanel({ foreground: ... }).
```

The panel reads `ScreenShareRuntime.current()` and never creates/owns another
runtime. Hiding the page only stops its local cached-state refresh; it does not
pretend an app background transition or stop an eligible media session. Root's
actual UIAbility callbacks exclusively supply `meshForeground`. The existing
`ScreenShareOwner` attachment is reused, not replaced. The selfHDC probe panel
and keys remain independent; this read-only runtime never opens or injects HDC
input and requires no human-input observer or accessibility provider.

## Source owner-installed contract

The app persists a DECLARED component identity in
`amita_screen_share_runtime_v1`: control session is
`<original-node-id>:screen-share-v1`, capability is
`<original-node-id>:ui-control-v1`. This identity is not a service callback or
proof of capture. The Source owner must explicitly install/enable that binding
with `screen_share_enabled: true`. While the system confirms an unlocked
foreground, the receiver checks the authenticated installed adapter through
`mesh_discover` before advertising an available capability. The Source can
return installed adapter information before the component has any presence.

The authenticated original Source session view must contain:

```json
{
  "lifecycle": "screen-share",
  "installed_adapter": {
    "enabled": true,
    "screen_share_enabled": true,
    "capability_id": "<original-node-id>:ui-control-v1",
    "epoch": 1,
    "execution_verification": "declared_only",
    "screen_share_standing_allowed": true
  }
}
```

The positive capability epoch is read from that actual view and compared with
the original selection, never inferred from node ID/current time. The Source
still validates its actual Registry epoch, installed adapter, grant and task
lease. Root supplies the agreed positive-epoch WorkProtocol change and the
actual ACK hash in `ScreenShareOwner.status().lastAcknowledgedSha256`. Missing
fields or an incorrect lifecycle/epoch fail closed. No lifecycle value here
asserts an AccessibilityExtension connection or full computer-use capability.

`screen_share_standing_allowed: true` enables automatic reception and consumes
the durable exact approval internally for `inspect`. HarmonyOS still supplies
the AVScreenCapture consent dialog. With the flag absent, the manual flow
remains available. Standing reception can be paused locally across app
restarts. The native launch path cannot operate under standing authorization
until a physical-input monitor can protect that action.

## Original operation and local authorization

Only `device.gui` / `inspect` carrying a hash-verified input Blob is supported:

```json
{"format":"amita-phone-screen-share/1","command":"screen_share","duration_ms":60000,"max_fps":1}
```

Only those four keys are allowed; duration is 1000–300000 ms and FPS is 1 or 2.
Legacy inspect/screenshot recipes, `perform_action`, arbitrary commands, URLs,
remote approval booleans and unrelated capability kinds are rejected. Optional
outer selection-scope `purpose` is bounded display text, not authority.

The existing `WorkEngine` owns every prepared/admit/start/attempt/result and
settlement transition. Its private WorkSnapshot contains the original
WorkRecords; the new preferences store contains only that execution journal,
component identity and per-request local approvals, not a copy of the Source
task ledger. The original prepared record and exact pending binding are durable
before the binding is presented to the owner or consumed under standing
authorization. The private approvalId/nonce, input hash, task/epoch, capability
epoch, revision and deadline identify the exact request. Source admission and
invocation follow the approval write. The nonce stays on the phone.
Foreground loss before the first genuine OS background ticket revokes pending,
approving and approved-but-not-yet-invoked requests.

`execute()` receives the actual durable `attempted` WorkRecord and passes it to
`ScreenShareOwner.startManaged(primary, actualControlSessionId, row, scope,
authorization, gate)`. The full original taskEpoch is mandatory. It waits for
the actual media stop, then awaits Owner.stop including Source close ACK, then
returns a <=64 KiB result Blob through the original WorkEngine. The first
frame never completes the task. Summary contains original IDs, bounded local
stop reason, acknowledged frame count/revision and the actual verified last
ACK hash; no pixels or local approval secrets enter the result.

Missing ACKs, zero acknowledged frames, native/background cleanup failure or
unknown Source close leave the original outcome unknown. No retry/replay of a
capture is made. On cold start, all non-closed/non-denied approvals and all
unresolved attempted WorkRecords become unknown and are only observed through
the existing reconciliation path. A durable closed metadata result can be
reported on foreground reunion without restarting capture or restoring an old
owner authorization.
Durable metadata results are reconciled before empty unknown records; those
unknown records are observed at most once per actual foreground/owner-receive
window, so a stale unknown cannot consume every polling slot or starve results.

## Presence and finite lifetime

A separate 10-second pulse is independent from the busy WorkEngine execution
loop. It renews only the DECLARED control component's presence, while actual
UIAbility foreground is true or a valid original scope has both real capture
and a real background ticket. It never calls a task/allocation lease renewal.
An old ONLINE presence intent is abandoned before offline withdrawal so the
WorkEngine's safe control retry cannot accidentally renew ineligible presence.
ONLINE presence is checked again after asynchronous Asset access, immediately
before dispatch. HTTP control/provider calls are serialized and bounded.

Current original allocations are expected to be capped around 60–90 seconds;
the requested five-minute maximum is not a promise of that lifetime. Local
scope expires no later than five seconds before the original deadline. Neither
presence nor valid media extends that deadline. In the background, after media
and its OS ticket end, final metadata settlement may wait for foreground
reunion instead of claiming an execution window that no longer exists.

The owner may disable request reception or stop the current request at any
time. Stop revokes the exact local binding and invokes actual media/Source
cleanup; it never cancels another selfHDC or accessibility task. Static scenes
do not fabricate frames/heartbeats and have no frame-idle qualification timer.

## Delivery boundary

No common EntryAbility, PhoneFeatures, manifest, package, Source, broker or
native file was changed in this worktree. Root granted one focused Node test
file through Click: the actual runtime and original WorkEngine run against
synthetic platform/transport/media boundaries. It covers durable exact approval,
first-frame non-completion, Source close before original result, independent
eligible presence, admission-time background revocation, and cold unknown
non-replay. This is not capture/Source/device acceptance. No SDK build, device,
signing, production or model operation was performed. Root owns the agreed contract
patches, full SDK build, integration and actual
phone batch. Declared adapter/configuration, source bytes, full build, OS
consent, verified frame delivery and genuine background operation remain
separate evidence.
