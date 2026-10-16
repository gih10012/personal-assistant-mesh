# Ordinary Chat: dedicated GitHub identity OAuth bridge

Source integration only: **not a created GitHub App, live login, deployed AS or
verified Chat connection**. The existing MCP SDK 2.3 server, hosting, three
tools, OwnerTokenVerifier, original request IDs, RemoteIngressBackend and fixed
`role=ingress` / `source=chatgpt-chat` / owner subject remain in place. This does
not move the server to Sites or create a placeholder plugin.

## Choice and minimum identity

Pinned `fastmcp-slim[server]==4.1.0` supplies mature OAuthProxy routes, DCR/CIMD,
S256 PKCE, browser-bound consent/CSRF, callback/code/token/refresh handling,
cryptographic JWT issuance/verification and persistent state adapters. This
project only adds a narrow owner/public-identity verifier, Mesh scope policy
translation, existing resource mounting and local serialization around the
component's token endpoint. It does not implement a new code/refresh server.

The built-in GitHubProvider is **not** used unmodified: its default requests
`user`, and its token verifier queries `/user/repos` and infers `user` when the
scope header is empty. Those behaviors exceed this owner's identity-only goal.
Our upstream authorization always explicitly sends `scope=` empty; verification
only performs `GET https://api.github.com/user`, requires an empty `X-OAuth-Scopes`
header and the exact owner-confirmed numeric user ID, and retains only that ID.
No repo, `public_repo`, `user`, `read:user`, `user:email`, profile/email lookup,
`offline_access`, or GitHub write permission is requested. A missing scope
header/broader token fails closed. Display names/login/email are not identity.
GitHub identity does not prove a ChatGPT email/account: the main Chat account
remains bound by the owner's existing confirmed profile/UI handoff separately.

Mesh `tasks:read` and `tasks:submit` are **our** resource permissions, not GitHub
permissions. The component's scope-translation hooks bind them to the original
SDK-validated request/refresh, approved client and owner scope subset. They are
never forwarded to GitHub. An upstream public identity or opaque GitHub bearer
alone grants no Mesh access. Resource requests need the component's real signed,
unexpired Mesh JWT, expected issuer/audience/client, live upstream identity,
original JTI mapping and existing OwnerTokenVerifier policy.
Both authorize and token exchange require the exact Mesh `resource`, including
refresh. A missing, duplicate or foreign token-request resource fails before
the component consumes the original code; no non-MCP missing-resource fallback.

The existing tools remain exactly `mesh_submit_task`, `mesh_task_status`, and
`mesh_approved_result`; no operator, global history, raw result, Shell or node
membership API is added. Actual Ingress still owns task/source/subject binding
and transactional same-request admission; approved results still need their
separate original owner publication. Native tools/network are not intercepted.

## Exact URLs and owner configuration

For approved public origin `https://<owner-host>`:

- GitHub OAuth App callback: **`https://<owner-host>/auth/callback`**.
- Mesh resource: `https://<owner-host>/mcp`.
- Canonical issuer: **`https://<owner-host>/`**, including the final slash.
  The pinned component normalizes origin issuers this way. Policy, AS metadata,
  callback `iss` and JWT `iss` must match exactly; no relaxed issuer check.

Use the account's dedicated free GitHub OAuth App, not an existing gh token,
PAT or unrelated repository App. App registration has no scope selector; the
actual authorize request's empty scope is what matters. Creation, client-secret
generation, live authorization and deployment remain separate owner operations.
Never put the App secret or actual private numeric identity into this repository.

Copy/adapt the private
[JSON example](../deploy/chat-github-oauth-owner.json.example) and
[factory example](../deploy/chat-github-oauth-owner.py.example). The example is
intentionally unusable until the owner replaces every placeholder. Its
`ingress_clients` must reference an **already separately authenticated** ingress
credential with the fixed source/subject, not an operator credential. A config
path alone does not prove the server-side role; inspect actual authority binding.

Provide three **distinct**, owner-owned 0600 files in safe private 0700 parents:
the new GitHub client secret; a high-entropy persistent JWT signing key of at
least32 bytes; and a valid independent Fernet encryption key. Pre-create the
private 0700 state directory. Factory construction checks these boundaries but
does not generate production credentials or recover a Store. Keep keys/state
backed up privately; encryption does not replace filesystem permissions.

`FileTreeStore` + `FernetEncryptionWrapper` persist registrations, consent,
transactions, code/JTI mappings and encrypted upstream tokens. Do not use
ephemeral/default/keyring-derived state or derive the Mesh signing key from the
GitHub App secret. Key replacement is a reviewed rotation, not a way to replay
an old Mesh request. Reconstruction with the same keys/state was tested; a
cross-node failover, old-backup rollback or distributed AS is not proved.

### Client registration is not permission

Use only the exact Chat client ID and redirect URI shown by its connection
management page. The component advertises CIMD, RFC9207 issuer identification
and S256; the current official client may use the stable CIMD/redirect or an
account/callback-specific value. Never guess an ID based on an account label.
All client IDs are pinned in the existing policy. The proxy rejects unknown
CIMD IDs **before fetching their URLs**. DCR can register an ID, but that random
ID receives no Mesh access until the owner independently approves it in the
private policy. No wildcard or auto-promotion from registration is permitted.
After a policy change restart/review the owned sidecar; cached policy is not an
instant distributed revocation service.

### Refresh/lifetime limitation

The adapter does **not** invent an upstream refresh grant. GitHub's common
no-scope response can omit `expires_in` and `refresh_token`; the mature component
then returns a900-second Mesh access token and **no** refresh token. Reauthorization
is needed after expiry. A synthetic real-component test covers that exact
shape. When the provider actually returns refresh, the established engine
handles it and scope narrowing, state, rotation and JWT signing were tested.
Do not claim seamless renewal from a test IdP that always returns refresh.
No extra `offline_access` permission or longer lifetime was silently enabled.

## Deployment gate: not just adding a proxy route

Install [pinned optional dependencies](../deploy/chat-github-oauth-requirements.txt.example)
in the modern isolated sidecar, never the legacy core or host Python. A trusted
owner may load the private factory via uvicorn with **one worker, one active
instance**, loopback binding, access logs disabled and only the actual loopback
proxy trusted. There is no source-import listener or automatic service install.

Before deployment, enable only the actual component auth paths/methods and
bounded bodies at TLS: AS metadata `GET/OPTIONS`, `/authorize` `GET/POST`,
`/consent` `GET/POST`, `/auth/callback` `GET`, `/register` and `/token`
`POST/OPTIONS` (and only actual supported revocation routes if explicitly
configured). Keep exact Host, HTTPS, payload/content limits, no upstream
retries and no access/query/body logs. Default other routes404; never expose the
Mesh `/v1` management API.

**The current TLS template globally rejects query strings and cookies.** That
would block authorize/callback queries and the mature consent/binding cookies.
It is not enough to append auth proxy locations: Root must scope the required
query/cookie/form allowance to **only those exact AS paths** while preserving
the existing `/mcp`, `/cloud/node`, metadata and management-API restrictions.
Do not globally disable these safeguards. Current production TLS routes do not
yet provide this AS flow; no claim of Chat OAuth connection is made here.

AS endpoint logging is reduced to fixed private-detail-free categories for
known component loggers; reverse proxy/uvicorn access logs must also be off.
OAuth query code, cookie, client secret, token, profile or raw upstream response
must not enter public logs, plugin packages or model context.

### Single-process token exchange boundary

A real risk test initially reproduced **two200 responses** for two concurrent
exchanges of the same authorization code: the component's FileTree `get/delete`
is not atomic. The adapter now serializes the **unchanged full `/token`
handler**, including SDK code/PKCE/refresh checks, in this one process; the same
test then permits only one success. This is not new token logic or a distributed
lock. Do not run multiple workers/active instances over these files. A future
distributed deployment needs an independently reviewed atomic backend/strategy;
do not claim Redis alone magically makes a `get/delete` sequence atomic.
This limitation does not weaken the separate actual Ingress task transaction.

## Evidence and next real acceptance

The isolated tests use real pinned components, real PKCE/CSRF/cookies, actual
signed JWTs, encrypted disk state and a fresh real Store/Ingress. Only the
upstream IdP HTTP transport/data are explicitly synthetic; no forged valid
signature is accepted. Tests cover issuer/audience/client/signature/expiry,
refresh-as-access, original GitHub bearer rejection, same numeric owner,
scope separation/narrowing, DCR denial, cross-browser consent refusal,
restart-style reconstruction, same-code concurrency and original request once.
They do not prove the live GitHub login, Chat-host behavior, real TLS or HA.

```sh
<isolated-python> -m unittest tests.test_chat_github_oauth tests.test_mcp_http tests.test_mcp_entrypoint
```

When the SDK is absent, its explicit skips are not OAuth acceptance. Root's next
real sequence is: verify dedicated App/callback and private config → independently
review fixed identity/ingress/client bindings → deploy exact AS/TLS routes →
exercise one real owner authorization from the correct Chat account → list the
original three tools → submit one authorized new task and read its original
status/approved result without changing request ID. Do not use a mutating tool
merely to check login, or resend an unknown task after reauthorization.

Sources: [OpenAI authentication contract](https://developers.openai.com/plugins/build/auth),
[FastMCP OAuthProxy](https://gofastmcp.com/servers/auth/oauth-proxy),
[GitHub no-scope definition](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps),
[pinned component package](https://pypi.org/project/fastmcp-slim/4.1.0/).
