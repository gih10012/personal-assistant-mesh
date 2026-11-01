"""Real pinned SDK state/TTL plus cancellation/drain and inactive staging.

Only the upstream IdP is synthetic. No actual OAuth state, credentials, browser,
SSH, GUI, network or model is used. A dependency skip is not migration evidence.
"""
import asyncio
import base64
import copy
import json
from pathlib import Path
import unittest

from assistant_mesh.chat_github_oauth import create_app
from assistant_mesh.oauth_state_bundle import (COLLECTIONS, COLLECTION_DIRECTORIES, KEY_ROLES, activate_staging, canonical,
    decode, export_bundle, sha, stage_bundle, verify_bundle)
from tests import test_chat_github_oauth as original


@unittest.skipUnless(original.AVAILABLE, 'real pinned OAuth environment required; a skip is not evidence')
class OAuthStateBundleTests(getattr(unittest, 'IsolatedAsyncioTestCase', unittest.TestCase)):
    asyncSetUp = original.ActualOAuthTests.asyncSetUp
    asyncTearDown = original.ActualOAuthTests.asyncTearDown
    idp = original.ActualOAuthTests.idp
    consent_start = original.ActualOAuthTests.consent_start
    authorization_code = original.ActualOAuthTests.authorization_code
    tokens = original.ActualOAuthTests.tokens
    rpc = original.ActualOAuthTests.rpc

    def make_app(self, **kwargs):
        from assistant_mesh.conversation import ConversationService
        if not hasattr(self, '_migration_conversation'):
            directory = self.directory / 'conversation-fixture'; directory.mkdir(mode=0o700)
            self._migration_conversation = ConversationService(directory, 'synthetic-owner', self.backend)
        return original.ActualOAuthTests.make_app(self, conversation_service=self._migration_conversation, **kwargs)

    def keys(self):
        return dict(zip(KEY_ROLES, (self.config['github']['client_secret_file'],
            self.config['state']['jwt_key_file'], self.config['state']['encryption_key_file'])))

    async def test_complete_encrypted_state_absolute_ttl_indexes_keys_and_bindings_survive_inactive_staging(self):
        from cryptography.fernet import Fernet
        from key_value.aio.stores.filetree import FileTreeStore
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
        async with self.app.router.lifespan_context(self.app):
            tokens = await self.tokens()
            consumed_code, verifier = await self.authorization_code('tasks:read')
            consumed = await self.http.post('/token', data={'grant_type': 'authorization_code',
                'client_id': original.CLIENT, 'code': consumed_code, 'code_verifier': verifier,
                'redirect_uri': original.CALLBACK, 'resource': original.ORIGIN + '/mcp'})
            self.assertEqual(200, consumed.status_code)
            revoked = consumed.json()['refresh_token']
            revoked_key = sha(revoked.encode())
            client = await self.provider.get_client(original.CLIENT)
            refresh = await self.provider.load_refresh_token(client, revoked)
            await self.provider.revoke_token(refresh)
            self.assertIsNone(await self.provider._refresh_token_store.get(key=revoked_key))
            self.assertIsNone(await self.provider._code_store.get(key=consumed_code))
            # Retain an unconsumed code and pending consent/CSRF transaction.
            code, _ = await self.authorization_code()
            await self.consent_start()
            response = await self.rpc(tokens['access_token'], 'tools/list')
            self.assertEqual(200, response.status_code)
            from assistant_mesh import conversation, session_nodes
            self.assertEqual({'mesh_submit_task', 'mesh_task_status', 'mesh_approved_result',
                'mesh_conversation_sync'} | set(conversation.TOOLS) | set(session_nodes.TOOLS),
                {value['name'] for value in response.json()['result']['tools']})
            self.assertEqual(13, len(response.json()['result']['tools']))
            encrypted = self.provider._client_storage
            await encrypted.put('expired_synthetic', {'unknown': {'receipt': 'keep complete'}},
                                collection='mcp-refresh-tokens', ttl=3600)
            source = Path(self.config['state']['directory'])
            expired = source / COLLECTION_DIRECTORIES['mcp-refresh-tokens'] / 'expired_synthetic.json'
            raw_expired = decode(expired.read_bytes())
            raw_expired['expires_at'] = '2000-01-01T00:00:00+00:00'
            expired.write_text(canonical(raw_expired))
            # Simulate an orphan from an interrupted SDK atomic write. It must
            # remain raw evidence, never be treated as a completed SDK entry.
            orphan = source / 'orphan-synthetic.tmp'; orphan.write_bytes(b'partial encrypted write\x00')
            orphan.chmod(0o600)
            nested_orphan = expired.parent / 'orphan-synthetic.tmp'
            nested_orphan.write_bytes(b'another unfinished write'); nested_orphan.chmod(0o600)
            lease = await self.app.oauth_migration.freeze()
            bundle = export_bundle(self.config, self.directory / 'bundle', lease=lease,
                                   allow_loopback_for_tests=True)
            self.assertEqual(set(COLLECTION_DIRECTORIES.values()), {name.split('/')[0] for name in bundle['directories']})
            self.assertEqual(COLLECTION_DIRECTORIES, {name: Path(str(info.directory)).name
                for name, info in encrypted.key_value._collection_infos.items()})
            self.assertEqual(0, bundle['writer_observation']['asgi_active'])
            self.assertEqual(0, bundle['writer_observation']['sdk_active'])
            self.assertFalse(bundle['writer_observation']['cross_process_writer_fenced'])
            self.assertEqual('disabled', bundle['writer_observation']['provider_state']['identity_assertion'])
            self.assertEqual(sorted(['orphan-synthetic.tmp', nested_orphan.relative_to(source).as_posix()]),
                             bundle['unknown_sdk_artifacts'])
            encoded = canonical(bundle).encode()
            self.assertNotIn(original.UPSTREAM_TOKEN.encode(), encoded)
            self.assertNotIn(original.UPSTREAM_REFRESH.encode(), encoded)
            for filename in self.keys().values():
                self.assertNotIn(Path(filename).read_bytes(), encoded)
            report = stage_bundle(self.directory / 'bundle/oauth-state-bundle.json', self.directory / 'staged',
                self.config, key_paths=self.keys(), expected_sha256=bundle['sha256'], allow_loopback_for_tests=True)
            self.assertFalse(report['activation_authorized'])
            self.assertTrue(report['key_dependencies_verified'])
            target = Path(report['state_directory'])
            for name, entry in bundle['files'].items():
                restored = (target / name).read_bytes()
                if name not in report['metadata_relocations']:
                    self.assertEqual(base64.b64decode(entry['base64']), restored)
                else:
                    old = decode(base64.b64decode(entry['base64'])); new = decode(restored)
                    self.assertEqual(str(target / COLLECTION_DIRECTORIES[old['collection']]), new.pop('directory'))
                    old.pop('directory'); self.assertEqual(old, new)
            offline = FernetEncryptionWrapper(key_value=FileTreeStore(data_directory=target),
                fernet=Fernet(Path(self.keys()['mesh_oauth_state_fernet']).read_bytes()))
            self.assertIsNotNone(await offline.get(code, collection='mcp-authorization-codes'))
            self.assertIsNone(await offline.get(consumed_code, collection='mcp-authorization-codes'))
            self.assertIsNone(await offline.get(revoked_key, collection='mcp-refresh-tokens'))
            self.assertIsNone(await offline.get('expired_synthetic', collection='mcp-refresh-tokens'))
            self.assertEqual('2000-01-01T00:00:00+00:00', decode((target / COLLECTION_DIRECTORIES['mcp-refresh-tokens'] / 'expired_synthetic.json').read_bytes())['expires_at'])
            target_config = copy.deepcopy(self.config); target_config['state']['directory'] = str(target)
            with self.assertRaisesRegex(ValueError, 'staging_not_activated'):
                create_app(target_config, backend=self.backend, http_client=self.identity_client,
                           allow_loopback_for_tests=True)
            with self.assertRaisesRegex(ValueError, 'trusted_original_writer_fence'):
                activate_staging(target, source_stopped=True, writer_fence={'verified': True})

    async def test_cancelled_asgi_and_detached_sdk_write_are_counted_until_real_io_finishes(self):
        boundary = self.app.oauth_migration
        filetree = self.provider._client_storage.key_value
        original_put = filetree._put_managed_entry
        entered = {key: asyncio.Event() for key in ('inflight', 'detached')}
        release = {key: asyncio.Event() for key in entered}
        async def delayed(*args, **kwargs):
            key = kwargs['key']
            if key in entered:
                entered[key].set(); await release[key].wait()
            return await original_put(*args, **kwargs)
        filetree._put_managed_entry = delayed
        completed_http = asyncio.Event()
        async def asgi(scope, receive, send):
            await self.provider._client_storage.put('inflight', {'original': 'request'}, collection='mcp-oauth-transactions')
            completed_http.set()
        async def receive():
            return {'type': 'http.disconnect'}
        async def send(message):
            pass
        outer = boundary.wrap_asgi(asgi)
        http = asyncio.create_task(outer({'type': 'http', 'method': 'POST'}, receive, send))
        detached = asyncio.create_task(self.provider._client_storage.put('detached', {'receipt': 'complete'},
            collection='mcp-oauth-transactions'))
        await asyncio.gather(*(event.wait() for event in entered.values()))
        http.cancel(); detached.cancel()
        for task in (http, detached):
            with self.assertRaises(asyncio.CancelledError):
                await task
        frozen = asyncio.create_task(boundary.freeze())
        await asyncio.sleep(0)
        self.assertFalse(frozen.done())
        self.assertTrue(boundary.http); self.assertTrue(boundary.ops)
        self.assertEqual(503, (await self.http.get('/.well-known/oauth-authorization-server')).status_code)
        release['inflight'].set(); await completed_http.wait(); await asyncio.sleep(0)
        self.assertFalse(frozen.done())  # detached real store write is still open
        release['detached'].set(); lease = await frozen
        self.assertEqual({}, boundary.http); self.assertEqual({}, boundary.ops)
        with self.assertRaisesRegex(ValueError, 'writer_frozen'):
            await self.provider._client_storage.put('late', {'not': 'written'}, collection='mcp-oauth-transactions')
        exported = export_bundle(self.config, self.directory / 'drained', lease=lease, allow_loopback_for_tests=True)
        prefix = COLLECTION_DIRECTORIES['mcp-oauth-transactions'] + '/'
        self.assertIn(prefix + 'inflight.json', exported['files'])
        self.assertIn(prefix + 'detached.json', exported['files'])
        self.assertNotIn(prefix + 'late.json', exported['files'])

    async def test_boolean_freeze_tampering_binding_key_and_existing_target_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'observed_freeze_lease'):
            export_bundle(self.config, self.directory / 'caller-boolean', lease=True, allow_loopback_for_tests=True)
        lease = await self.app.oauth_migration.freeze()
        changed = copy.deepcopy(self.config)
        changed['policy']['subjects'][0]['scopes'] = ['tasks:read']
        with self.assertRaisesRegex(ValueError, 'live_freeze_lease'):
            export_bundle(changed, self.directory / 'changed-binding', lease=lease, allow_loopback_for_tests=True)
        bundle = export_bundle(self.config, self.directory / 'export', lease=lease, allow_loopback_for_tests=True)
        source = self.directory / 'export/oauth-state-bundle.json'
        with self.assertRaisesRegex(ValueError, 'integrity_or_binding'):
            verify_bundle(source, changed, allow_loopback_for_tests=True)
        wrong_key = self.directory / 'wrong-service-key'; wrong_key.write_bytes(b'wrong' * 20); wrong_key.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'service_key_mismatch'):
            stage_bundle(source, self.directory / 'wrong-key-target', self.config,
                key_paths=dict(self.keys(), mesh_oauth_jwt_signing=str(wrong_key)), allow_loopback_for_tests=True)
        self.assertFalse((self.directory / 'wrong-key-target').exists())
        tampered = decode(source.read_bytes()); tampered['files']['mcp-oauth-proxy-clients/fake.json'] = {
            'size': 1, 'sha256': '0' * 64, 'base64': 'eA=='}
        source.write_text(canonical(tampered))
        with self.assertRaisesRegex(ValueError, 'integrity_or_binding'):
            stage_bundle(source, self.directory / 'tampered-target', self.config, allow_loopback_for_tests=True)
        self.assertFalse((self.directory / 'tampered-target').exists())
        source.write_text(canonical(bundle))
        staged = self.directory / 'existing'; staged.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'new_private_staging'):
            stage_bundle(source, staged, self.config, allow_loopback_for_tests=True)

    async def test_uncovered_memory_grants_replay_and_held_refresh_lock_cannot_claim_drain(self):
        import anyio
        boundary = self.app.oauth_migration
        self.provider._identity_assertion = object()
        with self.assertRaisesRegex(ValueError, 'id_jag_state_not_supported'):
            await boundary.freeze()
        self.provider._identity_assertion = None
        validator = self.provider._cimd_manager._assertion_validator
        validator._jti_cache['synthetic-replay'] = 4000000000
        with self.assertRaisesRegex(ValueError, 'cimd_replay_state_not_supported'):
            await boundary.freeze()
        validator._jti_cache.clear()
        lock = anyio.Lock(); await lock.acquire()
        self.provider._refresh_locks['synthetic-held-lock'] = lock
        with self.assertRaisesRegex(ValueError, 'refresh_lock_still_live'):
            await boundary.freeze()
        lock.release()
        lease = await boundary.freeze()
        exported = export_bundle(self.config, self.directory / 'memory-checked', lease=lease,
                                 allow_loopback_for_tests=True)
        self.assertEqual(0, exported['writer_observation']['provider_state']['cimd_assertion_replay_entries'])
