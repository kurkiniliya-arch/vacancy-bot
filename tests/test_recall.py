"""Regression cases from the public posts and their relevant edit/dedup paths."""
from dataclasses import replace
import sqlite3
import tempfile
from pathlib import Path
import unittest

from jobbot.filtering import assess, render
from test_support import Store
from test_bot import post, NOW

OUTSIDE = "вне предварительного отбора"


class EditRecallTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.ingest("demo", [post()], NOW)
        self.store.ingest("other", [], NOW)

    def tearDown(self):
        self.store.close()

    def test_rejected_then_completed_post_is_queued(self):
        self.store.ingest("demo", [post(2, text="Подробности скоро")], NOW)
        self.assertEqual(self.store.ingest("demo", [post(2, text="Business Analyst")], NOW)["queued"], 1)

    def test_pending_edit_updates_card(self):
        self.store.ingest("demo", [post(2)], NOW)
        self.store.ingest("demo", [post(2, text="Business Analyst\nSalary: €5000")], NOW)
        items = self.store.preview()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "pending")
        self.assertIn("€5000", items[0]["body"])

    def test_baseline_edit_never_sends_history(self):
        self.store.ingest("demo", [post(1, text="Business Analyst\nSalary: €5000")], NOW)
        self.assertEqual(self.store.preview(), [])

    def test_rejected_repost_does_not_poison_vacancy_url(self):
        url = "https://example.org/jobs/321"
        self.store.ingest("demo", [post(2, text="Подробности скоро", vacancy_url=url)], NOW)
        self.store.ingest("other", [post(1, source="other", vacancy_url=url)], NOW)
        self.assertEqual(len(self.store.preview()), 1)

    def test_cancelled_pending_can_be_reopened(self):
        self.store.ingest("demo", [post(2)], NOW)
        self.store.ingest("demo", [post(2, text="Closed")], NOW)
        self.assertEqual(self.store.preview(), [])
        self.store.ingest("demo", [post(2, text="System Analyst reopened")], NOW)
        self.assertEqual(len(self.store.preview()), 1)

    def test_edited_url_and_repost_never_resend_sent_or_uncertain(self):
        for state in ["sent", "uncertain", "failed"]:
            with self.subTest(state=state):
                s = Store(":memory:")
                try:
                    s.ingest("demo", [], NOW)
                    p = post(vacancy_url="https://example.org/jobs/1")
                    s.ingest("demo", [p], NOW)
                    s.claim(0)
                    s.resolve(p.key, state)
                    edited = replace(p, vacancy_url="https://example.org/jobs/2")
                    s.ingest("demo", [edited], NOW)
                    s.ingest("demo", [replace(edited, text="Business Analyst")], NOW)
                    s.ingest("other", [], NOW)
                    s.ingest("other", [post(source="other", vacancy_url=edited.vacancy_url)], NOW)
                    self.assertEqual(len(s.preview()), 1)
                    self.assertEqual(s.preview()[0]["state"], state)
                finally:
                    s.close()

    def test_edited_rate_limited_card_preserves_retry_delay(self):
        self.store.ingest("demo", [post(2)], NOW)
        item = self.store.claim(0)
        self.store.resolve(item["key"], "pending", next_attempt=120)
        self.store.ingest("demo", [post(2, text="Business Analyst")], NOW)
        self.assertIsNone(self.store.claim(119))
        self.assertIsNotNone(self.store.claim(120))

    def test_legacy_schema_migration_does_not_replay_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE posts (source TEXT,post_id INTEGER,key TEXT,fingerprint TEXT,payload TEXT,"
                       "first_seen TEXT,last_seen TEXT,PRIMARY KEY(source,post_id))")
            db.execute("INSERT INTO posts VALUES (?,?,?,?,?,?,?)", ("demo", 1, post().key, "old", "{}", NOW, NOW))
            db.commit()
            db.close()
            s = Store(path)
            try:
                s.ingest("demo", [post()], NOW)
                self.assertEqual(s.preview(), [])
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main()
