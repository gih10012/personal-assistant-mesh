# Snapshot expiry and acknowledged journal retention

This additive lifecycle update releases expired snapshot readers without
another RPC, and permits bounded deletion of acknowledged **journal** rows.
It does not delete source business rows, change unknown/receipt fields,
VACUUM/restore a database, elect an executor or provide HA. Coverage manifest,
incarnation and generation are unchanged by installation/configuration/ACK.

## Absolute snapshot expiry

Each admitted snapshot has one daemon Timer, bounded by the existing maximum
of two snapshots. Its deadline is fixed at creation; requests never renew it.
Timer expiry and explicit `end` both cancel the timer, close the iterator and
SQLite reader, and release pending rows, last-page data and raw BLOB cache.
An early timer wake reschedules only the remainder of the original deadline.
No model, incoming request, background polling loop or new service is needed.

Pruning on the same Journal instance retains the minimum baseline cursor of
its still-admitted snapshots, including a final page awaiting BLOB fetch/end.
Other processes can share the source DB. Before every page or BLOB fetch the
Journal reads the durable retention floor outside its pinned reader. If an
independent instance has crossed that snapshot's bridge cursor, it explicitly
closes the snapshot and requires rebootstrap. Export also rejects every cursor
below the floor. A stale snapshot cannot silently lose its incremental bridge.

## Private retention configuration

New internal tables `_business_replication_retention` and
`_business_replication_acks` are excluded from business coverage by the existing
internal prefix. Installation defaults to no required peers, keep 1000 events,
batch 200, floor 0. No ACK means no pruning; an empty required-peer list disables
pruning. Only an authenticated operator may configure it:

```json
POST /v1/business-replica/configure-retention
{"required_peers":["ACTUAL_REPLICA_NODE"],"keep_events":1000,"batch":200}
```

The required peers are explicit private node IDs, not roles inferred from
network activity. Keep must be at least one event, batch 1–2000 events. Omitting
keep/batch uses 1000/200. Configuration does not refresh coverage or activate a
new generation, and does not itself prune. Root selects the actual private
configuration after deployment under its writer-maintenance contract.

## Authenticated durable progress assertions

Status/export advertise `ack_protocol=amita-business-replica-ack/1` and the
persisted `retained_after` floor. Only a scoped authenticated `replica_reader`
may POST `/v1/business-replica/ack`, and only for its credential's own node.
There is no caller-selected peer field. Operators, ordinary workers and agent
peers cannot use this mutation, and a replica reader cannot configure retention
or call business/worker routes.

```json
{
  "authority":"ACTUAL_SOURCE",
  "incarnation":"ACTUAL_INCARNATION",
  "generation":1,
  "cursor":12345,
  "manifest_sha256":"ACTUAL_MANIFEST_SHA256",
  "receipt_id":"SHA256_OF_THE_OTHER_FIVE_CANONICAL_JSON_FIELDS"
}
```

Canonical JSON is the existing `business_journal.canonical`: sorted keys,
ASCII escaping, compact separators and no NaN. The receipt ID is the existing
`digest` over those five fields. Cursor must belong to the current source,
incarnation, generation and manifest, be no older than the retained floor or
generation start, never exceed head, and never regress that peer's watermark.
Unacknowledged/stale-generation/wrong-manifest required peers block pruning.
ACK is accurately reported as an `authenticated_receiver_assertion`, with
`remote_disk_independently_verified=false`; it cannot attest a remote disk or
authorize execution/redundancy by itself.

Each accepted ACK performs at most one configured batch in the same short
transaction: update its durable progress, find the minimum current required-peer
watermark, retain at least the configured most recent events and active local
snapshot bridges, then delete eligible old journal cells/events and advance the
floor. Repeating an ACK preserves its ID and watermark and may drain another
bounded batch. Missing ACKs never cause deletion. Head uses the committed SQLite
AUTOINCREMENT high water and stays monotonic as old events are removed. Export
checks contiguity as well as the floor; missing events require rebootstrap.

Deleted journal pages become available for SQLite reuse; this does not shrink
the database file or perform VACUUM. A bounded delete can temporarily add WAL
traffic until the existing SQLite checkpoint lifecycle completes. No business
table or original source primary row is deleted by this mechanism.

## Passive service receipts and compatibility

`business_replica_service.py` keeps its existing CLI. A normal sync first commits
Replica changes, then atomically writes/fsyncs its private `last-sync.json`.
Only then does it write/fsync `pending-ack.json` and send that exact ACK. ACK
failure does not change the published view. The next successful sync retries
the saved pending content/ID first; a lost response does not invent another ID.
`last-ack.json` records acknowledged/pending/not-supported status privately.
Only after recording an accepted response does the service remove/fsync the
pending file. This remains progress acknowledgement, never a task claim.

A source without the explicit ACK protocol marker receives no ACK mutation;
normal synchronization continues with `not_supported`. HTTP authorization or
payload errors on a source advertising support remain pending errors, rather
than being mislabeled as unsupported or causing weaker authorization. If an
authenticated new generation/floor forces and completes a fresh baseline, an
obsolete pending ACK is preserved in `last-superseded-ack.json` before staging
progress for that new committed baseline. Ordinary lost-response retries keep
their original ID.

`pull` detects `state.cursor < status.retained_after` and starts a new consistent
baseline. Same-generation manifest and original business identities remain
intact; no SQLite restore, credential copy or guard clearing is involved.

Root should freeze the three updated source business modules plus the updated
passive service/module on the target. No additional module or daemon is needed.
Root owns actual deployment, private configuration, writer coordination and
acceptance. This implementation worker has not opened production or run checks.

Five synthetic fixtures cover timer-only release, missing peer ACKs,
monotonic/bounded pruning and stale cursor rebootstrap, local/cross-instance
snapshot bridges, permission separation and original unknown fields, and
fsynced pending receipts surviving a lost response. Suggested Root Click short
id: `s1maint`; exact argv:

```sh
python3 -m unittest tests.test_business_replica_maintenance.BusinessReplicaMaintenanceTests tests.test_business_blob_cache.BusinessBlobCacheTests tests.test_business_replication.BusinessReplicationTests
```

Absolute workdir:
`/home/gihggiy/Github/personal-assistant-mesh-worktrees/business-replica-maintenance-20261029`.
