# Phone owner operations

These operations use the running receiver, its original Store, the existing
enrollment journal, independent byte oracle and capability Registry. They do
not construct a second business Store. Install the additive phone overlay once;
later phone relay and node bindings are installed under the live service lock,
without restarting the receiver for each enrollment.

## Before connecting the phone

Keep the commercial unsigned HAP, its build receipt and the same private signing
key/CSR. Obtain the Huawei debug certificate when the account permits it. The
device-specific Profile, signing, installation and real enrollment still require
the actual device. Do not manufacture its node identity, UDID or byte result.

Deploy `phone_owner.py` with `phone_authority.py`, `phone_http.py` and the existing
phone dependencies. `phone_http.start` attaches the operator dispatch wrapper.
The fixed owner directory is `phone-owner` beside the configured phone authority
JSON. Directories are owner-only and files mode 0600. An absent directory is
valid before the first real enrollment and does not create a challenge.

Create a private transport configuration pointing to the existing authenticated
operator interface, with its existing `control_url` and `token_file`. Do not use
a worker token or put operator credentials in the phone. For example:

```sh
python3 -m assistant_mesh.phone_owner --config /PRIVATE/operator.json plan
python3 -m assistant_mesh.phone_owner --config /PRIVATE/operator.json status
```

These calls report loaded state only; they do not register a phone or prove a
real device has performed a task. Public Nginx must keep these management routes
closed. The only routes added to the private receiver are POST
`/v1/phone/owner/{plan,status,prepare-existing-binding,verify,register,issue}`.
No caller-supplied path, command, validator or arbitrary module is accepted.

## When the phone is present

1. Install the legitimately signed HAP and obtain its actual first-start node
   identity. Bind the enrollment to the actual enrollment task, its current
   epoch and original request ID. That task must be live in the phone project
   scope `leader:project:amita-harmony-onboarding`; the development task for these
   tools is not a substitute for an enrollment request.
2. Prepare `phone-owner/bindings.json` with an `enrollments` array containing the
   local enrollment ID. Beneath that fixed ID directory, install `settings.json`
   and the existing Bootstrap `bootstrap.json`. Settings contain exactly
   `format: amita-phone-owner-binding/1`, `node_id`, `subject`, `session_id`,
   `capabilities`, `visible_nodes`, `task_id`, `task_epoch`, `entry_request_id`.
   Bootstrap must name the same real node and mesh, use interactive runtime and
   that directory's `state` subdirectory. Reuse the existing enrollment owner
   schema and byte oracle; do not write fabricated verification receipts.
3. Run the actions below with the same enrollment ID. Transfer the private
   `relay-export.json` to the phone through the approved initial setup channel.
   The phone calls the existing public `/enrollment/node` challenge/result
   protocol. Challenge retry observes the original operation and nonce.
4. Only after the phone's actual result is present, run `verify`, `register` and
   `issue`. The independent original Registry query must match. `issue` installs
   the scoped node binding live and stores its private bootstrap in
   `issued.json`; transfer only its `bootstrap` object to the phone. Update trust
   is included when the running update service is configured.

```sh
python3 -m assistant_mesh.phone_owner --config /PRIVATE/operator.json prepare-existing-binding --enrollment-id REAL_ID
python3 -m assistant_mesh.phone_owner --config /PRIVATE/operator.json status --enrollment-id REAL_ID
python3 -m assistant_mesh.phone_owner --config /PRIVATE/operator.json verify --enrollment-id REAL_ID
python3 -m assistant_mesh.phone_owner --config /PRIVATE/operator.json register --enrollment-id REAL_ID
python3 -m assistant_mesh.phone_owner --config /PRIVATE/operator.json issue --enrollment-id REAL_ID
```

Registration acknowledgement loss observes the original publication rather than
publishing another one. Repeating issuance checks the existing private export;
it does not rotate a token. An expired, revoked, cancelled or changed binding is
not silently renewed. An unconfirmed CLI response requires status/reconciliation
of the same operation, not a new enrollment ID.

Optional visible-node metadata maps actual node IDs to `display_name` and an
explicit capability subset observed in the original node registry. The phone
does not receive unrelated capability names or other projects' task counts.
Discovery does not grant execution permission.

## Actual device acceptance and recovery

After enrollment, issue a second independent business request through the
original Registry/Allocation path and read its complete result. Also exercise a
user-selected non-sensitive file, a signed data update, unplugging USB and
reconnecting while wireless HDC is closed. WorkScheduler registration and a
real background callback are separate evidence. No background callback means
background delivery remains pending acceptance, not a permanent worker.

Keep old same-signature HAP and data versions. Data activation remains atomic;
do not change native permissions through data updates. A receiver rollback must
retain the current ledger, enrollment journal and original unknown operations.
It must not restore an old database or reissue old challenges to replay work.

The initial implementation was written in the actual network task's worktree.
Its native turn stopped on a usage limit; Root retained the original task,
thread and unknown checkpoint, reviewed the code, ran the focused checks and
prepared this deployment handoff. Source delivery is not a claim that that
native task's unresolved execution record has been settled.
