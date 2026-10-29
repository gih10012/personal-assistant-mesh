# Authenticated outbound HTTP transport

This is the S4 transport core: a deployable Workers Free SQLite Durable Object
and a member-initiated WebSocket client. A member exposes only exact permitted
HTTP routes on one private loopback origin. CF owns transport attempt receipts,
not a business ledger, coordinator election, writer lease or execution right.
Direct entrances remain available. Neither connectivity nor a transport receipt
proves HA, completed execution, or a safe authority switch.

The inspected existing gateway registers the eight task/conversation tools and
five session-node tools at a stateless JSON `/mcp`. Their bytes, ordinary
Authorization, MCP headers, original application status and WWW-Authenticate
challenge pass unchanged. Host is a Root-pinned original resource host, never
client input. Issuer/audience checks stay in the original application and are
not loosened or rewritten. The only configurable public application routes are
GET/POST/OPTIONS `/mcp` and the two exact OAuth protected-resource metadata GETs.
`/v1/*`, management routes, `/token`, `/authorize`, registration, consent,
callback and revocation are refused. Responses are bounded finite HTTP messages,
not an indefinite SSE tunnel; the current gateway already uses JSON responses.

**This release is not ordinary Chat migration.** Requests additionally require
an independent `X-Amita-Relay-Access` admission token and retained
`X-Amita-Relay-Request-Id`. Existing application OAuth Authorization is separate
and still required by that application. Ordinary Chat does not automatically
supply these new headers. Full issuer/client/consent/code/token/refresh/revoke
state migration, public OAuth integration and plugin URL reconnection remain
separate work. No personal account credentials are copied into this transport.

## Root deployment contract

Root alone selects the actual Free account, performs deployment and supplies
private keys. This worker has not read Wrangler credentials or operated an
account. Current official docs support Free **SQLite** DO migrations through
`new_sqlite_classes`; use that template, not legacy-KV `new_classes`:

- [Free SQLite DO availability and limits](https://developers.cloudflare.com/durable-objects/platform/pricing/)
- [SQLite namespace migrations](https://developers.cloudflare.com/durable-objects/reference/durable-objects-migrations/)
- [Hibernating WebSockets](https://developers.cloudflare.com/durable-objects/best-practices/websockets/)

These primary docs were read through the owner's local proxy. No paid upgrade,
billing permission, KV service, model or external arbiter is required by this
code. Root must keep the deployed account on Free and confirm actual limits.

1. Copy this directory to a private versioned release, preserving `worker.mjs`
   and `protocol.mjs` together. Copy `wrangler.toml.example` to `wrangler.toml`.
   Set the actual worker name and exact `PUBLIC_ORIGIN`, for example the origin
   assigned under the Root-confirmed `gihggiy.workers.dev` account subdomain.
   A custom domain can be used later without changing the transport protocol.
2. Generate **distinct** 32+ character URL-safe random client/member tokens.
   Keep the raw client admission token private on each authorized transport
   caller; raw member tokens stay only on their member hosts. Configure only
   lowercase SHA256 token hashes in private `policy.json`, based on
   `policy.json.example`. IDs use 8–128 ASCII letters/digits/underscore/hyphen.
   Every route has one explicit member. There is no automatic backup selection.
3. Install the private policy as the Worker secret `RELAY_POLICY`; the public
   Wrangler template intentionally contains neither hashes nor account ID:

   ```sh
   wrangler secret put RELAY_POLICY --config /PRIVATE/RELAY-RELEASE/wrangler.toml < /PRIVATE/policy.json
   wrangler deploy --config /PRIVATE/RELAY-RELEASE/wrangler.toml
   ```

   Use Root's existing private Wrangler launcher/environment and authenticated
   session. If first-deploy secret installation needs an existing script, Root
   can deploy with the secret unset (all requests fail closed), put the secret,
   and deploy the final configuration. Do not bypass authorization or enable a
   paid fallback. The `.example` policy is intentionally invalid until replaced.
4. Node >=22.13 is used for the host and SQLite leaf fixtures. `package-lock.json`
   pins the two host libraries and transitives. Root installs with `npm ci
   --ignore-scripts --no-audit --no-fund`, using per-command proxy settings on
   the laptop; no install script or account key is needed. Worker code has no
   npm runtime imports and does not bundle the host dependencies.
5. Create owned mode-0600 `host.json` and typed token JSON
   `{"format":"amita-reverse-relay/1","member_id":"ACTUAL_MEMBER_ID","token":"RAW_MEMBER_TOKEN"}`.
   Use `host.json.example` with the actual loopback MCP port, original resource
   Host header, private mode-0700 attempt directory and matching exact routes.
   On the laptop only, optional `"proxy_url":"http://127.0.0.1:7890"` uses the
   local proxy for CF HTTPS/WSS; upstream loopback remains direct. Omit it on
   the VPS or other hosts without that proxy. Do not point at a remote generic
   API or add wildcard/upstream routes. `relay-host.service.example` is optional
   Linux supervisor wiring over the portable host client; no deployment occurs
   merely by building/importing it.

   ```sh
   node /PRIVATE/RELAY-RELEASE/host.mjs /PRIVATE/relay-host.json
   ```

The member gets an authenticated 20-second one-use ticket by HTTPS, then opens
WSS with that ticket in a subprotocol header, never a credential URL. A new
connection replaces only that same member's old transport connection; its old
inflight requests become unknown and never move to the new connection. Member
token rotation disables the old attachment on next application traffic. No
member token can admit a client or authenticate another member.

## One controlled actual acceptance

Root can request authenticated `GET /_mesh/relay/bootstrap` and confirm the
configured member reports connected. Then send one original retained request ID
through a real allowed route using both relay admission and the existing
application's valid Authorization (or explicitly check its original 401
challenge). Retain the request ID before submitting; never generate a new ID to
retry an unknown mutation. The data is sent to the host over its outbound WSS,
not through a generic reverse proxy to a fixed VPS address. Root can select a
qualified authorized member explicitly after a safe maintenance handoff; this
transport does not make that handoff safe by itself.

Authenticated `GET /_mesh/relay/receipt/ORIGINAL_REQUEST_ID` exposes only the
transport state/status/response hash, with `execution_verified=false`. Raw
Authorization, request body and response body are not persisted on CF or the
host. The original response is delivered once in memory. A duplicate original
ID returns 409 with its recorded state; changed bytes/headers under that ID are
rejected. Reconcile the original business receipt using its original business
IDs. No automatic reforward, new ID, another member, or business action follows
a timeout, disconnect, lost response, restart or cold DO reconstruction.

## Bounded lifecycle and idle cost

DO uses `acceptWebSocket`, serialized connection attachments and native
`mesh-ping`/`mesh-pong` auto-response. Idle connections can hibernate; no periodic
DO alarm/poll or durable heartbeat write is installed. Host sends a sparse
45-second keepalive and reconnects transport with bounded backoff. Inflight HTTP
waits actively for at most 25 seconds, with a corresponding absolute host HTTP
deadline, and retains at most 4 concurrent operations. Request/response bodies
are capped at 256/512 KiB and headers at 16 KiB.

Defaults are 2000 newly admitted HTTP requests/day, 1000 member tickets/day and
100000 immutable attempt receipts. Policy permits at most 10000/day and one
million receipt records. Capacity exhaustion fails closed before a new delivery,
while direct entrances remain unchanged. Receipts are not automatically erased:
forgetting an unknown ID could replay its action. Plan an explicit safe receipt
archive/protocol generation before reaching capacity; do not delete DO state to
make an unknown ID runnable again. Host attempt files similarly remain private
durable evidence. Free exhaustion does not enable paid usage.

## Focused checks for Root

No checks or tests were run by the implementation worker. Dependency locking
used `npm install --package-lock-only --ignore-scripts --no-audit --no-fund` with
the specified proxy; this did not execute the application or contact CF.

Root Click short ID `s4relay`, exact argv `node --test test/relay.test.mjs`, workdir
`/home/gihggiy/Github/personal-assistant-mesh-worktrees/cloudflare-reverse-relay-20261029/edge/relay`.
Run after `npm ci --ignore-scripts --no-audit --no-fund` with Root's command-local
proxy. Four leaf cases use real local SQLite and loopback HTTP: auth/ticket
ownership, strict routing, unchanged MCP/OAuth bytes, durable duplicate/timeout
handling and cold reconstruction, capacity rejection, and exclusive host
admission across overlapping deliveries. They do not contact CF or use actual
accounts, business ledgers, history, GUI or model calls. Actual WSS hibernation
and deployment compatibility remain Root's one real controlled acceptance.
