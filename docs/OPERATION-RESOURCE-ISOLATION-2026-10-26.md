# Independent new work beside an unresolved operation

This optional authority seam lets an owner or an actual live parent declare an
unstarted task independent of specified unresolved work. It does not settle or
retry that work. Ordinary undeclared tasks retain the existing conservative
scope admission. The business task ID, operation, scope, owner memories and
original histories remain intact.

## Original ledger and native resources

`operation_isolation.declare` records a private declaration in the existing
Store database. It binds the exact task/input, original operation, resources and
current blocker fingerprints. A task without an explicit operation retains the
existing task-ID fallback; a declaration cannot assign a replacement operation.
Only pending, epoch-zero, never-attempted work without a checkpoint can receive
a new native execution resource. Repeating a declaration reads its original
receipt. Old attempts cannot be renamed into new resources.

Claim, checkpoint update, native-session commit/download and continuation consume
that declaration. The declared task uses a separate native-session storage key
while retaining its business scope and memories. A changed blocker proof fences
that candidate, not every later candidate. Shared exclusive resources, an
unknown thread and a still-active yielded Goal remain fenced. Completion and
continuation keep the same task/operation/native resource. The old unknown task,
thread, history and effects are not modified.

Resource declarations are trusted scheduling decisions, not independent proof
of arbitrary filesystem/network independence. An owner or qualified parent must
assess the actual work and known effects; legacy metadata is not a complete OS
resource trace. Existing capability/allocation checks still apply. This adds no
global restriction on native Shell, files, network or MCP.

## Authenticated declaration entry

The owner ready chain installs
`operation_isolation_authority.attach(api)` on its ORIGINAL API/Store. The only
new route is exact POST `/v1/operation-isolation/action`. Existing dispatch,
constructor, database, channel/media and final hooks remain composed.

An operator uses its existing private Client:

```python
proof = client.request('/v1/operation-isolation/action', {
    'action': 'inspect', 'task_id': original_unstarted_task_id})
client.request('/v1/operation-isolation/action', {
    'action': 'declare', 'task_id': original_unstarted_task_id,
    'declaration_id': retained_declaration_id,
    'operation_id': original_operation_id,
    'resources': ['file:the-distinct-authorized-output'],
    'independent_of': proof['blockers'],
    'reason': 'Actual provenance and reason this new work is independent'})
```

A worker uses its OWN existing credential and adds `parent_task_id` and `epoch`
for its actual live parent. The server derives the node from the authenticated
peer and verifies the target is that parent's real child. The declaration helper
rechecks the live parent in its transaction. Ingress/viewer/foreign-parent
requests, role/approved JSON flags, changed operations and changed declarations
cannot grant admission. No task or execution is created by this endpoint.

## Frozen composition

Bind the reviewed modern method code to the original frozen Store BEFORE
entrance and optional native-Goal wrappers:

```python
isolated_base = operation_isolation.make_store_class(
    original_frozen_store, trusted_modern_methods.Store)
selected = original_entrance_store_factory(isolated_base, policy, media)
```

Keep the original passive `recover_inflight=False` construction, exact database,
source pins, final hook and owner media fix. Do not replace a live DB or copy
account/native history. The native-Goal candidate generator includes the pinned
isolation modules and uses the same execution key for continuation validation.

Root continued the original specialist worktree after its terminal quota error.
Ten focused cases passed through Click: six task/native-resource cases, three
authenticated endpoint cases and the actual pinned native-Goal candidate build.
They include the exact frozen be2df Store, media/passive construction, original
memory and continuation, transaction/final rollback and unchanged unknown work.
No model request, standing-task declaration or business retry was used by these
checks. Source integration and production acceptance are separate records.
