# Owner-abandoned entrance unknown: narrow maintenance helper

`scripts/review_entrance_abandon.py` is a separate owner-only maintenance path
for an independently audited **internal notification**, exactly two original
`exec` calls with complete outputs, and a durable silent decision. It does not
change either older pinned review helper or the receiver/Worker/Store. All real
identities, hashes, paths, tool inputs and chat remain in a private `0600` spec.
An owner-approved hash is review authority, not native-provider attestation.

The original `native_finish/codex_disconnected/unknown` is retained. A final
assistant message, newline, PID absence or current empty cgroup is NOT a native
terminal event, historical reader EOF, quota failure, or zero effects. The three
real action receipts remain unchanged. This helper never runs the tool inputs,
calls a model, sends/replies/handoffs, restores history, creates a Store, stops a
service, enqueues the old input or inserts an `entrance_closed_failures` proof.

## Owner gates and private spec

Root independently reviews the original complete two-call inputs/outputs, then
stops only the independent entrance Worker. Keep it stopped through committed
readback; the receiver and Leader can stay up. This helper supports only the
observed owner **user-systemd / named-systemd cgroup-v1** profile, not a generic
service manager. Other profiles fail closed. It reads fixed `/usr/bin/systemctl
--user` using the current UID's `/run/user/<uid>/bus` and runtime directory,
with a clean environment. It never uses SSH, sudo or a configurable executable.

The private spec has exactly these top-level keys:

```
protocol, case, issued_at, expires_at, worker_config, server_config,
review_root, expected, backup, closure
```

Use `protocol=owner-entrance-abandon/1`,
`case=internal-notification-two-exec-silent/1`, an at-most-1800-second live window,
and an existing owner-only `0700` review root. The CLI defaults to audit.

`expected` keys (see `load()` for exact schema):

- Original `task_id, epoch, scope, event_id, thread_id, turn_id`.
- `worker_sha256, server_sha256, task_sha256, session_sha256,
  checkpoint_sha256, session_state_sha256, event_sha256`.
- `action_row_sha256`: mapping from the three original action IDs to each full
  row's digest. No additional read is needed when the private audit supplies it.
- `rollout_path, rollout_file, selected_sha256, selected_size, record_counts`.
  `rollout_file` is `util.read_checked(...)[1]`: `dev,ino,uid,mode,size,mtime_ns,
  ctime_ns,sha256`; encode the two `*_ns` values as DECIMAL STRINGS. Do not copy a
  `nlink` field or round nanoseconds through JavaScript numbers. `record_counts`
  keys are `record['type'] + ':' + (payload.get('type') or '')`.
- `old_artifact`: `id,parts,compressed_size,compressed_sha256,raw_size,raw_sha256`.
- `tools`: two entries in ORIGINAL order, each containing `call_id,input,
  input_sha256,output_canonical_sha256`. The last field is `util.digest()` of the
  ORIGINAL output value (a content-block list in the reviewed case), not stdout.

Object/row digests are SHA256 of `json.dumps(value,sort_keys=True,
separators=(',',':'),ensure_ascii=True).encode('utf8')`; raw checkpoint/state
hashes use their exact stored UTF-8 strings. No private pins belong in source.

`backup` is an already-created, complete consistent gzip SQL backup with
`path,sha256,size,sql_sha256,sql_size`. Audit reads every byte, restores only a
disposable private DB with SQL authorization and a size/space bound, checks
transaction completion, integrity, complete schema, original selected rows,
receipts and prefix. It never restores over the authority DB or creates a new
backup. A concurrent unrelated task may advance after that consistent snapshot;
the original reviewed rows must still match at CAS.

The new empty verification DB has a hard `max_page_count` cap equal to one live
ledger's current size, with a separate 32MiB work reserve and a 1GiB free-space
floor checked before restoration and after each SQL statement. This reuses the
bounded disposable-DB policy from the independently reviewed deferred helper.
If the full restore needs more pages or free space falls below the floor, audit
fails and removes only its own temporary verification directory. It never grows
the cap, prunes history/backups, or skips complete gzip/SQL/selected-row checks.
Root must obtain a fresh space reading; the smaller cap does not promise every
backup fits. With the reported 1,423,278,080B free and 269,266,944B ledger, the
older gate requires 1,612,275,712B and would reject before restoration; this is
an arithmetic admission check, not an actual host execution. The bounded
preflight requires 1,376,563,200B, leaving only 46,714,880B at that reading.

`closure` is `evidence_path,evidence_sha256`. Its private JSON file has exactly:

```
protocol: owner-stopped-entrance-user-service/1
uid, unit, before, after
owner_stop_completed: true
current_execution_boundary_only: true
native_terminal_proven: false
historical_reader_eof_proven: false
```

`before` and `after` contain exactly the string-valued properties in
`SYSTEMD_FIELDS`. Root records the original active/running unit and its real
InvocationID/cgroup, then the actual inactive/dead state after an explicit stop.
The helper requires `KillMode=control-group`, no job/control/main PID, a newer
inactive generation, and an empty or removed ORIGINAL unit cgroup under
`/sys/fs/cgroup/systemd/user.slice/user-<uid>.slice/user@<uid>.service/<unit>`.
It re-observes that unit and cgroup before/at COMMIT and after readback. A static
owner assertion or missing PID alone cannot satisfy the gate. The entire original
unit subtree must be readable; inaccessible membership is a denial, not empty.

## Root-only commands and outcome

With the reviewed source on the owner host, first use the existing interpreter:

```sh
PYTHONPATH=/OWNER/REVIEWED/SOURCE python3 /OWNER/REVIEWED/SOURCE/scripts/review_entrance_abandon.py --spec-file /PRIVATE/approved-case.json
```

Only after independent approval, run the same command with
`--apply --approve-spec-sha256 <SHA256-OF-EXACT-PRIVATE-SPEC>`. This leaf never
executes it against production. Later query the SAME spec with `--status`, even
after its TTL. An existing review intent prohibits a second apply; unknown or
lost ACK means original-case status, not edited bindings, a new review ID or
replay. An interruption before a known receipt may require owner investigation.

Apply uses the reviewed deferred-helper storage pattern: stage the complete
original gzip, append NEW immutable chunks, CAS only the unchanged session
artifact reference, and CAS only the original `needs_review` task/epoch to
`failed`/epoch+1. Native state bytes, old chunks, input, result, original failure,
event and action receipts stay unchanged. The append-only review records full
before/after identity and `owner-abandoned-unknown`; it explicitly does NOT assert
native completion, historical EOF, zero effects or external effect settlement.
The checkpoint's effect-reservation bit becomes false ONLY under that explicit
owner-abandon review, so normal new input is not permanently blocked by this
abandoned internal notice. This does not authorize retrying the original input.

The original two isolated SQLite checks cover audit nonmutation, full/prefix artifact and
state preservation, original identity, lost ACK/status/no second apply, explicit
approval/closure/epoch/input/backup denials, and COMMIT-boundary rollback. They
mock the service boundary: they are NOT host systemd/cgroup or Python3.6 proof.
An additional focused SQLite check covers the exact reserve boundary, source
nonmutation and rejection of a larger fully pinned restore by the one-ledger
page cap; it passed locally without rerunning the original two checks.
Root must perform the actual host read-only audit before any apply.
