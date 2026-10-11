import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from scripts.capability_artifact import main


class CapabilityArtifactCliTests(unittest.TestCase):
    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                result = main(arguments)
            except SystemExit as error:
                result = error.code
        text = output.getvalue() + errors.getvalue()
        self.assertNotIn('DO_NOT_PRINT', text)
        return result, output.getvalue(), errors.getvalue()

    def producer_args(self, action='manifest'):
        return ['producer', action, '--owner-config', '/DO_NOT_PRINT/owner.json',
                '--owner-config-sha256', 'a' * 64, '--message-id', 'message-1',
                '--operation-id', 'operation-1', '--capability-id', 'campus-1']

    def test_manifest_uses_fixed_configuration_and_original_ids(self):
        reader = Mock()
        reader.manifest.return_value = {'artifact_sha256': 'b' * 64,
            'native_owner_file_authority': True, 'managed_artifact_grant_verified': False}
        with patch('assistant_mesh.artifact_transport.ArtifactProducer', return_value=reader) as factory:
            code, output, errors = self.invoke(self.producer_args())
        self.assertEqual(0, code)
        self.assertEqual('', errors)
        self.assertEqual('b' * 64, json.loads(output)['artifact_sha256'])
        factory.assert_called_once_with('/DO_NOT_PRINT/owner.json', 'a' * 64)
        reader.manifest.assert_called_once_with('message-1', 'operation-1', 'campus-1')
        reader.write_to.assert_not_called()

    def test_source_exception_has_no_path_or_body_leak(self):
        with patch('assistant_mesh.artifact_transport.ArtifactProducer',
                   side_effect=ValueError('DO_NOT_PRINT credentials and body')):
            code, output, errors = self.invoke(self.producer_args())
        self.assertEqual(2, code)
        self.assertEqual('', output)
        self.assertEqual(['artifact_transfer_failed'], json.loads(errors)['diagnostics'])

    def test_parser_error_is_redacted_before_producer(self):
        with patch('assistant_mesh.artifact_transport.ArtifactProducer') as factory:
            for args in ([], ['DO_NOT_PRINT'], ['producer', 'DO_NOT_PRINT'],
                         self.producer_args() + ['--DO_NOT_PRINT', 'DO_NOT_PRINT']):
                code, output, errors = self.invoke(args)
                self.assertEqual(2, code)
                self.assertEqual('', output)
                self.assertEqual(['artifact_cli_arguments_invalid'], json.loads(errors)['diagnostics'])
            factory.assert_not_called()

    def test_terminal_content_read_is_refused(self):
        reader = Mock()
        output, errors = io.StringIO(), io.StringIO()
        output.isatty = lambda: True
        with patch('assistant_mesh.artifact_transport.ArtifactProducer', return_value=reader), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(2, main(self.producer_args('read')))
        self.assertEqual('', output.getvalue())
        self.assertEqual(['artifact_private_pipe_required'], json.loads(errors.getvalue())['diagnostics'])
        reader.write_to.assert_not_called()

    def test_consumer_requires_private_pinned_manifest_before_fetch(self):
        with patch('assistant_mesh.provider_runtime._owner_bytes',
                   side_effect=ValueError('DO_NOT_PRINT private manifest')) as read, \
                patch('assistant_mesh.artifact_transport.fetch_artifact') as fetch:
            code, output, errors = self.invoke(['consumer', '--manifest-file',
                '/DO_NOT_PRINT/manifest.json', '--output-file', '/DO_NOT_PRINT/output.json'])
        self.assertEqual(2, code)
        self.assertEqual('', output)
        read.assert_called_once_with('/DO_NOT_PRINT/manifest.json', 65536, 'artifact_manifest_unavailable')
        fetch.assert_not_called()
        self.assertEqual(['artifact_transfer_failed'], json.loads(errors)['diagnostics'])

    def test_consumer_uses_private_manifest_and_bounded_stdin(self):
        manifest = {'artifact_sha256': 'b' * 64}
        incoming = io.BytesIO(b'private full bytes')
        stdin = Mock(buffer=incoming)
        with patch('assistant_mesh.provider_runtime._owner_bytes',
                   return_value=json.dumps(manifest).encode()), \
                patch('assistant_mesh.artifact_transport.fetch_artifact',
                      return_value={'sha256_verified': True}) as fetch, \
                patch('scripts.capability_artifact.sys.stdin', stdin):
            code, output, errors = self.invoke(['consumer', '--manifest-file',
                '/DO_NOT_PRINT/manifest.json', '--output-file', '/DO_NOT_PRINT/output.json',
                '--max-artifact-bytes', '4096'])
        self.assertEqual(0, code)
        self.assertEqual('', errors)
        self.assertTrue(json.loads(output)['sha256_verified'])
        fetch.assert_called_once_with(incoming, manifest, '/DO_NOT_PRINT/output.json',
                                      max_artifact_bytes=4096)

    def test_private_read_pipe_contains_only_producer_bytes(self):
        reader = Mock()
        def write(message, operation, capability, stream):
            stream.write(b'original full artifact bytes')
            return {'artifact_sha256': 'b' * 64}
        reader.write_to.side_effect = write
        output = io.BytesIO()
        stdout = io.TextIOWrapper(output, encoding='utf8')
        errors = io.StringIO()
        with patch('assistant_mesh.artifact_transport.ArtifactProducer', return_value=reader), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(errors):
            self.assertEqual(0, main(self.producer_args('read')))
        stdout.flush()
        self.assertEqual(b'original full artifact bytes', output.getvalue())
        self.assertEqual('', errors.getvalue())
        reader.manifest.assert_not_called()
