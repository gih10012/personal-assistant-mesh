# Owner-selected off-host complete backup verification

Leaf `work/claw-abandon-offhost-verify-20261020`, base `c18843f`, continuing the
same original entrance case. Root owns the actual export, stopped-unit boundary,
complete-case review and explicit final-spec approval. No older pinned helper,
core module, native history or production state is changed by installing this
source. The existing local verification path and its space gates stay intact.

## Read-only capture and gzip stream

Run the reviewed source using the original worker/server configs and original
task/epoch/rollout identity. The parent of `snapshot-out` must already be an
owner-only0700 private directory; its file must not exist.

```text
python3 -S -B scripts/verify_entrance_backup_offhost.py capture-export \
  --worker-config /PRIVATE/ORIGINAL_ENTRANCE_WORKER.json \
  --server-config /PRIVATE/ORIGINAL_AUTHORITY.json \
  --task-id ORIGINAL_TASK --epoch ORIGINAL_EPOCH \
  --rollout /PRIVATE/ORIGINAL_NATIVE.jsonl \
  --snapshot-out /PRIVATE/NEW_SOURCE_SNAPSHOT.json
```

Stdout is **only binary gzip**. Root must receive it directly into a new private
file on the owner machine, never display it in a terminal/chat. This helper
does not invoke SSH or copy credentials. It uses `mode=ro` and one `BEGIN` read
snapshot, calls no Store constructor, and streams the complete `iterdump()`
BEGIN-through-COMMIT SQL through gzip. It never creates a full gzip on the VPS.
Success metadata go only to stderr. Broken output or an unsuccessful snapshot
must not be treated as a validated backup; retain original operation identities.

Only after the complete stream and rechecking the original rows/native source
does capture write its new0600 snapshot with exclusive creation and fsync.
Its exact top-level fields are:

```text
protocol, captured_at, source, expected, selected, schema, backup
```

`protocol` is `owner-entrance-source-snapshot/1`. `source` contains original
worker/server paths and file fingerprints, DB path/physical size and logical
read-snapshot size, and only the required node/homes/entrance-policy identity
metadata. No token or original config body is exported. Nanosecond fields are
decimal strings. `expected` uses the existing helper's complete expected-case
schema; `selected` is its original complete task/session/checkpoint/event/three
action-row snapshot. `schema` is the sorted full list of
`[type,name,tbl_name,sql]` from sqlite_master. `backup` contains the complete
compressed `sha256,size` and uncompressed `sql_sha256,sql_size`.

Capture verifies the existing original notice/silent/unknown and two-exec shape,
old artifact and complete native source, but is **not** a successful off-host
restore or owner approval. Source native completion and historical reader EOF
remain unproven. Root still independently reviews the original tool effects.
The receiver and Leader may remain alive; a long read snapshot can retain WAL
pages, so Root must monitor existing authority free space during its owned
export. This source never prunes WAL, history or old backups to obtain space.

The first targeted local check uses synthetic real SQLite and complete gzip,
verifies exact SQL/hash/selected bindings and unchanged original DB/history,
and blocks Store construction. It is not a host3.6.8 or production export claim.

The owner-machine full restore/receipt command and remote receipt consumption
are the next implementation portion of this same leaf. This capture commit
does not yet permit a remote apply to skip the original local backup audit.
