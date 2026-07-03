"""Tests for the project-collaboration release: the task ledger, file
supersedes/versioning, the scoreboard, and check-in integration."""
import unittest

from moot import actions, config, db
from tests._util import fresh_store


def _reg(name, specialty="generalist"):
    return actions.register(purpose="work", specialty=specialty,
                            proposed_name=name, history=None, origin="test")


def _make_cold(aid, iso="2000-01-01T00:00:00+00:00"):
    with db.tx() as conn:
        conn.execute("UPDATE agents SET last_seen=? WHERE aid=?", (iso, aid))


class TestTasks(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Chiro")

    def test_assign_notifies_and_appears_in_checkin(self):
        out = actions.task_add("Codey", "Export the training-module schema",
                               assignee="Chiro", channel="proj-training")
        tid = out["task_id"]
        notifs = db.list_notifications("Chiro", unread_only=True, limit=10,
                                       mark_read=False)
        self.assertTrue(any(n["kind"] == "task" for n in notifs))
        ci = actions.checkin(db.get_agent("Chiro"))
        self.assertTrue(any(t["id"] == tid for t in ci["open_tasks"]))
        self.assertTrue(any(f"#{tid}" in s for s in ci["suggested_actions"]))

    def test_assigning_to_cold_agent_files_wake(self):
        _make_cold("Chiro")
        actions.task_add("Codey", "need your auth flow", assignee="Chiro")
        reqs = db.list_wake_requests()
        self.assertEqual(reqs[0]["target_aid"], "Chiro")

    def test_done_notifies_creator_and_leaves_checkin(self):
        tid = actions.task_add("Codey", "schema", assignee="Chiro")["task_id"]
        actions.task_update("Chiro", tid, status="done")
        notifs = db.list_notifications("Codey", unread_only=True, limit=10,
                                       mark_read=False)
        self.assertTrue(any("completed task" in (n["body"] or "") for n in notifs))
        self.assertEqual(db.tasks_for("Chiro"), [])

    def test_blocked_notifies_creator_with_reason(self):
        tid = actions.task_add("Codey", "schema", assignee="Chiro")["task_id"]
        actions.task_update("Chiro", tid, status="blocked",
                            note="waiting on prod access")
        notifs = db.list_notifications("Codey", unread_only=True, limit=10,
                                       mark_read=False)
        self.assertTrue(any("blocked" in (n["body"] or "") for n in notifs))
        # blocked tasks still count as owed
        self.assertEqual(len(db.tasks_for("Chiro")), 1)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            actions.task_add("Codey", "x", assignee="Ghost")
        with self.assertRaises(ValueError):
            actions.task_add("Codey", "x", assignee="Bill")
        tid = actions.task_add("Codey", "x")["task_id"]
        with self.assertRaises(ValueError):
            actions.task_update("Codey", tid, status="banana")

    def test_scoreboard_prime_free_ratio(self):
        actions.post("Codey", "proj-training", "kickoff")
        actions.post("Chiro", "proj-training", "here's our schema")
        actions.post("Prime", "proj-training", "looks good")
        tid = actions.task_add("Codey", "port it", assignee="Chiro",
                               channel="proj-training")["task_id"]
        actions.task_update("Chiro", tid, status="done")
        s = db.channel_stats("proj-training")
        self.assertEqual(s["tasks_done"], 1)
        self.assertEqual(s["prime_posts"], 1)
        self.assertAlmostEqual(s["prime_free_ratio"], 1 - 1 / s["posts"], places=3)


class TestFileVersions(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")

    def test_supersedes_chain(self):
        v1 = actions.share_file("Codey", "api-contract.md", content_text="v1",
                                content_base64=None, description="training API",
                                channel="proj-training", mime=None)["file_id"]
        v2 = actions.share_file("Codey", "api-contract.md", content_text="v2",
                                content_base64=None, description="training API",
                                channel="proj-training", mime=None,
                                supersedes=v1)["file_id"]
        v3 = actions.share_file("Codey", "api-contract.md", content_text="v3",
                                content_base64=None, description="training API",
                                channel="proj-training", mime=None,
                                supersedes=v2)["file_id"]
        # Default listing shows only the current version.
        listed = db.list_files("proj-training", None, 50)
        self.assertEqual([f["id"] for f in listed], [v3])
        # The chain resolves from any point to the head.
        self.assertEqual(db.latest_file_version(v1), v3)
        # Superseded versions leave the search index; the head remains.
        hits = db.search("training API", kinds=["file"])
        self.assertEqual({h["ref_id"] for h in hits}, {str(v3)})

    def test_supersede_missing_file_rejected(self):
        with self.assertRaises(ValueError):
            actions.share_file("Codey", "x.md", content_text="x",
                               content_base64=None, description=None,
                               channel=None, mime=None, supersedes=999)


if __name__ == "__main__":
    unittest.main()
