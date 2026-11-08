---
name: mesh-phone-computer-use
description: Inspect the owner's enrolled HarmonyOS phone UI and request individual GUI actions or a supported screenshot through Mesh, with device approval and original task receipts. Use for phone computer use; does not provide HDC, a general shell, enrollment, or remote permission changes.
---

Use the installed `assistant_mesh.phone_computer_use` CLI with the owner's private
current-task binding. It uses the existing worker Client, task/epoch and
`capability_calls` journal. No phone bearer, account login or operator credential
belongs in a command, request file, model context or this skill.

The installation supplies an absolute binding path, for example
`/PRIVATE/current-phone-task.json`, with this shape:

```json
{
  "format": "mesh-phone-computer-use-binding/1",
  "config_path": "/PRIVATE/existing-worker.json",
  "task_id": "EXISTING_RUNNING_TASK",
  "task_epoch": 1,
  "phone_node_id": "REAL_ENROLLED_PHONE",
  "commands": ["inspect_ui", "perform_action"]
}
```

This file and the worker config must be owner-installed and private. The worker
config already contains `node_id`, `control_url`, `token_file`, and
`capability_calls: {authority, journal_path}`. Reuse that exact original journal;
do not create another phone-specific journal, copy credentials or create a
business task for discovery. A missing binding is `not_configured`, not a reason
to guess an owner task, operate as operator, or switch to HDC.

Run from the installed Mesh source/package with its configured Python:

```sh
python3 -m assistant_mesh.phone_computer_use --binding /PRIVATE/current-phone-task.json capability_status
```

Choose and preserve one original `operation_id` and `decision_id` per requested
step. Prepare the exact request before choosing existing independent measurement
references or seeking an exact scope grant:

```sh
python3 -m assistant_mesh.phone_computer_use --binding /PRIVATE/current-phone-task.json prepare --command inspect_ui --operation-id ORIGINAL_OP
```

`prepare` returns the exact `action`, `scope` and `workload`. Observations must be
fresh, trusted, independent of this phone provider, and match that exact
scope/workload/epoch. Use original Registry observations and its existing owner
grant process. This CLI does not approve a grant or manufacture a probe. A
declaration, online service or queued allocation does not prove GUI execution.

The request file contains only arguments and existing observation references:

```json
{
  "arguments": {"max_elements": 64},
  "observations": [{
    "observation_id": "ACTUAL_INDEPENDENT_OBSERVATION",
    "metric": "ACTUAL_METRIC",
    "unit": "ACTUAL_UNIT",
    "max_age_seconds": 60
  }],
  "lease_seconds": 60
}
```

```sh
python3 -m assistant_mesh.phone_computer_use --binding /PRIVATE/current-phone-task.json inspect_ui --operation-id ORIGINAL_OP --decision-id ORIGINAL_DECISION --request-file /PRIVATE/request.json
python3 -m assistant_mesh.phone_computer_use --binding /PRIVATE/current-phone-task.json status --operation-id ORIGINAL_OP
python3 -m assistant_mesh.phone_computer_use --binding /PRIVATE/current-phone-task.json result --operation-id ORIGINAL_OP
```

The familiar actions map as follows:

| CLI action | Request arguments | Phone command |
| --- | --- | --- |
| `inspect_ui` | Optional `max_elements` (1–128, default 64) | JSON `inspect_ui`, Registry action `inspect` |
| `perform_action` | Exactly `action`, `element_ref`, `text` | JSON `perform_action`, Registry action `action` |
| `screenshot` | Empty object | JSON `screenshot`, Registry action `inspect` |

For `perform_action`, use a fresh `element_ref` from the original inspection;
`action` is `tap`, `set_text`, `scroll_forward` or `scroll_backward`. `text` is
empty except for `set_text` (maximum 4096 characters). The input JSON has flat
`format`, `command`, `element_ref`, `action`, `text` fields; it contains no task,
operation or epoch override. The phone obtains those from its real dispatch.
Approval, nonce, lease and human-input
fence fields cannot be supplied by the requester. The phone's ControlSession
creates and checks those locally, and the owner approves each input step there.
Human input, disconnect or a changed view can invalidate the step.

Screenshots require their own actual provider entitlement, owner opt-in and
successful probe before the installation adds `screenshot` to `commands`.
Unconfigured capture returns unavailable. Complete command/result bytes are
bounded to 64 KiB; over-limit capture must be bounded or explicitly rejected by
the device, never silently truncated.

Calls are asynchronous. Keep `operation_id`, `decision_id` and
`provider_receipt.receipt_id`; pending/accepted/running may be awaiting the
provider or local owner approval. Read `status`/`result` for the same original
operation. An existing invocation, including an unknown ACK, only queries its
original IDs on repetition. Do not switch IDs, replay input, settle on the
provider's behalf, or label a timeout as success. Results contain the original
complete Blob and verified size/SHA-256; this verifies byte integrity, not that
the requested GUI effect succeeded.
