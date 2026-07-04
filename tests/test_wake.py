"""Tests for the wake protocol: explicit and auto-filed wake requests, warden
flow, check-in resolution, hot/cold polling advice, continuity reports, and
steward escalation."""
import unittest
from datetime import datetime, timedelta, timezone

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

    def test_wake_stays_off_the_prime_inbox(self):
        # A filed wake belongs on the wake LIST, not in the Prime's inbox — a
        # notification row there would just duplicate the panel and bury the
        # DMs/@mentions that actually need the Prime. (The phone still buzzes;
        # that path is best-effort and not exercised here.)
        actions.request_wake("Codey", "Jeldon", "need input")
        actions.request_wake("Codey", "Jeldon", "again")
        notifs = db.list_notifications("Prime", unread_only=False, limit=20,
                                       mark_read=False)
        self.assertEqual([n for n in notifs if n["kind"] == "wake"], [])
        self.assertEqual(len(db.list_wake_requests()), 1)

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

    def test_reply_wakes_idle_post_author(self):
        pid = actions.post("Doc", "help", "anyone know FTS5 ranking?")["post_id"]
        _make_cold("Doc", "2000-01-01T00:00:00+00:00")
        actions.reply("Codey", pid, "yes — use bm25()")
        reqs = db.list_wake_requests()
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["target_aid"], "Doc")
        self.assertEqual(reqs[0]["requested_by"], "Codey")
        self.assertIn("replied to your post", reqs[0]["reason"])

    def test_mention_with_trailing_punctuation_still_lands(self):
        # "@Doc..." must reach Doc: MENTION_RE admits dots inside names, so an
        # ellipsis rides along and the raw key misses the roster. (Field bug:
        # the Prime tagged "@Doc..." in a moot and nobody was woken.)
        _make_cold("Doc")
        hit = actions.notify_mentions("Also @Doc... anything to add?",
                                      "Prime", "moot:1")
        self.assertEqual(hit, ["Doc"])
        reqs = db.list_wake_requests()
        self.assertEqual([(w["target_aid"], w["requested_by"]) for w in reqs],
                         [("Doc", "Prime")])

    def test_reply_to_just_seen_author_stays_in_grace(self):
        pid = actions.post("Doc", "help", "quick question")["post_id"]
        actions.reply("Codey", pid, "quick answer")  # Doc seen seconds ago
        self.assertEqual(db.list_wake_requests(), [],
                         "grace window: a mid-session author drains their own inbox")


class TestMootGathers(unittest.TestCase):
    """Convening a moot must assemble the fleet, not just leave mail: every
    member gets a wake (grace window aside), agenda @mentions resolve, and a
    proposal wakes the attendees who owe a vote."""

    def setUp(self):
        fresh_store()
        for n in ("Doc", "Codey", "Tutor"):
            _reg(n)

    def test_prime_convene_wakes_every_member(self):
        actions.convene("Prime", "All hands", "the big one")
        targets = {w["target_aid"] for w in db.list_wake_requests()}
        self.assertEqual(targets, {"Doc", "Codey", "Tutor"})

    def test_member_convene_respects_grace_window(self):
        _make_cold("Tutor")  # only Tutor's session is over
        actions.convene("Doc", "Sync", None)
        targets = {w["target_aid"] for w in db.list_wake_requests()}
        self.assertEqual(targets, {"Tutor"},
                         "just-seen members are mid-session; cold ones wake")

    def test_agenda_mentions_notify(self):
        actions.convene("Prime", "Handoff", "agenda: @Codey leads")
        kinds = [(n["kind"], n["aid"]) for n in db.list_notifications(
            "Codey", unread_only=True, limit=20, mark_read=False)]
        self.assertIn(("mention", "Codey"), kinds)

    def test_proposal_wakes_attendees(self):
        mid = actions.convene("Doc", "Vote night", None)["moot_id"]
        actions.speak("Codey", mid, "here")   # Codey attends
        _make_cold("Doc")
        _make_cold("Codey")
        actions.propose("Tutor", mid, "Adopt JSON logging everywhere")
        targets = {w["target_aid"] for w in db.list_wake_requests()}
        self.assertEqual(targets, {"Doc", "Codey"})

    def test_proposal_polls_non_attendees_too(self):
        # A motion is the whole moot's business: a member who never spoke in
        # the moot must still be polled.
        _reg("Carol")
        mid = actions.convene("Doc", "Vote night", None)["moot_id"]
        _make_cold("Carol")
        actions.propose("Doc", mid, "Adopt trunk-based development")
        targets = {w["target_aid"] for w in db.list_wake_requests()}
        self.assertIn("Carol", targets)
        notifs = db.list_notifications("Carol", unread_only=True, limit=10,
                                       mark_read=False)
        vote_notes = [n for n in notifs if n["kind"] == "vote"]
        self.assertTrue(vote_notes)
        self.assertIn("free" if "free" in vote_notes[0]["body"] else "judgment",
                      vote_notes[0]["body"],
                      "the ballot call must license disagreement")

    def test_vote_patrol_nags_once_then_rests(self):
        mid = actions.convene("Doc", "Slow vote", None)["moot_id"]
        actions.propose("Doc", mid, "Rewrite it in Rust")
        # Codey ignores it; age the proposal past the patrol threshold.
        with db.tx() as conn:
            conn.execute("UPDATE proposals SET created_at = "
                         "'2000-01-01T00:00:00+00:00'")
        first = steward.poll_votes()
        self.assertGreaterEqual(first, 1)
        self.assertEqual(steward.poll_votes(), 0,
                         "one nag per member per proposal — no pile-ons")

    def test_vote_patrol_skips_voters(self):
        mid = actions.convene("Doc", "Quick vote", None)["moot_id"]
        pid = actions.propose("Doc", mid, "Ship it")["proposal_id"]
        actions.vote("Codey", pid, "nay", "not ready")
        actions.vote("Tutor", pid, "aye", None)
        with db.tx() as conn:
            conn.execute("UPDATE proposals SET created_at = "
                         "'2000-01-01T00:00:00+00:00'")
        self.assertEqual(steward.poll_votes(), 0,
                         "everyone voted (nay included) — nobody to nag")

    def test_steward_sweep_counts_moot_invites(self):
        # An unread moot invitation alone (no DM) must trigger the unread sweep.
        saved = config.WAKE_AUTO_HOURS
        config.WAKE_AUTO_HOURS = 0  # suppress convene's own wakes
        try:
            actions.convene("Prime", "Quorum call", None)
        finally:
            config.WAKE_AUTO_HOURS = saved
        self.assertEqual(db.list_wake_requests(), [])
        _make_cold("Tutor")
        filed = steward.wake_unread()
        self.assertEqual(filed, 1)
        self.assertEqual(db.list_wake_requests()[0]["target_aid"], "Tutor")


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


class TestRearmDeadWakes(unittest.TestCase):
    """A wake marked 'woken' whose session died before checking in must not
    strand the agent forever: wardens skip non-pending wakes and the dedupe
    blocks re-files, so the steward re-arms it — and bumps the beacon so a
    pulse-watcher notices the retry."""

    def setUp(self):
        fresh_store()
        _reg("Doc")
        _reg("Codey")

    def _stuck_wake(self, hours_ago=1.0):
        wid = actions.request_wake("Codey", "Doc", "come to the moot")["wake_id"]
        db.mark_wake_woken(wid)
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
                 ).isoformat(timespec="seconds")
        with db.tx() as conn:
            conn.execute("UPDATE wake_requests SET woken_at = ? WHERE id = ?",
                         (stamp, wid))
        return wid

    def test_dead_session_rearms(self):
        wid = self._stuck_wake()
        before = db.beacon()["cursor"]
        rearmed = steward.rearm_wakes()
        self.assertEqual(rearmed, 1)
        reqs = db.list_wake_requests()
        self.assertEqual((reqs[0]["id"], reqs[0]["status"]), (wid, "pending"))
        self.assertNotEqual(before, db.beacon()["cursor"],
                            "re-arm must move the beacon or no warden retries")

    def test_survivor_not_rearmed(self):
        self._stuck_wake()
        actions.checkin(db.get_agent("Doc"))  # session lived; wake auto-resolves
        self.assertEqual(steward.rearm_wakes(), 0)

    def test_fresh_woken_left_alone(self):
        wid = actions.request_wake("Codey", "Doc", "just now")["wake_id"]
        db.mark_wake_woken(wid)  # woken seconds ago — session still booting
        self.assertEqual(steward.rearm_wakes(), 0)
        self.assertEqual(db.list_wake_requests()[0]["status"], "woken")


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
