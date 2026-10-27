# Phone maintenance contract

The native shell is updated only by a legitimate same-signature HAP installation
or supported distribution flow. This module updates application data. It does
not install a HAP, execute downloaded code, keep the app alive, or establish
that a phone has enrolled in AMITA.

The foreground page imports a trusted owner bootstrap separately from a node
credential. `security/NodeCredentialStore.ets` stores the narrow node credential
in application-scoped Asset, with `DEVICE_FIRST_UNLOCKED`, no cross-device sync
and no uninstall persistence. Its no-argument `load/save/clear` interface uses
`{nodeId, token, expiresAt?, scope?}`; expiry is Unix seconds. Tokens are not
stored in Preferences, the updates DB, filenames or diagnostics. The system
transfer service receives the authorization header for its authenticated job.

The update bootstrap is:

```json
{
  "format": "amita-update-trust/1",
  "node_id": "the-original-node-id",
  "manifest_url": "https://trusted-host/v1/harmony/updates/manifest",
  "key_id": "owner-pinned-key-id",
  "public_key_spki_base64": "base64-DER-SPKI-RSA2048-public-key",
  "enabled": true
}
```

Only an explicit foreground import sets this trust. Subsequent imports may
replace the HTTPS route while preserving the node and pinned signing key.
Installed local manifests keep their signed prior routes for verified local
access/rollback. Route replacement clears the pending candidate, requires a
fresh manifest, and never authorizes network downloads from the old origin.
The manifest cannot replace its own trusted key. The envelope is
`{format:"amita-data-manifest-envelope/1",key_id,algorithm:"RSA-SHA256",payload,signature}`.
`payload` is canonical base64 of the exact original UTF-8 manifest bytes;
`signature` is canonical base64 RSA-2048 PKCS#1 v1.5 SHA-256 over those bytes.
The payload is `{format:"amita-data-manifest/1",version,min_shell_version,artifacts}`.
Each artifact has `{path,kind:"config"|"skill"|"file",size,sha256,url}`.
The server's artifact route is the same origin followed by
`/v1/harmony/updates/artifacts/<sha256>`. All authenticated HTTP and system
downloads disable redirects. Consumers must continue treating these artifacts
as data; `resolveDataPath()` returns a verified private file, not an executor.

Versions are positive safe integers. `min_shell_version` is compared against
the shell's `versionCode` (currently 1000000). Maximums are 32 artifacts,
32 MiB per artifact, 64 MiB total. Relative paths cannot contain traversal,
empty components, absolute paths or supported native/executable suffixes.
Duplicate paths and same-version payload changes are rejected.

`amita_updates_v1.db` is independent of the business/operation journal.
Its compare-and-set lease serializes the UI and Extension. Network and hash
work run outside transactions. Complete artifact bytes must match both signed
size and SHA-256. Files are flushed into immutable content-addressed private
storage before one SQL statement switches the active pointer and retains the
previous signed manifest. A failed request/hash/signature leaves the active
pointer intact. Explicit foreground rollback verifies the previous complete
version, switches the pointer and preserves the highest accepted version;
the rolled-back version is not automatically reinstalled. A newer signed
version can proceed. No live journal/database is replaced or restored.

`AmitaUpdateWork` uses only short HTTP, with a 90-second wall deadline,
1 MiB total response budget and 256 KiB per artifact. Stop destroys the
in-flight HTTP request; downloaded verified content can be reused next time.
It never imports or calls `request.agent`, never instantiates NativeServices,
and never reads selected user files. Work ID 3101 is registered once, persisted,
network/battery/storage conditioned, and requests a four-hour repeat. This is
a scheduling preference: the OS may defer or omit callbacks. `scheduled`
means registration, not a successful callback or continuous online presence.
Large manifests remain pending until foreground reconciliation.

Only foreground `MaintenanceService` creates system-managed BACKGROUND GET
downloads. Creation intent is durable before creation, and the original tid
is durable before start. Paused or interrupted jobs are queried/retrieved by
that tid, never recreated. A lost create reply is reconciled by its durable
unique title; missing/ambiguous tasks remain `unknown`. Failed/removed jobs
are retained for explicit diagnosis. Complete system jobs still undergo full
size/hash verification before activation. The signed range and foreground
progress guard bound normal downloads; system-service transfer behavior while
the app is absent remains a device acceptance item. HTTP credentials may expire
while a transfer waits; failure preserves the original record and old data.

The page can use `MaintenanceService(context)` methods `configureTrusted`,
`setEnabled`, `syncForeground`, `rollback` and `status`. EntryAbility restores
the registered schedule and initiates a foreground sync; every five seconds
while foreground it reconciles original downloads and publishes JSON to
`meshUpdateStatus`. `enabled` is owner preference, `scheduled` is registered
system work, versions/counts are numbers, and last-check/success are Unix
milliseconds (zero means no evidence). Foreground exit cancels short HTTP and
the UI poll. Already-started system jobs remain OS-managed.

Validation uses pure manifest boundary tests and the production SQL executed
against SQLite, including stale/expired lease rejection, atomic activation,
rollback and retained anti-downgrade state. These do not demonstrate device
Asset access, actual background callbacks, transfer resumption, installation
or enrollment. Real SDK compilation and signed phone acceptance are reported
separately by the integrating owner.
