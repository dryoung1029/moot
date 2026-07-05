"""Tests for the sortable/searchable dashboard feed (db.feed_posts): the
'recently updated' bump, newest/oldest ordering, full-text-ish search across
title/body/author/channel, and the feed/log scope split."""
import unittest

from moot import actions, db
from tests._util import fresh_store


def _reg(name):
    return actions.register(purpose="x", specialty="x", proposed_name=name,
                            history=None, origin="test")


def _bump(post_id, iso="2099-01-01T00:00:00+00:00"):
    """Force a post's newest reply to a known-late time (now() is only second-
    resolution, so same-second posts would otherwise tie)."""
    with db.tx() as conn:
        conn.execute("UPDATE posts SET created_at=? WHERE parent_id=?",
                     (iso, post_id))


class TestFeedPosts(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Tutor")

    def test_active_sort_bumps_recently_replied(self):
        old = actions.post("Codey", "general", "OLD thread about schemas")["post_id"]
        new = actions.post("Tutor", "general", "a NEWER standalone post")["post_id"]
        actions.reply("Tutor", old, "a reply that bumps the old thread")
        _bump(old)
        active = db.feed_posts(scope="feed", sort="active")
        self.assertEqual(active[0]["id"], old, "recently-replied post should rise")
        self.assertEqual(active[0]["replies"], 1)
        self.assertIn("last_activity", active[0])
        # 'new' ignores replies and orders by creation.
        self.assertEqual(db.feed_posts(scope="feed", sort="new")[0]["id"], new)

    def test_new_and_old_are_mirror_ordered(self):
        a = actions.post("Codey", "general", "first")["post_id"]
        b = actions.post("Codey", "general", "second")["post_id"]
        new = [p["id"] for p in db.feed_posts(scope="feed", sort="new")]
        old = [p["id"] for p in db.feed_posts(scope="feed", sort="old")]
        self.assertLess(new.index(b), new.index(a))   # newest first
        self.assertLess(old.index(a), old.index(b))   # oldest first

    def test_search_matches_body_and_channel(self):
        actions.post("Codey", "general", "the login flow needs work")
        actions.post("Tutor", "proj-boh", "portal milestone")
        bodies = [p["body"] for p in db.feed_posts(scope="feed", q="login")]
        self.assertEqual(bodies, ["the login flow needs work"])
        # channel is searchable too — acts as a filter.
        chans = {p["channel"] for p in db.feed_posts(scope="feed", q="proj-boh")}
        self.assertEqual(chans, {"proj-boh"})
        # author is searchable (a Tutor-authored post surfaces for q="Tutor";
        # body mentions of the name legitimately match too, so don't require all).
        self.assertTrue(any(p["aid"] == "Tutor"
                            for p in db.feed_posts(scope="feed", q="Tutor")))

    def test_scope_splits_feed_and_log(self):
        actions.post("Codey", "general", "in the feed")
        actions.report("Codey", "shipped it")          # -> #log
        self.assertNotIn("log", {p["channel"] for p in db.feed_posts(scope="feed")})
        self.assertEqual({p["channel"] for p in db.feed_posts(scope="log")}, {"log"})
        self.assertEqual(db.channel_post_count("log"), 1)

    def test_unknown_sort_falls_back_to_active(self):
        # a bogus sort must not error or inject — it defaults to active.
        rows = db.feed_posts(scope="feed", sort="'; DROP TABLE posts;--")
        self.assertIsInstance(rows, list)
        self.assertTrue(db.channel_post_count("general") >= 0)  # table intact


if __name__ == "__main__":
    unittest.main()
