# Frozen task transaction facade

`task_transaction_adapter.make_store_class(base, methods_class)` accepts trusted
Python classes. If base already supplies both private atomic task helpers, it
returns base unchanged. Otherwise it returns a subclass reusing the supplied
helper function code, yielded static/class descriptors and final-hook marker.
Only the two helpers' global Conflict binding is changed, using FunctionType
with copied globals so the frozen authority's original exception type remains
catchable. The supplied modern methods class is never modified.

The adapter constructs no Store, opens no database, copies no SQL and loads no
class or source from task JSON. It inherits the original constructor, public
claim/update methods, transaction/election/live-lease checks and media behavior.
Root's original `recover_inflight=False` constructor call, original database
path and instance final/media hooks must remain in place.

Root can compose the class before the existing authority constructor:

```python
from assistant_mesh.task_transaction_adapter import make_store_class

# TaskMethods is the trusted modern helper class supplied by the integration,
# separate from the incumbent frozen module; do not replace that module.
StoreClass = make_store_class(OriginalFrozenStore, TaskMethods)
# Use StoreClass at the existing construction point with its original arguments.
# Keep the same passive constructor and DB; do not construct a second authority.
```

Interactive pull/report code calls `_claim_in_db(db, node, task_ids=...)` and
`_update_task_in_db(db, task_id, node, epoch, ...)` using the same caller-owned
authority transaction as its request receipt. Election stays outside the claim
transaction. Neither helper weakens the existing task/Leader lease or yielded
scope guard. Existing `_conversation_final_in_db` hooks run in that same report
transaction, so a lost receipt commit rolls back task/final/receipt together.

Focused validation uses `tests.test_task_transaction_adapter`, whose fixture is
the exact be2df48e frozen Store source already used by conversation-final tests.
It checks class passthrough, original passive constructor and media state,
filtered atomic claims, atomic report/final rollback, original LegacyConflict
identity and the reused yielded-scope descriptors. No production or server/OAuth
changes are included in this leaf.

The four focused cases passed via Click Evidence (`frozen-task-transactions`),
using `python3 -m unittest tests.test_task_transaction_adapter` with the exact
absolute leaf workdir. No previous model, API or OAuth suites were rerun.
