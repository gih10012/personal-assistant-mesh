# Chat interactive nodes

Source implementation: [session_nodes.py](../assistant_mesh/session_nodes.py),
[SDK registrations](../assistant_mesh/session_node_mcp.py), and
[HTTP factory](../assistant_mesh/mcp_http.py). This document records source and
isolated verification; it does not assert production deployment or Chat account
installation. Root owns production/API/OAuth/plugin wiring.

The existing eight task/conversation tools remain available when their existing
conversation service is installed. Five additional callable tools are registered
on the actual MCP SDK server: `mesh_connect`, `mesh_discover`,
`mesh_node_update`, `mesh_work_pull`, and `mesh_work_report`. `register_tools()`
uses the real SDK `.tool` factory (the installed MCP 2.3.0 server is `MCPServer`;
the registration interface also accepts FastMCP). An absent session authority
returns `session_nodes_not_configured`, rather than claiming membership.

## Membership and discovery

Connect with a retained `session_id`, `control_id`, and current `capabilities`
(string list). An optional agent observation is `ready`, `idle`, `waiting_model`,
or `unknown`; model observation is `available`, `unavailable`, or `unknown`.
`mesh_discover` works without a business task or a prior connect. Its optional
`session_id` reads that already registered membership. Trusted cluster identity
comes from the owner-installed authority and existing scoped ingress route;
there is no tool argument for a URL, owner, node, role or credential.

`mesh_node_update` publishes a complete snapshot with the same session ID and a
new retained control ID. Set `online=false` to disconnect explicitly. Membership
expires after the privately configured 15–60 second TTL; no keeper task or
background process refreshes it. During active interaction, connect again with
the same original ID to renew/restore the same node. A retried connect preserves
later capability observations. A retried older node update only observes the
current membership and does not overwrite newer state. Changing a control ID's
content is rejected. A lost heartbeat/join ACK can safely retry the same ID.

Capabilities are explicitly session-reported, not provider attestations. Only
the intersection with the private subject policy qualifies for managed work.
Advertising an ungranted native tool neither grants Mesh authority nor disables
that native Chat tool. Interactive nodes do not acquire a coordinator term.
Visible physical nodes use owner-configured display names and pseudonymous IDs.
Discovery separates network heartbeat/A2A reachability, coordinator lease,
agent/model observations, and task states. A non-running business task does not
mark a network Leader offline. Unsupported model/provider observations remain
unknown.

## Existing work and original receipts

Private installation can authorize an exact existing task, original request,
conversation and operation with `authorize_work(..., authorized=True)`. This
method is not an MCP/public action. By default, `tasks:submit` and capability
advertisements alone authorize no work. The optional private policy
`execute_own_requests=true` grants execution of suitable, genuinely unstarted
requests from that same ingress subject; it never adopts an existing operation.

For existing network work, pass its existing `task_id`; its original request must
already be recorded by ingress or in its task context/A2A message origin. This
adds an exact grant to the original Task context, without creating an ingress
request or a replacement task. Task capability requirements and device fences
still apply. Existing tasks requiring `leader` cannot be made Chat work by
advertising that capability. Native histories and sessions remain in their
original ledger and are not returned as Chat work.

Pull with `session_id` and a retained `pull_id`. The authority filters authorized
task IDs and uses `Store._claim_in_db` in the same transaction as its minimal
claim receipt. Repeating a pull returns its original task/epoch/operation, and
never claims another task. An empty pull remains empty under that ID; a later
poll uses a new ID. New pulls observe an existing live interactive turn before
claiming additional work. A work response exposes only the business input and
interactive checkpoint, with a current `lease_live`, bounded lease remainder,
and `may_execute` flag. Expired receipts do not authorize execution.

Report using the exact returned `task_id`, `epoch`, `operation_id`, `request_id`
and `conversation_id`, plus a retained `report_id`. Supported statuses are
`progress`, `effect_started`, `unknown`, `completed`, `failed`, `waiting_auth`,
and `waiting_backend`. The existing fenced Store update and report receipt
commit in one transaction. Lost ACK recovery reads that original receipt even
after session expiry; changed content, a foreign subject, or a stale lease
cannot issue another update. Results remain in the original Task and existing
final/conversation/events path; they are not independently verified or
owner-approved publications.

Before an external effect, report `effect_started`. If its result is uncertain,
report `unknown`, preserving the original operation and `needs_review` state.
Announced effects cannot complete without an `effect_receipt` containing the
same `operation_id` and its original `reference`. Such a receipt is still an
executor report, not native/provider execution attestation. Unknown work is not
replayed by reconnect or receipt recovery. Existing core native-session/Goal
occupancy guards remain enforced; this module does not clear them or switch
scopes to bypass an unsettled native execution. Independent scopes can continue.

## Root integration

On the existing authority, construct a `LocalIngressBackend` using its existing
`api.ingress` and `api.ingress_results`, then install:

```python
session_authority = LocalSessionNodes(local_backend, {
    "APPROVED_MESH_SUBJECT": {
        "capabilities": ["agent", "web.search"],
        "visible_nodes": {"PRIVATE_NODE_ID": "Computer"},
        "execute_own_requests": False,
    }
})
```

Root's existing `role=ingress` branch must route only the five fixed POST paths
`/v1/ingress/mesh/{connect,discover,node_update,work_pull,work_report}` to
`session_authority.request(peer['ingress'], action, payload)`. The transport must
retain its existing fixed source/subject binding. Do not fall through to worker
or operator routes. The compatible Store composition seams were supplied by
Root in `fa7e247`; this branch contains that dependency separately.

On the current remote sidecar, reuse its existing backend object:

```python
session_service = RemoteSessionNodes(existing_remote_ingress_backend)
server, app = create_server(existing_policy,
    token_verifier=existing_verifier,
    backend=existing_remote_ingress_backend,
    conversation_service=existing_conversation,
    event_service=existing_events,
    session_service=session_service)
```

The OAuth outer factory must forward the same `session_service`. Remote calls
use only the already installed per-subject ingress client and five fixed routes;
OAuth tokens are not forwarded. Every invocation checks the current OAuth grant
before execution and again before returning data. Discovery needs `tasks:read`;
the other four tools need `tasks:submit`, with the existing consent challenge.
There is no new OAuth/operator/worker scope.

Keep the existing event service and final-chain callbacks. Work MCP Events apply
only where the host actually supports them. Interactive membership does not
create an ordinary-Chat daemon, arbitrary wake API, or provider execution proof.

Suggested account-plugin instruction: when AMITA/network work is useful,
naturally connect/discover with a retained session/control identity, announce
the session's actual tools, use native tools for suitable work and A2A/capability
collaboration for other devices, pull only authorized work, and preserve original
IDs on every report. Root updates the existing account plugin/name; this branch
does not create another plugin or mutate an account.

## Verification

Click Evidence ran `tests.test_session_nodes.SessionNodeTests` (10 initial
business tests) against the actual Store and atomic seams, and
`SessionNodeSDKTests` (one actual loopback HTTP/SDK test). The latter discovered
all 13 real tools, checked OAuth scope/closed arguments, and completed an
original request/conversation/claim/report/status round trip. No model POST,
production service, or platform GUI was used. The additional lease-generation
boundary is verified separately without repeating those passing groups.

The existing isolated interpreter is
`/var/tmp/mesh-mcp-http-sdk.TQsZqe/venv/bin/python`; no global installation was
performed. Example targeted invocation, run with `click-gate` as the first token:

```text
click-gate verify '{"version":2,"workdir":"/ABSOLUTE/WORKTREE","checks":[{"evidence_id":"amita-session-lease-generation","argv":["/EXISTING/venv/bin/python","-m","unittest","-v","tests.test_session_nodes.SessionNodeTests.test_expired_lease_renews_original_operation_and_old_epoch_cannot_report"],"class":"targeted"}]}'
```
