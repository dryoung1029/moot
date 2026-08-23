"""Dispatch board, project health, git-linked tasks, and soft gold attestation."""
import unittest

from moot import actions, config, db
from tests._util import fresh_store


def _reg(name):
    return actions.register(purpose="work", specialty="x",
                            proposed_name=name, history=None, origin="test")


class TestDispatchBoard(unittest.TestCase):
    def setUp(self):
        fresh_store()
        for n in ("Codey", "Tutor", "Garfield"):
            _reg(n)
        self._saved = config.KEEPER_AID
        config.KEEPER_AID = "Garfield"

    def tearDown(self):
        config.KEEPER_AID = self._saved

    def _carry_to_desk(self):
        mid = actions.convene("Codey", "Ship the dispatch board", None)["moot_id"]
        pid = actions.propose(
            "Codey", mid, "Adopt the Prime dispatch board")["proposal_id"]
        # electorate 3 → majority 2
        actions.vote("Tutor", pid, "aye", None)
        out = actions.vote("Garfield", pid, "aye", None)
        self.assertEqual(out["status"], "awaiting_prime")
        return pid

    def test_dispatch_surfaces_signature_queue(self):
        pid = self._carry_to_desk()
        board = db.dispatch_board()
        ids = [p["id"] for p in board["needs_prime"]["signatures"]]
        self.assertIn(pid, ids)
        self.assertGreaterEqual(board["counts"]["needs_prime"], 1)

    def test_execute_files_keeper_task_and_fills_executive_queue(self):
        pid = self._carry_to_desk()
        out = actions.execute_proposal(pid)
        self.assertEqual(out["status"], "carried")
        self.assertIsNotNone(out.get("keeper_task_id"))
        board = db.dispatch_board()
        exec_ids = [p["id"] for p in board["needs_keeper"]["executive_queue"]]
        self.assertIn(pid, exec_ids)
        task = db.task_get(out["keeper_task_id"])
        self.assertEqual(task["assignee"], "Garfield")
        self.assertEqual(task["status"], "open")

    def test_blocked_task_lands_in_stuck(self):
        tid = actions.task_add("Codey", "stuck thing", assignee="Tutor")["task_id"]
        actions.task_update("Tutor", tid, status="blocked", note="waiting on schema")
        board = db.dispatch_board()
        blocked_ids = [t["id"] for t in board["stuck"]["blocked_tasks"]]
        self.assertIn(tid, blocked_ids)

    def test_task_git_fields_round_trip(self):
        out = actions.task_add(
            "Codey", "wire dispatch", assignee="Tutor",
            repo_url="https://github.com/example/moot",
            branch="feature/dispatch",
            pr_url="https://github.com/example/moot/pull/42",
        )
        t = db.task_get(out["task_id"])
        self.assertEqual(t["repo_url"], "https://github.com/example/moot")
        self.assertEqual(t["branch"], "feature/dispatch")
        self.assertEqual(t["pr_url"], "https://github.com/example/moot/pull/42")
        actions.task_update("Tutor", out["task_id"],
                            pr_url="https://github.com/example/moot/pull/43")
        self.assertEqual(db.task_get(out["task_id"])["pr_url"],
                         "https://github.com/example/moot/pull/43")

    def test_mark_executed_soft_gold_warning(self):
        pid = self._carry_to_desk()
        actions.execute_proposal(pid)
        bare = actions.mark_executed("Garfield", pid, "built it on a branch")
        self.assertTrue(bare["executed"])
        self.assertFalse(bare["gold_attested"])
        self.assertIn("warning", bare)

    def test_mark_executed_with_url_attests_gold(self):
        pid = self._carry_to_desk()
        actions.execute_proposal(pid)
        mid = actions.convene("Codey", "Gold trail", None)["moot_id"]
        pid2 = actions.propose(
            "Codey", mid, "Require PR URLs on execute")["proposal_id"]
        actions.vote("Tutor", pid2, "aye", None)
        actions.vote("Garfield", pid2, "aye", None)
        actions.execute_proposal(pid2)
        out = actions.mark_executed(
            "Garfield", pid2,
            "Shipped in https://github.com/example/moot/pull/99")
        self.assertTrue(out["gold_attested"])
        self.assertNotIn("warning", out)

    def test_projects_health_counts_tasks(self):
        p = actions.project_register(
            "Codey", "Dispatch Remodel", slug="dispatch",
            channel="proj-dispatch")
        actions.task_add("Codey", "build board", assignee="Tutor",
                         channel="proj-dispatch")
        actions.task_add("Codey", "blocked bit", assignee="Tutor",
                         channel="proj-dispatch")
        tid = actions.task_add("Codey", "parked", assignee="Tutor",
                               channel="proj-dispatch")["task_id"]
        actions.task_update("Tutor", tid, status="blocked", note="deps")
        health = {row["code"]: row for row in db.projects_health()}
        row = health[p["code"]]
        self.assertEqual(row["tasks_open"], 2)
        self.assertEqual(row["tasks_blocked"], 1)


if __name__ == "__main__":
    unittest.main()
