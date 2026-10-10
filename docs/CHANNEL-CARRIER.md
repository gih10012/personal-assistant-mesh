# Channel carrier candidate and once-only fence (PAM-009a/009c)

2026-10-10 source slice, **not integrated into production Channel/API/config,
not deployed HA and not an import/handoff implementation**. Existing production
VPS-only poll/send is unchanged. Synthetic tests establish state-machine
boundaries, not actual iLink, old-holder shutdown, or another node's admission.
ClawBot remains an owner↔Leader capability, not the Leader workspace.

## One designated authority, several possible carriers

`CarrierWitness(store, witness_id, handoff_verifier=None,
operation_verifier=None)` creates four opt-in tables and one send-identity index
in the supplied Store writer transaction. Instantiate it **only at the chosen
witness**, never independently on each carrier's local Store. A remote carrier
calls that same witness through authenticated RPC. Different local witnesses
are not consensus, even when their configured names match. A durable incarnation
pins the actual ledger; replacing a DB is not automatic renewal permission.

Placement is a deployment choice, not permanently VPS/Leader/Linux-specific.
A laptop witness could permit other carriers when the VPS is unavailable;
losing that laptop witness instead closes new shared-account IO. A separate
existing independent witness can improve fault coverage without new spend,
but none is installed by this source slice. Moving a witness requires an
exclusive, controlled stop/drain/state transfer; copying its DB to two writable
nodes creates split brain. Same-incarnation snapshot rollback or clone cannot
be detected reliably by this module. No default auto-migration is provided.

With just two nodes, mutual ping cannot safely grant both partition availability
and a unique unfenced iLink account. Neither timeout, process death alone, nor
a local `leader` row proves an admitted remote HTTP operation has ended. The
current iLink 40/15-second urllib values are socket timeouts, **not hard total
operation deadlines**. This implementation never uses them as stop evidence.

## Candidate, holder, and original authority are separate

Trusted private deployment code `enroll(account_binding, owner_ref,
canonical_authority)` binds the account and starts frozen, epoch 0/unowned.
It does not load credentials, ingest an account's pending messages, or start a
receiver. Candidate `contract` is exactly:

```json
{
  "protocol": "mesh-channel-carrier/1",
  "node": "candidate-node",
  "owner_ref": "opaque-owner-reference",
  "account_binding": "64-character-lowercase-sha256",
  "canonical_authority": "original-task-authority",
  "private_credential_ref": "opaque-private-reference-not-token-or-account-json",
  "revision": 1,
  "runtime": {
    "adapter": "ilink/0.3.0",
    "fence_protocol": "mesh-channel-carrier/1",
    "transport_available": true,
    "private_state": true
  }
}
```

`candidate(actor, contract, ttl=60)` validates the authenticated actor, bound
owner/account/authority, monotonically revised report and 1–300-second TTL.
Runtime booleans are **node reports**, not attestation or permission. Replacing
a holder never changes `canonical_authority`. Internal `cloud` remains ali-vps;
this is unrelated to official `openai-codex-cloud` enrollment.

## Admission → outcome → original durable persistence

```text
prepared → dispatched → returned → committing → committed
                └──────────┴──────────┴───────→ unknown
prepared + freeze → aborted
unknown/unsettled + frozen + trusted original-effect proof → reconciled
```

`begin` durably binds account, original authority, holder/epoch, operation ID,
kind, request digest, and the full original identity **before external IO**:

- Send: `{authority, outbox_id, client_id}`. A changed operation ID cannot
  resubmit the same original send, even after a new holder/epoch. Only an
  already-justified original outbox attempt with its distinct client ID can be
  a distinct send. The witness never invents that attempt or retries a send.
- Poll: `{authority, cursor_sha256}`; raw cursor/context/chat stay private in
  the original channel ledger/checkpoint, not in this witness. A completed empty
  poll may use the same cursor in a new operation; a repeated operation cannot.

`dispatch` persistently consumes the once-only permission and returns a private
token. Only a **fresh, verified ACK** can invoke the transport callback. A lost
begin ACK cannot be converted into a dispatch; a lost dispatch ACK leaves an
unsettled intent but never invokes IO in the wrapper. Prepared intents can be
aborted by freeze because dispatch was never admitted. A delayed dispatch ACK
or paused admitted caller remains represented by its blocking intent.

`returned` records an outcome digest, then `commit_authorize` grants one original
canonical-persistence callback. `committed` records its durable receipt digest.
The unresolved intent protects the check-to-persist interval: another holder
cannot be granted while it exists. Freeze/expiry closes **new external IO**;
the same admitted operation can still drain its original outcome into its
original authority. This is not instantaneous cancellation of iLink requests.
Known business errors should be normalized outcomes; a callback exception is
unknown. Accepted send is still not verified phone delivery.

`CarrierFence(client, term).run(operation_id, kind, identity, request_sha256,
call, persist)` performs these gates, checks witness/incarnation/account/holder/
epoch/identity receipts, and never retries either callback. Client must implement
`request(path, body)` against the configured witness with the holder's credential.
`persist(result, ticket)` must atomically store the original cursor/inbox/task/
outbox/result mapping and return a lowercase SHA-256 durable receipt. It must not
turn an old message into a new task/authority. Raw returned batches and original
native histories require private recovery storage; a digest is not their copy.
If a canonical write happened but its ACK is lost, the wrapper reports unconfirmed
and does not repeat it. Original journal readback is the reconciliation path.

Unknown sends retain their original identity and block handoff, but do not block
unrelated new sends **under the same holder/term**. An unknown poll blocks another
poll until its original operation is reconciled, to avoid overlapping pulls.
This source-only conservative poll failure boundary is not a deployed recovery
loop. There is no "unknown → pending" transition or automatic new-ID replay.

## Grant is gated by real stop and complete state, not expiry

Trusted operator `freeze(account_binding, expected_epoch)` closes new admission;
it does not stop services. `grant(account_binding, expected_epoch, holder,
seconds, proof_ref)` verifies external evidence outside the writer lock, then
CAS-checks the exact frozen term and absence of unsettled intents. The candidate
must still be fresh and available. Epoch/checkpoint revision advance atomically.

No verifier installed means **no grant**, including initial bootstrap. The trusted
`handoff_verifier(before_term, holder, proof_ref)` must independently verify and
return exact `mesh-channel-handoff-proof/1` bindings and all of:

- Matching account, original authority, from holder/epoch, to holder, proof ref;
  strictly newer checkpoint revision and externally pinned checkpoint SHA.
- `migration_ready`, `original_history_closed`, `original_execution_closed`,
  `previous_carrier_stopped`, `external_operations_quiescent` all true.

The initial bootstrap must account for any legacy unmanaged poller too. Client
JSON booleans, a model's claim, a service PID, or ping failure cannot implement
this verifier. [CHANNEL-CHECKPOINT.md](CHANNEL-CHECKPOINT.md)'s existing audit
format enforces `migration_ready=false`, and **cannot satisfy this proof**.

Unsettled intents are not waived merely because a stop proof is supplied.
`reconcile(account_binding, operation_id, proof_ref)` requires frozen state and
trusted `operation_verifier(receipt, proof_ref)` returning exact original ticket
digest/ref plus verified external-operation **and admitted-caller** quiescence, preserved original
identity, complete canonical persistence and its SHA. Reconciliation only closes
the original journal; it never dispatches/rewrites/retries the external effect.
The original send identity remains reserved afterwards. Current iLink does not
supply a hard epoch fence or a generic quiescence proof; no such actual verifier
or automatic failover is claimed here.
No active HTTP socket is not enough: a paused caller holding a dispatch ACK must
be proven unable to resume its admitted callback, including a delayed local write.

## Minimum integration owned by Root (still pending)

1. Configure one witness and private account/authority binding explicitly.
   Keep it opt-in, pin identity/incarnation; do not mirror a writable witness.
   Operator bootstrap/handoff verifier must use actual stop/drain and closed
   original history/execution evidence, not the audit-only export or posted flags.
2. Authenticate a dedicated node-bound carrier role and route
   `/channel-carrier/{candidate,renew,begin,dispatch,returned,commit_authorize,committed,unknown}`
   to `witness.request(authenticated_node, action, payload)`. These payloads cannot
   claim `actor`. Enrollment/freeze/grant/reconcile are not exposed by this method.
3. Channel poll: gate `transport.poll` before IO; fence the original atomic
   `store.ingest` + cursor/context/task mapping before persistence. Account
   constructor pending/context imports must not run independently on candidates.
4. Channel send: reserve one original outbox/client identity before dispatch;
   preserve a local intent alongside the witness intent. Coordinate `next_send`
   claiming with admission so an unavailable witness never silently turns pending
   into replayable submitting. Preserve `finish_send` known/unknown semantics and
   canonical authority. The callback wrapper alone is not this Store integration.
5. Prove old receiver loss-of-authority, original outcome/cursor/history closure,
   private checkpoint install and single-carrier runtime admission in isolation;
   then controlled real handoff. No second production poller for testing.

Every node's native Shell/files/network/MCP, autonomous reconnect/diagnosis and
independent new local work remain available. During a shared-channel partition,
owner Chat/terminal/other independent entrances can still be used; no host-wide
allowlist or default tool interception is introduced. These guarantees cover
**managed callbacks using this witness**, not arbitrary native network programs
that bypass it or trusted deployment code falsely attesting quiescence.
