# Cooperative retirement of an owner-accepted old runner

This SOURCE-only builder composes the inspected recovery parent with its exact
original fallback. It adds the already-reviewed `RuntimeStore.retire_closed_runner`
method, the authenticated heartbeat projection and the existing same-UID owner
control action. It does not replace modern `rolling_runtime.py`, restart a
service, signal a process, clear work/checkpoints, change business guards or
read a production ledger/credential during build.

The explicit supported sources are pinned in
`build-runner-retirement-backport.py`: recovery runtime, its base runtime,
recovery namespace init and existing control launcher. The builder verifies the
recovery source's explicit fallback matches the supplied installed base path.
It also pins the exact reviewed modern retirement method, rather than copying
the modern RuntimeStore constructor/transactions/recovery into an old parent.
Unrecognized sources or changed method bytes are refused before creating a
candidate directory. The previous service/runner is not manipulated by build.

## Build and deployment-owned resolver

```sh
python3 deploy/build-runner-retirement-backport.py \
  --destination /PRIVATE/NEW-CONTROL-RELEASE \
  --runtime /PRIVATE/CAPTURE/RECOVERY/assistant_mesh/rolling_runtime.py \
  --installed-runtime /ACTUAL/RECOVERY/assistant_mesh/rolling_runtime.py \
  --base-runtime /PRIVATE/CAPTURE/BASE/assistant_mesh/rolling_runtime.py \
  --installed-base-runtime /ACTUAL/BASE/assistant_mesh/rolling_runtime.py \
  --package-init /PRIVATE/CAPTURE/RECOVERY/assistant_mesh/__init__.py \
  --installed-init /ACTUAL/RECOVERY/assistant_mesh/__init__.py \
  --launcher /PRIVATE/CAPTURE/RECOVERY/launch_control.py \
  --installed-launcher /ACTUAL/RECOVERY/launch_control.py \
  --maintenance-client-config /PRIVATE/native-maintenance-client.json
```

Captured and installed paths are separate. The destination is new and absolute,
mode 0700, with files mode 0600; it is never overwritten. `--support-root` may
select the reviewed merged source root. Optional `--runtime-pins` is a JSON
object of additional installed absolute source paths to SHA256, verified at
startup (the builder does not open remote pin paths). The manifest records all
parent pins and generated module hashes. A transferred candidate resolves its
own script directory, without embedding a temporary local build path.

The old control-only namespace contains no Worker/config modules. Therefore a
small stdlib `runner_retirement.MaintenanceReceiptClient` supports only the
existing private `control_url`/`token_file` config contract. Configuration must
have exactly those two keys, use verified HTTPS or a Root-verified loopback HTTP
tunnel, and reference an owned regular mode-0600 token file. The config itself
is owned regular mode 0600, with absolute paths and no symlink ancestors. This
is the dedicated `native_maintenance` peer credential already authorized at the
original authority, not an operator/worker or personal account login.

Only the installed launcher reads its embedded fixed config path and installs
the resolver before constructing the original Supervisor. Neither the builder
nor ordinary module import reads credentials. The client refuses any other
route, redirects or ambient proxy routing. Its only authenticated request is:

```json
POST /v1/maintenance/native-partial/receipt
{"acceptance_id":"ORIGINAL_ACCEPTANCE_ID"}
```

The resolver requires the original accepted receipt's protocol and acceptance
ID. The copied reviewed retirement method then checks the full evidence basis,
task/epoch/thread/turn, original runtime work hash, runner PID/birth and absence
of owned children. Missing/wrong closure, changed work or unverified process
identity rejects retirement. Default without the deployment resolver is closed.

## Parent rolling assembly and owner action

Root controls the parent maintenance window and actual service overrides. Use
the same existing private runtime `--state`, same working directory as needed,
and the **new** `launch_control.py` as the service entry:

```sh
python3 /PRIVATE/NEW-CONTROL-RELEASE/launch_control.py --state /PRIVATE/EXISTING-RUNTIME-STATE serve
```

Keep the original process-only supervisor shutdown behavior so existing workers
and native processes remain alive during parent replacement. The new namespace
is first and preserves the original namespace init bytes/fallback chain. The
original RuntimeStore constructor, transaction function, PID recovery,
Supervisor start/UID handling, reconcile and recovery implementation remain
the actual old objects. The unguarded incumbent launcher is pinned but never
imported/executed as part of composition.

There is no new public/remote/model API or caller-selected resolver. Through the
existing owned mode-0600 Unix socket, the original same-UID owner may request:

```json
{"protocol":1,"action":"retire_closed","body":{"operation":"ORIGINAL_RETIRE_OPERATION","runner":"ORIGINAL_RUNNER_ID","acceptance_id":"ORIGINAL_ACCEPTANCE_ID"}}
```

Only these fields are accepted. Caller receipt bodies, config paths, resolver
names and extra fields are refused. A runner-bearing request stays on its old
token/PID-authenticated runner path and cannot invoke the owner action. The
socket Handler still enforces real SO_PEERCRED same UID.

Retirement appends an immutable local receipt retaining the **exact original
work string**, requests draining and disables that runner's own automatic
recovery. It does not clear work or fabricate a safe checkpoint. Every heartbeat
still completes the original token and observed PID/birth validation first.
Only then can an accepted retirement project `state=stopped,busy=true` so that
the original adapter cooperatively exits. The database work remains unknown;
the process is not signaled and no business effect is replayed. A later original
operation replay returns its existing receipt without resolving a new proof.

Source completion, trusted retirement admission and actual adapter exit are
separate outcomes. Root alone submits the real original acceptance and verifies
actual cooperative exit/next ready runner; this worker has not done so.

## Root checks

No tests/checks were run by this implementation worker. Three leaf fixtures use
the actual pinned old source composition with isolated temporary control DBs,
a synthetic trusted authority and their own process identity. They do not open
real runtime ledgers, private maintenance configs, tokens or business histories.
They cover constructor/transaction/recovery preservation, authenticated
heartbeat ordering and raw work/append-only receipt retention, and the original
same-UID socket control path rejecting injected receipts/configuration.

Click short id `s3retire`; exact argv:

```sh
python3 -m unittest tests.test_runner_retirement_backport.RunnerRetirementBackportTests
```

Absolute workdir:
`/home/gihggiy/Github/personal-assistant-mesh-worktrees/runner-retirement-backport-20261030`.
