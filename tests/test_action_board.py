"""Action Board: promote candidates, idea→assign→ship pipeline."""
import unittest

from moot import actions, db
from tests._util import fresh_store


def _reg(name):
    return actions.register(purpose="work", specialty="x",
                            proposed_name=name, history=None, origin="test")


class TestActionBoard(unittest.TestCase):
    def setUp(self):
        fresh_store()
        for n in ("Codey", "Tutor", "Garfield"):
            _reg(n)
        actions.post("Codey", "skunkworks",
                     "We should export the briefing skill to Cursor.",
                     title="Skill export idea")

    def test_unassigned_task_starts_as_idea(self):
        tid = actions.task_add("Prime", "park this thought")["task_id"]
        self.assertEqual(db.task_get(tid)["status"], "idea")

    def test_assigned_task_starts_open(self):
        tid = actions.task_add("Prime", "build it", assignee="Tutor")["task_id"]
        self.assertEqual(db.task_get(tid)["status"], "open")

    def test_promote_post_idempotent(self):
        posts = db.feed_posts(scope="feed", sort="new", q=None, limit=5)
        self.assertTrue(posts)
        pid = posts[0]["id"]
        out = actions.promote_to_action("Prime", "post", pid)
        self.assertTrue(out["created"])
        task = out["task"]
        self.assertEqual(task["status"], "idea")
        self.assertEqual(task["source_kind"], "post")
        self.assertEqual(task["source_id"], pid)
        again = actions.promote_to_action("Prime", "post", pid)
        self.assertFalse(again["created"])
        self.assertEqual(again["task"]["id"], task["id"])

    def test_assign_promotes_idea_to_open(self):
        tid = actions.task_add("Prime", "needs an owner")["task_id"]
        actions.task_update("Prime", tid, assignee="Codey")
        t = db.task_get(tid)
        self.assertEqual(t["status"], "open")
        self.assertEqual(t["assignee"], "Codey")

    def test_ship_attests_gold(self):
        tid = actions.task_add("Prime", "ship me", assignee="Tutor")["task_id"]
        actions.task_update("Tutor", tid, status="done")
        bare = actions.ship_action("Prime", tid)
        self.assertEqual(bare["task"]["status"], "shipped")
        self.assertFalse(bare["gold_attested"])
        self.assertIn("warning", bare)

        tid2 = actions.task_add("Prime", "ship with url", assignee="Tutor")["task_id"]
        actions.task_update("Tutor", tid2, status="done")
        gold = actions.ship_action(
            "Prime", tid2,
            pr_url="https://github.com/example/moot/pull/7")
        self.assertTrue(gold["gold_attested"])
        self.assertNotIn("warning", gold)
        self.assertIsNotNone(db.task_get(tid2)["shipped_at"])

    def test_action_board_aggregates(self):
        posts = db.feed_posts(scope="feed", sort="new", q=None, limit=1)
        actions.promote_to_action("Prime", "post", posts[0]["id"])
        actions.task_add("Prime", "active work", assignee="Tutor")
        done = actions.task_add("Prime", "landed already", assignee="Codey")["task_id"]
        actions.task_update("Codey", done, status="done")
        board = db.action_board()
        self.assertGreaterEqual(board["counts"]["ideas"], 1)
        self.assertGreaterEqual(board["counts"]["active"], 1)
        self.assertGreaterEqual(len(board["landed"]), 1)
        self.assertIn("candidates", board)

    def test_candidates_exclude_promoted(self):
        posts = db.feed_posts(scope="feed", sort="new", q=None, limit=1)
        pid = posts[0]["id"]
        before = {c["source_id"] for c in db.action_candidates()
                  if c["source_kind"] == "post"}
        self.assertIn(pid, before)
        actions.promote_to_action("Prime", "post", pid)
        after = {c["source_id"] for c in db.action_candidates()
                 if c["source_kind"] == "post"}
        self.assertNotIn(pid, after)


if __name__ == "__main__":
    unittest.main()
