"""One synthetic entrypoint case; no actual runtime, ledger or service access."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BusinessReplicaOverlayTests(unittest.TestCase):
    def test_pinned_relocated_launchers_hook_before_constructor_and_preserve_argv(self):
        spec = importlib.util.spec_from_file_location('_business_overlay_builder',
            str(ROOT / 'deploy/build-business-replica-overlay.py'))
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory(prefix='amita-business-overlay.') as directory:
            root = Path(directory)
            actual = root / 'actual' / 'assistant_mesh'
            actual.mkdir(parents=True)
            original_init = b'# incumbent namespace bytes\n__version__ = "fixture"\n'
            (actual / '__init__.py').write_bytes(original_init)
            (actual / 'store.py').write_text(
                'hooked = False\nclass Store:\n    def __init__(self):\n'
                '        assert hooked, "constructor connection hook missing"\n')
            (actual / 'config.py').write_text(
                'def private_json(path):\n    return {"path": path}\n')
            (actual / 'cli.py').write_text(
                'import json, sys\nfrom .store import Store\n'
                'Store()\ndef main():\n    print(json.dumps(sys.argv[1:]))\n')
            support = root / 'support' / 'assistant_mesh'
            support.mkdir(parents=True)
            (support / 'business_journal.py').write_text(
                'def hook_store_type(cls):\n'
                '    import sys\n    sys.modules[cls.__module__].hooked = True\n'
                'def hook_store(store):\n    store.instance_hooked = True\n')
            (support / 'business_replica_api.py').write_text(
                'from types import SimpleNamespace\n'
                'def attach(api, policy):\n    assert api.store.instance_hooked\n'
                '    api.journal = SimpleNamespace(snapshot_ttl=0)\n'
                '    return api.journal\n')
            (support / 'business_replica.py').write_text('# passive module fixture\n')
            incumbent = root / 'launch_actual.py'
            incumbent.write_text(
                'from types import SimpleNamespace\nfrom assistant_mesh.store import Store\n'
                'Store()\n'
                'def ready(http_server, api, channel):\n'
                '    assert api.journal.snapshot_ttl == 600\n'
                '    print("original-ready")\n'
                'def main():\n    entry.ready(None, SimpleNamespace(store=Store()), None)\n'
                'entry = SimpleNamespace(ready=ready, main=main)\n'
                '_recovery_incumbent = SimpleNamespace(_incumbent=SimpleNamespace(previous=entry))\n')
            common = dict(package_init=actual / '__init__.py',
                init_sha256=hashlib.sha256(original_init).hexdigest(),
                installed_init=str(actual / '__init__.py'), support_root=support.parent)
            pins = root / 'pins.json'
            pins.write_text(json.dumps({str(actual / 'store.py'):
                hashlib.sha256((actual / 'store.py').read_bytes()).hexdigest()}))
            for component in ('receiver', 'node'):
                built = root / ('build-' + component)
                args = dict(common, component=component, destination=built, runtime_pins=pins)
                if component == 'receiver':
                    args.update(launcher=incumbent,
                        launcher_sha256=hashlib.sha256(incumbent.read_bytes()).hexdigest(),
                        installed_launcher=str(incumbent), policy_file='/private/policy.json')
                report = builder.build(**args)
                self.assertEqual({
                    'assistant_mesh/' + name + '.py' for name in builder.MODULES
                } | {'assistant_mesh/__init__.py', 'launch_' + component + '.py'},
                    set(report['module_sha256']))
                self.assertTrue((built / 'assistant_mesh/__init__.py').read_bytes().endswith(original_init))
                with self.assertRaisesRegex(ValueError, 'new_absolute_destination'):
                    builder.build(**args)
                moved = root / ('final-' + component)
                shutil.move(str(built), str(moved))
                argv = ['--config', '/private/node.json', 'node'] if component == 'node' else []
                result = subprocess.check_output([sys.executable, '-B',
                    str(moved / report['launcher'])] + argv, cwd=str(root), universal_newlines=True)
                if component == 'node':
                    self.assertEqual(json.loads(result), argv)
                else:
                    self.assertEqual(result.strip(), 'original-ready')
            (actual / 'store.py').write_text('# changed incumbent\n')
            rejected = subprocess.run([sys.executable, '-B', str(root / 'final-node/launch_node.py')],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('business_overlay_runtime_pin_changed', rejected.stderr)


if __name__ == '__main__':
    unittest.main()
