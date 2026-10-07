from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from unittest.mock import patch
import contextlib
import io
import types
from jobbot.model import Post, canonical_url
from jobbot.filtering import assess, render
from test_support import Store, MATCHING
from jobbot.telegram import BotAPI, TelegramError, deliver_one
from jobbot.websource import parse_preview, SourceError
from jobbot.runtime import run

NOW = "2026-09-22T14:00:00+00:00"


def post(i=1, source="demo", text="System Analyst\nAPI, Kafka, спецификации", vacancy_url=None):
    return Post(source, i, text, f"https://t.me/{source}/{i}", NOW, vacancy_url)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def queue(self):
        self.store.ingest("demo", [post()], NOW)
        self.store.ingest("demo", [post(2)], NOW)

    def test_baseline_no_history(self):
        self.store.ingest("demo", [post(i) for i in range(1, 101)], NOW)
        self.assertEqual(self.store.preview(), [])

    def test_restart_does_not_duplicate(self):
        self.queue()
        self.store.close()
        self.store = Store(self.path)
        self.store.ingest("demo", [post(), post(2)], NOW)
        self.assertEqual(len(self.store.preview()), 1)

    def test_cross_channel_duplicate(self):
        self.store.ingest("demo", [], NOW)
        self.store.ingest("other", [], NOW)
        self.store.ingest("demo", [post(vacancy_url="https://example.org/jobs/1?utm_source=a")], NOW)
        self.store.ingest("other", [post(source="other", vacancy_url="https://example.org/jobs/1?utm_source=b")], NOW)
        self.assertEqual(len(self.store.preview()), 1)

    def test_edited_post_cancels_stale_pending(self):
        self.queue()
        result = self.store.ingest("demo", [post(2, text="Vacancy closed")], NOW)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.store.preview(), [])

    def test_failed_first_fetch_does_not_start_history_delivery(self):
        self.store.source_failed("demo", 0)
        self.assertFalse(self.store.due("demo", 1))
        self.assertTrue(self.store.due("demo", 30))
        self.assertEqual(self.store.ingest("demo", [post()], NOW)["baseline"], 1)

    def test_retry_after_persisted(self):
        self.store.source_failed("demo", 10, retry_after=9000)
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.due("demo", 9009))
        self.assertTrue(self.store.due("demo", 9010))

    def test_old_unseen_post_not_sent(self):
        self.store.ingest("demo", [post(10)], NOW)
        self.assertEqual(self.store.ingest("demo", [post(9)], NOW)["old"], 1)
        self.assertEqual(self.store.preview(), [])

    def test_timeout_not_automatically_retried(self):
        self.queue()
        api = Mock()
        api.send.side_effect = TelegramError("uncertain")
        self.assertEqual(deliver_one(self.store, api, 123, 0), "uncertain")
        self.assertEqual(deliver_one(self.store, api, 123, 10), "halted")
        api.send.assert_called_once()

    def test_flood_wait(self):
        self.queue()
        api = Mock()
        api.send.side_effect = [TelegramError("rate_limit", 120), 555]
        self.assertEqual(deliver_one(self.store, api, 123, 0), "rate_limit")
        self.assertEqual(deliver_one(self.store, api, 123, 119), "waiting")
        self.assertEqual(deliver_one(self.store, api, 123, 120), "sent")

    def test_crash_recovery_is_uncertain(self):
        self.queue()
        self.store.claim(0)
        self.store.recover_after_exclusive_restart()
        self.assertEqual(self.store.preview()[0]["state"], "uncertain")
        self.assertIsNone(self.store.claim(100))

    def test_claim_only_once_across_connections(self):
        self.queue()
        other = Store(self.path)
        try:
            self.assertIsNotNone(self.store.claim(0))
            self.assertIsNone(other.claim(0))
        finally:
            other.close()

    def test_flood_wait_stops_other_messages_and_survives_restart(self):
        self.queue()
        self.store.ingest("demo", [post(3)], NOW)
        api = Mock()
        api.send.side_effect = TelegramError("rate_limit", 120)
        deliver_one(self.store, api, 123, 0)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(deliver_one(self.store, api, 123, 1), "waiting")
        api.send.assert_called_once()

    def test_uncertain_halts_remaining_queue_after_restart(self):
        self.queue()
        self.store.ingest("demo", [post(3)], NOW)
        api = Mock()
        api.send.side_effect = TelegramError("uncertain")
        deliver_one(self.store, api, 123, 0)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(deliver_one(self.store, api, 123, 1000), "halted")
        api.send.assert_called_once()


class TelegramTests(unittest.TestCase):
    def test_webhook_never_deleted(self):
        api = BotAPI("fake:test")
        api.call = Mock(side_effect=[{"username": "example_jobs_bot", "is_bot":True}, {"url": "https://example.org/hook"}])
        with self.assertRaisesRegex(TelegramError, "webhook_exists"):
            api.preflight("example_jobs_bot")
        self.assertEqual([c.args[0] for c in api.call.call_args_list], ["getMe", "getWebhookInfo"])

    def test_wrong_bot(self):
        api = BotAPI("fake:test")
        api.call = Mock(return_value={"username": "another_bot"})
        with self.assertRaisesRegex(TelegramError, "wrong_bot"):
            api.preflight("example_jobs_bot")

    def test_payload_plain_and_free(self):
        api = BotAPI("fake:test")
        api.call = Mock(return_value={"message_id": 3})
        api.send(123, "<b>untrusted</b>")
        payload = api.call.call_args.args[1]
        self.assertNotIn("parse_mode", payload)
        self.assertFalse(payload["allow_paid_broadcast"])


class WebTests(unittest.TestCase):
    def test_preview(self):
        html = '<html><body><div class="tgme_widget_message" data-post="demo/42"><div class="tgme_widget_message_text">System Analyst<br>API <b>Kafka</b><a href="https://example.org/jobs/1">Job</a></div><a><time datetime="2026-09-22T10:00:00+00:00">10:00</time></a></div></body></html>'
        parsed = parse_preview("demo", html)
        self.assertEqual(parsed[0].text, "System Analyst\nAPI KafkaJob")
        self.assertEqual(parsed[0].vacancy_url, "https://example.org/jobs/1")
        self.assertEqual(parsed[0].post_id, 42)

    def test_unavailable_and_truncated_fail_closed(self):
        for html in ["<html>Sign in</html>", '<html><div class="tgme_widget_message" data-post="demo/42">']:
            with self.assertRaises(SourceError):
                parse_preview("demo", html)


class RuntimeTests(unittest.TestCase):
    def test_dry_run_never_constructs_telegram_client(self):
        # Control flow is portable; Linux flock itself is checked during deployment.
        fake_fcntl = types.SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.sqlite"
            with patch.dict("sys.modules", {"fcntl": fake_fcntl}), \
                    patch("jobbot.runtime.fetch", return_value=[post()]), \
                    patch("jobbot.runtime.BotAPI") as api, \
                    patch("jobbot.runtime.signal.signal"), contextlib.redirect_stdout(io.StringIO()):
                run({"matching":MATCHING,"sources": ["demo"], "delivery": {"enabled": False}}, path, once=True)
                api.assert_not_called()
            store = Store(path)
            try:
                self.assertEqual(store.get_setting("mode"), "dry-run")
                self.assertEqual(store.preview(), [])
            finally:
                store.close()

    def test_dry_database_cannot_be_replayed_live(self):
        fake_fcntl = types.SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.sqlite"
            store = Store(path)
            store.set_setting("mode", "dry-run")
            store.close()
            with patch.dict("sys.modules", {"fcntl": fake_fcntl}), patch("jobbot.runtime.BotAPI") as api:
                with self.assertRaisesRegex(ValueError, "new live database"):
                    run({"matching":MATCHING,"sources": ["demo"], "delivery": {"enabled": True}}, path, send=True, once=True)
                api.assert_not_called()

    def test_send_disabled_without_config_switch(self):
        fake_fcntl = types.SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
        with patch.dict("sys.modules", {"fcntl": fake_fcntl}), patch("jobbot.runtime.BotAPI") as api:
            with self.assertRaisesRegex(ValueError, "disabled"):
                run({"matching":MATCHING,"sources": ["demo"], "delivery": {"enabled": False}}, "unused.sqlite", send=True)
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
