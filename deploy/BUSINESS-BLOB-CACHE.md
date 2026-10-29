# Optional snapshot BLOB reuse

An initial full bootstrap can reuse complete local history chunks instead of
transferring their bytes again. The source still reads every covered row in
one bounded consistent snapshot; the passive replica publishes the baseline
only after every row and complete BLOB has been stored and verified. SHA256
references alone never constitute a replica. This does not add execution
failover or satisfy the outstanding external-history/workspace dependencies.

The original inline snapshot contract is unchanged. An optional authenticated
begin request selects `blob_mode=references`:

```json
{"action":"begin","blob_mode":"references"}
```

In this mode, snapshot pages include `blob_mode`, a `blob_sizes` object mapping
SHA256 to original byte count, and typed cells `['blob_ref', SHA256]`. These
fields participate in the original page hash. The source keeps at most 6 MiB
of unique raw BLOBs for the current admitted page, with at most 500 rows. A
single BLOB must fit the 4 MiB fetch bound; larger values fail explicitly and
need a future fragmentation contract. This bound accommodates the observed
64 KiB production session chunks without changing the control response limit.

Missing bytes are fetched only from that page's cache:

```json
{"action":"blob","snapshot_id":"...","sha256":"..."}
```

The response has exactly `snapshot_id`, `sha256`, `size` and `base64` fields.
The API checks the existing authenticated reader role/capability, snapshot
owner and absolute expiry. It admits no file paths, SQL, arbitrary historical
digests or other owner's snapshots. Advancing a page replaces the allowed
digest set. Retrying the last page's original `after` returns the identical
mode/page/receipt, even if the requested limit changes. Fetch remains available
for the final page after its snapshot DB connection closes, until explicit end
or the existing absolute TTL; TTL is never extended by requests.

`Replica.apply(page, authority, blob_resolver=None)` checks the original wire
page hash before invoking a resolver. A references resolver receives
`(sha256, size)` and must return complete bytes. Apply verifies size and full
SHA256, restores ordinary typed BLOB values, and stores the original wire hash
as its replay receipt. It does not manufacture a new source page hash. Missing,
disconnected or corrupt dependencies leave the previously published view
intact. Replaying an already committed page needs no resolver/source fetch.

`pull(..., blob_cache=None)` uses references only when a cache is supplied. It
rechecks a local hit's complete bytes; a miss uses the current authenticated
snapshot fetch and verifies the reply before caching/applying it. Incremental
export remains inline. There is no global retry loop, background daemon, new
transport, DB restore or unbounded read window.

## Local seed and CLI

`business_blob_cache.BlobCache(seed_databases=(), directory=None)` indexes only
the explicit `session_chunks(id,part,body)` table of owned standalone SQLite
snapshots. It never constructs a Store, selects other business tables or opens
account credentials. Seed connections use `mode=ro&immutable=1` and
`PRAGMA query_only=ON`; WAL-backed live databases are refused. Supply a complete
closed snapshot, retaining its private ownership. No seed path is hardcoded.

The digest index points to the original seed/file/key. Each hit checks that
file identity, size and modification time still match, then rereads and hashes
the entire original BLOB. A disconnected/changed seed is a miss; a wrong hash
cannot be accepted as a hit. The optional private directory must be owned mode
0700; fetched cache files are atomically replaced at mode 0600 and are checked
again on every read. Cache status prints counts only, without paths or history.

```sh
python3 -m assistant_mesh.business_replica \
  --database /PRIVATE/NEW-PASSIVE-REPLICA.sqlite3 \
  --authority ACTUAL_SOURCE_AUTHORITY_ID \
  --client /PRIVATE/read-only-replication-client.json \
  --seed-db /PRIVATE/COMPLETE-OLD-HISTORY-SNAPSHOT.sqlite3 \
  --blob-cache-dir /PRIVATE/business-blob-cache \
  --pages 1000
```

`--seed-db` may repeat for multiple explicit snapshots. `--blob-cache-dir` alone
also selects references and can retain newly fetched missing blocks. Without
either option, the CLI retains the original inline behavior. An interrupted
bootstrap starts a new consistent snapshot on the next invocation; complete
local/cache bytes can be reused again, while incomplete staging stays private.

Root upgrades the existing source overlay's three business modules under its
writer-maintenance contract. The source does not need `business_blob_cache.py`
and the source overlay builder still emits exactly its three business modules.
Only the passive receiver's frozen namespace adds the cache module. Deployment,
the actual private seed selection and full-row/BLOB acceptance belong to Root.

Four small synthetic fixtures cover local reuse plus one missing fetch,
current-page/owner/expiry bounds, fixed retries and inline compatibility,
original wire receipts, failed dependencies preserving a published view, and
changed seeds/corrupt fetched data. Root runs Click Evidence with short id
`s1blob`, exact argv `python3 -m unittest tests.test_business_blob_cache.BusinessBlobCacheTests
tests.test_business_replication.BusinessReplicationTests`, and absolute workdir
`/home/gihggiy/Github/personal-assistant-mesh-worktrees/business-blob-cache-20261029`.
These checks have not been run by the implementation worker.
