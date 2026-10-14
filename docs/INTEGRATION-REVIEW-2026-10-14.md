# Independent local-artifact and frontier integration review

2026-10-14, base `ec14fb7`, isolated branch `work/integration-review-20261014`.
Verdict: suitable for Root integration with the small cached-receipt correction
below. This review is not production deployment or the full Mesh acceptance.

## Reviewed merge scope

- `c6c0ce40f8f07d288de16f2438c2ba7e32a11f17`, local artifact reader/package;
  merged without conflict as `ed23cb7`.
- `af8c1239086b0cce3680a18ce1c738a5f2b85a4f`, frontier refresh/advice;
  merged without conflict as `89fb0d0`.

The author branches, main, coordination files, live services and original
business/native operations were not changed by this review.

## Boundaries verified

Local reader uses the existing original-call and completed provider contracts,
fixed source recipe/SHA and a fixed private cache. No Client, writable call
constructor, Store/provider constructor, GET, auth copy or fabricated A2A record
is needed. The existing full-file/body verifier and non-overwriting spool refuse
wrong pins, unsettled state and conflicting cache. The isolated tests execute the
real fixed pyz child/CommandArtifactReader and compare complete synthetic raw
artifact and 153600 B body, not a summary or hash-only result. This POSIX package
is an adapter example, not a universal device enrollment requirement.

Frontier refresh is an explicitly invoked public GET observation adapter, not
an installed timer, model inference or upgrade. Original request/context and
incomplete attempts remain; the configuration-bound cadence and same-ID retry
do not permit replaying an unknown model/business operation. Source blobs are
untrusted reference data. New docs do not renew old benchmark/route/cost/quota
times. The model adviser remains optional and leaves native features available.
Available model controls can depend on plan/client/workspace, as confirmed by
the [official model documentation](https://learn.chatgpt.com/docs/models#gpt-61-sol);
catalog/document observations alone cannot prove a usable physical route.

## Concrete correction

The completed-ID fast path in `FrontierRefresh._refresh` initially decoded its
cached ledger receipt directly, bypassing the existing `receipt()` hash check.
An isolated fixture changed the receipt's request ID and next due without
changing its SHA: `receipt()` rejected it, but `refresh(original_id)` returned
the altered identity/due. It made no extra GET, but its return was not a verified
original receipt. The fast path now reuses `receipt(request_id)`. One regression
asserts both readers reject, old evidence is not repaired/overwritten, and fetch
count remains unchanged. No live state or timer was modified.

## Evidence levels

- Final integration group: 248 tests, 35.086 s, passed. Frontier focused group:
  18 tests, 0.061 s, passed. Existing Store fixture ResourceWarnings were emitted;
  this is not a warning-free claim. The original interrupted test handle was
  unavailable and no live matching process remained; its lost terminal output
  was not counted as success. Only isolated tests were rerun, not business work.
- Actual local candidate metadata: complete package/source-tree/binding/recipe
  hashes verified; archive contains source/license only, expected local-reader
  module matches the reviewed source, cache has zero files. It declares no
  production install or native Leader consumption. This review did not reread
  the original private artifact or claim the author's earlier inspection as
  independent full-byte verification.
- Actual frontier sample: original receipt SHA
  `f3545e63faefd8bc162f705df13df33087cf71635e9613eeab730f932e49a402`
  verified; all 7 stored HTTP-200 source blobs total 1162488 B and pass complete
  byte/hash checks. Original next due remains `1792288058.8299882`, not yet due.
  The real local `model_advice` reader consumes that evidence and verifies its
  decorated advice SHA: 91 catalog variants, zero numeric points, no forced
  recommendation or model call. No source was refetched for this review.

Root still owns freeze/install and original-Leader full-byte consumption,
maintenance scheduler wiring, actual model/effort dispatch evidence, and the
first complete 3–5-day cycle. CLI consumption is not Leader consumption;
candidate integrity is not deployment, and neither closes the whole Goal.
