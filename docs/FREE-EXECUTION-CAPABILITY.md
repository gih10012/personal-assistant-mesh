# Temporary native free execution callback

Source candidate only. No production enrollment, service/config change, model
request, credential creation or paid fallback was performed by this slice.
The separate [stock-client trial](ANONYMOUS-STOCK-FREE-TRIAL.md) remains the
actual anonymous inference/tool/same-session evidence. Its old operation is not
repeated by this implementation.

`assistant_mesh.free_execution.execute` is callable from an owner-installed
`ManagedProvider.Adapter` or a hash-pinned private `ProviderRuntime` wrapper.
Codex remains the continuous Leader; Pi remains optional. Genuine OpenCode is
only an explicitly selected temporary execution resource, not their replacement
or an impersonated API transport.

## Contract

The fixed private owner config binds provider, capability/epoch/action, genuine
client and bubblewrap binary hashes, the complete public catalog/hash, a single
`opencode/*-free` model, physical-model reference, effort, expires-at time and
bounded input/output/runtime/step limits. Catalog input/output/cache-read/
cache-write costs must all be zero. A non-default variant must appear in that
catalog; this is still not independent AA quality/effort verification.

Private owner-owned directories are `0700`; config, catalog and request JSON
are single-link regular `0600` files, with exact expected hashes. Example files
in `deploy/opencode-free-*` intentionally have unusable references/expiry until
reviewed and filled privately. Importing the module does not start anything.

Only the admitted scope and workload choose an immutable payload:

```json
{
  "scope": {
    "request_sha256": "SHA256-OF-EXACT-PRIVATE-REQUEST-BYTES",
    "privacy": "public-synthetic"
  },
  "workload": {
    "kind": "native-free-execution",
    "timeout_seconds": 180,
    "max_output_bytes": 1048576
  }
}
```

The owner/native file entry creates `<request_sha256>.json` in the fixed
private request directory; it is not a model-selected host pathname. Its shape:

```json
{
  "schema": 1,
  "prompt": "Perform the explicitly delegated work on the attached files.",
  "inputs_base64": {"input.txt": "aGVsbG8K"},
  "artifacts": ["result.txt"]
}
```

Inputs/artifacts are bounded relative paths; absolute paths, traversal, symlinks
and `.git` entries are rejected. Prompts/input/full native exports do not enter
public capability metadata. `public-synthetic` is the example's only allowed
privacy scope. `owner-approved-third-party` requires a separately reviewed
owner config and actual payload authorization; a string label is not proof of
that authorization or a data-use guarantee. This callback never uploads the
Leader's full history or searches for credentials.

## Native execution and no replay

1. Existing ManagedProvider records authenticated admission/start/invocation
   intent. Its journal is preserved across restart, node/model/version changes.
2. Before launching, callback atomically creates a private directory keyed by
   original provider/operation/capability and writes an immutable intent. A new
   nonce cannot bypass an existing intent; accidental direct reentry is rejected.
3. This Linux example exposes only the new payload workspace/state to a
   bubblewrap namespace. Personal home is masked, original `HOME`/`CODEX_HOME`
   environment values remain unchanged, and credential environment variables,
   personal plugins/skills/auth/history are not copied. The client's config and
   catalog mounts are read-only. No global host policy/proxy is changed.
4. Genuine stock `build`, default prompt and all its built-in tools remain
   intact, including shell. Only the selected free provider/model is enabled;
   auxiliary title/summary/compaction work is disabled. There is no fabricated
   user agent, session header, replacement tool schema, relay, login or retry
   with a paid model. A private owner proxy may be used solely in this child.
5. Native server/session IDs are created once; SSE is observed before the single
   explicit user turn. First observed native retry aborts that original session,
   not a replacement. Hidden transport retry/HTTP POST counts remain unknown.
6. CLI exit zero is insufficient. Completed assistant records must use the
   selected physical route and report cost zero, with no native error or pending/
   failed tool. Original native export must exactly equal all API messages.
   Full requested artifact bytes are collected and hashed. Full messages/export,
   events and the native data directory remain private and continuous.
7. Owned clients/server are terminated and waited; their PID namespaces are
   configured with `--unshare-pid`/`--die-with-parent`, so namespace init exit
   also kills its remaining children. The callback joins its observer before
   reporting local compute quiescence. This is **provider-reported**, not an
   independent production observer or proof of remote side-effect closure.
8. Timeout, missing outcome, paid/model drift, unsettled tool, export mismatch,
   missing artifact or unproved cleanup raises a fixed diagnostic. ManagedProvider
   retains `unknown`/capacity and never invokes it again; explicit same-ID
   reconciliation only inspects/reports already durable outcomes.

Successful `result_reference` is `mesh-native-free-execution:sha256:<digest>`;
public evidence contains a private artifact ID/hash, model/reference/effort,
cost basis, TTL, privacy scope and bounded observation—not session IDs, prompts,
tool output or machine paths. `read_result` reads/hash-checks the original private
result without launching anything. It requires neither a current client/catalog
nor an unexpired execution contract, so upgrading/removing dependencies does not
hide old results. `describe` emits a candidate, never auto-advertises and never
claims live availability merely because files or catalog entries exist.

## Verified here; remaining boundaries

16 focused local tests passed; 80 callback/provider/runtime tests passed. These
use fake native execution and real local ManagedProvider/API/SQLite admission,
settlement and unknown no-replay behavior. They include private/result hashes,
path/symlink checks, zero-price drift, TTL/privacy/limits, selected-model/native
cost checks, export equality, retained intents, quiescence rejection, same-ID
settlement/no replay and historical reads after catalog deletion/expiry.
There were **zero actual model requests or production mutations**.

Still required: one new explicitly scoped task chosen/called by the original
Mesh Leader through the provider, actual namespace/process cleanup and original
result consumption, then availability evidence/renewal. This source does not
register that capability, install a service, wire model dispatch, change Codex/Pi
history or prove official Chat/Codex Cloud integration.

Each invocation currently has one new native session and preserves its exact
export/data. Cross-operation continuation of that external session is not yet
implemented; no summary is passed off as its complete memory. Native logs/session
files are private and runtime-bounded, but this is not a hard OS CPU/disk quota
or protection against a malicious same-UID host process. Namespace tools may
contact networks as explicitly requested; remote effects need their own contract.
The ordinary host agent's native shell/network remains unaffected. Windows,
mobile or a different installation adapts the public contract rather than being
forced to use this `/usr`-client/Linux namespace example.

Catalog/native cost zero is a route observation, not a billing audit, unlimited
quota, permanent free availability or benchmark of the matching AA variant.
The configured proxy's downstream VPS path remains unverified. Callback success
does not grant independent execution attestation; `execution_verified` remains
false in the ManagedProvider authority.
