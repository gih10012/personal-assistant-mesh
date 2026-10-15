# Executor-scoped communication queue before LIMIT

Branch `work/claw-authority-queue-fairness-20261015`, base `997267c`.
This is a source fix and isolated SQLite/authenticated-loopback validation;
no production, desktop, iLink, model or personal message operation was performed.

## The verified starvation

The old dispatcher fetched the global first 20 non-confirmed events and then
filtered their required/target authority. Twenty-five unattempted old `/pause`
messages belonging to offline `cloud` could permanently occupy that window.
A later ordinary message intended for an enabled local companion never reached
its admission logic. Prioritizing unattempted events over old unknowns did not
fix this separate case: all those old controls remained unattempted forever.

`CommunicationStore.pending` now applies required authority, original target
and ordinary-input policy in its SQL WHERE clause **before** the existing sort
and limit. The original default limit and hard maximum remain 20 and 100;
there is no unbounded scan loop, growing limit or automatic retargeting.

The broker derives authority solely from the authenticated executor peer. The
current Dispatcher sends its existing trusted owner-configured
`accept_ordinary` boolean with `limit`. The endpoint still accepts the legacy
`{"limit":20}` body, retaining visibility of unassigned ordinary candidates,
but scopes even that old body to its authenticated executor. A posted authority
selector or another role cannot select somebody else's candidates. Unscoped
owner-side `store.pending()` remains the original global diagnostic view.

Policy filtering skips new unattempted ordinary/media input when disabled;
required controls stay with their original authority. An already attempted
same-authority event remains available for receipt-only reconciliation even if
ordinary input is subsequently disabled. Other authorities' required controls
and attempted targets never consume this executor's page.

Candidate visibility is not delivery permission. The unchanged broker writer
transaction still binds the original target in `prepare_delivery`. Existing
Dispatcher checks remain defense in depth; attempted/unknown events never POST
prepare/deliver again and never switch authority. Different executors seeing
the same new unassigned message still arbitrate through the original writer.

Media follows the existing admission semantics: with ordinary input enabled,
the original Store can save the reference and unsupported-attachment notice;
this does not download content, manufacture understanding or create a model
task. The fix does not add media upload/send capability.

## Risk tests

Six new cases use actual private temporary SQLite state, the original frozen
ingestion-only Store and a real authenticated loopback broker. They cover:

- Exactly 25 unattempted `cloud` controls plus one later new local ordinary
  message; bounded `limit=20` admits that new message and preserves every old
  control row byte-for-byte.
- Ordinary policy disabled before `limit=1`, so 25 skipped ordinary events do
  not hide the executor's later required control.
- Legacy request compatibility, two authenticated executor authorities,
  forged authority rejection, wrong roles and unauthenticated requests.
- Original local/foreign unknown targets, receipt-only processing under disabled
  ordinary policy, lost-ACK receipt reconciliation and no task redelivery.
- Twenty-five media references, unchanged disabled-policy rows, bounded old
  Store reference-only admission, strict boolean and limit validation.

All fixtures use synthetic accounts/proofs. Each test checks zero iLink wire
calls. Runtime deployment, controlled cutover and a real owner-to-Leader task
remain Root's separately authorized serialized operations.
