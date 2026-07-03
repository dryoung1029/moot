"""Tests for the governor model: a simple majority of the electorate closes
the house's business — aye-majorities go to the Prime's desk (nothing carries
without the signature), nay-majorities fail outright, and the Prime may veto
any motion at any stage."""
import unittest

from moot import actions, db
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
        out = actions.sign_proposal(self.pid)
        self.assertEqual(out["status"], "carried")
        with self.assertRaises(ValueError):
            actions.sign_proposal(self.pid)  # can't sign twice

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


if __name__ == "__main__":
    unittest.main()
