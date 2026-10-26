# AMITA qualified model routes and local recovery

This source change implements the model-recovery slice of the 2026-10-26
contract. It does not install production configuration, qualify a real model,
change an account, send inference, redeem resets, or claim runtime acceptance.

## Consumer and supervisor interfaces

Keep the existing launcher composition and original Worker/Codex classes:

```python
governance = Governance(existing_private_database, existing_owner_policy)
GovernedWorker = make_worker(OriginalWorker, OriginalCodex, governance,
    scopes=existing_scopes, profile_loader=existing_profile_loader,
    receipt_callback=existing_receipt_callback)
worker = GovernedWorker(existing_config, client=existing_client)
plan = worker.model_recovery_state(original_task)
# Equivalently, without constructing any Worker/backend:
plan = governance.recovery_plan(original_task, existing_config, role_profile)
```

The state query reads local governance, provider metadata, zero-cost contracts
and an optional existing model-advice package. It never reads provider secret
environment files, calls account/read or model/list, starts a child, contacts a
provider, claims work or changes task history. It performs no LLM classification.
Network/ingress/standby readiness must remain independent of this result.

`status` is `ready`, `waiting_model`, `waiting_policy` or
`hold_original_operation`. `qualified_routes` and `blocked_routes` carry actual
model, effort, provider fingerprint, endpoint, opaque configured account/profile,
egress label and network-configuration hash. Missing egress observation is
explicitly `inherited_environment`; a configuration hash is not a verified public
IP. Public projections omit auth_home, configuration paths and secret values.

`next_recheck_at` is an absolute Unix deadline, or null. A supervisor can
re-evaluate earlier after a known quota/auth/network/config/capability change.
At the deadline, an unavailable route becomes eligible for one ordinary
preflight; it is not declared healthy. The supervisor must use the original
task's normal claim/epoch fence before calling the original `run_once`.
Do not create a replacement business task, clear an owner pause, or replay an
unknown operation because a deadline has arrived. No Worker.run override is
required; rolling_worker can retain the composed instance and its run_once.

`rescue_harness_allowed` is only a hint after actual Codex executable/transport
startup failure or an observed nonzero exit of the owned app-server. Model
quota/capacity/auth/network/provider failures keep Codex.
The governed business task never silently falls through to the old Pi candidate.
A supervisor may separately arrange an authorized framework repair and return
to Codex; this module does not launch that repair or transfer business work to Pi.

## Existing policy and evidence

The private route list stays editable owner policy, with no model-name list in
code. Existing D1/D2/D3 defaults and role profiles are initial preferences. A
weaker or unavailable preferred profile cannot exclude another qualified route.
D3 still requires the owner's strong quality floor. Free/non-OpenAI labels do
not bar advanced execution.

Task rating metadata accepts optional `requirements`: intelligence score,
capabilities (including tools/vision), context length, task-specific quality,
data scope, latency and a zero incremental cash budget. Task metadata cannot
install a provider, declare calibration or authorize additional spend.

Reuse `codex` and `codex_accounts` for actual execution bindings. Each entry
may already contain model_provider, provider_file, provider_env_file,
cost_policy/cost_contract, auth_home and network_env_file. Optional endpoint_id,
account_id and egress_id are owner-provided opaque identity labels. Each governed
binding requires an explicit strict account profile. Only its selected child
loads credentials. Do not copy authentication to another route or machine.

Explicit provider routes declare provider, endpoint_id, allowed_scopes and
capability/context facts in the existing private governance route. They require
one of:

- `quality_evidence`: verified owner-installed calibration with evidence_ref,
  observed_at/expires_at, quality and the exact pair (provider, endpoint_id,
  model, reasoning_effort, reasoning_mode and fallback_mode). Optional numeric
  intelligence_score/task_quality are only used when supplied by that evidence.
- `frontier_candidate_id`: reference into the already installed model-advice
  package. The consumer reuses its comparison, requirements and provenance;
  only exact_measured_pair or verified_same_variant_route qualifies. A same
  name or assumed equivalence remains advice. A route being dominated on the
  reference frontier does not bar it when another route is unavailable.

Legacy owner calibration remains explicitly `owner_policy`, never an AA score
or independently verified quality claim. Explicit providers are admitted only
for allowed private data scopes. The real existing cost contract is checked
locally and again by Codex at inference send boundaries. Included subscription,
local and free API routes must have zero authorized marginal cash cost, no paid
fallback or auto reload. An OpenAI API-key account cannot be labeled an included
subscription. No key creation, purchases, resets or provider disguise is added.

## Persistence and execution safety

The original private governance SQLite gains additive tables
`model_route_availability` and `model_attempts`. Existing ratings, quotas,
decisions and review plans retain their schema. There is no second business
ledger or external-effect journal. Older adjacent versions can ignore the
new tables. Transactions are short and contain no network/model operations.

Failures are deterministic categories: quota, capacity, auth, network, provider
or framework. Generic 429 and overload are capacity, not exhausted quota or a
broken harness. Reliable observed resets/Retry-After are retained; otherwise
bounded local recheck delays apply. Unknown/missing/stale quota is not exhausted.
Availability survives restart and is keyed by the entire route identity, so a
failed account, model or exit does not disable other qualified work.

The real GovernedWorker.open_backend consumes this plan. It tries qualified
bindings before native work, retains original backend decorators, checks actual
native model/effort catalog support and records the selected route. A catalog is
not entitlement or successful inference proof; actual_model_effort_verified
remains false until native observed turn metadata supplies that evidence.

Each model attempt has a separate ID referencing the original task/epoch and
operation ID when present. A work-producing native RPC records possible native
start before sending. The original Worker still owns start intent, side-effect
guards, native history, sessions, claim/epoch and final settlement. Once native
work may have started, errors are recorded without trying another route. Owner
pause, unknown runtime results and unsettled checkpoints hold that task only.

Safe checkpoints can resume the same native thread on another qualified
provider. The governed consumer mints a process-local NativeRouteAuthorization
only after trusted route and original claim admission; neither task JSON nor a
config `approved` flag can create it. It binds the original task/epoch/attempt,
exact target provider fingerprint/model/effort and digests of safe authoritative
source checkpoints. A provider change requires an explicit original provider
binding, false side-effect guard and settled native lifecycle/execution intent.
Missing original native history/artifact blocks that route before startup;
account migration continues to use the original session artifact restoration.

Codex.start preserves the old binding as the transition source, writes its
intent before resume, and sends real native modelProvider plus
config.model_reasoning_effort in thread/resume. This also sets the effort used
by automatic native Goal continuation. The returned model/provider/effort and
collaboration-mode settings must match. The transition records verified native
configuration separately from effective inference, which remains unverified
without observed native turn evidence. Original Goal identity/createdAt and
cumulative token/time use cannot reset; no Goal set/clear or replacement thread
is used for the conversion. Transition lineage is saved inside native_lifecycle
alongside the actual target binding through the original Worker checkpoint and
session-save path. Unknown results retain the original effect guard and intent.

## Validation and deployment handoff

Initial consumer evidence passed before the native conversion additions:

```text
click-gate verify '{"version":2,"workdir":"/home/gihggiy/Github/personal-assistant-mesh-worktrees/amita-model-recovery-20261026","checks":[{"evidence_id":"amita-model-recovery-targeted","argv":["python3","-m","unittest","tests.test_model_recovery","tests.test_leader_governance","tests.test_codex_provider"],"class":"targeted"}]}'
```

It covers actual original Worker preflight/run_once with synthetic model
transports: advanced qualified free route after OpenAI exhaustion, independent
quota/accounts/exits, no RPC during standby, delayed local recovery, exact
calibration, private-data/capability floors, six failure classes and unknown
native work preserving original identities without route replay. Provider
configuration/secret-child/cost contracts keep their existing tests.

Root owns deployment and real authorized route acceptance. Install the updated
leader_governance, governance_worker, codex_provider and new model_recovery with
their existing local dependency closure. Keep the same governance DB and actual
launcher composition, including native-goal/Entrance decorators. Before any
real acceptance, use a genuinely installed, authorized, calibrated route and
record observed effective model/effort/provider separately from catalog support.
Synthetic fixtures are not real model calibration or production availability.

The additional focused check `amita-native-model-transition` runs only
`python3 -m unittest tests.test_native_model_transition` through Click with the
same absolute workdir. It combines the real Worker.open_backend/Codex.start
paths with a synthetic stdio transport: safe same-thread cross-provider resume,
real native provider/effort overrides, source and Goal accounting preservation,
JSON approval rejection, source-change rejection and no replay after a native
configuration mismatch. Prior passing suites are not rerun as a substitute for
this new protocol evidence. Install updated codex/reasoning as well; any optional
native_goal_runtime snapshot must be rebuilt from this source so its selected
Codex.start contains the same transition support.

Final incremental checks passed through Click after the conversion and source
pin update: `amita-native-model-transition`, `amita-transition-compat` (five
affected individual old cases plus the original scoped MRO case), and
`amita-route-namespace` (the generated isolated runtime's real same-thread
provider conversion). The selected backend's make_route_authorization factory
mints the capability inside its own actual namespace; same-named classes from
different optional runtime packages are never accepted by weakening type checks.
The builder keeps Root's atomic Store/scope compatibility commits and changes
only the reviewed codex/codex_provider/reasoning source pins for this slice.

Official OpenAI documentation checked during implementation:
[app-server auth and quota](https://learn.chatgpt.com/docs/app-server#auth-endpoints)
and [native thread continuation](https://learn.chatgpt.com/docs/app-server#start-or-resume-a-thread).
