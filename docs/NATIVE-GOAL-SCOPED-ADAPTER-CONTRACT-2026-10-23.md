# Scoped native Goal lifecycle: implementation contract

Status: source audit and implementation contract only. No runtime is changed,
no original case is resumed, and no model/native-RPC/app-server/production
trial was run.

Owner: `mobius_task_design_audit_20261021`; branch
`work/native-goal-lifecycle-contract-20261023`; base `f64e5c2`.
Scope: PAM-007b/c, retaining the original task graph and native identities.
Root owns integration, private configuration, serialized deployment and any
separate original-case reconciliation. School evaluation remains paused.

## 1. Decision

Implement one owner-selected **Goal-aware execution path** for future normal
work, reusing the existing candidate's lifecycle algorithms. Keep the old path
for other scopes. This is not a backend-only substitution, a longer sleep, a
new task system, or a reason to replace the whole modern core in production.

The required boundary spans Backend, Worker, session commit and authority.
Original core files may remain as the immutable base, but their selected-scope
semantics cannot remain unchanged. Prefer a new narrowly reviewed source
candidate with an explicit claimed-task dispatch seam, additive authority
facades and isolated Goal modules. Where the seam changes a frozen member,
record its new exact pin and reviewed diff; do not claim all five pins stayed
unchanged. Preservation of an old hash is not a substitute for fixing behavior.

Do not enable the active-yield path until its Worker, authority and persistence
pieces are installed together. Healthy auto-continuation and active-goal child
yield are separate outcomes, not interchangeable demonstrations.

## 2. Authoritative protocol and observed source gap

Official documentation was searched and fetched on 2026-10-23:

- [Goal continuation](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex#how-goals-are-designed-in-codex):
  a persistent, thread-scoped Goal can continue at an idle boundary after one
  turn ends. A completed turn is not the Goal's completion condition.
- [Goal API](https://learn.chatgpt.com/docs/app-server#manage-a-thread-goal):
  a new objective replaces the Goal and resets accounting. Updating the same
  non-terminal objective, or omitting it, has different accounting semantics.
- [Turn events](https://learn.chatgpt.com/docs/app-server#turn-events) distinguish
  started/completed/interrupted/failed turns; thread status is another signal.
- [Unsubscribe](https://learn.chatgpt.com/docs/app-server#unsubscribe-from-a-loaded-thread)
  is subscription removal, not an atomic stop-admission or execution seal.

The audited production baseline is the actual Git object
`3198c877234a8fd0c6912fdbe218f47185effedc`, not today's similarly named files:

| Boundary | Baseline source | Gap |
| --- | --- | --- |
| Goal restore | `codex.py:276-281` | Replays configured/checkpoint Goal through `goal/set` instead of reading actual state. |
| Turn recognition | `codex.py:313-328` | Accepts only its first turn/start receipt; ignores a different automatic turn. |
| Finish | `codex.py:375-401` | Returns at that first turn's completion. |
| Persistence | `worker.py:703-710` | Sets local session state effect=false, then reads/saves while the runtime is still open. |
| Task result | `worker.py:715-723` | Uses actual goal status only when task.context.goal exists; a model-created Goal can be missed. |
| Child wait | `store.py:439-443` | Coordination status itself unconditionally clears the effect flag. |
| Scope ownership | `store.py:381-385` | No reservation for a safely yielded but still active Goal. |

Owner-supplied evidence of an automatic next turn immediately after the first
completion is consistent with these source gaps. It does not settle the old
attempt's effects or authorize resuming it. Runtime closure, native history
preservation, artifact commit and business completion remain distinct facts.

No local official Rust checkout/source cache was found in the bounded search;
this audit did not fetch another CLI or execute the installed wrapper. The
previous version-specific socket-drain source review and trial are retained in
[NATIVE-PLAN-EVENTS](NATIVE-PLAN-EVENTS.md) and
[NATIVE-SOCKET-DRAIN-TRIAL](NATIVE-SOCKET-DRAIN-TRIAL.md). They are not fresh proof
that any different installed CLI implements the same drain contract. Root must
pin the actual supported native executable/version for the new installation.

## 3. Reuse existing mechanisms, not new approximations

At base `f64e5c2`, these are already implemented source candidates:

| Existing surface | Reuse |
| --- | --- |
| `codex.py:519-801` | Actual Goal read, no checkpoint goal/set, same-thread resume, automatic-turn adoption and one explicit resumed-input intent/receipt. |
| `codex.py:830-982` | Ordered current-thread notifications, all adopted turn IDs, current plan, native Goal/status and drain admission observations. |
| `codex.py:1110-1193` | Post-RPC event consumption, actual terminal/idle barrier, continued active-Goal pumping rather than first-turn return. |
| `codex.py:984-1107` | Owner-PID socket admission drain, durable intent ACK and admitted-work event pump. |
| `codex.py:1195-1329` | Independent yielded seal and terminal runtime-close/late-event seal. |
| `native_goal_observer.py` | Unloaded, read-only observation; same Goal identity/createdAt and monotonic accounting; no resume or Goal write. |
| `native_history.py`, `native_ws.py` | Original byte-prefix/full-file proof and owned Unix transport. |
| `worker.py:673-803` | Intent/effect guard, turn adoption, actual Goal checkpoint, drain ACK and safe steering conditions. |
| `worker.py:900-999`, `1002-1186` | Verified seal, guarded artifact upload/ACK and final task update ordering. |
| `sessions.py` | Existing `expected_fingerprint` upload path, not a new archive format. |
| `store.py:365-451`, `475-515` | Active-yield scope reservation, checkpoint validation and original parent wakeup. |

`tests/test_native_yield_worker.py` is a test module, not a standalone production
adapter. These source mechanisms have to be composed; importing that test or
only injecting modern Codex into the old Worker is not the missing wiring.

Preserve governance/profile/closed-failure decorators and their callbacks.
Governance still selects the qualified route before native work, waits for D3
quota rather than free/lower-quality takeover, and does not restart an already
unknown attempt. Diagnostics is an outer observer, not a settlement authority.

## 4. Minimum implementation seams

### A. One claim, explicit selected-scope strategy

Add a claimed-task dispatch seam to the new Worker candidate: exactly one real
authenticated claim yields the original task/node/epoch, then trusted owner
scope policy selects the old or Goal-aware path. Both paths receive that same
claimed task. Do not run two `run_once` methods that each claim, return a task to
pending to switch paths, or invent a replacement task/scope.

The old claimed-task body stays behavior-compatible for unselected scopes.
Factor its body without semantic changes; the Goal-aware claimed-task strategy
reuses the existing modern lifecycle sequence. This is the small explicit
Worker boundary change: a literal unchanged 3198 run_once has no safe post-
finish/pre-save extension point and must not be disguised as backend-only work.

Owner scope policy is loaded privately, default off, fixed before claim
execution. Task text, checkpoint flags, model output and merely observing an
active native Goal cannot grant a new managed execution profile. Once a scope
uses this path, later work in that scope cannot silently fall back to 3198 if
the policy/module/strong-model route is unavailable. Record a wait instead.

### B. Goal-aware backend, same native identity

Use the existing candidate algorithms under an isolated, pinned optional
backend/transport closure; do not reimplement the native model loop. Keep the
existing native auth-home/provider identity and selected thread. Before any
potentially work-producing resume/start/set/steer, synchronously persist the
original task/epoch intent and effect=true through its actual authority.

- Existing Goal: read actual native state. Never restore checkpoint status,
  replace the objective, reset createdAt/accounting or reactivate owner pause.
- Fresh explicitly authorized Goal: one task-input turn precedes a necessary
  initial goal/set. Do not race a set-created auto turn with a second turn/start.
- Active resume: adopt real same-thread turn/started; deliver current authorized
  input once to its precise turn through the existing intent/hash/receipt path.
  Unknown steer stays unknown, never retried on model/node/epoch change.
- Keep effect=true between every automatic turn. Persist distinct adopted and
  completed turn IDs and the current plan; an old completed turn or plan does
  not stand for the next turn. Queued host requests/notifications retain wire
  order and original task/turn correlation.
- Continue heartbeat/fencing while waiting; do not interleave optional steering
  into another outstanding native RPC or drain boundary.

At a terminal Goal, only actual non-active Goal + no active turn + idle and
event consumption permit provisional quiescence. Read the correct rollout
while allowed, then close/reap and inspect all late events before settlement.
No post-close native RPC is allowed; pass frozen path/Goal/proof to persistence.

An interrupted/failed automatic turn is not a successful natural completion.
The candidate permits owner-interrupted/paused terminal quiescence; do not turn
that allowance into proof that an aborted turn's external effects are settled.
Retain every turn's terminal status and keep unknown external operations under
their original guards unless the existing original-case evidence closes them.
Runtime-closed metadata is not detached Shell/MCP/remote-effect closure.

### C. Active Goal waiting for children

Pass the Worker's real wait-children signal into finish. Reuse the existing
opt-in **owned Unix** admission-drain path, not stdio SIGTERM, unsubscribe,
fork, artificial Goal pause/complete, or a fixed idle sleep.

After one persisted/ACKed PID drain intent, consume every admitted turn and
host reply to natural completion, then natural exit/EOF/reap. This is only
`drained_unverified`. Reuse the independent unloaded observer and original
history-prefix/full-file proof to get `yielded` while the same Goal stays active.
Check same thread/objective/createdAt/budget and nondecreasing tokens/time.
Any late/aborted/unknown admitted turn or forced cleanup retains the guard.

### D. Worker save/finalization gate

The scoped strategy must replace 3198's unconditional false-guard save:

1. Keep task and saved session guard=true throughout observation and upload.
2. Upload only the sealed original file using the existing fingerprinted save;
   no second native_rollout/goal RPC after closure and no archive recreation
   as a substitute for an unknown old upload.
3. Require original same-task/epoch commit ACK with
   `ok/saved/artifact_saved/scope`, not HTTP200 or a local return alone.
4. Only then atomically write the original task's full lifecycle proof,
   effect=false and the correct terminal/wait state; require final update ACK.
5. ACK loss reads the original receipt/row for reconciliation. It never reruns
   native work, sends a second steer, clears old chunks or fabricates a seal.

3198 session commit lacks the additive `ok` field, while the current candidate
requires it. Include a narrow same-authority ACK change after the original
commit succeeds, retaining all existing fields; never synthesize ACK for a
failed/ambiguous commit or relax the candidate's gate to suit the old server.

### E. Authority active-yield fence

The new authority facade/candidate must reuse these exact existing rules:

- `waiting_children` never manufactures effect=false.
- An active Goal can accept a false guard only with a valid same-thread,
  same-Goal, runtime-closed/settled yielded checkpoint and a permitted wait state.
- That yielded Goal reserves its native scope through waiting_children,
  parent pending/wakeup and preflight auth/backend waits; a different task may
  not take its thread merely because no runtime currently holds a live lease.
- Reuse original child completion wakeup and same parent row/new epoch. Neither
  snapshot merge nor a new executor grants an old operation execution rights.

Do not copy the complete modern Store. Extract/reuse the reviewed claim/update
conditions and additive session ACK as a narrow same-ledger facade/patch;
preserve passive construction, existing final hooks, SQL transactions, inbox,
Claw and all prior unknown rows. Any changed Store/Worker/sessions/backend pin
belongs to the explicit new candidate manifest and its deployment review.

## 5. Installation and compatibility boundary

- Build in a new owned source directory; pin the exact frozen base, changed
  members, optional import closure and launcher. Never run from a live dev tree.
- Legacy VPS Python3.6 needs a pure compatible closure: Goal backend, Worker
  strategy, native WS/history/observer and save helpers; no MCP/FastMCP/FastAPI
  or modern OAuth modules in that process. Inspect syntax/import dependencies
  and use bounded local units before Root's host-specific installation check.
- Reuse the verified native executable and owned socket support; do not upgrade
  the system Python, shared Codex installation or auth profiles for this leaf.
  A different native version requires its own source/capability pin, not guessed
  compatibility with the historical 0.159.2 socket trial.
- Existing governance, role/model/effort selection, Entrance closed-failure and
  metadata observer ordering stays explicit. Other scopes retain their original
  path and do not inherit a Goal or lifecycle conversion from this rollout.
- Enable only explicit future normal scopes under owner policy after the
  authority/save/Worker pieces are all installed. Old unknown tasks/scopes are
  not admission samples and are not silently opted in by checkpoint presence.
- Rollback retains new tables/receipts/native history and stops new admissions
  to the new path first. Do not drop back to 3198 for an admitted active scope;
  finish or hand off its original lifecycle under its installed version.

## 6. Small verification and handoff

Implementation should add only the missing seam checks: immediate auto-turn
after first completion; abort retaining unknown; existing Goal accounting and
once-only resumed input; one claim/scope dispatch with old-path compatibility;
and active-yield scope reservation plus commit/finalize ACK-loss handling.
Reuse existing lifecycle/yield fixtures rather than another full matrix.
No passing mock is a real native lifecycle roundtrip.

Root may perform a serialized unit-tested deployment. New model/transport/
parent-child integration and fault acceptance belong to the Leader's existing
weekly queue, using a new authorized future case rather than the old attempts.
Original parent/children need a separate read-only case audit of every native
turn, native closure, exact history/chunks/commit and external operation effects;
their first-turn completion or source commits cannot authorize a replay.

This contract does not resolve those old cases, complete PAM-007, prove native
history migration, or claim the current production runtime has been repaired.
