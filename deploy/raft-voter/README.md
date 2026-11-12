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

Only `{format:"amita-quorum-digest/1",operation_id,digest}` enters normal log
entries. `digest` is 64 lowercase hex characters. No business text, credentials,
chat or payload is accepted. Duplicate original operation/digest is idempotent;
conflicting committed digests retain a hold instead of overwriting receipts.

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

## Cloudflare candidate and budget gate

`cloudflare-worker.mjs` imports the static compiled WASM module; only its DO
constructor calls generated `initSync`. It uses SQLite `transactionSync` and
`storage.sync()`, one serialized driver, and system alarms rather than an interval
or a resident-WebSocket trick. Outgoing packets flush outside the mutation queue;
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

Remaining integration: real three-host transport and existing authority binding,
Free DO startup/CPU/budget measurement, portable snapshots/compaction, S2 leases
and effect fencing, and both full business state copies. This candidate neither
opens the existing business DB nor claims those outcomes.
