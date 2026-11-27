"""Activate only the replicated SQLite ledger after a controlled writer transfer."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from .business_checkpoint import _read_snapshot, _replica_snapshot, _sqlite_rows, _typed_witness
from .business_journal import Journal, PREFIX, canonical, digest, hook_store_type, quote
from .business_recovery import _checkpoint, _publish, _validate_destination, inspect_recovery
from .business_writer_grant import confirm_grant
from .business_writer_handoff import _read, assemble_transfer
from .business_writer_seal import STATE as SEAL_STATE, _integer
from .config import private_json
from .operation_isolation import WRITER_HANDOFF_HOLD, native_work_unsettled, writer_handoff_held
from .store import Store


ACTIVATION = PREFIX + 'writer_activation'


def ledger_path(directory, proposal):
    """One canonical file for this role and scope in the target's state directory."""
    return Path(directory).absolute() / ('writer-' + digest([proposal['group'], proposal['scope']]) + '.db')


def _retain_attempts(db, operation_id):
    retained = 0
    rows = db.execute("SELECT id,status,node,epoch,deadline,paused_status,paused_deadline,checkpoint FROM tasks "
                      "WHERE status IN ('running','continuing','needs_review') "
                      "OR (status='paused' AND paused_status IN ('running','continuing','needs_review'))").fetchall()
    for row in rows:
        checkpoint = json.loads(row['checkpoint'])
        if row['status'] in ('paused', 'needs_review') and native_work_unsettled(checkpoint):
            # This existing guard survives recovery already.
            continue
        if not writer_handoff_held(checkpoint):
            checkpoint['writer_handoff'] = dict(format=WRITER_HANDOFF_HOLD, transfer_id=operation_id,
                task_id=row['id'], source_epoch=row['epoch'], source_node=row['node'], source_status=row['status'],
                source_deadline=row['deadline'], source_paused_status=row['paused_status'],
                source_paused_deadline=row['paused_deadline'],
                original_checkpoint_sha256=hashlib.sha256(row['checkpoint'].encode('utf8')).hexdigest())
        # Neither an expired lease nor a copied heartbeat retires the old
        # worker. Remove its local authority and keep the original attempt.
        status = 'paused' if row['status'] == 'paused' else 'needs_review'
        db.execute('UPDATE tasks SET status=?,deadline=NULL,checkpoint=? WHERE id=?',
                   (status, canonical(checkpoint), row['id']))
        retained += 1
    return retained


def _new_ledger(path, candidate, manifest, position, observed, operation_id):
    generation = position.generation + 1
    if not _integer(generation) or not _integer(position.head + 1):
        raise ValueError('business_writer_journal_range_exhausted')
    sha256 = hashlib.sha256()
    with open(candidate, 'rb') as source, path.open('xb') as dest:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            sha256.update(chunk)
            dest.write(chunk)
    if sha256.hexdigest() != observed['candidate_sha256']:
        raise ValueError('business_writer_candidate_changed')
    store = Store(path, recover_inflight=False)
    journal = Journal(store, dict(authority=position.authority, tables=list(manifest['tables']),
                                 meta_keys=manifest['meta_keys'], excluded=manifest['excluded']))
    journal.install()
    with store.transaction() as db:
        state = journal._description(db)
        if state['coverage_issues'] or state['manifest']['sha256'] != position.manifest_sha256:
            raise ValueError('business_writer_target_schema_changed')
        witness = _typed_witness('recovery', position, manifest, lambda table, info: _sqlite_rows(
            db, table, info, journal._filter(table, quote(table), db, manifest['meta_keys'])))
        if witness.typed_rows_sha256 != observed['checkpoint']['typed_rows_sha256']:
            raise ValueError('business_writer_store_start_changed_checkpoint')
        for table, info in manifest['tables'].items():
            for name, sql in journal._trigger_statements(db, table, info, generation):
                # Trigger names are derived from the validated table manifest.
                db.execute('DROP TRIGGER ' + quote(name))
                db.execute(sql)
        db.execute('UPDATE ' + PREFIX + 'state SET incarnation=?,generation=?,start=? WHERE singleton=1',
                   (position.incarnation, generation, position.head))
        db.execute('UPDATE ' + PREFIX + 'retention SET retained_after=? WHERE singleton=1', (position.head,))
        db.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', (PREFIX + 'events', position.head))
        retained = _retain_attempts(db, operation_id)
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if db.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
            raise ValueError('business_writer_staging_wal_not_closed')
    return retained


def activate_ledger(plan, source, target, source_key, target_key, replica, template, candidate,
                    directory, dependencies, retained_effects, voters):
    """Make a fresh local SQLite writer. External-effect dispatch stays disabled."""
    proposal = assemble_transfer(plan, source, target, source_key, target_key)
    committed = proposal['checkpoint']
    if (digest(dependencies) != committed['external_dependencies_manifest_sha256']
            or digest(retained_effects) != committed['retained_effects_sha256']):
        raise ValueError('business_writer_external_evidence_changed')
    path = ledger_path(directory, proposal)
    _validate_destination(path)
    checkpoint = source['body']['evidence']['checkpoint']
    observed = inspect_recovery(replica, template, candidate, checkpoint)
    signed = target['body']['evidence']
    if any(observed[key] != signed[key] for key in ('candidate_sha256', 'schema_sql_sha256')):
        raise ValueError('business_writer_signed_candidate_changed')
    position = _checkpoint(checkpoint)
    with _replica_snapshot(replica, position) as (_, manifest):
        pass
    with tempfile.TemporaryDirectory(prefix='.amita-writer-', dir=path.parent) as stage:
        staged = Path(stage) / 'staged.db'
        retained = _new_ledger(staged, candidate, manifest, position, observed, proposal['operation_id'])
        confirmation = confirm_grant(proposal, voters)
        proof = dict(format='amita-business-writer-activation/1', **confirmation,
                     original_position=checkpoint, scope='replicated_sqlite_ledger',
                     retained_attempts=retained,
                     external_execution_authorized=False, ha_qualified=False)
        with contextlib.closing(sqlite3.connect(staged)) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE ' + ACTIVATION + '(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
                       'proof TEXT NOT NULL)')
            db.execute('INSERT INTO ' + ACTIVATION + ' VALUES(1,?)', (canonical(proof),))
        _publish(staged, path, 0o600)
    return dict(proof, database=str(path))


def inspect_activation(path):
    path = Path(path).absolute()
    with _read_snapshot(path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SEAL_STATE,)).fetchone():
            raise ValueError('business_writer_sealed')
        row = db.execute('SELECT proof FROM ' + ACTIVATION + ' WHERE singleton=1').fetchone()
        if row is None:
            raise ValueError('business_writer_activation_proof_missing')
        return dict(json.loads(row['proof']), database=str(path))


class ActivatedLedger(Store):
    """Reopen this local writer and use the existing SQLite transaction boundary."""

    def __init__(self, path, proof):
        if inspect_activation(path) != proof:
            raise ValueError('business_writer_activation_proof_conflict')
        super().__init__(path, recover_inflight=False)


hook_store_type(ActivatedLedger)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest='action', required=True)
    inspect = subcommands.add_parser('inspect')
    inspect.add_argument('--database', required=True)
    activate = subcommands.add_parser('activate')
    for field in ('plan', 'source', 'target', 'source-public-key', 'target-public-key', 'replica', 'template',
                  'candidate', 'writer-directory', 'dependencies', 'retained-effects', 'voter-config'):
        activate.add_argument('--' + field, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'inspect':
            result = inspect_activation(args.database)
        else:
            result = activate_ledger(_read(args.plan), _read(args.source), _read(args.target), args.source_public_key,
                                     args.target_public_key, args.replica, args.template, args.candidate,
                                     args.writer_directory, _read(args.dependencies), _read(args.retained_effects),
                                     private_json(args.voter_config))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(canonical(dict(error=str(error))))
        return 2
    print(canonical(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
