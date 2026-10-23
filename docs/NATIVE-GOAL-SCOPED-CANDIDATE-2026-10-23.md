# Scoped native Goal source candidate

Status: implemented source candidate and bounded local units; not installed,
enabled, independently reviewed or accepted against a native process/model.
The original failed/unknown cases were not opened, resumed or rewritten.

Owner: `mobius_task_design_audit_20261021`; branch
`work/native-goal-scoped-candidate-20261023`; base `35b0598`.
Root owns shared coordination, exact private configuration, integration and
serialized operations. School evaluation remains paused. This implements the
[approved contract](NATIVE-GOAL-SCOPED-ADAPTER-CONTRACT-2026-10-23.md), not a new
Goal, new business attempt or replacement task system.

## Implemented boundary

- One actual claim enters a narrow `Worker.run_claimed(task)` seam. Its legacy
  body is byte-identical to the frozen body; unselected scopes use that body.
- An owner-selected future scope uses the existing modern native lifecycle
  algorithms in `native_goal_runtime`, not incumbent modern core replacements.
  Actual Goal state, automatic turns and the once-only resumed-input intent
  are retained; no checkpoint Goal is replayed through `goal/set`.
- Backend selection sits beneath the installed governance/profile/Entrance
  closed-failure decorators. Worker scope routing surrounds that stack;
  diagnostics surrounds the result. Selected scopes must also be governed.
  Selected work cannot silently fall back to Pi/free/lower-quality execution.
- Every started automatic turn must naturally complete. An interrupted or
  failed turn retains the guard. Goal identity/createdAt/budget stay fixed and
  accounting is checked against the previous actual snapshot, including late
  Goal updates. Plain turns receive the same idle/event/close boundary.
- Active-Goal child yield reuses the owned Unix admission-drain, independent
  unloaded observer and original-history fingerprint algorithms. There is no
  fake pause, fixed grace sleep or unsubscribe-as-drain substitute.
- Task and committed native session stay guarded through the real original
  fingerprinted upload. The facade adds `ok` only after an original successful
  same-task/epoch session commit. Finalization requires both commit and final
  update ACK; a lost response never triggers another native attempt/upload.
- Same-ledger authority methods retain epoch/Leader lease checks, the original
  final-reply transaction hook, original child wakeup, and active-yield scope
  reservation. `waiting_children` cannot manufacture a cleared effect guard.

`make_store_class` adds only scope metadata/binding tables to the original
ledger. It extracts selected claim/update/delegate/wait methods rather than
installing the complete modern Store. Saved native session state intentionally
remains guarded; successful final task proof, not an optimistic artifact state,
permits later coordination. Session state alone is never a replay permit.

## Source closure and new pins

`deploy/build-native-goal-scoped-candidate.py` reads source only. It checks
the owner-supplied full input `.py` manifest, all incumbent core pins, ten
modern algorithm pins, and the scoped assembly module pin. It writes a NEW
owned directory, never configuration, credentials, a ledger or a service.

Only three original `.py` files change:

| Member | Candidate SHA-256 |
| --- | --- |
| `worker.py` | `5e5aedcdfac1dc144d2864084ef155e7726421c95b74d2145f3699b24a9109e8` |
| `entrance_runtime.py` | `e4d6fce4bde5639ec3e84f7388de1197d9846efe07a685836d91fa1d3247e198` |
| `legacy_fenced_receiver.py` | `aca3e78e5f74d2510bc6db77965c408868df19979b0f130b361b73385989f4a1` |

The last two only update their exact incumbent Worker pin. Their role,
constructor, single receiver, ready/close and transport implementations do not
change. The exact original `codex.py`, `sessions.py`, `server.py`, `channel.py`
and final-hook `store.py` bytes remain; Store still has SHA-256
`be2df48e03021f48c382efc4690fa7a8ee5e67da8bd93497e725566532e30c0b`.
Their selected-scope semantics are intentionally extended by explicit facades.

There are twelve generated optional `.py` modules: package init, config bridge,
Codex, WorkerStrategy, AuthorityMethods, native Goal observer, native history,
native WebSocket, provider, provider policy, reasoning, and save helpers. Every
nested relative import is checked. The only incumbent import boundaries are
`config` and `store`; there is no SDK/MCP/ASGI import in this closure and no
source restore/migration helper. The isolated save module contains only the
existing fingerprinted upload path. The builder records all actual output
hashes and the import graph in `native-goal-candidate.json`.

The builder requires Python 3.10+; local verification used Python 3.14.
Generated runtime syntax is parsed as Python 3.6 and isolated `python -S -B`
imports were checked without site packages or ledger construction. This is
NOT an actual Python 3.6/native executable compatibility trial. Build the
reviewed source artifact once, inspect its manifest and transfer that exact
artifact; do not assume `ast.unparse` output is identical across build versions.

To inspect an explicitly selected source-only input without building:

```sh
python3 deploy/build-native-goal-scoped-candidate.py --base-root APPROVED_SOURCE
```

Supply its independently approved full manifest, NEW destination and reviewed
support source to build. Inspection is not approval of unknown input files.
The local historical fixture's full manifest is not the production manifest.
Existing owner launchers, policy and native executable/socket implementation
must be pinned separately by Root; this builder does not approve them.

## Root integration: all pieces together, default off

No import or class definition activates a scope. Root must privately load an
explicit detached policy with exact fields `format`, `enabled`, `revision`,
`authority`, `owner_id`, `scopes`, `executors`, `activated_at`. The format is
`mesh-native-goal-scopes/1`, lists are finite/exact, and activation is a fixed
UTC epoch time chosen before work, not a sliding admission cutoff.

For the ONE existing authority, compose `make_store_class(original_store,
AuthorityMethods, policy)` BEFORE its original Entrance/Claw/passive factory
constructs the Store. Keep `recover_inflight=False` and one ledger/API/channel.
Restore module factories with the existing serialized ready/finally discipline;
do not create a second Store or listener in a ready hook. After original
Ingress exists, call `attach_authority(api, policy)` in that same ready chain.
Preserve the existing emit/final/feed hooks on that exact API and Store.

Each listed executor must ALREADY be a real worker peer with the explicit
`owner.native-goal.lifecycle` capability. The helper never grants it. Actual
authenticated peer context is installed thread-locally for each dispatch and
removed in `finally`; task JSON cannot supply it. Disabled policy does not
attach a route or caller privilege. A missing required grant fails readiness.

The Worker class assembly is explicit owner code, not an arbitrary model-
supplied module name:

```python
from assistant_mesh.native_goal_scoped import assemble_worker

WorkerClass = assemble_worker(
    original_worker, original_codex, private_policy,
    governance_factory=installed_governance_make_worker,
    governance=existing_governance,
    governance_scopes=existing_governance_scopes,
    profile_loader=existing_owner_role_profile_loader,
    receipt_callback=existing_reasoning_receipt_callback)
# Install the existing metadata observer OUTSIDE WorkerClass, then instantiate
# with the original config/client. Do not call another run_once inside it.
```

`original_worker` may be the existing EntranceWorker; its constructor and
closed-failure callback are retained. Its old redundant session-ready guard is
subsumed by the selected strategy's stricter guarded save/ACK path, not doubled.
Do not wrap an already decorated Codex with the new Goal class: that skips
governance/profile/closed-failure initialization. `assemble_worker` builds the
conditional base first, then calls the installed original governance factory.
When disabled it does not import the optional Goal controller.

If metadata phase observation is installed, register BOTH original
`assistant_mesh.sessions` and `assistant_mesh.native_goal_runtime.sessions`;
the selected strategy uses the latter. Retain the already reviewed byte cap,
nonblocking lock, success-poll suppression and safe-metadata-only defaults.
These diagnostics are never settlement or model-execution proof.

## Admission, rollback and unsupported paths

- First activation refuses a scope that already has ANY saved native session.
  It does not adopt the old parent/children or any existing unknown case.
  Explicit fresh future scope names and creation cutoff prevent accidental
  historical admission. Original IDs/history/guards remain unchanged.
- Scope bindings are durable and immutable. Removing a scope or disabling
  policy cannot restore its legacy claim path. Keep the authority facade and
  binding tables installed on rollback; do not drop tables or restart admitted
  work through 3198. Disabled, never-bound ledgers use the exact legacy claim
  path without creating these tables.
- Live policy reload is not implemented; finite executor or scope changes
  require Root's reviewed serialized configuration/restart. Missing modules,
  auth, grant, compatible native socket or qualified quota wait/fail closed.
- Cross-node/auth-home native history migration is explicitly unsupported in
  this path. It retains the original task/session and waits, rather than
  importing history or selecting a new thread. General migration remains on
  the existing graph.
- A naturally closed native process/history does not prove detached Shell,
  MCP or external operation effects settled. Interrupted/unknown attempts keep
  their original effect guards; old-case reconciliation remains separate.
- Native executable compatibility/owned socket admission semantics, real
  parent-child continuation, host Python 3.6, and actual per-auto-turn
  model/effort evidence still require the authorized deployment/Leader queue.
  A configured profile/catalog receipt is not physical-model attestation.

## Bounded evidence and handoff

Five NEW tests in `tests/test_native_goal_scoped.py` reuse the existing
historical-source and native lifecycle fixture helpers, not their old suites:
one claim/byte-identical old body/pure closure, real temporary authority grant
and active-yield reservation, governance MRO and guarded failed save, generated
controller auto-turn/abort/accounting plus synthetic history seal, and real
fingerprinted gzip/chunk/commit with both confirmed and lost ACK outcomes.

Latest complete run before this documentation: **5 passed in 0.650s**. One
new history-seal fixture initially omitted its own native `turn/started`
notification and was correctly rejected; adding that synthetic event made
the targeted case pass, then all five passed. No old test suite, native
app-server/model/RPC, production/SSH/GUI, or original business case was run.

Root should independently review the scoped assembly/facade and exact built
diff, then may perform the already-authorized serialized installation. Any
real future-case Goal/parent-child/platform/fault acceptance belongs to the
Leader's weekly queue. Do not label this source delivery PAM-007 completion,
production lifecycle repair or proof of original-case settlement.
