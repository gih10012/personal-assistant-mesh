# Scoped native Goal: existing launcher wiring

Implementation handoff on `work/native-goal-scoped-fixes-20261024`, following
the accepted scoped candidate and its four fixes. No production, account,
running ledger, original task, native process or model was changed here.
Root owns the private policy, grants, launchers and serialized installation.

## Thin composition

`assistant_mesh.native_goal_launch` adds three owner-code helpers:

- `load_policy(path=None)` uses the existing private-file loader and exact
  scoped policy validator. No file means default off; no identity, executor,
  scope or activation time is invented.
- `compose_worker(...)` returns one class. A listed enabled executor uses the
  existing `assemble_worker` beneath its existing governance/profile/Entrance
  closed-failure decorators. Other workers use the original launcher factory.
  The existing metadata callbacks register BOTH save modules for the selected
  path, then wrap the resulting Worker last. No worker/model is constructed.
- `serve_entrance(...)` wraps the existing `entrance_store_class` composition
  point, before the original Claw/passive factory constructs its sole Store.
  The frozen receiver still forces `recover_inflight=False` and owns the same
  socket, channel, API and close chain. Its original ingress/final/emit/feed
  ready chain runs before `attach_authority` wraps that same API dispatch.
  The temporary class factory is restored at ready and on every failure.

The helper has no installer, listener, poller, authentication material or
ledger of its own. An explicit disabled policy keeps the authority facade
installed, preserving durable bound-scope fences on rollback. After admission,
removing the policy/helper is not a supported rollback.

The source builder now includes and pins this additional top-level module,
records `launch_source_sha256`, and parses it as Python 3.6 syntax. The twelve
optional algorithm modules, scoped assembly pin and original core edits stay
unchanged. Build/transfer the final artifact once after integrating this leaf;
do not run production from the branch. Retain the separately pinned existing
governance/profile/metadata support modules used by the original launchers;
the 58-member frozen base does not contain those installed support overlays.

## Apply to the existing private launchers

The callable recipes in
[`native-goal-launch-fragment.py.example`](../deploy/native-goal-launch-fragment.py.example)
are composition points, not new service entrypoints. Keep the original runtime
roots, config, instructions, profile paths, receipt append callback, `--once`
behavior and service identities. Add the optional `--native-goal-policy` flag
without a default path; load that explicit private file once.

1. In cloud Leader's `launch_profiled_worker.py` and the computer's
   `launch_worker.py`, replace only the existing `make_worker`/metadata class
   block with `governed_class(...)`. Supply ORIGINAL Worker/Codex, existing
   Governance/scopes, the same profile loader (cloud only) and receipt callback.
   Keep the final original construction and `run`/`run_once` call.
2. In the Claw branch, `claw_run(...)` retains the original profiled backend,
   EntranceWorker constructor, failure preservation and pinned native runner;
   its temporary Worker factory is restored in `finally`. Claw is currently
   profile-only. A Leader-only policy leaves it on that path. Selecting Claw
   later needs an independently configured Governance and governed scope;
   missing governance fails closed, and this change grants neither.
3. In the sole `launch_final_receiver.py`, call `receiver_run(...)` with its
   existing `composed` ready callable and the same socket/config. Keep the
   original final/emit/feed policy inputs. A previous inner readiness print
   occurs before the new capability check and cannot prove scoped readiness.

Root must supply actual worker peer grants already approved for the exact
executors. The helper only checks `owner.native-goal.lifecycle`; it never adds
that capability or changes an operator/worker role. Configure the qualified
workers and authority together in the serialized cutover before allowing new
scoped claims. An enabled authority must not dispatch to an executor still
running its old default-off launcher. Native executable/socket compatibility
and actual per-auto-turn model/effort remain the existing deployment/Leader
acceptance boundaries. The original final-ready checks still apply separately.

## Existing continuous legacy scope: explicit remaining gap

First binding still rejects ANY existing `native_sessions` row for the scope,
even if its last legacy task appears completed/effect=false. The legacy
first-turn boundary can omit later automatic work; that flag is not a sealed
native lifecycle. The original `leader:owner` needs_review and workspace
parent/children unknown cases remain blocked. A new scope may not be used to
replay them or relabel the same business as fresh work.

A future same-scope transition needs a separately reviewed adoption protocol;
none is enabled here. Its minimum design is:

1. Reconcile the original case and pin the exact same scope/task/epoch,
   native thread, executor/auth-home/provider, actual Goal/accounting, complete
   original history, committed artifact/chunks and settled external operations.
   Preserve owner pause and unknown guards. No Goal reset or replacement thread.
2. Fence that scope's admissions during the transition. In the SAME authority
   transaction, compare the exact reconciled source session/task revisions and
   artifact proof, then append an immutable owner-authorized adoption receipt.
   It must not rewrite the old history, effect flags or acceptance receipts.
3. Admit only subsequent explicit owner work against that receipt through the
   existing continuation protocol, retaining the original task/thread. Current
   `created < activated_at` admission also rejects the original legacy task;
   receipt-bound admission must handle that exact source explicitly. Merely
   removing the existing-session rejection would leave this gap and weaken the
   fence. Conflicting/new source revisions must fail closed rather than adopt.

This is a plan, not authority to adopt any history. Root's original-case
reconciliation and Leader's real lifecycle queue remain separate.

## Bounded verification

The existing nine scoped units plus final continuation regression remain the
algorithm evidence; do not rerun them for this composition leaf. Four new
units exercise only default/nonexecutor routing, the real decorator/save
composition, the single passive Store with original ready/close and disabled
fences, and readiness/pre-ready failure cleanup. They use synthetic callbacks
and temporary SQLite, never an actual native process/listener or original case.

Set `MESH_NATIVE_GOAL_TEST_SOURCE`, or pass Python's explicit
`-X mesh_native_goal_test_source=/absolute/built/source` option, to Root's
already-built source artifact to reuse it read-only. The new module is loaded from this branch; unchanged
algorithms/core load from that artifact. Without this variable, CI can reuse
the existing historical fixture builder. Run only
`tests.test_native_goal_launch.NativeGoalLaunchTests` through `click-gate verify`;
the handoff records that invocation's actual outcome. This is not deployment
or native/model acceptance.

Root continuation on 2026-10-25: the original specialist stopped at quota;
the same branch's files were preserved. The four new class-scoped units ran
through Click successfully (one targeted shard executed, nothing reused).
No existing algorithm suite was rerun. Import the old fixture as a module so
normal discovery does not accidentally collect its nine TestCase methods again.
Use the ordinary class-scoped command; a small fixture build is preferable to
repeated attempts to pass unsupported environment prefixes through the Hook.

The actual approved 94bc0146 production input, unlike the historical fixture,
produced entrance_runtime SHA-256
`746761a50f2b08eb74ca81e25f7394c53096070cc35121c2892a752e06d9eb73`.
Use the real build manifest, not a fixture output pin, for deployment. Preserve
any later owner adapter fix when selecting the current production base.
