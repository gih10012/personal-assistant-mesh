"""Routing integration with real private-token HTTP and native-tool passthrough."""
import contextlib
import copy
import io
import json
import urllib.error
import unittest
from unittest import mock

from assistant_mesh.cli import main
from assistant_mesh.worker import Client
from tests import test_mesh_gateway as fixture


class RoutingGatewayTests(unittest.TestCase):
    setUp = fixture.MeshGatewayTests.setUp
    tearDown = fixture.MeshGatewayTests.tearDown
    value = staticmethod(fixture.MeshGatewayTests.value)
    gateway = fixture.MeshGatewayTests.gateway
    count = fixture.MeshGatewayTests.count

    def decision(self):
        return {'decision_id': 'decision-fixture', 'work': {'action': 'read-only-fixture'},
                'candidates': [{'peer': 'authorized-peer'}], 'selection': {'peer': 'authorized-peer'},
                'rationale': 'Fixture evidence is declared only.', 'evidence_refs': []}

    def direct(self, action, arguments=None, **extra):
        return self.worker.client.request('/v1/routing/action', dict(task_id=self.parent_id,
            epoch=self.worker.current['epoch'], action=action, arguments=arguments or {}, **extra))

    def peer(self, role, node=None):
        return {'role': role, 'node': node} if node else {'role': role}

    def cli(self, role, *args):
        config = self.root / (role + '-routing.json')
        config.write_text(json.dumps({'control_url': self.worker_config['control_url'],
                                      'token_file': str(self.root / (role + '.token'))}))
        config.chmod(0o600)
        stream = io.StringIO()
        with mock.patch('sys.argv', ['mesh', '--config', str(config)] + list(args)), contextlib.redirect_stdout(stream):
            main()
        return json.loads(stream.getvalue())

    def test_context_propose_inspect_list_and_owner_read_real_http(self):
        context = self.value(self.gateway('routing_context'))
        self.assertEqual(self.parent_id, context['task']['id'])
        self.assertFalse(context['model_ranking_applied'])
        first = self.value(self.gateway('route_propose', {'decision': self.decision()}))
        self.assertTrue(first['proposal_created'])
        self.assertFalse(first['execution_verified'])
        self.assertFalse(self.value(self.gateway('route_propose', {'decision': self.decision()}))['proposal_created'])
        inspected = self.value(self.gateway('route_inspect', {'decision_id': 'decision-fixture'}))
        self.assertEqual(first['content_sha256'], inspected['content_sha256'])
        self.assertEqual(1, len(self.value(self.gateway('route_list'))['decisions']))
        self.assertEqual(1, len(self.operator.request('/v1/routing/decisions?task_id=' + self.parent_id)['decisions']))
        self.assertEqual(1, self.count('tasks'))
        self.assertEqual(0, self.count('mesh_deliveries'))
        self.assertEqual(0, self.count('managed_allocations'))

    def test_outer_auth_roles_are_not_forged_by_node_field(self):
        payload = {'task_id': self.parent_id, 'epoch': self.worker.current['epoch'],
                   'action': 'context', 'arguments': {}}
        for role in ('operator', 'viewer', 'agent_peer', 'unknown'):
            with self.assertRaises(PermissionError):
                self.api.dispatch('POST', '/v1/routing/action', payload, self.peer(role, 'local'))
        for role in ('worker', 'agent_peer', 'unknown'):
            with self.assertRaises(PermissionError):
                self.api.dispatch('GET', '/v1/routing/decisions', {}, self.peer(role, 'local'))
        self.api.dispatch('GET', '/v1/routing/decisions', {}, self.peer('viewer'))
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.worker.client.request('/v1/routing/decisions')
        self.assertEqual(403, failure.exception.code)

    def test_task_actor_authority_and_action_injection_rejected_without_write(self):
        for field in ('task_id', 'epoch', 'node', 'authority', 'actor'):
            response = self.gateway('route_propose', {'decision': self.decision(), field: 'forged'})
            self.assertFalse(response['success'])
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.direct('propose', {'decision': self.decision()}, **{field: 'forged'}) if field not in ('task_id', 'epoch') else \
                    self.worker.client.request('/v1/routing/action', {
                        'task_id': 'forged' if field == 'task_id' else self.parent_id,
                        'epoch': 99 if field == 'epoch' else self.worker.current['epoch'],
                        'action': 'propose', 'arguments': {'decision': self.decision()}})
            self.assertIn(failure.exception.code, (400, 409))
        for payload in ({'action': 'propose', 'arguments': {}}, {'action': 'invoke', 'arguments': {}},
                        {'action': [], 'arguments': {}}, {'action': 'context', 'arguments': {'actor': 'operator'}}):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.worker.client.request('/v1/routing/action', dict(payload,
                    task_id=self.parent_id, epoch=self.worker.current['epoch']))
            self.assertEqual(400, failure.exception.code)
        self.assertEqual(0, self.count('route_decisions'))

    def test_link_real_child_preserves_identity_no_model_execution_proof(self):
        self.gateway('route_propose', {'decision': self.decision()})
        child = self.value(self.gateway('remote_delegate', {'peer': 'authorized-peer', 'input': 'fixture only'}, call_id='stable-child'))
        linked = self.value(self.gateway('route_link', {'decision_id': 'decision-fixture',
            'kind': 'remote_delegation', 'reference_id': child['id']}))
        self.assertTrue(linked['link_created'])
        self.assertEqual(child['id'], linked['linked_execution']['reference_id'])
        self.assertFalse(linked['model_selection_verified'])
        self.assertFalse(linked['linked_execution']['external_outcome_verified'])
        self.assertEqual(1, self.count('mesh_deliveries'))
        self.assertEqual(2, self.count('tasks'))
        self.assertFalse(self.value(self.gateway('route_link', {'decision_id': 'decision-fixture',
            'kind': 'remote_delegation', 'reference_id': child['id']}))['link_created'])

    def test_changed_proposal_conflict_keeps_current_lease_and_original_decision(self):
        self.gateway('route_propose', {'decision': self.decision()})
        changed = self.decision()
        changed['selection'] = {'peer': 'different'}
        response = self.gateway('route_propose', {'decision': changed})
        self.assertFalse(response['success'])
        self.assertEqual(409, self.value(response)['http_status'])
        self.assertFalse(self.value(response)['retry_with_new_id'])
        self.assertTrue(self.gateway('route_inspect', {'decision_id': 'decision-fixture'})['success'])
        self.assertEqual(1, self.count('route_decisions'))

    def test_stale_worker_rejected_but_owner_historical_read_survives_completion(self):
        self.gateway('route_propose', {'decision': self.decision()})
        self.api.store.update_task(self.parent_id, 'local', self.worker.current['epoch'], result='fixture done', status='completed')
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.direct('list')
        self.assertEqual(409, failure.exception.code)
        self.assertEqual(1, len(self.operator.request('/v1/routing/decisions?decision_id=decision-fixture')['decisions']))

    def test_unknown_transport_does_not_retry_replace_thread_or_intercept_native_tools(self):
        with mock.patch.object(self.worker, 'tick'), \
                mock.patch.object(self.worker.client, 'request', side_effect=OSError('fixture secret transport')) as request:
            response = self.gateway('route_propose', {'decision': self.decision()})
        output = self.value(response)
        self.assertFalse(response['success'])
        self.assertEqual('unknown', output['outcome'])
        self.assertFalse(output['automatic_retry'])
        self.assertFalse(output['native_tools_intercepted'])
        self.assertNotIn('fixture secret', json.dumps(output))
        self.assertEqual(1, request.call_count)
        reference = self.worker.resource_reference()
        self.assertIn('routing', reference)
        self.assertEqual(self.parent_id, reference['cli']['routing_task_binding']['task_id'])
        self.assertFalse(reference['routing']['managed_invocation_authorized'])

    def test_get_post_query_field_and_duplicate_rejections(self):
        for path in ('/v1/routing/decisions?actor=operator', '/v1/routing/decisions?limit=1&limit=2'):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.operator.request(path)
            self.assertEqual(400, failure.exception.code)
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.operator.request('/v1/routing/decisions', {})
        self.assertEqual(403, failure.exception.code)
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.worker.client.request('/v1/routing/action?authority=forged', {
                'task_id': self.parent_id, 'epoch': self.worker.current['epoch'], 'action': 'context', 'arguments': {}})
        self.assertEqual(400, failure.exception.code)

    def test_cli_private_payload_task_binding_and_owner_read(self):
        payload = self.root / 'decision.json'
        payload.write_text(json.dumps({'decision': self.decision()}))
        payload.chmod(0o600)
        result = self.cli('worker', 'routing', '--action', 'propose', '--payload-file', str(payload),
            '--task-id', self.parent_id, '--epoch', str(self.worker.current['epoch']))
        self.assertTrue(result['proposal_created'])
        self.assertEqual(1, len(self.cli('operator', 'route-decisions', '--id', 'decision-fixture')['decisions']))
        for args in (('routing', '--action', 'context', '--principal', 'operator'),
                     ('routing', '--action', 'context'), ('route-decisions', '--action', 'propose'),
                     ('route-decisions', '--epoch', '1'), ('route-decisions', '--kind', 'x')):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.cli('worker', *args)


if __name__ == '__main__':
    unittest.main()
