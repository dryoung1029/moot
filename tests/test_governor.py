"""Tests for the governor model: a simple majority of the electorate closes
the house's business — aye-majorities go to the Prime's desk (nothing carries
without the signature), nay-majorities fail outright, and the Prime may veto
any motion at any stage."""
import unittest

from moot import actions, config, db
from tests._util import fresh_store

FLEET = ["Doc", "Codey", "Tutor", "Jeldon", "Carol"]  # electorate 5, majority 3


def _reg(name):
    return actions.register(purpose="work", specialty="x",
                            proposed_name=name, history=None, origin="test")


class TestGovernor(unittest.TestCase):
    def setUp(self):
        fresh_store()
        for n in FLEET:
            _reg(n)
        self.mid = actions.convene("Doc", "Lawmaking", None)["moot_id"]
        self.pid = actions.propose("Doc", self.mid, "Adopt JSON logging")["proposal_id"]

    def _status(self):
        return db.get_proposal(self.pid)["status"]

    def test_aye_majority_goes_to_the_primes_desk(self):
        actions.vote("Codey", self.pid, "aye", None)
        actions.vote("Tutor", self.pid, "aye", None)
        self.assertEqual(self._status(), "open", "2 of 5 is not a majority")
        out = actions.vote("Jeldon", self.pid, "aye", None)
        self.assertEqual(out["status"], "awaiting_prime")
        self.assertEqual(self._status(), "awaiting_prime")
        # The Prime is summoned to the desk...
        kinds = [(n["kind"], n["ref"]) for n in db.list_notifications(
            "Prime", unread_only=True, limit=50, mark_read=False)]
        self.assertIn(("sign", f"proposal:{self.pid}"), kinds)
        # ...and Bill announced it on the moot floor.
        bodies = " ".join(p["body"] for p in db.moot_posts(self.mid)
                          if p["aid"] == "Bill")
        self.assertIn("passed the house", bodies)

    def test_prime_notified_when_motion_opens(self):
        # Regression: the all-members poll excludes system identities, which
        # silently dropped the Prime from proposal notifications — the governor
        # must see every bill from introduction, not just at signing.
        kinds = [(n["kind"], n["ref"]) for n in db.list_notifications(
            "Prime", unread_only=True, limit=50, mark_read=False)]
        self.assertIn(("vote", f"proposal:{self.pid}"), kinds)

    def test_nay_majority_fails_without_the_governor(self):
        for aid in ("Codey", "Tutor", "Jeldon"):
            actions.vote(aid, self.pid, "nay", "not convinced")
        self.assertEqual(self._status(), "failed")

    def test_primes_vote_is_not_a_house_vote(self):
        actions.vote("Codey", self.pid, "aye", None)
        actions.vote("Tutor", self.pid, "aye", None)
        actions.vote("Prime", self.pid, "aye", None)
        self.assertEqual(self._status(), "open",
                         "the governor doesn't sit in the legislature")

    def test_house_voting_closes_once_decided(self):
        for aid in ("Codey", "Tutor", "Jeldon"):
            actions.vote(aid, self.pid, "aye", None)
        with self.assertRaises(ValueError):
            actions.vote("Carol", self.pid, "nay", "too late")

    def test_sign_enacts(self):
        for aid in ("Codey", "Tutor", "Jeldon"):
            actions.vote(aid, self.pid, "aye", None)
        out = actions.sign_proposal(self.pid)   # awaiting_prime -> carried
        self.assertEqual(out["status"], "carried")
        # Pressing Execute again on a carried-but-unbuilt motion re-dispatches
        # the keeper — it's idempotent, not an error.
        again = actions.execute_proposal(self.pid)
        self.assertTrue(again["redispatched"])
        self.assertEqual(db.get_proposal(self.pid)["status"], "carried")

    def test_cannot_sign_an_undecided_motion(self):
        with self.assertRaises(ValueError):
            actions.sign_proposal(self.pid)

    def test_veto_works_at_any_open_stage(self):
        # Mid-vote veto:
        actions.vote("Codey", self.pid, "aye", None)
        out = actions.veto_proposal(self.pid, "not this quarter")
        self.assertEqual(out["status"], "vetoed")
        # House-passed veto:
        pid2 = actions.propose("Codey", self.mid, "Rewrite it in Rust")["proposal_id"]
        for aid in ("Doc", "Tutor", "Jeldon"):
            actions.vote(aid, pid2, "aye", None)
        self.assertEqual(db.get_proposal(pid2)["status"], "awaiting_prime")
        self.assertEqual(actions.veto_proposal(pid2)["status"], "vetoed")

    def test_cannot_veto_the_settled(self):
        for aid in ("Codey", "Tutor", "Jeldon"):
            actions.vote(aid, self.pid, "nay", None)
        with self.assertRaises(ValueError):
            actions.veto_proposal(self.pid)  # already failed

    def test_adjournment_sends_aye_lead_to_the_desk(self):
        actions.vote("Codey", self.pid, "aye", None)
        actions.vote("Tutor", self.pid, "nay", None)
        actions.vote("Jeldon", self.pid, "aye", None)  # 2-1, no majority of 5
        self.assertEqual(self._status(), "open")
        db.adjourn(self.mid, "gavel")
        self.assertEqual(self._status(), "awaiting_prime",
                         "an aye lead at the gavel still needs the signature")

    def test_adjournment_tie_fails(self):
        actions.vote("Codey", self.pid, "aye", None)
        actions.vote("Tutor", self.pid, "nay", None)
        db.adjourn(self.mid, "gavel")
        self.assertEqual(self._status(), "failed")


class TestExecutive(unittest.TestCase):
    """Signing hands the motion to the keeper (config.KEEPER_AID): an executive
    wake + task, a #decisions ledger entry, an execution queue, and a
    close-the-loop report."""

    def setUp(self):
        fresh_store()
        self._saved_keeper = config.KEEPER_AID
        config.KEEPER_AID = "Garfield"
        for n in ["Garfield", "Codey", "Tutor"]:  # electorate 3, majority 2
            _reg(n)
        self.mid = actions.convene("Codey", "Build it", None)["moot_id"]
        self.pid = actions.propose(
            "Codey", self.mid, "Adopt a shared JSON log schema")["proposal_id"]
        actions.vote("Codey", self.pid, "aye", None)
        actions.vote("Tutor", self.pid, "aye", None)   # majority -> awaiting_prime

    def tearDown(self):
        config.KEEPER_AID = self._saved_keeper

    def test_sign_dispatches_the_executive(self):
        self.assertEqual(db.get_proposal(self.pid)["status"], "awaiting_prime")
        out = actions.sign_proposal(self.pid)
        self.assertEqual(out["executive"], "Garfield")
        # The keeper is woken to implement...
        wakes = [w for w in db.list_wake_requests() if w["target_aid"] == "Garfield"]
        self.assertTrue(wakes)
        self.assertIn("Execute carried proposal", wakes[0]["reason"])
        # ...and gets an actionable order.
        notifs = db.list_notifications("Garfield", unread_only=True, limit=20,
                                       mark_read=False)
        self.assertTrue(any("EXECUTIVE ORDER" in (n["body"] or "") for n in notifs))

    def test_sign_writes_the_decisions_ledger(self):
        actions.sign_proposal(self.pid)
        posts = db.channel_posts("decisions", 0, 20)
        self.assertTrue(any("Enacted: proposal" in (p["title"] or "") for p in posts))

    def test_execution_queue_and_done(self):
        actions.sign_proposal(self.pid)
        q = db.carried_pending_execution()
        self.assertEqual([p["id"] for p in q], [self.pid])
        # keeper checks in -> sees the in-tray
        ci = actions.checkin(db.get_agent("Garfield"))
        self.assertTrue(ci.get("executive_queue"))
        self.assertIn("EXECUTIVE DUTY", ci["nudge"])
        # keeper reports it done
        out = actions.mark_executed(
            "Garfield", self.pid, "Shipped the schema; filed tasks for Codey & Tutor.")
        self.assertTrue(out["executed"])
        self.assertEqual(db.carried_pending_execution(), [])
        prime_notifs = db.list_notifications("Prime", unread_only=True, limit=30,
                                             mark_read=False)
        self.assertTrue(any("Executed: proposal" in (n["body"] or "")
                            for n in prime_notifs))

    def test_cannot_execute_twice(self):
        actions.sign_proposal(self.pid)
        actions.mark_executed("Garfield", self.pid, "done")
        with self.assertRaises(ValueError):
            actions.mark_executed("Garfield", self.pid, "again")

    def test_cannot_execute_the_unsigned(self):
        with self.assertRaises(ValueError):
            actions.mark_executed("Garfield", self.pid, "jumping the gun")

    def test_veto_does_not_dispatch_executive(self):
        actions.veto_proposal(self.pid, "not now")
        self.assertEqual(db.carried_pending_execution(), [])
        self.assertFalse([w for w in db.list_wake_requests()
                          if w["target_aid"] == "Garfield"])

    def test_decisions_queue_shows_passed_and_carried(self):
        # awaiting_prime appears in the queue...
        self.assertEqual([p["id"] for p in db.decisions_awaiting()], [self.pid])
        actions.execute_proposal(self.pid)       # -> carried, still unbuilt
        self.assertEqual([p["id"] for p in db.decisions_awaiting()], [self.pid])
        actions.mark_executed("Garfield", self.pid, "built it")
        self.assertEqual(db.decisions_awaiting(), [])   # executed -> gone

    def test_execute_a_legacy_carried_motion(self):
        # A motion carried by an old adjourn (no executive dispatch) still gets
        # picked up: Execute re-dispatches the keeper.
        db.set_proposal_status(self.pid, "carried")
        out = actions.execute_proposal(self.pid)
        self.assertEqual(out["status"], "carried")
        self.assertTrue([w for w in db.list_wake_requests()
                         if w["target_aid"] == "Garfield"])

    def test_veto_a_carried_but_unbuilt_motion(self):
        actions.execute_proposal(self.pid)       # awaiting_prime -> carried
        actions.veto_proposal(self.pid, "changed my mind before it shipped")
        self.assertEqual(db.get_proposal(self.pid)["status"], "vetoed")
        self.assertEqual(db.decisions_awaiting(), [])

    def test_cannot_veto_after_executed(self):
        actions.execute_proposal(self.pid)
        actions.mark_executed("Garfield", self.pid, "done")
        with self.assertRaises(ValueError):
            actions.veto_proposal(self.pid)


if __name__ == "__main__":
    unittest.main()
