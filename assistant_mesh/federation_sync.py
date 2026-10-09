"""Optional enrolled-peer catalogue pull, independent of native task execution.

Only GET requests; no resource admission, task ownership transfer, scanning,
grant copying, operation replay or transport installation. Backoff/cursor survive
restart; failures affect this catalogue, not the peer's A2A/native permissions.
"""
import urllib.error
from urllib.parse import urlencode

from .federation_projection import Projection
from .networking import PROTOCOL, identifier
from .resources import _finite
from .store import Conflict


class FederationSync:
    def __init__(self, store, peer_node, issuer, client, interval_seconds=15,
                 page_limit=100, freshness_seconds=30):
        self.store = store
        self.peer, self.issuer = identifier(peer_node), identifier(issuer)
        if not callable(getattr(client, 'request', None)):
            raise ValueError('federation_client_required')
        self.client = client
        self.interval = _finite(interval_seconds, 'federation_interval')
        if not 1 <= self.interval <= 300:
            raise ValueError('invalid_federation_interval')
        if isinstance(page_limit, bool) or not isinstance(page_limit, int) or not 1 <= page_limit <= 1000:
            raise ValueError('invalid_federation_page_limit')
        self.limit = page_limit
        self.projection = Projection(store, freshness_seconds)
        with store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS federation_sync_state(
                peer TEXT NOT NULL,issuer TEXT NOT NULL,failures INTEGER NOT NULL,
                retry_at REAL NOT NULL,code TEXT NOT NULL,observed REAL NOT NULL,
                PRIMARY KEY(peer,issuer))''')

    def status(self):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM federation_sync_state WHERE peer=? AND issuer=?',
                             (self.peer, self.issuer)).fetchone()
            now = self.store.clock()
        return {'peer': self.peer, 'issuer': self.issuer,
                'failures': row['failures'] if row else 0,
                'retry_at': row['retry_at'] if row else 0,
                'diagnostics': [row['code']] if row and row['code'] != 'ok' else [],
                'observed': row['observed'] if row else None,
                'due': row is None or row['retry_at'] <= now,
                'projection': self.projection.status(self.issuer),
                'managed_invocation_authorized': False, 'global_consensus_verified': False,
                'task_or_operation_replayed': False, 'native_tools_intercepted': False}

    @staticmethod
    def _failure(error):
        if isinstance(error, urllib.error.HTTPError):
            return {401: 'catalog_authentication_rejected', 403: 'catalog_authorization_rejected',
                    404: 'catalog_route_missing', 409: 'catalog_source_conflict'}.get(error.code, 'catalog_http_error')
        if isinstance(error, Conflict):
            return 'catalog_projection_conflict'
        if isinstance(error, PermissionError):
            return 'catalog_identity_rejected'
        if isinstance(error, (ValueError, KeyError, TypeError)):
            return 'catalog_contract_invalid'
        return 'catalog_connection_unavailable'

    def step(self, stop=None):
        if stop is not None and stop.is_set():
            return dict(self.status(), state='stopped')
        current = self.status()
        if not current['due']:
            return dict(current, state='waiting')
        try:
            hello = self.client.request('/v1/mesh/hello')
            if (not isinstance(hello, dict) or hello.get('protocol') != PROTOCOL
                    or hello.get('node') != self.peer or hello.get('authority') != self.issuer):
                raise PermissionError('federation_enrolled_identity_mismatch')
            if stop is not None and stop.is_set():
                return dict(self.status(), state='stopped')
            cursor = self.projection.status(self.issuer)['cursor']
            page = self.client.request('/v1/mesh/capability-export?' +
                                       urlencode({'after': cursor, 'limit': self.limit}))
            if stop is not None and stop.is_set():
                return dict(self.status(), state='stopped')
            applied = self.projection.apply(self.issuer, page)
            # This actual, fresh authenticated read may refresh connection
            # evidence, but Projection never extends a replayed capability.
            self.projection.observe_connection(self.issuer, True,
                                               lease_seconds=self.projection.freshness_seconds)
            with self.store.transaction() as db:
                now = self.store.clock()
                db.execute('INSERT OR REPLACE INTO federation_sync_state VALUES(?,?,0,?,?,?)',
                           (self.peer, self.issuer, now + self.interval, 'ok', now))
            return dict(self.status(), state='observed', applied=applied['applied'],
                        complete=page['complete'])
        except Exception as error:
            code = self._failure(error)
            try:
                self.projection.observe_connection(self.issuer, False)
            except ValueError:
                pass  # no accepted catalogue yet; don't invent one
            with self.store.transaction() as db:
                row = db.execute('SELECT failures FROM federation_sync_state WHERE peer=? AND issuer=?',
                                 (self.peer, self.issuer)).fetchone()
                failures = (row['failures'] if row else 0) + 1
                now = self.store.clock()
                db.execute('INSERT OR REPLACE INTO federation_sync_state VALUES(?,?,?,?,?,?)',
                           (self.peer, self.issuer, failures, now + min(300, 2 ** min(failures, 8)), code, now))
            return dict(self.status(), state='degraded')
