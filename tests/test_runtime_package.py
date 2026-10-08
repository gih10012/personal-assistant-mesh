"""Offline full-artifact installation fixtures; no models/network/auth access."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.runtime_admin import main as runtime_main
from scripts.upgrade_runtime import install_package, main as upgrade_main, verify_installation


class RuntimePackageTests(unittest.TestCase):
    version = '0.159.2'
    target = 'x86_64-unknown-linux-musl'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifact = self.root / 'codex-package-fixture.tar.gz'
        self.install_dir = self.root / 'installed'
        self.manifest = {'layoutVersion': 1, 'version': self.version, 'target': self.target,
                         'variant': 'codex', 'entrypoint': 'bin/codex',
                         'resourcesDir': 'codex-resources', 'pathDir': 'codex-path'}
        self.files = {'codex-package.json': (json.dumps(self.manifest).encode(), 0o644),
            'bin/codex': (b'#!/bin/sh\necho codex-cli 0.159.2\n', 0o755),
            'bin/codex-code-mode-host': (b'fixture code-mode host same release', 0o755),
            'codex-resources/zsh/bin/zsh': (b'fixture bundled shell', 0o755),
            'codex-resources/bwrap': (b'fixture bundled resource', 0o755),
            'codex-path/rg': (b'fixture bundled path resource', 0o755)}

    def tearDown(self):
        self.temporary.cleanup()

    def archive(self, files=None, prefix='', extra=None):
        with tarfile.open(str(self.artifact), 'w:gz') as archive:
            for name, (body, mode) in (self.files if files is None else files).items():
                member = tarfile.TarInfo(prefix + name)
                member.size, member.mode = len(body), mode
                archive.addfile(member, io.BytesIO(body))
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b'x') if extra.isfile() else None)
        return hashlib.sha256(self.artifact.read_bytes()).hexdigest()

    def install(self, digest=None, **changes):
        return install_package(self.artifact, digest or self.archive(), changes.get('version', self.version),
                               changes.get('target', self.target), self.install_dir)

    def config(self):
        path = self.root / 'worker.json'
        value = {'codex': {'executable': '/previous/complete-package/bin/codex', 'model': 'previous-selection',
                          'sandbox': 'danger-full-access', 'approval_policy': 'never'},
                 'private_sentinel': 'PRIVATE_CONFIG_DO_NOT_PRINT'}
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path, value

    def switch(self, path, package, digest, **changes):
        argv = ['runtime_admin', '--config', str(path), 'set-cloud-runtime',
                '--package-dir', package['package_dir'], '--sha256', digest]
        for name, value in changes.items():
            argv += ['--' + name.replace('_', '-'), str(value)]
        output = io.StringIO()
        with patch('sys.argv', argv), contextlib.redirect_stdout(output):
            runtime_main()
        return json.loads(output.getvalue())

    def test_complete_official_layout_is_preserved_with_whole_artifact_not_binary_hash(self):
        digest = self.archive()
        package = self.install(digest)
        root = Path(package['package_dir'])
        self.assertEqual(digest, hashlib.sha256((root / 'artifact.tar.gz').read_bytes()).hexdigest())
        for name, (body, mode) in self.files.items():
            self.assertEqual(body, (root / 'payload' / name).read_bytes())
            self.assertEqual(mode, (root / 'payload' / name).stat().st_mode & 0o777)
        self.assertEqual(str(root / 'payload/bin/codex'), package['executable'])
        self.assertEqual(self.version, package['version'])
        self.assertTrue(package['complete_package_verified'])
        self.assertNotEqual(digest, hashlib.sha256(Path(package['executable']).read_bytes()).hexdigest())
        self.assertEqual(package['executable'], verify_installation(root, digest)['executable'])

    def test_npm_platform_package_prefix_is_kept_without_flattening(self):
        prefix = 'package/vendor/' + self.target + '/'
        digest = self.archive(prefix=prefix)
        package = self.install(digest)
        self.assertEqual(str(Path(package['package_dir']) / 'payload' / prefix / 'bin/codex'), package['executable'])
        self.assertTrue(Path(package['executable']).with_name('codex-code-mode-host').is_file())

    def test_archived_directory_mode_is_preserved_and_verified(self):
        member = tarfile.TarInfo('codex-resources')
        member.type, member.mode = tarfile.DIRTYPE, 0o700
        digest = self.archive(extra=member)
        package = self.install(digest)
        directory = Path(package['package_dir']) / 'payload/codex-resources'
        self.assertEqual(0o700, directory.stat().st_mode & 0o777)
        directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'package_tree_mismatch'):
            verify_installation(package['package_dir'], digest)

    def test_implicit_directory_cannot_become_group_writable(self):
        digest = self.archive()
        package = self.install(digest)
        directory = Path(package['package_dir']) / 'payload/bin'
        directory.chmod(0o775)
        with self.assertRaisesRegex(ValueError, 'package_tree_mismatch'):
            verify_installation(package['package_dir'], digest)

    def test_implicit_nested_directories_are_safe_with_group_write_umask(self):
        previous = os.umask(0o002)
        try:
            digest = self.archive()
            package = self.install(digest)
        finally:
            os.umask(previous)
        payload = Path(package['package_dir']) / 'payload'
        for directory in payload.rglob('*'):
            if directory.is_dir():
                self.assertFalse(directory.stat().st_mode & 0o022)
        self.assertTrue(verify_installation(package['package_dir'], digest)['complete_package_verified'])

    def test_complete_looking_noncanonical_manifest_is_not_published(self):
        for key, old, new in (('entrypoint', 'bin/codex', 'elsewhere/codex'),
                              ('resourcesDir', 'codex-resources', 'elsewhere-resources'),
                              ('pathDir', 'codex-path', 'elsewhere-path')):
            with self.subTest(key=key):
                manifest = dict(self.manifest, **{key: new})
                files = {}
                for name, body in self.files.items():
                    renamed = new + name[len(old):] if name == old or name.startswith(old + '/') else name
                    if key == 'entrypoint' and name == 'bin/codex-code-mode-host':
                        renamed = 'elsewhere/codex-code-mode-host'
                    files[renamed] = body
                files['codex-package.json'] = (json.dumps(manifest).encode(), 0o644)
                with self.assertRaisesRegex(ValueError, 'canonical_layout_required'):
                    self.install(self.archive(files))
                self.assertEqual([], list(self.install_dir.iterdir()))

    def test_standalone_that_can_print_version_is_not_complete_package(self):
        standalone = self.root / 'standalone-codex'
        standalone.write_bytes(self.files['bin/codex'][0])
        standalone.chmod(0o755)
        result = subprocess.check_output([str(standalone), '--version']).decode().strip()
        self.assertEqual('codex-cli 0.159.2', result)
        digest = self.archive(files={'codex-' + self.target: self.files['bin/codex']})
        with self.assertRaisesRegex(ValueError, 'complete_package_required'):
            self.install(digest)
        self.assertEqual([], list(self.install_dir.iterdir()))

    def test_missing_helper_or_resource_tree_is_not_published(self):
        for missing in ('bin/codex-code-mode-host', 'codex-path/rg', 'codex-resources/zsh/bin/zsh'):
            with self.subTest(missing=missing):
                files = dict(self.files)
                if missing.startswith('codex-resources'):
                    files = {name: body for name, body in files.items() if not name.startswith('codex-resources')}
                else:
                    files.pop(missing)
                digest = self.archive(files)
                with self.assertRaisesRegex(ValueError, 'complete_package_required'):
                    self.install(digest)
                self.assertEqual([], list(self.install_dir.iterdir()))

    def test_explicit_release_identity_and_full_artifact_digest_must_match(self):
        digest = self.archive()
        cases = [({'version': '0.159.3'}, 'version_mismatch'), ({'target': 'aarch64-unknown-linux-musl'}, 'target_mismatch')]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, expected):
                    self.install(digest, **changes)
        with self.assertRaisesRegex(ValueError, 'integrity_mismatch'):
            self.install(hashlib.sha256(self.files['bin/codex'][0]).hexdigest())
        self.assertEqual([], list(self.install_dir.iterdir()))

    def test_archive_traversal_links_duplicates_and_unsafe_modes_are_rejected(self):
        for name, kind in (('../outside', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('symlink', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE),
                           ('fifo', tarfile.FIFOTYPE), ('bin/codex', tarfile.REGTYPE)):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.type, member.mode, member.linkname = kind, 0o644, '../outside'
                member.size = 1 if member.isfile() else 0
                digest = self.archive(extra=member)
                with self.assertRaisesRegex(ValueError, 'archive_unsafe'):
                    self.install(digest)
        member = tarfile.TarInfo('writable')
        member.size, member.mode = 1, 0o666
        with self.assertRaisesRegex(ValueError, 'archive_unsafe_member'):
            self.install(self.archive(extra=member))
        self.assertFalse((self.root / 'outside').exists())

    def test_published_install_is_idempotent_but_modified_tree_is_not_reused(self):
        digest = self.archive()
        first, second = self.install(digest), self.install(digest)
        self.assertEqual(first['package_dir'], second['package_dir'])
        self.assertFalse(first['already_installed'])
        self.assertTrue(second['already_installed'])
        helper = Path(first['executable']).with_name('codex-code-mode-host')
        helper.write_bytes(b'helper from another release')
        with self.assertRaisesRegex(ValueError, 'package_tree_mismatch'):
            self.install(digest)
        self.assertEqual(b'helper from another release', helper.read_bytes())  # never overwritten

    def test_verification_detects_removed_added_changed_or_nonexecutable_files(self):
        digest = self.archive()
        package = self.install(digest)
        root = Path(package['package_dir'])
        helper = Path(package['executable']).with_name('codex-code-mode-host')
        original = helper.read_bytes()
        helper.unlink()
        with self.assertRaisesRegex(ValueError, 'package_tree_mismatch'):
            verify_installation(root, digest)
        helper.write_bytes(original)
        helper.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'package_tree_mismatch'):
            verify_installation(root, digest)
        helper.chmod(0o755)
        additional = root / 'payload/bin/mixed-release-helper'
        additional.write_bytes(b'not from artifact')
        with self.assertRaisesRegex(ValueError, 'package_tree_mismatch'):
            verify_installation(root, digest)
        additional.unlink()
        helper.write_bytes(b'x' * len(original))
        with self.assertRaisesRegex(ValueError, 'package_tree_mismatch'):
            verify_installation(root, digest)

    def test_relative_or_symlink_installation_paths_are_rejected(self):
        digest = self.archive()
        with self.assertRaisesRegex(ValueError, 'absolute_non_symlink'):
            install_package(self.artifact, digest, self.version, self.target, Path('relative'))
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'absolute_non_symlink'):
            install_package(self.artifact, digest, self.version, self.target, link / 'installation')
        artifact_link = self.root / 'artifact-link'
        artifact_link.symlink_to(self.artifact)
        with self.assertRaisesRegex(ValueError, 'absolute_non_symlink'):
            install_package(artifact_link, digest, self.version, self.target, self.install_dir)

    def test_runtime_switch_verifies_complete_artifact_then_backs_up_private_config(self):
        digest = self.archive()
        package = self.install(digest)
        path, previous = self.config()
        output = self.switch(path, package, digest, version=self.version, target=self.target)
        value = json.loads(path.read_text())
        self.assertEqual(package['executable'], value['codex']['executable'])
        self.assertEqual('danger-full-access', value['codex']['sandbox'])
        self.assertEqual('never', value['codex']['approval_policy'])
        self.assertEqual('catalog-first', value['codex']['model_policy'])
        self.assertNotIn('model', value['codex'])
        self.assertEqual(previous, json.loads(list(self.root.glob('worker.json.before-runtime-*'))[0].read_text()))
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertTrue(output['complete_package_verified'])
        self.assertNotIn('PRIVATE_CONFIG_DO_NOT_PRINT', json.dumps(output))

    def test_runtime_switch_refuses_mixed_helper_or_unrelated_executable_without_config_change(self):
        digest = self.archive()
        package = self.install(digest)
        path, previous = self.config()
        with self.assertRaisesRegex(ValueError, 'not_from_verified_package'):
            self.switch(path, package, digest, executable='/unrelated/codex')
        helper = Path(package['executable']).with_name('codex-code-mode-host')
        helper.write_bytes(b'wrong-release-helper')
        with self.assertRaisesRegex(ValueError, 'package_tree_mismatch'):
            self.switch(path, package, digest)
        self.assertEqual(previous, json.loads(path.read_text()))
        self.assertEqual([], list(self.root.glob('worker.json.before-runtime-*')))

    def test_binary_only_admin_interface_is_not_accepted_as_complete_installation(self):
        path, previous = self.config()
        executable = self.root / 'standalone'
        executable.write_bytes(self.files['bin/codex'][0])
        executable.chmod(0o755)
        argv = ['runtime_admin', '--config', str(path), 'set-cloud-runtime', '--executable', str(executable),
                '--sha256', hashlib.sha256(executable.read_bytes()).hexdigest()]
        with patch('sys.argv', argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runtime_main()
        self.assertEqual(previous, json.loads(path.read_text()))

    def test_upgrade_cli_installs_explicit_offline_package_and_retains_old_state_migration(self):
        digest = self.archive()
        argv = ['upgrade_runtime', '--artifact', str(self.artifact), '--sha256', digest,
                '--version', self.version, '--target', self.target, '--install-dir', str(self.install_dir)]
        output = io.StringIO()
        with patch('sys.argv', argv), contextlib.redirect_stdout(output):
            upgrade_main()
        self.assertTrue(json.loads(output.getvalue())['complete_package_verified'])
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        path = state / 'laptop-worker.json'
        path.write_text(json.dumps({'codex': {}, 'capabilities': []}))
        path.chmod(0o600)
        with patch('sys.argv', ['upgrade_runtime', '--state', str(state)]), contextlib.redirect_stdout(io.StringIO()):
            upgrade_main()
        value = json.loads(path.read_text())
        self.assertEqual('danger-full-access', value['codex']['sandbox'])
        self.assertEqual('never', value['codex']['approval_policy'])
        self.assertTrue(path.with_suffix('.json.before-model-led').is_file())


if __name__ == '__main__':
    unittest.main()
