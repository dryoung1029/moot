import unittest

from moot import actions, db
from tests._util import fresh_store


class TestMootCore(unittest.TestCase):
    def setUp(self):
        fresh_store()

    def _register(self, purpose, specialty, name):
        return actions.register(
            purpose=purpose, specialty=specialty, proposed_name=name,
            history=None, origin="test")

    def test_registration_assigns_identity_and_token(self):
        r = self._register("write code", "backend", "Codey")
        self.assertEqual(r["agent"]["aid"], "Codey")
        self.assertTrue(len(r["token"]) >= 20)
        self.assertIn(r["agent"]["quirk"], __import__("moot.identity",
                                                      fromlist=["QUIRKS"]).QUIRKS)
        # token authenticates back to the same agent
        self.assertEqual(db.get_agent_by_token(r["token"])["aid"], "Codey")

    def test_reserved_names_cannot_be_claimed(self):
        r = self._register("misc", None, "Bill")
        self.assertNotEqual(r["agent"]["aid"], "Bill")

    def test_join_code_enforced_when_configured(self):
        from moot import config
        config.JOIN_CODE = "sesame"
        with self.assertRaises(ValueError):
            self._register("x", None, "Nope")
        ok = actions.register(purpose="x", specialty=None, proposed_name="Yep",
                              history=None, origin="test", join_code="sesame")
        self.assertEqual(ok["agent"]["aid"], "Yep")
        config.JOIN_CODE = None

    def test_mention_creates_notification(self):
        self._register("code", "backend", "Codey")
        self._register("health", "healthcare", "Doc")
        out = actions.post("Codey", "debate", "hey @Doc look at this", "t")
        self.assertEqual(out["mentioned"], ["Doc"])
        notifs = db.list_notifications("Doc", unread_only=True, limit=10, mark_read=False)
        self.assertTrue(any(n["kind"] == "mention" for n in notifs))

    def test_dm_notifies_recipient(self):
        self._register("code", "backend", "Codey")
        self._register("health", "healthcare", "Doc")
        actions.dm("Codey", "Doc", "private note")
        inbox = db.inbox("Doc", unread_only=True, limit=10, mark_read=False)
        self.assertEqual(inbox[0]["body"], "private note")
        self.assertTrue(db.notif_unread_count("Doc") >= 1)

    def test_convene_vote_tally(self):
        self._register("code", "backend", "Codey")
        self._register("health", "healthcare", "Doc")
        mid = actions.convene("Codey", "tooling", "pick logs")["moot_id"]
        pid = actions.propose("Codey", mid, "adopt json logs")["proposal_id"]
        actions.vote("Codey", pid, "aye", None)
        v = actions.vote("Doc", pid, "yes", "sounds good")  # synonym -> aye
        self.assertEqual(v["tally"]["aye"], 2)
        # re-voting changes, not adds
        actions.vote("Doc", pid, "nay", "changed mind")
        self.assertEqual(db.tally(pid), {"aye": 1, "nay": 1, "abstain": 0})

    def test_convene_invites_all_agents(self):
        self._register("code", "backend", "Codey")
        self._register("health", "healthcare", "Doc")
        out = actions.convene("Codey", "sync", None)
        self.assertEqual(out["invited"], 1)  # Doc, not Codey, not system
        notifs = db.list_notifications("Doc", unread_only=True, limit=10, mark_read=False)
        self.assertTrue(any(n["kind"] == "moot" for n in notifs))

    def test_file_share_and_read_roundtrip(self):
        self._register("code", "backend", "Codey")
        out = actions.share_file("Codey", "note.md", content_text="# hi\nbody",
                                 content_base64=None, description="d", channel="art",
                                 mime=None)
        meta = db.get_file(out["file_id"])
        self.assertTrue(meta["is_text"])
        from moot import storage
        data = storage.read(meta["path"])
        self.assertEqual(data.decode(), "# hi\nbody")
        # sharing announces a post in the channel
        posts = db.channel_posts("art", 0, 10)
        self.assertTrue(any("note.md" in p["body"] for p in posts))

    def test_binary_file_roundtrip_base64(self):
        import base64
        self._register("art", "design", "Vinci")
        raw = bytes(range(256))
        out = actions.share_file("Vinci", "blob.bin", content_text=None,
                                 content_base64=base64.b64encode(raw).decode(),
                                 description=None, channel="art", mime="application/octet-stream")
        meta = db.get_file(out["file_id"])
        self.assertFalse(meta["is_text"])
        from moot import storage
        self.assertEqual(storage.read(meta["path"]), raw)

    def test_checkin_digest_only_shows_new(self):
        self._register("code", "backend", "Codey")
        self._register("health", "healthcare", "Doc")
        actions.post("Codey", "debate", "@Doc ping", None)
        actions.convene("Codey", "meet", None)
        prev = db.mark_checkin("Doc")
        self.assertIsNone(prev)
        notifs = db.list_notifications("Doc", unread_only=True, limit=50, mark_read=True)
        kinds = {n["kind"] for n in notifs}
        self.assertIn("mention", kinds)
        self.assertIn("moot", kinds)
        # second drain is empty
        self.assertEqual(db.list_notifications("Doc", True, 50, mark_read=True), [])

    def test_insight_ledger(self):
        self._register("code", "backend", "Codey")
        self._register("health", "healthcare", "Doc")
        db.add_insight("Doc", "Codey", "logging", "learned json logs")
        got = db.list_insights("Codey")
        self.assertEqual(got[0]["teacher"], "Codey")
        self.assertEqual(got[0]["learner"], "Doc")

    def test_overdue_detection(self):
        self._register("code", "backend", "Codey")
        # Nobody is overdue against a 6h window; a future cutoff (negative window)
        # flags everyone — this exercises the query without second-boundary flake.
        self.assertEqual(db.overdue_agents(6), [])
        od = {a["aid"] for a in db.overdue_agents(-1)}
        self.assertIn("Codey", od)


if __name__ == "__main__":
    unittest.main()
