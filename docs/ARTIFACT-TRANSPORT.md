# Original private artifact read / transfer

`assistant_mesh.artifact_transport` is an independently installable library for
reading an **already completed** campus/HTTPS provider artifact and transferring
its exact raw bytes. It does not replace the production core, start a provider,
request the original URL again, change tasks/history/unknown, reconcile, reserve,
settle, grant or launch a new receiver. Native Shell/SSH/file access is untouched.

## Producer: original records, not a child-reported path

The owner installs an owned regular single-link `0600` configuration inside an
owned `0700` directory and externally pins its exact SHA-256:

```json
{
  "schema": 1,
  "authority": "actual-source-authority",
  "sender": "original-enrolled-A2A-sender",
  "authority_db": "/private/original-authority.sqlite",
  "max_artifact_bytes": 25165824,
  "bindings": {
    "actual-capability-id": {
      "adapter": "campus",
      "provider": "node:actual-provider",
      "provider_journal": "/private/original-provider.sqlite",
      "owner_config_path": "/private/original-adapter-owner.json",
      "owner_config_sha256": "EXTERNALLY_PINNED_64_CHARACTER_LOWERCASE_SHA256"
    }
  }
}
```

The example hash is a placeholder, not a valid configuration. `adapter` is only
`campus` or `https`; it never selects a Python module/executable. Configuration,
sender/authority, DBs, root and credential references are deployment inputs, not
model-selectable request arguments. The caller's existing authorized **native
owner SSH/file authority** permits this read; it is not a newly granted Mesh
artifact-read permission. Wrappers must not expose these owner arguments or
credentials as model overrides.

```python
from assistant_mesh.artifact_transport import ArtifactProducer

producer = ArtifactProducer(FIXED_OWNER_CONFIG, EXTERNALLY_PINNED_CONFIG_SHA256)
manifest = producer.manifest(original_message_id, original_operation_id,
                             original_capability_id)
# A private binary pipe/file/SSH channel, NOT an ordinary log/model result:
same_manifest = producer.write_to(original_message_id, original_operation_id,
                                 original_capability_id, private_binary_stream)
```

Both methods read the fixed original authority/provider SQLite files with
`mode=ro`, `PRAGMA query_only=ON` and bounded exact-row queries. They never
instantiate `Store`, `ManagedProvider`, `ProviderRuntime` or an API/server.
Supported table column layouts are checked explicitly; missing or changed
evidence/schema fails, and missing DB files are not created. WAL snapshots can
observe the source's ordinary current committed state; no immutable flag is used
to hide WAL contents. These reads do not freeze concurrent writers or prove a
database's independent cryptographic incarnation.

The source checks:

- Original `(fixed sender, message_id)` maps to the deterministic original A2A
  task; its origin peer/message and local authority still agree.
- The original completed allocation belongs to that task and original epoch;
  task continuation can advance its current epoch without changing the original
  artifact identity. Its selected capability epoch/action/scope/workload agree.
- Completed dispatch, deterministic provider receipt, actual settled provider
  journal/authority metadata, exact context fingerprint and nonce agree.
- Actual canonical settlement equals the journal's original adapter outcome plus
  its original handle/nonce/result reference. Unknown/unsettled records are
  refused without changing or reconciling them.
- The artifact filename is computed from original operation/capability/nonce and
  located only inside the fixed adapter root. Existing adapter `read_artifact`
  verifies the complete raw file and decoded body. Returned identity and
  scope/workload SHA match the original records.

It never parses `task.result`/child text to choose a path. No target URL, source
path, raw body, credential or school network address is returned in the manifest.
The exact artifact file is still private and may contain private response data.

## Consumer: an externally pinned manifest and private stream

```python
from assistant_mesh.artifact_transport import fetch_artifact

report = fetch_artifact(private_binary_stream, externally_trusted_manifest,
                        FIXED_PRIVATE_OUTPUT, max_artifact_bytes=25165824)
```

The consumer **does not authenticate the manifest**. Its wrapper must obtain it
through the existing authorized source route and verify the original immutable
A2A acceptance/task mapping plus the expected operation. A hash supplied only by
the same untrusted stream/model/child cannot attest its own origin. This API
accepts a detached manifest dictionary so native SSH, existing peer transports
or environment-specific agent-created tools can carry bytes; it does not force
a new HTTP protocol or a host-wide policy.

Manifest fields retain authority/sender/message/fingerprint, original task/epoch,
operation/capability/epoch/provider/receipt/nonce, scope/workload/context/settlement
SHA, artifact ID/format/raw byte count/raw SHA and decoded body byte count/body
SHA. Complete raw SHA and body SHA are deliberately separate; JSON is **not
reserialized** during transfer. Evidence stays `provider_reported`; native owner
file authority is explicit, while `managed_artifact_grant_verified`, execution,
target authenticity, account authentication and inference verification remain
false. `artifact_content_verified=true` in the consumer report means exact bytes
and body were checked, not that the fetched page is trustworthy or authenticated.

The output must be an absolute file in an already-owned `0700` directory with
safe non-symlink ancestors. Reads/writes are bounded to at most 24 MiB in 64 KiB
chunks. Binary stream blocking must be bounded by the caller's transport (for
example the owned SSH process deadline); the library opens no network/process.
Raw parsing/body validation are bounded by the same file limit, not constant
memory or a hard real-time guarantee.

Consumer writes a new `0600` staging file, checks exact stream size/raw SHA/full
identity/body SHA, fsyncs, and publishes with a non-overwriting atomic link under
an open directory handle. It removes only its own staging link, then verifies
the named single-link output and attached parent directory before success.
Truncation, extra bytes, conflicting output or directory replacement fail. An
existing identical verified output is an idempotent read and consumes no stream;
an existing different file is never overwritten. A late fsync/namespace/ACK
failure can leave a complete artifact in place: inspect the original output/SHA,
do not delete it or re-run the original business request.

Only immutable file transfer/read may be resumed. This permission cannot be
generalized to re-dispatch the original GET, provider invocation or unknown
effects. The library provides fixed redacted error codes and never prints.
A CLI that intentionally carries bytes through stdout must treat that stdout as
a private binary transport, not log it, and emit ordinary summaries separately.

## Validation / remaining integration

Focused tests use real in-process API/Network/allocation/provider journals with
synthetic HTTP replies, including a body larger than A2A result limits. They cover
original binding, readonly SQL/no source mutations or callback reconstruction,
unknown preservation, schema/config changes, complete bytes/body SHA, historical
epoch reads, private permissions, bounded/truncated/extra streams, no overwrite,
same-file idempotency, disappearing publish conflicts and parent replacement.
HTTPS/campus paths both read original artifacts without another network request.

These fixtures are not proof of a deployed two-node transfer or automatic
foreign capability composition. A production acceptance must show full bytes at
the destination, original acceptance/task/operation bindings, original provider
unchanged/no second GET, independent body/oracle verification and actual parent
agent reading the saved file. Unified discovery/delegate/status convenience and
other artifact kinds remain separate work; the formal core need not be replaced
to install an owner-controlled native wrapper around this library.
