import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from jobbot.check import check
from jobbot.telegram import TelegramError


class SetupCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secret = Path(self.tmp.name) / 'credentials.json'
        self.secret.write_text(json.dumps(dict(token='fake:test', chat_id=123,
                                             recipient_confirmed=True, exclusive_bot_confirmed=True)))
        self.secret.chmod(0o600)
        self.api = Mock()
        self.api.call.return_value = dict(type='private', id=123)
        self.api.send.return_value = 55
        self.factory = Mock(return_value=self.api)

    def tearDown(self):
        self.tmp.cleanup()

    def test_setup_message_not_sent_twice(self):
        self.assertEqual(check(self.secret, self.factory), 'sent')
        self.assertEqual(check(self.secret, self.factory), 'already_sent')
        self.api.send.assert_called_once()

    def test_timeout_is_not_retried(self):
        self.api.send.side_effect = TelegramError('uncertain')
        with self.assertRaises(ValueError):
            check(self.secret, self.factory)
        with self.assertRaises(ValueError):
            check(self.secret, self.factory)
        self.api.send.assert_called_once()

    def test_wrong_recipient_stops_before_send(self):
        self.api.call.return_value = dict(type='private', id=456)
        with self.assertRaises(ValueError):
            check(self.secret, self.factory)
        self.api.send.assert_not_called()
