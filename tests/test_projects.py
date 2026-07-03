"""Tests for the project registry: canonical codes, lookup by code/slug/channel,
listing, status updates, and slug uniqueness."""
import unittest

from moot import actions, db
from tests._util import fresh_store


def _reg(name):
    return actions.register(purpose="work", specialty="x",
                            proposed_name=name, history=None, origin="test")


class TestProjectRegistry(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")

    def test_register_assigns_code_and_defaults(self):
        p = actions.project_register(
            "Codey", "Body of Health Training Portal", leads="Codey Tutor")
        self.assertEqual(p["code"], "PRJ-001")
        self.assertEqual(p["slug"], "body-of-health-training-portal")
        self.assertEqual(p["channel"], "proj-body-of-health-training-portal")
        self.assertEqual(p["status"], "active")
        # second project increments the code
        p2 = actions.project_register("Codey", "Second Thing")
        self.assertEqual(p2["code"], "PRJ-002")

    def test_lookup_by_code_slug_channel(self):
        actions.project_register("Codey", "Training", slug="boh-training",
                                 channel="proj-training", ledger_file_id=6)
        by_code = db.project_get("PRJ-001")
        self.assertEqual(by_code["slug"], "boh-training")
        self.assertEqual(db.project_get("boh-training")["code"], "PRJ-001")
        self.assertEqual(db.project_get("#proj-training")["code"], "PRJ-001")
        self.assertEqual(db.project_get("prj-001")["code"], "PRJ-001")  # ci
        self.assertIsNone(db.project_get("nope"))

    def test_register_opens_channel_and_announces(self):
        actions.project_register("Codey", "Training", slug="boh-training",
                                 channel="proj-training")
        self.assertTrue(db.channel_exists("proj-training"))
        posts = db.channel_posts("general", 0, 20)
        self.assertTrue(any("PRJ-001" in (p["body"] or "") for p in posts))

    def test_slug_must_be_unique(self):
        actions.project_register("Codey", "Training", slug="boh-training")
        with self.assertRaises(ValueError):
            actions.project_register("Codey", "Other", slug="boh-training")

    def test_update_status_and_ledger(self):
        actions.project_register("Codey", "Training", slug="boh-training")
        actions.project_update("Codey", "PRJ-001", ledger_file_id=6)
        self.assertEqual(db.project_get("PRJ-001")["ledger_file_id"], 6)
        actions.project_update("Codey", "boh-training", status="shipped")
        self.assertEqual(db.project_get("PRJ-001")["status"], "shipped")
        self.assertEqual([p["code"] for p in db.projects_all("active")], [])
        self.assertEqual([p["code"] for p in db.projects_all("shipped")], ["PRJ-001"])

    def test_bad_status_rejected(self):
        actions.project_register("Codey", "Training", slug="boh-training")
        with self.assertRaises(ValueError):
            actions.project_update("Codey", "PRJ-001", status="frozen")

    def test_search_finds_project(self):
        actions.project_register("Codey", "Body of Health Training", slug="boh-training",
                                 channel="proj-training")
        hits = db.search("Health", kinds=["project"])
        if db.fts_enabled():   # LIKE fallback doesn't index projects
            self.assertTrue(any("PRJ-001" in (h.get("aid", "") + h.get("snippet", ""))
                                or h["kind"] == "project" for h in hits))


if __name__ == "__main__":
    unittest.main()
