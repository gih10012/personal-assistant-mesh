# Independent communication/bootstrap peer review

2026-10-14; isolated branch `work/claw-bootstrap-peer-review-20261014`, base
`8a07fab`. Verdict: source is suitable for Root integration **with the three
small communication corrections below**. Production cutover/enrollment is not
proved or performed by this review.

## Merge and review scope

- Communication state commit `f46d08a5d76194595e4d61b178b6c4c330b8e94a`,
  merged on this review branch as `1a3daa8`.
- Bootstrap commit `b300dba05eb86e466da91e14323342ae2b92321f` including its
  `4c81110` implementation parent, merged as `73e024d`.

Author branches and the earlier integration-review worktree remain unchanged.
Main, coordination files, GUI, production services/state/credentials, model
calls and external GETs were not used or changed by this review.

## Necessary corrections, independently reproduced

1. `record_delivery` and `mark_delivery_unknown` did not require activation,
   although authenticated executor HTTP routes called them on prepared state.
   An imported unknown event became confirmed while `activated=false`. Both
   methods now use the existing active gate inside the writer transaction.
   One real loopback HTTP regression checks valid executor reconciliation and
   unknown routes return 403 and preserve the original prepared event/wire.
2. `verify_prepared` compared only JSON reference/provenance fields, not their
   actual lookup columns. Changing `communication_task_refs.authority` while
   leaving its JSON unchanged still returned `candidate_bytes_verified=true`;
   `/pause` routing uses that column. Verification now compares complete task,
   control and legacy-relay reference rows, including keys and target columns.
3. The identity/import SHA comparison used the Store's constructor cache. An
   actual DB marker change after construction still passed. Verification now
   reads and checks the actual singleton identity/import SHA/prepared/activation
   row against the external artifact before reaching the owner verifier.

One additional parameterized regression covers identity marker and all three
reference-table lookup-column changes; it verifies owner approval is never
called for the changed candidate. These fixes use the existing schema/protocol,
do not reconstruct old events or create new migration/grant authority.

## Preserved invariants

Communication state remains separate from runnable tasks and native history;
old inbox is dedup only, unknown/client IDs and notify receiver incarnation
stay original. Attempted delivery retains its target and cannot be reposted or
re-routed. Old task/control rows remain references. The original notification
relay checks its persistent `post_attempted` before POST; the producer subclass
does not generate relay intents for existing old outbox rows. Ordinary new
messages remain independent of older unknowns.

Migration activation is trusted owner-code only, requiring an actual callable
and the pinned original artifact; JSON booleans, PID/TTL/timeout observations
are not substitutes. The hook's real source-writer/canonical/external closure
evidence remains a Root operational responsibility. Activation itself grants
no carrier. Disabled prepared transport performs no iLink IO; active grants,
stop/in-flight observation and cross-node handoff still require real verification.

Bootstrap preserves original operation/challenge/nonce/continuation/result,
checks complete bytes against an independently supplied oracle, and distinguishes
declared capability, provider report, byte verification and registry receipt.
Adapter parameters/oracle/registrar are trusted installation inputs, not node or
model-supplied authority. Missing/expired/disabled state cannot silently initialize
a replacement challenge; original full receipts remain readable. Process exit
and byte proof are not device execution/ownership, model access or permission
to use shared capabilities. Native features remain available. The Python/POSIX
control-plane and Node stdio recipe are adapters, not Windows/phone requirements.
No bootstrap code change was necessary.

## Evidence and limits

- Final risk-proportional group: 268 tests, 19.497 s, passed; communication focus:
  16 tests, 1.297 s, passed. Includes original fence/state/relay, enrollment,
  bootstrap, network inventory and Cloud membership/HTTP boundaries. This is not
  the whole repository CI or actual production operation.
- Original isolated JS sample independently read in SQLite `mode=ro`: exactly
  one original operation, same challenge/request/nonce/continuation and complete
  result/receipt SHA; full 153600 B match the separate non-secret fixture oracle
  byte for byte. Source journal raw bytes stayed unchanged. State remains
  `verified/not_registered`, with no new adapter execution. This is a real
  isolated JS run with synthetic authority, not a third physical device,
  Windows/Harmony execution or official Cloud enrollment.
- Old frozen communication source candidate independently matches all its
  recorded module hashes: 40 original modules, 38 unchanged, only original
  Store/server overlay changes. It declares no migration/install/carrier.
  **It does not include this review's Store/migration fixes**; Root must rebuild
  a frozen candidate from the integrated reviewed source, not deploy that old
  candidate because its old manifest is valid.

Root still owns real writer closure, backup/export/prepare verification, actual
owner hooks, credential/account binding and activation/grant/one-message cutover.
New-device real-authority callbacks, specific device adaptation and disconnect/
rejoin/native autonomous work also remain. Neither source fixtures nor this
isolated byte receipt closes the full four-stream or long-term Goal.
