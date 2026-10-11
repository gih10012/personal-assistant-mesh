# Optional model advice in the continuous Leader

2026-10-11 implementation slice. This is **source wiring**, not evidence that
the production3198 Worker has loaded it or changed its actual model. Existing
Codex/Pi sessions, native settings and managed dispatch journals are untouched.

`Worker.resource_reference()` now includes a bounded projection of the owner's
current model/effort/route advice. At most eight candidate records enter the
prompt; total/truncation and the hash's full-decision scope are explicit.
The additional `mesh(action="model_advice", arguments={...})` action returns
the full bounded decision without an authority heartbeat or provider request.
It does not intercept any other native tool. No file means `not_configured`;
invalid/unreadable evidence is a fixed safe diagnostic, never a native gate.

## Installation contract

An optional private Worker field `model_advice_file` points to an owned regular
0600 evidence file inside an owned0700 directory, outside the public repository.
The source file is read with no symlink following, bounded at1MiB, with stable
identity checks. Duplicate JSON keys and more than256 points/routes are rejected.
The file has exactly these fields:

```json
{"schema":1,"snapshot":{},"candidates":[],"requirements":{},"policy":{}}
```

Fill `snapshot`/`candidates` using the actual evidence contract in
[MODEL-FRONTIER-POLICY](MODEL-FRONTIER-POLICY.md), not fictional example scores.
Store no credentials, personal conversations or native transcripts here.
Endpoint IDs are opaque references; benchmark, cost, availability and privacy
references must be safe metadata intended for the Leader. Current source
observations are not made by this reader. A trusted caller must maintain them.

Task arguments can set workload requirements only: quality/context/capabilities,
latency, data scope, cost ceiling, preference and operation state. They cannot
supply a new catalog, evidence, policy, provider authentication or endpoint.
These requirements are advice, **not grants**. `operation_state="unknown"`
withholds a recommendation; saying `new` cannot un-fence an actual unknown
execution. Only the original execution authority can reconcile it.

Old native threads that lack a new dynamic tool may use their ordinary Shell:

```sh
python3 -m assistant_mesh.model_advice --evidence-file /PRIVATE/model-evidence.json
```

Optional `--requirements-file /PRIVATE/requirements.json` has the same private
file contract and a64KiB bound. The command performs no model/API request,
credential discovery, history sync, billing, provider selection or execution.
It neither starts nor rewrites a Leader session.

## Remaining runtime work

Install an independently frozen thin CLI, create actual sourced metadata, and
let the original Leader inspect/use it in a new explicitly scoped task. Do not
upgrade the complete old Worker merely to add this suggestion. Registering a
free execution callback and proving actual selection/call/result are separate
requirements. There is no3–5day persistent refresh task yet.

The genuine free Step stock-client trial supports a zero-incremental-cost
external capability candidate, not a measured AA effort score or a permanent
availability promise. Do not invent the missing quality variant. Subscription
Luna(max)/Sol remain an owner preference, not a hardcoded everlasting shortlist.
Official [model guidance](https://learn.chatgpt.com/docs/models#gpt-61-sol)
was fetched again in this slice: catalog controls depend on account, client and
workspace. API price, subscription marginal cost and quota remain distinct.

## Verification

Twelve new offline tests exercise private/bounded evidence, zero cost, expiry,
unknown preservation, no authority call, unchanged backend/file, small prompt
projection and the same read-only CLI. A115-test model/Worker/gateway/runtime
related group passed19.685s. These fixtures are synthetic; they do not establish
production adoption or current benchmark scores.
