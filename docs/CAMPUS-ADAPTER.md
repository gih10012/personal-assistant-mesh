# Selective campus network capability

`assistant_mesh.campus_adapter` is a minimal owner-installed HTTP/HTTPS callback
on a node that actually has the campus path. It is not a global proxy, SOCKS
service, SSH daemon modification, native-network allowlist or school login tool.
Owner authorizes a network/port/protocol range once; models choose URLs and
addresses within that range, not a fixed per-URL canary.

## Fixed private installation

Example **synthetic** configuration; replace with owner-reviewed actual campus
network and expiry. Do not publish actual private machine/account details:

```json
{
  "schema": 1,
  "provider": "node:campus-connected-node",
  "capability_id": "campus-selective-v1",
  "capability_epoch": 1,
  "action": "network.campus.http",
  "egress_node": "campus-connected-node",
  "artifact_root": "/private/owned-0700-directory/campus-artifacts",
  "allowed_networks": ["10.42.0.0/16", "fd42::/64"],
  "allowed_ports": [[80, 80], [443, 443], [8080, 8082]],
  "allowed_schemes": ["http", "https"],
  "expires_at": 1792252740,
  "max_bytes": 262144,
  "timeout_seconds": 20
}
```

CIDRs must be canonical and nonzero-prefix. RFC1918/ULA and owner-authorized
public campus networks are allowed; the public HTTPS adapter's `is_global` rule
is intentionally **not** reused. Loopback, unspecified, multicast and link-local
addresses are outside this campus surface. Arbitrary TCP, authentication and
account business endpoints are not implied by a network range. This contract
applies only to the shared callback, never the node's native Shell/network tools.

Save configuration as owned regular single-link `0600`; its parent must be
owner-controlled. The artifact root must already be owned `0700`. An owner
version/hash-pinned ProviderRuntime wrapper is:

```python
from assistant_mesh.campus_adapter import execute

def invoke(context):
    return execute(context,
                   '/private/owned-0700-directory/campus-owner.json',
                   'ACTUAL_SHA256_OF_EXACT_PRIVATE_CONFIG_BYTES')
```

Use the existing ProviderRuntime manifest with `provider`, `authority_id`, its
separate private `journal`, and one `adapters` entry: fixed module path/byte SHA,
`function="invoke"`, capability ID/epoch/action matching the config, versioned
handle and `new_spend_minor=0`. Configuration path/hash, executable client, trust
store, artifact root and identity come from owner installation, not model scope.
Register that runtime's actual `managed_adapter` descriptor and an owner capacity
pool on the campus node's authority. Do not replace/restart existing providers to
install this additive capability or erase their journals.

## Model-selected scope

Literal-IP URL (no resolver needed):

```json
{
  "operation": "get",
  "url": "http://10.42.1.5/public-entry",
  "query": {"page": "2"}
}
```

DNS/virtual-host URL with an explicit actual address chosen/discovered by the
agent using its existing native tools:

```json
{
  "operation": "get",
  "url": "https://portal.example.edu/public-entry",
  "address": "10.42.1.5",
  "query": {"search": "mesh"}
}
```

Workload for either scheme:

```json
{"kind": "campus_http", "max_bytes": 262144, "timeout_seconds": 15}
```

Only `get` and `probe` (HEAD) are supported. Scope accepts only operation, URL,
optional address and optional string-valued nonsecret query map. The URL itself
has no userinfo/query/fragment; query is sorted and encoded separately. Known
credential keys, private URL metadata, headers, cookies, methods, proxy/config
and executable overrides are rejected. No school session/cookie/auth file is
read. Free-form path/query values are not perfect DLP: **never place secrets in
them**. Public GET does not guarantee the remote application has no side effects.

Each address must be canonical and fall in the installed CIDR; port and actual
scheme must match owner ranges. Literal-IP URL/address cannot disagree. For a
DNS URL, address is required and is fixed with curl `--resolve`; there is no
second ambient DNS lookup or automatic multi-address retry. The URL hostname is
retained for Host/SNI/certificate verification. The adapter does not attest a
DNS name-to-address relationship supplied by the model; HTTPS still verifies the
certificate for that URL hostname. HTTP has no such authenticity proof.

The same owner config handles many targets/path selections without further
owner URL/hostname configuration. Discovery/routing still requires truthful
path observations and current capacity. Existing Core admission requires fresh
independent observations of the **exact scope/workload**; this adapter does not
relax or forge that constraint. Automated witness maintenance can remove manual
per-request probing without presenting old path evidence as new target content.

## Actual request and outcome

Each curl invocation disables curlrc and every inherited proxy explicitly:
`--disable --proxy '' --noproxy '*'`. It changes no environment/global network
setting. Only the selected HTTP/HTTPS protocol is enabled, no `--location`, retry,
netrc, cookies, auth headers or TLS bypass is used. The owned bounded capture
helper discards stderr, limits bytes/time, and reaps its own curl on completion,
overflow or timeout. Actual `remote_ip` must equal the scoped address; an error,
different IP, invalid status, TLS failure or excess bytes stays unknown.

No redirect is followed, even within an authorized CIDR: a 3xx reports only the
first response. HTTP/TLS reachability, HTTP success, content validity and account
authentication are separate facts. `http_status=401/403` can be a completed
transport observation, not usable authentication or successful inference.
HTTP evidence reports `tls_used=false`, `tls_verified=false` and no TLS verify
result. Successful HTTPS reports actual TLS verification, not school login or
target-content independent verification.

`execute` returns the existing callback shape:

```text
outcome=completed, resource_quiescent=true,
result_reference=mesh-campus-http-artifact:sha256:<exact-file-sha>,
evidence={HTTP/TLS facts, scheme, egress_node, destination_contract_sha256,
          body/header bytes+SHA, artifact_id+SHA, no-auth/no-inference facts}
```

Receipt/log metadata contains no raw URL/address/query, private configuration
path, body or response headers. The private `0600` artifact preserves original
operation/capability/provider/task/epoch/receipt/nonce, exact scope/workload SHA,
sampling time and base64 response. HEAD headers can contain sensitive server
data and therefore stay in this private artifact, never the public receipt.

```python
from assistant_mesh.campus_adapter import read_artifact
artifact = read_artifact(fixed_owner_path, fixed_owner_sha,
                         exact_artifact_id, exact_artifact_file_sha)
```

This local owner-authorized read independently checks file SHA and response
length/SHA, not remote content truth. It follows no URL, retries no network,
opens no tunnel and does not reconcile or settle an unknown journal.

## Original identity and remote use

ProviderRuntime supplies the actual admitted context and invokes once under its
original operation/receipt/nonce journal. An additional immutable `O_EXCL` intent
is written/fsynced before the HTTP request, keyed by provider + operation ID +
capability ID, **not** nonce. Direct duplicate calls, a new nonce, exception,
restart or byte-limit failure cannot replay that same original invocation.
Preserve intent/artifact roots and the ProviderRuntime journal across upgrades.
Failure after intent but before request is conservatively ambiguous; do not
delete it or create new IDs to work around an unresolved old operation.

Execution remains on the campus-connected node. A remote Leader can discover
its projected capability and use the existing A2A/delegate channel to give that
node a task; the campus agent uses its current task-bound thin call helper against
the local actual authority. Projection is not a foreign allocation grant. Remote
status/result/artifact retrieval uses existing task/result/native-file authority,
not a new general shell/SSH allowlist or a public artifact server.

## One bounded real validation for Root

This source slice made **no real request, SSH, school login or production change**.
After installing and registering an additive runtime, Root can:

1. Privately reuse the already reviewed unauthenticated **public campus entry**
   from `CAMPUS-PATH.md` evidence/ncut capability contract, verify its actual
   address/network and owner range. Do not publish that private URL/address in
   source or substitute a school private-business/authenticated endpoint.
2. Use one bounded new explicit task/operation and actual independent scope/
   workload observation; delegate to the campus node as needed. Do not replay the
   previous partial native-SSH probe or any old unknown under a different ID.
3. Let the task-bound thin call → existing ProviderRuntime perform the GET; Root
   only reads the original authority/journal/artifact. The former 64 KiB probe
   exceeded its bound; use an explicit reviewed larger limit or a smaller public
   response, never call a truncated body a complete verified artifact.
4. Read exact private artifact, independently compare bytes/SHA and original
   IDs/scope/workload hashes, verify provider settled and owner pool released.
   Keep HTTP no-TLS and authenticated-business/inference false where applicable.

Tests use synthetic replies, owner configs and the real in-process
API/ProviderRuntime/allocation pipeline. They cover CIDR/port/protocol selection,
multiple model targets under one contract, IPv4/IPv6, hostname/address pinning,
nonsecret query, proxy/redirect/auth bypass prevention, malformed/oversize/TLS/
remote-IP errors, immutable original intent, private artifact hashes, exact
admitted scope, settlement/capacity and unknown across provider restart. They
are not real campus reachability or arbitrary-platform acceptance. Node agents
can adapt the thin contract to their actual Windows/mobile environment; this
implementation reuses current Python/curl/POSIX-private-storage primitives, not
a requirement that every Mesh node run Linux or a permanently installed proxy.
