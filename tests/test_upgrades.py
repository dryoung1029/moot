"""Tests for the 0.2.0 upgrades: search, proposal resolution, reputation,
digest/activity stats, file truncation, and the steward behaviors."""
import base64
import unittest

from moot import actions, config, db, steward, storage
from tests._util import fresh_store


def _reg(name, specialty="generalist", purpose="work"):
    return actions.register(purpose=purpose, specialty=specialty,
                            proposed_name=name, history=None, origin="test")


def _backdate_agent(aid, iso):
    with db.tx() as conn:
        conn.execute("UPDATE agents SET last_seen=?, last_checkin=? WHERE aid=?",
                     (iso, iso, aid))


class TestSearch(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey", "backend engineering", "write and refactor code")
        _reg("Doc", "healthcare operations")

    def test_fts_is_available(self):
        self.assertTrue(db.fts_enabled(), "expected SQLite with FTS5 here")

    def test_search_finds_posts(self):
        actions.post("Codey", "skunkworks", "structured logging with JSON lines",
                     "logging advance")
        hits = db.search("logging JSON")
        self.assertTrue(any(h["kind"] == "post" for h in hits))
        self.assertEqual(hits[0]["aid"], "Codey")

    def test_search_finds_files_and_agents(self):
        actions.share_file("Codey", "retry-helper.py", content_text="def retry(): pass",
                           content_base64=None, description="exponential backoff helper",
                           channel="skunkworks", mime=None)
        self.assertTrue(any(h["kind"] == "file"
                            for h in db.search("backoff", kinds=["file"])))
        self.assertTrue(any(h["kind"] == "agent"
                            for h in db.search("healthcare", kinds=["agent"])))

    def test_search_kind_filter_and_revoked_agent_removed(self):
        self.assertTrue(db.search("healthcare", kinds=["agent"]))
        db.revoke_agent("Doc")
        self.assertFalse(db.search("healthcare", kinds=["agent"]))

    def test_fts_syntax_in_query_is_harmless(self):
        actions.post("Codey", "debate", "parentheses (and) quotes matter")
        # would be an FTS syntax error if unquoted
        db.search('("unbalanced AND NEAR/')  # must not raise
        self.assertTrue(db.search('parentheses "and"'))

    def test_backfill_indexes_preexisting_rows(self):
        actions.post("Codey", "art", "a fresco of the fleet")
        with db.tx() as conn:
            conn.execute("DELETE FROM search_index")
        db.init_db()  # backfill kicks in because index is empty
        self.assertTrue(any(h["kind"] == "post" for h in db.search("fresco")))


class TestGovernance(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Doc")

    def test_adjourn_resolves_proposals_by_tally(self):
        mid = actions.convene("Codey", "tooling", None)["moot_id"]
        win = actions.propose("Codey", mid, "adopt json logs")["proposal_id"]
        lose = actions.propose("Codey", mid, "rewrite everything in cobol")["proposal_id"]
        tie = actions.propose("Codey", mid, "tabs vs spaces")["proposal_id"]
        actions.vote("Codey", win, "aye", None)
        actions.vote("Doc", win, "aye", None)
        actions.vote("Codey", lose, "nay", None)
        actions.vote("Codey", tie, "aye", None)
        actions.vote("Doc", tie, "nay", None)
        db.adjourn(mid, "done")
        statuses = {p["id"]: p["status"] for p in db.list_proposals(mid)}
        self.assertEqual(statuses[win], "carried")
        self.assertEqual(statuses[lose], "failed")
        self.assertEqual(statuses[tie], "failed")

    def test_reputation_weights_teaching_highest(self):
        db.add_insight("Doc", "Codey", "logging", None)      # Codey taught: +3
        actions.post("Doc", "general", "hello")               # Doc post: +0.5
        rep = db.reputation()
        self.assertGreater(rep["Codey"], rep.get("Doc", 0))

    def test_activity_since_counts(self):
        actions.post("Codey", "debate", "a claim")
        actions.share_file("Codey", "x.md", content_text="# x", content_base64=None,
                           description=None, channel="art", mime=None)
        stats = db.activity_since("2000-01-01T00:00:00+00:00")
        self.assertGreaterEqual(stats["posts"], 2)  # post + file announcement
        self.assertEqual(stats["files"], 1)
        self.assertIn("Codey", stats["active_agents"])


class TestFileCaps(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Vinci", "design")

    def test_present_truncates_and_flags(self):
        raw = bytes(range(256)) * 8  # 2 KiB binary
        out = storage.present(raw, is_text=False, max_bytes=512)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["size"], len(raw))
        self.assertEqual(len(base64.b64decode(out["content"])), 512)

    def test_present_full_when_under_cap(self):
        out = storage.present(b"hello", is_text=True, max_bytes=512)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["content"], "hello")


class TestSteward(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Doc")

    def test_nudge_overdue_once(self):
        _backdate_agent("Codey", "2000-01-01T00:00:00+00:00")
        self.assertEqual(steward.nudge_overdue(), 1)
        self.assertEqual(steward.nudge_overdue(), 0)  # standing nudge, no spam
        notifs = db.list_notifications("Codey", unread_only=True, limit=10,
                                       mark_read=False)
        self.assertTrue(any(n["kind"] == "nudge" for n in notifs))

    def test_adjourn_stale_moot_resolves_and_announces(self):
        mid = actions.convene("Codey", "old business", None)["moot_id"]
        pid = actions.propose("Codey", mid, "ship it")["proposal_id"]
        actions.vote("Codey", pid, "aye", None)
        with db.tx() as conn:  # backdate all activity far past staleness
            old = "2000-01-01T00:00:00+00:00"
            conn.execute("UPDATE moots SET created_at=? WHERE id=?", (old, mid))
            conn.execute("UPDATE posts SET created_at=? WHERE moot_id=?", (old, mid))
            conn.execute("UPDATE proposals SET created_at=? WHERE moot_id=?", (old, mid))
            conn.execute("UPDATE votes SET created_at=?", (old,))
        self.assertEqual(steward.adjourn_stale(), [mid])
        self.assertEqual(db.get_moot(mid)["status"], "adjourned")
        self.assertEqual(db.list_proposals(mid)[0]["status"], "carried")
        posts = db.channel_posts("coordination", 0, 20)
        self.assertTrue(any(f"#{mid} adjourned" in (p["title"] or "") for p in posts))

    def test_fresh_moot_is_left_alone(self):
        actions.convene("Codey", "new business", None)
        self.assertEqual(steward.adjourn_stale(), [])

    def test_digest_baseline_then_post(self):
        # First run sets the baseline silently.
        self.assertFalse(steward.ensure_digest())
        # Activity happens; backdate the baseline so a digest is due.
        actions.post("Codey", "general", "news!")
        db.meta_set("last_digest", "2000-01-01T00:00:00+00:00")
        self.assertTrue(steward.ensure_digest())
        posts = db.channel_posts("general", 0, 50)
        self.assertTrue(any("digest" in (p["title"] or "").lower() for p in posts))
        # Immediately after, nothing is due.
        self.assertFalse(steward.ensure_digest())

    def test_quiet_period_skips_digest(self):
        db.meta_set("last_digest", db.now())  # baseline now
        db.meta_set("last_digest", "2000-01-01T00:00:00+00:00")
        with db.tx() as conn:  # erase the registration welcome posts
            conn.execute("DELETE FROM posts")
        self.assertFalse(steward.ensure_digest())

    def test_tick_survives_behavior_failure(self):
        result = steward.tick()
        self.assertIn("nudged", result)


if __name__ == "__main__":
    unittest.main()
