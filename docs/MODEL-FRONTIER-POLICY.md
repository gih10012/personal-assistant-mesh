# Owner-scenario model frontier

2026-10-11 source candidate. `assistant_mesh/model_frontier.py` provides a pure
`advise_models(...)` library. **Not wired into the live Leader, Codex/Pi adapters,
provider dispatch, scheduler or account configuration.** No model call, price
probe, key, purchase, retry, process or native-tool restriction is created.
The 28 focused tests use synthetic scores and prices, not live AA observations;
85 related frontier/Codex-provider/Pi-provider/extension tests also passed.

## Owner preference and actual price

Prefer capable, good-value model/effort combinations on the current frontier and
in the attractive upper-left region. The owner's present shortlist is GPT-6 Luna
`max` plus task-appropriate GPT-6.1 Sol, **not a forever ranking**. Free access to
the same physical variant can move its effective x-coordinate to exactly zero.
Subscription access can also have zero *additional cash expense*, while still
consuming quota and time. Having Plus does not make a separately billed API free.

Maintain two different plots:

| Plot | Intelligence axis | Cost axis |
| --- | --- | --- |
| Published reference | Same-suite published benchmark result | Published benchmark task cost |
| Owner scenario | Matched physical model, effort, mode and fallback variant | Current authorized route's incremental task estimate, including actual free zero |

The second plot is **our re-rated scenario, not an official AA published chart**.
Do not use epsilon as a fictional price for free routes; mathematical comparison
does not require a logarithmic chart. Keep quotas, latency and privacy explicit.

AA's published task cost depends on weighted benchmark token consumption and
provider prices. It is not a quote for every personal task or an owner's
subscription marginal cost. [AA methodology](https://artificialanalysis.ai/methodology)

## Evidence contract

The caller supplies one snapshot with `schema: 1`, `snapshot_id`, `source_url`,
`methodology_version`, `comparison_set`, `cost_basis`, `evidence_ref`,
`observed_at` and `points`. Each point has a unique `point_id`, `pair`,
`evaluated`, `intelligence_score`, `reference_cost_per_task_usd`, `evidence_ref`,
and matching methodology/comparison/cost fields. No shipped current AA numeric
catalog is fabricated from screenshots. Preserve the underlying source privately
when appropriate; an input digest is not independent source verification.

The pair is `{provider, endpoint_id, model, reasoning_effort, reasoning_mode,
fallback_mode}`. Use opaque endpoint IDs, not URLs carrying credentials. Effort
and fallback modes are distinct measured points. For a different route, an
explicit `equivalence: {state: "assumed" | "verified", evidence_ref: ...}` may
reuse quality only for the **same model/effort/mode/fallback**. The assumption is
visible in the result; it does not pretend the route's tools, privacy, latency,
availability or physical variant have independently passed a test. Relabelled,
quantized, truncated, distilled or different-effort models need separate evidence.

Each candidate includes `candidate_id`, `benchmark_point_id`, `pair`,
`capabilities`, optional `context_tokens`, and these independently scoped inputs:

- `availability`: current `available` observation.
- `cost`: `effective_cost_usd`, policy (`existing_subscription`, `local`,
  `free_api`, `paid_api`), `authorized`, `quota_available`, `paid_fallback: false`,
  `auto_reload: false`; optional `quota_units` and explicit `quota_unit`.
- `privacy`: owner-authorized `allowed_scopes` (for example `public_synthetic`).
  A public trial does not authorize uploading private Leader history.
- Optional `latency: {seconds: ...}` and `task_quality: {score: ...}`.

Each observation has `verified: true`, `evidence_ref`, `observed_at`, and
`expires_at`, with `observed_at <= now < expires_at`. These are facts supplied by
the trusted owner observer, **not authentication or billing verification performed
by this library**. The existing `model_provider_policy.py` free contract remains
separate; live physical dispatch must still validate its exact route and scope.
Never authorize all paid physical routes through a free virtual alias.
Effective task-cost estimates must refer to the same requested workload; do not
compare one route's per-token quote with another route's full-task estimate.

Benchmark metadata defaults to a three-day freshness window. Stale last-known
metadata remains usable advice, explicitly labelled `stale_last_known`; callers
can require fresh evidence for a particular task. Unknown time/evaluation or
incomparable suites cannot produce a confident managed shortlist. This does not
block native work or offline autonomous agents. Refresh or perform a separately
authorized synthetic evaluation, not a fabricated score.

AA currently documents Intelligence Index v4.3.2 and its limitations: it does not
by itself prove every modality, language or agent workflow. Task-specific quality
and tools matter. [AA Intelligence Index](https://artificialanalysis.ai/evaluations/artificial-analysis-intelligence-index),
[intelligence methodology](https://artificialanalysis.ai/methodology/intelligence-benchmarking)

## Task selection

`requirements` can set capability/context/quality/latency floors,
`data_scope` (default `public_synthetic`), `max_incremental_spend_usd` (default
zero), and preference (`lowest_cost`, `lowest_latency`, `highest_quality`).
Requirements are not silently relaxed when a good model is inaccessible.
Records explain unavailable routes, insufficient quality, budget and privacy
constraints, including when an available route was below the *reference* frontier
but has become competitive at its actual owner price.

`policy.resource_axes` defaults to `['effective_cost_usd']`. A route is dominated
when another **task-feasible** route has at least its measured quality, no larger
cost on every requested axis, and a strict advantage in at least one dimension.
There is no invented convex/interpolated chart-line hard gate. Equal points are
not strict domination; unknown metrics are not zero. Optional `latency_seconds`
and `quota_units` axes retain practical tradeoffs; quota dominance is asserted
only for the same budget unit. Example: a fast Luna route and a slower Sol route
can both remain useful at zero cash cost. Set task latency/quota requirements or
axes from actual observations instead of always picking maximum effort.

Optional `green_region` contains explicit `max_effective_cost_usd`,
`min_intelligence_score`, and `evidence_ref` supplied by the current owner
preference/source. It is a **preference**, not a shipped permanent dollar/score
cut. Outside-green routes can still meet a difficult task's requirements.

Official model guidance exposes model plus effort controls, with higher effort
trading time/usage for reasoning depth; access depends on the actual client and
account. [Codex model and effort selection](https://learn.chatgpt.com/docs/agent-configuration/subagents#choosing-models-and-reasoning),
[API reasoning effort](https://developers.openai.com/api/docs/guides/deployment-checklist#set-up-reasoningeffort)

The result retains source and candidate evidence refs, exact pair/equivalence,
published-reference frontier, re-rated shortlist, rejection/comparator reasons,
earliest current evidence expiry, and deterministic input/decision digests.
`operation_state` must be `new` or `confirmed_no_effect` for a recommendation.
Unknown, in-flight, completed or unsettled selection holds the original operation:
changing price/model/node must not create a new identity and replay it.
Warnings explicitly identify stale benchmark metadata, assumed route equivalence,
and recommendations that differ from the published reference frontier. Original
reference dominators remain visible even when unavailable or re-priced.

## Integration still required

Root owns GOAL/PLAN/TASKS and original coordination identities. Before claiming
live selection: bind the snapshot and observer to real authority; integrate
advice into the original continuous Leader's task context; observe the actual
selected physical dispatch and supported effort (especially Pi virtual routes);
recheck current cost/quota/privacy before submitting **once**; record real tools,
session continuity and artifact outcomes without rewriting unknown histories.
Keep model/effort changes at supported native idle/settled boundaries. This
library is not a replacement harness, global allowlist, migration tool, or proof
that the runtime already follows this policy.

## Manual comparison requested by the owner, 2026-10-11

The approximate intelligence values below are read from the owner's supplied AA
screenshots, **not a fresh independent AA catalog or a verified endpoint score**.
Retain the exact effort/mode assumption when using them. Zero is the advertised
free cash price, not proof that this account/harness can currently submit a call.

| Candidate | Manual quality reference | Actual free-cost comparison and use |
| --- | --- | --- |
| Step 5 Preview Free | Step 5 Preview about 44, if the physical variant/effort matches | Re-rate to `(0, about 44)`; a worthwhile general-task candidate, not rejected for its paid-chart position |
| Muse Spark 1.3 Contributor Free | Muse Spark 1.3 `max` about 48, only if that configuration matches | Re-rate to zero cash, but the contributor program permits training; not a route for private Leader memory |
| Nemotron 3 Ultra Free | Nemotron 3 Ultra about 23, if the configuration matches | Zero cash does not make its intelligence higher; normally not the complex-task first choice if a stronger zero-cost route is feasible |
| MiMo-V2.6-Flash Free / MiMo-V2.5 Free | No matching score established by these screenshots | Do not borrow MiMo-V2.6-**Pro** or MiMo-V2.5-**Pro** scores; evaluate a public task or fetch a matching reference |
| LongCat 2.5 Preview Free / Space Bunny Free | No matching point in these screenshots | Unknown score is not a fabricated low score or permanent rejection; use scoped task evidence |

The owner-preferred subscription Luna `max` and task-appropriate GPT-6.1 Sol can
also have zero incremental cash price while quota remains available. Several
zero-cost models can be useful: task difficulty, latency, remaining quota, tools
and privacy distinguish them. Do not always spend maximum effort merely because
the cash price is zero. [Zen models, free prices and privacy](https://opencode.ai/docs/zen/)

**Actual availability correction:** the one Pi 1.1.0 LongCat trial made one POST
through an owned VPS SOCKS5h/TLS route and terminated with HTTP 403,
`FreeTierError` / free-tier client restriction. There was no successful inference,
tool or follow-up model call. OpenCode's collaborator explicitly says the free
tier cannot be used in other harnesses; the general Zen API documentation is not
permission for that tier. Do not invent an authentication/credit explanation,
spoof OpenCode identity, or silently switch to a paid endpoint. Keep these free
points as price/quality candidates **for a legitimately available route**; they
are not currently verified usable Pi/Codex free routes.
[OpenCode collaborator's tier-specific policy](https://github.com/anomalyco/opencode/issues/49621)

The policy/source release is
[4860c17](https://github.com/gih10012/personal-assistant-mesh/commit/4860c170ea367e4676ac62a2ef58193b5ab7eb3f).
Root's 174 related tests passed; the frozen archive's local 2002 tests passed in
168.940s and exact-head CI passed on Python 3.8/3.12/3.14. These are source tests,
not evidence that the production continuous Leader already dispatches through
this adviser. [Exact source CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/38129270175)
The same frozen archive also passed 2002 tests in 177.832s on the VPS's actual
Python 3.6.8, in a separate directory with no production service/config change.

### Distinct Go route and existing local command

The owner additionally identified `codex-opencode`. Read-only local inspection
confirmed it ultimately runs native Codex, but its old default selects a Go Pro
model and its startup can modify configuration and synchronize session/SQLite
files. Root is not executing that default against the continuous Leader. Existing
Go authentication is present, not a request for a new login or billing change.

Go is a distinct route: its official documentation validates Codex and Pi,
requires honest client identity plus a stable conversation header, and lists
Step/LongCat free models at `/zen/go/v1/chat/completions`. The old direct **Zen**
403 must not globally exclude those physical models or **Go**. Go's non-free
Muse Contributor and Space Bunny are not the Zen `*-free` price rows. An isolated
native Pi Step trial is being prepared using the existing Go key, true Pi UA and
actual new native session ID; no outcome is claimed before the real tool/session
results. [Go contract](https://opencode.ai/docs/go/)

18:30 actual result: the distinct Pi Go Step trial made exactly one POST and
settled with HTTP403 requiring an active Go subscription, with zero tools or
successful artifacts. This is not the prior Zen client restriction and not proof
that all free routes fail. Do not ask for a new key or enable billing merely to
repeat it. Owner subsequently accepted a real anonymous OpenCode session only
as a temporary external execution capability; Codex/Pi remain the Leader.
Isolation/catalog preflight is not inference availability or a quality score.
[Actual Go trial](PI-GO-FREE-TRIAL.md)

19:03 the distinct genuine OpenCode stock builtin fixture actually succeeded
without login: read/bash artifact generation and verification, followed by
read-only verification in the same native session with complete prior messages
preserved. Native cost fields were zero and no owner credential was available.
This legal temporary external execution route can now be compared at zero
incremental cash, without making OpenCode the Leader or claiming direct Pi/Go
API success. Matching the screenshot's Step score/effort remains an explicit
reference assumption, not a new benchmark. Availability expires and must be
observed; synthetic success is not production capability registration.
[Stock builtin proof](ANONYMOUS-STOCK-FREE-TRIAL.md)
