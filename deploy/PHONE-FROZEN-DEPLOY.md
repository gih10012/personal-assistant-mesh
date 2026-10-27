# Pinned actual receiver phone overlay

Run the builder from this source checkout. It only writes a new source
candidate; it imports no runtime, opens no ledger, reads no private config,
creates no phone identity/challenge and installs no service.

```sh
python3 deploy/build-phone-frozen-candidate.py \
  --snapshot /var/tmp/amita-phone-baseline-20261028/snapshot.json \
  --snapshot-sha256 d27e5fc5f24e86f79fa69a42dbe27c8cf6fd86d8cc50d23e5305061925b499e5 \
  --extra-snapshot /var/tmp/amita-phone-baseline-20261028/extra-provider_runtime.json \
  --extra-snapshot-sha256 486b2715a69a82572fc3cd61affab29567a90c8b3b711ccc6450304c13c7209b \
  --destination /NEW/phone-candidate \
  --runtime-config /PRIVATE/phone-runtime-owner.json \
  --origin https://47-84-73-177.sslip.io --port 17684
```

The destination must not already exist. The two source snapshot manifests and
every source byte/hash are checked. Session node bindings and the two allocation
admission callbacks are exact small transforms of the actual resolved modules;
unknown surrounding methods remain byte-for-byte intact. Store, server, worker,
Codex, entrance/media and history modules are forbidden output overrides.

The package namespace prepends the overlay then the complete original package
directory, and retains the original namespace initializer bytes and fallbacks.
Unknown existing modules remain available. The generated launcher pins all
snapshotted actual deployed source files before loading the original launcher.
If one changed since the snapshot, rebuild from a fresh actual snapshot; do not
silently widen pins. Only missing cloud_http/enrollment dependencies are added.
The original provider_runtime is pinned separately, not copied from HEAD.

The launcher imports the original incumbent launch_receiver.py. Its outer hook
calls the captured original ready function first, preserving final conversation,
emit/final/event-feed, session, worker-maintenance and operation-isolation hooks.
It updates the nested original final receiver's `previous.ready`, because
assigning only the incumbent module's ready attribute would not affect main().
Original main() and its existing --config/--socket arguments remain in use.

Root deployment:

1. Reserve loopback 17684 and prepare a private signed data-release directory.
   Keep the RSA private key offline; runtime receives only public SPKI.
2. Write phone-runtime-owner.json using its adjacent template, with real private
   paths and public key. Use the phone-authority-owner.json.example template for
   an empty nodes list. Keep enrollments empty until a real phone node_id and
   original task-bound Bootstrap challenge exist. Protect files/directories.
3. Copy the **new candidate** directory to its versioned deployed path. Keep the
   original receiver and all its loaded files in place: the candidate references
   them and verifies their pins. Do not replace/copy the running SQLite file.
4. Retain the sole receiver's service, account, config and owner socket. Through
   existing authorized maintenance, change only its launcher path to the new
   candidate launch_receiver.py. Preserve its previous Python command and
   --config/--socket args. The original passive Store factory is untouched.
5. Add only the four exact paths in phone-nginx-routes.conf.example, using 17684.
   Do not forward the private original `/v1/phone/result` or any `/v1/` wildcard.
6. Perform one upper-entry acceptance with a separately identified synthetic
   fixture if no phone is present. A transport-ready marker, empty nodes or
   configured public key does not mean real phone enrollment/business acceptance.

Rollback switches the service launcher and exact proxy routes back to the
previous version, preserving all committed tasks/operations/results/unknowns.
The candidate introduces no worker, poller, account copy or inflight recovery.

`tests.test_phone_frozen_candidate` assembles from the actual source snapshots,
imports their actual frozen API/Store (not the modern repo API), composes the
incumbent isolation/entrance/atomic factories with passive construction, and
performs one authenticated loopback dispatch/accept/start/fullbytes/settlement
and original-caller read. Synthetic identity and independent synthetic fixture
measurements/grants/pools are explicit; this claims no real phone execution.
