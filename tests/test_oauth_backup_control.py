"""Real local socket/lifespan + pinned OAuth SDK, synthetic IdP/state only."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest import mock

from assistant_mesh.oauth_backup_control import PROTOCOL
from assistant_mesh.oauth_state_bundle import decode, verify_bundle
from scripts.oauth_owner_backup import request as owner_request
from tests import test_chat_github_oauth as original


@unittest.skipUnless(original.AVAILABLE, 'real pinned OAuth environment required; skip is not backup evidence')
class OwnerBackupControlTests(getattr(unittest, 'IsolatedAsyncioTestCase', unittest.TestCase)):
    asyncSetUp = original.ActualOAuthTests.asyncSetUp
    asyncTearDown = original.ActualOAuthTests.asyncTearDown
    idp = original.ActualOAuthTests.idp
    consent_start = original.ActualOAuthTests.consent_start
    authorization_code = original.ActualOAuthTests.authorization_code
    tokens = original.ActualOAuthTests.tokens

    def make_app(self, **kwargs):
        if not hasattr(self, 'control_config'):
            directory = self.directory / 'owner-backup'; directory.mkdir(mode=0o700)
            self.control_config = {'directory': str(directory), 'drain_timeout': 1}
        return original.ActualOAuthTests.make_app(self, backup_control=self.control_config, **kwargs)

    async def cli(self, action, operation=None, instance=None):
        # Exercise the shipped CLI transport without blocking the ASGI loop.
        return await asyncio.to_thread(owner_request, self.control_config, action, operation, instance)

    async def wire(self, payload, lost_ack=False):
        reader, writer = await asyncio.open_unix_connection(str(Path(self.control_config['directory']) / 'control.sock'))
        writer.write((json.dumps(payload) + '\n').encode()); await writer.drain()
        if lost_ack:
            writer.close(); await writer.wait_closed(); return None
        try:
            return decode(await asyncio.wait_for(reader.readline(), 5))
        finally:
            writer.close(); await writer.wait_closed()

    async def settled(self, operation, instance):
        async def poll():
            while True:
                result = await self.cli('status', operation, instance)
                if result['state'] not in ('not_found', 'backup_pending'):
                    return result
                await asyncio.sleep(.01)
        return await asyncio.wait_for(poll(), 5)

    async def test_lost_backup_ack_original_operation_query_and_explicit_same_lease_resume(self):
        async with self.app.router.lifespan_context(self.app):
            await self.tokens()
            info = await self.cli('info'); instance = info['instance_id']
            operation = 'synthetic-original-backup'
            await self.wire({'protocol': PROTOCOL, 'action': 'backup', 'operation_id': operation,
                             'instance_id': instance}, lost_ack=True)
            result = await self.settled(operation, instance)
            self.assertEqual('backup_committed_source_closed', result['state'])
            self.assertEqual('backup_not_handoff', result['purpose'])
            self.assertFalse(result['activation_authorized']); self.assertFalse(result['cross_process_writer_fenced'])
            self.assertEqual((0, 0), (result['asgi_active'], result['sdk_active']))
            bundle_path = Path(result['bundle_path']); before = bundle_path.stat().st_ino, bundle_path.read_bytes()
            self.assertEqual(result, await self.cli('backup', operation, instance))
            self.assertEqual(before, (bundle_path.stat().st_ino, bundle_path.read_bytes()))
            manifest, _, _ = verify_bundle(bundle_path, self.config,
                expected_sha256=result['bundle_sha256'], allow_loopback_for_tests=True)
            self.assertEqual('disabled', manifest['writer_observation']['provider_state']['identity_assertion'])
            self.assertEqual(503, (await self.http.get('/.well-known/oauth-authorization-server')).status_code)
            lease = self.app.oauth_migration._lease
            resumed = await self.cli('resume', operation, instance)
            self.assertEqual('source_resumed_backup_only', resumed['state'])
            self.assertFalse(resumed['handoff_authorized'])
            self.assertEqual(200, (await self.http.get('/.well-known/oauth-authorization-server')).status_code)
            self.assertEqual(resumed, await self.cli('resume', operation, instance))
            with self.assertRaisesRegex(ValueError, 'live_freeze_lease_required'):
                lease.evidence(self.app.oauth_migration.state_root, self.app.oauth_migration.binding,
                               self.app.oauth_migration.key_references)
            self.assertEqual(before, (bundle_path.stat().st_ino, bundle_path.read_bytes()))

    async def test_restart_can_query_original_backup_but_cannot_resume_or_replace_its_instance(self):
        operation = 'synthetic-restart-backup'
        async with self.app.router.lifespan_context(self.app):
            instance = (await self.cli('info'))['instance_id']
            await self.cli('backup', operation, instance)
            original_result = await self.settled(operation, instance)
            self.assertEqual('backup_committed_source_closed', original_result['state'])
            original_lease = self.app.oauth_migration._lease
        _, _, restarted = self.make_app()
        with self.assertRaisesRegex(ValueError, 'live_freeze_lease_required'):
            await restarted.oauth_migration.resume_backup(original_lease)
        async with restarted.router.lifespan_context(restarted):
            info = await self.cli('info')
            self.assertNotEqual(instance, info['instance_id'])
            self.assertTrue(info['restart_closed']); self.assertEqual('sealed', info['source_phase'])
            result = await self.cli('status', operation, instance)
            self.assertEqual(original_result['bundle_sha256'], result['bundle_sha256'])
            for action, op, selected in (('resume', operation, instance), ('backup', 'new-fake-operation', info['instance_id'])):
                denied = await self.cli(action, op, selected)
                self.assertEqual('oauth_backup_original_live_instance_required', denied['error'])
            async with self.real_client(transport=original.httpx2.ASGITransport(app=restarted), base_url=original.ORIGIN) as http:
                self.assertEqual(503, (await http.get('/.well-known/oauth-authorization-server')).status_code)

    async def test_owner_auth_exact_fields_and_drain_timeout_keep_original_admission_closed(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def stalled(scope, receive, send):
            started.set(); await release.wait()
        async def receive():
            return {'type': 'http.disconnect'}
        async def send(message):
            pass
        async with self.app.router.lifespan_context(self.app):
            instance = (await self.cli('info'))['instance_id']
            with mock.patch('assistant_mesh.oauth_backup_control.owner_peer', side_effect=PermissionError('oauth_backup_owner_uid_required')):
                denied = await self.wire({'protocol': PROTOCOL, 'action': 'info'})
                self.assertFalse(denied['request_accepted'])
                self.assertEqual('oauth_backup_owner_uid_required', denied['error'])
            payload = {'protocol': PROTOCOL, 'action': 'backup', 'operation_id': 'synthetic-timeout', 'instance_id': instance}
            for changed in (dict(payload, destination='/untrusted/path'), dict(payload, source_stopped=True),
                            dict(payload, action='activate')):
                self.assertFalse((await self.wire(changed))['request_accepted'])
            live = asyncio.create_task(self.app.oauth_migration.wrap_asgi(stalled)({'type': 'http'}, receive, send))
            await started.wait()
            try:
                await self.cli('backup', payload['operation_id'], instance)
                failed = await self.settled(payload['operation_id'], instance)
                self.assertEqual('backup_failed_source_closed', failed['state'])
                self.assertEqual('oauth_backup_drain_timeout', failed['error'])
                self.assertEqual(503, (await self.http.get('/.well-known/oauth-authorization-server')).status_code)
                denied = await self.cli('resume', payload['operation_id'], instance)
                self.assertEqual('oauth_backup_committed_original_lease_required', denied['error'])
                self.assertEqual(failed, await self.cli('backup', payload['operation_id'], instance))
            finally:
                release.set(); await live
