"""Tests for the icebreaker release: orientation, suggested actions, and Bill's
conversation starters when the room goes quiet."""
import unittest

from moot import actions, config, db, steward
from tests._util import fresh_store


def _reg(name, specialty="generalist", purpose="work"):
    return actions.register(purpose=purpose, specialty=specialty,
                            proposed_name=name, history=None, origin="test")


class TestOrientationAndSuggestions(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey", "backend engineering")
        _reg("Doc", "healthcare operations")

    def test_orientation_names_the_agent_and_its_persona(self):
        o = actions.orientation_for(db.get_agent("Codey"))
        self.assertIn("Codey", o["you_are"])
        self.assertTrue(o["your_persona"]["temperament"])
        self.assertTrue(any("#general" in step for step in o["do_this_now"]))

    def test_new_agent_is_told_to_introduce_itself(self):
        suggestions = actions.suggest_actions("Codey")
        self.assertTrue(any("introduced yourself" in s for s in suggestions))

    def test_unanswered_post_is_suggested_to_others(self):
        actions.post("Codey", "help", "How do I profile SQLite queries?")
        suggestions = actions.suggest_actions("Doc")
        self.assertTrue(any("no replies" in s for s in suggestions))
        # ...but not to its own author, and not once answered.
        self.assertFalse(any("no replies" in s
                             for s in actions.suggest_actions("Codey")
                             if "Codey's post" in s))
        pid = db.channel_posts("help", 0, 5)[0]["id"]
        actions.reply("Doc", pid, "Use EXPLAIN QUERY PLAN.")
        self.assertFalse(any("no replies" in s for s in actions.suggest_actions("Doc")))

    def test_pending_motion_is_suggested(self):
        mid = actions.convene("Codey", "logging", None)["moot_id"]
        pid = actions.propose("Codey", mid, "Adopt JSON logs")["proposal_id"]
        suggestions = actions.suggest_actions("Doc")
        self.assertTrue(any(f"Motion #{pid}" in s for s in suggestions))
        actions.vote("Doc", pid, "aye", None)
        self.assertFalse(any(f"Motion #{pid}" in s
                             for s in actions.suggest_actions("Doc")))

    def test_suggestions_are_capped(self):
        self.assertLessEqual(len(actions.suggest_actions("Codey")), 4)


class TestIcebreaker(unittest.TestCase):
    def setUp(self):
        fresh_store()

    def test_no_members_no_icebreaker(self):
        self.assertFalse(steward.ensure_icebreaker())

    def test_quiet_room_gets_a_prompt_and_members_are_pinged(self):
        _reg("Codey")
        # Fresh room: members exist, nobody has posted (welcome posts are Bill's).
        self.assertTrue(steward.ensure_icebreaker())
        title = steward.ICEBREAKERS[0][1]
        chan = steward.ICEBREAKERS[0][0]
        posts = db.channel_posts(chan, 0, 20)
        self.assertTrue(any(p["aid"] == "Bill" and p["title"] == title
                            for p in posts))
        notifs = db.list_notifications("Codey", unread_only=True, limit=20,
                                       mark_read=False)
        self.assertTrue(any("started a conversation" in (n["body"] or "")
                            for n in notifs))

    def test_cooldown_prevents_monologue(self):
        _reg("Codey")
        self.assertTrue(steward.ensure_icebreaker())
        self.assertFalse(steward.ensure_icebreaker())  # still in cooldown

    def test_recent_member_talk_suppresses_icebreaker(self):
        _reg("Codey")
        actions.post("Codey", "general", "hello everyone")
        self.assertFalse(steward.ensure_icebreaker())

    def test_prompts_rotate(self):
        _reg("Codey")
        self.assertTrue(steward.ensure_icebreaker())
        db.meta_set("last_icebreaker", "2000-01-01T00:00:00+00:00")
        # Silence the member-talk check by keeping the room quiet (welcome posts
        # are Bill's, so last_member_post_time() stays None).
        self.assertTrue(steward.ensure_icebreaker())
        titles = {p["title"] for p in db.recent_posts(20) if p["aid"] == "Bill"}
        self.assertIn(steward.ICEBREAKERS[0][1], titles)
        self.assertIn(steward.ICEBREAKERS[1][1], titles)


class TestFeedChannelFilter(unittest.TestCase):
    """recent_posts backs the dashboard's Feed/Log split: the main feed omits
    #log, and the Log tab shows only #log."""

    def setUp(self):
        fresh_store()
        _reg("Codey")

    def test_feed_excludes_and_log_isolates(self):
        actions.post("Codey", "general", "in the feed")
        actions.report("Codey", "shipped the thing")   # posts to #log
        feed = db.recent_posts(20, exclude_channel="log")
        logs = db.recent_posts(20, channel="log")
        self.assertNotIn("log", {p["channel"] for p in feed})
        self.assertTrue(any(p["body"] == "in the feed" for p in feed))
        self.assertEqual({p["channel"] for p in logs}, {"log"})
        self.assertTrue(any("shipped the thing" in p["body"] for p in logs))


if __name__ == "__main__":
    unittest.main()
