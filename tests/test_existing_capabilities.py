import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from assistant_mesh import existing_capabilities as adapter
from assistant_mesh.networking import Network
from assistant_mesh.remote import Remote
from assistant_mesh.resources import Registry
from assistant_mesh.store import Conflict, Store


class ExistingCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.selection = {'format': adapter.FORMAT, 'provider': 'laptop', 'revision': 1,
                          'sources': [], 'dependencies': {
                              'ncut.account': {'ref': 'private:owner-campus-session',
                                               'state': 'configured_not_verified'}}}
        sources = {
            'ncut-web-api': {'SKILL.md': 'read local campus contract',
                'scripts/ncut.py': 'print("not executed")', 'scripts/knowledge.py': 'pass',
                'references/services.json': '{"services":[]}',
                'references/capabilities/example.md': '---\nid: "example"\n---\n'},
            'xiumi-native-clipboard': {'SKILL.md': 'read format before authoring',
                'scripts/xiumi_components.py': 'pass', 'scripts/xiumi_clipboard.py': 'pass',
                'references/format.md': 'synthetic format contract'},
            'niri-computer-use': {'SKILL.md': 'begin/wake/end; preserve owner windows'},
        }
        for name, files in sources.items():
            source = self.root / name
            source.mkdir(mode=0o700)
            for relative, content in files.items():
                path = source / relative
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_text(content)
                path.chmod(0o600)
            # Not selected: a source tree is not an export permission.
            private = source / 'accounts'
            private.mkdir(mode=0o700)
            (private / 'owner.json').write_text('DO_NOT_EXPORT_AUTH_SENTINEL')
            self.selection['sources'].append({'id': name, 'root': str(source),
                'files': [{'path': relative} for relative in files]})
        self.inventory = adapter.pin_selection(self.selection)

    def tearDown(self):
        self.temp.cleanup()

    def test_action_portability_and_existing_registry_declarations(self):
        view = adapter.inspect_inventory(self.inventory)
        self.assertEqual(7, len(view['actions']))
        portability = {item['id']: item['portability'] for item in view['actions']}
        self.assertEqual('portable', portability['ncut.knowledge'])
        self.assertEqual('provider', portability['ncut.business'])
        self.assertEqual('portable', portability['xiumi.validate'])
        self.assertEqual('provider', portability['xiumi.localize'])
        self.assertEqual('provider', portability['niri.observe'])
        remote = adapter.advertisements(self.inventory, 'cloud')
        self.assertEqual({'ncut.knowledge', 'xiumi.compose', 'xiumi.validate'},
                         {item['arguments']['spec']['id'] for item in remote['requests']})
        self.assertNotIn(str(self.root), json.dumps(remote))
        self.assertNotIn('private:owner-campus-session', json.dumps(remote))
        store = Store(self.root / 'registry.sqlite')
        registry = Registry(store)
        for item in remote['requests']:
            row = registry.advertise('node:cloud', **item['arguments'])
            self.assertEqual('declared', row['verification'])
            self.assertFalse(row['spec']['runtime_verified'])
            self.assertFalse(row['spec']['managed_use_authorized'])
        self.assertEqual(3, len(registry.discover()['capabilities']))

    def test_private_bundle_install_preserves_code_but_no_account_or_device_backend(self):
        value = adapter.build_bundle(self.inventory)
        encoded = adapter._encoded(value)
        self.assertNotIn(str(self.root).encode(), encoded)
        self.assertNotIn(b'private:owner-campus-session', encoded)
        self.assertNotIn(b'DO_NOT_EXPORT_AUTH_SENTINEL', encoded)
        self.assertNotIn('niri-computer-use', value['sources'])
        destination = self.root / 'installed'
        receipt = adapter.install_bundle(value, destination)
        self.assertFalse(receipt['profile_changed'])
        self.assertFalse(receipt['authentication_copied'])
        self.assertFalse(receipt['runtime_verified'])
        for item in receipt['files']:
            path = destination / item['path']
            self.assertEqual(item['sha256'], adapter._sha(path.read_bytes()))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
        with self.assertRaises(FileExistsError):
            adapter.install_bundle(value, destination)

    def test_drift_links_private_paths_and_bundle_traversal_fail_before_install(self):
        source = self.inventory['sources'][0]
        target = Path(source['root']) / 'scripts/knowledge.py'
        target.write_text('changed')
        with self.assertRaises(adapter.CapabilityAdaptationError):
            adapter.inspect_inventory(self.inventory)
        self.inventory = adapter.pin_selection(self.selection)
        target.unlink()
        target.symlink_to(Path(source['root']) / 'scripts/ncut.py')
        with self.assertRaises((adapter.CapabilityAdaptationError, OSError)):
            adapter.inspect_inventory(self.inventory)
        for value in ('../state.json', 'accounts/owner.json', 'scripts/cookie.json',
                      'references/../accounts.json', 'scripts/x\\bad.py', '/SKILL.md'):
            with self.subTest(value=value):
                with self.assertRaises(adapter.CapabilityAdaptationError):
                    adapter._relative(value)
        target.unlink()
        target.write_text('pass')
        self.inventory = adapter.pin_selection(self.selection)
        bundle = adapter.build_bundle(self.inventory)
        bundle['files'][0]['path'] = 'ncut-web-api/../escape.py'
        destination = self.root / 'invalid-install'
        with self.assertRaises(adapter.CapabilityAdaptationError):
            adapter.install_bundle(bundle, destination)
        self.assertFalse(destination.exists())

    def test_delegate_uses_existing_atomic_ledger_and_original_operation_identity(self):
        now = 1000
        store = Store(self.root / 'remote.sqlite', clock=lambda: now)
        network = Network(store, 'cloud')
        remote = Remote(store, network)
        store.heartbeat('cloud', ['leader', 'agent'])
        parent = store.create_task('owner authorized read-only windows')
        task = store.claim('cloud')
        request = adapter.delegate_request(self.inventory, parent, task['epoch'],
            'original-window-observation', 'niri.observe', {'include_titles': False})
        self.assertEqual('laptop', request['peer'])
        self.assertIn('original-window-observation', request['arguments']['input'])
        self.assertIn('single-operator', request['arguments']['input'])
        first = remote.delegate(node='cloud', **request)
        self.assertEqual(first, remote.delegate(node='cloud', **request))
        self.assertEqual('waiting_remote', first['status'])
        with store.transaction() as db:
            self.assertEqual(2, db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM mesh_deliveries').fetchone()[0])
        changed = adapter.delegate_request(self.inventory, parent, task['epoch'],
            'original-window-observation', 'niri.observe', {'include_titles': True})
        with self.assertRaises(Conflict):
            remote.delegate(node='cloud', **changed)
        with self.assertRaises(Conflict):
            remote.delegate(node='cloud', **dict(request, epoch=task['epoch'] + 1))

    def test_private_cli_hash_pin_and_no_overwrite(self):
        owner = self.root / 'owner.json'
        raw = adapter._encoded(self.inventory)
        adapter._write_new(owner, raw)
        value, fingerprint = adapter._private_json(owner, expected_sha=adapter._sha(raw))
        self.assertEqual(self.inventory, value)
        self.assertEqual(adapter._sha(raw), fingerprint)
        with self.assertRaises(adapter.CapabilityAdaptationError):
            adapter._private_json(owner, expected_sha='0' * 64)
        with self.assertRaises(FileExistsError):
            adapter._write_new(owner, b'overwrite')
        self.assertEqual(raw, owner.read_bytes())
        owner.chmod(0o644)
        with self.assertRaises(adapter.CapabilityAdaptationError):
            adapter._private_json(owner)


if __name__ == '__main__':
    unittest.main()
