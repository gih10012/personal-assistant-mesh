# Optional Work MCP Events leaf

Base: `153d51b`; branch `work/work-mcp-events-20261020`. No production,
account, GUI, new key/dependency or model calls. This leaf changes only the
modern sidecar; it must NOT enter the Python3.6 receiver's frozen dependency
closure. Root owns deployment and plugin rescan/real Work validation.

The current [official MCP Events page](https://developers.openai.com/plugins/build/mcp-events)
was independently searched and fetched in full. This implements its webhook
profile on the existing authenticated `/mcp`, protocol `2026-07-28`, with
`events/list`, `events/subscribe`, `events/unsubscribe` and the TOP-LEVEL
`server/discover` capability `events: {}`. It does not inject arbitrary ordinary
Chat messages. Work web / desktop with Cloud selected are distinct from ordinary
Chat's own native scheduled follow-up. No polling/streaming/replay protocol is
advertised: subscriptions and events return `cursor: null`.

## Source / interfaces

- `mcp_events.MCPEvents`: separate owner-private `work-events.sqlite`, fixed
  Chat source, durable subscriptions, challenge cache, original receipt refs,
  event outbox. Existing Mesh Store/Worker/43 tasks are not constructed/recovered.
- `mcp_event_transport`: Standard Webhooks HMAC-SHA256, exact serialized bytes,
  fresh signing timestamp, `whsec_` 24–64-byte keys; no new dependency. Signed
  fresh single-use callback challenges must echo within 30 seconds. Verification
  cache is five minutes per principal/URL/key. Replacing a key re-verifies, then
  signs with old+new keys for five minutes.
- `mcp_event_extension`: pinned SDK2.3 public `Extension` + `MethodBinding`,
  strict root params and 2026-only methods. Existing middleware's public result
  seam adds the draft capability after the SDK's core model serialization.
- `mcp_http.create_server(..., event_service=None)` and
  `chat_github_oauth.create_app(..., event_service=None)`: default OFF. Existing
  eight tools/arguments, OAuth identities and approval/verification remain.
  Instructions now distinguish natural task-agent replies (`human_reviewed=false`)
  from separately owner-approved `mesh_approved_result` publications.

`MCPEvents(directory, owner_binding, authorize=...)` requires an existing owned
0700 directory. It creates only its own 0600 SQLite file. `authorize(principal,
name, arguments)` must return STRICT `True` from real current account and
owner-installed project authorization; `name=None, arguments={}` is catalog
discovery. No callback means fail closed, not a permissive default. Root must
bind the established OAuth connection/revocation authority and a private project
grant map; a supplied `project_id`, model assertion, task PID or label is not a
grant. The stored principal is rechecked on each delivery. No access token is
copied into the event ledger or forwarded to a webhook. Signing secrets and
callback URLs remain private. Account/transport failures expose fixed categories,
not credential bodies or raw exceptions.

Both event names accept required `project_id` and optional `request_id` filters.
Delivery additionally matches original owner/source/subject, so a project match
cannot cross an ingress identity. Lifetime defaults to one hour, minimum one
minute, maximum one day; `ttlMs:null` is granted a finite lifetime. Repeated
creation/refresh uses the same deterministic principal/URL/name/canonical-args
identity. Expiration/revocation stops delivery. Unsubscribe is idempotent for
the same original identity and cancels pending attempts; it waits for an already
inflight send before acknowledging. It cannot retract an already accepted event.

## Final leaf weak coupling / owner lifecycle

The sole publisher is trusted owner code, not an MCP tool/public HTTP route:

```python
events.record_task_event(event_id=stable_original_id, name='task.completed',
    owner_id=owner, source=original_source, subject=original_subject,
    project_id=original_project, request_id=original_request,
    task_id=original_task, task_epoch=original_epoch,
    occurred_at=original_receipt_time, message_id=original_message)
```

`leader.attention_required` uses the same reference-only shape. Source values
must come from the final leaf's actual durable receipts, never model JSON.
Missing project is durably retained as unroutable `missing_authorized_project`,
not guessed; it creates no delivery and does not block later feed receipts. Events
contain only project/request/task/epoch/message refs, no whole native histories,
stdout, new authorization instructions or claimed independent execution proof.

The final producer commits its own business/final transaction FIRST. It never
calls this ledger or the network inside that transaction. Root can supply its
trusted `ConversationFinals.completion_events(after=int, limit=int)` through:

```python
from assistant_mesh.mcp_events import EventDeliveryLoop, completion_receipt_source
loop = EventDeliveryLoop(events, receipt_source=completion_receipt_source(finals))
# Start in the existing app's owner lifespan, after its own startup:
loop.start()
# In that lifespan's finally (offload join from async code):
loop.close()
```

Cross-process authority access remains an explicit private Root adapter; do not
mount the publisher/feed as a new public OAuth/model-selected route. The adapter
maps `{'events','cursor'}` to `{'receipts','next_cursor'}`. Its private cursor moves
only after every original ref is durably recorded; lost ACK uses the same ID.
The loop resumes durable pending deliveries after restart, stops on close, and
does not start automatically just by importing/registering an event service.
Root must wire BOTH service registration and this owned lifecycle before claiming
asynchronous delivery. Otherwise discovery alone is not an active dispatcher.

Each callback connection re-resolves DNS, rejects ANY private/local/non-public
answer (including translated/tunnel addresses), dials only that numeric sockaddr,
and preserves the hostname for TLS/SNI/Host. No environment proxies or redirects.
HTTPS443 only; ten-second I/O deadline interrupts the owned socket even on slow
reads, response bounded to64KiB.
System DNS resolver latency is OS-managed; an unconfirmed close is reported,
never silently called stopped. Root should retain its normal bounded resolver
configuration. Verification uses the identical transport. Bodies are <=256KiB.

Outbox retries only transient failures, at most five attempts with bounded
exponential delay. It preserves event ID, body bytes and occurrence time; only
signature/signing timestamp change. `410/413` never retry; `410` deactivates the
subscription. Cancellations serialize against sends. `2xx` records webhook
receipt ONLY (`chatgpt_resumed=false`), not a model continuation, task success,
human acceptance or permission to repeat business. Past events are not replayed
to new subscriptions. The source cursor is not a protocol replay cursor.

## Minimal evidence / Root remaining

Existing local environment: Python3.14.7, MCP2.3.0, no install. Six local
SQLite/transport checks and two REAL SDK ASGI checks (eight total, zero skips)
cover stable identity/restart/rotation, fixed independently calculated HMAC
vector, DNS-at-connect/TLS hostname/redirect boundary, filters/revocation/TTL,
bounded retry/cancel/lifecycle, source cursor, Extension registration, top-level
discover, OAuth401, strict fields, callback errors and original eight tools.
The SDK/Pydantic emits a benign TypedDict `_meta` extra-config warning; the
top-level forged owner-field rejection is actually checked.

```sh
/EXISTING/ISOLATED/venv/bin/python -m unittest -v tests.test_mcp_events tests.test_mcp_event_extension
```

Root remaining: isolated Python3.12 check; supply real current account/project
revocation callback, private final-feed adapter and lifecycle; load into SAME
OAuth sidecar; rescan SAME plugin/App; validate real Work subscribe/challenge,
matching task completion/attention, actual platform continuation and cancel.
No existing ordinary Chat, completed diagnostic or unknown task is re-submitted.
