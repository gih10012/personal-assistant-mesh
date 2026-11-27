# Real Raft WASM host candidate

This isolated host runs the actual upstream raft-rs `Voter` from successful
[run 43153105993](https://github.com/gih10012/personal-assistant-mesh/actions/runs/43153105993),
source `9ed4aa8`, module SHA256
`5478f7883b27f9c1c52f060f957095193402a853cdb7c35eb5ac12484993ce50`.
The previous research branch and its native/WASM single-vote receipts remain
unchanged. No Rust rebuild, Cloudflare deployment, business ledger or model is
part of this directory. Source code and candidate files are distinct from a
qualified production quorum or business-effect fencing.

## Runtime and persistence

`driver.mjs` is the same pure ESM driver on Node and a SQLite Durable Object.
Members are exactly `[1,2,3]`. There are no independent CAS witnesses. The
generated API has no propose/status/compact export: `codec.mjs` encodes actual
upstream protobuf `MsgPropose`/normal entries, checked against the
[pinned official proto](https://github.com/tikv/raft-rs/blob/v0.7.0/proto/proto/eraftpb.proto).
The Rust RawNode performs elections, replication, term/vote rules and majority
commit. Local status exposes persisted term/commit/applied, original digest
receipts and an explicitly nonauthoritative leader hint. It issues no lease,
coordinator permission, ReadIndex guarantee or Shell/OAuth/iLink fencing grant.

Normal log entries support opaque digest receipts and controlled business
writer transfers. Existing `{format:"amita-quorum-digest/1",operation_id,digest}`
operations accept a 64-character lowercase hex digest. Repeating the original
operation and digest returns the original receipt. Conflicting committed v1
digests retain a hold. Legacy receipts confer no writer epoch.

Before calling `persisted(sequence)`, every nonempty Ready HardState/log batch
is atomically saved and synchronized. No outgoing message is sent at this point.
The matching `persisted()` output is normalized, LightReady commit-index changes
are saved back into HardState, and digest receipts, applied index, authenticated
outbox and receive deduplication are saved together before any ACK or transport.
Empty committed leader/no-op entries advance applied too. Only after this durable
barrier does `applied(index)` release RawNode's apply gate. Empty Ready storage
batches do not cause an unnecessary first save.

A failed store poisons the volatile RawNode. Reconstruction uses the original
stable JSON and first drains committed entries. An unfinished persisted Ready
retains its original input and replays that input after restore; it never claims
that an unknown response was delivered. Peer retries use the same signed sequence.
Once produced, outbox packets remain durable until an authenticated matching ACK;
network errors never silently remove them. ACK deletion for both peers is batched.
Transport never holds the driver mutation queue across a reciprocal request.

Wire packets bind format, group, scope, sender, recipient, monotonically ordered
per-recipient sequence and protobuf bytes with pairwise HMAC-SHA256. ACKs are
signed too. Replay permits only the identical original packet in a bounded
64-receipt window. Messages are at most 32 KiB, HTTP bodies 48 KiB, the normal
outbox 256 packets, uncompressed log 2048 entries/1 MiB, state 4 MiB. Limits stop
the affected candidate; there is no paid fallback or business-state cleanup.

The unchanged port cannot compact or bind digest FSM snapshot metadata. Remote
snapshot/configuration-change messages are rejected and not ACKed; an unexpected
nonempty Ready snapshot is retained durably in a hold before `persisted()` can
release messages. u64 values are parsed as BigInt and limited to JavaScript's
exact integer range before passing the old JSON API. A proper upstream port
snapshot/compaction and u64 schema remain required before unbounded operation.

## Controlled business writer transfers

`POST /raft/manage` with `action: "propose"` accepts an
`amita-replica-writer-transfer/1` command on a voter with the optional private
`owner_command_key` configured. This bearer may submit transfers and read
status, owner and original receipts. The existing operator may manage the
voter, submit opaque digest proposals and read the same local views. Configure
a distinct 64-character lowercase hex owner key on explicitly chosen command
entrances only. All three trusted members must understand the wire format
before a holder submits the first typed entry.

The typed command contains the exact original `operation_id`, voter `group`
and `scope`, `role: "replica_writer"`, `previous: {owner,epoch,head}`,
`next_owner`, `next_epoch`, `checkpoint` and `checkpoint_sha256`. The first
explicit transfer uses `previous.owner = null`, epoch and head index zero,
and a head SHA256 of 64 zeroes. It identifies the sealed source holder in the
checkpoint. Each later command must name the current holder, epoch and head
pair and increment the epoch by exactly one. The new head binds the actual
Raft entry index and SHA256 of the complete canonical request. The Raft term
is recorded separately on the immutable receipt.

Each checkpoint binds these fields:

- `authority`, `incarnation`, `generation`, `manifest_sha256`,
  `sealed_journal_head` and `typed_rows_sha256` identify the sealed data.
- `external_dependencies_manifest_sha256`, `worker_claim_protocol`
  (`amita-worker-claim/1`) and `retained_effects_sha256` preserve evidence
  references for outstanding claims, native work, external effects and unknowns.
- `source_holder`, `source_seal_sha256` and two distinct `holder_receipts`
  identify the old writer seal and the independent disk receipts. The array
  contains exactly the source and target, sorted by ASCII holder ID, each
  with `{holder,sha256}`. Every digest uses 64 lowercase hex characters.

Hash the UTF-8 bytes of compact JSON with sorted object keys at every level
and array order preserved. The checkpoint hash covers every field, including
both receipt digests. The request hash additionally covers the format, original
operation, group, role, scope, predecessor and successor. A submission response
of `submitted_not_commit_receipt` can precede loss of the original operation.
After a lost response, read `action: "receipt"` with that `operation_id`.
A missing local receipt returns `unknown_here`. A recorded receipt identifies
the exact request hash. An eligible committed transfer returns `applied`,
and a stale predecessor returns a durable `rejected` receipt. Keep the same
operation identity for reconciliation. Previously committed IDs stay occupied
across both formats.

`action: "owner"` returns the member's locally durable applied current state
and applied index. A historical grant remains queryable after its successor.
The voter stores the current compact checkpoint, receipt, applied index and
peer outbox atomically. Reconstruction checks the complete retained applied
log against the owner state and receipts before continuing. A missing row
requires recovery of the original store. Existing snapshot limits apply.

These commands commit control data. Private verification of the actual signed
disk receipts and old writer seal, coverage of retained effects, and the
business SQLite activation/fencing boundary belong to the subsequent handoff.
A local read may lag the quorum. Business consumers must keep their original
execution guards. All status responses continue to report `business_fencing:
false` and `ha_qualified: false`.

### Prepare the target SQLite candidate

On the target, use an authenticated Source witness for the sealed journal head.
Compare it with the target's passive replica witness and save the six matching
fields from `assistant_mesh.business_checkpoint.compare_checkpoints` as private
JSON. Use a locally trusted empty template from the matching application
version with the declared tables and their indexes. Prepare the candidate in a
private directory with a fresh file name:

```sh
python3 -m assistant_mesh.business_recovery \
  --replica passive.db --schema-template empty-schema.db \
  --checkpoint sealed-checkpoint.json --destination private/candidate.db
```

The command imports typed rows at the exact sealed position and recomputes
their digest from the new SQLite file. It publishes read-only via atomic
no-replace creation. Scoped `meta` keys, original task IDs, unknowns and
declared native history chunks keep their original values. The receipt reports
the SQL schema digest separately. Activation still requires the applied writer
grant and verification of external dependencies, runtime configuration and
retained effects. Keep this candidate outside configured authority paths.

The focused local acceptance uses `node --test owner-smoke.mjs` in the assembled
candidate. In a source checkout set `AMITA_RAFT_ARTIFACT` to the original
download directory. The test uses the pinned WASM, real local fsync/SQLite
stores, authenticated management calls and an interrupted apply boundary.

## Candidate assembly

`build-candidate.mjs` takes a new absolute private destination. It copies only
the explicit host source list at its committed Git revision, the five pinned
WASM/glue/type/ESM files, original hosted manifest/recovery receipt, and public
license files allowlisted and hashed by that manifest. It never traverses or
uploads private configurations, source histories or native binaries. Its new
`CANDIDATE-MANIFEST.json` binds all copied bytes and the original hosted manifest
hash. No Rust toolchain or account access is needed.

```sh
node deploy/raft-voter/build-candidate.mjs --artifact-root /ABSOLUTE/ORIGINAL-DOWNLOADED-ARTIFACT --destination /ABSOLUTE/NEW-PRIVATE-CANDIDATE
```

`node smoke.mjs` in the assembled candidate runs one bounded actual three-voter
acceptance with in-memory authenticated transport, file fsync plus real Node
SQLite WAL/FULL stores, restart, minority isolation, two-member replacement
election and log reconciliation. It performs no network access. Run it only in
Root's coordinated Click window. It does not repeat the old single-vote CI case
and does not claim real Cloudflare or cross-host acceptance.

## Node entrance

`node node.mjs /ABSOLUTE/EXISTING_PRIVATE_CONFIG.json` requires Node with
`node:sqlite` if SQLite is selected. The configuration must be owned by the
current user, mode 600; state directory mode 700. File writes use exclusive
temporary files, fsync, atomic rename and directory fsync. SQLite uses WAL and
`synchronous=FULL`. Both hold a process-exclusive lock; existing/unknown locks
are never stolen. Running DB files are not restored/copied.

Private configuration structure (angle-bracket values are invalid placeholders,
not usable public credentials):

```json
{
  "storage": "file",
  "state_directory": "/ABSOLUTE/NEW-PRIVATE-VOTER-STATE",
  "artifact_directory": "/ABSOLUTE/CANDIDATE/artifact",
  "listen": {"host":"127.0.0.1","port":18763},
  "voter": {
    "group":"<OWNER-APPROVED-GROUP>","scope":"<EXACT-CONTROL-SCOPE>","member":1,
    "links":{"2":"<EXISTING-PAIR-1-2-HMAC-HEX>","3":"<EXISTING-PAIR-1-3-HMAC-HEX>"},
    "operator_key":"<EXISTING-NARROW-OPERATOR-HEX>",
    "peers":{"2":"https://<EXISTING-PEER-2-INGRESS>/raft/peer","3":"https://<EXISTING-PEER-3-INGRESS>/raft/peer"},
    "tick_ms":5000,"start_enabled":false
  }
}
```

Alternatively `listen` is `{"socket":"/ABSOLUTE/PRIVATE/voter.sock"}`; mode 600
UDS is not replaced if already present. HTTP binds only loopback. Root must bind
the existing authenticated HTTPS ingress to these routes before cross-host
transport works. Configurations require explicit existing pairwise secrets;
the candidate generates/copies no private credentials. `operator_key:null`
disables management. No runtime public/default token exists. Outbound HTTPS
validates normal TLS and rejects redirects. On the laptop use the owner's
command-local proxy environment (with Node's supported env-proxy mode); do not
change global Git/system proxy or forward that laptop proxy to other devices.

`POST /raft/peer` accepts only the exact signed peer envelope. `POST /raft/manage`
requires `Authorization: Bearer <existing operator key>` and exact
`{group,scope,member,action}`. Actions are `status`, `enable`, `disable`; `propose`
additionally requires `proposal` in the digest-only schema above. A proposal ACK
means submitted locally, never committed. Read the original operation's receipt
in status for actual local durable application. S2 must separately bind that
commit to its existing term, data copies, original operations and effect fences.

### Outbound-only hosted voter

On holders 1 and 2, retain their mutual HTTPS `/raft/peer` endpoints and set
`peers["3"]` to the fixed hosted HTTPS URL ending in `/raft/exchange`. On
member 3 set `peers` to `{"1":"accept_exchange","2":"accept_exchange"}`. This
configuration has no egress URL for either private holder. Keep the original
pairwise keys, group, scope, member IDs and stored states. Exchange routes
require `start_enabled:false` for fresh stores. Existing stores retain their
durable enabled state. Disable an existing candidate before switching routes.

For a bounded trial, set the optional private integer `qualification_until_ms`
on all three voters to the same absolute Unix epoch millisecond deadline.
Deploy the stopped candidate, verify all three status responses show the
intended deadline, then enable each voter through its existing management route.
The hosted voter caps its alarm at the deadline. At expiry each voter stops
participating, preserves its queue and receipts, and rejects re-enablement.
Continue using status to inspect the result. A later trial needs an explicit
private configuration change. Keep the clocks synchronized.

Each enabled holder initiates one HTTPS POST per tick, including idle ticks.
The signed request binds its format, group, scope, sender, recipient, expiration,
one optional original outbound envelope and the signed ACK of the last durable
packet from the edge. The holder signs a 15-second validity window, capped at
its trial deadline. The edge accepts an expiry up to 30 seconds ahead of its
current time. The public verifier bounds the body and authenticates the request,
packet and ACK before invoking the one configured DO. Its response contains
the matching ACK and at most the original queued envelope at the ACK cursor's
next sequence. Receipt and queue changes share the existing durable save
barrier. Lost responses, restarts and replay reuse the original sequences.
Only a matching signed ACK retires a queued packet.

`node exchange-smoke.mjs` in the assembled candidate exercises the real pinned
WASM, a file holder and SQLite voter, the public verifier and DO handler over
in-memory HTTP. The holder and edge must commit without the other holder,
including a lost response, a holder restart, scoped replay and trial expiry.

## Cloudflare candidate and budget gate

`cloudflare-worker.mjs` imports the static compiled WASM module; only its DO
constructor calls generated `initSync`. It uses SQLite `transactionSync` and
`storage.sync()`, one serialized driver, and system alarms rather than an interval
or a resident-WebSocket trick. The public Worker authenticates signed peer packets
and checks the operator bearer before forwarding to the DO. Bodies are read
once with a fixed size limit, and invalid traffic never initializes the voter.
Worker ingress quota is still consumed by rejected external requests. Outgoing packets flush outside the mutation queue;
only one bounded packet per peer is sent per invocation. Alarm retries cannot
accelerate persisted tick time. Reconstructed RawNode legitimately returns as a
follower; in-memory candidacy/leadership is not claimed to survive DO eviction.
The remaining Node candidate can still start the actual majority election.

`wrangler.candidate.json` only defines a new SQLite class migration. It disables
workers.dev and previews and contains no account ID, route, credential or private
state. Root alone may inspect the real account, select a separate candidate
namespace, provide the existing private `VOTER_CONFIG_JSON` binding and route,
and deploy. `DEPLOYMENT_PLAN=free` is a fail-closed assertion, not evidence of
the account's plan. Do not deploy to Paid or enable credit/overage fallback.

Official pages read 2026-11-12:

- [DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/):
  Free SQLite only; 100,000 requests/day, 13,000 GB-s/day, 5 million rows read/day,
  100,000 rows written/day, 5 GB/account. Alarm invocations are requests and
  each setAlarm is one row written. The account allocation is shared.
- [Workers limits](https://developers.cloudflare.com/workers/platform/limits/):
  Free Worker HTTP CPU is 10 ms, with 100,000 requests/day and 128 MiB memory.
- [DO limits](https://developers.cloudflare.com/durable-objects/platform/limits/)
  separately lists a 30-second DO invocation default and says Worker limits
  also apply. This candidate conservatively requires actual startup/handler
  measurements against the 10 ms gate; it does not assume a free CPU exemption.
- [SQLite persistence](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/):
  synchronous SQL transaction plus explicit sync before outgoing messages.

The pinned port hardcodes heartbeat_tick=1 and election_tick=10; it has no setter.
At 5-second ticks actual algorithm election is approximately 50–100 seconds and
leader heartbeats are 5 seconds. A 10-second heartbeat requires a future genuine
upstream artifact change to heartbeat_tick=2; the host does not pretend it changed.
With no proposal/retry/status traffic, one CF follower has about 34,560 DO
requests/day (alarm+heartbeat); a CF leader about 51,840 (alarm+two responses).
The current one-row save/ACK batching estimates 69,120/86,400 SQL row writes/day
respectively, including alarm writes. SQL save checks the actual cursor reports
exactly one written row, otherwise rolls back and closes the candidate. The
driver also counts state/alarm writes and input events and holds before its
90,000/day local allowance. These are per-candidate estimates/guards, not proof
of free account headroom, CPU, cold start or complete platform accounting.
Other account traffic, cold reconstruction, retry bursts and proposals consume
the remaining margin. Quota exhaustion holds only this candidate; never buys
capacity. Root must qualify actual CPU, row accounting and account-wide budget
before enabling continuous production ticks. The approximate two-minute Goal
remains an unfulfilled real deployment/failover requirement.

For two outbound-only holders polling every five seconds, the steady full-day
estimate is 34,560 Worker requests and 51,840 DO invocations including 17,280
alarms. With one durable event save per poll, one save per tick and one alarm
write per tick, the estimate is 69,120 rows written. Elections, log changes,
activations and extra Ready batches can add writes. Startup, status and rejected
traffic also draw on shared account quotas. One packet each way per holder tick
limits recovery throughput. A sustained backlog or an offline voter can reach
the existing 256-packet outbox limit. Inspect queue counts during the trial.

The separate laptop/VPS/Cloudflare trial observed a term-1 VPS leader followed
by a term-2 laptop leader in 96.325 seconds. That trial exercised the earlier
digest voter. This typed command adds an applied control-state record for a
future controlled handoff. Signed disk and old-writer-seal verification,
production business writer activation, portable snapshots/compaction and
sustained Free-tier qualification remain separate integration work.
