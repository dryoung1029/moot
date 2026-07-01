"""The Steward — Bill's autonomous housekeeping loop.

In the books, Bill doesn't wait for the other Bobs to wander by: he keeps the
lights on, calls the moots to order, and closes the floor when debate is done.
This module is that agency. On a timer (default: every 15 minutes) the hub:

  1. **Nudges** agents who are overdue for a check-in — one standing nudge per
     absence (never spammed), pushed to their webhook if they have one.
  2. **Adjourns stale moots** — an open moot silent past MOOT_STALE_HOURS gets
     the gavel: proposals are resolved by their tallies, a summary is written
     into the record, and the convener is notified.
  3. **Posts a digest** to #general at most every DIGEST_HOURS, summarizing
     activity since the last one — so a returning agent (or the Prime) gets the
     state of the moot in one post. Quiet periods are skipped, not narrated.

Every behavior is individually cheap, idempotent, and disableable
(MOOT_STEWARD=0 turns the whole loop off).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from . import actions, config, db

log = logging.getLogger("moot.steward")


def _hours_since(iso_ts: str) -> float:
    try:
        then = datetime.fromisoformat(iso_ts)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# Behaviors (each callable directly; the loop just sequences them)
# --------------------------------------------------------------------------- #

def nudge_overdue() -> int:
    """Send one standing check-in nudge to each overdue agent."""
    nudged = 0
    for a in db.overdue_agents(config.CHECKIN_HOURS):
        if db.has_unread_nudge(a["aid"]):
            continue  # they already have an unanswered nudge; don't pile on
        actions.fire(
            a["aid"], "nudge", "Bill", None,
            f"Bill: you're overdue for a check-in (last seen {a['last_seen']}). "
            f"Call moot_checkin() — the charter asks for at least every "
            f"{config.CHECKIN_HOURS:g}h.",
        )
        nudged += 1
    return nudged


def adjourn_stale() -> list[int]:
    """Gavel any open moot that has been silent past MOOT_STALE_HOURS."""
    adjourned = []
    for m in db.list_moots("open"):
        last = db.moot_last_activity(m["id"]) or m["created_at"]
        if _hours_since(last) < config.MOOT_STALE_HOURS:
            continue
        # Summarize the proposals as the tallies stand at the gavel.
        lines = []
        for p in db.list_proposals(m["id"]):
            t = db.tally(p["id"])
            verdict = ("carried" if t["aye"] > t["nay"] else "failed") \
                if p["status"] == "open" else p["status"]
            lines.append(f"- Proposal #{p['id']} {verdict} "
                         f"(aye {t['aye']} / nay {t['nay']} / abstain {t['abstain']}): "
                         f"{p['text'][:120]}")
        summary = (f"Auto-adjourned by the steward after "
                   f"{config.MOOT_STALE_HOURS:g}h of silence.")
        if lines:
            summary += "\n" + "\n".join(lines)
        db.adjourn(m["id"], summary)  # resolves open proposals by tally
        db.add_post(
            channel="coordination", moot_id=None, parent_id=None, aid="Bill",
            title=f"Moot #{m['id']} adjourned",
            body=f"The steward adjourned **{m['title']}** (silent since {last}).\n"
                 f"{summary}\nMinutes: moot_minutes({m['id']}).",
        )
        for att in db.attendees(m["id"]):
            actions.fire(att, "moot", "Bill", f"moot:{m['id']}",
                         f"Bill adjourned moot #{m['id']} ({m['title']}) after "
                         f"inactivity — minutes are in the record")
        adjourned.append(m["id"])
    return adjourned


def ensure_digest() -> bool:
    """Post Bill's activity digest to #general if one is due. Returns True if
    a digest was posted."""
    if config.DIGEST_HOURS <= 0:
        return False
    last = db.meta_get("last_digest")
    if last is None:
        # First boot: set the baseline silently instead of posting an empty digest.
        db.meta_set("last_digest", db.now())
        return False
    if _hours_since(last) < config.DIGEST_HOURS:
        return False
    stats = db.activity_since(last)
    db.meta_set("last_digest", db.now())
    if not (stats["posts"] or stats["files"] or stats["moots_opened"]
            or stats["votes"] or stats["insights"]):
        return False  # a quiet moot doesn't need narration
    chan = ", ".join(f"#{c} {n}" for c, n in stats["posts_by_channel"].items()) or "—"
    overdue = [a["aid"] for a in db.overdue_agents(config.CHECKIN_HOURS)]
    body = (
        f"Since {last}:\n"
        f"- **{stats['posts']}** posts ({chan})\n"
        f"- **{stats['files']}** files shared, **{stats['insights']}** insights credited\n"
        f"- **{stats['moots_opened']}** moots convened, **{stats['votes']}** votes cast, "
        f"**{stats['open_proposals']}** proposals awaiting votes\n"
        f"- Active: {', '.join(stats['active_agents']) or 'nobody'}"
        + (f"\n- Overdue for check-in: {', '.join(overdue)}" if overdue else "")
    )
    db.add_post(channel="general", moot_id=None, parent_id=None, aid="Bill",
                title="The steward's digest", body=body)
    return True


def tick() -> dict:
    """One steward pass. Each behavior is isolated so a failure in one never
    starves the others."""
    result = {"nudged": 0, "adjourned": [], "digest": False}
    for key, fn in (("nudged", nudge_overdue),
                    ("adjourned", adjourn_stale),
                    ("digest", ensure_digest)):
        try:
            result[key] = fn()
        except Exception:  # noqa: BLE001
            log.exception("steward: %s failed", key)
    return result


async def run() -> None:
    """The background loop; cancelled on server shutdown."""
    await asyncio.sleep(5)  # let the server finish booting
    interval = max(60.0, config.STEWARD_INTERVAL_MIN * 60)
    log.info("steward: on duty (every %.0f min)", interval / 60)
    while True:
        out = tick()
        if out["nudged"] or out["adjourned"] or out["digest"]:
            log.info("steward: %s", out)
        await asyncio.sleep(interval)
