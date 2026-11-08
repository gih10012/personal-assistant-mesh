# Active signed configuration consumers

`await new TrustedRuntimeConfig(context).load(originalWorkConfig)` returns
`{dataVersion, work, liveviewEvent}` or null when there is no installed file.
Signature, complete byte hash, schema, original node or identity failure throws;
the caller must retain its current configuration and original journal.

The only source is the original UpdateDB active pointer and original pinned
trust. The consumer re-verifies active_json, reads the exact immutable artifact,
checks its full size/hash and rechecks that pointer/trust did not change across
capture. It never reads pending data or creates a new trust root. A locally
approved signed rollback is readable; the existing updater still retains its
highest anti-downgrade watermark for network admission.

Publish a config artifact at the fixed logical path `config/runtime.json`:

```json
{
  "format": "amita-trusted-runtime/1",
  "node_id": "ORIGINAL_NODE",
  "mesh_id": "ORIGINAL_MESH",
  "authority_id": "ORIGINAL_AUTHORITY",
  "session_id": "ORIGINAL_MEMBER_SESSION",
  "resource_url": "https://OWNER-APPROVED-ROUTE/phone/node",
  "liveview_event": "PROGRESS"
}
```

Only the route and recognized LiveView presentation scenario may vary; node,
mesh, authority and member session must equal the existing WorkConfig. Extra
keys are refused, including owner_enabled, enabled, screen_capture, permissions,
credentials or commands. This presentation value never requests an entitlement
or grants local owner control. It does not authorize screenshots, start work or
replay an unknown. There are no real identity values in this example.

Root applies `result.work` to the existing WorkClient/SelfMaintenance transport
at a safe request boundary. Replace only config, retaining original record,
operation, receipt, invocation and pending IDs. ControlRuntime must retain its
separate control-session identity when adopting the member's trusted route;
do not pass its independent session as the member baseline or reset its journal.
Its current configureFromMember route-change restriction is a separate Root
integration step. Apply liveviewEvent as display metadata while preserving the
owner's existing enabled/capture/gesture grants and actual system eligibility.

The shared InstalledConfig helper has only two allowed paths: runtime.json and
the existing PhoneFeatures `config/native-distribution.json`. NativeUpgrade's
existing `openOwnerDistributionLink(raw)` remains callable on the original UI
path, but now also requires those provided bytes to equal the currently active
signed distribution config. `openConfiguredDistributionLink()` is an optional
convenience. Both support agc-invitation-test and agc-designated-device and only
request a system/browser confirmation flow. Neither checks account eligibility,
proves HAP installation, grants silent updates or establishes a Profile lifetime.

This leaf's pure TypeScript checks exercise schema/identity/channel rejection.
They do not replace Root's real SDK import/compile or device/network acceptance.
