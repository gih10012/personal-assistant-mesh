# Local worker rolling runtime

Branch: `work/amita-rolling-runtime-20261026`, base `409572b`.
This adds runnable Linux process control. Root has now installed the laptop
supervisor and original worker adapter, completed an actual idle version roll,
then upgraded the parent while both original worker PIDs remained alive and the
existing authority API stayed reachable. Confirmed-death recovery is installed;
in-flight long-task handover and VPS worker adoption are still pending. See
[current deployment](AMITA-DEPLOYMENT-2026-10-26.md). The existing receiver,
SQLite business authority and private owner
launchers remain the sources of truth; this module creates none of them.

## What runs

`python3 -m assistant_mesh.rolling_runtime --state /absolute/private/control serve`
starts one stable supervisor with an owned 0700 directory, a 0600 Unix socket,
an exclusive process lock and `runtime.sqlite3`. It imports no Worker, Codex,
account configuration or model catalog. Its status and queued launch intents are
durable. `status` reads the journal even when the supervisor or a runner is down:

```sh
python3 -m assistant_mesh.rolling_runtime --state /absolute/private/control status
```

The journal contains worker version/process identities, local admission state,
control operation receipts and original task ID/epoch references. It contains no
duplicate prompts, native histories, business queue or account credentials.
Runner tokens and owner launch specs are private local control data. Status
does not print the launch argv, environment, token or log contents.

The existing ingress/API/channel supervisor owns communication, discovery,
business queues and iLink. It continues independently when an execution process
exits. This supervisor never starts a receiver or renews a channel holder term.
`kind=receiver` is rejected; worker launch specs must point ONLY to a worker
entry point. Arbitrary owner launch commands remain trusted host configuration.

## Thin connection to existing launchers

Keep the laptop/cloud launcher's complete existing governed/profiled/native Goal
class assembly, receipt callbacks, observers, original config and source roots.
Change only its final continuous worker invocation:

```python
import os

# Inside the existing worker branch, after its original class composition:
if os.environ.get('AMITA_RUNTIME_SOCKET'):
    from assistant_mesh.rolling_worker import run_worker
    return run_worker(lambda: cls(config, config_path=args.config))
return cls(config, config_path=args.config).run()
```

Move any account/model initialization into that factory if a particular private
launcher currently performs it earlier. `run_worker` registers process readiness
and waits in standby BEFORE calling the factory. With the public Worker, a
direct executable is also available:

```sh
/absolute/python3.12 -m assistant_mesh.rolling_worker --config /absolute/private/worker.json
```

It requires the supervisor-provided `AMITA_RUNTIME_SOCKET`, `AMITA_RUNNER_ID` and
`AMITA_RUNNER_TOKEN` environment. It reads worker configuration/imports Worker
only after activation. For the actual laptop/cloud use the original composed
launcher above, preserving existing governance/native Goal/profile/failure
decorators. No model/harness routing is introduced here. Any independently added
`GovernedWorker.model_recovery_state(task)` remains on that composed instance;
model capacity never controls supervisor or standby readiness.

`run_worker` keeps the original `run_once`, `tick`, backend, native lifecycle,
session publication and task finalization. Its Client wrapper gates the ONE
original `/v1/claim`; all business requests go to the original Client exactly
once. An admission permit issued just before a drain can finish its existing
claim. No later old-runner admission is issued. Only original task ID/epoch
references enter the runtime journal; session/thread/operation IDs stay at the
original authority and in the original native history.

For an existing native automatic Goal, its original `run_once` must cover the
whole managed lifecycle, as the scoped native Goal strategy does. First-turn
completion, `current=None`, an idle status or a local process exit alone never
allows transfer. The adapter requires the confirmed final task update, a false
effect guard, and any managed Goal's settled/closed terminal or yielded seal,
then return from `run_once` with no live native references. A yielded active
Goal is accepted only with the same thread/goal and `waiting_children` status.
The existing strategy's independent observer/history/session save remains the
proof source; this layer does not replace or weaken it.

## Roll and rollback

Copy the two new modules and their normal package dependencies to a pinned
control source directory. Use Python >=3.8; the VPS's private 3.12 can supervise
an existing 3.6/native process without replacing its interpreter. A newly managed
worker imports the adapter from its own pinned source; do not alter a live import
tree or overwrite the deployed entrance media fix.

Install the supervisor service using
`deploy/assistant-mesh-rolling-supervisor.service.example`, adapting its exact
interpreter/control source paths. Keep `KillMode=process`; runners are detached
and supervisor shutdown does not signal them or any receiver/native process.
The existing server/receiver/tunnel services remain independent. Do not leave
the original worker unit also auto-launching an unmanaged duplicate.

Create an owned 0600 launch spec from `deploy/rolling-worker.json.example` using
the exact original laptop worker or cloud Leader/Claw WORKER command. Every
candidate release has its own immutable cwd/source path and version label.
The supervisor's current protocol is 1. Adjacent worker releases share protocol
1 and schema major 1; optional additive fields are tolerated. A range containing
1 is accepted. An incompatible range is rejected before draining anything.
Schema changes are additive short transactions; no authority database is copied
or restored and no transaction spans a native/network call.

```sh
python3 -m assistant_mesh.rolling_runtime --state /absolute/private/control \
  roll --operation-id release-v2 --spec /absolute/private/release-v2.json
```

This queues one idempotent launch, waits for the child's authenticated ready
handshake, then atomically activates it and closes the previous version's new
claim admission. `stage` + `activate --runner ID --operation-id ID` expose the
same steps separately. An operation ID with different content is rejected.
Repeating a launch operation never creates a second process. A crash between
launch intent and PID receipt stays unknown and is never auto-launched again.
Its real same-user peer may actively reconnect using the original runner token:
the supervisor verifies SO_PEERCRED PID/birth and kernel-observed argv, executable
and cwd against the original owner spec. JSON pid/work fields cannot establish
identity or clear busy work. Unverifiable wrappers/observations remain unknown.

The old runner completes only its admitted work. A sealed checkpoint permits
the original authority to resume the original task/session at its normal claim
boundary. While an old native operation/Goal cannot safely hand over it remains
alive, renewing its original lease through its original Worker; the new version
can process independent work. There is no forced drain, native interrupt,
artificial child wait, new task/operation ID, Goal recreation or unknown replay.

After a safe drain, the old process stays as rollback standby. It keeps its
ready control heartbeat and cannot claim. Candidate startup failure leaves the
old active. If an activated candidate exits or its control heartbeat goes stale,
the previous healthy ready standby/draining runner becomes active. A timeout
only closes admission and retains the suspect process; it never proves death
or permits a signal. If every remaining runner is confirmed dead and no healthy
standby exists, deterministic recovery queues a new process attempt using the
same owner spec, a NEW local control operation/runner ID and a `restart_of`
reference. Old runner/task/work records remain intact. The new worker still
claims through the original authority's task/epoch/effect guards; restarting a
process never authorizes replay of an old business operation.

`autorestart` defaults to true when absent, including old persisted specs;
setting it to false opts out. Missing defaults are never inserted into an old
stage request or its idempotency fingerprint. Only a known PID/birth's missing
process, zombie, PID reuse, confirmed old boot or owned Popen exit proves death.
Failed `/proc` reads, heartbeat/ready timeouts and missing PID receipts do not.
Launch failures have persistent exponential backoff from 1 to at most 60 seconds
(`restart_backoff={"initial":1,"maximum":60}`); owner values may use an initial
delay >=0.2 seconds and a maximum <=60. Thirty healthy active seconds reset the
failure count. `status.recovery` exposes the next attempt and pending runner.
The supervisor/status stay available during repeated startup failure. Draining
the current active/source/pending runner disables its component's automatic
recovery; candidate activation establishes the new owner-desired source.

Manual rollback uses `activate` on the original ready standby ID and a new
control operation ID. Retire an unnecessary known-good standby explicitly:

```sh
python3 -m assistant_mesh.rolling_runtime --state /absolute/private/control \
  drain --operation-id retire-v1 --runner ORIGINAL_RUNNER_ID
```

Retirement is cooperative and waits for any admitted work; there is no force
kill command. An unknown business claim/final receipt keeps the original task
reference and blocks only that runner's further claims. The original authority
owns receipt reconciliation. Safe control-checkpoint ACK loss is different:
only that same idempotent local metadata handshake is retried after reconnect,
without touching the original authority/native action again.

## First enrollment and the two existing nodes

An already running old process cannot acquire a claim gate retroactively. Do
not pretend adopting its PID proves readiness or quiescence. Root first connects
the adapter at the existing launcher's next confirmed safe startup boundary.
If the current legacy runner has unknown or unsealed native work, retain it and
its existing authority/session; enroll another independent worker component
only where it cannot race that unmanaged runner's claims. The adapter alone
does not fence an unmodified process. Once both versions use this gate, normal
rolls need no manual restart or second receiver.

On the laptop, adapt only the existing laptop worker invocation; keep the tunnel
and native desktop helpers. On VPS, adapt only existing cloud Leader/Claw worker
invocations as separate component labels; retain the sole existing receiver and
all ingress/final/feed/source-pin hooks. `native_goal_launch` and its separately
installed native runtime remain unchanged. The parent service can use private
Python 3.12 while an old native process keeps its interpreter and working tree.

Linux is implemented now. Another OS adapter must supply stable PID birth
identity, detached launch, exclusive local-parent ownership, same-user private
IPC and cooperative lifecycle reporting. It must preserve the same admission
and unknown semantics; no unsupported platform is reported as ready.

## Targeted evidence and deployment boundary

The concrete classes are `RollingRuntimeStoreTests`,
`RollingWorkerBoundaryTests` and `RollingRuntimeProcessTests` in
`tests/test_rolling_runtime.py`. They cover admission versus drain, immutable
identities, incompatible versions, idempotent launch/control receipts, safe
native Goal boundaries, lost claim/final ACK prevention, private real child
standby/activation/fallback, supervisor restart and offline CLI status. They use
only new harmless processes and temporary directories; no standing service,
real task/account/model/network or receiver is touched.

Root's one actual acceptance after deployment should exercise the existing
upper CLI/API during a controlled worker roll, confirm the same task/session/
operation references, and distinguish retained unknown work from execution
readiness. This branch's isolated process tests are not that production result.
