"""Tests for the catch-up report (the Prime's on-demand "what did I miss"
digest): the new db.py aggregation functions, the proposals.resolved_at stamp
(including the adjourn() bypass gotcha), and the /api/catchup route's
composition (needs-you scoping, missing-since validation)."""
import asyncio
import unittest

from moot import actions, config, db, web
from tests._util import fresh_store

FLEET = ["Doc", "Codey", "Tutor", "Jeldon", "Carol"]  # electorate 5, majority 3


def _reg(name):
    return actions.register(purpose="work", specialty="x",
                            proposed_name=name, history=None, origin="test")


def _backdate(table, id_col, id_val, col, iso):
    with db.tx() as conn:
        conn.execute(f"UPDATE {table} SET {col} = ? WHERE {id_col} = ?",
                     (iso, id_val))


OLD = "2000-01-01T00:00:00+00:00"
CUTOFF = "2020-06-15T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"  # "since" a point nothing has happened yet


class TestNeedsYouAggregation(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")

    def test_unread_dms_since_cutoff_only(self):
        actions.dm("Codey", "Prime", "old news")
        old_id = db.recent_dms(1)[0]["id"]
        _backdate("dms", "id", old_id, "created_at", OLD)
        actions.dm("Codey", "Prime", "fresh news")
        rows = db.dms_unread_since("Prime", CUTOFF)
        self.assertEqual([r["body"] for r in rows], ["fresh news"])

    def test_read_dms_excluded_even_if_recent(self):
        actions.dm("Codey", "Prime", "hello")
        db.mark_dms_read("Prime", "Codey")
        self.assertEqual(db.dms_unread_since("Prime", CUTOFF), [])

    def test_flood_incident_surfaces_for_prime(self):
        db.notify("Prime", "flood", source_aid="Codey", ref=None,
                  body="Codey hit the cap")
        rows = db.notifications_since("Prime", "flood", CUTOFF)
        self.assertEqual(len(rows), 1)
        self.assertEqual(db.notifications_since("Prime", "flood", FUTURE), [])


class TestWakeOverdueFlag(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Jeldon")

    def test_overdue_flag_uses_escalate_threshold(self):
        wid = actions.request_wake("Codey", "Jeldon", "need input")["wake_id"]
        old = f"2000-01-01T00:00:00+00:00"
        _backdate("wake_requests", "id", wid, "created_at", old)
        resp = asyncio.run(web._catchup(_FakeRequest(CUTOFF)))
        body = _json(resp)
        wakes = body["needs_you"]["wake_list"]
        self.assertEqual(len(wakes), 1)
        self.assertTrue(wakes[0]["overdue"])

    def test_fresh_wake_not_overdue(self):
        actions.request_wake("Codey", "Jeldon", "need input")
        resp = asyncio.run(web._catchup(_FakeRequest(CUTOFF)))
        wakes = _json(resp)["needs_you"]["wake_list"]
        self.assertFalse(wakes[0]["overdue"])


class TestDecisionsAggregation(unittest.TestCase):
    def setUp(self):
        fresh_store()
        for n in FLEET:
            _reg(n)
        self.mid = actions.convene("Doc", "Lawmaking", None)["moot_id"]

    def _propose_and_pass(self, text="Adopt JSON logging"):
        pid = actions.propose("Doc", self.mid, text)["proposal_id"]
        for aid in ("Codey", "Tutor", "Jeldon"):
            actions.vote(aid, pid, "aye", None)
        return pid  # now awaiting_prime

    def test_carried_stamps_resolved_at_and_appears_since_cutoff(self):
        pid = self._propose_and_pass()
        actions.execute_proposal(pid)
        rows = db.proposals_resolved_since(CUTOFF)
        self.assertEqual([r["id"] for r in rows], [pid])
        self.assertIsNotNone(db.get_proposal(pid)["resolved_at"])

    def test_resolved_before_cutoff_excluded(self):
        pid = self._propose_and_pass()
        actions.execute_proposal(pid)
        self.assertEqual(db.proposals_resolved_since(FUTURE), [])

    def test_open_proposal_never_appears(self):
        pid = actions.propose("Doc", self.mid, "Undecided motion")["proposal_id"]
        self.assertEqual(db.proposals_resolved_since(OLD), [])
        self.assertIsNone(db.get_proposal(pid)["resolved_at"])

    def test_vetoed_stamps_resolved_at(self):
        pid = self._propose_and_pass()
        actions.veto_proposal(pid, "not this quarter")
        rows = db.proposals_resolved_since(CUTOFF)
        self.assertEqual([r["id"] for r in rows], [pid])

    def test_failed_via_majority_stamps_resolved_at(self):
        pid = actions.propose("Doc", self.mid, "Unpopular motion")["proposal_id"]
        for aid in ("Codey", "Tutor", "Jeldon"):
            actions.vote(aid, pid, "nay", None)
        self.assertEqual(db.get_proposal(pid)["status"], "failed")
        rows = db.proposals_resolved_since(CUTOFF)
        self.assertEqual([r["id"] for r in rows], [pid])

    def test_failed_via_adjournment_stamps_resolved_at(self):
        # The adjourn() gotcha: it bypasses set_proposal_status with its own
        # inline UPDATE, so this path must independently stamp resolved_at.
        pid = actions.propose("Doc", self.mid, "Tied motion")["proposal_id"]
        actions.vote("Codey", pid, "aye", None)
        actions.vote("Tutor", pid, "nay", None)
        self.assertEqual(db.get_proposal(pid)["status"], "open")
        db.adjourn(self.mid, "gavel")
        self.assertEqual(db.get_proposal(pid)["status"], "failed")
        rows = db.proposals_resolved_since(CUTOFF)
        self.assertEqual([r["id"] for r in rows], [pid])

    def test_awaiting_prime_via_adjournment_not_stamped(self):
        # Non-terminal verdict — must NOT get a resolved_at (it isn't decided).
        pid = actions.propose("Doc", self.mid, "Aye-lead motion")["proposal_id"]
        actions.vote("Codey", pid, "aye", None)
        actions.vote("Tutor", pid, "aye", None)
        db.adjourn(self.mid, "gavel")
        self.assertEqual(db.get_proposal(pid)["status"], "awaiting_prime")
        self.assertIsNone(db.get_proposal(pid)["resolved_at"])
        self.assertEqual(db.proposals_resolved_since(CUTOFF), [])


class TestNeedsYouRouteScope(unittest.TestCase):
    def setUp(self):
        fresh_store()
        for n in FLEET:
            _reg(n)
        self.mid = actions.convene("Doc", "Lawmaking", None)["moot_id"]

    def test_awaiting_prime_included_carried_pending_execution_excluded(self):
        awaiting = actions.propose("Doc", self.mid, "Needs a signature")["proposal_id"]
        for aid in ("Codey", "Tutor", "Jeldon"):
            actions.vote(aid, awaiting, "aye", None)
        carried = actions.propose("Doc", self.mid, "Already carried")["proposal_id"]
        for aid in ("Codey", "Tutor", "Jeldon"):
            actions.vote(aid, carried, "aye", None)
        actions.execute_proposal(carried)  # carried, no longer needs a signature

        resp = asyncio.run(web._catchup(_FakeRequest(CUTOFF)))
        body = _json(resp)
        ids = [p["id"] for p in body["needs_you"]["awaiting_signature"]]
        self.assertEqual(ids, [awaiting])

    def test_missing_since_is_rejected(self):
        resp = asyncio.run(web._catchup(_FakeRequest(None)))
        self.assertEqual(resp.status_code, 400)


class TestNotableThreads(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")
        _reg("Jeldon")

    def test_ranks_by_reply_plus_reaction_count(self):
        quiet = actions.post("Codey", "general", "quiet post")["post_id"]
        loud = actions.post("Codey", "general", "loud post")["post_id"]
        actions.reply("Jeldon", loud, "a reply")
        actions.react("Jeldon", loud, "👍")
        rows = db.notable_threads_since(OLD)
        self.assertEqual(rows[0]["id"], loud)
        self.assertIn(quiet, [r["id"] for r in rows])

    def test_thread_surfaces_via_recent_reply_even_if_root_is_old(self):
        pid = actions.post("Codey", "general", "old root")["post_id"]
        _backdate("posts", "id", pid, "created_at", OLD)
        actions.reply("Jeldon", pid, "fresh reply")
        rows = db.notable_threads_since(CUTOFF)
        self.assertIn(pid, [r["id"] for r in rows])

    def test_log_channel_excluded(self):
        actions.report("Codey", "did some work")  # posts to #log
        rows = db.notable_threads_since(OLD)
        self.assertEqual([r for r in rows if r["channel"] == "log"], [])


class TestHousekeeping(unittest.TestCase):
    def setUp(self):
        fresh_store()
        _reg("Codey")

    def test_new_agent_since_cutoff(self):
        aids = [r["aid"] for r in db.agents_registered_since(OLD)]
        self.assertEqual(aids, ["Codey"])
        self.assertEqual(db.agents_registered_since(FUTURE), [])

    def test_file_since_cutoff(self):
        actions.share_file("Codey", "notes.txt", content_text="hi",
                           content_base64=None, description=None,
                           channel=None, mime="text/plain")
        rows = db.files_since(OLD)
        self.assertEqual(len(rows), 1)
        self.assertEqual(db.files_since(FUTURE), [])

    def test_task_completed_since_cutoff(self):
        tid = actions.task_add("Codey", "Ship the thing")["task_id"]
        rows_before = db.tasks_changed_since(OLD)
        self.assertEqual(rows_before, [])  # still open, not done/blocked
        actions.task_update("Codey", tid, status="done")
        rows = db.tasks_changed_since(OLD)
        self.assertEqual([r["id"] for r in rows], [tid])
        self.assertEqual(db.tasks_changed_since(FUTURE), [])

    def test_task_blocked_since_cutoff(self):
        tid = actions.task_add("Codey", "Ship the thing")["task_id"]
        actions.task_update("Codey", tid, status="blocked", note="waiting on review")
        self.assertEqual([r["id"] for r in db.tasks_changed_since(OLD)], [tid])

    def test_moot_adjourned_since_cutoff(self):
        mid = actions.convene("Codey", "Standup", None)["moot_id"]
        self.assertEqual(db.moots_adjourned_since(OLD), [])
        db.adjourn(mid, "done")
        rows = db.moots_adjourned_since(OLD)
        self.assertEqual([r["id"] for r in rows], [mid])
        self.assertEqual(db.moots_adjourned_since(FUTURE), [])


class _FakeRequest:
    """Just enough of starlette.Request for _catchup(): it only reads
    request.query_params.get("since")."""
    def __init__(self, since):
        self.query_params = {"since": since} if since is not None else {}


def _json(response):
    import json
    return json.loads(response.body)


if __name__ == "__main__":
    unittest.main()
