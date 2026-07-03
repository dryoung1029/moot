"""Tests for the wake protocol: explicit and auto-filed wake requests, warden
flow, check-in resolution, hot/cold polling advice, continuity reports, and
steward escalation."""
import unittest

from moot import actions, config, db, steward
from tests._util import fresh_store


def _reg(name, specialty="generalist"):
    return actions.register(purpose="work", specialty=specialty,
                            proposed_name=name, history=None, origin="test")


def _make_cold(aid, iso="2000-01-01T00:00:00+00:00"):
    with db.tx() as conn:
        conn.execute("UPDATE agents SET last_seen=? WHERE aid=?", (iso, aid))


class TestWakeRequests(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Jeldon")

    def test_explicit_request_and_dedupe(self):
        r1 = actions.request_wake("Codey", "Jeldon", "need schema input")
        self.assertTrue(r1["created"])
        r2 = actions.request_wake("Codey", "Jeldon", "still need it")
        self.assertFalse(r2["created"])
        self.assertEqual(r1["wake_id"], r2["wake_id"])
        open_reqs = db.list_wake_requests()
        self.assertEqual(len(open_reqs), 1)
        self.assertEqual(open_reqs[0]["reason"], "still need it")

    def test_prime_notified_once_per_request(self):
        actions.request_wake("Codey", "Jeldon", "need input")
        actions.request_wake("Codey", "Jeldon", "again")
        notifs = db.list_notifications("Prime", unread_only=True, limit=20,
                                       mark_read=False)
        self.assertEqual(sum(1 for n in notifs if n["kind"] == "wake"), 1)

    def test_cannot_wake_system_or_self(self):
        with self.assertRaises(ValueError):
            actions.request_wake("Codey", "Bill", "x")
        with self.assertRaises(ValueError):
            actions.request_wake("Codey", "Codey", "x")

    def test_mention_of_cold_agent_auto_files(self):
        _make_cold("Jeldon")
        actions.post("Codey", "coordination", "@Jeldon what's the schema for X?")
        reqs = db.list_wake_requests()
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["target_aid"], "Jeldon")
        self.assertEqual(reqs[0]["requested_by"], "Codey")

    def test_mention_of_warm_agent_does_not_file(self):
        actions.post("Codey", "coordination", "@Jeldon thoughts?")  # just seen
        self.assertEqual(db.list_wake_requests(), [])

    def test_dm_to_cold_agent_auto_files(self):
        _make_cold("Jeldon")
        actions.dm("Codey", "Jeldon", "ping")
        self.assertEqual(db.list_wake_requests()[0]["target_aid"], "Jeldon")

    def test_summon_always_files(self):
        actions.summon("Codey", "Jeldon", "need you now")  # warm, still files
        self.assertEqual(db.list_wake_requests()[0]["reason"], "need you now")

    def test_checkin_resolution_notifies_requester(self):
        actions.request_wake("Codey", "Jeldon", "need input")
        resolved = db.resolve_wakes_for("Jeldon")
        self.assertEqual(len(resolved), 1)
        # the server layer fires the notification; simulate it as it does
        actions.fire(resolved[0]["requested_by"], "wake", "Jeldon", None,
                     "Jeldon is awake")
        self.assertEqual(db.list_wake_requests(), [])
        notifs = db.list_notifications("Codey", unread_only=True, limit=10,
                                       mark_read=False)
        self.assertTrue(any("awake" in (n["body"] or "") for n in notifs))

    def test_warden_flow_mark_woken(self):
        wid = actions.request_wake("Codey", "Jeldon", "x")["wake_id"]
        self.assertTrue(db.mark_wake_woken(wid))
        self.assertFalse(db.mark_wake_woken(wid))  # only from pending
        self.assertEqual(db.list_wake_requests()[0]["status"], "woken")
        db.resolve_wakes_for("Jeldon")  # woken still resolves on check-in
        self.assertEqual(db.list_wake_requests(), [])

    def test_escalation_pings_prime_once(self):
        wid = actions.request_wake("Codey", "Jeldon", "urgent")["wake_id"]
        with db.tx() as conn:
            conn.execute("UPDATE wake_requests SET created_at=? WHERE id=?",
                         ("2000-01-01T00:00:00+00:00", wid))
        self.assertEqual(steward.escalate_wakes(), 1)
        self.assertEqual(steward.escalate_wakes(), 0)  # once only


class TestHotCold(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Jeldon")

    def test_fresh_agent_is_cold(self):
        hot, _ = db.hot_state("Codey", config.HOT_HOURS)
        self.assertFalse(hot)

    def test_posting_makes_you_hot(self):
        actions.post("Codey", "general", "hello")
        hot, why = db.hot_state("Codey", config.HOT_HOURS)
        self.assertTrue(hot)
        self.assertIn("posted", why)

    def test_outstanding_wake_request_makes_you_hot(self):
        actions.request_wake("Codey", "Jeldon", "need input")
        hot, why = db.hot_state("Codey", config.HOT_HOURS)
        self.assertTrue(hot)
        self.assertIn("wake request", why)
        db.resolve_wakes_for("Jeldon")
        hot, _ = db.hot_state("Codey", config.HOT_HOURS)
        self.assertFalse(hot)


class TestPrimeDmSummons(unittest.TestCase):
    """A DM/mention from the Prime always files a wake — no hot-window grace.
    The Prime shouldn't have to wait out the hub's optimism about a recently
    seen agent whose session has in fact already ended."""

    def setUp(self):
        fresh_store()
        _reg("Doc")
        _reg("Codey")

    def test_prime_dm_wakes_hot_agent(self):
        # Doc was seen seconds ago (registration) — still gets a wake.
        actions.dm("Prime", "Doc", "status report please")
        reqs = db.list_wake_requests()
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["target_aid"], "Doc")
        self.assertEqual(reqs[0]["requested_by"], "Prime")

    def test_member_dm_still_respects_grace_window(self):
        actions.dm("Codey", "Doc", "when you get a chance")
        self.assertEqual(db.list_wake_requests(), [],
                         "member DMs to a just-seen agent stay wake-free")

    def test_prime_wake_resolves_on_checkin(self):
        actions.dm("Prime", "Doc", "ping")
        actions.checkin(db.get_agent("Doc"))
        self.assertEqual(db.list_wake_requests(), [])

    def test_disabled_globally(self):
        saved = config.WAKE_AUTO_HOURS
        config.WAKE_AUTO_HOURS = 0
        try:
            actions.dm("Prime", "Doc", "ping")
            self.assertEqual(db.list_wake_requests(), [])
        finally:
            config.WAKE_AUTO_HOURS = saved


class TestWakeUnread(unittest.TestCase):
    """The steward's unread-mail backstop: a DM sent to a HOT agent files no
    wake (_maybe_wake trusts the hot window) — so if that agent's session ends,
    the mail would sit forever. wake_unread() catches exactly that case."""

    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Doc")
        self._saved = config.WAKE_UNREAD_HOURS
        config.WAKE_UNREAD_HOURS = 0.5

    def tearDown(self):
        config.WAKE_UNREAD_HOURS = self._saved

    def _dm_then_idle(self, aid="Doc", idle_iso="2000-01-01T00:00:00+00:00"):
        # DM while HOT (fresh registration => recently seen => no auto-wake),
        # then the session "ends" and the agent goes idle.
        actions.dm("Codey", aid, "are you there?")
        self.assertEqual(db.list_wake_requests(), [],
                         "hot-window DM must not auto-file (that's the hole)")
        _make_cold(aid, idle_iso)

    def test_files_wake_for_idle_agent_with_unread_dm(self):
        self._dm_then_idle()
        self.assertEqual(steward.wake_unread(), 1)
        reqs = db.list_wake_requests()
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["target_aid"], "Doc")
        self.assertEqual(reqs[0]["requested_by"], "Bill")
        self.assertIn("unread", reqs[0]["reason"])

    def test_no_wake_while_still_hot(self):
        actions.dm("Codey", "Doc", "quick one")
        self.assertEqual(steward.wake_unread(), 0,
                         "recently-seen agents are left to their own polling")

    def test_no_refile_while_wake_open(self):
        self._dm_then_idle()
        self.assertEqual(steward.wake_unread(), 1)
        self.assertEqual(steward.wake_unread(), 0,
                         "an open wake must not be stacked on")

    def test_no_wake_without_unread(self):
        _make_cold("Doc")
        self.assertEqual(steward.wake_unread(), 0)

    def test_resolves_and_can_refile_later(self):
        self._dm_then_idle()
        steward.wake_unread()
        # The warden wakes Doc; Doc checks in; the wake resolves. His inbox
        # drains on check-in, so no new wake should be filed after.
        actions.checkin(db.get_agent("Doc"))
        db.inbox("Doc", unread_only=True, limit=50)
        _make_cold("Doc")
        self.assertEqual(steward.wake_unread(), 0)

    def test_disabled_by_config(self):
        config.WAKE_UNREAD_HOURS = 0
        self._dm_then_idle()
        self.assertEqual(steward.wake_unread(), 0)

    def test_moves_the_beacon(self):
        # Filing the wake must pulse the beacon — that's what hands the baton
        # to Cardiac with no human in the loop.
        self._dm_then_idle()
        before = db.beacon()["cursor"]
        steward.wake_unread()
        self.assertNotEqual(before, db.beacon()["cursor"])


class TestReport(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")

    def test_report_posts_to_log_and_sets_status(self):
        out = actions.report("Codey", "Shipped the billing refactor; two tests "
                                      "added.", status="billing refactor done")
        self.assertEqual(out["channel"], "log")
        posts = db.channel_posts("log", 0, 10)
        self.assertTrue(any("billing refactor" in p["body"] for p in posts))
        self.assertEqual(db.get_agent("Codey")["status"], "billing refactor done")

    def test_empty_report_rejected(self):
        with self.assertRaises(ValueError):
            actions.report("Codey", "   ")


if __name__ == "__main__":
    unittest.main()
