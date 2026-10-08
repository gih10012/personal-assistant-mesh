import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from assistant_mesh.server import API, serve
from assistant_mesh.store import Conflict


class AllocationAPITests(unittest.TestCase):
    """Journal/API fixtures, not evidence of real provider tool execution."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.config = {'database': str(Path(self.temp.name) / 'db'), 'peers': [],
                       'resources': {'trusted_verifiers': ['node:checker']}}
        self.api = API(self.config)
        self.api.store.clock = lambda: self.now
        self.worker = {'role': 'worker', 'node': 'laptop'}
        self.provider = {'role': 'worker', 'node': 'cloud'}
        self.other = {'role': 'worker', 'node': 'other'}
        self.owner = {'role': 'operator', 'node': 'not-an-impersonation'}
        self.viewer = {'role': 'viewer', 'node': 'cloud'}
        self.scope = {'fixture': 'read-only-digest'}
        self.workload = {'input_bytes': 64}
        self.api.store.heartbeat('laptop', ['leader', 'agent'])
        self.api.store.create_task('fixture parent, no real provider effect', task_id='parent')
        self.task = self.api.store.claim('laptop')
        self.api.resources.advertise('node:cloud', 'target', 'future.unenumerated.tool')
        self.api.resources.observe('node:checker', 'target', 'measured.future', 2, 'ms',
                                   'independent fixture', epoch=1, verification='verified',
                                   evidence={'scope': self.scope, 'workload': self.workload},
                                   observation_id='observation')
        self.api.resources.request_grant('node:laptop', 'target', 'future.read', self.scope,
                                        request_id='grant-request')
        self.api.resources.grant('operator', 'grant-request')
        self.action('define_pool', {'id': 'shared', 'dimensions': {
            'request.capacity': {'capacity': 1, 'unit': 'request-quantum'}}}, self.owner)
        self.action('bind_pool', {'capability_id': 'target', 'capability_epoch': 1,
                                  'pool_id': 'shared', 'pool_epoch': 1,
                                  'dimension': 'request.capacity', 'quantity': 1,
                                  'unit': 'request-quantum'}, self.owner)
        self.plan = {'target': 'target', 'selections': [{
            'capability_id': 'target', 'epoch': 1, 'action': 'future.read',
            'scope': self.scope, 'workload': self.workload, 'observations': [{
                'observation_id': 'observation', 'metric': 'measured.future',
                'unit': 'ms', 'max_age_seconds': 60}]}], 'route': []}

    def tearDown(self):
        self.temp.cleanup()

    def action(self, action, arguments=None, peer=None):
        return self.api.dispatch('POST', '/v1/allocation/action',
                                 {'action': action, 'arguments': arguments or {}},
                                 self.worker if peer is None else peer)

    def reserve(self, operation_id='operation', peer=None, **extra):
        arguments = {'operation_id': operation_id, 'task_id': self.task['id'],
                     'task_epoch': self.task['epoch'], 'plan': copy.deepcopy(self.plan),
                     'lease_seconds': 40}
        arguments.update(extra)
        return self.action('reserve', arguments, peer)

    def provider_action(self, action, epoch, peer=None, **extra):
        arguments = {'operation_id': 'operation', 'capability_id': 'target', 'epoch': epoch}
        if action in ('accept', 'start', 'settle'):
            arguments['receipt_id'] = 'provider-receipt'
        if action == 'settle':
            arguments.update(outcome='completed', evidence={
                'resource_quiescent': True, 'result_reference': 'fixture-only-result'})
        arguments.update(extra)
        return self.action(action, arguments, self.provider if peer is None else peer)

    def remaining(self):
        return self.action('pools')['pools'][0]['dimensions']['request.capacity']['remaining']

    def test_reservation_is_node_bound_and_does_not_execute_or_create_task(self):
        reserved = self.reserve()
        self.assertEqual('node:laptop', reserved['actor'])
        self.assertEqual('laptop', reserved['task_node'])
        self.assertFalse(reserved['execution_authorized'])
        self.assertFalse(reserved['execution_verified'])
        self.assertFalse(reserved['native_tools_intercepted'])
        with self.api.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM managed_dispatch').fetchone()[0])
        self.assertEqual(0, self.remaining())
        self.assertFalse(self.reserve()['reservation_created'])
        for peer in (self.provider, self.other, self.owner):
            with self.subTest(peer=peer), self.assertRaises(PermissionError):
                self.reserve('cannot-impersonate-parent', peer)

    def test_identity_fields_cannot_be_supplied_even_if_matching(self):
        for key in ('actor', 'principal', 'owner', 'node', 'role', 'provider', 'trusted_verifiers'):
            for location in ('body', 'arguments'):
                request = {'action': 'pools', 'arguments': {}}
                if location == 'body':
                    request[key] = 'operator'
                else:
                    request['arguments'][key] = 'node:laptop'
                with self.subTest(key=key, location=location), self.assertRaises(PermissionError):
                    self.api.dispatch('POST', '/v1/allocation/action', request, self.worker)
        with self.api.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM managed_allocations').fetchone()[0])

    def test_viewer_and_agent_peer_cannot_mutate_or_impersonate_provider(self):
        for peer in (self.viewer, {'role': 'viewer'}, {'role': 'agent_peer', 'node': 'cloud'},
                     {'role': 'unknown', 'node': 'cloud'}, {'role': 'worker'}):
            for action in ('pools', 'pending', 'define_pool', 'reserve', 'start'):
                with self.subTest(peer=peer, action=action), self.assertRaises(PermissionError):
                    self.action(action, peer=peer)

    def test_only_owner_can_change_pool_binding_or_expire_journal(self):
        operations = {
            'define_pool': {'id': 'other', 'dimensions': {'x': {'capacity': 2, 'unit': 'u'}}},
            'renew_pool': {'id': 'shared', 'epoch': 1},
            'revoke_pool': {'id': 'shared', 'epoch': 1},
            'bind_pool': {'capability_id': 'target', 'capability_epoch': 1, 'pool_id': 'shared',
                          'pool_epoch': 1, 'dimension': 'request.capacity', 'quantity': 1,
                          'unit': 'request-quantum'},
            'expire': {},
        }
        for action, arguments in operations.items():
            for peer in (self.worker, self.provider):
                with self.subTest(action=action, peer=peer), self.assertRaises(PermissionError):
                    self.action(action, arguments, peer)
        pool = self.action('renew_pool', {'id': 'shared', 'epoch': 1}, self.owner)
        self.assertEqual(1, pool['epoch'])
        self.assertFalse(pool['native_resource_isolation'])
        binding = self.action('bindings', {'capability_id': 'target'})['bindings'][0]
        self.assertEqual(0, binding['new_spend_minor'])
        with self.assertRaises(PermissionError):
            self.action('bind_pool', dict(operations['bind_pool'], new_spend_minor=1), self.owner)

    def test_provider_queue_and_inspection_are_principal_bound(self):
        self.reserve()
        pending = self.action('pending', peer=self.provider)['dispatch']
        self.assertEqual(1, len(pending))
        self.assertEqual('node:cloud', pending[0]['provider'])
        self.assertTrue(pending[0]['admission_required'])
        self.assertFalse(pending[0]['execution_authorized'])
        self.assertEqual([], self.action('pending')['dispatch'])
        self.assertEqual([], self.action('pending', peer=self.other)['dispatch'])
        for peer in (self.worker, self.provider, self.owner):
            inspected = self.action('inspect', {'operation_id': 'operation'}, peer)
            self.assertFalse(inspected['execution_verified'])
        with self.assertRaises(PermissionError):
            self.action('inspect', {'operation_id': 'operation'}, self.other)

    def test_provider_transitions_cannot_be_performed_by_parent_or_operator(self):
        self.reserve()
        for action in ('accept', 'start', 'unknown', 'settle', 'decline'):
            for peer in (self.worker, self.other, self.owner):
                with self.subTest(action=action, peer=peer), self.assertRaises(PermissionError):
                    self.provider_action(action, 1, peer)
        accepted = self.provider_action('accept', 1)
        self.assertTrue(accepted['accepted_new'])
        self.assertFalse(self.provider_action('accept', 1)['accepted_new'])
        started = self.provider_action('start', accepted['epoch'])
        self.assertTrue(started['execute_once'])
        self.assertFalse(started['execution_authorized'])
        self.assertFalse(self.provider_action('start', accepted['epoch'])['execute_once'])
        settled = self.provider_action('settle', started['epoch'])
        self.assertEqual('provider_reported', settled['execution_verification'])
        self.assertFalse(settled['execution_authorized'])
        self.assertEqual(1, self.remaining())

    def test_fencing_blocks_new_start_but_not_unknown_reporting_or_settlement(self):
        self.reserve()
        accepted = self.provider_action('accept', 1)
        with self.api.store.transaction() as db:
            db.execute('UPDATE leader SET epoch=epoch+1')
        with self.assertRaises(Conflict):
            self.provider_action('start', accepted['epoch'])
        unknown = self.provider_action('unknown', accepted['epoch'])
        self.now += 120
        inspected = self.action('inspect', {'operation_id': 'operation'})
        self.assertEqual('unknown', inspected['state'])
        self.assertTrue(inspected['capacity_held'])
        self.assertTrue(inspected['lease_expired'])
        self.assertEqual(0, self.remaining())
        self.assertEqual(0, self.action('expire', peer=self.owner)['expired_unaccepted'])
        settled = self.provider_action('settle', unknown['epoch'], outcome='stopped')
        self.assertEqual('stopped', settled['state'])
        self.assertEqual(1, self.remaining())

    def test_invalid_task_epoch_is_not_coerced_and_cancellation_is_bound(self):
        for epoch in (None, True, 1.0, '1'):
            with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                self.reserve(task_epoch=epoch)
        reserved = self.reserve()
        with self.assertRaises(PermissionError):
            self.action('cancel', {'operation_id': 'operation', 'epoch': reserved['epoch']}, self.other)
        cancelled = self.action('cancel', {'operation_id': 'operation', 'epoch': reserved['epoch']}, self.owner)
        self.assertEqual('cancelled', cancelled['state'])
        self.assertEqual(1, self.remaining())

    def test_revoke_prevents_provider_admission_and_decline_releases_pending(self):
        self.reserve()
        self.action('revoke_pool', {'id': 'shared', 'epoch': 1}, self.owner)
        with self.assertRaises(Conflict):
            self.provider_action('accept', 1)
        self.assertEqual('stopped', self.provider_action('decline', 1)['state'])
        inspected = self.action('inspect', {'operation_id': 'operation'})
        self.assertFalse(inspected['capacity_held'])

    def test_finite_journal_api_does_not_expose_arbitrary_native_methods(self):
        for action in ('_dispatch', '_task', 'transaction', '__init__', 'shell', 'execute', 'advertise'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.action(action)
        for body in (None, [], {'action': []}, {'action': 'pools', 'arguments': []},
                     {'action': 'pools', 'unexpected': True}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.api.dispatch('POST', '/v1/allocation/action', body, self.worker)
        with self.assertRaises(PermissionError):
            self.api.dispatch('GET', '/v1/allocation/action', {}, self.worker)

    def test_actual_http_bearer_binding_and_forbidden_requests(self):
        # Dedicated loopback-only fixture server; no native worker is launched.
        tokens = {'worker': 'w' * 40, 'provider': 'p' * 40, 'owner': 'o' * 40, 'viewer': 'v' * 40}
        config = dict(self.config, port=0, peers=[])
        for name, peer in (('worker', self.worker), ('provider', self.provider),
                           ('owner', self.owner), ('viewer', self.viewer)):
            token_file = Path(self.temp.name) / (name + '.token')
            fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(tokens[name])
            config['peers'].append(dict(peer, token_file=str(token_file)))
        ready, holder = threading.Event(), {}

        def on_ready(server, api, channel):
            api.store.clock = lambda: self.now
            holder.update(server=server, api=api)
            ready.set()

        thread = threading.Thread(target=serve, args=(config, on_ready), daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5), 'fixture server failed to become ready')
        address = 'http://127.0.0.1:%d/v1/allocation/action' % holder['server'].server_address[1]
        opener = build_opener(ProxyHandler({}))

        def post(token, body):
            request = Request(address, data=json.dumps(body).encode('utf8'),
                              headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            try:
                with opener.open(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode('utf8'))
            except HTTPError as error:
                with error:
                    return error.code, json.loads(error.read().decode('utf8'))

        try:
            self.assertEqual(401, post('incorrect', {'action': 'pools'})[0])
            self.assertEqual(403, post(tokens['viewer'], {'action': 'pools'})[0])
            self.assertEqual(403, post(tokens['worker'], {'action': 'expire'})[0])
            self.assertEqual(403, post(tokens['worker'], {'action': 'pools', 'actor': 'operator'})[0])
            self.assertEqual(403, post(tokens['worker'], {'action': 'pools', 'arguments': {'provider': 'node:cloud'}})[0])
            status, pools = post(tokens['worker'], {'action': 'pools'})
            self.assertEqual(200, status)
            self.assertFalse(pools['execution_authorized'])
            status, reserved = post(tokens['worker'], {'action': 'reserve', 'arguments': {
                'operation_id': 'http-operation', 'task_id': 'parent', 'task_epoch': self.task['epoch'],
                'plan': self.plan, 'lease_seconds': 40}})
            self.assertEqual(200, status)
            self.assertEqual('node:laptop', reserved['actor'])
            for name, expected in (('worker', 403), ('owner', 403), ('provider', 200)):
                status, result = post(tokens[name], {'action': 'accept', 'arguments': {
                    'operation_id': 'http-operation', 'capability_id': 'target',
                    'epoch': 1, 'receipt_id': 'http-receipt'}})
                self.assertEqual(expected, status)
                if status == 200:
                    self.assertFalse(result['execution_authorized'])
        finally:
            holder['server'].shutdown()
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'fixture server did not stop')


if __name__ == '__main__':
    unittest.main()
