"""One source-builder boundary: frozen pins, complete import closure, no DB/SDK."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_legacy_fenced_receiver import _source_base


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConversationFinalCandidateTests(unittest.TestCase):
    def test_exact_store_patch_preserves_incumbents_and_imports_full_closure_without_database_or_sdk(self):
        with tempfile.TemporaryDirectory(prefix='mesh-final-source-only.') as temporary:
            from assistant_mesh import ingress_backend, mcp_http
            self.assertIs(ingress_backend.LocalIngressBackend, mcp_http.LocalIngressBackend)
            self.assertIs(ingress_backend.RemoteIngressBackend, mcp_http.RemoteIngressBackend)
            root = Path(temporary)
            historical = root / 'historical' / 'assistant_mesh'
            historical.mkdir(parents=True, mode=0o700)
            for name, data in _source_base().items():
                (historical / name).write_bytes(data)
            entrance = load('_final_fixture_entrance', ROOT / 'deploy/build-entrance-candidate.py')
            entrance.build(historical.parent, root / 'base', ROOT)
            base = root / 'base'
            (base / 'assistant_mesh/conversation_authority.py').write_bytes(
                (ROOT / 'assistant_mesh/conversation_authority.py').read_bytes())
            (base / 'original_owner_runner.py').write_text('ORIGINAL_READY_HOOK = True\n')
            (base / 'private-config.json').write_text('{"not_source":true}\n')
            builder = load('_final_source_builder', ROOT / 'deploy/build-conversation-final-candidate.py')
            pin = builder.inspect_base(base)['base_manifest_sha256']
            target = root / 'candidate'
            with self.assertRaisesRegex(ValueError, 'base_manifest_changed'):
                builder.build(base, target, '0' * 64, ROOT)
            self.assertFalse(target.exists())
            report = builder.build(base, target, pin, ROOT)
            self.assertEqual(10, len(report['overlay_modules']))
            allowed = {'assistant_mesh/' + name for name in builder.SUPPORT_SHA256}
            allowed.update(('assistant_mesh/store.py', 'assistant_mesh/legacy_fenced_receiver.py'))
            for name, expected in builder.inspect_base(base)['source_sha256'].items():
                if name not in allowed:
                    self.assertEqual((base / name).read_bytes(), (target / name).read_bytes(), name)
            self.assertFalse((target / 'private-config.json').exists())
            probe = r'''
import json, sqlite3, sys
from pathlib import Path
def no_database(*args, **kwargs):
    raise AssertionError('import must not open a ledger')
sqlite3.connect = no_database
from assistant_mesh import server, conversation_final, conversation_emit, conversation_event_feed
from assistant_mesh.legacy_fenced_receiver import _check_frozen_source
from assistant_mesh.entrance_runtime import serve_entrance
from assistant_mesh.conversation_runtime import ConversationBridge
from assistant_mesh.conversation_sync import sync_original_message
_check_frozen_source(server)
assert Path(server.__file__).resolve().parent == Path.cwd() / 'assistant_mesh'
assert 'assistant_mesh.mcp_http' not in sys.modules
assert not any(n == 'mcp' or n.startswith(('mcp.', 'fastmcp')) for n in sys.modules)
print(json.dumps({'import_without_database_or_sdk':True,'model_calls':0}))
'''
            actual = subprocess.run([sys.executable, '-S', '-B', '-c', probe], cwd=str(target),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=20)
            self.assertEqual(0, actual.returncode, actual.stderr)
            self.assertTrue(json.loads(actual.stdout)['import_without_database_or_sdk'])
            with self.assertRaisesRegex(ValueError, 'destination_must_be_new'):
                builder.build(base, target, pin, ROOT)


if __name__ == '__main__':
    unittest.main()
