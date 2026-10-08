"""Mesh runtime deployment must not silently sandbox the native toolbox."""
import unittest
from pathlib import Path


class NativeRuntimePermissions(unittest.TestCase):
    def test_model_services_do_not_add_native_tool_restrictions(self):
        deploy = Path(__file__).resolve().parents[1] / 'deploy'
        for name in ('assistant-mesh-laptop-worker.service',
                     'assistant-mesh-cloud-worker.service',
                     'assistant-mesh-laptop-node.service'):
            with self.subTest(service=name):
                settings = [line.strip() for line in (deploy / name).read_text().splitlines()
                            if line.strip() and not line.lstrip().startswith('#')]
                self.assertNotIn('NoNewPrivileges=true', settings)
                self.assertNotIn('PrivateTmp=true', settings)


if __name__ == '__main__':
    unittest.main()
