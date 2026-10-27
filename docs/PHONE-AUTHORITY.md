# Phone authority and signed data updates

These are source adapters for the incumbent receiver. They create neither a
second business Store nor a replacement receiver, worker, channel or media
pipeline. A real phone identity/challenge has deliberately not been prepared.
Root owns deployment and the private owner configuration.

`PhoneAuthority` receives the existing API, Registry, Allocations and
LocalSessionNodes instances. The optional session `node_bindings` seam maps a
phone subject/session to its real enrolled node ID; existing Chat mappings are
unchanged. The phone never receives an operator, worker, OAuth or account token.

## Runtime integration

Append `phone_http.start(api, private_config, origin=..., port=..., relays=...,
updates=...)` after the **actual** ready chain. Use the current receiver loaded
sources; do not overwrite it with an old frozen receiver. `start` binds only
127.0.0.1 and requires an explicit Root-reserved port. It returns the server and
service so the incumbent lifecycle can close them. Startup fails if attached
twice. There is no Store constructor or inflight recovery in this adapter.

The private config template is
`deploy/phone-authority-owner.json.example`; an empty nodes list is deployable
but authorizes nobody. Protect the file and its directory as owner-only.
`deploy/phone-ready-fragment.py.example` shows the append-only ready call.
`deploy/phone-nginx-routes.conf.example` shows exact HTTPS routes. Preserve the
existing TLS, OAuth, conversation and media configuration. Replace the proxy's
forwarded-proto header with `https`, preserve Host, and keep loopback private.
No redirect is returned by the phone backend.

For the incumbent frozen package, include the phone modules, the compatible
`node_bindings` addition in session_nodes/session_node_authority, and the
optional `admission_check(db)` addition to Allocations.accept/start. That callback
runs inside the existing allocation admission writer transaction. Retain all
incumbent Store/receiver wrappers, passive recovery and media changes. Restart
the receiver through its already authorized maintenance procedure. Roll back
the ready addition and exact proxy routes without restoring a database.

## Real enrollment window

1. The phone generates and preserves its own real node_id. Bind a real original
   task/epoch/request under `leader:project:amita-harmony-onboarding`; never use
   the owner/paused school scope or create a fake task solely for discovery.
2. Prepare the existing EnrollmentBootstrap owner document/independent oracle
   and challenge using that node ID. Keep the one original operation, nonce,
   continuation and expiry. Generate a strong one-operation token; only its
   SHA-256 is kept in the existing EnrollmentRelay owner config.
3. Attach `(existing_bootstrap, relay_owner_path)` in `relays`. The service
   installs `LiveTaskGate` using the **original** Store. Its BEGIN IMMEDIATE
   spans the live task/epoch/request validation and journal admission/result
   commit. Concurrent Store cancellation/reclaim cannot cross this boundary.
   Outside that transaction the validator fails closed. Status/original
   immutable readback remain available according to the original relay.
4. The phone invokes the unchanged `/enrollment/node` challenge/result/status
   protocol. A lost challenge/start acknowledgement never permits replay.
5. Owner invokes the original bootstrap.verify (full bytes vs independent
   oracle), then register with `RegistryEnrollment.register`, and independently
   reconcile with `RegistryEnrollment.observe`. For proxy source-device
   capabilities provide an actual independent connection observer; none is
   fabricated here. The registrar publishes a real declared Registry entry
   carrying the verified publication digest and returns its exact Registry
   reference. It does not fabricate independent metric observations/grants.
6. Owner calls `issue_node(...)`. It requires the original fresh verified,
   registered bootstrap receipt and a separate matching real Registry query.
   Write the returned node config atomically into the private nodes list and
   reload the ready hook. Import the returned private bootstrap on the phone.
   The plaintext bearer exists only in this private export/phone Asset, never
   in discovery, logs or the persisted authority config.

Bootstrap export:

```json
{
  "format": "mesh-phone-bootstrap/1",
  "config": {
    "format": "mesh-phone-client/1",
    "resource_url": "https://47-84-73-177.sslip.io/phone/node",
    "node_id": "REAL_PHONE_GENERATED_ID",
    "session_id": "PRESERVED_PHONE_SESSION",
    "mesh_id": "EXISTING_MESH",
    "authority_id": "ORIGINAL_AUTHORITY"
  },
  "credential": {"kind": "bearer", "token": "PRIVATE_EXPORT_ONLY", "scope": ["phone:node", "phone:updates"]},
  "enrollment": {
    "node_id": "REAL_PHONE_GENERATED_ID", "mesh_id": "EXISTING_MESH",
    "authority_id": "ORIGINAL_AUTHORITY", "operation_id": "ORIGINAL_OP",
    "request_sha256": "ORIGINAL_CHALLENGE_SHA256",
    "registration_receipt": {
      "format": "mesh-enrollment-registration/1", "operation_id": "ORIGINAL_OP",
      "authority_id": "ORIGINAL_AUTHORITY", "publication_sha256": "ORIGINAL_PUBLICATION_SHA256",
      "registry_ref": "capability:ACTUAL_CAPABILITY_ID:EPOCH"
    }
  }
}
```

To revoke, atomically set node.enabled=false (or global enabled=false). Each
authenticated request rereads it; identity/capability widening requires a ready
reload. Already committed execution is reconciled using its original receipt,
never replaced by a new operation. Owner can temporarily re-enable the same
narrow identity to collect its late result. Credential rotation is an explicit
owner change/reload, preserving node/session/task/operation identities.

## Work and complete result bytes

`POST /phone/node` accepts `{action:"mesh_*",arguments:{...}}` and returns the
original `mesh-interactive-node/1` envelope directly. Control uses the original
sessions and creates no business task. `mesh_work_pull` returns `dispatch` from
Allocations.pending for `node:<real-phone-id>`, filtered by its currently
advertised installed capability IDs. Each item retains the original
operation_id/capability_id/epoch/selection/task_id/task_epoch/deadline.

`mesh_work_report` arguments are `{session_id,report_id,provider:{action,arguments}}`.
Actions are inspect, accept, start, unknown, result and settle. Returned data is
in `provider`. Inspect returns the original allocation; other managed actions
return original dispatch views. Accept/start retain the original Registry,
independent measurements, grants, pools, task/leader leases and epochs.
Phone presence/capability checks also run inside admission. Start retry returns
execute_once=false even if its original ACK was lost. A reused report_id with
different arguments is rejected; positive start receipts are never cached for
re-execution.

Result accepts `{operation_id,capability_id,receipt_id,result:{bytes,sha256,base64}}`
with at most 65536 complete bytes. The exact immutable bytes and original
provider/receipt are committed in the original Store before returning
`{result_reference:"phone-result:<sha256>",sha256,bytes}`. This is artifact
storage, not a task/operation ledger. Settle requires that exact saved reference,
hash and size; a remote summary cannot stand in for complete bytes. Late results
for original unknown executions are retained. Unknown does not start new work.

The original caller (allocation actor) or operator reads the complete Blob via
the private incumbent `POST /v1/phone/result` route with
`{operation_id,capability_id}`. This route is never in the public phone proxy.
Another provider cannot read it. Existing caller task/operation/allocation
identities remain unchanged.

## Offline signed data publishing

Create an RSA-2048 key **offline**, retain the private key there, and distribute
only SPKI DER public bytes in the independently trusted phone bootstrap. No
server-side key, paid API or model request is involved. The publisher accepts a
private JSON spec with version, min_shell_version (native versionCode), and
artifacts `[{path,kind,source}]` where source is the offline local file.

```sh
python3 -m assistant_mesh.phone_updates --directory /PRIVATE/data-release \
  --spec /PRIVATE/publish-spec.json --private-key /OFFLINE/data-signing.pem \
  --key-id owner-data-v1 --origin https://47-84-73-177.sslip.io
```

Publish artifacts before atomically replacing manifest.json. Every artifact URL
is same-origin `/v1/harmony/updates/artifacts/<sha256>`. The signed envelope is
`amita-data-manifest-envelope/1` with key_id, algorithm RSA-SHA256, exact original
UTF-8 payload base64 and RSA PKCS#1 v1.5 SHA-256 signature base64. Payload format
is `amita-data-manifest/1`, with integer version/min_shell_version and artifacts
`[{path,kind,size,sha256,url}]`. Only config/skill/file; maximum 32 files, 32 MiB
per file, 64 MiB total. Executable/native suffixes and path traversal are rejected.
Native code/permissions still require a legitimate same-signature HAP update.

UpdateService receives the published directory, key_id, trusted public SPKI and
origin. The runtime reads no private signing key and verifies the envelope before
serving the manifest or a current hash-addressed artifact. GETs require a narrow
node bearer with phone:updates. No arbitrary URL, old unsigned file, redirect or
general filesystem read is available. A failed offline publish keeps the old
manifest; a rollback is a newer signed version referring to known-good bytes.

`updates.trust(real_node_id)` returns the agreed `amita-update-trust/1` object
with node_id, manifest_url, key_id, public_key_spki_base64 and enabled=true.
Its import is independent from a manifest signed by an unknown key. Shell
background limits and atomic installation are enforced by the native updater.

## Scope of evidence

Tests use explicitly synthetic identities and keys against real local
Store/Registry/Allocation, HTTP, oracle and signatures. They do not prove phone
installation, Harmony signing eligibility, real enrollment, background callback
delivery, a second real phone business request or removal of the VPS single
point. Those remain separate actual deployment/device acceptance steps.
