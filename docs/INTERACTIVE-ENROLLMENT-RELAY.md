# Interactive enrollment relay

This is a narrow transport for the **existing** owner-created
`mesh-enrollment/1` interactive node challenge. It is not official Codex Cloud
membership, a new node/authorization database, a public listener, an installer,
or a general Mesh management API. Import and handler construction do not bind a
socket, create credentials, initialize a journal, or execute an adapter.

The phone implements the same JSON contract in its native language. It does not
need Python, POSIX, this reference SQLite journal, an always-running daemon, or
the authority's local stdio adapter. Mesh does not intercept or restrict the
phone agent's native tools/network; these rules govern this additional managed
enrollment interface only.

## Owner prerequisites

Use an existing `EnrollmentBootstrap` installation with an explicitly initialized
journal and one explicitly owner-created original challenge. The runtime kind
must be `interactive`. The existing bootstrap binding, enrollment SHA, nonce,
operation ID, complete challenge SHA and deadline remain authoritative; this
relay cannot create or renew them.

Install a private 0600 owner file using
[`deploy/enrollment-relay-owner.json.example`](../deploy/enrollment-relay-owner.json.example).
Every value in that disabled example is a placeholder, not a usable credential
or an authorization. Populate it from the original trusted receipt and task
authority, not a model/device claim. The HTTP resource must be canonical HTTPS
on port 443 at **exactly `/enrollment/node`**. No query, fragment, redirects,
alternate path, userinfo or arbitrary target is supported.

The owner separately provisions a strong, operation-scoped bearer to the phone
and stores only its SHA-256 in this file. This source does not mint it, read a
general operator bearer, or copy existing account credentials. It binds one
owner/device/enrollment, task ID and epoch, original entry-request ID,
operation, nonce, request SHA and expiry. A bearer is not proof that the device
performed a challenge.

The owner also installs a **code-level live task validator**:

```python
from assistant_mesh.enrollment_bootstrap import EnrollmentBootstrap
from assistant_mesh.enrollment_relay import EnrollmentRelay, make_http_handler

bootstrap = EnrollmentBootstrap("/PRIVATE/existing-bootstrap-owner.json")
relay = EnrollmentRelay(
    bootstrap, "/PRIVATE/enrollment-relay-owner.json",
    live_task_validator=installation.validate_original_enrollment_task,
)
Handler = make_http_handler(relay, trusted_proxy_peers=("127.0.0.1",))
```

`installation` above is the deployment's existing trusted authority integration,
not a module supplied here. Do **not** replace its validator with a constant
`True`, a JSON `approved` field, a device self-report, or a constructor that
migrates or recovers a production Store. It must read the real original task
authority and reject mismatched/stale epoch, original request, operation/device
binding, cancellation/revocation, completed/failed/review-needed scope, and
expired authority. Its sole argument is a detached copy of the exact trusted
binding. Missing validation, exception, `False`, integer `1`, or an approval
object cannot admit a new challenge or append the first result.

The validator is a trusted point-in-time check, not distributed consensus or a
global atomic transaction with an external task store. A production installer
must serialize eligibility changes with this scope or enforce an equivalent
current generation/lease at its authority. Journal admission and deadline
checks are atomic only within the existing enrollment database. No production
validator or public service is enabled by this commit.

## Wire contract

All actions use `POST /enrollment/node`, JSON and the original scoped bearer.
The envelope is exact; extra fields and arbitrary actions are rejected:

```json
{
  "format": "mesh-enrollment-relay-request/1",
  "binding": {
    "mesh_id": "existing-owner-mesh",
    "node_id": "existing-interactive-phone",
    "owner_binding": "existing-owner-binding",
    "enrollment_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "operation_id": "existing-owner-enrollment-operation",
    "request_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "nonce": "0000000000000000000000000000000000000000000000000000000000000000",
    "expires_at": 2100000000,
    "task_id": "existing-original-mesh-task",
    "task_epoch": 1,
    "entry_request_id": "existing-original-owner-request"
  },
  "action": "challenge",
  "arguments": {}
}
```

Replace every illustrative binding above with the exact original private owner
configuration. Those eleven fields are exact; no additional fields are allowed.
Integers are strict, including rejecting `true` for
epoch `1`. The phone cannot pick a different ID/epoch or supply an authorization
inside `arguments`.

Actions:

| Action | Arguments | Behavior |
| --- | --- | --- |
| `challenge` | `{}` | Return the original complete `mesh-enrollment-challenge/1`; executable **only** when `may_execute_original_challenge` is exactly `true`. |
| `result` | Complete original `mesh-enrollment-result/1` envelope | Validate original IDs/nonce/continuation/proxy and every result byte's length, canonical base64 and SHA; retain in the original row. |
| `status` | `{}` | Read original journal state; no challenge execution, deadline renewal, result generation, verification or registration. |

Response format is `mesh-enrollment-relay-response/1`, with the same binding,
action and original `status`. Every response says `automatic_retry: false`,
`automatic_replay: false`, `relay_performed_verification: false`,
`relay_performed_registration: false`, `managed_use_authorized: false`,
`native_execution_attested: false`, and `native_features_restricted: false`.
Only `challenge` includes the original challenge/input. A result response
confirms retained original data, **not** remote effect/process closure.

### One external admission, even when an ACK is lost

The original journal operation changes from `intent` to `attempted` and commits
**before** a first executable challenge is returned. Concurrent requests can
receive at most one `may_execute_original_challenge: true` response. Reopen,
lost response, repeated request or an already `unknown` operation returns the
same original challenge/input/hash with that flag **false**. No new operation,
nonce, executable grant, TTL, epoch or task is created.

The phone must preserve its own original operation/nonce/request-SHA attempt
record before execution, and obey the flag. A repeated challenge is observation,
not permission to try again. If the first executable response is lost **before**
the phone sees it, automatic liveness is deliberately sacrificed: neither side
has evidence that execution did not occur. Continue observation or obtain an
explicit trusted reconciliation; do not invent a replacement operation.

The first result requires original prior admission, current owner enable flags,
current task validation, and original lifetime. In the original journal's write
transaction it uses `record_result(..., require_live=True)`; the default native
`record_result` behavior still permits owner-local late original observations.
An already saved **identical canonical full result** is a readback, allowed
after expiry or owner execution-disable without invoking a new task validator.
Changed terminal bytes are a conflict. A missing original journal is an error,
never a reason to create one or repeat the challenge.

### Read authority and verification remain separate

Owner execution-disable and challenge expiry do not erase original history.
With the still-valid original read credential, `status`, nonexecutable
`challenge`, and identical saved-result readback remain available. A first late
result is not admitted through this remote endpoint; a trusted owner may use
the existing local late-observation path without replaying anything. Changing
the pinned owner file fields (other than `enabled`) fails closed for an existing
facade; removing/unpublishing the endpoint or revoking its credential authority
can revoke observation. There is no anonymous post-expiry access.

Stored bytes are `provider_reported`, not independently verified execution.
Separate original owner oracle verification and an explicit trusted registrar
remain necessary. The relay exposes no `run`, `verify`, `register`, `call`,
`create`, bootstrap, task mutation, Shell or management action. It does not
authorize the phone's public egress, filesystem, camera or other business
capabilities merely because the join route and bytes work.

## Deployment boundary

The handler reuses `cloud_http` **transport only** (strict JSON, bounds, exact
Host/path/method, no logs of raw headers/body, no redirects/retries). It never
constructs CloudMembership, CloudJobs or Store. Safe transport error categories
retain their existing `cloud_http_*` names; that is not Cloud enrollment.

Direct connections require actual TLS. Trust for forwarded `https` is opt-in
and only accepts explicitly configured loopback reverse-proxy peers. Keep the
plaintext backend private. The owner must expose **only this exact route**
through an authenticated gateway when ready, with compatible method/body/Host
limits. The existing IP-TLS route allowlist does **not** include this endpoint;
this commit does not change it or publish `/v1`. A loopback caller able to
impersonate an owner-installed proxy is inside that trusted host boundary.

The phone reference currently uses owner manual import/readback. Wiring this
endpoint into its native adapter and installing a real live-task validator,
credential and public-route allowance are separate reviewed deployment steps.
No SDK, signing, background availability, true-device execution, remote closure,
registration, phone capability invocation or full Mesh membership is proved by
the loopback tests.

## Verification

```sh
python3 -m unittest tests.test_enrollment_relay tests.test_enrollment \
  tests.test_enrollment_bootstrap tests.test_cloud_http
```

Tests use real bounded loopback HTTP and the original SQLite journal, with no
Node/phone SDK dependency in the new relay suite. They cover a complete 153600 B
result, independent concurrent admissions, discarded admission ACK/reopen,
unknown operation observation, all binding/epoch/credential/nonce boundaries,
missing validator, expiry/disable/read authority, first-write lifetime inside
the original transaction, immutable terminal results, strict transport and no
new tables/adapter execution/verification/registration. Existing journal
`ResourceWarning` observations are not evidence of process or remote closure.
