# Private receiver -> existing Work sidecar event feed

Follow-up to the default-off `4385402` Events leaf. Producer dependency is
`a444ce4` / main `a526f8f`: the reviewed final/emit receipt tables and original
ConversationAuthority/Bridge. This does not modify `conversation_final.py`,
Store, Worker, OAuth engine, public Nginx or the eight MCP tools.

## Receiver: one optional PURE module

`assistant_mesh/conversation_event_feed.py` is Python3.6 syntax and imports only
the receiver's existing pure authority/ingress/store modules. In the EXISTING
ready hook, AFTER original conversation + emit + final attachment:

```python
from assistant_mesh.conversation_event_feed import attach as attach_event_feed
attach_event_feed(api, finals, enabled=True, policies=private_event_projects)
```

Default `enabled=False` does nothing. The API/finals/bridge must share the exact
already-open Store/ingress. There is no new Store, migration, recovery or table.
Root's frozen candidate needs this ONE additional pure module, not modern MCP,
Events/HMAC/network modules or a modern replacement server/Worker.

`private_event_projects` is a bounded list of owner-installed entries:

```text
{source: chatgpt-chat, subject: EXISTING-MESH-SUBJECT, projects: [EXPLICIT-GRANTED-ID]}
```

It is a real grant map, not inferred from a project label or a task description.
Unknown source/subject/project, wildcard grants and duplicate bindings fail.
The exact private POST route is `/v1/ingress/conversation/events`; only existing
server-authenticated ingress peers with `tasks:read` can use it. Operator/worker
bearers cannot. Do NOT expose this path in public Nginx or add an MCP publisher.

Payload is exactly `after,limit,projects`: cursor is `None` or
`{final: nonnegative-int, question: nonnegative-int}`, limit1–20, projects is the
current authorized subset of that peer's installed grant. Caller-supplied
owner/source/subject/task/role fields are rejected. A requested project is only
a filter and MUST already be in the receiver's private grant.

Final rows JOIN the original ingress binding, filter source/subject/project IN
SQL BEFORE LIMIT, then invoke the producer's original `_record` verifier. It
never exposes the trusted owner-global `completion_events` method. Question
rows are SQL-scoped to source/subject FIRST and scanned at most100 per call;
original binding/body-project, unanswered question kind, current real task and
Leader lease/epoch, and secret flags are checked before returning a reference.
No JSON1 assumption is introduced on the legacy host. Only eligible references
consume output limit. Cursor advancement does not skip unprocessed candidates.

Live question `message_id` is EXACTLY the existing Bridge's
`mesh-question-<digest(original interaction/task/epoch/params-hash)>`; attention
event ID is deterministic for that original target and binding. The feed returns
only the standard event refs, never question text, tool params, approvals, native
history or raw final text. The original eight-tool receive can then project the
actual public question under the same ID. This is not a claim the client has
already received/displayed it. An event may become stale before delivery: the
client reads the original task/message and still follows its original live fence.

## Sidecar: current account + projects + actual Client + owned lifespan

Use `mcp_event_sources.ProjectEventAccess(policy, private_event_projects,
account_active=...)`. The synchronous callback MUST consult actual current
account/subject revocation/grant authority and return STRICT `True`; absent,
unknown, exceptions, coroutine objects and truthy1 do not grant access. Do not
replace it with `lambda: True`, a live PID or historical OAuth installation.
The `policy` is the existing validated MCP OAuth owner policy. Trusted
`access.replace(...)` reloads private project grants; it is not a remote tool.

```python
from assistant_mesh.mcp_events import MCPEvents
from assistant_mesh.mcp_event_sources import (
    ProjectEventAccess, RemoteReceiptSource, attach_lifecycle)

access = ProjectEventAccess(policy, private_event_projects,
    account_active=current_account_subject_grant_live)
events = MCPEvents(private_event_directory, policy['owner_binding'], authorize=access)
source = RemoteReceiptSource(existing_subject_ingress_client, fixed_principal, access)
# Existing owner factory: same backend, conversation service, OAuth identity/App.
provider, server, app = create_app(existing_document, backend=existing_backend,
    conversation_service=existing_conversations, event_service=events)
attach_lifecycle(app, events, source, enabled=True)
```

`existing_subject_ingress_client` is the EXISTING Client constructed from the
same private ingress config as normal Chat backend/bridge, NOT an operator or
OAuth bearer. Receiver authentication, not a path/class name, proves its role.
`fixed_principal` reuses the exact owner/source/subject/scopes mapping; no model
supplies it. The adapter checks live access before requesting only this fixed
route, requests the current project subset, and verifies every returned audience,
project and cursor before enqueueing. The same access object gates delivery.

`attach_lifecycle(..., enabled=False)` is default-off. Explicit enable preserves
the existing ASGI OAuth/SDK startup context and its state; it starts the owned
EventDeliveryLoop and closes/joins it in finally. A mismatched source/access or
changed fixed feed principal rejects startup. The private cursor namespace is
bound to that owner/source/subject; an old in-process integer cursor is NOT
silently converted into a remote feed cursor.

All receipt IDs are durable before cursor advance; loss repeats only the same
refs. New-feed errors retain the original cursor and no longer prevent delivery
of ALREADY durable events. Delivery still rechecks account/project permission;
revocation cancels pending notifications. It never redoes completed business.

## Evidence and remaining Root work

Two NEW targeted checks pass locally on existing Python3.14.7 (no install):

```sh
/EXISTING/ISOLATED/venv/bin/python -m unittest -v tests.test_mcp_event_private_feed
```

They use exact public `a444ce4` producer classes to seed consistent final rows,
24 foreign-subject rows before the visible page, real Store lease/interaction
and existing Bridge receive, actual Client on an authenticated LOOPBACK HTTP
fixture, access revocation, feed outage with durable delivery, and original ASGI
lifespan preservation/close. This is NOT another completion/native/model test or
production/account acceptance. No old6+2 suite or full suite was rerun. The pure
facade additionally parses with Python3.6 grammar; actual host3.6 import and
production3.12 integration remain Root checks, not claimed passed here.

Root must install matching private project grants and REAL current revocation
evidence, include the pure facade in the existing frozen receiver ready hook,
wire the actual per-subject private Client and lifespan in the SAME sidecar,
then rescan/test Work subscription, genuine live question/final, actual platform
continuation and cancel. No new public route, account, key or model is needed.
