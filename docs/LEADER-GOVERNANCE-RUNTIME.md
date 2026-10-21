# Opt-in task quality consumer and private review queue

Source candidate; not deployed. No model or production operation was performed.
Implements the owner contract `LEADER-GOVERNANCE-2026-10-21.md`, not an OpenAI
difficulty grading standard or an independently verified AA score.

## Small frozen deployment closure

Add `leader_governance.py` and `governance_worker.py`, plus the already deployed
`reasoning.py`. These import only the standard library and each other. Reuse the
original frozen Worker/Codex/config/session helpers; do not copy modern core or
start another daemon. Actual frozen Worker SHA is
`816a8eb2f6bc5a24a22ae345b1b7b94b0dd5d0ce52f99a6658688e683ce9cbf0`;
Codex SHA is `b32e2ba28c2c06ce1b1208a023de01ddd56081c012d9b5723e1158449bff1f98`.
Root owns staged source checks, backups and serialized production cutover.

In the existing profiled launcher, use the original Codex class, not an already
profiled wrapper, and preserve the existing owner-profile loader. Opt in ONLY
the new workspace project scope initially:

```python
from assistant_mesh.leader_governance import Governance, private_json
from assistant_mesh.governance_worker import make_worker
governance = Governance('/PRIVATE/governance/state.sqlite',
                        private_json('/PRIVATE/governance/policy.json'))
GovernedWorker = make_worker(Worker, Codex, governance,
    scopes=['leader:project:personal-assistant-workspace'],
    profile_loader=lambda agent: load_profile(PROFILE_PATH, 'leader'),
    receipt_callback=receipt)  # Existing private per-turn selection receipt hook.
GovernedWorker(config, config_path=config_path).run()
```

`load_profile` is the existing `agent_manager.load_profile`; the owner loader
must support `agent=None` for preflight and the actual agent at turn/start. A
qualified explicit role model/effort is retained instead of silently replaced
by the task default. A weak/out-of-policy selection waits with an explicit
`governance_role_profile_below_or_outside_floor` reason, not a silent override.
The policy example permits the stronger high/max routes for ordinary tasks as
well. Profile changes between preflight and turn/start explicitly stop that
attempt rather than misreporting its selection; ordinary configure-for-next-turn
continues working. Native `model/list` still validates actual effort support.

Other scopes bypass task grading and retain existing owner-profile/native
backend behavior, including authorized candidate fallback. Empty `scopes`
disables this consumer rather than treating every old task as ungraded. No
old task is reclassified, resumed or silently moved to another model.

**Do not globally enable this on Claw yet.** Current normal entrance tasks have
no grading metadata; that would stall every new message. A separately explicit
owner adapter may assign D1 to the fixed communication-only role without
guessing business difficulty from keywords; the delegated business still needs
Leader grading. When prepared, use `EntranceWorker` as the base class so its
original closed-failure/history-preservation backend decorator remains intact.
Root preserves role, channel, thread and scope mappings. The new wrapper's
unit checks are not evidence that any production role has adopted it.

## Actual consumer, not instructions alone

Each **new** task needs `context.governance`:

```json
{"difficulty":"D3","rationale":"Architecture and consistency changes","risk":"high","priority":7,"route_id":"advanced"}
```

Alternatively the authorized Leader rates its original task ID using private
CLI metadata. Missing grading does not guess from task text or default to a
weak model. Existing unknown/paused/effectful tasks remain unavailable. Changing
metadata never requeues them, clears a guard, overwrites history or creates a
replacement task. Explicit rating revisions use an expected revision and a new
configuration intent, not a new execution identity.

`deploy/leader-governance-policy.json.example` records this owner's current
Luna-max / qualified Sol-high-or-max choices. Routes are private mutable owner
policy, not a permanent model list. D3 requires an explicitly qualified strong
Codex route; no automatic free/Pi downgrade. Other already authorized accounts
may provide the **same qualified model/effort** before a turn starts. No new key,
paid credits, reset redemption or auth copying is implemented.

The consumer executes the original account chain, observes the real
`account/rateLimits/read`, then records only the Codex bucket, observation time,
window duration, used percent and reset time. Weekly means an observed 10080
minute window, never an assumption about `secondary`. Unknown/stale information
is not exhausted. Raw upstream credit/token/account metadata is not persisted.
All candidates definitely exhausted yields `waiting_quota` and the earliest
qualified-account recovery time (each account must clear all exhausted windows).
The original task/epoch and normal checkpoint are retained.

**Scheduling boundary:** `next_safe_retry` is durable advice in this initial
consumer. The original authority still applies its 60/300 second waiting delay;
it has NOT been fixed merely by installing the Worker wrapper. Root must attach
a separately reviewed authority deadline hook before claiming reset-aware
backoff. Never fix this by sleeping the whole Worker or consuming a claim and
returning no task. Unrelated work must remain claimable. Native active-goal
automatic turns, manager-created standalone processes and cross-harness memory
are not covered by this explicit-turn wrapper.

## Leader weekly queue

```bash
python3 -m assistant_mesh.leader_governance --database /PRIVATE/governance/state.sqlite rate --task-id ORIGINAL --request-id RATING_INTENT --metadata-file /PRIVATE/rating.json
python3 -m assistant_mesh.leader_governance --database /PRIVATE/governance/state.sqlite review-enqueue --id STABLE_CASE --metadata-file /PRIVATE/review.json
python3 -m assistant_mesh.leader_governance --database /PRIVATE/governance/state.sqlite review-list
python3 -m assistant_mesh.leader_governance --database /PRIVATE/governance/state.sqlite quota-list
python3 -m assistant_mesh.leader_governance --database /PRIVATE/governance/state.sqlite decision --task-id ORIGINAL --epoch ORIGINAL_EPOCH
python3 -m assistant_mesh.leader_governance --database /PRIVATE/governance/state.sqlite review-schedule --leader-id CURRENT_LEADER --resources-available
python3 -m assistant_mesh.leader_governance --database /PRIVATE/governance/state.sqlite review-cancel --id STABLE_CASE
```

Metadata files are private 0600 single-link regular files; the database has a
private 0700 parent. Review entries reference the stable business task/project,
code version, original operation, required nodes/model, owner cooperation and
quota budget. Queue accepts integration/model-comparison/GUI/fault-drill/acceptance
cases, not daily local unit tests. Scheduling requires pending work and available
resources; prefer a fresh observed weekly reset minus 24h, otherwise Sunday
20:00 Asia/Shanghai. It records intent only: **no execution right, lease or
cross-partition exclusivity is granted by a queue row or claimed Leader string**.
Actual execution must use Mesh's current authority/operation fence. No timer,
integration test, model comparison or fault drill is started by these commands.

The nine bounded local tests use fake native RPC and private synthetic SQLite.
They prove local consumer/decision behavior, not deployment, actual quota,
observed inference, batch execution or owner acceptance. The original 43 task
IDs/dependencies and all other coordination files are untouched.
