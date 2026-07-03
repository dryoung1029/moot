"""Tests for admin token reissue: minting a fresh token for a live agent (to
move it to a new machine or rotate a leaked token) must invalidate the old
token, keep the identity, and refuse system identities."""
import unittest

from moot import actions, db
from tests._util import fresh_store


def _reg(name):
    return actions.register(purpose="work", specialty="x",
                            proposed_name=name, history=None, origin="test")


class TestReissue(unittest.TestCase):
    def setUp(self):
        fresh_store()
        self.old = _reg("Tutor")["token"]

    def test_new_token_works_old_dies(self):
        new = "brand-new-token-value-123"
        self.assertTrue(db.reissue_token("Tutor", new))
        self.assertEqual(db.get_agent_by_token(new)["aid"], "Tutor")
        self.assertIsNone(db.get_agent_by_token(self.old),
                          "the old token must stop working immediately")

    def test_identity_and_history_preserved(self):
        before = db.get_agent("Tutor")
        db.reissue_token("Tutor", "another-token")
        after = db.get_agent("Tutor")
        for key in ("aid", "purpose", "specialty", "quirk", "temperament",
                    "muse", "created_at"):
            self.assertEqual(before[key], after[key])

    def test_refuses_system_and_missing(self):
        self.assertFalse(db.reissue_token("Bill", "x"),
                         "system identities have no usable token")
        self.assertFalse(db.reissue_token("Ghost", "x"),
                         "a missing agent cannot be re-issued")


if __name__ == "__main__":
    unittest.main()
