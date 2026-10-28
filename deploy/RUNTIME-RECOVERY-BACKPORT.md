# Pinned recovery overlays

`build-runtime-recovery-backport.py` assembles source only. Run it locally, then
stage its small output directories through the existing release workflow. It
does not read configuration, credentials, histories or a ledger, and does not
install, start or stop anything. The original source files must stay in place.

The receiver input is the actual 31-file source snapshot, SHA-256
`d27e5fc5f24e86f79fa69a42dbe27c8cf6fd86d8cc50d23e5305061925b499e5`.
Its actual helper is
`/home/ai_agent/.local/share/personal-assistant-mesh-tools/amita-receiver-isolation-20261026.1/assistant_mesh/amita_task_methods.py`,
76,392 bytes, SHA-256
`2ee2b5db192426d2c5882684d76c8f3c8a2b7960b3fb0a679a6500d9836c42c6`.
The same directory's `operation_isolation.py` is 12,945 bytes, SHA-256
`5f58ffaf130a6e86090eec9f7d78267dd1388b0f5a31387f516bcdec105c0c84`.

The new helper imports that unchanged pinned source and overrides only
`_claim_in_db`, `_update_task_in_db`, `_control_task` and `retry_waiting`.
The isolated Store factory binds these methods with its original Conflict
class. Original Store construction, transaction/final hooks, entrance/media,
Worker, Codex, sessions and history remain inherited from the actual release.
Both new unknown reports and already-persisted backend waits are held; a
preflight quota failure retains its normal backoff. Paused/unknown old work is
not reconciled or resumed by installation.

From the repository root, prepare a receiver overlay that delegates to the
already-staged phone candidate:

```sh
mkdir -p /var/tmp/amita-runtime-recovery-release-20261028
python3 deploy/build-runtime-recovery-backport.py receiver \
  --snapshot /var/tmp/amita-phone-baseline-20261028/snapshot.json \
  --snapshot-sha256 d27e5fc5f24e86f79fa69a42dbe27c8cf6fd86d8cc50d23e5305061925b499e5 \
  --phone-candidate /var/tmp/amita-phone-release-20261028/receiver-source \
  --installed-phone-root /home/ai_agent/.local/share/personal-assistant-mesh-tools/amita-phone-receiver-20261028.1 \
  --destination /var/tmp/amita-runtime-recovery-release-20261028/receiver-source
```

Use the overlay's `launch_receiver.py` as the final receiver entry. It verifies
and imports the staged phone launcher, then calls its original nested `main`.
The phone ready chain and original-file pins run unchanged, with the recovery
namespace already first on the import path. Stage all four generated source
files and its report together; switch the one receiver once. The test-only
`--installed-methods-source` relocation option is unnecessary for this release.

Prepare the laptop control overlay from its actual pinned source:

```sh
python3 deploy/build-runtime-recovery-backport.py control \
  --source /home/gihggiy/.local/share/amita/control/20261026.2/assistant_mesh/rolling_runtime.py \
  --source-sha256 1d6feb38369d62d09f8bdf4f05a998fd8fb614892d0a43766459ca25be347271 \
  --package-init /home/gihggiy/.local/share/amita/control/20261026.2/assistant_mesh/__init__.py \
  --init-sha256 3c382e0fac38eb8650a900925a2cf3edd64e4fd766d3b0d8a5c0d578c582899b \
  --destination /var/tmp/amita-runtime-recovery-release-20261028/laptop-control-source
```

Install this directory as a new control version and select it through the
supervisor unit's existing WorkingDirectory/PYTHONPATH. Keep its current
`--state /home/gihggiy/.local/state/amita-runtime/laptop serve`,
`KillMode=process` and original source directory. Only `Supervisor._recover`
is overridden; its process/database/control functions remain original objects.
No worker command, runner identity or runtime database is rewritten. A control
restart reconnects the same workers; source-owner-only readmission still needs
fresh PID/birth/heartbeat, idle work and fenced peers.

For VPS control, supply its separately captured actual source/init and hashes;
set `--installed-source` and `--installed-init` to their unchanged VPS paths.
The laptop hashes do not attest the VPS version. Exact source seams and hashes
are checked before output. Each output requires a new absolute destination.

Validation: Root executed Click Evidence `runtime-recovery-actual-backport`,
`python3 -m unittest tests.test_runtime_recovery_backport.RuntimeRecoveryBackportTests`,
and both actual-source cases passed in 0.107 seconds without reuse. This covers
the frozen receiver's real factory/helper composition and temporary ledger,
plus the actual laptop control's same-runner readmission. Production deployment
and real service acceptance remain separate.
