# Generic HTTPS capability adapter

PAM-004d/PAM-002a now has an owner-installed callback in
[`assistant_mesh.egress_adapter`](../assistant_mesh/egress_adapter.py), usable by
the **existing** `ProviderRuntime`/`ManagedProvider` execution chain. A model
selects an ordinary public HTTPS target in the admitted allocation scope; the
owner does not rewrite the callback for every target. Native Shell, network,
SSH, files and MCP are untouched.

This adapter performs unauthenticated HTTPS GET/query or HEAD probe, with a
bounded private artifact that can be read back and independently hashed. It is
not an authenticated model API, a browser session, a general Shell runner or a
host-wide network allowlist. HTTP 401/403 proves neither login nor inference.

## Once-per-install owner configuration

The provider's private, hash/version-pinned wrapper fixes the owner JSON path
and **exact file SHA-256**. The model cannot supply either, change mode, select
SSH/credential paths or choose an output directory. All owner JSON files are
regular 0600 files under owner-controlled 0700 parents; the artifact directory
must already exist with mode 0700. Do not put them in this public repository.

For a provider process actually running on the VPS, use direct egress:

```json
{
  "schema": 1,
  "provider": "node:cloud",
  "capability_id": "owner-generic-public-https",
  "capability_epoch": 1,
  "action": "network.https",
  "transport_mode": "direct",
  "egress_node": "ali-vps",
  "artifact_root": "/private/provider/artifacts",
  "allowed_origins": ["*"],
  "expires_at": 1893456000,
  "max_bytes": 1048576,
  "timeout_seconds": 20
}
```

Replace the expiry with the actual owner contract lifetime. `allowed_origins`
can be `['*']` for an owner-approved open public HTTPS capability, or an exact
list such as `['https://docs.example.org']`, without suffix matching. Only
standard HTTPS port 443 is supported in this slice. Literal private/local IPs,
localhost and `.local` targets are rejected; wildcard hostname authorization
does **not** independently prove DNS cannot resolve to a private address. The
origin scope is an owner contract, not a universal SSRF or network sandbox.
Campus/private destination access remains a separate capability.

Direct mode sets curl `--proxy '' --noproxy '*' --disable` explicitly for every
request, overriding inherited `ALL_PROXY`/`HTTPS_PROXY`/`HTTP_PROXY` and `.curlrc`.
It does not change the parent environment, global routes, DNS or proxy settings.
It has no fictional ready connection: only a real successful curl/TLS/HTTP result
proves reachability. The receipt's `egress_node` is owner-installed attribution,
not physical-host attestation.

For laptop-to-VPS egress, set `transport_mode` to `ssh_socks5h` and additionally
provide `transport_config` and `transport_config_sha256` referring to the private
[owned SSH SOCKS configuration](EGRESS-PATH.md). These two fields must be absent
in direct mode. An SSH mode invocation opens/closes only its own loopback carrier;
target DNS is remote, TLS remains client verified. No existing tunnel is adopted
or terminated.

The minimal owner wrapper registered in the existing runtime manifest is:

```python
from assistant_mesh.egress_adapter import execute

def invoke(context):
    return execute(context, '/private/provider/https-owner.json',
                   'EXACT_64_HEX_SHA256_OF_OWNER_JSON')
```

Use a frozen installed Mesh release for imports, not the development worktree.
The runtime hashes the wrapper; this wrapper pins owner policy bytes and SSH
transport config bytes. Imported package/dependency integrity remains the
existing trusted-installation contract, not a new sandbox/supply-chain claim.
Policy changes require an owner-approved new pinned wrapper/advertised binding
and matching capability epoch, not modifying a JSON file behind a live binding.

## Everyday model scope

The installed action is fixed (example `network.https`). A selection supplies:

```json
{
  "scope": {
    "operation": "get",
    "url": "https://docs.example.org/path",
    "query": {"search": "mesh agent", "page": "2"}
  },
  "workload": {
    "kind": "https",
    "max_bytes": 65536,
    "timeout_seconds": 15
  }
}
```

`operation='probe'` uses HEAD; `get` returns bounded response bytes. `query` is
optional and contains string values only, sorted and URL-encoded by the adapter.
The base URL has no userinfo, raw query or fragment. Core metadata deliberately
rejects secret-bearing URLs; a query map supports ordinary public searches
without weakening that rule. Known credential fields/secret URL values are
rejected, but no text classifier can detect every secret: never supply tokens,
personal messages or other private data. No cookies, request body, model-selected
headers, credentials or executable shell text are supported in this slice.

The model's existing allocation plan still includes capability epoch, target,
observations and any required grant. `ManagedProvider` compares returned pending
scope/workload against the **actual authority allocation**, obtains accept/start
once, durably journals original invocation identity, then executes the wrapper.
The adapter checks provider/capability/action/epoch, workload limits, policy
expiry and the managed invocation nonce before opening any request.

The current core requires an exact scope grant for cross-principal calls and
scope/workload-matched independent observations. An owner public-origin policy
does not silently replace those contracts. A task executed by the same provider
principal can use its own capability without a cross-principal grant; another
node can delegate such a task to that node through A2A. Broader standing grants
or reusable path observations, if needed, are separate core work and must not be
faked by stamping an old observation onto every new URL.

## Result and readback

The callback returns the existing adapter result shape:

```json
{
  "outcome": "completed",
  "resource_quiescent": true,
  "result_reference": "mesh-https-artifact:sha256:...",
  "evidence": {
    "transport": "direct",
    "egress_node": "ali-vps",
    "artifact_id": "https-...json",
    "artifact_sha256": "...",
    "http_status": 200,
    "tls_verify_result": 0,
    "response_bytes": 123,
    "response_sha256": "...",
    "authentication_verified": false,
    "model_inference_verified": false
  }
}
```

`completed` means this bounded transport observation completed, not that the HTTP
status was 2xx, contents were correct or authentication/inference succeeded.
The receipt separately reports `http_success`. HEAD response size/hash describes
headers, not an inferred webpage body. Redirects are not followed. Curl settings
are fixed, certificate verification remains enabled, output and lifetime are
bounded, and the owned curl reader/process (plus SSH carrier if any) finish before
quiescence is reported.

An immutable 0600 artifact stores original operation/task/receipt/nonce,
scope/workload hashes, observed time/status and base64 bytes. No response content,
SSH identity or private artifact path is put into the public result metadata.
The owner can read it locally/over authorized native SSH using:

```python
from assistant_mesh.egress_adapter import read_artifact

artifact = read_artifact(OWNER_CONFIG, OWNER_CONFIG_SHA,
                         receipt['artifact_id'], receipt['artifact_sha256'])
```

This verifies exact file SHA and response size/SHA, never opens a network path
or retries a request. The original bytes remain readable after contract expiry.
It is not a new unauthenticated HTTP artifact server or an ingress publication;
remote agents use existing authorized node/native file access or an explicit
owner-approved result projection.

## Failure and validation boundary

No HTTP retry, redirect retry, route fallback or automatic re-invocation is
implemented. A timeout, invalid/missing status, TLS error, excessive body,
artifact failure or unproved resource shutdown remains a callback error and
durable original `unknown` in the existing provider journal. A private immutable
intent also prevents another direct call for that original operation/capability,
even with a different nonce. Preserve both journals/intents and artifacts across
restarts; never delete them or choose a new operation ID to replay unknown work.
Existing recorded results may reconcile settlement under their original IDs
without calling the adapter again.

20 focused fixtures verify generic/query/probe and artifact hashes, private
config/SHA/origin/TTL limits, owner-only transport selection, explicit inherited
proxy bypass, fake direct connectivity rejection, immutable unknown identities,
and the actual local allocation→runtime→artifact→settlement/capacity-release chain.
The latter uses a network capture fixture, not public HTTPS/model execution.
Those fixtures are not proof of production installation, public networking,
real cross-node invocation/readback or authenticated model usefulness.

## Actual installation and bounded Leader trial (2026-10-10)

Frozen `dd5ae77` was independently tested on laptop and VPS Python 3.6.8
(1761 tests each); [CI 38063838338](https://github.com/gih10012/personal-assistant-mesh/actions/runs/38063838338)
completed successfully on Python 3.8/3.12/3.14. The existing production core
remains `3198c87`. A **separate**, enabled VPS provider service now imports this
frozen source, renews its fixed capability/request-pool leases, and does not
replace the Leader or ClawBot receiver. Actual laptop authority projection sees
`ali-vps-public-https-v1` epoch 1. The private fixed owner contract expires at
2026-10-17 23:59 Asia/Shanghai; pool capacity is one managed request, not a
measurement of physical network bandwidth.

Root independently fetched two distinct public document scopes, checked their
bytes against local frozen Git content, and registered the actual observations.
The original Leader then selected one under the stable task
`PAM-004d-generic-https-handoff-20261010-v1`, epoch 16, retaining its original
native thread. It reserved and linked
`PAM-004d-generic-https-get-20261010-v1`; Root did not reserve or invoke the
callback on its behalf. The standing provider automatically executed GET:
HTTP 200, TLS verification 0, 6294 bytes, body SHA-256
`8fc55daa7d508de4c4a2829a188e1ac3b8f354ea66fbc50b9093c42ab151ac1f`.
Readonly private artifact readback independently matched that Git oracle;
artifact SHA-256 is
`db18fc3035f22d31cf686f0b2107ac2feb8f675b6adb929987fbe69be34d9b3b`.
The original provider journal is `settled`, authority operation `completed`,
and pool held quantity zero. The original native task completed in that same
thread, with all three native plan steps completed. Readonly routing-ledger
checks matched its two-candidate choice, immutable body SHA and original
operation link. This bounded task completion is not completion of the Mesh goal.

Earlier quota/auth/preflight and native-restore failures are retained. Recovery
reused the authorized newer **same-account** cache with backup, and only refreshed
a verified strict old history prefix after full-byte/consistent-ledger backups
and quiescence checks. Authority task/native rows were unchanged during that
maintenance; no old DB restore, thread reset, unknown replay or native-tool block.
An initial backup process exited 137 before publish; the worker was restored and
streaming backup completed the reviewed maintenance.

This proves one generic public scope was genuinely selected/executed/read back,
not arbitrary authenticated Chat/model traffic, physical-host attestation, a
global proxy, another independent egress candidate, or seamless everyday
cross-node standing admission. Those remain PAM-002a/004d; campus is separate.
