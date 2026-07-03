"""Tests for v0.9.0: flood control, reactions/endorsements, pinned briefs,
steward task-nagging, quiet-hours push holding, and the Prime DM oversight log."""
import unittest

from moot import actions, config, db, notify, steward
from tests._util import fresh_store


def _reg(name, specialty="generalist"):
    return actions.register(purpose="work", specialty=specialty,
                            proposed_name=name, history=None, origin="test")


def _backdate_task(tid, iso):
    with db.tx() as conn:
        conn.execute("UPDATE tasks SET updated_at=?, nagged_at=NULL WHERE id=?",
                     (iso, tid))


class TestFloodControl(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Loopy")
        _reg("Watcher")

    def test_cap_refuses_and_pages_prime(self):
        config.POST_RATE_PER_HOUR = 5
        try:
            for i in range(5):
                actions.post("Loopy", "general", f"msg {i}")
            with self.assertRaises(ValueError) as cm:
                actions.post("Loopy", "general", "one too many")
            self.assertIn("Flood control", str(cm.exception))
            # Prime was paged exactly once
            notifs = db.list_notifications("Prime", unread_only=True, limit=20,
                                           mark_read=False)
            self.assertEqual(sum(1 for n in notifs if n["kind"] == "flood"), 1)
        finally:
            config.POST_RATE_PER_HOUR = 30

    def test_cap_off_when_zero(self):
        config.POST_RATE_PER_HOUR = 0
        try:
            for i in range(40):
                actions.post("Loopy", "general", f"m{i}")  # no raise
        finally:
            config.POST_RATE_PER_HOUR = 30


class TestReactions(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Tutor")

    def test_react_dedupes_and_notifies(self):
        pid = actions.post("Codey", "skunkworks", "a useful snippet")["post_id"]
        actions.react("Tutor", pid, "🔥")
        actions.react("Tutor", pid, "👍")  # replaces, not adds
        reacts = db.reactions_for([pid])[pid]
        self.assertEqual(len(reacts), 1)
        self.assertEqual(reacts[0]["emoji"], "👍")
        notifs = db.list_notifications("Codey", unread_only=True, limit=10,
                                       mark_read=False)
        self.assertTrue(any(n["kind"] == "reaction" for n in notifs))

    def test_reactions_received_feed_standing(self):
        pid = actions.post("Codey", "general", "hi")["post_id"]
        base = db.reputation().get("Codey", 0)
        actions.react("Tutor", pid, "👍")
        self.assertGreater(db.reputation()["Codey"], base)


class TestPins(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")

    def test_pin_surfaces_in_read(self):
        pid = actions.post("Codey", "proj-x", "current state: scaffolding done")["post_id"]
        db.pin_post(pid, True)
        pins = db.pinned_posts("proj-x")
        self.assertEqual([p["id"] for p in pins], [pid])
        db.pin_post(pid, False)
        self.assertEqual(db.pinned_posts("proj-x"), [])


class TestTaskNagging(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Tutor")

    def test_stale_open_task_nags_assignee_once(self):
        tid = actions.task_add("Codey", "port the schema", assignee="Tutor")["task_id"]
        _backdate_task(tid, "2000-01-01T00:00:00+00:00")
        self.assertEqual(steward.nag_tasks(), 1)
        self.assertEqual(steward.nag_tasks(), 0)  # nagged_at now recent
        notifs = db.list_notifications("Tutor", unread_only=True, limit=10,
                                       mark_read=False)
        self.assertTrue(any("sat open" in (n["body"] or "") for n in notifs))

    def test_blocked_task_nags_creator(self):
        tid = actions.task_add("Codey", "x", assignee="Tutor")["task_id"]
        actions.task_update("Tutor", tid, status="blocked", note="need prod access")
        _backdate_task(tid, "2000-01-01T00:00:00+00:00")
        self.assertEqual(steward.nag_tasks(), 1)
        notifs = db.list_notifications("Codey", unread_only=True, limit=10,
                                       mark_read=False)
        self.assertTrue(any("still blocked" in (n["body"] or "") for n in notifs))

    def test_fresh_task_not_nagged(self):
        actions.task_add("Codey", "brand new", assignee="Tutor")
        self.assertEqual(steward.nag_tasks(), 0)


class TestQuietHours(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")

    def test_pushes_held_and_flushed(self):
        config.PRIME_PUSH_URL = "https://ntfy.example/topic"
        try:
            # Force "quiet" by covering all 24 hours.
            config.QUIET_HOURS_UTC = "0-24"
            self.assertTrue(notify.in_quiet_hours())
            notify.push_prime("t", "held one")
            notify.push_prime("t", "held two")
            self.assertEqual(int(db.meta_get("held_pushes")), 2)
            # Outside quiet hours, the steward flushes to a single summary.
            config.QUIET_HOURS_UTC = ""
            self.assertEqual(notify.flush_held_pushes(), 2)
            self.assertEqual(int(db.meta_get("held_pushes")), 0)
        finally:
            config.PRIME_PUSH_URL = None
            config.QUIET_HOURS_UTC = ""

    def test_malformed_spec_is_never_quiet(self):
        config.QUIET_HOURS_UTC = "garbage"
        self.assertFalse(notify.in_quiet_hours())
        config.QUIET_HOURS_UTC = ""


class TestOversightLog(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Tutor")

    def test_prime_sees_all_dms(self):
        actions.dm("Codey", "Tutor", "psst, what's your auth schema")
        actions.dm("Tutor", "Codey", "here it is")
        log = db.recent_dms(10)
        self.assertEqual(len(log), 2)
        bodies = " ".join(d["body"] for d in log)
        self.assertIn("auth schema", bodies)


if __name__ == "__main__":
    unittest.main()
