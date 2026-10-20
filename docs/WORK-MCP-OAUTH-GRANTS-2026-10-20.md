# Existing OAuth current-grant checker and same-app factory

Continuation of `395c94a`, not another OAuth server. Root supplied pure source
from the actual FastMCP-slim4.1.0 / KV image and its private factory's assembly
shape. No production state/token/config bytes, external request, new key,
dependency install, model call or old8+2 check ran in this leaf.

## Source-derived authority, not token existence

The pinned proxy's load_access_token follows signed JWT -> JTI mapping ->
encrypted upstream token -> actual token validator, with the original transparent
refresh path. Code/refresh exchange writes upstream, access/refresh JTI mappings
and refresh metadata. Refresh metadata is keyed by SHA256 of the wrapper token;
it contains client/scopes/expiry but NO upstream ID. A refresh-token revocation
deletes that hash; the upstream row and JTI can remain. Thus neither finding an
upstream row nor matching client/time among unrelated metadata proves a grant.

`mcp_event_grants.CurrentOAuthGrants` persists a separate owned0600
`oauth-grant-refs.sqlite` under the configured owned0700 Events directory.
It stores only fixed owner/source/subject/client, opaque upstream/JTI refs,
wrapper-token HASHES, scope association and explicit revocation markers/history.
No short access/refresh JWT, GitHub bearer, upstream refresh token or new key is
stored there or in the Events ledger. The original encrypted OAuth store is
accessed through its existing instance adapters, never copied or reinterpreted.

The component is installed on the SAME original provider/server/app after
build_app and before ASGI startup. It wraps successful authorization-code /
refresh exchange to record their verified associations, and wraps explicit
provider.revoke_token to mark Events revocation. Each ORIGINAL method is still
called ONCE; returned tokens, issuer/audience/scopes, PKCE/consent machinery and
900s access expiry are unchanged. Metadata failure after a successful exchange
does not replay the consumed code/refresh or replace its successful response.
Unknown Events metadata never supplies a positive authorization decision.

For the already-connected no-refresh GitHub case, SDK middleware observes a
FRESH authenticated MCP request and validates through the existing provider;
it can register the real opaque upstream grant without re-login or retaining
the bearer. An existing refresh-type grant with no recorded hash/JTI association
is NOT guessed: the next normal successful refresh/code hook establishes it.
An incomplete old refresh grant remains unavailable for Events meanwhile.

Every check verifies current approved owner/issuer-subject/client/read scopes,
upstream ID/client/scope, and (when present) the CURRENT refresh metadata HASH
and JTI->upstream association/expiry. A deleted refresh metadata row wins even
while upstream/JTI rows still exist and a positive identity cache is warm.
The real existing `_token_validator` verifies identity; its original provider
refresh lock/translation/transparent-refresh function is reused if necessary.
Positive upstream identity is cached for at most30s keyed to the actual current
upstream credential hash; association and local-revocation checks are not cached.
Failure/unknown returns false. No expired JWT is accepted by the resource API.

The long-lived grant is distinct from the short resource JWT: after900s the
original API still requires a valid OAuth access token, but an established Work
subscription need not lose its grant merely because that access JTI expired.
No-refresh grants still require the live upstream credential/recorded expiry
and real identity; refresh grants require their current association. Subscription
TTL/refresh and the original no-refresh OAuth reauthorization limitation remain;
this does not promise invisible perpetual client reauthorization.

## Revocation paths and precise limits

- `events/unsubscribe`, `410`, subscription TTL: existing Events lifecycle stops
  the corresponding delivery; already accepted/inflight data cannot be retracted.
- Upstream GitHub revocation: real validator detects it, with at most30s positive
  identity-cache freshness. Missing grant/refresh associations deny immediately.
- Existing `provider.revoke_token`, IF actually invoked: token hash or verified
  fixed-owner claims identify the subject, then a persistent Events-only block
  is written before the original method. No new public revoke/manage route.
- Owner-private `revoke_subject`: persists that same block and marks all known
  Events grant refs for the fixed subject revoked. Old still-valid JWTs and
  ordinary refresh cannot undo it. Only a successful NEW original OAuth code
  authorization established AFTER the block may register a new grant; old
  revoked refs stay revoked. Active subscription still needs normal refresh /
  re-subscribe with its original identity after an actual reauthorization.

Actual source enables RFC7009 routes only with a configured upstream revocation
endpoint. The current GitHub factory does NOT configure one. Do not claim a
ChatGPT Disconnect necessarily emits a visible RFC7009 callback. Unsubscribe,
upstream GitHub revocation and explicit owner revocation are the real observable
paths. UI disappearance alone is neither grant evidence nor a revoke signal.

If writing the local revocation marker fails, the original revoke is STILL
called once, but Events fails closed in process and reports
`events_local_revoke_metadata_unconfirmed` rather than false success. Do not
restart to erase that unknown; first reconcile the SAME subject using the local
revoke and its durable marker. No completed business is repeated.

## Root executable wiring (all identities/paths stay private)

The updated `deploy/work-events-factory-fragment.py.example` is now a complete
`create_app` reference for the verified current factory shape. It keeps existing
`MESH_CHAT_OAUTH_CONFIG` and `MESH_CONVERSATION_CONFIG`, strict private JSON,
required exact ingress_clients mapping, original owner/single-subject binding,
and ONE load_context/backend/service/bridge/Client. Existing build_app is called
once. No App/GitHub registration, token settings, keys, routes or contexts change.

Optional `MESH_WORK_EVENTS_CONFIG` points to a NEW owner-private JSON document:

```json
{
  "format": "mesh-work-events-owner/1",
  "enabled": true,
  "directory": "/PRIVATE/owned-0700-event-state",
  "project_policies": [
    {"source": "chatgpt-chat", "subject": "EXISTING-MESH-SUBJECT", "projects": ["EXPLICIT-AUTHORIZED-PROJECT"]}
  ]
}
```

Root precreates that private directory; no key is generated. Policies must match
the private receiver feed and actual original subject; task labels are NOT
grants. With env unset or enabled=false, the new grant ledger/hooks/lifecycle are
not constructed. Toggle false may retain the other known config fields for
rollback; unknown fields still reject. Preserve the SAME image/mounts/factory
entrypoint/port, existing OAuth config/state and original journals.

Enabled order is: original load_context -> shared ProjectEventAccess using
grants.account_active -> Events + existing bridge.client/principal source ->
original build_app -> grants.install (original OAuth/SDK lifespan) -> existing
attach_lifecycle outer wrapper. The synchronous callback bridges to the original
ASGI loop with a bounded12s wait; it never blocks that loop or creates a new auth
loop. It is used from the existing Events worker / SDK to_thread paths. Exit
closes the Events worker before detaching that original loop/context.

Owner-local explicit revoke (no public HTTP/tool endpoint, no bearer in args):

```sh
/EXISTING/ISOLATED/venv/bin/python -m assistant_mesh.mcp_event_grants \
  --oauth-config /PRIVATE/EXISTING-owner.json \
  --event-directory /PRIVATE/owned-0700-event-state \
  --subject EXISTING-MESH-SUBJECT --revoke-subject
```

It touches only Events grant-reference metadata and reports a count/fixed
outcome, not identities/tokens. It does not revoke GitHub globally, reset a task,
requeue business, alter native state or clear old grant/audit history.

## New verification only

Two focused SQLite/source-shaped-provider checks passed in the existing3.14
environment: short JWT expiry + restart, fresh identity failure, durable owner
block, no raw bearer bytes, unguessable refresh association, rotation/hash
revocation, wrong client, original exchange/revoke call counts and metadata
failure. These fixtures do NOT prove the actual FastMCP integration.

Root's isolated actual3.12 image can run the ONE additional real pinned-engine
fixture (existing synthetic upstream HTTP, no real account/business request):

```sh
python -m unittest -v tests.test_mcp_event_grants.PinnedOAuthGrantCheck
```

It covers real code/refresh/store/JWT association, rotation/revoke, then the
actual no-refresh response and loss of the short access JTI without losing the
current established grant. This was prepared but NOT run in the minimal local
MCP-only env (FastMCP absent). It does not rerun the old8+2 suite. Factory syntax
was parsed, not executed against private config. Deployment/real Work subscribe,
actual account/revoke observations and client continuation remain Root-only;
HTTP2xx still means receipt, not model execution or owner acceptance.
