# Owner-operated SQLite writer seal

This S2 prerequisite retires the current declared SQLite business writer. It
does not activate another writer, change a Raft owner, enable an online switch,
or establish HA. Root owns use against an actual source database; this change
has only a synthetic local SQLite fixture.

Run a read-only inspection without opening `Store`, whose constructor can run
migrations and recover in-flight rows:

```sh
python3 -m assistant_mesh.business_writer_seal inspect --database /private/source.db
```

Prepare one original owner request JSON using the inspected journal identity:

```json
{
  "format": "amita-business-writer-seal-request/1",
  "operation_id": "original-transfer-operation",
  "source_holder": "source-holder",
  "authority": "source-authority",
  "incarnation": "copy-from-inspection",
  "generation": 3,
  "manifest_sha256": "copy-the-64-character-inspected-manifest-hash",
  "meta_keys": ["copy-the-complete-selected-key-list"]
}
```

`meta_keys` must exactly match the current explicit journal selection, sorted
and unique; the seal covers **every** currently declared table. Existing
exclusions remain exclusions. Missing or altered capture triggers, incomplete
coverage, a changed generation/manifest, and unsafe secondary meta uniqueness
are rejected before any seal is committed. Capture SQL is compared with the
same generator used by `Journal.install`, including existing installations.

```sh
python3 -m assistant_mesh.business_writer_seal seal \
  --database /private/source.db --request /private/original-seal-request.json
```

The command opens an existing database with `mode=rw`, requests FULL SQLite
synchronization, and enters `BEGIN IMMEDIATE`. Already admitted transactions
finish before it gets the writer lock. It then validates coverage, installs
persistent `BEFORE INSERT/UPDATE/DELETE` abort triggers, reads the final head,
and commits the original request and immutable receipt under that same lock.
The head is therefore the final committed head of the requested generation,
including writes admitted before the seal. There is no preliminary stop flag
or gap in which another writer can enter. Existing and new raw SQLite
connections are fenced, including `REPLACE` with recursive triggers disabled.
Covered meta keys are protected on both sides of key updates and against
implicit-rowid replacement; unselected keys and explicitly excluded tables
retain their original write scope.

The journal state, events and cells are also frozen so generation refresh and
history pruning cannot change the sealed checkpoint through ordinary DML.
Read/export and acknowledgements remain possible. Existing business/native
rows and unknown operations are retained byte for byte. A sealed source must
be inspected through the read-only CLI: restarting its old `Store` constructor
can correctly fail on its attempted covered writes. No process or native runner
is killed by this seal.

Re-submit only the identical original request after response loss. It returns
the same receipt; another operation or changed request is rejected. There is
no unseal operation. `inspect` uses `mode=ro`, one read transaction and no Store
constructor. A valid inspection returns `source_seal_sha256`, the SHA256 of
the canonical receipt before its `sha256` field is added. The receipt includes
the source holder, authority, incarnation, generation, manifest hash and
`sealed_journal_head`, ready to be referenced by the typed Raft checkpoint.
The typed row digest, two independent holder disk receipts, retained-effects
and external-dependency verification remain separate handoff prerequisites.

The intentional trust limit is a local SQL administrator: this is a persistent
SQL DML boundary. SQLite triggers cannot stop privileged `DROP/ALTER`,
`writable_schema`, internal-table edits, disabling triggers through a privileged
connection API, incremental BLOB/direct-file writes, database replacement, or
a bypass through another database. Inspection checks
the complete manifest, exact capture and seal trigger SQL, original receipt and
current head; missing/tampered evidence yields `valid: false` and no usable
source seal hash. A receipt is a local integrity record, not a signature or
independent attestation against that administrator. Native execution, OAuth,
channel sends and external side effects retain their existing guards; all
results keep `external_effects_fenced`, `execution_authorized` and `ha_qualified`
false.

The single focused fixture is `python3 -m unittest -q
tests.test_business_writer_seal`, submitted through Click Evidence with this
worktree as the absolute `workdir`. It uses temporary files only.
