"""Owned local fixtures, not real remote-host/model/provider acceptance proof."""
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_mesh.server import API, serve
from assistant_mesh.worker import Client
from scripts import probe_provider_execution as probe


class FixtureClient:
    """Credential-role-bound in-process API fixture, NOT a real remote client."""
    def __init__(self, api, peer):
        self.api, self.peer = api, peer
        self.requests = []
        self.after = None

    def request(self, path, body=None):
        self.requests.append((path, copy.deepcopy(body)))
        result = self.api.dispatch('GET' if body is None else 'POST', path, body, self.peer)
        return self.after(path, body, result) if self.after else result


class ProviderExecutionProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='provider-proof-fixture.', dir='/var/tmp')
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.state = probe.prepare(self.root)
        cfg = probe.server_body(self.state)
        self.api = API(cfg)
        self.clients = {role: FixtureClient(self.api, cfg['peers'][index])
                        for index, role in enumerate(probe.ROLES)}

    def tearDown(self):
        self.temporary.cleanup()

    def submitted(self):
        return probe.submit(self.state, self.clients)

    def reserved(self):
        self.submitted()
        # Only the TEST OWNER calls real Store heartbeat/claim; the helper never
        # secretly obtains a lease on behalf of the operator.
        self.api.store.heartbeat(self.state['nodes']['requester'], probe.capabilities(self.state))
        task = self.api.store.claim(self.state['nodes']['requester'])
        self.assertEqual(self.state['ids']['task'], task['id'])
        return probe.reserve(self.state, self.clients['operator'], self.clients['requester'],
                             task['epoch'], task['leader_epoch'])

    def executed(self):
        self.reserved()
        return probe.execute(self.state, self.clients['provider'])

    def rewrite(self, path, value):
        path = Path(path)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode('utf8'))
        path.chmod(0o600)
        return path

    def local_row(self):
        with contextlib.closing(sqlite3.connect(str(self.root / 'provider.sqlite'))) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute('SELECT * FROM provider_executions').fetchone())

    def snapshot(self):
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                for path in self.root.iterdir() if path.is_file()}

    def test_prepare_immutable_ids_and_reference_not_disclosed_to_state_plan_or_prompt(self):
        before = self.snapshot()
        self.assertEqual(self.state, probe.prepare(self.root))
        self.assertEqual(before, self.snapshot())
        reference = probe.reference(self.state)
        self.assertEqual(64, len((self.root / 'input.bin').read_bytes()))
        for payload in (self.state, probe.plan(self.state), probe.task_body(self.state), probe.server_body(self.state)):
            self.assertNotIn(reference['sha256'], json.dumps(payload))
        for path in self.root.iterdir():
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(1, path.stat().st_nlink)
        self.assertNotIn('auth_home', json.dumps(self.state))

    def test_prepare_partial_or_missing_state_never_generates_replacement_run(self):
        (self.root / 'state.json').unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(probe.ProbeError, 'prepare_requires_empty_root'):
            probe.prepare(self.root)
        self.assertEqual(before, self.snapshot())

    def test_private_root_rejects_git_marker_including_broken_git_symlink(self):
        marker = self.root / '.git'
        marker.symlink_to(self.root / 'missing-git-target')
        with self.assertRaisesRegex(probe.ProbeError, 'inside_git'):
            probe.load_state(self.root)

    def test_private_root_rejects_nonprivate_mode_or_root_base(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(probe.ProbeError, '0700_root'):
            probe.load_state(self.root)
        with self.assertRaisesRegex(probe.ProbeError, 'temporary_root'):
            probe.private_root('/var/tmp')

    def test_input_rejects_hardlinks_symlink_and_nonprivate_file(self):
        source = self.root / 'input.bin'
        os.link(str(source), str(self.root / 'alias.bin'))
        with self.assertRaisesRegex(probe.ProbeError, 'single_link'):
            probe.input_data(self.state)
        (self.root / 'alias.bin').unlink()
        source.chmod(0o644)
        with self.assertRaisesRegex(probe.ProbeError, '0600'):
            probe.input_data(self.state)
        source.chmod(0o600)
        source.rename(self.root / 'real-input.bin')
        source.symlink_to(self.root / 'real-input.bin')
        with self.assertRaisesRegex(probe.ProbeError, 'symlink'):
            probe.input_data(self.state)

    def test_input_swapped_fifo_is_nonblocking_and_rejected_before_read(self):
        path = self.root / 'input.bin'
        original_open = os.open
        def swapped(value, flags, *args, **kwargs):
            if value == str(path):
                self.assertTrue(flags & os.O_NONBLOCK)
                path.rename(self.root / 'original-input.bin')
                os.mkfifo(str(path), 0o600)
            return original_open(value, flags, *args, **kwargs)
        with patch.object(probe.os, 'open', side_effect=swapped):
            with self.assertRaisesRegex(probe.ProbeError, 'input_identity_changed'):
                probe.input_data(self.state)

    def test_input_actual_open_descriptor_mode_checked_after_path_metadata(self):
        path = self.root / 'input.bin'
        original_open = os.open
        def changed(value, flags, *args, **kwargs):
            if value == str(path):
                path.chmod(0o644)
            return original_open(value, flags, *args, **kwargs)
        with patch.object(probe.os, 'open', side_effect=changed):
            with self.assertRaisesRegex(probe.ProbeError, 'input_identity_changed'):
                probe.input_data(self.state)

    def test_exclusive_publication_fsyncs_file_and_final_parent_directory(self):
        synced = []
        original = os.fsync
        def tracked(descriptor):
            synced.append(os.fstat(descriptor).st_mode)
            return original(descriptor)
        with patch.object(probe.os, 'fsync', side_effect=tracked):
            probe.publish(self.root / 'durability.json', {'actual': 'owner fixture'})
        self.assertTrue(any(stat.S_ISREG(mode) for mode in synced))
        self.assertTrue(stat.S_ISDIR(synced[-1]))
        self.assertEqual(1, (self.root / 'durability.json').stat().st_nlink)

    def test_state_and_client_identity_changes_fail_closed(self):
        value = copy.deepcopy(self.state)
        value['ids']['operation'] = 'substitute'
        self.rewrite(self.root / 'state.json', value)
        with self.assertRaisesRegex(probe.ProbeError, 'bindings_changed'):
            probe.load_state(self.root)
        self.rewrite(self.root / 'state.json', self.state)
        value = probe.client_body(self.state, 'provider')
        value['token_file'] = str(self.root / 'operator.token')
        self.rewrite(self.root / 'provider-client.json', value)
        with patch.object(probe, 'Client') as constructor:
            with self.assertRaisesRegex(probe.ProbeError, 'client_binding_changed'):
                probe.client_for(self.state, 'provider')
            constructor.assert_not_called()

    def test_setup_real_workload_measurement_exact_grant_and_single_pool_not_performance(self):
        output = self.submitted()
        self.assertEqual(64, output['measured_input_bytes'])
        self.assertFalse(output['parent_task_claimed_by_helper'])
        self.assertFalse(output['independent_performance_verified'])
        calls = [call for client in self.clients.values() for call in client.requests]
        self.assertFalse(any(path in ('/v1/heartbeat', '/v1/claim') for path, body in calls))
        expected = probe.reference(self.state)['sha256']
        self.assertNotIn(expected, json.dumps(calls))
        description = self.api.resources.describe(self.state['ids']['capability'])
        metric = description['metrics'][0]
        self.assertEqual(('input_bytes', 64, 'byte', 'verified'),
                         (metric['metric'], metric['value'], metric['unit'], metric['verification']))
        self.assertEqual('node:' + self.state['nodes']['verifier'], metric['actor'])
        self.assertNotEqual(description['principal'], metric['actor'])
        self.assertEqual(probe.scope(self.state), metric['evidence']['scope'])
        pools = self.api.allocations.pools('operator')['pools']
        self.assertEqual(1, len(pools))
        self.assertEqual(1, pools[0]['dimensions']['requests']['capacity'])

    def test_setup_retry_keeps_original_observation_time_and_ids(self):
        self.submitted()
        self.submitted()
        with self.api.store.transaction() as db:
            for table in ('tasks', 'capabilities', 'capability_metrics', 'resource_pools', 'capability_grants'):
                self.assertEqual(1, db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0])
            self.assertEqual(self.state['sample_time'], db.execute('SELECT sample_time FROM capability_metrics').fetchone()[0])

    def test_reserve_requires_owner_observed_actual_claim_and_leader_epoch(self):
        self.submitted()
        with self.assertRaisesRegex(probe.ProbeError, 'claim_or_leader_not_current'):
            probe.reserve(self.state, self.clients['operator'], self.clients['requester'], 1, 1)
        self.api.store.heartbeat(self.state['nodes']['requester'], probe.capabilities(self.state))
        task = self.api.store.claim(self.state['nodes']['requester'])
        for task_epoch, leader_epoch in ((True, 1), (1, None), (2, task['leader_epoch']),
                                         (task['epoch'], task['leader_epoch'] + 1)):
            with self.subTest(task_epoch=task_epoch, leader_epoch=leader_epoch), self.assertRaises(probe.ProbeError):
                probe.reserve(self.state, self.clients['operator'], self.clients['requester'], task_epoch, leader_epoch)
        with self.api.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM managed_allocations').fetchone()[0])

    def test_actual_local_sha_executes_once_and_independent_readonly_observe_matches(self):
        self.reserved()
        binding = probe.hash_adapter(self.state)
        calls = []
        original = binding.invoke
        binding.invoke = lambda context: (calls.append(copy.deepcopy(context)), original(context))[1]
        with patch.object(probe, 'hash_adapter', return_value=binding):
            result = probe.execute(self.state, self.clients['provider'])
            self.assertEqual('settled', result['state'])
            repeat = probe.execute(self.state, self.clients['provider'])
        self.assertEqual(result, repeat)
        self.assertEqual(1, len(calls))
        self.assertNotIn(probe.reference(self.state)['sha256'], json.dumps(calls))
        before = self.snapshot()
        with patch.object(probe, 'ManagedProvider', side_effect=AssertionError('observe must not construct bridge')):
            report = probe.observe(self.state, self.clients['operator'])
        self.assertEqual(before, self.snapshot())
        self.assertTrue(report['actual_result_matches_independently_retained_owner_reference'])
        self.assertTrue(report['authority_settlement_and_capacity_release_confirmed'])
        for flag in ('execution_verified_by_core', 'independent_performance_verified',
                     'independent_physical_host_verified', 'resource_quiescence_independently_verified',
                     'automatic_replay', 'retry_with_new_id', 'native_tools_intercepted'):
            self.assertFalse(report[flag])

    def test_callback_and_execute_never_read_owner_reference(self):
        self.reserved()
        original = probe.read_json
        def read(path):
            self.assertNotEqual('owner-reference.json', Path(path).name)
            return original(path)
        with patch.object(probe, 'read_json', side_effect=read), patch.object(probe, 'reference', side_effect=AssertionError):
            self.assertEqual('settled', probe.execute(self.state, self.clients['provider'])['state'])

    def test_unknown_callback_outcome_after_bad_input_not_automatically_reexecuted(self):
        self.reserved()
        self.rewrite(self.root / 'input.bin', b'changed')
        original = probe.hash_adapter(self.state)
        calls = []
        invoke = original.invoke
        original.invoke = lambda context: (calls.append(True), invoke(context))[1]
        with patch.object(probe, 'hash_adapter', return_value=original):
            first = probe.execute(self.state, self.clients['provider'])
            self.rewrite(self.root / 'input.bin', os.urandom(64))
            second = probe.execute(self.state, self.clients['provider'])
        self.assertEqual('unknown', first['state'])
        self.assertEqual(first, second)
        self.assertEqual([True], calls)
        self.assertIsNone(first['local_invocation_started'])
        self.assertTrue(first['local_invocation_intent_recorded'])
        self.assertEqual(0, self.api.allocations.pools('operator')['pools'][0]['dimensions']['requests']['remaining'])

    def test_missing_journal_for_already_admitted_operation_is_not_recreated(self):
        self.executed()
        (self.root / 'provider.sqlite').unlink()
        with patch.object(probe, 'ManagedProvider') as constructor:
            with self.assertRaisesRegex(probe.ProbeError, 'journal_missing_no_replay'):
                probe.execute(self.state, self.clients['provider'])
            constructor.assert_not_called()
        self.assertFalse((self.root / 'provider.sqlite').exists())

    def test_forged_authority_plan_does_not_construct_journal_or_call_adapter(self):
        self.reserved()
        def forge(path, body, result):
            if body['action'] == 'inspect':
                result['plan']['selections'][0]['scope'] = {'fixture_path': '/private-not-granted'}
            return result
        self.clients['provider'].after = forge
        with patch.object(probe, 'ManagedProvider') as constructor:
            with self.assertRaisesRegex(probe.ProbeError, 'identity_or_plan_changed'):
                probe.execute(self.state, self.clients['provider'])
            constructor.assert_not_called()
        self.assertFalse((self.root / 'provider.sqlite').exists())

    def test_settlement_response_lost_reconcile_same_result_never_enters_callback(self):
        self.reserved()
        def lose(path, body, result):
            if body['action'] == 'settle':
                raise TimeoutError('simulated private network detail')
            return result
        self.clients['provider'].after = lose
        self.assertEqual('settlement_unknown', probe.execute(self.state, self.clients['provider'])['state'])
        self.assertFalse(probe.observe(self.state, self.clients['operator'])[
            'actual_result_matches_independently_retained_owner_reference'])
        self.clients['provider'].after = None
        with patch.object(probe, 'hash_adapter', side_effect=AssertionError('reconcile entered callback')):
            self.assertEqual('settled', probe.reconcile(self.state, self.clients['provider'])['state'])
        report = probe.observe(self.state, self.clients['operator'])
        self.assertTrue(report['actual_result_matches_independently_retained_owner_reference'])
        self.assertEqual(1, sum(body['action'] == 'start' for path, body in self.clients['provider'].requests))

    def test_reconcile_requires_existing_journal_never_creates_one(self):
        with patch.object(probe, 'ManagedProvider') as constructor:
            with self.assertRaises(probe.ProbeError):
                probe.reconcile(self.state, self.clients['provider'])
            constructor.assert_not_called()
        self.assertFalse((self.root / 'provider.sqlite').exists())

    def test_observe_missing_journal_never_constructs_bridge_or_creates_sidecars(self):
        self.reserved()
        before = self.snapshot()
        with patch.object(probe, 'ManagedProvider') as constructor:
            with self.assertRaises(probe.ProbeError):
                probe.observe(self.state, self.clients['operator'])
            constructor.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_old_sqlite_reader_fails_closed_instead_of_creating_shm(self):
        self.executed()
        before = self.snapshot()
        with patch.object(probe.sqlite3, 'sqlite_version_info', (3, 7, 17)):
            with self.assertRaisesRegex(probe.ProbeError, 'immutable_reader_required'):
                probe.observe(self.state, self.clients['operator'])
        self.assertEqual(before, self.snapshot())

    def test_observe_rejects_wal_snapshot_or_active_execution_lock_without_writes(self):
        self.executed()
        sidecar = self.rewrite(self.root / 'provider.sqlite-wal', b'not a closed snapshot')
        before = self.snapshot()
        with self.assertRaisesRegex(probe.ProbeError, 'closed_journal_snapshot_required'):
            probe.observe(self.state, self.clients['operator'])
        self.assertEqual(before, self.snapshot())
        sidecar.unlink()
        lock = Path(str(self.root / 'provider.sqlite') + '.lock-' +
                    probe.digest([self.state['ids']['operation'], self.state['ids']['capability']]))
        with lock.open('rb') as owner:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(probe.ProbeError, 'execution_instance_active'):
                probe.observe(self.state, self.clients['operator'])

    def test_observe_validates_canonical_namespace_and_context(self):
        self.executed()
        with contextlib.closing(sqlite3.connect(str(self.root / 'provider.sqlite'))) as db:
            db.execute("UPDATE provider_metadata SET value='another-authority' WHERE key='authority'")
            db.commit()
        with self.assertRaisesRegex(probe.ProbeError, 'journal_namespace_changed'):
            probe.observe(self.state, self.clients['operator'])

    def test_wrong_provider_result_not_independently_verified(self):
        self.reserved()
        wrong = probe.Adapter(probe.HANDLE, 1, probe.ACTION, lambda context: {
            'outcome': 'completed', 'resource_quiescent': True, 'result_reference': 'sha256:' + '0' * 64,
            'evidence': {'sha256': '0' * 64, 'input_bytes': 64, 'file_read_closed': True, 'child_processes_started': 0}})
        with patch.object(probe, 'hash_adapter', return_value=wrong):
            self.assertEqual('settled', probe.execute(self.state, self.clients['provider'])['state'])
        report = probe.observe(self.state, self.clients['operator'])
        self.assertFalse(report['actual_result_matches_independently_retained_owner_reference'])
        self.assertEqual('not_verified', report['result_verification'])

    def test_cli_defaults_to_readonly_observe_and_never_recreates_missing_state(self):
        (self.root / 'state.json').unlink()
        before = self.snapshot()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), patch.object(probe, 'ManagedProvider') as constructor:
            self.assertEqual(1, probe.main(['--root', str(self.root)]))
            constructor.assert_not_called()
        self.assertEqual(before, self.snapshot())
        self.assertFalse(json.loads(stream.getvalue())['retry_with_new_id'])

    def test_cli_observe_sanitized_report_only_written_with_explicit_report(self):
        self.executed()
        before = self.snapshot()
        with patch.object(probe, 'client_for', return_value=self.clients['operator']):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, probe.main(['--root', str(self.root)]))
            self.assertEqual(before, self.snapshot())
            report = self.root / 'report.json'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, probe.main(['observe', '--root', str(self.root), '--report', str(report)]))
        data = report.read_text()
        self.assertNotIn(probe.reference(self.state)['sha256'], data)
        self.assertNotIn('input.bin', data)
        self.assertEqual(0o600, report.stat().st_mode & 0o777)

    def test_cli_does_not_echo_exception_body_or_auth_information(self):
        stream = io.StringIO()
        with patch.object(probe, 'client_for', side_effect=RuntimeError('private credential secret 123456')):
            with contextlib.redirect_stdout(stream):
                self.assertEqual(1, probe.main(['--root', str(self.root)]))
        data = stream.getvalue()
        self.assertNotIn('123456', data)
        self.assertNotIn('private credential', data)
        self.assertEqual('provider_probe_RuntimeError', json.loads(data)['error'])

    def test_client_uses_only_pinned_mesh_token_reference(self):
        with patch.object(probe, 'Client') as constructor:
            probe.client_for(self.state, 'provider')
        constructor.assert_called_once_with(probe.client_body(self.state, 'provider'))

    def test_unicode_owner_root_uses_exact_core_canonical_fingerprint(self):
        with tempfile.TemporaryDirectory(prefix='provider-能力-fixture.', dir='/var/tmp') as value:
            root = Path(value)
            root.chmod(0o700)
            state = probe.prepare(root)
            cfg = probe.server_body(state)
            api = API(cfg)
            clients = {role: FixtureClient(api, cfg['peers'][index])
                       for index, role in enumerate(probe.ROLES)}
            probe.submit(state, clients)
            api.store.heartbeat(state['nodes']['requester'], probe.capabilities(state))
            task = api.store.claim(state['nodes']['requester'])
            probe.reserve(state, clients['operator'], clients['requester'], task['epoch'], task['leader_epoch'])
            self.assertEqual('settled', probe.execute(state, clients['provider'])['state'])
            self.assertTrue(probe.observe(state, clients['operator'])[
                'actual_result_matches_independently_retained_owner_reference'])

    def test_actual_loopback_http_bound_roles_and_owned_file_execution(self):
        # Actual localhost HTTP/authentication and SHA, still not cross-host
        # provider acceptance. The owner fixture alone launches this test server.
        cfg = dict(probe.server_body(self.state), port=0)
        ready, holder = threading.Event(), {}
        def on_ready(server, api, channel):
            holder.update(server=server, api=api)
            ready.set()
        thread = threading.Thread(target=serve, args=(cfg, on_ready), daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5))
        try:
            url = 'http://127.0.0.1:%d' % holder['server'].server_address[1]
            clients = {role: Client(dict(probe.client_body(self.state, role), control_url=url))
                       for role in probe.ROLES}
            probe.submit(self.state, clients)
            # HTTP heartbeat/claim are explicit owner test actions, never inside
            # submit/reserve/execute/observe or a native model/tool wrapper.
            clients['requester'].request('/v1/heartbeat', {'capabilities': probe.capabilities(self.state)})
            claimed = clients['requester'].request('/v1/claim', {})['task']
            reserved = probe.reserve(self.state, clients['operator'], clients['requester'],
                                     claimed['epoch'], claimed['leader_epoch'])
            self.assertEqual('reserved', reserved['state'])
            self.assertEqual('settled', probe.execute(self.state, clients['provider'])['state'])
            self.assertTrue(probe.observe(self.state, clients['operator'])[
                'actual_result_matches_independently_retained_owner_reference'])
        finally:
            holder['server'].shutdown()
            thread.join(5)
            self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
