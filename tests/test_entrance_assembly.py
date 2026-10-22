"""Bounded assembly checks; no listener, private config, account or model.

Existing final/feed/transport semantics are not rerun here. These checks cover
the previously missing projection opt-in and sole-receiver composition only.
"""
import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from assistant_mesh.mcp_http_auth import NotConfigured
from tests.test_mcp_http import policy


ROOT = Path(__file__).resolve().parents[1]


def template(name):
    path = ROOT / 'deploy' / (name + '.py.example')
    spec = importlib.util.spec_from_loader('_fixture_' + name.replace('-', '_'),
        importlib.machinery.SourceFileLoader('_fixture_' + name.replace('-', '_'), str(path)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EntranceAssemblyTests(unittest.TestCase):
    def test_default_off_and_explicit_projection_share_the_original_context(self):
        assembly = template('work-events-factory-fragment')
        runtime = {'owner_id': 'owner-fixture', 'subject': 'owner-main-chat',
                   'ingress_client': {'existing': 'private-reference'}}
        oauth = {'ingress_clients': {runtime['subject']: runtime['ingress_client']}}
        owner_policy = policy(); owner_policy['subjects'] = owner_policy['subjects'][:1]
        backend, service, bridge = object(), object(), object()
        provider, server, app = object(), object(), object()
        with patch.object(assembly, 'load_context', return_value=(backend, service, bridge)) as load, \
             patch.object(assembly, 'validate_config', return_value={'policy': owner_policy}), \
             patch.object(assembly, 'build_app', return_value=(provider, server, app)) as build, \
             patch.object(assembly, 'enable_projection') as project, \
             patch.object(assembly, 'CurrentOAuthGrants') as grants:
            self.assertEqual((provider, server, app), assembly.assemble(oauth, runtime))
            project.assert_not_called(); grants.assert_not_called()
            load.assert_called_once_with(runtime)
            build.assert_called_once_with({}, backend=backend, conversation_service=service, event_service=None)
            load.reset_mock(); build.reset_mock()
            assembly.assemble(oauth, runtime, final_projection=True)
            load.assert_called_once_with(runtime)
            project.assert_called_once_with(bridge, enabled=True)
            grants.assert_not_called()
            with self.assertRaises(NotConfigured):
                assembly.assemble(oauth, runtime, final_projection='1')

    def test_receiver_retains_original_ready_checks_grant_and_uses_same_api(self):
        assembly = template('conversation-final-ready-fragment')
        calls, finals = [], object()
        policy = {'source': 'chatgpt-chat', 'subject': 'fixed-chat', 'node': 'original-leader'}
        projects = [{'source': 'chatgpt-chat', 'subject': 'fixed-chat', 'projects': ['explicit-project']}]
        original = lambda http, api, channel: calls.append('original')
        ready = assembly.compose_ready(original, sources=('chatgpt-chat',),
                                       reply_policies=[policy], event_projects=projects)
        api = NS(peers=[(b'SYNTHETIC_NOT_A_REAL_CREDENTIAL', {'role': 'worker',
            'node': 'original-leader', 'capabilities': [assembly.conversation_final.CAPABILITY]})])
        with patch.object(assembly.conversation_authority, 'attach', side_effect=lambda *a, **k: calls.append('authority')) as authority, \
             patch.object(assembly.conversation_emit, 'attach', side_effect=lambda *a, **k: calls.append('emit')), \
             patch.object(assembly.conversation_final, 'attach', side_effect=lambda *a, **k: calls.append('final') or finals), \
             patch.object(assembly, 'attach_event_feed', side_effect=lambda *a, **k: calls.append('feed')) as feed:
            ready(None, api, None)
            self.assertEqual(['original', 'authority', 'emit', 'final', 'feed'], calls)
            authority.assert_called_once_with(api, sources=('chatgpt-chat',))
            feed.assert_called_once_with(api, finals, enabled=True, policies=tuple(projects))
            calls.clear(); api.peers[0][1]['capabilities'].clear()
            with self.assertRaises(PermissionError):
                ready(None, api, None)
            self.assertEqual([], calls)  # Never grant or partly attach on missing role.

    def test_receiver_cannot_infer_projects_or_change_the_original_audience(self):
        assembly = template('conversation-final-ready-fragment')
        reply = [{'source': 'chatgpt-chat', 'subject': 'fixed-chat', 'node': 'original-leader'}]
        for kwargs in ({'sources': ('openai-codex-cloud',), 'reply_policies': reply},
            {'sources': ('chatgpt-chat',), 'reply_policies': reply,
             'event_projects': [{'source': 'chatgpt-chat', 'subject': 'another-chat', 'projects': ['explicit-project']}]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                assembly.compose_ready(lambda *args: None, **kwargs)

    def test_finite_executor_membership_snapshots_and_requires_each_existing_worker_grant(self):
        assembly = template('conversation-final-ready-fragment')
        policy = {'source': 'chatgpt-chat', 'subject': 'fixed-chat',
                  'nodes': ['original-leader', 'alternate-leader']}
        for nodes in ([], ['original-leader', 'original-leader'], ['*'], ['n' + str(i) for i in range(21)]):
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                assembly.compose_ready(lambda *args: None, sources=('chatgpt-chat',),
                                       reply_policies=[dict(policy, nodes=nodes)])
        calls = []
        ready = assembly.compose_ready(lambda *args: calls.append('original'), sources=('chatgpt-chat',),
                                       reply_policies=[policy])
        policy['nodes'].append('not-in-the-snapshot')
        api = NS(peers=[(b'SYNTHETIC', {'role': 'worker', 'node': 'original-leader',
                         'capabilities': [assembly.conversation_final.CAPABILITY]}),
                        (b'SYNTHETIC_ALTERNATE', {'role': 'worker', 'node': 'alternate-leader',
                         'capabilities': assembly.conversation_final.CAPABILITY})])
        with self.assertRaises(PermissionError):
            ready(None, api, None)
        self.assertEqual([], calls)
        api.peers[1][1]['capabilities'] = [assembly.conversation_final.CAPABILITY]
        with patch.object(assembly.conversation_authority, 'attach'), \
             patch.object(assembly.conversation_emit, 'attach'), \
             patch.object(assembly.conversation_final, 'attach') as attach:
            ready(None, api, None)
        self.assertEqual(['original'], calls)
        self.assertEqual(({'source': 'chatgpt-chat', 'subject': 'fixed-chat',
                           'nodes': ['alternate-leader', 'original-leader']},),
                         attach.call_args[1]['policies'])


if __name__ == '__main__':
    unittest.main()
