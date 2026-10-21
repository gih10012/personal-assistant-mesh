"""Current, owner-bound Events grants over the EXISTING pinned OAuth proxy.

Only opaque grant/JTI/token-HASH references persist here. No bearer, key, OAuth
server or token issuance. The original engine owns every exchange/refresh/revoke.
An authenticated MCP call may adopt a no-refresh grant, never a guessed refresh
association. Async checks run on the original ASGI loop, not a new auth loop.
"""
import asyncio
import contextlib
import hashlib
import json
import sqlite3
import threading
import time

from .cloud_jobs import _private_dir, _private_file
from .conversation import digest, encoded
from .ingress import validate_peer
from .mcp_http_auth import NotConfigured, SOURCE
from .store import Conflict


FORMAT = 'mesh-work-current-oauth-grants/1'


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class CurrentOAuthGrants:
    def __init__(self, directory, policy, *, clock=time.time):
        self.policy, self.clock = policy, clock
        self.root = _private_dir(directory, create=False)
        self.database = self.root / 'oauth-grant-refs.sqlite'
        _private_file(self.database, create=True)
        self.provider = self.loop = self.lock = None
        self.loop_thread = None; self.cache = {}; self.uncertain = False
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY,body TEXT NOT NULL,revoked INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS token_refs(hash TEXT PRIMARY KEY,grant_id TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS subject_blocks(subject TEXT PRIMARY KEY,created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS revocations(sequence INTEGER PRIMARY KEY,grant_id TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS revoke_intents(
                sequence INTEGER PRIMARY KEY,subject TEXT,token_hash TEXT NOT NULL,
                created REAL NOT NULL,state TEXT NOT NULL)''')
            expected = {'format': FORMAT, 'owner': policy['owner_binding'],
                        'issuer': policy['issuer_url'], 'resource': policy['resource_url'], 'source': SOURCE}
            old = dict(db.execute('SELECT key,value FROM metadata'))
            if old != expected:
                if old or db.execute('SELECT 1 FROM grants LIMIT 1').fetchone():
                    raise Conflict('events_oauth_grant_namespace_changed')
                db.executemany('INSERT INTO metadata VALUES(?,?)', expected.items())

    @contextlib.contextmanager
    def transaction(self):
        _private_dir(self.root); _private_file(self.database)
        db = sqlite3.connect(str(self.database), timeout=5); db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE'); yield db; db.commit()
        except BaseException:
            db.rollback(); raise
        finally:
            db.close()

    def _entry(self, principal):
        binding = validate_peer({'role': 'ingress', 'ingress': principal}, self.policy['owner_binding'])
        if binding['source'] != SOURCE or 'tasks:read' not in binding['scopes']:
            raise PermissionError('events_oauth_subject_not_authorized')
        entry = next((item for item in self.policy['subjects'] if item['mesh_subject'] == binding['subject']), None)
        if entry is None or 'tasks:read' not in entry['scopes']:
            raise PermissionError('events_oauth_subject_not_authorized')
        return entry

    def _claims(self, token, use='access'):
        claims = self.provider.jwt_issuer.verify_token(token, expected_token_use=use)
        entry = next((item for item in self.policy['subjects']
                      if item['issuer_subject'] == (claims.get('upstream_claims') or {}).get('owner_subject')
                      and claims.get('client_id') in item['client_ids']), None)
        scopes = claims.get('scope')
        if (entry is None or claims.get('iss') != self.policy['issuer_url']
                or claims.get('aud') != self.policy['resource_url']
                or type(claims.get('exp')) is not int or claims['exp'] <= self.clock()
                or not isinstance(claims.get('jti'), str) or not claims['jti']
                or not isinstance(scopes, str) or 'tasks:read' not in scopes.split()
                or not set(scopes.split()) <= set(entry['scopes'])):
            raise PermissionError('events_oauth_verified_grant_required')
        return claims, entry

    @staticmethod
    def _pending_revoke(db, subject):
        # A missing verified subject is an unresolved owner-namespace revoke,
        # never permission to ignore the operation after a restart.
        return db.execute("SELECT 1 FROM revoke_intents WHERE state='pending' "
                          'AND (subject=? OR subject IS NULL) LIMIT 1', (subject,)).fetchone() is not None

    async def _register(self, access_token, refresh_token=None, *, fresh_code=False):
        claims, entry = self._claims(access_token)
        mapping = await self.provider._jti_mapping_store.get(key=claims['jti'])
        if mapping is None or mapping.jti != claims['jti']:
            return False
        upstream = await self.provider._upstream_token_store.get(key=mapping.upstream_token_id)
        if upstream is None or upstream.client_id != claims['client_id'] or upstream.upstream_token_id != mapping.upstream_token_id:
            return False
        identifier = digest([FORMAT, self.policy['owner_binding'], SOURCE, entry['mesh_subject'],
                             claims['client_id'], upstream.upstream_token_id])
        with self.transaction() as db:
            old = db.execute('SELECT body,revoked FROM grants WHERE id=?', (identifier,)).fetchone()
            blocked = db.execute('SELECT created FROM subject_blocks WHERE subject=?', (entry['mesh_subject'],)).fetchone()
            if self._pending_revoke(db, entry['mesh_subject']):
                return False
        if old and old['revoked']:
            return False  # An old still-valid JWT cannot undo explicit Events revocation.
        if blocked and not (fresh_code and upstream.created_at > blocked[0]):
            return False
        refresh_jti = refresh_hash = None
        if refresh_token is not None:
            refresh, refresh_entry = self._claims(refresh_token, 'refresh')
            refresh_mapping = await self.provider._jti_mapping_store.get(key=refresh['jti'])
            refresh_hash = _hash(refresh_token)
            metadata = await self.provider._refresh_token_store.get(key=refresh_hash)
            if (refresh_entry != entry or refresh['client_id'] != claims['client_id']
                    or refresh_mapping is None or refresh_mapping.jti != refresh['jti']
                    or refresh_mapping.upstream_token_id != upstream.upstream_token_id
                    or metadata is None or metadata.client_id != claims['client_id']):
                return False
            refresh_jti = refresh['jti']
        elif upstream.refresh_token is not None:
            if not old:
                # Hash->client/scopes metadata cannot prove which upstream/JTI
                # owns it. Wait for the next NORMAL successful refresh hook.
                return False
            previous = json.loads(old['body'])
            refresh_jti, refresh_hash = previous['refresh_jti'], previous['refresh_hash']
            if refresh_jti is None or refresh_hash is None:
                return False
        body = {'owner_id': self.policy['owner_binding'], 'source': SOURCE, 'subject': entry['mesh_subject'],
                'issuer_subject': entry['issuer_subject'], 'client_id': claims['client_id'],
                'upstream_id': upstream.upstream_token_id, 'scopes': sorted(set(claims['scope'].split())),
                'refresh_jti': refresh_jti, 'refresh_hash': refresh_hash}
        # Current identity is checked through the real provider, not a record
        # or decoded upstream bearer. It also covers no-refresh GitHub grants.
        identity = await self.provider._token_validator.verify_token(self.provider._get_verification_token(upstream))
        if identity is None or identity.subject != entry['issuer_subject']:
            return False
        with self.transaction() as db:
            # Owner-private revocation can race an awaited identity request.
            blocked = db.execute('SELECT created FROM subject_blocks WHERE subject=?', (entry['mesh_subject'],)).fetchone()
            current = db.execute('SELECT revoked FROM grants WHERE id=?', (identifier,)).fetchone()
            if (self._pending_revoke(db, entry['mesh_subject']) or (current and current[0])
                    or (blocked and not (fresh_code and upstream.created_at > blocked[0]))):
                return False
            if blocked:
                db.execute('DELETE FROM subject_blocks WHERE subject=?', (entry['mesh_subject'],))
            db.execute('INSERT OR REPLACE INTO grants VALUES(?,?,0)', (identifier, encoded(body).decode()))
            for token in (access_token, refresh_token):
                if token is not None:
                    db.execute('INSERT OR IGNORE INTO token_refs VALUES(?,?)', (_hash(token), identifier))
        self.cache.clear()
        return True

    async def observe_access(self, token):
        async with self.lock:
            return await self._register(token)

    async def _associated(self, ref, upstream):
        if (upstream is None or upstream.upstream_token_id != ref['upstream_id']
                or upstream.client_id != ref['client_id'] or 'tasks:read' not in upstream.scope.split()
                or not set(upstream.scope.split()) <= set(ref['scopes'])):
            return False
        if ref['refresh_hash'] is None:
            return upstream.refresh_token is None and upstream.expires_at > self.clock()
        metadata = await self.provider._refresh_token_store.get(key=ref['refresh_hash'])
        mapping = await self.provider._jti_mapping_store.get(key=ref['refresh_jti'])
        return (metadata is not None and metadata.client_id == ref['client_id']
                and 'tasks:read' in metadata.scopes and set(metadata.scopes) <= set(ref['scopes'])
                and (metadata.expires_at is None or metadata.expires_at > self.clock())
                and mapping is not None and mapping.jti == ref['refresh_jti']
                and mapping.upstream_token_id == ref['upstream_id'] and upstream.refresh_token is not None
                and (upstream.refresh_token_expires_at is None or upstream.refresh_token_expires_at > self.clock()))

    async def _current(self, principal):
        if self.uncertain:
            return False
        entry = self._entry(principal)
        async with self.lock:
            with self.transaction() as db:
                if (self._pending_revoke(db, entry['mesh_subject']) or db.execute(
                        'SELECT 1 FROM subject_blocks WHERE subject=?', (entry['mesh_subject'],)).fetchone()):
                    return False
                rows = [(row[0], json.loads(row[1])) for row in db.execute('SELECT id,body FROM grants WHERE revoked=0')]
            for identifier, ref in rows:
                if (ref['subject'] != entry['mesh_subject'] or ref['issuer_subject'] != entry['issuer_subject']
                        or ref['client_id'] not in entry['client_ids'] or not set(ref['scopes']) <= set(entry['scopes'])):
                    continue
                upstream = await self.provider._upstream_token_store.get(key=ref['upstream_id'])
                if not await self._associated(ref, upstream):
                    continue
                fingerprint = (ref['upstream_id'], _hash(upstream.access_token))
                if self.cache.get(fingerprint, 0) > self.clock() and self._unrevoked(identifier, ref['subject']):
                    return True
                identity = await self.provider._token_validator.verify_token(self.provider._get_verification_token(upstream))
                if (identity is None and upstream.refresh_token is not None
                        and upstream.expires_at <= self.clock() + self.provider._token_expiry_threshold_seconds):
                    # Reuse the engine's actual refresh lock and OwnerIdentityProxy
                    # translation/refresh; do not implement another OAuth exchange.
                    async with self.provider._get_refresh_lock(ref['upstream_id']):
                        current = await self.provider._upstream_token_store.get(key=ref['upstream_id'])
                        if not await self._associated(ref, current):
                            continue
                        upstream = await self.provider._try_transparent_refresh(current)
                        identity = await self.provider._token_validator.verify_token(self.provider._get_verification_token(upstream))
                if (identity is not None and identity.subject == entry['issuer_subject']
                        and await self._associated(ref, upstream) and self._unrevoked(identifier, ref['subject'])):
                    self.cache[(ref['upstream_id'], _hash(upstream.access_token))] = self.clock() + 30
                    return True
            return False

    def _unrevoked(self, identifier, subject):
        with self.transaction() as db:
            row = db.execute('SELECT revoked FROM grants WHERE id=?', (identifier,)).fetchone()
            return row is not None and row[0] == 0 and not self._pending_revoke(db, subject) and not db.execute(
                'SELECT 1 FROM subject_blocks WHERE subject=?', (subject,)).fetchone()

    def account_active(self, principal):
        """Sync callback for existing Events worker / SDK to_thread handlers."""
        if self.uncertain or self.loop is None or self.loop.is_closed() or self.loop_thread == threading.get_ident():
            return False  # Never block/run a second loop inside the real app loop.
        future = asyncio.run_coroutine_threadsafe(self._current(principal), self.loop)
        try:
            return future.result(timeout=12) is True
        except Exception:
            future.cancel(); return False

    def revoke_subject(self, principal, *, reason='owner_revoked', reconcile=True):
        """Owner-code-only local revocation; no HTTP/tool route or bearer needed."""
        if reason not in ('owner_revoked', 'oauth_revoke'):
            raise ValueError('events_oauth_revoke_reason_invalid')
        entry = self._entry(principal); count = reconciled = 0
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO subject_blocks VALUES(?,?)', (entry['mesh_subject'], self.clock()))
            for row in db.execute('SELECT id,body FROM grants WHERE revoked=0').fetchall():
                ref = json.loads(row['body'])
                if ref['subject'] == entry['mesh_subject']:
                    db.execute('UPDATE grants SET revoked=1 WHERE id=?', (row['id'],))
                    db.execute('INSERT INTO revocations(grant_id,reason,created) VALUES(?,?,?)',
                               (row['id'], reason, self.clock())); count += 1
            if reconcile:
                # Owner-local reconciliation closes only Events metadata. It
                # NEVER repeats the original provider's possibly applied revoke.
                reconciled = db.execute("UPDATE revoke_intents SET state='locally_blocked' "
                    "WHERE subject=? AND state='pending'", (entry['mesh_subject'],)).rowcount
                subjects = {item['mesh_subject'] for item in self.policy['subjects']}
                blocked = {row[0] for row in db.execute('SELECT subject FROM subject_blocks')}
                if subjects <= blocked:
                    reconciled += db.execute("UPDATE revoke_intents SET state='locally_blocked' "
                                             "WHERE subject IS NULL AND state='pending'").rowcount
        self.cache.clear(); self.uncertain = False
        return {'revoked': count, 'reconciled_pending': reconciled}

    def _token_principal(self, token):
        with self.transaction() as db:
            row = db.execute('SELECT g.body FROM token_refs t JOIN grants g ON g.id=t.grant_id WHERE t.hash=?', (_hash(token),)).fetchone()
        if row:
            ref = json.loads(row[0])
            return {'owner_id': ref['owner_id'], 'source': ref['source'],
                    'subject': ref['subject'], 'scopes': ['tasks:read']}
        for use in ('access', 'refresh'):
            try:
                unused_claims, entry = self._claims(token, use)
            except Exception:
                continue
            return {'owner_id': self.policy['owner_binding'], 'source': SOURCE,
                    'subject': entry['mesh_subject'], 'scopes': ['tasks:read']}
        return None

    def _prepare_revoke(self, token):
        principal = self._token_principal(token)
        subject = self._entry(principal)['mesh_subject'] if principal is not None else None
        with self.transaction() as db:
            row = db.execute("INSERT INTO revoke_intents(subject,token_hash,created,state) VALUES(?,?,?,'pending')",
                             (subject, _hash(token), self.clock()))
            sequence = row.lastrowid
        self.cache.clear()
        return sequence

    def _revoke_token(self, token):
        principal = self._token_principal(token)
        if principal is None:
            return False
        self.revoke_subject(principal, reason='oauth_revoke', reconcile=False)
        return True

    def _finish_revoke(self, sequence):
        with self.transaction() as db:
            db.execute("UPDATE revoke_intents SET state='closed' WHERE sequence=? AND state='pending'", (sequence,))

    def install(self, provider, server, app):
        """AFTER original pinned build_app, BEFORE any ASGI startup/requests."""
        if self.provider is not None or getattr(provider, '_mesh_events_grant_guard', None) is not None:
            raise Conflict('events_oauth_grant_guard_already_installed')
        for name in ('_upstream_token_store', '_jti_mapping_store', '_refresh_token_store'):
            if not callable(getattr(getattr(provider, name, None), 'get', None)):
                raise NotConfigured('events_existing_pinned_oauth_store_required')
        self.provider = provider
        original_code, original_refresh, original_revoke = (provider.exchange_authorization_code,
            provider.exchange_refresh_token, provider.revoke_token)
        async def code(client, authorization_code):
            async with self.lock:
                result = await original_code(client, authorization_code)
                # An optional Events metadata failure cannot rerun an already-
                # consumed OAuth code or discard the engine's successful token.
                try:
                    await self._register(result.access_token, result.refresh_token, fresh_code=True)
                except Exception:
                    pass  # Fail closed on Events access until a verified capture.
                return result
        async def refresh(client, refresh_token, scopes):
            async with self.lock:
                result = await original_refresh(client, refresh_token, scopes)
                try:
                    await self._register(result.access_token, result.refresh_token)
                except Exception:
                    pass
                return result
        async def revoke(token):
            async with self.lock:
                # Durable BEFORE the original provider can change a credential.
                # Failure here is NOT an attempted upstream revocation.
                try:
                    intent = self._prepare_revoke(token.token)
                except Exception:
                    self.cache.clear(); self.uncertain = True
                    raise NotConfigured('events_revoke_intent_unconfirmed_not_attempted') from None
                confirmed = False
                try:
                    confirmed = self._revoke_token(token.token) is True
                except Exception:
                    self.cache.clear()  # Durable intent, not a memory-only fence.
                # Once admitted, preserve ONE original call even if the later
                # local metadata failed. Exceptions/unknown leave intent pending.
                result = await original_revoke(token)
                if not confirmed:
                    raise NotConfigured('events_local_revoke_metadata_unconfirmed')
                try:
                    self._finish_revoke(intent)
                except Exception:
                    raise NotConfigured('events_local_revoke_metadata_unconfirmed') from None
                return result
        provider.exchange_authorization_code, provider.exchange_refresh_token, provider.revoke_token = code, refresh, revoke
        provider._mesh_events_grant_guard = self
        from mcp.server.auth.middleware.auth_context import get_access_token
        async def capture(context, call_next):
            token = get_access_token()
            if token is not None:
                try:
                    await self.observe_access(token.token)
                except Exception:
                    pass  # Never disable the original eight tools for Events.
            return await call_next(context)
        server.middleware.append(capture)
        previous = app.router.lifespan_context
        @contextlib.asynccontextmanager
        async def lifespan(application):
            async with previous(application) as state:
                self.loop, self.loop_thread, self.lock = asyncio.get_running_loop(), threading.get_ident(), asyncio.Lock()
                try:
                    yield state
                finally:
                    self.loop = None; self.loop_thread = None; self.cache.clear()
        app.router.lifespan_context = lifespan
        app._mesh_event_grants = self
        return self


def main(argv=None):
    """Owner-local explicit Events revoke only; never an HTTP/model operation."""
    import argparse
    from .native_messaging import _private_bytes, _unique, _nonfinite
    from .mcp_http_auth import validate_policy
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--oauth-config', required=True)
    parser.add_argument('--event-directory', required=True)
    parser.add_argument('--subject', required=True)
    parser.add_argument('--revoke-subject', required=True, action='store_true')
    args = parser.parse_args(argv)
    try:
        document = json.loads(_private_bytes(args.oauth_config).decode(), object_pairs_hook=_unique, parse_constant=_nonfinite)
        policy = validate_policy(document['policy'])
        gate = CurrentOAuthGrants(args.event_directory, policy)
        result = gate.revoke_subject({'owner_id': policy['owner_binding'], 'source': SOURCE,
            'subject': args.subject, 'scopes': ['tasks:read']})
        print(json.dumps(dict(result, events_only=True, original_business_retried=False)))
        return 0
    except Exception:
        print(json.dumps({'error': 'events_local_revoke_unconfirmed', 'retry_business': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
