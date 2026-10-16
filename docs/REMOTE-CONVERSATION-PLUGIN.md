# Public Mesh plugin and connected conversation

Source delivery, not a live ChatGPT connection or owner acceptance. This follows
`owner-entrance-20261016-duplex-v1`: ChatGPT/the official Cloud client initiates
the public connection; messages can travel both ways within that workflow. The
owner's computer, Firefox, local stdio and browser credentials are not involved.
The existing VPS `/mcp`, HTTPS hosting and reviewed GitHub identity OAuth remain.
No `/v1`, publisher, raw Store result or native control endpoint becomes public.

## What the official client supports

Checked 2026-10-16 against the actual official pages:

- [MCP server tools and elicitation](https://developers.openai.com/plugins/build/mcp-server):
  tools can return model-readable data and elicit structured information while
  executing a tool. Elicitation is not proof of a human decision; agent clients
  may answer themselves. Do not use it for secrets or native approval.
- [MCP Events](https://developers.openai.com/plugins/build/mcp-events): the
  documented integration is **Work web, desktop Work with Cloud selected, and
  dots**, using MCP 2.0/`2026-07-28` and verified signed **webhooks**. That
  integration explicitly does not support polling/streaming. Availability and
  workspace controls still require actual account acceptance.
- [Connect and test](https://developers.openai.com/plugins/deploy/connect-chatgpt):
  use ChatGPT Plugins, public Streamable HTTP and the supported OAuth flow;
  refresh discovered metadata after server changes.

This slice implements explicit tool-result messages and bounded receive, **not
the MCP Events extension**, a standalone SSE inbox, background activation or
an assertion that ordinary Chat will autonomously run another turn. Native MCP
progress notifications are not durable messages and are not used as delivery
proof. No Apps UI, Extensions, new Skill or replacement OAuth engine is needed.
The model can show a received question naturally and call the reply tool after
the user/client answers. A future elicitation adapter must negotiate the actual
client capability and back-channel; this source does not silently assume it.

## Small authenticated surface

Default construction still exposes exactly the original three tools. Passing
an owner-installed `ConversationService` opts in to four additional tools on
the **same authenticated `/mcp`**:

| Tool | Purpose | Resource scopes |
| --- | --- | --- |
| `mesh_conversation_open` | Bind retained conversation ID to an existing original ingress request/task; no task submission | `tasks:read` + `tasks:submit` |
| `mesh_conversation_receive` | Read ordered messages after a retained cursor; optional wait 0–20 seconds | `tasks:read` |
| `mesh_conversation_reply` | Append a retained-ID client message, optionally replying to an original message | `tasks:read` + `tasks:submit` |
| `mesh_conversation_receipt` | Same original message lookup after an uncertain write ACK | `tasks:read` |

All parameters are closed-schema. Clients cannot supply source, subject, role,
owner, task/epoch grant, directory or native session. `OwnerTokenVerifier`
resolves the same actual issuer/resource/client/owner mapping; the existing
Ingress backend authorizes the original request and derives its task ID again.
The OAuth factory only passes the optional service to the existing MCP server.
No GitHub scope, token forwarding, authentication machinery or existing local
plugin changes. The private service directory is 0700, journal 0600; this is a
POSIX deployment adapter, not a claim that every device needs Python/SQLite.

One conversation binds one original request/task in this first contract. Use
separate retained conversation IDs for other tasks rather than reassigning an
existing one. The public opaque conversation ID is not a verified ChatGPT chat
ID; the authenticated principal plus original task provide the authority.

## Message semantics and restart

The private append-only journal retains exact text/hash, original message ID,
direction (`to_client` / `to_mesh`), reply-to, conversation/request/task binding,
reported original timestamp (UTC, or null when unavailable), and actual journal
receive/process times. `sent_at` stays null: journal publication is not proof
that ChatGPT displayed it, a human read it, or Leader processed the reply.
Here `message_id` is the retained owner/client operation identity, not a
fabricated verified ChatGPT platform message ID. If the host does not expose
its platform ID/time, those remain unavailable; client-reported values are not
treated as independent platform evidence.

Messages have monotonic per-conversation sequence numbers. Receive returns a
bounded page (at most20 and60000 encoded message bytes), cursor and `has_more`.
Only advance the client's durable cursor after consuming that entire page.
Reopening with the same private journal and original identity preserves history;
expired execution qualification does not delete prior read receipts. A lost
reply ACK requires only `mesh_conversation_receipt` with the original message
ID. Never make a new ID or auto-replay. Conflicting same-ID text/binding is
rejected; exact repeats return the original record without another effect.

Receive waits are local bounded read loops, not event subscriptions. Same-process
publishes notify immediately; cross-process private publishers are noticed within
250ms. There is no permanent timer, network retry, task replay or cursor ACK.
HTTP request cancellation can leave its read-only thread alive until the bounded
wait ends; configure normal authenticated connection/concurrency limits at the
owned sidecar. Token expiry is checked again before returning a waited result.
This is not instantaneous distributed policy/revocation synchronization.

## Leader/entrance helper and live questions

`ConversationService(directory, owner_id, backend, live_interaction_validator=...)`
uses the **same backend instance** supplied to the OAuth/MCP factory. See the
[private wiring fragment](../deploy/chat-conversation-owner.py.example).

Owner-installed code may call `service.publish(fixed_principal, payload)` to
publish selected message/progress/question/result text. This is a private
Python helper, not a discoverable tool or HTTP route. The principal is fixed
owner/source/subject policy, never a posted claim. Leader/entrance can consume
`direction=to_mesh` records with its own retained processing checkpoint, then
publish a response. Recording a reply alone is not proof of actual handoff;
Root must wire and verify that consumer before calling the assistant connected.

Never automatically copy native rollouts, credential files, raw task results or
all private history. A `kind=result` message is explicitly selected conversation
communication; it does **not** create the original `mesh_approved_result`
publication or authorize an artifact. Keep that original approval workflow.

`kind=question` requires the exact derived task ID, original interaction ID,
task epoch, params SHA-256 and an installed trusted **live** validator at publish
and at first reply. Missing validation fails closed for questions, not normal
messages. `store_interaction_validator(existing_store)` reuses the actual
Store live predicate, requiring unanswered/nonsecret `question`, exact epoch,
params hash and live task lease. It never constructs/recoveries a Store or
calls `resolve_interaction`. Replies bind the stored original interaction.

**Every response remains `native_interaction_effective=false`.** This source
does not apply an answer or approval to the old native interaction. Checking
live status and appending to this separate journal is not one atomic native
resolution transaction; Root's future bridge must use the original authority's
atomic validation/once receipt, not treat these records as an approval grant.
Elicitation or text claiming “approved” cannot bypass that boundary.

## Installable remote package

The original `plugins/personal-assistant-mesh` local plugin is untouched. The
new `plugins/personal-assistant-mesh-remote` is a portable Agent Plugins1.0
package: root `plugin.json` + `mcp.json`, actual public
`https://47.84.73.177/mcp`, Streamable HTTP, OAuth CIMD, no headers/secrets/local
commands. Existing auth policy still pins approved client IDs and redirects;
the package's CIMD declaration does not grant an unknown client permission.

Build outside its source directory:

```bash
python3 scripts/package_remote_plugin.py --output /PRIVATE/mesh-remote.zip
```

The packager uses an exact two-file allowlist, refuses overwrite and inspects
both manifests in the final archive. Root alone saves/installs it through the
actual Plugin Creator/account UI, completes existing OAuth and refreshes tools.
Package creation is not hosting, login, transport or owner acceptance. No public
directory submission is requested or performed.

## Minimal actual acceptance still owed

Root must install the optional service in the existing isolated VPS sidecar,
preserve its private journal/backup and fixed ingress binding, then install the
remote plugin in the already confirmed main account. No extra public routes
are required. Verify original3 plus new4 tools, one explicit task, original
conversation open, Leader-published progress/question, client reply, private
consumer receipt and same-cursor reconnect. Distinguish journal receipt, actual
Leader consumption and owner phone/Chat acceptance. The official Codex Cloud
client can use the same public contract only through its actually supported
connection; these loopback checks do not prove a real Cloud job has joined.

Tests use real private SQLite/Ingress and actual pinned MCP2.3 authenticated
HTTP/modern client, with synthetic verifier identity only. They cover both
directions, waiting publication, same-ID receipt/reconstruction, principal/scope
isolation, original task count, live question boundaries and exact packaging.
They do not use a real account, model, external request, production database,
browser or phone, and are not evidence that the whole Mesh goal is complete.
