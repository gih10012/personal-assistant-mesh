"""One targeted owner-CLI seam check; no receiver, Store, account or network."""
import contextlib
import io
import unittest
from unittest import mock

from assistant_mesh import conversation_authority as authority


class ConversationAuthorityCLITests(unittest.TestCase):
    def test_owner_source_default_explicit_and_invalid_before_start(self):
        owner_config, api, outcome = object(), object(), object()
        base = ['--config', '/PRIVATE/original-server.json', '--socket', '/PRIVATE/original-owner.sock']
        def serve(config, socket, ready):
            self.assertIs(owner_config, config)
            self.assertEqual('/PRIVATE/original-owner.sock', socket)
            ready(object(), api, object())
            return outcome
        with mock.patch('assistant_mesh.config.private_json', return_value=owner_config) as reader, \
                mock.patch('assistant_mesh.entrance_runtime.serve_entrance', side_effect=serve) as receiver, \
                mock.patch.object(authority, 'attach') as attach:
            for arguments, sources in (([], ('chatgpt-chat',)),
                    (['--source', 'chatgpt-chat', '--source', 'openai-codex-cloud'],
                     ('chatgpt-chat', 'openai-codex-cloud'))):
                with self.subTest(sources=sources):
                    self.assertIs(outcome, authority.main(base + arguments))
                    reader.assert_called_once_with('/PRIVATE/original-server.json')
                    receiver.assert_called_once()
                    attach.assert_called_once_with(api, sources=sources)
                    reader.reset_mock()
                    receiver.reset_mock()
                    attach.reset_mock()
            for source in ('', ' ', 'chat\ncloud', 'x' * 201):
                with self.subTest(invalid=repr(source)), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as rejected:
                        authority.main(base + ['--source', source])
                    self.assertEqual(2, rejected.exception.code)
                    reader.assert_not_called()
                    receiver.assert_not_called()
                    attach.assert_not_called()


if __name__ == '__main__':
    unittest.main()
