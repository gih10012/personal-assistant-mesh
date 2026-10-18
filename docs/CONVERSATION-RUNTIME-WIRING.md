# Actual Mesh wiring for the existing remote conversation

Leaf base `e593479`, branch `work/conversation-runtime-bridge-20261017`.
This is a deployable **source candidate**, not a deployed Chat connection,
successful model turn, owner acceptance or proof the entire Mesh works.
It continues [the original conversation](REMOTE-CONVERSATION-PLUGIN.md),
the same HTTPS host, OAuth, plugin identity, request IDs and private journal.
No browser, new model, credentials/account, public route or background wake.

## Smallest Root integration

1. In the new frozen receiver source, add **only**
   `assistant_mesh/conversation_authority.py` alongside the already approved
   entrance modules. Keep original `store.py`, `worker.py`, `server.py`,
   `codex.py`, `sessions.py` and all history bytes unchanged. This module is
   Python3.6 syntax-compatible and uses the receiver's already-open Store.
   Update the candidate's additive-source manifest/archive digest privately;
   do not copy it into the live tree or restart outside Root's controlled window.
2. Preserve the original server configuration, owner socket and sole carrier.
   Configure the already authorized original ingress owner/source/subject peer
   exactly as planned for the MCP backend; **not an operator credential**.
   In the existing `serve_entrance(..., ready=...)` owner ready hook call
   `conversation_authority.attach(api)`. The optional equivalent CLI is:

   ```text
   python3 -m assistant_mesh.conversation_authority \
     --config ORIGINAL_PRIVATE_SERVER_JSON --socket ORIGINAL_OWNER_SOCKET
   ```

   It still calls `serve_entrance`; it does not start a second receiver/API.
   Default old entrypoints are unaffected. `api.ingress` must exist; absence
   fails closed. The hook adds **one internal POST**
   `/v1/ingress/conversation`, with `observe`, `deliver` and `receipt` actions.
   Authentication remains the existing server principal, restricted to
   `role=ingress`, fixed `source=chatgpt-chat`, original owner and subject scopes.
   Query/fragment/other methods are denied. The new route is NOT added to TLS.
   Its only schema addition is `conversation_deliveries`, an operation receipt
   table in the original authority transaction, not a new task/identity registry.
3. In the existing modern OAuth/MCP isolated image, add
   `conversation_runtime.py`, updated `conversation.py` and `mcp_http.py`;
   `conversation_authority.py` is also imported for the fixed route constants.
   `chat_github_oauth.py` and OAuth engine/config remain unchanged from the
   original reviewed integration. Existing dependency pins still apply.
4. Copy the exact [private bridge JSON shape](../deploy/conversation-runtime-owner.json.example)
   outside the repository, owner0600, safe0700 parents; pre-create the same
   conversation directory0700. Its owner/subject must equal the existing OAuth
   policy and server-side ingress peer. `ingress_client` points to the existing
   private **scoped ingress** client JSON/token, never a bearer in this document.
   Keep its control URL loopback/private; never expose the whole management API.
5. Use [the private same-ASGI factory fragment](../deploy/conversation-runtime-factory.py.example):
   `backend, service, bridge = load_context(private_json)` and pass the SAME
   backend/service into the existing `chat_github_oauth.create_app`.
   `load_context` installs owner-code callbacks, not model-supplied role claims.
   Root's original one-worker app, issuer/client/subject policy, keys and OAuth
   callback routes remain. Refresh the installed plugin's tool metadata because
   runtime-enabled receive/reply annotations accurately reflect publication /
   actual native forwarding. No plugin package or connection URL change.

## No extra daemon is required for connected calls

- `mesh_conversation_receive` first publishes actual original-task status,
  live nonsecret questions, and any existing owner-approved result. It does
  **not consume pending replies**, even when its token has write scope. The
  ordinary bounded journal wait then runs; a closed Chat is not awakened.
- A **newly recorded** `mesh_conversation_reply` forwards only that original
  message once. A duplicate original journal message only reads its native
  receipt, even when no native delivery was previously admitted. A read-only
  token cannot invoke this write hook or drain another message.
- `mesh_conversation_receipt` queries the original native receipt only; it
  cannot perform even a first delivery. Lost ACKs never get new IDs or POSTs.
- The journal's trusted live-question callback receives its immutable bound
  request ID, so an ASGI process started before a separate CLI creates a new
  conversation does not rely on a stale startup map or recursively open the
  journal while its append transaction is held.

An optional private owner/Leader invocation remains available:

```text
python3 -m assistant_mesh.conversation_runtime --config PRIVATE_BRIDGE_JSON \
  --conversation-id ORIGINAL_CONVERSATION --request-id ORIGINAL_REQUEST
```

This is **one bounded step**, not an installed service/timer. It can process
previously deferred messages when their original task actually has a live
claim. It does not create/requeue a task, change native history, upgrade a
client's scope, or activate Chat/Cloud. If a new reply arrives before any live
claim, its journal message remains queued and the hook says
`awaiting_original_live_task`; ordinary receive does not pretend it was sent.
An owner/Leader private step can handle it later. No silent fallback posts it
to a new task or pretends a dead/paused task has consumed it.

## Native delivery levels are intentionally different

The authority derives the task from the original Ingress request, checks its
live task/Leader lease and exact epoch, then records the operation receipt in
the **same transaction** as the native answer/steering write:

- Ordinary text enters the original `steering` queue contract. Initially
  `queued_for_native` is **not** Leader processing. Receipt polling can observe
  the real original Worker's `submitted` ACK, but that still is not proof the
  model understood/completed the request (`native_consumption_verified=false`).
- A nonsecret native `question` must match task/epoch/interaction/complete
  canonical params SHA. The existing Store answer validator is used; the exact
  original `interactions.answer` is then recorded atomically. This can report
  `native_answer_recorded` / `native_interaction_effective=true`: actual native
  question state changed, not merely a journal entry. It does not prove a human
  supplied the answer, and says `human_consent_verified=false`.
- `approval`, secret questions, closed/replaced interactions and stale epochs
  cannot become consent. They remain private-owner-required or rejected.
  A plain message saying “approve” is ordinary text, not an approval resolver.
- Results are read only through original `IngressResults` approved-publication
  validation. No raw task result, checkpoint, rollout or credential is copied.
  Large/unavailable publications fail closed; use the original approved-result
  tool, never a truncated or raw-result substitute.

The private bridge persists an attempted intent **before** the first native
POST. Restart/new model/unknown ACK only queries the same authority receipt.
No-record is an observation, not permission to repost. Replies to published
running-epoch progress retain that epoch; a reclaim cannot retarget them.
Original task/operation unknown guards are never cleared or replayed.

## Checks and remaining real gates

The targeted checks use actual SQLite/Ingress/native lease predicates,
question validation, original Worker steering poll/ACK contract, one actual
authenticated internal HTTP server, and the real MCP2.3 ASGI stack. Cases cover
one-message hooks, read-token non-escalation, same-ID replay, ACK loss/rebuild,
closed question/approval, old epoch and approved-result-only publication.
No OpenAI model, account, iLink, desktop, production database or external HTTP
is used. The legacy authority module is parsed under Python3.6 grammar; that is
not a claim of actual host/phone execution or successful new-model inference.

Root still owns the single production switch, private factory/config, OAuth
connection, actual installed Chat plugin and owner validation. A real client
must receive a native question, reply, and have the original running Leader
actually observe its answer or steering. Record journal receipt, native queue /
answer commit, native submission ACK and owner acceptance separately. Official
Codex Cloud must connect from its real environment; these tests do not prove a
Cloud job or closed-client background action.
