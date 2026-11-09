"""Pure public-source package assembly; no incumbent import or Store created."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('phone_overlay_builder', str(ROOT / 'deploy/build-phone-autonomy-overlay.py'))
builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)


class PhoneAutonomyOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.parent = self.root / 'parent'; (self.parent / 'assistant_mesh').mkdir(parents=True)
        self.originals = {'assistant_mesh/__init__.py': b"__path__.append('/SYNTHETIC/fallback')\n"}
        for name in ('native_partial_adapter', 'native_partial_continuation', 'native_partial_methods'):
            self.originals['assistant_mesh/' + name + '.py'] = b'VALUE = "synthetic-public-parent"\n'
        self.pins = {'/SYNTHETIC/protected/store.py': 'a' * 64}
        launcher = ("from pathlib import Path\n_root = Path(__file__).resolve().parent\n_pins = " + repr(self.pins) +
                    '\n_new_sources = ' + repr({n: builder.sha(v) for n, v in self.originals.items()}) + '\n_entry = None\n').encode()
        self.originals['launch_receiver.py'] = launcher
        manifest = {'format': 'amita-native-partial-overlay/1', 'component': 'receiver', 'runtime_pins': self.pins,
                    'source_sha256': {n: builder.sha(v) for n, v in self.originals.items()}}
        for relative, raw in self.originals.items(): (self.parent / relative).write_bytes(raw)
        raw = (json.dumps(manifest, sort_keys=True) + '\n').encode()
        (self.parent / builder.PARENT_MANIFEST).write_bytes(raw)
        self.manifest_hash = hashlib.sha256(raw).hexdigest()
        self.commit = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).decode().strip()

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        return builder.build(str(self.parent), self.manifest_hash, self.commit, str(self.root / 'candidate'),
                             '/SYNTHETIC/installed-parent', '/SYNTHETIC/installed-candidate', ROOT)

    def test_byte_preservation_full_hashes_and_pin_failure_before_runtime_import(self):
        report = self.build(); candidate = Path(report['destination'])
        for relative, raw in self.originals.items():
            target = 'parent_launch_receiver.py' if relative == 'launch_receiver.py' else relative
            self.assertEqual(raw, (candidate / target).read_bytes())
        self.assertEqual((self.parent / builder.PARENT_MANIFEST).read_bytes(), (candidate / builder.PARENT_MANIFEST).read_bytes())
        manifest = json.loads((candidate / 'phone-autonomy-overlay.json').read_text())
        self.assertFalse(manifest['production_installed']); self.assertFalse(manifest['ledger_opened'])
        self.assertEqual(self.pins, manifest['inherited_runtime_pins']); self.assertEqual(self.commit, manifest['source_commit'])
        launcher = (candidate / 'launch_receiver.py').read_bytes()
        sources = builder.literal(builder.parse36(launcher), '_new_sources')
        for name in builder.MODULES:
            relative = 'assistant_mesh/' + name + '.py'
            self.assertEqual(sources[relative], builder.sha((candidate / relative).read_bytes()))
        for name in builder.PROTECTED:
            self.assertFalse((candidate / 'assistant_mesh' / (name + '.py')).exists())
        old_path = list(sys.path)
        try:
            with self.assertRaisesRegex(RuntimeError, 'runtime_pin_changed'):
                exec(compile(launcher, 'synthetic-launcher', 'exec'),
                     {'__file__': str(candidate / 'launch_receiver.py'), '__name__': 'not_main'})
        finally:
            sys.path[:] = old_path

    def test_parent_tamper_does_not_repin_or_create_candidate(self):
        (self.parent / 'assistant_mesh/native_partial_adapter.py').write_bytes(b'CHANGED = True\n')
        with self.assertRaisesRegex(ValueError, 'source_pin_changed'): self.build()
        self.assertFalse((self.root / 'candidate').exists())
