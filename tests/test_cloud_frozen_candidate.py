"""Source-only build plus isolated frozen Store runtime checks, no provider."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_legacy_fenced_receiver import _source_base


REPOSITORY = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(REPOSITORY / 'deploy' / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROBE = r'''
import importlib.util, io, json, sqlite3, sys, unittest
from pathlib import Path
from assistant_mesh import server
from assistant_mesh.legacy_fenced_receiver import _check_frozen_source
_check_frozen_source(server)
assert Path(server.__file__).resolve().parent == Path.cwd() / 'assistant_mesh'
original_connect = sqlite3.connect
def no_database(*args, **kwargs):
    raise AssertionError('source import must not open any SQLite state')
sqlite3.connect = no_database
try:
    from assistant_mesh.cloud_owner_runtime import CloudOwnerRuntime, install_owner
    from assistant_mesh.entrance_runtime import EntranceWorker, serve_entrance
    from assistant_mesh.claw_cli import claw_store_class
    from assistant_mesh.claw_upload import uploader_from_config
finally:
    sqlite3.connect = original_connect
assert not any(name == 'mcp' or name.startswith(('mcp.', 'fastmcp')) for name in sys.modules)
spec = importlib.util.spec_from_file_location('_frozen_owner_runtime_checks', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
output = io.StringIO()
result = unittest.TextTestRunner(stream=output).run(
    unittest.defaultTestLoader.loadTestsFromTestCase(module.CloudOwnerRuntimeTests))
assert result.wasSuccessful(), output.getvalue()
assert result.testsRun == 8 and not result.skipped, output.getvalue()
print(json.dumps({'actual_frozen_store': True, 'import_without_database_or_sdk': True,
    'runtime_tests': result.testsRun, 'runtime_skips': len(result.skipped),
    'model_calls': 0, 'production_state_opened': False}))
'''


class CloudFrozenCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prepared = tempfile.TemporaryDirectory(prefix='mesh-cloud-frozen-base.')
        cls.addClassCleanup(cls.prepared.cleanup)
        root = Path(cls.prepared.name)
        base = root / 'historical' / 'assistant_mesh'
        base.mkdir(parents=True, mode=0o700)
        for name, data in _source_base().items():
            (base / name).write_bytes(data)
        entrance = _load('_cloud_existing_entrance_builder', 'build-entrance-candidate.py')
        entrance.build(base.parent, root / 'existing', REPOSITORY)
        cls.files = {path.name: path.read_bytes() for path in (root / 'existing' / 'assistant_mesh').glob('*.py')}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mesh-cloud-frozen-build.')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / 'base'
        package = self.base / 'assistant_mesh'
        package.mkdir(parents=True, mode=0o700)
        for name, data in self.files.items():
            (package / name).write_bytes(data)
        (self.base / 'original_owner_runner.py').write_text('ORIGINAL_READY_HOOK = True\n')
        (self.base / 'private-config.json').write_text('{"not_source":true}\n')
        self.builder = _load('_cloud_source_candidate_builder', 'build-cloud-owner-candidate.py')
        self.pin = self.builder.inspect_base(self.base)['base_manifest_sha256']

    def test_frozen_bytes_and_hooks_preserved_and_eight_runtime_checks_execute(self):
        target = self.root / 'candidate'
        report = self.builder.build(self.base, target, self.pin, REPOSITORY)
        self.assertEqual(15, len(report['overlay_modules']))
        self.assertEqual(['config', 'entrance_runtime', 'store', 'worker'], report['preserved_import_boundaries'])
        for name, data in self.files.items():
            if name not in self.builder.SUPPORT_SHA256:
                self.assertEqual(data, (target / 'assistant_mesh' / name).read_bytes(), name)
        for name, expected in self.builder.CORE_SHA256.items():
            self.assertEqual(expected, hashlib.sha256((target / 'assistant_mesh' / name).read_bytes()).hexdigest())
        self.assertEqual((self.base / 'original_owner_runner.py').read_bytes(),
                         (target / 'original_owner_runner.py').read_bytes())
        self.assertFalse((target / 'private-config.json').exists())
        manifest = (target / 'cloud-owner-candidate.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'destination_must_be_new'):
            self.builder.build(self.base, target, self.pin, REPOSITORY)
        self.assertEqual(manifest, (target / 'cloud-owner-candidate.json').read_bytes())
        probe = subprocess.run([sys.executable, '-S', '-B', '-c', PROBE,
            str(REPOSITORY / 'tests' / 'test_cloud_owner_runtime.py')], cwd=str(target),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=20)
        self.assertEqual(0, probe.returncode, probe.stderr)
        result = json.loads(probe.stdout)
        self.assertEqual(8, result['runtime_tests'])
        self.assertEqual(0, result['runtime_skips'])
        self.assertTrue(result['actual_frozen_store'])
        self.assertTrue(result['import_without_database_or_sdk'])

    def test_wrong_base_pin_or_modern_core_cannot_create_destination(self):
        target = self.root / 'candidate'
        with self.assertRaisesRegex(ValueError, 'base_manifest_changed'):
            self.builder.build(self.base, target, '0' * 64, REPOSITORY)
        self.assertFalse(target.exists())
        # Even a newly calculated package manifest cannot bless a different
        # Store/server/Worker implementation as the already reviewed core.
        (self.base / 'assistant_mesh' / 'store.py').write_bytes(
            (REPOSITORY / 'assistant_mesh' / 'store.py').read_bytes())
        with self.assertRaisesRegex(ValueError, 'frozen_core_changed'):
            self.builder.inspect_base(self.base)
        self.assertFalse(target.exists())

    def test_support_hash_and_unreviewed_relative_import_fail_before_output(self):
        support = self.root / 'support'
        package = support / 'assistant_mesh'
        package.mkdir(parents=True, mode=0o700)
        for name in self.builder.SUPPORT_SHA256:
            (package / name).write_bytes((REPOSITORY / 'assistant_mesh' / name).read_bytes())
        path = package / 'mcp_http.py'
        changed = path.read_bytes() + b'\nfrom .server import API\n'
        path.write_bytes(changed)
        target = self.root / 'candidate'
        with self.assertRaisesRegex(ValueError, 'support_pin_changed'):
            self.builder.build(self.base, target, self.pin, support)
        self.assertFalse(target.exists())
        # A future pin update still must explicitly review any new closure edge.
        self.builder.SUPPORT_SHA256['mcp_http.py'] = hashlib.sha256(changed).hexdigest()
        with self.assertRaisesRegex(ValueError, 'pure_library_closure_changed'):
            self.builder.build(self.base, target, self.pin, support)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
