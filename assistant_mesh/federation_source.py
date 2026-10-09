"""Bounded, read-only exports of one initialized capability authority.

This is a latest-state incremental snapshot, not an immutable historical event
feed, permission transfer, consensus protocol or native-tool interceptor.
Issuer identity MUST come from deployment configuration/authenticated routing.
The caller must authenticate the recipient and choose which peers may see this
catalogue; this module performs no network, model, credential or service action.

Revision is the last advertise/renew/revoke audit sequence for an instance, not
its capability epoch. Non-exported audit events legitimately create gaps.
The page and source head are read in one Store transaction. Rows absent from a
page say nothing about deletion; explicit revoked rows are tombstones. A cursor
ahead of the current head is rejected, but restoring an older database with a
still-high-enough head is not detectable without a separate incarnation scheme.
"""
import json

from .resources import Registry, _finite, _name, _safe_metadata
from .store import Conflict


FORMAT = 'mesh-capability-export/1'
MAX_RECORDS = 1000
MAX_RECORD_BYTES = 65536
MAX_PAGE_BYTES = 8 * 1024 * 1024


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf8')


class FederationSource:
    """Read an existing Registry; never initialize or mutate its core tables."""

    def __init__(self, registry, issuer):
        if not isinstance(registry, Registry):
            raise ValueError('initialized_capability_registry_required')
        self.registry = registry
        self.store = registry.store
        self.issuer = _name(issuer, 'federation_issuer')

    @staticmethod
    def _record(row, exported_at):
        # Keep public columns explicit: future credential/config columns must
        # not become an export merely because capabilities gains a new column.
        capability = {key: row[key] for key in (
            'id', 'principal', 'kind', 'description', 'epoch', 'health',
            'deadline', 'created', 'updated')}
        spec = json.loads(row['spec'])
        if not isinstance(spec, dict):
            raise ValueError('invalid_source_capability_spec')
        spec, newly_redacted = _safe_metadata(spec)
        capability['spec'] = spec
        capability['secret_fields_redacted'] = bool(row['redacted']) or newly_redacted
        capability['revoked'] = bool(row['revoked'])
        # Same shape as Registry._view, frozen to the ONE export clock sample.
        # These two derived fields are not same-revision content fingerprints.
        capability['available'] = (not capability['revoked']
                                   and capability['deadline'] > exported_at
                                   and capability['health'] not in ('unavailable', 'failed'))
        capability['lease_expired'] = capability['deadline'] <= exported_at
        capability['verification'] = 'declared'
        capability['health_verification'] = 'declared'
        return {'id': row['id'], 'revision': row['revision'], 'capability': capability}

    def export(self, after=0, limit=100):
        """Return a bounded current-state page, with no implicit backfill/reset.

        Complete pages advance to the source audit head (including irrelevant
        events); partial pages advance only to their final exported revision.
        Exported deadlines use the issuer clock. ``exported_at`` lets a receiver
        estimate freshness, not prove a live lease or authorize execution.
        Free-form descriptions are declared catalogue text, not guaranteed DLP.
        """
        if (isinstance(after, bool) or not isinstance(after, int)
                or not 0 <= after <= 9223372036854775807):
            raise ValueError('invalid_federation_cursor')
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= MAX_RECORDS):
            raise ValueError('invalid_federation_limit')
        with self.store.transaction() as db:
            exported_at = _finite(self.store.clock(), 'federation_exported_at')
            if exported_at < 0:
                raise ValueError('invalid_federation_exported_at')
            head = db.execute('SELECT COALESCE(MAX(sequence),0) FROM capability_audit').fetchone()[0]
            if after > head:
                raise Conflict('federation_cursor_ahead_of_source')
            # Do not silently declare a complete catalogue when a damaged or
            # legacy source row has no usable mutation sequence at all.
            missing = db.execute('''SELECT 1 FROM capabilities c WHERE NOT EXISTS (
                SELECT 1 FROM capability_audit a WHERE a.capability_id=c.id
                AND a.operation IN ('advertise','renew','revoke')) LIMIT 1''').fetchone()
            if missing:
                raise Conflict('source_capability_revision_missing')
            rows = db.execute('''SELECT c.id,c.principal,c.kind,c.description,
                c.spec,c.redacted,c.epoch,c.health,c.deadline,c.revoked,c.created,
                c.updated,latest.revision FROM capabilities c JOIN (
                    SELECT capability_id,MAX(sequence) AS revision
                    FROM capability_audit
                    WHERE operation IN ('advertise','renew','revoke')
                    GROUP BY capability_id
                ) latest ON latest.capability_id=c.id
                WHERE latest.revision>? ORDER BY latest.revision,c.id LIMIT ?''',
                              (after, limit + 1)).fetchall()
            page = {'format': FORMAT, 'issuer': self.issuer, 'after': after,
                    'source_sequence': head, 'next_cursor': head, 'complete': False,
                    'exported_at': exported_at, 'records': []}
            # The head is at least every row revision, so this reserves the
            # largest cursor width. False also reserves more bytes than True.
            page_bytes = len(_encoded(page))
            for row in rows[:limit]:
                record = self._record(row, exported_at)
                record_bytes = len(_encoded(record))
                if record_bytes > MAX_RECORD_BYTES:
                    raise ValueError('federation_record_too_large')
                addition = record_bytes + bool(page['records'])
                if page_bytes + addition > MAX_PAGE_BYTES:
                    if not page['records']:
                        raise ValueError('federation_page_too_small')
                    break
                page['records'].append(record)
                page_bytes += addition
            page['complete'] = len(page['records']) == len(rows)
            page['next_cursor'] = (head if page['complete']
                                   else page['records'][-1]['revision'])
            return page
