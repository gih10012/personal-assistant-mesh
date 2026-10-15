# Owner review of one known-effects native failure

`scripts/review_native_effects.py` is a narrow, owner-local recovery tool for the
independently audited `exclusive-auth-copy-query-close/1` case. It is not an
agent tool, generic approval/guard-clear endpoint, zero-effect quota retry or
permission boundary on native Shell. No Worker, server or production schema is
changed merely by installing or auditing this candidate.

The reviewed turn failed with `server_overloaded` after six commands and two
original-process observation calls. The known write was an exclusive new private
auth file, not a zero-effect turn. The old frozen Store's `claim` examines guarded
tasks even when their status is `failed`; a status-only change cannot unblock its
scope. Conversely, clearing the flag and making the old input `pending` would
replay work. This tool does neither of those incomplete operations.

## Required independent owner evidence

The private 0600 case spec binds the original task/epoch/scope/native thread and
turn, complete task/checkpoint/session digests, exact full rollout and selected
segment, every call's source/command/full-output digest, original chunk IDs and
the two observation handles. It has a short reviewer time window, at most 30
minutes. A model-supplied `reviewed` boolean is rejected.

Admission is also bound to a **code-owned closed historical case**, not merely
digests supplied by that private spec. Opaque public SHA pins cover every byte of
the six JavaScript call envelopes, complete shell commands (including prefixes,
tails and options), both observation envelopes, selected record counts and
original task/thread/turn/segment identity. A second bundle binds all remaining
spec fields except its short review time window, including source, auth, Worker
generation and private-path bindings. There is no CLI/environment/spec override
to admit a future case. Different source or generation requires independent
source/case review; refreshing a time window alone grants nothing and changes
the exact spec digest requiring explicit owner approval. Do not blindly extend
an expired spec. Configured paths are not proof of authority: the operations
owner must still independently confirm the original ledger and live identity.

Audit actually reparses the complete bytes and all six command/event/output
associations. It verifies all eight outputs are bound to original calls, chunks
join to the complete native stdout, both original handles terminate, no later
turn/unknown effect exists, and the exact failed terminal has no final answer.
The auth-copy AST must still use `O_CREAT | O_EXCL | O_WRONLY`, a 0600 file, 0700
directory, a bounded plain existing SSH auth read and a closed complete write.
The original post-copy account result must confirm authentication; no new model
or account RPC is sent to obtain that evidence.

The AST and shell-shape checks are additional checks on this fixed case, **not a
general Shell/JavaScript effect verifier**. Independent review found five paths
that those old checks alone accepted: a write before the copy heredoc, a write
before the query heredoc, a changed `chmod` target, a write after `rg &&`, and a
non-tool JavaScript `fetch` after the native call. They are now all rejected by
whole-source/command pins even if a private spec is repinned. None of those
synthetic strings was executed. This does not assert that arbitrary future
commands, app-server bookkeeping or external effects have been audited.

Fresh fixed read-only observers on the authority and already-authorized local
SSH peer check auth file identities/complete hashes/generations and authorized
account-binding hashes, emitting only booleans and metadata. They also inspect
the pinned Codex source's `account/read(refreshToken=False)` and owned-runtime
close/reap structure. Actual process generations and original native time-window
survivors are checked alongside original terminal receipts and ledger idleness;
a PID, exit code or process absence alone is not settlement. Account validity
*now*, lack of all native bookkeeping, or closure of arbitrary external effects
is not inferred from this evidence.

The same original ledger must contain no active task on this Worker or scope,
no selected task children/allocation/delegation bindings, and no other guarded
same-scope task. Dependency schemas, the original artifact chunks and a strict
complete old-history prefix are checked. Native history, auth bytes, private
specs and before snapshots never enter the public repository.

## Explicit owner operation, not automatic recovery

Run from the frozen source root, with its normal package import path:

```sh
python3 -m scripts.review_native_effects --spec-file /PRIVATE/owner-review.json
```

Only after independent review and authority to settle this exact known-effects
failure may the operations owner add `--apply --approve-spec-sha256 <digest>`.
The digest identifies the approved private case; it is not itself evidence that
effects are safe or that an account action was authorized. A stale/edited spec,
different generation, incomplete receipt or unreviewed command is refused.

Before the single compare-and-swap, an exclusive private same-case directory
retains a durable intent, full original task/checkpoint/session, original gzip
artifact, complete unchanged rollout gzip and a streamed consistent read-
transaction SQL backup. Backup gzip readback and source `quick_check` are
verified. This works with Python 3.6 without `Connection.backup()`. Never restore
that historical backup over the live ledger.

One transaction appends a new full-history artifact and append-only review row,
preserves the old chunks, preserves `native_sessions.state` byte-for-byte, and
promotes only its complete artifact reference. It retains original task input,
failure result and checkpoint fields; appends an explicit non-zero-effect owner
review; settles only this guard; and marks the original task `failed` with a new
fencing epoch. It never queues the old input, starts a turn, resumes a Goal,
sets a Goal, changes native history files or mutates auth. A separate already-
authorized pending task may subsequently be claimed by the normal Worker.

An existing/partial intent or lost commit ACK must use the same spec's
`--status` readback. No automatic reapply, replacement case ID, deleted intent or
reset of an old unknown is permitted. Status can read an expired original spec;
it cannot grant a new mutation or accept a regenerated spec after an intent.
Readback checks the exact originally approved spec digest and before-state
digests, typed receipt invariants, the original task's failed/new-epoch state and
all preserved fields/checkpoint, the unchanged complete old chunks, and every
uncompressed byte of the retained full-history artifact. A journal row or a
receipt string alone is not proof of successful settlement. A later authorized
task may advance the live native session; readback does not restore or require
that live session to remain at this historical artifact. The append-only
journal and retained private before snapshots preserve original uncertainty
and every earlier failure.

## Explicit completion of the same prepared phase

One real owner invocation failed with `file_size_bound` after writing and
fsyncing `04-consistent-ledger.sql.gz`: its 286,909,413 compressed bytes exceeded
the generic 256 MiB `read_checked` cap. The failure was before `05`, any
`06-cas-intent`, or any ledger review. Original same-case status reported only
`prepared`, no ledger review, and no retry authorization. This is a local
preparation failure, **not permission to run `--apply` again**. Those observed
phase facts do not establish the current phase of a later owner invocation.

The backup alone now uses an owned/no-follow, complete 64 KiB streaming hash
with exact generation checks, including at CAS. The global file cap and native
history bounds remain unchanged; the tool does not read the large gzip into
memory, truncate it, omit tables or make a second backup. Normal new-case
preparation still verifies the complete uncompressed SQL hash and size against
the streaming dump producer.

For this already-prepared case, the operations owner can first run the new
**read-only prepared audit** from the reviewed frozen source root:

```sh
python3 -m scripts.review_native_effects --spec-file /PRIVATE/original-review.json \
  --audit-prepared --fresh-spec-file /PRIVATE/separate-fresh-review.json
```

The original spec can be expired. A supplied fresh spec must be separately
captured and reviewed in Python, currently in its short window, and have
**exactly the same non-window bindings**; it never replaces the original spec
or `00-intent`. If the original is still fresh, omit `--fresh-spec-file`.
Audit refuses any `06`, `07`, existing ledger review namespace, missing original
phase file, conflicting before-state, or unrecognized phase file. It reparses
the entire original native turn and obtains new challenge-bound live controls.

Audit reads every byte of the preserved gzip through CRC/EOF, restoring every
SQL statement into a private **temporary** SQLite database, never the live
ledger. It requires the complete `BEGIN TRANSACTION` through final `COMMIT`,
denies external-database/extension/pragma operations, runs `quick_check`, checks
the complete schema against the authority and the original selected
task/session/checkpoint/old chunks against both the pinned case and
`01-before.json`. It rechecks the backup's full hash/generation and all fresh
controls. This validates the actual retained owner-produced SQL snapshot; it
does not assert that unrelated live rows have remained frozen since that
snapshot. The temporary database is removed; original `00`–`04` bytes remain.

Before restoring, free disk must cover **two live-ledger sizes plus 1 GiB**.
The temporary database has a two-ledger-size page limit and the 1 GiB free-space
floor is checked as statements are restored. No disk expansion, prune, deletion
of recovery state, or second compressed backup is attempted. Low space fails
closed before case mutation; Root may instead arrange an independently reviewed
private verification route, not substitute a model-supplied `verified` boolean.
Large SQLite statements retain a 256 MiB bound; Node is not needed here.

Only after independently reviewing that audit, explicitly complete the **same**
prepared phase, approving the original spec, the separately reviewed fresh
spec (when supplied), and the complete saved-backup hash returned by audit:

```sh
python3 -m scripts.review_native_effects --spec-file /PRIVATE/original-review.json \
  --resume-prepared --approve-spec-sha256 ORIGINAL_DIGEST \
  --fresh-spec-file /PRIVATE/separate-fresh-review.json \
  --approve-fresh-spec-sha256 FRESH_DIGEST \
  --approve-prepared-backup-sha256 AUDITED_COMPLETE_GZIP_DIGEST
```

If no separate fresh spec is needed, omit both fresh-spec options. This performs
the full audit again, appends a `04-resume-review-<fresh-digest>.json` linking both
approvals and verified backup, completes `05`, then uses the original one-shot
`06`/SQLite CAS. An already-written valid `05` can be verified and retained after
a known failure before `06`; it is never overwritten. The original spec and
all existing phase files are retained, not renamed or regenerated. No timer or
automatic retry calls this command. CAS intent creation is exclusive; any
existing `06` or ledger review is **status-only**, even after apparent rollback.

The journal's original approval binding and `--status` remain anchored to the
**original** spec digest. The fresh review is an additional explicit record,
not a silent replacement grant. A lost ACK still requires that original spec's
`--status`; neither a renewed window nor a new spec ID authorizes another CAS.
Task status remains `failed` at the new epoch, never `pending`; native history,
original failure and original input are not replayed.

## Validation scope

Thirty synthetic risk tests exercise the actual parser, original stdout
binding, O_EXCL rejection, authenticated post-check, source/process generations,
dependencies, strict history prefix, CAS race, lost ACK and append-only journal.
They also demonstrate that status-only `failed` still blocks, and that reviewed
failure permits only the new pending task while preserving its native context.
The five original parser counterexamples, private case/source/generation repins,
expired same-spec readback, changed checkpoint, missing old/new history and
subsequent live-session progress are covered. Tests explicitly replace the
code-owned manifest with synthetic fixture pins in process; the installed tool
has no such override.

Prepared-phase tests reproduce the size failure after a complete saved gzip,
guard against any whole-backup read or replacement, verify read-only CLI audit,
same-case completion, unchanged `00`–`04`, expired-original dual approval and
lost ACK. They reject corrupt/truncated/incomplete/reduced/externally attaching
SQL, changed before-state, low space and existing CAS/review. A local interruption
before creating `06` can complete the same retained `05`; an existing `06`
cannot resume. No large production backup or production prepared phase was
restored, completed or mutated by the implementation specialist.

The actual Python 3.6 deployment lacked the Python names for three authorizer
actions, causing its callback to fail closed before restoration. The callback
now uses SQLite's documented fixed C API values only when those names are
absent: `CREATE_VTABLE=29`, `DROP_VTABLE=30`, `FUNCTION=31`. Attach/detach,
virtual-table/pragma operations and extension loading remain denied; function
names are compared case-insensitively as SQLite specifies. The missing-symbol
regression exercises both these denials and a full synthetic prepared audit.
`set_authorizer(None)` is unchanged.
[SQLite authorizer action codes](https://www.sqlite.org/c3ref/c_alter_table.html).

The original production turn passed the author's prior complete read-only
parser and both live observers under Python 3.6; those private receipts remain
historical evidence, not fresh approval of this repair. Independent peer review
then read only the exact eight selected call sources over existing authorized
SSH: all complete source/command pins matched, only the six native execution
calls and two empty original-handle observations were present, and the complete
shell/JS envelopes and original copy target were inspected for the five gaps.
That review did not execute recorded code, read auth or call a model/account RPC.
The old SSH quote-splice delimiter and `input_text` native output encoding were
verified from actual records, not invented by the fixture. The private case's
expired time window was not extended by this specialist.

### Exact metadata handoff correction

A subsequent independently inspected Python read-only capture was eligible in
the full parser and both challenge-bound observers, but failed the code-owned
bindings digest. Excluding the two explicitly different review-time-window
fields, the only differences were 13 integer `*_ns` metadata values. Every
inode/device/size/owner/mode/hash, command, original task/session and Worker
binding was unchanged. Complete V8 `JSON.parse`/`JSON.stringify` of the fresh
non-window spec reproduced the old non-window spec exactly; the original final
proof's rollout/auth/source metadata likewise matched that complete V8
roundtrip. The values exceeded JavaScript's exact integer range (`2**53`), and
the old decimal values ended in `00`, with differences from the exact Python
integers of -55 through +80 ns.

Only the code-owned bindings digest was corrected to the independently checked
Python integer metadata. The case/source/command/poll/history/task pins and all
live generation comparisons remain exact. No float normalization, nanosecond
tolerance, automatic spec rewrite or future-case repinning is supported. The
regression executes a real V8 roundtrip on a complete synthetic spec, shows why
`int(float(value))` does not reproduce `JSON.stringify`'s decimal output, and
rejects both the rounded spec and a genuine one-nanosecond metadata change even
when their floating-point values are equal. It explicitly skips on systems
without Node; Node is a test reference, not an owner-tool runtime dependency.

The old private spec and its expired window remain unchanged and denied. The
new capture is historical read-only review evidence, not permission to apply:
Root must freshly recapture/review the exact Python spec, observe the original
ledger/config/identity and independently approve that spec digest. Do not pass
the spec/proof through a lossy JavaScript-number transport; retain private raw
bytes or encode an opaque transport without reparsing its integers.

No production `--apply`, new model/RPC, service operation or auth write was
performed by the implementation specialist. Deployment, explicit review
settlement and the original Leader's next real task remain operations-owner
work. This one Linux owner repair is not a universal node-platform requirement,
global failure recovery or completion of the Mesh goal.
