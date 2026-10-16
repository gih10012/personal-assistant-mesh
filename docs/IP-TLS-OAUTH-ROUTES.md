# Optional exact OAuth authorization-server proxy routes

Source-only leaf `work/ip-tls-oauth-routes-20261016`, base `3c366fb`.
No deployment, nginx reload, Docker daemon operation, certificate/CA account,
GitHub login, credentials or production state were performed. Root remains the
single operations owner. This extends the existing IP-TLS renderer; it does not
implement another OAuth server, replace the MCP SDK or grant Mesh capabilities.

## Explicit opt-in and unchanged default

The existing four owner-config fields are unchanged. An optional `oauth_port`
may be absent or null; both keep **all nine rendered assets byte-identical** to
the old renderer, including configured MCP/Cloud behavior. The original example
therefore stays unchanged. To opt in, the owner adds e.g. `"oauth_port": 18685`
only when `"mcp_port": 18685` already selects the reviewed combined ASGI app.
It must be the **same port**, never another OAuth/FileTree process or a generic
management service. The independent Cloud port and its task-token gate remain.

The contract was directly aligned with OAuth author commit `52e8e41` and actual
FastMCP4.1/MCP2.3 routes/cookie source, not guessed `/oauth/*` endpoints. Root
must install and verify that owned combined app before enabling this proxy.
Selecting a port or rendering these assets does not prove a live/authenticated
backend, owner consent, Chat installation or public OAuth completion.

| Exact AS path | Methods | Query/body boundary |
| --- | --- | --- |
| `/.well-known/oauth-authorization-server` | GET, HEAD, OPTIONS | No query/body |
| `/authorize` | GET, HEAD, POST | GET/HEAD known query keys; POST form, no query |
| `/auth/callback` | GET, HEAD | Known code/state or error query keys; no body |
| `/consent` | GET, HEAD, POST | GET/HEAD only txn_id; POST form, no query |
| `/token` | POST, OPTIONS | POST form, no query; empty OPTIONS |
| `/register` | POST, OPTIONS | POST JSON, no query; empty OPTIONS |

No revocation endpoint is configured, so `/revoke` is not exposed. HTTP80 still
serves only its original ACME challenge route. `/v1`, operator/carrier/panel
paths, arbitrary well-known endpoints, path prefixes and normalized/encoded
path variants remain denied. Original `/mcp`, `/cloud/node` and protected-resource
metadata routes still reject all query/cookie variants and retain their guards.

Authorize keys are response_type, client_id, redirect_uri, scope, state,
code_challenge, code_challenge_method and resource. Callback keys are code,
state, error, error_description and error_uri. Values remain URL-encoded and
are passed without decoding, reordering or rewriting. Unknown/repeated/raw-
encoded key variants and malformed percent escapes are denied; total query
length is bounded to8KiB. The AS remains responsible for required fields, exact
resource/redirect/owner/client/PKCE/one-use state semantics, not nginx regexes.

POST bodies are bounded to64KiB, with the exact form or JSON MIME type (optional
UTF-8 charset). GET/HEAD/OPTIONS bodies must be empty. No POST query is allowed.
Nginx does not parse form/JSON fields: the existing SDK/adapter validates token
resource uniqueness, PKCE, assertions, DCR metadata/owner approval and consent
CSRF/browser binding. Passing a request is not authentication. No client/owner
ID is filled in, no GitHub/Mesh secret is substituted, and no upstream retry is
allowed. Existing nginx header/request-line limits continue to apply.

## Cookies, headers and responses

Only the three front-channel paths can carry these exact production cookies:
`__Host-MCP_CONSENT_BINDING` and `__Host-MCP_CONSENT_STATE_[0-9a-f]{32}`. The
logical total Cookie bound is16KiB, subject also to nginx's existing header
buffer limits. Quoted/unquoted signed standard-base64 values, including `+`,
`/`, `=` and their dot separator, are preserved. Multiple request Cookie headers
may be combined by stock nginx into the RFC semicolon form; signed values and
cookie pairs are not modified. Metadata/token/registration and non-AS routes
still reject cookies. Unrelated panel cookies, legacy flat consent state and
remember-approved/denied-client cookies are not forwarded; this factory does
not enable remember mode. Use an appropriate browser session rather than
silently deleting the owner's unrelated cookies to make linking work.

Front-channel **multiple Set-Cookie response headers** pass separately and
unchanged, including clearing cookies and Domain/Path/SameSite/Secure/HttpOnly
attributes. Non-front-channel AS paths suppress unexpected Set-Cookie. Required
Origin/Referer/Sec-Fetch and CORS-preflight request headers pass to the mature
component; no wildcard/credential CORS is invented. Host is the fixed reviewed
public IP, X-Forwarded-Proto is a single `https` and peer forwarding identity is
rebuilt. Caller Authorization is cleared on AS routes, not used as a shared
internal Mesh/GitHub credential. MCP's existing bearer handling is unchanged.

All AS responses use no-store/no proxy cache. Upstream 302/303 Location and
multiple Set-Cookie are not rewritten/followed. Access/query/body logs stay off,
and X-Accel-Redirect is not interpreted as an internal navigation. See official
[map matching](https://nginx.org/en/docs/http/ngx_http_map_module.html) and
[proxy URI/header/redirect behavior](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).

## Verification, not production evidence

- 17 pure renderer/argv/boundary tests passed. Two prior complete-asset SHA
  goldens cover disabled default and configured legacy sidecars; explicit null
  gives exactly the same bytes. Tests exercise known/duplicate/unknown query
  keys, encoded paths, MIME/body maps, actual quoted-base64 cookie grammar,
  same-sidecar configuration and original routes/default rejects.
- For actual syntax/packet semantics, nginx1.30.6 was privately built from the
  [official source download](https://nginx.org/en/download.html). Its tarball
  SHA256 is `3cf46f5ed7504d460125866b7bea99fb357f31d887e2e63b35503310e9b8b6c8`;
  detached signature verified with the public key linked on nginx's official
  HTTPS key page, fingerprint `D6786CE303D9A9022998DC6CC8464D549AF75C0A`.
  No global keyring/tool installation, Docker daemon or external CA was used.
- The real nginx ran in a private network namespace with a synthetic loopback
  backend. Both listeners retained kernel-bound sockets inherited by nginx,
  not sampled/released ports. Only TLS/listener/filesystem fixture lines were
  overlaid; original maps, locations, guards and proxy behavior were unchanged.
  No certificate/key was created. **This tests routing over local HTTP, not the
  production TLS certificate, pinned container image or real OAuth identity.**
- 12 allowed classes each reached the backend exactly once;23 rejected classes
  reached it zero times. Actual requests verified unchanged encoded query/form/
  quoted cookie, multiple Set-Cookie, Location, fixed Host/XFP/real peer, stripped
  caller Authorization, no-store, body/MIME caps, and no HTTP80 OAuth path.
  The private probe's first run completed those assertions but failed its own
  cleanup by calling `.close()` on HTTPServer; that original failure is retained.
  Correcting only the probe to the server context manager gave clean exit0 and
  stopped its owned server/thread/process. Renderer was not loosened to pass.

The resulting test binary SHA256 is
`e41bb8d2ed24dfcd8183cc7118dcbfd6dd0b093de07259c0fb15709160385721`;
final wire-result log SHA256 is
`fa32b0b5b41c48b40df17d186e2edbdc3915d625feb9a0dfd989d42935400c72`.
Generated toolchain/logs/configs remain private, not in this public repository.
Root must still test the reviewed config in the exact pinned production image,
preserve/back up the existing gateway state, and separately verify real OAuth
consent, Mesh token/resource handling and the original MCP task round trip.
