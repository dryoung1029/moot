"""Tests for wake signals v1 (file 27 — the frozen Tier 0 contract): typed
per-recipient fan-out, DB-enforced dedupe (exact retry-collapse + burst-fold),
and recipient-cap accounting that only 'summon' ever touches."""
import unittest

from moot import actions, config, db
from tests._util import fresh_store


def _reg(name):
    return actions.register(purpose="work", specialty="x",
                            proposed_name=name, history=None, origin="test")


def _signal_count(recipient=None):
    with db.tx() as conn:
        if recipient is None:
            return conn.execute("SELECT COUNT(*) c FROM wake_signals").fetchone()["c"]
        return conn.execute(
            "SELECT COUNT(*) c FROM wake_signals WHERE recipient = ?",
            (recipient,)).fetchone()["c"]


class TestWakeSignalDedupe(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Jeldon")
        self._settle = config.WAKE_SIGNAL_SETTLE_SECONDS
        # Wide settle window so same-test-run calls land in one coalesce
        # bucket deterministically (no flake at the millisecond level).
        config.WAKE_SIGNAL_SETTLE_SECONDS = 3600

    def tearDown(self):
        config.WAKE_SIGNAL_SETTLE_SECONDS = self._settle

    def test_double_fire_collapses_via_idempotency_key(self):
        out1 = actions.file_wake_signal("Codey", "Jeldon", "summon",
                                        idempotency_key="retry-1")
        out2 = actions.file_wake_signal("Codey", "Jeldon", "summon",
                                        idempotency_key="retry-1")
        self.assertTrue(out1["created"])
        self.assertFalse(out2["created"])
        self.assertEqual(out1["signal_id"], out2["signal_id"])
        self.assertEqual(_signal_count("Jeldon"), 1)

    def test_bucket_boundary_retry_still_collapses(self):
        # Same logical action, retried after landing in a different coalesce
        # bucket: the burst-fold constraint alone would miss this (file 27
        # §5) — only the exact idempotency-key constraint catches it.
        out1 = actions.file_wake_signal("Codey", "Jeldon", "summon", post_id=1,
                                        idempotency_key="action-42")
        with db.tx() as conn:
            conn.execute(
                "UPDATE wake_signals SET coalesce_bucket = coalesce_bucket - 1000 "
                "WHERE id = ?", (out1["signal_id"],))
        out2 = actions.file_wake_signal("Codey", "Jeldon", "summon", post_id=1,
                                        idempotency_key="action-42")
        self.assertFalse(out2["created"])
        self.assertEqual(_signal_count("Jeldon"), 1)

    def test_distinct_burst_folds_via_post_kind_bucket(self):
        # Two DISTINCT idempotency keys (so the exact-collapse constraint
        # never fires), same recipient/post/kind/bucket: the burst-fold
        # constraint alone must catch this.
        out1 = actions.file_wake_signal("Codey", "Jeldon", "mention", post_id=7,
                                        idempotency_key="k1")
        out2 = actions.file_wake_signal("Codey", "Jeldon", "mention", post_id=7,
                                        idempotency_key="k2")
        self.assertTrue(out1["created"])
        self.assertFalse(out2["created"])
        self.assertEqual(_signal_count("Jeldon"), 1)

    def test_separate_events_an_hour_apart_both_land(self):
        # Real, distinct events on the same thread must not be swallowed —
        # only fold when they're actually a burst.
        out1 = actions.file_wake_signal("Codey", "Jeldon", "mention", post_id=8,
                                        idempotency_key="ev-1")
        with db.tx() as conn:
            conn.execute(
                "UPDATE wake_signals SET coalesce_bucket = coalesce_bucket - 1800 "
                "WHERE id = ?", (out1["signal_id"],))
        out2 = actions.file_wake_signal("Codey", "Jeldon", "mention", post_id=8,
                                        idempotency_key="ev-2")
        self.assertTrue(out2["created"])
        self.assertEqual(_signal_count("Jeldon"), 2)


class TestWakeSignalFanout(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        for n in ("Doc", "Tutor", "Carol"):
            _reg(n)

    def test_group_post_fans_out_one_row_per_recipient(self):
        out = actions.fan_out_wake_signals(
            "Codey", ["Doc", "Tutor", "Carol"], "mention", post_id=3)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(o["created"] for o in out))
        with db.tx() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM wake_signals WHERE post_id = 3").fetchone()["c"]
        self.assertEqual(n, 3)

    def test_fanout_excludes_sender_and_dedupes_recipient_list(self):
        out = actions.fan_out_wake_signals(
            "Codey", ["Doc", "Codey", "Doc"], "mention", post_id=9)
        self.assertEqual([o["recipient"] for o in out], ["Doc"])

    def test_signal_to_unknown_agent_rejected(self):
        with self.assertRaises(ValueError):
            actions.file_wake_signal("Codey", "Nobody", "mention")

    def test_bad_kind_rejected(self):
        with self.assertRaises(ValueError):
            actions.file_wake_signal("Codey", "Doc", "urgent")


class TestWakeSignalCap(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Jeldon")
        self._old_cap = config.WAKE_SUMMON_DAILY_CAP
        config.WAKE_SUMMON_DAILY_CAP = 2

    def tearDown(self):
        config.WAKE_SUMMON_DAILY_CAP = self._old_cap

    def test_mention_never_debits_the_cap_or_files_a_wake(self):
        for i in range(5):
            actions.file_wake_signal("Codey", "Jeldon", "mention", post_id=i)
        self.assertEqual(db.wake_signal_count("Jeldon", "summon", hours=24), 0)
        self.assertEqual(db.list_wake_requests(), [])

    def test_summon_debits_and_stops_filing_the_wake_list_over_cap(self):
        outs = [actions.file_wake_signal("Codey", "Jeldon", "summon", post_id=i)
                for i in range(4)]
        self.assertEqual([o["wake_filed"] for o in outs], [True, True, False, False])
        # Every summon still queues a notification, even once the cap is spent
        # — the cap protects wake-list filing (session spawn), not delivery.
        self.assertEqual(db.wake_signal_count("Jeldon", "summon", hours=24), 4)
        self.assertEqual(len(db.list_wake_requests()), 1)

    def test_cap_of_zero_disables_the_limit(self):
        config.WAKE_SUMMON_DAILY_CAP = 0
        outs = [actions.file_wake_signal("Codey", "Jeldon", "summon", post_id=i)
                for i in range(5)]
        self.assertTrue(all(o["wake_filed"] for o in outs))


if __name__ == "__main__":
    unittest.main()
