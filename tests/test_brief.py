"""Tests for moot_brief / MOOT_REP.md: the agent's actionable moot-state as
structured data + rendered Markdown, read-only, projecting hub truth into the
agent's repo."""
import unittest

from moot import actions, db
from tests._util import fresh_store


def _reg(name, specialty="x"):
    return actions.register(purpose="work", specialty=specialty,
                            proposed_name=name, history=None, origin="test")


class TestBrief(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey", "backend")
        _reg("Tutor", "education")

    def test_brief_gathers_tasks_dms_projects(self):
        actions.project_register("Codey", "Body of Health Training Portal",
                                 slug="boh-training", channel="proj-training",
                                 ledger_file_id=6, leads="Codey Tutor")
        actions.task_add("Tutor", "Port the login flow", assignee="Codey",
                         channel="proj-training")
        actions.dm("Tutor", "Codey", "reference data model incoming")
        b = actions.brief(db.get_agent("Codey"))
        self.assertEqual(b["aid"], "Codey")
        self.assertEqual(b["live_url"], "/v1/brief.md")
        self.assertEqual(len(b["tasks"]), 1)
        self.assertEqual(len(b["unread_dms"]), 1)
        self.assertEqual([p["code"] for p in b["projects"]], ["PRJ-001"])
        md = b["markdown"]
        self.assertIn("# MOOT_REP — Codey's moot state", md)
        self.assertIn("Port the login flow", md)
        self.assertIn("DM from Tutor", md)
        self.assertIn("PRJ-001", md)
        self.assertIn("supersedes=#6", md)

    def test_brief_is_read_only(self):
        actions.dm("Tutor", "Codey", "unread please stay unread")
        actions.brief(db.get_agent("Codey"))
        actions.brief(db.get_agent("Codey"))
        # The DM must still be unread — brief must not drain the inbox.
        self.assertEqual(db.unread_count("Codey"), 1)

    def test_brief_does_not_touch_presence(self):
        before = db.get_agent("Codey")["last_seen"]
        actions.brief(db.get_agent("Codey"))
        self.assertEqual(db.get_agent("Codey")["last_seen"], before)

    def test_brief_shows_votes_due(self):
        mid = actions.convene("Tutor", "Decide", None)["moot_id"]
        actions.propose("Tutor", mid, "Adopt trunk-based dev")
        b = actions.brief(db.get_agent("Codey"))
        self.assertTrue(b["votes_due"])
        self.assertIn("Vote due", b["markdown"])

    def test_empty_brief_is_graceful(self):
        b = actions.brief(db.get_agent("Codey"))
        self.assertIn("none", b["markdown"])
        self.assertIn("nothing addressed to you", b["markdown"])

    def test_brief_points_at_the_live_url_not_a_repo_file(self):
        # The rep is live-primarily: the doc names its canonical hub URL and the
        # note offers export, rather than telling the agent to write a repo copy.
        b = actions.brief(db.get_agent("Codey"))
        self.assertIn("/v1/brief.md", b["markdown"])
        self.assertIn("export_rep.py", b["note"])
        self.assertNotIn("write_to", b)


if __name__ == "__main__":
    unittest.main()
