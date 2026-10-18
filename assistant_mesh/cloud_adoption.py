"""Owner-initiated Cloud enrollment, separate from Mesh's launch ledger.

No CLI, account home, provider submission or credential creation exists here.
An already running owner task can join with the existing enrollment/membership
wire contracts. A scoped Mesh credential authenticates that delegated entrance,
not its physical provider. Only an installed independent verifier can add an
official job observation; JSON or a claimed URL cannot promote provenance.
"""
import contextlib
import json
import sqlite3
import time

from .cloud_jobs import PROTOCOL, _digest, _encoded, _private_dir, _private_file, _text
from .cloud_ui import _ID, _observation, _task_url
from .store import Conflict


class CloudAdoptions:
    """The small journal interface consumed by CloudMembership, never submit().

    mesh_task_id/epoch refer to the Mesh enrollment work established when the
    owner grants this entrance. They need not precede the owner's Cloud task.
    Business calls still require CloudMembership's live authority validator.
    """
    def __init__(self, state_dir, owner_binding, *, provider_verifier=None,
                 clock=time.time, max_bytes=4 * 1024 * 1024):
        self.owner = _text(owner_binding, 'owner_binding')
        if provider_verifier is not None and not callable(provider_verifier):
            raise ValueError('cloud_adoption_verifier_required')
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 16 * 1024 * 1024:
            raise ValueError('invalid_cloud_configuration')
        self.clock, self.max_bytes, self.verifier = clock, max_bytes, provider_verifier
        self.path = _private_dir(state_dir, create=True)
        self.database = self.path / 'cloud-adoptions.sqlite'
        _private_file(self.database, create=True)
        with self._transaction(initialize=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cloud_adoption_metadata(owner TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS cloud_adoptions(
                id TEXT PRIMARY KEY,body TEXT NOT NULL,fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,attempted INTEGER NOT NULL DEFAULT 0,
                receipt TEXT,receipt_sha256 TEXT,provider_id TEXT UNIQUE)''')
            owners = [row[0] for row in db.execute('SELECT owner FROM cloud_adoption_metadata')]
            if not owners and not db.execute('SELECT 1 FROM cloud_adoptions LIMIT 1').fetchone():
                db.execute('INSERT INTO cloud_adoption_metadata VALUES(?)', (_digest(self.owner),))
            elif owners != [_digest(self.owner)]:
                raise Conflict('cloud_adoption_owner_mismatch')

    @contextlib.contextmanager
    def _transaction(self, initialize=False):
        _private_dir(self.path)
        _private_file(self.database)
        # Missing durable state must not be recreated on a read/reconnect.
        db = sqlite3.connect(self.database.as_uri() + '?mode=rw', uri=True,
                             isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            if not initialize and [row[0] for row in db.execute(
                    'SELECT owner FROM cloud_adoption_metadata')] != [_digest(self.owner)]:
                raise Conflict('cloud_adoption_owner_mismatch')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def planned_id(self, mesh_task_id, request_id):
        return 'cloud-adopt-' + _digest([self.owner, _text(mesh_task_id, 'mesh_task_id'),
                                        _text(request_id, 'request_id')])

    @staticmethod
    def _binding(value):
        if not isinstance(value, dict) or set(value) != {'mesh_task_id', 'mesh_epoch', 'request_id'}:
            raise ValueError('invalid_cloud_adoption_binding')
        _text(value['mesh_task_id'], 'mesh_task_id')
        _text(value['request_id'], 'request_id')
        if type(value['mesh_epoch']) is not int or not 1 <= value['mesh_epoch'] < (1 << 63):
            raise ValueError('invalid_cloud_mesh_epoch')
        return json.loads(_encoded(value))

    def _row(self, db, job_id):
        row = db.execute('SELECT * FROM cloud_adoptions WHERE id=?',
                         (_text(job_id, 'job_id'),)).fetchone()
        if row is None:
            raise PermissionError('cloud_adoption_not_found')
        try:
            body = self._binding(json.loads(row['body']))
            valid = (row['id'] == self.planned_id(body['mesh_task_id'], body['request_id'])
                     and row['body'] == _encoded(body) and row['fingerprint'] == _digest(body)
                     and row['state'] == 'adopted' and row['attempted'] == 0)
            if row['receipt'] is not None:
                receipt = self._receipt(json.loads(row['receipt']))
                valid = valid and (row['receipt'] == _encoded(receipt)
                    and row['receipt_sha256'] == _digest(receipt)
                    and row['provider_id'] == receipt['provider_task_id'])
            else:
                valid = valid and row['receipt_sha256'] is None and row['provider_id'] is None
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise Conflict('cloud_adoption_journal_invalid')
        return row, body

    @staticmethod
    def _view(row):
        body = json.loads(row['body'])
        receipt = json.loads(row['receipt']) if row['receipt'] else None
        return dict(body, format=PROTOCOL, job_id=row['id'], enrollment_origin='owner_initiated',
            adoption_state='adopted', submission_state='not_submitted_by_mesh',
            submission_attempted=False, cli_attempted=False, submission_channel=None, can_submit=False,
            provider_task_id=receipt['provider_task_id'] if receipt else None,
            provider_url=receipt['provider_url'] if receipt else None,
            environment_id=receipt['environment_id'] if receipt else None,
            provider_identity_verified=receipt is not None,
            provenance='owner_observed_official_cloud' if receipt else 'owner_authorized_unattested_entrance',
            execution_verified=False, native_tools_intercepted=False)

    def adopt(self, binding, *, authorized=False):
        """Record Mesh enrollment binding AFTER an owner's existing Cloud launch."""
        if authorized is not True:
            raise PermissionError('cloud_adoption_owner_required')
        body = self._binding(binding)
        job_id = self.planned_id(body['mesh_task_id'], body['request_id'])
        with self._transaction() as db:
            old = db.execute('SELECT * FROM cloud_adoptions WHERE id=?', (job_id,)).fetchone()
            if old is None:
                db.execute('INSERT INTO cloud_adoptions VALUES(?,?,?,\'adopted\',0,NULL,NULL,NULL)',
                           (job_id, _encoded(body), _digest(body)))
            row, saved = self._row(db, job_id)
            if saved != body:
                raise Conflict('cloud_adoption_binding_conflict')
            return self._view(row)

    def get(self, job_id):
        with self._transaction() as db:
            return self._view(self._row(db, job_id)[0])

    @staticmethod
    def _receipt(receipt):
        if not isinstance(receipt, dict) or set(receipt) != {
                'provider_task_id', 'provider_url', 'environment_id', 'observation'}:
            raise ValueError('invalid_cloud_adoption_observation')
        provider = receipt['provider_task_id']
        if (not isinstance(provider, str) or not _ID.fullmatch(provider)
                or provider.lower().startswith(('client-new-thread', 'client_new_thread', 'setup'))):
            raise ValueError('invalid_cloud_adoption_provider_id')
        _task_url(receipt['provider_url'], provider)
        _text(receipt['environment_id'], 'environment_id')
        _observation(receipt['observation'])
        return json.loads(_encoded(receipt))

    def record_provider_observation(self, job_id, receipt, *, authorized=False):
        """Private normal-UI observation, not a provider receipt wire schema.

        The verifier must inspect the already saved official task, owner and
        published environment, independently of the client's URL/claims. It
        cannot infer physical execution from the scoped Mesh bearer or OAuth.
        """
        if authorized is not True or self.verifier is None:
            raise PermissionError('cloud_adoption_verifier_required')
        receipt = self._receipt(receipt)
        if receipt['observation']['observed_at'] > self.clock():
            raise ValueError('cloud_adoption_future_observation')
        with self._transaction() as db:
            row, binding = self._row(db, job_id)
            if row['receipt'] is not None:
                if row['receipt'] != _encoded(receipt):
                    raise Conflict('cloud_adoption_observation_conflict')
                return self._view(row)
        try:
            verified = self.verifier(dict(binding, job_id=job_id, owner_binding=self.owner),
                                     json.loads(_encoded(receipt)))
        except Exception:
            raise PermissionError('cloud_adoption_observation_unverified') from None
        if verified is not True:
            raise PermissionError('cloud_adoption_observation_unverified')
        with self._transaction() as db:
            row, current = self._row(db, job_id)
            if current != binding or (row['receipt'] is not None and row['receipt'] != _encoded(receipt)):
                raise Conflict('cloud_adoption_observation_conflict')
            if db.execute('SELECT 1 FROM cloud_adoptions WHERE provider_id=? AND id!=?',
                          (receipt['provider_task_id'], job_id)).fetchone():
                raise Conflict('cloud_adoption_provider_already_bound')
            db.execute('UPDATE cloud_adoptions SET receipt=?,receipt_sha256=?,provider_id=? WHERE id=?',
                       (_encoded(receipt), _digest(receipt), receipt['provider_task_id'], job_id))
            return self._view(self._row(db, job_id)[0])
