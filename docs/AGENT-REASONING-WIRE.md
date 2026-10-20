# Explicit native reasoning selection

This is a separately authorized control improvement while the long-running
development Goal and teaching-evaluation task remain paused. It does not resume
either, replay old work, replace a Leader, or change native permissions.

`assistant_mesh.reasoning` is a small pure contract shared by process controls:

- `read_model_catalog(rpc)` reads the current initialized app-server's paginated
  `model/list`, including hidden entries. It does not start model inference.
- `validate_selection(catalog, model, reasoning_effort)` rejects an unavailable
  model or unsupported exact effort. The live account/client catalog is the
  authority; no fixed model/effort whitelist or silent downgrade is used.
- `prepare_turn(rpc, params, model, reasoning_effort)` writes both the native
  `turn/start.effort` and `collaborationMode.settings.reasoning_effort` because
  collaboration settings take precedence. None leaves the native default alone.
- `observed_turn(records, thread_id, turn_id)` extracts matching native
  `turn_context` metadata from an original rollout selected/authenticated by the
  caller. Model-written claims are not evidence of the actual setting.

The modern adapter accepts `reasoning_effort` in its existing owner configuration.
Its native lifecycle checkpoint records the requested setting/catalog hash and
original thread/turn receipt. `accepted` means the original RPC acknowledged a
turn; `effective_verified=false` stays false until native metadata is separately
checked. Transport failure records unknown and never retries.

## Existing frozen Worker

Do not replace the original 3198 Codex/Worker to obtain this feature. Add only
this module to a frozen source candidate and select:

```python
ProfiledCodex = make_codex(OriginalCodex, profile_loader, receipt_callback)
```

`profile_loader(agent)` is bound by the trusted launcher to that managed agent's
owner-private profile. It returns None or a snapshot containing `revision`,
optional `model`, and explicit `reasoning_effort`. For example, a separate
manager can maintain `{"format":"mesh-agent-profiles/v1","agents":{...}}` and
return only the identity-bound entry. The helper does not discover files or
choose an agent by model-provided identity. It calls `receipt_callback(agent,
receipt)` at prepared/accepted/unknown boundaries so the control journal can
persist exact revisions/operation IDs without exposing prompts or credentials.

Updates affect the **next explicit native `turn/start`**, never the active turn.
The same original thread, input, history, permissions, developer instructions,
goal and old unknown remain unchanged. No self-restart is needed to apply a
profile update after the overlay is installed. Autonomous native-goal turns
that do not pass through client `turn/start` are not claimed to adopt a change.
A custom-provider overlay cannot select a different model and bypass its
existing model-scoped cost contract.

Stopping a managed process is not deleting its native thread/history. An unknown
turn must retain its original identity even when a profile/process changes.

## Evidence scope

Five focused tests cover exact catalog validation, the real modern turn wire,
the exact original 3198 start method under a narrow subclass, next-turn revision
updates, one-send unknown behavior, and native-metadata identity matching. They
do not prove real-model adoption or live deployment; Root owns controlled real
acceptance and production installation separately.

Official source actually read for this change:
[Codex App Server: model list and per-turn configuration](https://learn.chatgpt.com/docs/app-server).
The already-generated local native schema also confirms `effort` and
`Settings.reasoning_effort` are strings rather than a fixed enum. Current
model/effort availability remains specific to the real account and client.
