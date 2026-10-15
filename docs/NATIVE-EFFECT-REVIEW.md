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

Audit actually reparses the complete bytes and all six command/event/output
associations. It verifies all eight outputs are bound to original calls, chunks
join to the complete native stdout, both original handles terminate, no later
turn/unknown effect exists, and the exact failed terminal has no final answer.
The auth-copy AST must still use `O_CREAT | O_EXCL | O_WRONLY`, a 0600 file, 0700
directory, a bounded plain existing SSH auth read and a closed complete write.
The original post-copy account result must confirm authentication; no new model
or account RPC is sent to obtain that evidence.

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
it cannot grant a new mutation. The append-only journal and retained private
before snapshots preserve original uncertainty and every earlier failure.

## Validation scope

Seventeen synthetic risk tests exercise the actual parser, original stdout
binding, O_EXCL rejection, authenticated post-check, source/process generations,
dependencies, strict history prefix, CAS race, lost ACK and append-only journal.
They also demonstrate that status-only `failed` still blocks, and that reviewed
failure permits only the new pending task while preserving its native context.
The original production turn has additionally passed the candidate's complete
read-only parser and both live observers under Python 3.6; private evidence is
retained separately. The old SSH quote-splice delimiter and `input_text` native
output encoding were verified from actual records, not invented by the fixture.

No production `--apply`, new model/RPC, service operation or auth write was
performed by the implementation specialist. Deployment, explicit review
settlement and the original Leader's next real task remain operations-owner
work. This one Linux owner repair is not a universal node-platform requirement,
global failure recovery or completion of the Mesh goal.
