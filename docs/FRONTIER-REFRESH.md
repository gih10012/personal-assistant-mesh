# Durable frontier refresh for the continuous Leader

PAM-007e / PAM-010a, 2026-10-14. This adds a directly runnable observation
adapter and model-advice consumer, not a new Leader/harness. Production 3198
Worker, native history, account settings and timers are unchanged by this branch.

## Environment-neutral contract

Any node can implement these actions using its own scheduler/storage/network:

- `due/status`: read the original schedule and current receipt, without network
  access or initializing/migrating other Mesh/native databases.
- `refresh(request_id)`: durably bind the original goal/task/project, policy and
  explicit read-only observation request; retain each source attempt and complete
  body/hash. Return `collecting`, `partial` or `completed` plus next due/retry.
- `run-due`: no-op before due; resume the original incomplete read-only request,
  or derive a stable ID from the prior persisted due slot after completion.
- `receipt(request_id)`: read the original receipt, never launch an upgrade,
  inference, model switch, task replay or replacement session.

The cadence is owner-configured within 3–5 days, default 4 days. A required-source
failure does not pretend the whole review is current: `partial` keeps the old
attempt and retries the **same** request after the configured retry interval.
After a process interruption, a local execution lock—not PID/mtime speculation—
prevents concurrent local refresh. Once the lock is released, the same original
read-only intent can resume. Completed IDs only return the existing receipt.
Changing a request's config under its old ID is rejected. Changing the original
schedule's goal/task/project namespace is also rejected; it does not reset Goal.

This is not exactly-once distributed execution. Public GETs can safely be repeated
after an interrupted source read, with old attempts retained. This permission
does **not** extend to model POSTs, GUI sends, deployments, or unknown business
effects. Their existing journals and reconciliation rules remain authoritative.

## Minimal local adapter

`assistant_mesh.frontier_refresh` is an optional Python/POSIX/SQLite example.
Its dependencies are standard library plus existing Mesh private-file helpers.
Other environments need this contract, **not** Python, flock, systemd or a copy
of this Linux installation. The owner must choose/install a scheduler separately.
Native Shell/files/network/browser/MCP continue to work whether refresh is due,
offline, missing, partial or invalid.

Create a private 0700 state/config parent outside Git. Adapt
[frontier-refresh-owner.json.example](../deploy/frontier-refresh-owner.json.example)
using the **original** goal reference/task/project and a private 0600 config.
The source watchlist is configurable; it is not a model whitelist. Required
coverage includes models, harness, skills and protocol. HTTPS URLs cannot carry
credentials/query/fragment, redirects stay on configured source hosts, raw
bodies are bounded at 4 MiB per source. `timeout_seconds` is a socket timeout,
not a claim of a strict whole-download wall deadline. An explicitly configured
loopback HTTP proxy affects this opener only; no host proxy/default route changes.
TLS/DNS/that proxy remain the local transport's trust boundary; this is not an
independent upstream execution or DNS-pinning attestation.

```sh
python3 -m assistant_mesh.frontier_refresh --config /PRIVATE/owner.json due
python3 -m assistant_mesh.frontier_refresh --config /PRIVATE/owner.json run-due
python3 -m assistant_mesh.frontier_refresh --config /PRIVATE/owner.json refresh --request-id ORIGINAL-CHECK-ID
python3 -m assistant_mesh.frontier_refresh --config /PRIVATE/owner.json receipt --request-id ORIGINAL-CHECK-ID
python3 -m assistant_mesh.model_advice --refresh-dir /PRIVATE/frontier-state
```

Use the already installed frozen CLI/runtime, then let the original Leader
read/use it from its existing native thread. `run-due` is suitable for an
existing authorized scheduler or native maintenance turn. This implementation
does not install/enable timers, create a Chat scheduled task, invoke another
agent, or silently mutate the old Worker merely to add advice.

Private SQLite stores the schedule and attempts. Content-addressed 0600 blobs
store complete original source bytes, model evidence and immutable receipt
revisions, with fsync before ledger publication. Readback checks receipt/evidence
and source byte lengths/hashes. Original partial/error/interrupted attempts and
receipt files remain. `receipt_sha256` hashes canonical receipt JSON excluding
that hash field itself, not the complete serialized file including its own hash.
Source/model-evidence SHA values cover their complete raw bytes.
SHA verifies stored bytes, not the source's accuracy or
permission to execute its suggestions. Source summaries are short navigational
**untrusted data**, not executable instructions. A changed content hash can be
cosmetic; the Leader reviews actual version/content before proposing an upgrade.

## Route, quality, cash and freshness are separate

Optional `local_metadata` reads only selected harmless Codex catalog/config
fields or hashes reviewed wrapper/skill files. It never runs a wrapper (even
`--help` can mutate state), reads auth.json, copies identity/base instructions,
syncs history, submits a model call or renews credentials. Catalog output retains
the **original** fetched_at, client version and file mtime. A newly observed old
cache is still an old cache, not live model/quota/effort execution verification.

`baseline_evidence_file`, if provided, reuses the existing private
[model evidence contract](MODEL-FRONTIER-POLICY.md) **unchanged**. Fetching new
documentation cannot renew its old benchmark, availability, cost, privacy,
latency or quota observation times. Matching physical model/effort reference
quality can be an explicit assumption in that original evidence, not a gateway
proof. Free/included-subscription incremental cash may be exactly zero; quota,
privacy, latency and tool reliability are distinct, and paid API is not free
merely because the owner has Plus. No new keys, spend or paid fallback are used.

Without a benchmark snapshot, the package has **zero numeric points**. Actual
catalog model/effort pairs enter advice with unknown/unverified access and
quality, not made-up AA scores, epsilon prices or an evergreen ranking. A
`no_qualified_available_route` here means no proven **numeric shortlist** under
these inputs; it does not forbid the Leader's native models or local work.
The Leader can inspect official guidance and scoped actual evidence rather than
treat this optional numeric adviser as a mandatory intelligence gate.

`model_advice` accepts optional private `model_refresh_dir`; its CLI has
`--refresh-dir`. An existing explicit `model_advice_file` still wins for model
facts, while fresh update metadata is attached separately. If only the optional
refresh is unavailable, an otherwise valid explicit evidence file remains usable.
Reading advice never runs refresh. Unknown operations retain a held original
identity, even when fresh documentation suggests another model/node.
`decision_sha256` still covers the pure model-frontier decision; its scope is
explicit when refresh metadata is attached. `advice_sha256` covers the complete
decorated advice. Prompt summary bounds records and reports source counts rather
than inserting every public document into native memory.

## Actual observation, not fixture or production adoption

The one actual read-only request `PAM-007e-frontier-observation-20261014-v1`
completed 2026-10-14 01:47:38 UTC, seven public sources all HTTP 200, original
bodies totaling 1,162,488 B retained privately. Next due is 1792288058.8299882
(2026-10-18 01:47:38 UTC); a subsequent `run-due` was a no-op. Receipt SHA:
`f3545e63faefd8bc162f705df13df33087cf71635e9613eeab730f932e49a402`.
The original observation preceded the small later content-diff/heading-reader
enhancement; its immutable receipt was not rewritten or refetched to claim that
enhancement ran. Current `model_advice` read/hashed those same complete bytes.

Observed primary versions were Codex 0.162.1, Pi 1.2.0, MCP 2026-07-28,
A2A 1.0.1 and OpenAI skills commit 49f948faa9258a0c61caceaf225e179651397431.
These are source observations, **not installed upgrades**.
[Codex release](https://github.com/openai/codex/releases/tag/rust-v0.162.1),
[Pi release](https://github.com/earendil-works/pi/releases/tag/v1.2.0),
[MCP release](https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28),
[A2A release](https://github.com/a2aproject/A2A/releases/tag/v1.0.1),
[skills source](https://github.com/openai/skills/commit/49f948faa9258a0c61caceaf225e179651397431).

Current official guidance also matters for routing: GPT-5.5 retirement from
Chat/Work/Codex on October 14 does not retire its separately billed API. Sol
Ultrafast is plan-limited, not a new free Plus fallback. Cached listings cannot
override either fact. [Official changelog](https://learn.chatgpt.com/docs/changelog)

Actual private metadata projected two existing Codex caches, one selected native
config and three wrapper/skill digests; no credentials, account identity, private
history or inferred quota were copied. Actual `model_advice --refresh-dir`
returned 91 catalog variant records, no intelligence-score fields and no forced
recommendation, with all 7 source bodies hash-checked. The numeric quality/actual
quota/selected physical dispatch and **original Leader's actual consumption**
remain separate next acceptance steps. Root must review/freeze/install and wire
the existing scheduler/native turn; no actual production timer or 3–5 day cycle
has elapsed/proven by this first observation.

Seventeen focused tests cover original-context/ID continuity, lock/interruption,
source failures/cadence, private immutable bytes, no credential projection,
unchanged old observation times, zero-cost and unknown safety, real adviser
consumption and bounded HTML projection. The 151-test frontier/advice/provider/
gateway/native-Worker related group passed 18.883 s. An earlier test command named
a nonexistent `tests.test_worker`; its import failure was retained and the real
modules were selected, not weakened. These fixtures are not live model scores or
full Mesh autonomy. No GUI/server/network/production state changes occurred.
