# Pinned modern native worker release

This is SOURCE assembly, not a running worker or a native-inference proof.
No account/token/worker-config/ledger is read, and no process/model starts while
building. Root alone chooses the maintenance window, installs/stages/activates
the candidate and accepts any original task's reviewed continuation.

## What changes, what retains its owner

The generated launcher imports the hash-pinned incumbent `20261028.1` launcher
and still calls its `main()`. Its original Governance instance, policy/state DB,
governed scopes, receipt writer, metadata observer, private phone profile loader,
worker configuration path and rolling `run_worker(factory)` remain there.

The new namespace contains the reviewed modern Codex/lifecycle modules and a
modern claimed-task strategy extracted from the current Worker. The existing
Worker class remains the base: constructor, authenticated client, resource/CLI
references, tools and interaction callback are inherited. The adapter claims
once then runs that strategy. Original wait-children results are interpreted
before requesting a native yield; an already-ready result does not force a new
turn/child wait. Both original and modern session-save modules get the existing
metadata observer. No VPS Store/API/receiver/entrance is included or replaced.

Modern terminal/yield paths reuse the existing qualified converter's full
history fingerprint, owned-runtime closure and commit-ACK ordering, including
plain turns with Goal=None. Unknown native work, failed history/ACK or a missing
safe closure does not become a rolling checkpoint. The original task/thread/
operation and its full native history stay bound.

The release uses owned Unix native transport with `native_goal_drain=true` and
`native_goal_yield=true`, applied to a temporary configuration copy. Original
auth_home, account roster, executable, workspace, permissions, Goal budgets and
on-disk configuration are unchanged. The installed native executable must really
support this protocol; a source manifest does not attest the binary or account.
This release rejects Pi-configured workers and implicit cross-account/native
history migration. Such cases wait rather than silently change harness/history.

## Goal and profile semantics

Existing threads always use `thread/resume`, preserving the exact thread. A real
`thread/goal/get` precedes release of resumed host requests or explicit next-turn
input. For a first `continue_remaining` resume the observed Goal must match the
owner acceptance snapshot; changed/active unexpected Goal or stale host work
fails closed. An existing Goal is never reset from a checkpoint/config. A node
default objective can create a Goal only on a genuinely new thread; it cannot
create one while resuming the S2 Goal=None thread.

Additional scope→profile bindings use a separate private release policy:

```json
{
  "format":"amita-native-worker-release/1",
  "revision":1,
  "additional_profiles":[
    {"scope":"leader:project:amita-control-quorum","profile_path":"/private/existing-phone-profile.json"}
  ]
}
```

The original phone binding remains untouched. Root may point S2 at the same
already-authorized Sol/high profile. Values are not compiled into this release:
future model/effort updates use the existing mutable private profile and actual
catalog. Unmatched scopes keep their original profile/Governance path; no global
model whitelist or prompt-based effort claim is introduced.

The original Governance factory still calls `make_codex`. The release's small
reasoning facade enables the modern optional thread-profile contract: validate
the initialized native model catalog, send the chosen `model` and
`config.model_reasoning_effort` on thread start/resume, and verify the returned
native configuration. Explicit turn/start also sets `effort` and the overriding
`collaborationMode.settings.reasoning_effort`. Unsupported or mismatched fields
reject; there is no downgrade to a default. Receipts say native configuration
was verified, **effective inference remains unverified**. Root must read the
matching new turn's actual native `turn_context` metadata before reporting the
effective model/effort.

## Reproducible source inputs

`deploy/build-native-worker-release.py` pins current algorithm files and the
already reviewed native-goal converter. Runtime output hashes and the pure
relative-import graph are in `native-worker-release.json`. Pure runtime modules
are Codex, provider/cost checks, reasoning, native socket/Goal observer/history,
sessions, the claimed Worker strategy and the extracted S3 resume guard. Config
access crosses only the original parent's config module. It imports no modern
Store, server or maintenance authority and opens nothing on import.

Supply a JSON source manifest for these ORIGINAL fallback modules:

`worker,codex,config,governance_worker,leader_governance,metadata_diagnostics,
task_scoped_profile,rolling_worker,rolling_runtime,reasoning,sessions`.

Each key maps to `{"snapshot":"/capture/MODULE.py","installed":"/actual/MODULE.py",
"sha256":"EXACT_SHA256"}`. These are public Python sources, never runtime
configuration or credentials. Capture the actual resolution through the
incumbent namespace, not an unrelated checkout. The builder checks snapshots;
the launcher checks all installed pins and new outputs before importing the
incumbent. It then anchors the new package while retaining the complete original
namespace fallback. Original files are never overwritten.

```sh
python3 deploy/build-native-worker-release.py \
  --destination /private/new-native-worker-source \
  --launcher /capture/launch_worker.py --launcher-sha256 EXACT_SHA256 \
  --installed-launcher /actual/20261028.1/launch_worker.py \
  --package-init /capture/assistant_mesh/__init__.py --init-sha256 EXACT_SHA256 \
  --installed-init /actual/20261028.1/assistant_mesh/__init__.py \
  --incumbent-manifest /private/incumbent-python-source-pins.json \
  --policy-file /private/native-worker-release-policy.json
```

The running spec still invokes `launch_worker.py --config ORIGINAL_CONFIG`.
Build/import do not qualify native RPC availability. Root should stage first,
observe readiness, then activate under the existing rolling protocol. The old
retained/unknown runners and their work remain untouched.

## Actual original S2 continuation

Keep the existing `amita-control-quorum-20261029` worktree/branch and its already
created HEAD. Root accepts **continue_remaining**, preserving the original
task/epoch/thread and full three outer-tool receipts, only after this release is
qualified. The new input must say worktree creation is completed and specify
remaining source work; never repeat worktree add or submit a replacement task.
Normal claim increments epoch; acceptance/build do not start a native turn.
Already-delivered phone tools use reviewed completion and require no model turn.

For old wrapper retirement, keep the existing S3 dedicated-maintenance receipt
resolver: a trusted local control composition supplies a Client which reads the
original authority's immutable acceptance id. Never put that credential into
the worker policy or accept a model-supplied receipt. Retirement remains
default-off unless the local supervisor supports and installs this resolver;
use original stage/activate as the safe transition. No signal, fake checkpoint,
new management privilege or clearing of old work unknown is part of this release.

No new authority/business database tables are introduced here. Existing local
Governance/metadata/profile files retain their original owners and locations.
