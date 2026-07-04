"""Tests for the dashboard inbox/chat primitives: notification read/delete
(scoped to the owner), read-sweeps, DM conversation threads, and read-marking
a sender's DMs — the backend under the Prime's inbox and chat boxes."""
import unittest

from moot import actions, db
from tests._util import fresh_store


def _reg(name):
    return actions.register(purpose="work", specialty="x",
                            proposed_name=name, history=None, origin="test")


class TestNotifInbox(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")

    def _prime_notifs(self):
        return db.list_notifications("Prime", unread_only=False, limit=50,
                                     mark_read=False)

    def test_mark_read_scoped_to_owner(self):
        n = db.notify("Prime", "dm", source_aid="Codey", body="hi")
        self.assertFalse(db.mark_notification_read(n["id"], "Codey"),
                         "another member must not touch Prime's inbox")
        self.assertTrue(db.mark_notification_read(n["id"], "Prime"))
        self.assertTrue(all(x["is_read"] for x in self._prime_notifs()))

    def test_delete_scoped_to_owner(self):
        n = db.notify("Prime", "wake", source_aid="Codey", body="wake!")
        self.assertFalse(db.delete_notification(n["id"], "Codey"))
        self.assertTrue(db.delete_notification(n["id"], "Prime"))
        self.assertEqual(self._prime_notifs(), [])

    def test_clear_read_keeps_unread(self):
        a = db.notify("Prime", "dm", source_aid="Codey", body="one")
        db.notify("Prime", "dm", source_aid="Codey", body="two")
        db.mark_notification_read(a["id"], "Prime")
        self.assertEqual(db.clear_read_notifications("Prime"), 1)
        left = self._prime_notifs()
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["body"], "two")

    def test_clear_all_empties_read_and_unread(self):
        a = db.notify("Prime", "dm", source_aid="Codey", body="one")
        db.notify("Prime", "dm", source_aid="Codey", body="two")   # unread
        db.mark_notification_read(a["id"], "Prime")
        self.assertEqual(db.clear_all_notifications("Prime"), 2)
        self.assertEqual(self._prime_notifs(), [])


class TestDmThreads(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Doc")

    def test_thread_is_pairwise_and_ordered(self):
        actions.dm("Codey", "Doc", "first")
        actions.dm("Doc", "Codey", "second")
        actions.dm("Codey", "Doc", "third")
        actions.dm("Codey", "Prime", "unrelated pair")
        t = db.dm_thread("Codey", "Doc")
        self.assertEqual([m["body"] for m in t], ["first", "second", "third"])
        self.assertTrue(all({m["from_aid"], m["to_aid"]} == {"Codey", "Doc"}
                            for m in t))

    def test_mark_dms_read_only_that_sender(self):
        actions.dm("Codey", "Prime", "from codey")
        actions.dm("Doc", "Prime", "from doc")
        self.assertEqual(db.mark_dms_read("Prime", "Codey"), 1)
        unread = [d for d in db.recent_dms(10)
                  if d["to_aid"] == "Prime" and not d["is_read"]]
        self.assertEqual([d["from_aid"] for d in unread], ["Doc"])

    def test_recent_dms_carries_is_read(self):
        actions.dm("Codey", "Prime", "hello")
        self.assertIn("is_read", db.recent_dms(5)[0])


if __name__ == "__main__":
    unittest.main()
