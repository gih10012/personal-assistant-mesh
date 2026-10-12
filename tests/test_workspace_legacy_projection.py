"""Two read-projection modules over the exact old3198 package, never production.

No modern Store/Worker/server is substituted. Original source unavailable means
an explicit skip, not compatibility success. All authority writes create fresh
isolated fixture rows; snapshots then prove read projections leave them intact.
"""
import ast
import contextlib
import copy
import hashlib
import importlib
import importlib.abc
import importlib.util
import io
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from assistant_mesh.workspace import MeshWorkspace
from tests.test_workspace import BoundClient


BASE = '3198c877234a8fd0c6912fdbe218f47185effedc'
ROOT = Path(__file__).resolve().parents[1]
PREFIX = '_pam_exact3198_projection_fixture'
HASHES = {
    'assistant_mesh/store.py': '7d8f750e8dfe10045ca97b52ac0ff853d645ea66b4b01f9a9aaf60fb48df5440',
    'assistant_mesh/worker.py': '816a8eb2f6bc5a24a22ae345b1b7b94b0dd5d0ce52f99a6658688e683ce9cbf0',
    'assistant_mesh/routing.py': 'b59a63dd70473285ed241d0495d9ce33afaad39c013c38bd4c6d1a22aec2e2e5',
    'assistant_mesh/routing_context.py': '65763f5df31a085bdc45af3d8f9dcd6159b91725f4673039e037567f0c07474b',
}


def original_sources():
    provided = os.environ.get('PAM_LEGACY_BASE_ROOT')
    if provided:
        package = Path(provided) / 'assistant_mesh'
        result = {'assistant_mesh/' + str(path.relative_to(package)): path.read_bytes()
                  for path in package.rglob('*.py')}
    else:
        try:
            archive = subprocess.run(['git', 'archive', BASE, 'assistant_mesh'],
                                     cwd=str(ROOT), stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise unittest.SkipTest('exact3198 archive unavailable; set PAM_LEGACY_BASE_ROOT')
        if archive.returncode:
            raise unittest.SkipTest('exact3198 git object unavailable; no modern package substitution')
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode='r:') as source:
            result = {item.name: source.extractfile(item).read() for item in source.getmembers()
                      if item.isfile() and item.name.endswith('.py')}
    for name, expected in HASHES.items():
        if name not in result or hashlib.sha256(result[name]).hexdigest() != expected:
            raise AssertionError('exact3198 source hash mismatch: ' + name)
    return result


class OldPackageLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """In-memory old namespace; only the two nominated modules are overlaid."""
    def __init__(self, sources):
        self.sources = dict(sources)
        for name in ('routing.py', 'routing_context.py'):
            self.sources['assistant_mesh/' + name] = (ROOT / 'assistant_mesh' / name).read_bytes()
        self.loaded_sources = {}

    def find_spec(self, fullname, path=None, target=None):
        if fullname != PREFIX and not fullname.startswith(PREFIX + '.'):
            return None
        suffix = fullname[len(PREFIX):].replace('.', '/')
        filename = 'assistant_mesh' + suffix + '.py'
        if fullname == PREFIX:
            filename = 'assistant_mesh/__init__.py'
        if filename not in self.sources:
            raise ImportError('old3198 module missing; current package fallback forbidden')
        spec = importlib.util.spec_from_loader(fullname, self, is_package=fullname == PREFIX)
        spec.origin = '<exact3198-or-two-read-projections:' + filename + '>'
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        suffix = module.__name__[len(PREFIX):].replace('.', '/')
        name = 'assistant_mesh' + suffix + '.py'
        if module.__name__ == PREFIX:
            name = 'assistant_mesh/__init__.py'
        source = self.sources[name]
        self.loaded_sources[name] = source
        module.__file__ = module.__spec__.origin
        exec(compile(source, module.__file__, 'exec'), module.__dict__)


class WorkspaceLegacyProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = original_sources()
        cls.loader = OldPackageLoader(cls.original)
        sys.meta_path.insert(0, cls.loader)
        try:
            cls.Server = importlib.import_module(PREFIX + '.server')
            cls.Worker = importlib.import_module(PREFIX + '.worker')
        except BaseException:
            sys.meta_path.remove(cls.loader)
            for name in tuple(sys.modules):
                if name == PREFIX or name.startswith(PREFIX + '.'):
                    del sys.modules[name]
            raise

    @classmethod
    def tearDownClass(cls):
        sys.meta_path.remove(cls.loader)
        for name in tuple(sys.modules):
            if name == PREFIX or name.startswith(PREFIX + '.'):
                del sys.modules[name]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh-old3198-projection.')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        # Fresh fixture ONLY. Never instantiate this API/Store on a live ledger:
        # the original constructor still performs inflight recovery/migrations.
        self.api = self.Server.API({'database': str(self.root / 'fresh-authority.db'),
            'peers': [], 'node_id': 'cloud', 'network_peers': {'laptop': {'send_allowed': True}}})
        self.api.store.clock = lambda: 1000.0
        self.api.store.heartbeat('worker', ['leader', 'agent'])
        self.api.store.create_task('isolated legacy projection fixture', task_id='parent')
        self.task = self.api.store.claim('worker')
        self.client = BoundClient(self.api)
        self.workspace = MeshWorkspace(self.client, str(self.root / 'workspace.db'),
            'cloud', 'worker', 'parent', self.task['epoch'])
        self.arguments = {'issuer': 'laptop', 'operation_id': 'original-op', 'decision_id': 'original-choice',
            'selection': {'capability_id': 'declared-campus-cap', 'epoch': 1,
                'action': 'network.campus.http', 'scope': {'operation': 'get', 'url': 'http://10.42.1.5/fixture'},
                'workload': {'kind': 'campus_http', 'max_bytes': 1024, 'timeout_seconds': 5},
                'observations': [{'observation_id': 'fixture-measurement', 'metric': 'reachability',
                                  'unit': 'bool', 'max_age_seconds': 60}]},
            'candidates': [{'issuer': 'laptop', 'capability_id': 'declared-campus-cap', 'epoch': 1}],
            'rationale': 'Isolated old source compatibility only.',
            'project_id': 'mesh-self-maintenance', 'agent_id': 'network-specialist'}

    def snapshot(self):
        uri = Path(self.api.store.path).as_uri() + '?mode=ro'
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
            return tuple(db.iterdump())

    def test_exact_old_store_worker_server_and_all_non_projection_sources_are_preserved(self):
        self.assertTrue(self.loader.loaded_sources)
        for name, loaded in self.loader.loaded_sources.items():
            if name not in ('assistant_mesh/routing.py', 'assistant_mesh/routing_context.py'):
                self.assertEqual(self.original[name], loaded, name)
        self.assertEqual(HASHES['assistant_mesh/store.py'], hashlib.sha256(
            self.loader.loaded_sources['assistant_mesh/store.py']).hexdigest())
        self.assertEqual(HASHES['assistant_mesh/worker.py'], hashlib.sha256(
            self.loader.loaded_sources['assistant_mesh/worker.py']).hexdigest())

    def test_only_two_read_method_asts_change_no_constructor_schema_or_native_logic(self):
        allowed = {'routing.py': ('Routing', '_reference'),
                   'routing_context.py': ('RoutingContext', 'read')}
        for name, (class_name, method_name) in allowed.items():
            before = ast.parse(self.original['assistant_mesh/' + name])
            after = ast.parse((ROOT / 'assistant_mesh' / name).read_bytes())
            original_class = next(item for item in before.body if isinstance(item, ast.ClassDef) and item.name == class_name)
            changed_class = next(item for item in after.body if isinstance(item, ast.ClassDef) and item.name == class_name)
            method = next(item for item in original_class.body if isinstance(item, ast.FunctionDef) and item.name == method_name)
            index = next(i for i, item in enumerate(changed_class.body) if isinstance(item, ast.FunctionDef) and item.name == method_name)
            changed_class.body[index] = copy.deepcopy(method)
            self.assertEqual(ast.dump(before, include_attributes=False), ast.dump(after, include_attributes=False), name)

    def test_new_workspace_can_link_using_both_actual_original_fingerprints_on_old_authority(self):
        result = self.workspace.call(**self.arguments)
        self.assertEqual('observed', result['phase'])
        self.assertEqual({'propose': 'confirmed', 'delegate': 'confirmed', 'link': 'confirmed'}, result['stages'])
        row = self.workspace._row('original-op')
        linked = result['routing']['linked_execution']
        self.assertEqual(self.workspace._delegation_fingerprint(row), linked['delegation_fingerprint'])
        old_digest = importlib.import_module(PREFIX + '.networking').digest
        self.assertEqual(old_digest(self.workspace._remote_message(row)), linked['message_fingerprint'])
        self.assertFalse(linked['execution_verified'])
        self.assertFalse(result['native_tools_intercepted'])

    def test_read_context_and_inspect_preserve_entire_original_fixture_schema_and_rows(self):
        self.workspace.call(**self.arguments)
        before = self.snapshot()
        context = self.workspace.discover(limit=100)
        remote = context['sections']['remote_execution_candidates']['records'][0]
        row = self.workspace._row('original-op')
        self.assertEqual(self.workspace._delegation_fingerprint(row), remote['delegation_fingerprint'])
        self.assertIsInstance(remote['message_fingerprint'], str)
        for unused in range(3):
            self.assertEqual('observed', self.workspace.status('original-op')['phase'])
        self.assertEqual(before, self.snapshot())

    def test_old_authority_lost_delegate_ack_reconciles_original_hash_without_repost(self):
        self.client.fail_after = 'delegate'
        self.assertEqual('unresolved', self.workspace.call(**self.arguments)['phase'])
        self.assertEqual('observed', self.workspace.call(**self.arguments)['phase'])
        self.assertEqual(1, self.client.calls.count('delegate'))
        self.assertEqual(1, self.client.calls.count('link'))

    def test_absent_delivery_keeps_delegation_unconfirmed_without_replay(self):
        self.client.fail_after = 'delegate'
        self.workspace.call(**self.arguments)
        with self.api.store.transaction() as db:
            db.execute('DELETE FROM mesh_deliveries')  # isolated fixture only
        before = self.snapshot()
        self.assertEqual('unresolved', self.workspace.call(**self.arguments)['phase'])
        self.assertEqual(1, self.client.calls.count('delegate'))
        self.assertEqual(0, self.client.calls.count('link'))
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
