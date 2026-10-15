# Personal VPS: isolated free IP HTTPS, not a public management API

Source preparation branch `work/vps-free-ip-tls-20261015`, base `549bdb1`.
The owner authorized free public IP certificates and automatic renewal on their
personal VPS. Root owns every actual install, CA/account/key creation, port,
firewall and service change. This specialist performed only read-only preflight,
official-source research, local asset generation and isolated tests.

## Current evidence and tool choice

The 2026-10-15 SSH preflight found Alibaba Cloud Linux 3.2104 U11 (al8), glibc
2.32, system Python 3.6.8 and host OpenSSL 1.1.1k. No nginx, Caddy, Certbot,
acme.sh, uv or newer Python was found in the inspected tool paths/packages.
Docker **daemon**, not just CLI, responded as 26.1.3; docker.service was active.
There were no running containers and no TCP 80/443 listeners. Host IPv4/IPv6
INPUT policies were ACCEPT, DOCKER-USER only RETURN, firewalld inactive and
SELinux disabled. Common system/user ACME, nginx and Caddy certificate paths
did not exist. These are bounded observations, not a proof of all possible
certificate files or Alibaba security-group/public reachability.

Use the already-running Docker daemon with two separately pinned official
images; do not replace system Python, install a new daemon, change host proxy,
SSHD, existing application ports or current Claw/Mesh processes. Official
registry manifest bodies were retrieved read-only and their SHA-256 matched
the registry digest header. Current observed tags and linux/amd64 pins are:

| Official image/version | linux/amd64 manifest digest |
| --- | --- |
| `certbot/certbot:v5.8.0` | `sha256:398c47284a6d6782825be71685f677ef3a1e65b8b5c278a8b1e99f6da84b4eb9` |
| `nginx:1.30.6-alpine` | `sha256:959ae6b898852db82e5b29b929388e59914f42be0a8f4d44c89a4c1f53b49208` |

Version/tag observations can age; explicit repository@digest, `--pull=never`
at runtime and independent image inspection prevent silently running `latest`.
The registry image index digests were respectively
`f70ad0adbb7e117f0fe42a63c553f28ea451edabc0148757b6efcd9735acaa20`
and `ff8328b4279ec385af48cde9ef486e7165ad74b7065cb3cd86c377384e2e2dd5`.
[Official Certbot installation](https://eff-certbot.readthedocs.io/en/stable/install.html),
[official nginx image inventory](https://github.com/docker-library/official-images/blob/master/library/nginx).

## ACME facts and privacy

Let’s Encrypt IP certificates are publicly available, require the `shortlived`
profile and last 160 hours. HTTP-01 requires public TCP 80; DNS-01 cannot prove
an IP address. Certbot added `--ip-address` in 5.3; webroot IP support needs at
least 5.4. This candidate pins 5.8.0 and uses **certonly + webroot**, never an
installer plugin or standalone server that stops the gateway.
[LE availability](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability),
[Certbot IP guide](https://letsencrypt.org/2026/03/11/shorter-certs-certbot),
[challenge rules](https://letsencrypt.org/docs/challenge-types/).

`--required-profile shortlived` fails rather than silently taking a different
profile/CA. CA endpoint is explicitly LE staging or production, no paid fallback.
Registration uses the owner-approved no-email option; no personal email, local
auth, browser cookies, API keys or Mesh credentials are given to Certbot. This
still creates a CA account/key and publicly logged certificate for the IP;
account creation and acceptance of CA terms must be Root's explicit operation.
The flag is present in the [pinned Certbot CLI source](https://github.com/certbot/certbot/blob/v5.8.0/certbot/src/certbot/_internal/cli/__init__.py).

## Render, then Root's serialized bootstrap

Copy [the owner example](../deploy/ip-tls-owner.json.example) to private 0600
state and set the authorized public IPv4 and a NEW dedicated `/opt/` directory.
Both sidecar ports remain null until independently authenticated loopback
backends are actually configured. A port number is not proof of authentication.

From the source root:

```sh
python3 -m deploy.prepare-ip-tls --owner-config /PRIVATE/ip-tls-owner.json \
  --destination /PRIVATE/new-ip-tls-assets
```

The renderer only writes a new destination; it never executes plan commands,
Docker, CA, systemctl or network operations. `plan.json` supplies exact argv for
directory creation, explicit image pulls, nginx config test/run, separate staging
and production issuance and an explicitly staging renewal dry run. Root reviews
then executes these commands; existing paths/state must be inspected/backed up.

1. Create only the planned private runtime directories; the separate webroot is
   0755 for public challenge files. Copy nginx configs and deploy-hook into
   `runtime_root/config/`, renew.sh into `runtime_root/bin/`; retain their private
   permissions/owner. No account/key or host directories enter the repository.
2. Start with `nginx-current.conf = nginx-http.conf`. Test this config using the
   pinned image before enabling the rendered dedicated gateway systemd unit.
   It binds **only TCP 80**, serves only GET of validated ACME-token filenames
   for the exact IP Host, and otherwise returns 404. No management route or
   redirect is present. Confirm actual public reachability using a harmless
   challenge-root canary before asking the CA to validate. Host firewall results
   do not prove Alibaba security-group access; do not guess or widen other ports.
3. Run the exact staging `certonly` plan once, then inspect its public certificate
   metadata/SAN. Staging accounts/certificates/work/logs are in entirely separate
   volumes and can never overwrite production lineage. Do not trust a staging
   certificate or switch HTTPS to it.
4. Run the exact production `certonly` plan once after staging/reachability pass.
   Independently inspect the leaf IP SAN, chain, issuer, validity and private-key
   permissions, without printing/copying private key bytes. Do not use force-
   renewal or a replacement certificate name to bypass failures/rate limits.
5. Keep the HTTP bootstrap config as recovery. Atomically publish the reviewed
   `nginx-tls.conf` as `config/nginx-current.conf`, test, then reload/start the
   dedicated gateway. The **directory** is mounted, not the config inode, so
   an atomic file replacement remains visible in the container. On config
   failure keep the existing nginx generation and original certificate; never
   restore an old Mesh database or restart unrelated application services.
6. Externally verify public CA/IP hostname trust and actual served certificate,
   unconfigured `/mcp` and `/cloud/node` without bearer => 401, unconfigured
   authenticated-shaped requests/resource metadata => 503, wrong
   Host/path/query/management paths => rejection. These results prove
   TLS/boundaries, not Chat/Cloud integration or usable OAuth. Only then
   configure audited sidecar ports. Once the real authenticated MCP SDK sidecar
   is selected, missing/invalid bearer must reach its verifier and return its
   standard 401 `WWW-Authenticate` protected-resource metadata challenge; do
   not substitute a challenge-less nginx 401 or configure an unauthenticated
   backend to make linking work.

## Narrow gateway and container boundaries

TLS exposes exactly `/mcp`, `/cloud/node` and
`/.well-known/oauth-protected-resource/mcp`. Everything else, including `/v1`,
task/operator/carrier APIs, panel paths and arbitrary well-known routes, is 404.
Normalized/encoded/queried path variants are rejected. Cloud accepts only POST;
MCP permits its POST/GET/DELETE transport methods and still delegates actual
protocol/session/auth decisions to the SDK. Body caps are 64 KiB for MCP and
8 MiB for the current Cloud facade; unsupported content/transfer encodings,
cookies, expectation/upgrade headers are rejected. Public metadata is GET-only,
small and forwarded without bearer. No wildcard route or dynamic upstream exists.

Nginx's bearer shape check for Cloud and unconfigured routes is **not token
authentication**. With an explicitly configured `/mcp` port, nginx does not
preempt missing/invalid bearer: the actual MCP SDK performs OAuth verification
on every request and supplies the RFC 9728 challenge. Upstream errors and their
`WWW-Authenticate` headers pass through unchanged. Cloud's shape gate remains;
its task/lease token verifier is still mandatory. MCP owner/subject/client/scope
and Cloud task/lease bindings remain enforced in their respective sidecars.
Public resource metadata alone does not make either endpoint authorized or
Chat connected.
[Official OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth#implementing-token-verification).

No operator bearer or generic management port is selected. Proxy requests
reconstruct fixed validated Host, X-Forwarded-Proto
`https` and real peer address; caller forwarding headers are not trusted. No
automatic upstream retry can replay an effectful request.

Both containers use host networking to avoid adding Docker NAT, read-only
rootfs, no-new-privileges, bounded PIDs/memory/CPU and no Docker socket mount.
Certbot drops all capabilities and receives only dedicated ACME/webroot state.
Nginx gets NET_BIND_SERVICE/CHOWN/SETUID/SETGID only, to bind low ports and drop
workers to nginx user with private tmpfs paths. Config, webroot and certificate
live/archive directories are read-only in nginx; CA account keys and renewal
config are not mounted there. Host networking is not an OS isolation boundary against
a compromised container; keep exact image/mount/role review. No host HOME,
browser/profile/SSH credentials or project worktree is mounted.

Access logs and nginx error request logs are disabled, container logs disabled;
Certbot's separate private logs are retained with bounded backup count. Never
enable debug/body/header logging to diagnose bearer failures.

## Twelve-hour renewal and checked reload

Install the rendered renewal service/timer only after the production lineage
exists. It checks every 12 hours, jitter at most 30 minutes; it does **not** force
issuance every time. Certbot automatically renews short-lived certificates when
less than half their lifetime remains. Certbot exit 0 can also mean no renewal,
so an actual successful deploy hook writes a private marker. The host wrapper
tests nginx and reloads only with that marker; it clears the marker only after
both succeed. A failed reload keeps the marker for a later attempt, not another
certificate request. The container never receives Docker socket/control access.
[Renewal/hook semantics](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates).

Run the production-lineage renewal dry-run against **explicit LE staging**, not
a production `--server` accidentally combined with dry-run. Deploy hooks are not
run by that test unless deliberately requested; do not count a timer file or
dry-run as an observed real renewal/reload. Keep certificate/lineage identity,
timer/service results and **served** certificate expiry under observation. Warn
well before expiry (for example 48 hours remaining); a nonzero service result
requires owner/Leader attention. No-email registration provides no email safety
net. A true issuance+later-renewal+served-certificate change remains an actual
operations acceptance, not satisfied by this source/template slice.

### Avoid two layers of renewal jitter

The pinned Certbot **5.8.0** defaults `random_sleep_on_renew=True`. Its renewal
handler, without a TTY, can sleep a uniformly selected 1–480 seconds after the
`Processing` notice and before the first due renewal. `--dry-run` also enters
that branch even when the certificate is not due; a no-renewal exit does not
exercise it. This exceeds the rendered oneshot's `TimeoutStartSec=300` for some
delays, independently of ACME network time. The exact version accepts
`--no-random-sleep-on-renew` and sets that internal option false.
[5.8.0 defaults](https://github.com/certbot/certbot/blob/v5.8.0/certbot/src/certbot/_internal/constants.py#L79),
[5.8.0 renewal handling](https://github.com/certbot/certbot/blob/v5.8.0/certbot/src/certbot/_internal/renewal.py#L607-L648),
[5.8.0 flag definition](https://github.com/certbot/certbot/blob/v5.8.0/certbot/src/certbot/_internal/cli/__init__.py#L426-L429).

Every rendered `renew` argv, including staging and the explicit production-
lineage staging dry-run, now disables this **internal** delay. `certonly` argv
is unchanged. The 12-hour systemd cadence, 30-minute randomized start delay,
300-second timeout, marker/reload order and no-force/no-extra-hook rules remain.
This source diagnosis does not establish why any particular live job is still
running or authorize replay: observe the original job/container receipt until
its state is known. The specialist did not inspect production logs, terminate
an in-flight renewal, issue a certificate or rerun the owner's dry-run.

Eleven local pure tests cover safe new output, shell syntax, official immutable
images, hardening/mounts, exact fail-closed routes/headers/body limits, no email,
separate staging state, staging-only dry-run, hook/reload order and injection
rejection, configured-MCP challenge passthrough and unchanged Cloud/unconfigured
token gates. They do not run nginx, issue certificates or claim production TLS
success. The separate real SDK loopback tests verify actual missing/invalid
issuer/audience/scope rejection and protected-resource challenges using only
synthetic tokens; they are not public TLS/OAuth or actual Chat acceptance. Root
must perform the pinned-image `nginx -t` and real public probes after sidecar
configuration. Neither review changes HTTP bootstrap assets, certificate
issuance/hook/mount commands, or the unconfigured TLS configuration. Only renew
argv, its host wrapper and rendered plan gain the no-internal-sleep flag.
