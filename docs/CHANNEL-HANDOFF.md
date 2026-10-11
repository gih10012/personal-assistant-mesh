# Carrier handoff readiness (PAM-009c)

This source slice adds owner-code verifier injection and an honest **read-only
inventory**, not a deployed handoff verifier, second receiver, authority move,
or partition HA. Production deployment remains a separate Root-owned step.

## Owner injection, closed by default

`ChannelAuthority(..., handoff_verifier=None, operation_verifier=None)` forwards
explicit Python callables to its original `CarrierWitness`. Non-callables are
rejected before witness initialization. Missing hooks still reject bootstrap,
grant and reconciliation. JSON configuration cannot select a module/function or
post an attestation; carrier HTTP routes cannot grant/freeze/reconcile. Existing
server startup does not install these hooks automatically.

The owner handoff callable receives the actual frozen `before_term`, proposed
holder and proof reference. It must independently verify the full contract in
[CHANNEL-CARRIER](CHANNEL-CARRIER.md), including legacy unmanaged callers at
bootstrap. This inventory does **not** implement that callable. A synthetic test
verifier is not an acceptable production deployment hook.

## Inventory from actual state, not posted booleans

`assistant_mesh.channel_handoff.collect_readiness` takes protected owner pins:

```python
report = collect_readiness(
    private_original_database, original_authority, original_account_binding,
    original_owner_ref, original_witness_id, original_witness_incarnation,
    checkpoint_path=private_audit_checkpoint,
    checkpoint_sha256=independently_retained_payload_sha,
    spool_paths=[private_original_carrier_spool],
    caller=actual_owned_channel_supervisor,  # optional, actual in-process handles
    max_bytes=128 * 1024 * 1024,             # explicit environment-dependent limit
)
```

No Store is constructed, writable witness `inspect` called, schema initialized,
service stopped, request dispatched, task submitted, credential read, checkpoint
imported or unknown replayed. SQLite opens `mode=ro`/`query_only`, and one `BEGIN`
pins the original witness, channel rows, full canonical intents and source
dependency anchors to the same read view. Source files must be owned private
0600 files under 0700 directories. Errors are fixed codes without private paths
or raw exceptions. The returned report is a private inventory: do not publish
deployment pins or accompanying checkpoints/spools.

Each check records `checked` facts or a fixed `missing` reason:

- Original witness ID/incarnation/account/owner/canonical-authority binding.
- Full original inbox/task tree/cursor/context/outbox and source anchors using
  the existing strict audit-checkpoint contract. Dependency anchors are hashes,
  **not copied native history or full runnable execution migration**.
- Full original canonical tickets, outcomes and recomputed durable receipts
  against witness intents. Unknown/unsettled intents remain explicitly unclosed;
  no caller report can make them terminal or pending. An original witness intent
  independently reconciled with the same persistence receipt is closed while its
  canonical unknown outcome remains unchanged. That local fact still does not
  let this inventory attest the independent proof or external quiescence.
- Owned remote spool authority, exact original ticket and full outcome compared
  with canonical persisted state. A retained return without canonical ACK is
  unconfirmed, not a receipt of accepted persistence. Missing supplied spools and
  uncovered canonical dispatches remain visible; the collector cannot discover
  unregistered copies or guarantee that the caller supplied every machine.
- Private audit checkpoint validated against an **external retained SHA**, then
  exact selected rows/schema/dependency comparison with the current read view.
  A current writer can invalidate an older snapshot; no freeze is inferred.
- Actual owner-held `ChannelSupervisor` poll/send/maintenance Thread handles,
  started state, current liveness and stop event. A JSON snapshot, candidate bool,
  disappeared PID, timeout or unstarted group is not accepted as these handles.
  Stop timeout leaves the original live caller and intents unclosed. Thread
  return is a local lifecycle fact, not independent proof of remote quiescence.

The collector does not call `stop` or infer natural external-operation completion
from a dead thread. A trusted owner runtime must separately observe the actual
callbacks returning, full persistence and admitted-caller inability to resume.
Tests exercise real owned threads with a held synthetic callback: timeout is
missing, release permits original outcome persistence and thread return. This
is not proof about a production iLink request.

## What remains missing even with all local facts checked

Every report always has `migration_ready=false`, `grant_authorized=false` and
independent quiescence/history-migration/fault-domain-arbitration checks missing.
The [audit format](CHANNEL-CHECKPOINT.md) still cannot satisfy
`mesh-channel-handoff-proof/1`. Ledger terminality or a matching SHA is not an
attestation of the external world. Candidate availability booleans do not feed
this collector and are not a source of grant authority.

This slice prepares **communication-carrier-only placement with the original
authority/history retained**. It does not move Leader sessions or make an
unavailable original authority writable somewhere else. If witness/state remain
on VPS, moving the carrier does not fix VPS partition availability. Independently
available communication authority/state/arbitration and actual old-call closure
remain needed for [COMMUNICATION-CARRIER-HA](COMMUNICATION-CARRIER-HA.md).

Native Shell/files/network/MCP and unrelated new local work remain available.
The report closes nothing and gates no host-wide capability. Shared-channel
unknown identities are preserved without blocking independent new node work.
