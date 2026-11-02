# Controlled OAuth state bundle and inactive staging

This adds an observed freeze seam to the existing GitHub OAuth gateway and
copies its complete pinned SDK FileTree state into a new private bundle and
target staging directory. It does not activate a target, change a public URL,
install a service, exchange a real token, elect a writer or implement failover.
The original issuer/resource/client/owner/scope/redirect policy remains exact.
The gateway still passes the original conversation/session services to the
unchanged MCP factory and retains its thirteen tools when configured with them.

Only the configured dedicated Mesh OAuth App state is in scope. No browser
cookies/profile, PAT, gh/Codex login, signing material or account search is read
or copied. The three dedicated service secrets remain separate private
references, not plaintext bundle contents.

## Actual writer boundary

`chat_github_oauth.create_app` returns the same `(provider, server, app)` shape.
The returned ASGI object delegates the original app/router/lifespan and exposes
`app.oauth_migration` only to trusted deployment code; no HTTP/tool/admin route
is added. It instruments the actual provider and FileTreeStore async methods,
including lazy collection/index creation, reads that can initialize metadata,
put/delete and cull. This uses the real SDK implementations, not a new OAuth
code/refresh engine.

`await app.oauth_migration.freeze()` closes new outer ASGI admission, waits for
all admitted calls, seals new independent SDK calls and waits for the actual
registered provider/storage tasks. Existing nested work can finish; a detached
child gets its own tracked task. Caller cancellation/disconnect does not cancel
or remove still-running shielded I/O. A drain timeout leaves admission closed
and issues no lease. Frozen HTTP returns 503/not_attempted; later SDK writes
are refused. Nothing automatically reopens after a failed export.

The ASGI lifespan continues to be forwarded; HTTP/WebSocket calls are counted.
Any lifespan or detached SDK access still passes the observed provider/store
boundary and cannot start a write after sealing. The pinned OAuth/FileTree
sources do not install a separate periodic writer.

The returned in-memory FreezeLease is bound to this controller, zero current
counters, the exact original policy and three key references. A bool, JSON
`source_stopped=true`, forged receipt or another configuration is not accepted
by export. The lease is not a cross-process or restart fence.

Runtime checks bind all seven typed SDK stores to the same encrypted backend
and configured FileTree data/metadata root. ID-JAG must actually be disabled,
its validator absent, its in-memory revocations empty, transparent-refresh
locks unlocked, and CIMD private-key-JWT replay cache empty. Nonempty replay or
unsupported memory-grant state blocks a claimed frozen export; it is never
silently dropped. Per-request ContextVars are drained. Non-authoritative CIMD
metadata/verifier caches are cold reconstruction dependencies under the same
approved client/redirect policy, not copied authorization grants. This is not
support for migrating active ID-JAG/private-key-JWT replay state.

The reviewed runtime inventory checks exact package versions and source hashes
for the proxy/consent/upstream/models, CIMD/ID-JAG, FileTree, managed entries,
serialization and encryption/adapters. Required versions remain FastMCP-slim
4.1.0, MCP 2.3.0, py-key-value-aio 0.4.6 and cryptography 50.0.2. A changed SDK
must be reviewed and deliberately repinned; version strings alone do not prove
that the write boundary is unchanged.

## Complete bytes and explicit restore dependencies

The seven collections are upstream token sets, client registrations,
transactions, consent CSRF tokens, authorization codes, JTI mappings and refresh
metadata. Export recursively preserves every supported encrypted entry's exact
bytes, original absolute created_at/expires_at, collection metadata/index and
empty directories. Expired entries and orphan SDK `.tmp` bytes remain evidence;
they are not culled, renewed or promoted into successful grants. Unsupported
non-SDK files/directories fail closed instead of being omitted.

Two complete source walks compare bytes, membership and dev/inode/size/
mtime/ctime/mode. Changing a file or collection during capture rejects the
bundle. Every file has size/SHA256/base64, the whole inventory has a hash, and
the canonical envelope has its own SHA256. Bounds are 10000 files, 1000 dirs,
8 MiB/file and 64 MiB raw total. Writes use new owned-0700 directories and
owned-0600 files with fsync, never overwrite a running FileTree root.

The configured FileTree 0.4.6 uses its default `PassthroughStrategy`, so these
seven logical collection names are the actual physical directories. Its
optional V1 sanitizers are not installed by this gateway. Freeze verifies both
actual strategies; their reviewed sources are pinned. Its `*-info.json`
metadata records an **absolute** collection directory. The
source bundle preserves those original bytes; staging creates a separately
hashed view which changes only that metadata `directory` field. All original
timestamps, ciphertext and other fields stay intact, and the report maps each
source/target metadata hash. Entry TTL is never rebased via SDK `put()`.
The original source bundle remains alongside the derived view.

Key roles are `dedicated_github_oauth_app_secret`, `mesh_oauth_jwt_signing` and
`mesh_oauth_state_fernet`. Their explicit source references, complete-file
hashes, sizes and dedicated-service scope are recorded. Target key paths are
supplied separately by Root through a private role map. Verification checks
each file and decrypts every encrypted entry with the matching Fernet key
without changing its TTL. It never opens a source-host reference on the target.
Missing key paths can still produce inactive staging, but the report says
`key_dependencies_verified=false`.

The owner-bound ingress backend, conversation/session services, exact TLS/AS
routes and all original writer/restart exclusion remain external dependencies.
They are neither fabricated nor silently copied from account credentials.
An envelope hash is integrity, not producer authentication: pin the source
bundle SHA256 separately with `expected_sha256` on the target.

## Deployment-owned use; no public activation

Trusted code holding the actual running app may later do the following during
Root's controlled maintenance window. These are templates, not executed
production operations:

```python
from assistant_mesh.oauth_state_bundle import export_bundle, stage_bundle

lease = await app.oauth_migration.freeze(timeout=30)
manifest = export_bundle(owner_document, '/PRIVATE/NEW-EXPORT', lease=lease)
# Transfer NEW-EXPORT/oauth-state-bundle.json privately; record manifest['sha256'].

report = stage_bundle('/PRIVATE/RECEIVED/oauth-state-bundle.json',
    '/PRIVATE/NEW-TARGET-STAGING', target_owner_document,
    key_paths=private_dedicated_service_key_role_map,
    expected_sha256=source_bundle_sha256)
```

`owner_document` and target policy must bind the same issuer/resource, owner,
approved clients/scopes, GitHub App and redirects. Only local file locations
may differ. An optional export without a FreezeLease is accurately marked
`unfenced_stable_file_copy`, not an observed ASGI/SDK drain.

Every target is **staging-only**. A marker is durably installed before copying
files, so an interrupted staging copy stays blocked too. The new gateway
factory refuses a marked directory. `activate_staging` always rejects, even
with caller-supplied true/JSON fence claims. There is currently no trusted
exclusion proof for other processes, old code, machines or restart paths.
Removing the marker or starting an old unguarded factory is not activation
authority. Actual common fencing belongs to the independent S2 work; this
module does not invent a CAS and label it consensus.

Source implementation, passing synthetic fixtures, deployed freeze and actual
controlled OAuth migration are separate facts. This release does not move
ordinary Chat, relax issuer/audience, or claim OAuth HA.

## Focused evidence

The new fixtures use real pinned SDK PKCE/consent/code/refresh/revoke state and
thirteen tool registrations, a synthetic IdP transport and temporary private
directories/keys. They cover complete restored ciphertext and metadata/TTL,
consumed-code/revoked-refresh absence, cancellation plus detached I/O draining,
unsupported memory/replay/lock refusal, integrity/policy/key rejection, and
startup-blocked staging. No real credentials, network, GUI, production data or
model request is used. Existing token-consume serialization remains in place.

Run the targeted unittest selection through Click Evidence in this worktree,
using the existing isolated pinned Python environment. A dependency skip is not
OAuth evidence. Actual execution results and the final command are reported in
the implementation handoff, rather than claimed in advance here.

On 2026-11-02 the standard direct argv (`PINNED_VENV/bin/python -m unittest`,
without `-B`) was accepted by Click as `oauthsdk` and ran all four new fixtures
plus the existing concurrent-code-consume and issuer/resource/client denial
leaves. The initial real run exposed the mistaken optional-V1 layout
assumption; the corrected default-Passthrough layout and Path/string boundary
then passed this same group. The earlier `-B` argv's pre-execution rejection
remains a separate historical fact, not a current testing blocker or a test
pass. No wrapper, system-Python dependency skip or check substitution was used.
