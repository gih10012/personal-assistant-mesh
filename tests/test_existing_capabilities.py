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
                parent = source
                for component in Path(relative).parts[:-1]:
                    parent = parent / component
                    parent.mkdir(mode=0o700, exist_ok=True)
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

    def test_install_nested_directories_private_with_umask_0002(self):
        bundle = adapter.build_bundle(self.inventory)
        destination = self.root / 'group-umask-install'
        original_umask = os.umask(0o002)
        try:
            receipt = adapter.install_bundle(bundle, destination)
        finally:
            observed_umask = os.umask(original_umask)
        self.assertEqual(0o002, observed_umask)  # Installer never changes umask.
        for directory, directories, files in os.walk(destination):
            self.assertEqual(0o700, Path(directory).stat().st_mode & 0o777)
            for name in files:
                self.assertEqual(0o600, (Path(directory) / name).stat().st_mode & 0o777)
        self.assertEqual(9, len(receipt['files']))

    def test_explicit_resume_checks_complete_partial_tree_before_adding_missing(self):
        bundle = adapter.build_bundle(self.inventory)
        completed = self.root / 'receipt-source'
        receipt = adapter.install_bundle(bundle, completed)
        expected = {item['path']: (completed / item['path']).read_bytes()
                    for item in receipt['files']}
        initial = list(expected)[:4]

        def partial(name):
            destination = self.root / name
            destination.mkdir(mode=0o700)
            for relative in initial:
                path = destination / relative
                adapter._make_private_parents(destination, Path(relative).parent)
                adapter._write_new(path, expected[relative])
            bad_directory = destination / 'ncut-web-api/references/capabilities'
            bad_directory.mkdir(mode=0o775)
            bad_directory.chmod(0o775)  # Exact legacy parents=True/umask0002 defect.
            return destination, bad_directory

        destination, bad_directory = partial('original-partial-target')
        before = {name: ((destination / name).stat().st_ino,
                         (destination / name).stat().st_mtime_ns)
                  for name in initial}
        with self.assertRaises(adapter.CapabilityAdaptationError):
            adapter.install_bundle(bundle, destination, resume_exact=True)
        self.assertFalse((destination / 'installation.json').exists())
        self.assertFalse((destination / receipt['files'][4]['path']).exists())
        # Only the owner repairs its exact known partial-install directory;
        # resume itself neither chmods nor accepts the broader old permission.
        self.assertEqual(0o775, bad_directory.stat().st_mode & 0o777)
        bad_directory.chmod(0o700)
        original_umask = os.umask(0o002)
        try:
            self.assertEqual(receipt, adapter.install_bundle(bundle, destination, resume_exact=True))
        finally:
            self.assertEqual(0o002, os.umask(original_umask))
        for name, observation in before.items():
            path = destination / name
            self.assertEqual(observation, (path.stat().st_ino, path.stat().st_mtime_ns))
        receipt_path = destination / 'installation.json'
        receipt_observation = (receipt_path.stat().st_ino, receipt_path.stat().st_mtime_ns)
        self.assertEqual(receipt, adapter.install_bundle(bundle, destination, resume_exact=True))
        self.assertEqual(receipt_observation, (receipt_path.stat().st_ino, receipt_path.stat().st_mtime_ns))

        # Each refusal leaves all missing members missing: no prefix write is
        # allowed before discovering a late extra/link/content/receipt problem.
        for kind in ('extra', 'symlink', 'hardlink', 'mismatch', 'receipt', 'early-receipt'):
            with self.subTest(kind=kind):
                target, private_directory = partial('refused-' + kind)
                private_directory.chmod(0o700)
                path = target / initial[0]
                if kind == 'extra':
                    adapter._write_new(target / 'extra.json', b'not a bundle member')
                elif kind in ('symlink', 'hardlink'):
                    path.unlink()
                    if kind == 'symlink':
                        path.symlink_to(completed / initial[0])
                    else:
                        os.link(str(completed / initial[0]), str(path))
                elif kind == 'mismatch':
                    path.write_bytes(b'changed source must never be overwritten')
                else:
                    adapter._write_new(target / 'installation.json',
                        adapter._encoded(receipt) if kind == 'early-receipt' else b'{}')
                with self.assertRaises(ValueError):
                    adapter.install_bundle(bundle, target, resume_exact=True)
                self.assertFalse((target / receipt['files'][4]['path']).exists())


if __name__ == '__main__':
    unittest.main()
