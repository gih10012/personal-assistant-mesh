# Same-ASGI Work Events factory: read-only deployment preparation

The following is the historical `395c94a` preparation/checkpoint. Root has now
provided the actual pinned pure source and factory shape; the implemented
[current-grant wiring](WORK-MCP-OAUTH-GRANTS-2026-10-20.md) supersedes its missing
checker gate. Deployment and actual-account acceptance remain Root operations.

Base `906be49`; independent `work/work-mcp-factory-prep-20261020` branch in the
original Events worktree. No production read/write, GUI, account, callback,
model, dependency installation or old8+2 test rerun. The supplied fragment is a
TEMPLATE, not a claim that actual authorization or deployment is completed.

The [official Events authorization requirement](https://developers.openai.com/plugins/build/mcp-events#manage-subscriptions)
was searched/fetched again: subscription access must be rechecked and delivery
must stop after revocation. This does NOT define the private FastMCP grant
storage or prove a current account's authorization from installed software.

## Minimal change once Root has the real factory/control-flow copy

Adapt only the assembly section using
[`deploy/work-events-factory-fragment.py.example`](../deploy/work-events-factory-fragment.py.example).
Keep the same image, mounts/PYTHONPATH, private factory entrypoint, host port,
OAuth config path, signing/encryption keys, GitHub App/client IDs, original
backend/conversation journal/bridge/ingress Client and single instance.
Do not change receiver schema/config until the original Claw review/apply gate
has finished. This preparation does not open or modify that authority.

The fragment takes the original already-loaded OAuth and runtime documents.
Absent Events config or exactly `{format: mesh-work-events-owner/1, enabled:
false}` preserves no-Events construction. Explicit enable requires exactly
`format,enabled,directory,project_policies` plus a real synchronous
`account_active(principal)` callable. Missing/async checker rejects BEFORE
constructing an Events ledger. It does NOT synthesize a checker or a project.

It calls original `load_context` once, checks original ingress-client/owner/
subject binding, builds Events using the same access object, then calls original
OAuth `create_app` once. Its private source reuses `bridge.client` and
`bridge.principal`. `attach_lifecycle` wraps original startup/shutdown and
contexts; it does not replace/mount a second app. The original provider/server/
app remain available to Root. Registration and actual owned lifecycle are BOTH
needed; no new public route or tool is installed by this fragment.

Root must precreate/confirm the Events directory privately, supply a finite
explicit project grant list for the exact existing source/subject, and retain
the original private ingress role/scopes. A task's requested `project_id` is not
that grant. The receiver's private project map must match; no operator token or
new Model/API key is needed. The checker is evaluated through the shared
ProjectEventAccess on discovery, subscription, private-feed access and delivery.

## What actually needs source verification before enabling

Repository `chat_github_oauth.py` already enforces the exact issuer/resource,
approved owner subject/client and scope. Its resource verifier calls BOTH the
component's JWT validation AND `provider.load_access_token(raw_token)`. The
provider is pinned `fastmcp-slim==4.1.0`, with encrypted FileTree persistence and
`fastmcp_access_token_expiry_seconds=900`. Public identity is verified through
the existing no-scope upstream flow. Those facts are NOT a current grant query.

The presently known local MCP2.3 environment does not contain the separate
FastMCP/key-value packages. Do not guess engine store/collection/key names or
read encrypted production state. The current-grant implementation must be
chosen after reading the actual pinned source, in particular:

- How load_access_token checks a signed token's JTI/original upstream grant,
  fixed owner/client/scopes, expiry and revocation, including whether it refreshes
  or writes state. Code/refresh/JTI consumption must remain the original engine.
- What revoke_token/disconnect invalidates, and whether that invalidation is
  visible using non-secret stable grant identifiers after process restart.
- Which actual grant/index store supports a read-only account/subject lookup,
  and whether a live upstream identity validation is also required. A token
  stored somewhere, consent record or registered client alone is insufficient.
- How an async grant lookup runs on the existing app loop from the Events
  worker thread. A boolean cache must have an explicit bounded freshness and
  revocation policy, never turn missing/timeout into authorization.

RAM-only remembered bearers are not an acceptable durable-subscription solution:
they disappear on restart and the current Mesh JWT expires after900s. A raw
GitHub bearer is not a Mesh grant. No new long-lived bearer, silently extended
JWT lifetime, wildcard client/scope or replacement OAuth engine is proposed.
Until the real grant reader is reviewed, use Events disabled; do not fill the
template with lambda True/False and claim account integration complete.

## Smallest read-only materials requested from Root

No credentials, authorization headers, token bodies, state/DB or private chats:

1. Current private factory's redacted create_app assembly ONLY: field names,
   loading order and references to load_context/create_app/lifespan. Need to
   know whether original runtime object and provider are retained for wiring.
2. Actual pinned FastMCP4.1.0 PURE SOURCE for OAuthProxy load_access_token,
   revoke_token and the relevant token/grant models/store definitions; the
   storage wrapper's read/list interface if needed. An existing local full
   dependency environment path is sufficient instead. Do not export auth state.
3. Runtime config keys and whether it is exactly one existing subject/client;
   no actual identity/path values are needed. Project grants remain Root's
   explicit authorized private choice, not read from task descriptions.

The template is syntax-inspected only and never invoked. Root's already-passed
isolated Python3.12 SDK2 and pure frozen-builder1 are retained as evidence; no
new test run here claims actual account wiring. After source/factory review,
Root can perform its own single controlled candidate validation and deployment,
then real Work subscription/continuation/cancel acceptance. HTTP2xx remains
webhook receipt, not model continuation or owner acceptance.
