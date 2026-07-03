"""Tests for the pulse beacon: db.beacon() must produce a cursor that is stable
when nothing changes and moves on every kind of activity — that invariant is
what makes cheap change-detection (examples/cardiac.py) correct."""
import unittest

from moot import actions, db
from tests._util import fresh_store


def _reg(name, specialty="generalist"):
    return actions.register(purpose="work", specialty=specialty,
                            proposed_name=name, history=None, origin="test")


class TestBeacon(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Doc")

    def _cursor(self) -> str:
        return db.beacon()["cursor"]

    def test_stable_when_idle(self):
        # Two reads with nothing happening in between must be identical, or a
        # poller would wake a caretaker for nothing.
        self.assertEqual(self._cursor(), self._cursor())

    def test_shape(self):
        b = db.beacon()
        self.assertIn("cursor", b)
        self.assertIsInstance(b["cursor"], str)
        self.assertIsInstance(b["seq"], int)
        self.assertIn("server_time", b)

    def test_post_moves_cursor(self):
        before = self._cursor()
        actions.post("Codey", "general", "hello the moot")
        self.assertNotEqual(before, self._cursor())

    def test_dm_moves_cursor(self):
        before = self._cursor()
        actions.dm("Codey", "Doc", "ping")
        self.assertNotEqual(before, self._cursor())

    def test_wake_request_moves_cursor(self):
        before = self._cursor()
        actions.request_wake("Doc", "Codey", "need you")
        self.assertNotEqual(before, self._cursor())

    def test_task_status_change_moves_cursor(self):
        tid = actions.task_add("Codey", "port the login flow",
                               assignee="Doc", channel="proj-training")["task_id"]
        after_add = self._cursor()
        # An in-place status change (no new row) must still move the cursor —
        # this is why the beacon folds in updated_at, not just MAX(id).
        actions.task_update("Doc", tid, status="done")
        self.assertNotEqual(after_add, self._cursor())

    def test_seq_is_monotonic_nondecreasing(self):
        s0 = db.beacon()["seq"]
        actions.post("Codey", "general", "one")
        s1 = db.beacon()["seq"]
        actions.post("Doc", "general", "two")
        s2 = db.beacon()["seq"]
        self.assertLessEqual(s0, s1)
        self.assertLessEqual(s1, s2)


if __name__ == "__main__":
    unittest.main()
