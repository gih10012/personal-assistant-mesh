# First business replication chain

This release provides a transactional SQLite change journal, authenticated
bounded export and a separate durable passive replica. It does **not** provide
execution failover, full device redundancy or HA. The manifest always reports
`redundancy=degraded` and `execution_authorized=false`.

Deploy only these new modules in an additive versioned `assistant_mesh`
namespace, retaining the actual incumbent namespace/fallback paths:

- `assistant_mesh/business_journal.py`
- `assistant_mesh/business_replica_api.py`
- `assistant_mesh/business_replica.py`

Do not replace Store, Worker, server, receiver, media or native-history files.
Root must compose the existing ready chain and credentials; this release does
not install/restart a service or read production configuration.

## Source installation order

1. Arrange a controlled window for **every process writing this source DB**.
   First give each existing Store instance the connection hook, without yet
   installing capture:

   ```python
   from assistant_mesh.business_journal import hook_store
   hook_store(store)
   ```

   Before constructing a Store on later restarts, install the same hook on its
   original base type (and any independent transaction implementation):

   ```python
   from assistant_mesh.business_journal import hook_store_type
   hook_store_type(ActualIncumbentStore)
   ```

   This keeps persisted triggers compatible with the original constructor's
   session-copy/migration writes; installing an instance hook only at ready is
   too late on restart. It retains the original constructor/business logic and enables
   `PRAGMA recursive_triggers=ON` on each connection. It is necessary to capture
   secondary-UNIQUE `INSERT OR REPLACE` deletions. Once capture is installed,
   writes to covered rows from unhooked connections fail explicitly rather
   than silently losing changes. SQLite >= 3.16 is required for the pragma
   guard. Do not activate triggers while old unhooked writers remain.

2. Supply an explicit private policy. Core tables below are always included;
   add every actual business extension table, dependency/receipt table and
   metadata key required by that authority. Every excluded table needs a
   reviewed reason. Example structure, **not a production coverage policy**:

   ```json
   {
     "authority": "ACTUAL_SOURCE_AUTHORITY_ID",
     "tables": ["ACTUAL_BUSINESS_EXTENSION_TABLE"],
     "meta_keys": ["AUDITED_BUSINESS_META_KEY"],
     "excluded": {"ACTUAL_NONBUSINESS_TABLE": "Reviewed explicit exclusion reason"}
   }
   ```

   Mandatory core: tasks, task_continuations, interactions, steering,
   agent_actions, sessions, native_sessions, session_chunks and memories.
   All fields and declared primary keys are retained, including full raw JSON,
   parent IDs/dependencies, unknown/revocation state, receipts and BLOB bytes.
   Extensions require explicit PKs; NULL PK values cannot be exported safely.
   `meta` exports only the configured keys. Account/token/credential/auth keys,
   `owner_context` and `cursor` are refused; no login files are read.

3. In the original API ready chain, before exposing writers/listeners, attach:

   ```python
   from assistant_mesh.config import private_json
   from assistant_mesh.business_replica_api import attach
   journal = attach(api, private_json("/PRIVATE/business-replication-policy.json"))
   previous_ready(http_server, api, channel)
   ```

   Installation commits journal identity/schema/triggers in one existing Store
   transaction. It does not copy baseline rows into the source journal.
   Business writes, typed change cells and delete tombstones commit or roll
   back together. It uses neither executescript nor polling snapshots.

## Authorization and transport

Give the receiving peer an independent narrowly scoped credential through the
existing server peer configuration: role `replica_reader`, its own `node`, and
capability `business.replica.read`. Do not copy a personal account credential
or grant an operator/worker execution role merely for replication. Operators
may also read; only operators may refresh or suspend. The dedicated reader role
is rejected by this wrapper on every unrelated route, before incumbent
discovery/A2A/worker handling. Existing explicitly granted worker/agent_peer
readers remain compatible, but deployment should use the narrower role.

All routes are private authenticated POSTs, with no query/fragment:

| Route | JSON body |
| --- | --- |
| `/v1/business-replica/status` | `{}` |
| `/v1/business-replica/snapshot` | `{"action":"begin"}` |
| same | `{"action":"page","snapshot_id":"…","after":0,"limit":100}` |
| same | `{"action":"end","snapshot_id":"…"}` |
| `/v1/business-replica/export` | `{"after":123,"incarnation":"…","generation":1,"limit":100}` |
| `/v1/business-replica/refresh` | `{}`; operator only |
| `/v1/business-replica/suspend` | `{}`; operator only |

Snapshots fix a manifest and journal cursor in one consistent SQLite read
transaction. Pages stream original table rows, retaining at most a page and
one pending row in memory. There is no second full baseline file/log on the
source. The reader pins WAL until completion/close/expiry: allow space for
concurrent writes during this bounded window. Default absolute lifetime is
300 seconds, maximum two sessions. A lost process/expired snapshot requires a
new bootstrap. Pages default to <=6 MiB and <=100 rows; a single row exceeding
the budget fails explicitly, with no truncation. The fixture covers a 1 MiB
BLOB; actual chunk/extension row sizes must fit this budget. Larger rows need
a later fragmentation contract. The source journal is retained without automatic pruning, so plan
space for future changes; this version never drops unacknowledged changes.

## Passive local receiver

Use a **new independent DB path**, never the running authority DB or a restored
copy of it. Existing non-replica databases are refused before modification.
With the additive namespace first on PYTHONPATH:

```sh
python3 -m assistant_mesh.business_replica \
  --database /PRIVATE/business-replica.sqlite3 \
  --authority ACTUAL_SOURCE_AUTHORITY_ID \
  --client /PRIVATE/read-only-replication-client.json --pages 1000
```

The private client JSON uses the existing Client contract (`control_url` and
`token_file`); only HTTPS or a verified loopback tunnel is allowed. It performs
no model or task/claim request. Set the page budget large enough to complete
the baseline within its read window. If exhausted, staging remains unpublished;
the next CLI run starts a new consistent snapshot, not a partial restore.

Each page is committed durably. A complete baseline atomically publishes the
staging view; prior published rows remain available until then. Incremental
events require ordered source/incarnation/generation/cursors and exact replay
receipts. Conflicts retain the incoming page separately and roll back apply.
Delete tombstones and newer unknown/revocation state cannot be overwritten by
old frames. A new incarnation requires a separate replica DB and reconciliation;
it never silently replaces an old incarnation. Do not restore an old source DB
under its prior incarnation or prune/rewrite the source journal.

Local JSON query (BLOBs use explicit typed base64 cells):

```sh
python3 -m assistant_mesh.business_replica \
  --database /PRIVATE/business-replica.sqlite3 \
  --authority ACTUAL_SOURCE_AUTHORITY_ID --incarnation SOURCE_INCARNATION --table tasks
```

Python `Replica.query()` returns original bytes and includes source cursor,
observed head, coverage and non-execution status. An intermediate cursor can
be a prefix of a source transaction; only catch-up to the observed head closes
that prefix. Catch-up does not attest the live source's current head or HA.

For a Linux host's model-independent background adapter, freeze
`business_replica_service.py` with the replica modules and the incumbent Client
dependencies, then use the adjacent service example. It keeps the seed index
open instead of rescanning the whole historical snapshot every interval. A
private writer lock prevents two service instances writing one target; each
cycle reloads the existing scoped client credential. Success/failure receipts
are private atomic files, and a failed pull retains the last published view.
Normal shutdown interrupts between requests but still ends the original source
snapshot. No model, task claim, source role mutation or automatic failover is
performed. Other host platforms should use their own supervisor adapter over
the same replica protocol, not depend on systemd or this Linux writer lock.

## Coverage, refresh and rollback

The manifest includes every covered column/type/default/PK and explicit
exclusions. Missing core/configured tables, new undeclared tables, schema drift,
missing triggers or changed desired policy report degraded coverage. New
columns do not block ordinary hooked business writes. After reviewing new
schema/extension policy, attach the desired configuration and explicitly call
operator `refresh`: one transaction updates triggers/manifest/generation and
forces a new full baseline. Old-generation pages cannot overwrite it.

For a controlled rollback to an unhooked old writer, operator `suspend` removes
only capture triggers, retains the journal and makes coverage degraded. Resume
capture through explicit refresh plus a new baseline; never restore an old DB.

Workspace/external native-history files, their complete manifests, OAuth state
and writer authority, channel state/receipts and a fenced communication exit
remain outstanding dependencies. Row replication does not make native sessions
resumable, transfer a role/lease, clear guards or authorize a second execution.

Validation: Root ran Click Evidence `s1-chain`, exactly
`python3 -m unittest tests.test_business_replication.BusinessReplicationTests`:
four targeted cases passed in 0.269 seconds, without reuse. Production
installation, actual coverage policy and real source/replica acceptance remain
for Root's controlled integration window.
