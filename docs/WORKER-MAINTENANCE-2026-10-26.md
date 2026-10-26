# Private worker admission maintenance

`assistant_mesh.worker_maintenance` adds one private operator route to the
existing receiver. It does not construct an API/Store/worker, migrate a schema,
initialize a model/account, start another receiver or change a business task,
native session, operation effect guard or unknown result. Control records use
only namespaced keys in the original Store's existing `meta` table.

Root installs the module beside the actual pinned receiver package and composes
its existing LAST ready hook before requests are accepted:

```python
from assistant_mesh.worker_maintenance import attach as attach_maintenance

def ready(http_server, api, channel):
    original_composed_ready(http_server, api, channel)
    attach_maintenance(api)
```

Keep every original ingress/final/feed/native Goal/close hook, original Store,
peer/token bindings and source pins. `attach` performs no DB IO, constructor or
recovery. Reattaching the same API is idempotent; changing its Store or finite
configured worker set requires the original ready/deployment path. The module
uses Python 3.6-compatible syntax for the existing frozen receiver. No rolling,
resource-limit, native framework, provider or worker changes are included here.

The route is exactly `POST /v1/worker-maintenance`, with no query, fragment or
absolute-URL alias. It is available only through the existing authenticated
`operator` peer. `node` must be an existing `role=worker` node from `api.peers`;
caller-supplied identities cannot register another node or enlarge permissions.
Do not publish this private route through public MCP/ingress.

Use the existing private operator Client/config; never print its bearer. Exact
payloads, with locally retained IDs, are:

```python
from assistant_mesh.config import private_json
from assistant_mesh.worker import Client

client = Client(private_json('/absolute/private/operator-client.json'))
route = '/v1/worker-maintenance'
node = 'EXACT_EXISTING_WORKER_NODE_FROM_PRIVATE_PEERS'
operation = 'cloud-worker-enrollment-20261026'

acquire = {'action': 'acquire', 'node': node, 'operation_id': operation,
           'control_id': operation + '.acquire', 'ttl': 120}
result = client.request(route, acquire)

status = client.request(route, {'action': 'status', 'node': node,
    'operation_id': operation, 'control_id': operation + '.acquire'})

renew = {'action': 'renew', 'node': node, 'operation_id': operation,
         'control_id': operation + '.renew.001', 'ttl': 120}
result = client.request(route, renew)

release = {'action': 'release', 'node': node, 'operation_id': operation,
           'control_id': operation + '.release'}
result = client.request(route, release)
```

`ttl` is an integer, defaults to 120 seconds and is bounded to 15..600. Each
explicit renewal uses its own retained control ID; the original operation stays
the same. Status may omit both IDs for the current node lease. Supplying IDs
also reads the original control receipt/operation without changing anything.

`lease` describes the current node holder, `operation` describes the requested
original operation, and `claim_blocked` reports whether the lease is live now.
States are `none`, `live`, `expired` and `released`; `receipt` is historical and
does not claim the lease is still live. Replaying the same control ID/body returns
that same receipt plus fresh lease status. Changed action/node/operation/TTL for
an existing control ID conflicts. Reusing an operation with another node or
initial acquire TTL also conflicts. Acquire replay never extends TTL, and a
released/expired operation never re-acquires, even with a new acquire control ID.
Expired renewals are rejected: acquire a NEW operation and inspect idle again.
Release of an old holder cannot clear another operation's lease. Old acquire,
renew and release receipts remain readable after a new holder takes over.

For initial enrollment, acquire before inspecting/stopping the unmanaged worker.
Acquisition waits for every earlier target-node `/v1/claim` dispatch to finish
its commit; every later target-node claim returns exactly `{'task': None}`.
The per-node RLock spans only the existing short claim dispatch or short local
control transaction. It is released after acquire, while the persisted lease
continues blocking claims. No lock spans model/network/worker execution, and
status, heartbeat, current task updates and other nodes retain their original
dispatch. Direct Store calls and other claim protocols are outside this narrow
legacy `/v1/claim` enrollment fence.

With the lease confirmed live, Root inspects the existing worker/native process
and authority state using their existing interfaces. A live maintenance lease
does not prove idle, pause business work or settle unknown effects. Retain any
unsafe in-flight execution. If the old worker is confirmed safely idle, Root
stops its old unit, activates the already-ready managed candidate, then releases
the original maintenance operation. The candidate's claims for this same node
are blocked until release too. Renew explicitly while waiting; if the lease
expires, original claim behavior resumes and the safe-idle inspection must be
done again under a newly acquired operation.

After receiver restart, attaching to the same original authority reinstates any
persisted live lease on the first claim. There is no restore, startup recovery,
automatic extension or task replay. Expiry alone restores original behavior.

Focused evidence: `tests.test_worker_maintenance.WorkerMaintenanceTests`, four
passing Click tests against the exact frozen Store fixture. They cover concurrent
claim/acquire ordering, unaffected routes/nodes, operator/route/finite-node auth,
changed control content, TTL/renewal/expiry, released-operation tombstones and
receiver-ready restart persistence without new tables or task changes. This
branch performs no production installation or acceptance; Root owns those steps.
