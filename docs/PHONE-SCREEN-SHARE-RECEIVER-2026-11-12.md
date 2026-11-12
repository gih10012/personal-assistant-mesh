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
dedicated workload's max_fps also limits new frames by source receive time and
actual capture timestamp spacing; identical revision retries are not new frames.

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

Private reader hook Root must attach to the **existing** authenticated worker/
operator API dispatch, without a new listener or arbitrary bearer endpoint:

```python
# Exact POST path, no query/other methods; same authenticated peer handling as
# existing /v1/phone/result. NodeToken/provider /phone/node does NOT expose pixels.
if path.split('?', 1)[0] == '/v1/phone/screen-share/frame':
    if method != 'POST' or path != '/v1/phone/screen-share/frame' or peer.get('role') not in ('operator', 'worker'):
        raise PermissionError('screen_share_reader_route_not_authorized')
    actor = 'operator' if peer['role'] == 'operator' else 'node:' + peer.get('node', '')
    return service.screen_share.read(actor, payload)
```

Payload is exactly the public binding above. `read()` independently requires
actor == original allocation.actor; operator role alone, other workers, phone
provider and other Mesh members cannot read pixels. Existing API authentication
must select that original worker; do not copy a phone token or accept an actor
from JSON. Response `amita-phone-screen-share-frame/1` has public binding fields
and latest `frame` including canonical base64, never prior frames or a URL.
Serve with existing private/no-store handling, and do not log body/response bytes.

Root still owns HTTPS phone sink/receiver ACK binding, private worker route,
commercial SDK build/signing, production deployment and actual phone-to-source
frame acceptance. Synthetic unittest evidence proves these software fences only.
