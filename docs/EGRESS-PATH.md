# Opt-in VPS egress path

PAM-002a now has a small usable transport primitive in
[`assistant_mesh.egress`](../assistant_mesh/egress.py): a foreground, owned SSH
dynamic SOCKS5 connection bound to **127.0.0.1 only**, with remote destination
DNS (`socks5h`) and TLS still verified by the calling application. It reuses the
owner's existing SSH profile; no proxy/API credentials are copied into Mesh.

This is not a host firewall, mandatory global proxy, Mesh execution grant,
campus-egress installation, public unauthenticated proxy, or HA claim. Native
Shell/network/MCP remain available. VPS unavailability does not forbid an agent
from using a different native network or repairing/rejoining Mesh.

## Select the path for one operation

Keep this JSON outside the repository in an owner-only file (0600 or 0400).
Use an existing private SSH config/host alias with **no inherited local, remote,
or dynamic forwards**. The primitive refuses such forwards to avoid reopening
or changing a production tunnel. A separate host alias using existing identity
references is sufficient; do not duplicate key contents.

```json
{
  "ssh_destination": "owner-vps",
  "ssh_config": "/absolute/path/to/private/ssh-config",
  "readiness_seconds": 15,
  "max_seconds": 900
}
```

Readiness is bounded and includes an actual SOCKS5 method handshake and a check
that this invocation's SSH process is still running. There is no `ssh -f`,
shared ControlMaster, reused listener, background PID adoption or process-name
kill. A new tunnel chooses an unused loopback port unless `port` is explicitly
configured. Start/bind/handshake failure terminates and reaps only the new owned
SSH process. Raw SSH/curl errors remain private and are not returned in reports.

```sh
python3 -m assistant_mesh.egress --config /private/egress.json probe https://example.com/
python3 -m assistant_mesh.egress --config /private/egress.json run -- curl --disable https://example.com/
python3 -m assistant_mesh.egress --config /private/egress.json hold
```

`probe` is deliberately unauthenticated HTTPS GET/HEAD only: no browser cookies,
API keys, redirects, `.curlrc`, disabled certificate verification or unrestricted
body download. It returns HTTP/TLS status, bounded size/hash and observation
time, never response content. **HTTP 401/403 is reachability, not authentication
or inference success.** HEAD size/hash describes headers produced by curl.

`run` changes a **copy** of the environment for that child only, including
clearing inherited `NO_PROXY` bypasses. The host's environment, routes, DNS,
iptables and existing tunnels are untouched. Applications must actually support
SOCKS proxy variables; an application's own proxy overrides may also bypass
them. For decisive verification, pass the selected `socks5h` URL explicitly to a
supporting client. Mesh does not intercept all native traffic or silently retry
a possibly started model request over another route.

`hold` prints a local proxy URL and serves in foreground until private
`max_seconds`, process failure or interruption. Do not publish the loopback URL
as a remote endpoint; another node establishes its own authorized path. This is
an SSH/curl implementation example, not a requirement that Windows/mobile
agents reproduce systemd or Linux paths.

Python callers can use `with SshSocksEgress(config) as path:` and call
`path.proxy_url`, `path.child_environment()`, `path.probe()` or `path.run()`.
All operations check ownership/liveness and remaining lifetime. Always retain
the context or call `close()`; the library does not install a daemon or promise
to recover an abandoned caller. CLI `hold`/`run` lifetimes are bounded. Closing
this path never stops someone else's SSH tunnel.

## Actual evidence, 2026-10-10

At `2026-10-10T14:02:41Z`, the laptop established a **new dedicated** loopback
SOCKS5h process using its existing owner SSH profile. Server context had first
been checked (`ai_agent`, expected server home, noninteractive sudo available).
An HTTPS GET through this path fetched the public repository README at
`32f68946df18213660d034ef6a1f7e1af637fbd3`:

- HTTP 200, TLS verification result 0; 12,327 bytes.
- SHA-256 `ad50b296f9d754827c0a96e37d467eab238d915c8e18eba5c490c04bcfbfbb27`,
  exactly equal to independent local `git show <commit>:README.md` bytes.
- Through the same path, unauthenticated ChatGPT HEAD returned HTTP 403/TLS 0;
  OpenAI `/v1/models` HEAD returned HTTP 401/TLS 0.
- The owned SSH process exited 0 and was reaped. No production service or old
  tunnel was restarted; no global proxy was changed; no API key/model call/new
  billable resource was created.

The private receipt, target request metadata and owner config stay outside the
public repository. This proves **one real laptop→VPS→public HTTPS transport**,
not browser login, model inference, sustainable throughput or all destinations.
The module has 17 focused tests covering private config, inherited forwards,
owned lifecycle, readiness timeout, split SOCKS replies, environment scope,
expiry, body/time limits and precise 401/403 semantics. These 17 tests passed
locally and on VPS Python 3.6.8 in a new isolated temporary source directory;
the VPS production installation was not replaced. A second short-lived tunnel
after the final configuration hardening produced the same Git oracle match,
403/401 observations and owned exit 0/reap.

## What remains

PAM-002a is not wholly accepted yet. This primitive is not installed as an
always-on production path or advertised/reserved in the capability directory;
another execution environment has not yet performed a routed real request.
ChatGPT account/browser usefulness and authenticated model/tool inference are
separate next checks using existing authority, never inferred from TLS.

PAM-004d can expose a thin discover/call/delegate/status/artifact wrapper around
this actual path, record scope/TTL/failure and shared VPS bottleneck, then let
the model choose it for new work. It should not require the owner to provision
a per-request SSH command. A node adapter can renew its foreground carrier and
report fresh probes; liveness/expired evidence is not authority to replay an
unknown request. PAM-002b campus selective routing/DNS has separate acceptance
and is not made true by this SOCKS path.
