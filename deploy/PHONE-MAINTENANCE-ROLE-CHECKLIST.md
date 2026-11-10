# Root-only phone maintenance activation

This is a laptop agent role, attached to the existing laptop Worker and rolling
runner. No hardware identity, bearer, operator privilege or parallel claim loop
is created. Root alone performs actual idle checks, Source authorization,
stage/activate and real incident acceptance. Candidate assembly calls no models.

The package contains a pinned additive release, candidate-config and a private
role workspace/AGENTS.md. Do not edit any 20261101.1/20261028.1 protected source or
replace old expected hashes. The original launcher is imported unchanged; its
native install composes first, then the exact-device role backend composes at
the original already-claimed open_backend seam. The original WorkerStrategy,
Client, tool/activity/interaction callbacks, native save/seal and ClaimGate own
execution. Only backend construction uses the scoped governance facade.

1. Review private candidate-release/phone-maintenance-worker.json. Confirm the
   hashes of original laptop-worker.json, modern-worker-policy.json and original
   leader-governance-v1/policy.json still match. Preserve every old capability,
   scope, default, profile, account reference and native history. The ordinary
   policy is already Luna/max; no global model default changes are needed.
2. Source authorization is the existing worker peer selected by role=worker,
   node=laptop in /home/ai_agent/.local/state/personal-assistant-mesh/server.json:
   append phone-maintenance to that peer's allowed capabilities. Keep the same
   token/config binding. Root applies this only through its original safe Source
   configuration/reload lifecycle. No client can self-authorize the new cap.
3. The local candidate-config/laptop-worker.json only adds phone-maintenance to
   the original caps. Keep it private. It points to original credential paths;
   no login or token bytes were copied. Do not overwrite the old config. The
   staged spec reads the candidate directly so the old runner/config remain a
   rollback option.
4. Copy candidate-release to the installed_target in the manifest, preserving
   mode 700 directories/600 files. Keep candidate-config and workspace at their
   reviewed paths; launcher policy paths are explicit. Existing additional
   profiles remain unchanged. Do not append this scope to the old static profile
   list: its legacy hard binding would defeat qualified route recovery.
5. Use the real amita-laptop-supervisor.service (control/20261031.1) and existing
   state /home/gihggiy/.local/state/amita-runtime/laptop. First inspect the current
   runner with Root's usual status tool. Require actual idle, no claiming/running
   work, and a safe original native/authority boundary. Retained/unknown runners
   stay retained; do not clear their work, fake a checkpoint or re-admit them.
6. Stage candidate-config/stage-spec.json with one original stage operation ID;
   keep its returned runner ID. Wait for actual standby readiness. Recheck the
   original active runner is idle, then activate with a separate original
   activation operation. Preserve previous runner/version for ordinary rollback.
   Root alone sends these commands; this checklist never changes lifecycle.
   The existing control CLI uses these exact flags (do not execute before the
   preceding idle and Source-authorization steps):

   ```sh
   PYTHONPATH=/home/gihggiy/.local/share/amita/control/20261031.1 /usr/bin/python3 -m assistant_mesh.rolling_runtime --state /home/gihggiy/.local/state/amita-runtime/laptop status
   PYTHONPATH=/home/gihggiy/.local/share/amita/control/20261031.1 /usr/bin/python3 -m assistant_mesh.rolling_runtime --state /home/gihggiy/.local/state/amita-runtime/laptop stage --operation-id owner-phone-maintenance-stage-20261109.1 --spec /home/gihggiy/.local/state/amita-phone-maintenance-role/20261109/candidate-config/stage-spec.json
   PYTHONPATH=/home/gihggiy/.local/share/amita/control/20261031.1 /usr/bin/python3 -m assistant_mesh.rolling_runtime --state /home/gihggiy/.local/state/amita-runtime/laptop activate --operation-id owner-phone-maintenance-activate-20261109.1 --runner ACTUAL_STAGED_RUNNER_ID
   ```
7. Only an actual device maintenance incident starts cognition. Its context must
   match this one device scope, isolated memory, node-maintenance origin, exact
   agent+phone-maintenance requirements, D1 and low risk. The existing original
   policy supplies all qualified candidates: Luna/max is a preference; actual
   qualified Sol/high and other authorized zero-marginal-cost candidates remain.
   Account/route/catalog preflight uses the real subscription only for that task.
   Cached failures/backoff/next_recheck_at remain in the original governance DB;
   there is no model heartbeat or grading prompt.
8. Fresh scope/session creates its first native thread. A retained session needs
   original safe closed lifecycle and actual native model/provider/effort receipt.
   Only an identical qualified route can resume; missing/conflicting evidence or
   no matching route holds the original thread/task. Resume removes model/effort
   override parameters. It never changes an old model to satisfy a new profile.
9. Accept once from the original incident: real worker/capability, original
   task/operation, actual selected route and native receipt, scoped memory, and
   a bounded diagnostic result. Complex code/permissions/signing work produces
   a Leader recommendation, not a Luna development attempt. No fake test task,
   model benchmark or inference keepalive is part of acceptance.

The role AGENTS requires amita-phone-maintenance-repair/1 suggestions using only
diagnose/recommend_signed_data_sync/reconcile_original_receipts enums. Source
stores results and executes no repair. The phone's independently authorized
local policy must validate and apply any safe enum; until connected, results
remain suggestions. Local phone cognition stays not_configured.

Rollback uses the existing supervisor and previous runner/version in a safe idle
window, and restores the original Source allowed-cap set only if that grant is
being revoked. Do not restore SQLite, rotate identity, replay unknown operations
or remove old native history. Candidate files can remain private for review.
