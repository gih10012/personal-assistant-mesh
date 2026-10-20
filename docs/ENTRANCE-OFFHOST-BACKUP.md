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

## Complete verification on the owner machine

Use the same reviewed helper sources on both machines. The validator hashes
its four script sources into its receipt; the apply host must see those same
hashes. Its Python/SQLite versions and owner-selected auditor label are recorded,
not claimed as host attestation. A snapshot from the initial `8c3cd91` capture
remains compatible; it need not be exported again merely to add this validator.

```text
python3 -S -B scripts/verify_entrance_backup_offhost.py verify \
  --snapshot /PRIVATE/SOURCE_SNAPSHOT.json \
  --backup /PRIVATE/COMPLETE_OWNER_BACKUP.sql.gz \
  --rollout /PRIVATE/COMPLETE_ORIGINAL_NATIVE.jsonl \
  --receipt-out /PRIVATE/NEW_OFFHOST_RECEIPT.json \
  --work-root /PRIVATE/EMPTY_VERIFICATION_WORKSPACE
```

All inputs are owned ordinary0600 files. The work root and receipt parent must
already be private0700 directories. The receipt must not exist. Full compressed
bytes/hash, gzip CRC/EOF, all SQL bytes/hash and BEGIN-through-COMMIT are checked.
The complete SQL is restored only into a new disposable local DB. Its logical
page cap is one original read-snapshot DB size, with32MiB work reserve and1GiB
free-space floor. SQL authorizers still reject external databases/extensions/
PRAGMAs and virtual tables. The restored DB must pass quick_check, complete
schema, every selected original row/three action receipt, and original artifact
prefix checks. The full native copy must match the exact source hash/size and
all original selected records, two exec inputs and complete output digests.
Source-machine inode/ns metadata are kept as origin pins; local copy metadata
are checked for stability, not falsely required to equal a remote filesystem.

The new receipt has exactly `protocol,request,started_at,completed_at,auditor,
checks,authority_basis,host_attestation,native_terminal_proven,
historical_reader_eof_proven`. Its canonical request binds the unchanged case ID,
snapshot file SHA, expected/selected/schema digests, complete backup/SQL digests,
full native source and logical restore cap. This avoids a spec/receipt hash
cycle: the final spec later pins the complete snapshot and receipt files.
`authority_basis=explicit-owner-final-spec-sha256`; host/native-terminal/EOF
attestation flags remain false. A receipt, its check booleans, or a source hash
alone is NOT write authority. Root must independently review the original case
and the exact complete receipt, then approve the final spec SHA explicitly.

## Explicit original-case off-host mode

Root transfers only the small snapshot/receipt back as private immutable
evidence. The full gzip need not exist on the VPS. Preserve the original spec
fields and populate its `expected` from the reviewed snapshot, and `backup`
from its complete hash/size fields plus an owner-machine path label. Add exactly:

```json
{
  "backup_verification": {
    "mode": "owner-off-host",
    "snapshot_path": "/PRIVATE/REVIEWED_SOURCE_SNAPSHOT.json",
    "snapshot_sha256": "EXACT_COMPLETE_FILE_SHA256",
    "receipt_path": "/PRIVATE/REVIEWED_OFFHOST_RECEIPT.json",
    "receipt_sha256": "EXACT_COMPLETE_FILE_SHA256"
  }
}
```

The optional mode does not change case_id. Without it the original local path,
space gates, backup restore and receipt shape remain unchanged. Existing case
intent still means same original spec/status only, never a replacement apply.

The apply host checks the exact approved files and their generations, receipt
request/complete checks/auditor code, current original config identities and
generations, complete schema and selected task/session/event/action pins. The
normal native source/old chunks and current stopped user-unit/cgroup checks
remain. Receipt/snapshot checks repeat before the write transaction and at
COMMIT. After committed state readback, their complete bytes/generations and
auditor sources plus the live stopped boundary are checked again. That last
file check does not compare the pre-review schema to the review table/triggers
which this transaction deliberately added.

Apply still requires `--apply --approve-spec-sha256` for the **entire exact final
spec**, including these pins. It records complete backup verification as
`owner-off-host`, `host_attestation=false`, and
`complete_backup_verified_on_apply_host=false`. It does not claim that the VPS
read the gzip or SQL bytes verified on the owner machine. The original unknown,
nonzero effects, no-native-terminal/no-historical-EOF facts, append-only full
artifact/state preservation, owner-abandon marker and lost-ACK/status-only
behavior are unchanged. There is no new native task, replay or permission.

Four new isolated boundary checks cover capture, real complete local restore,
the documented audit CLI, exact-spec approval, absence of a backup on the apply
host, tampered inputs/receipt/auditor code, and a receipt change at COMMIT causing
rollback with the original intent retained. They use synthetic SQLite/files and
a mocked stopped-unit boundary; no old group, model, SSH or production operation
is run. Root's real complete off-host backup verification and real stopped-unit
audit are separate required evidence.
