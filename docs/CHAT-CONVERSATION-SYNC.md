# Explicit sync of an original Chat message

PAM-006d source leaf: agent `chat_explicit_sync_20261019`, branch
`work/chat-explicit-sync-20261019`, base `443ad99`, worktree
`personal-assistant-mesh-chat-explicit-sync-20261019`. Merge dependency: shared
`conversation_sync.py` commit `9e3d8f4` (identical cherry-pick `7664f74`).
Root owns review, deployment, the installed Chat connection and owner acceptance.

`mesh_conversation_reply` may durably record a message before Leader claims its
original task. Its runtime result is then `awaiting_original_live_task` with
`post_attempted=false`. Once the original task has a real live claim, an
authorized client can explicitly call the eighth conversation-enabled MCP tool:

```json
{
  "name": "mesh_conversation_sync",
  "arguments": {
    "conversation_id": "ORIGINAL_CONVERSATION",
    "request_id": "ORIGINAL_REQUEST",
    "message_id": "ORIGINAL_CLIENT_MESSAGE"
  }
}
```

The current `OwnerTokenVerifier` principal must have `tasks:read` and
`tasks:submit`. The same owner, fixed `chatgpt-chat` source, subject, existing
request/task/conversation and original `to_mesh` message must remain visible.
Caller-supplied text, owner, source, subject, task grants or other extra fields
are rejected before SDK argument coercion. Tool metadata advertises write scope
in both `securitySchemes` locations; read tokens receive the normal OAuth scope
challenge. Missing or non-bridge runtime returns
`conversation_sync_runtime_unavailable`, with `outcome=not_attempted`.

The shared helper uses the original bridge, message and durable native delivery
intent. It advances only that message; explicit no-claim results can be synced
later. A sync intent also preserves interruption before bridge entry. Once any
native delivery or unknown sync is admitted, missing ACK/receipt, restart or
repeat sync cannot cause another native POST. Existing native attempts only
query the original native receipt. Sync does not create a task, loop, keeper,
background activation or native permission. Ordinary text such as “approve”
remains steering, with `human_consent_verified=false`.

The original seven tools retain their behavior. Receive, status and receipt do
not drain client messages; repeating a reply still only reads its native
receipt. A lost write ACK still calls for the original receipt, preserving all
original IDs. `queued_for_native` remains distinct from actual submission ACK,
Leader consumption and owner acceptance.

Deploy the updated `mcp_http.py` and shared `conversation_sync.py` into the same
existing modern sidecar with its original backend/service/bridge; refresh the
installed tool metadata. No route, credential, source default or receiver change
is needed. This source commit does not perform deployment.

Local targeted validation: four actual SQLite/Ingress/claim/steering checks
cover pre-claim deferral, read non-drain, same-ID sync, ACK loss/reconstruction,
scope/identity/closed arguments, message visibility and missing runtime. The
separate `tests.test_mcp_conversation_sync.SyncMCPTests` checks actual pinned
MCP2.3 ASGI metadata, write challenge, strict fields and the same claim flow.
That SDK environment is absent locally; its execution remains a Root check,
not a passed or skipped-as-passed claim. No real account/model/production state
or external request is used by these checks.
