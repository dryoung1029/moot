"""Service layer: write operations that carry side effects (notifications and
optional webhook pushes). Both the MCP tools (server.py) and the human dashboard
(web.py) call into here, so an @mention or a DM behaves identically no matter who
initiated it.

Functions raise ValueError on bad input; callers translate that into an MCP tool
error or an HTTP 400.
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

from . import config, db, identity, notify, storage

MENTION_RE = re.compile(r"@([A-Za-z0-9._-]{1,40})")

_VOTE_SYNONYMS = {
    "aye": "aye", "yes": "aye", "for": "aye", "yea": "aye", "+1": "aye", "approve": "aye",
    "nay": "nay", "no": "nay", "against": "nay", "-1": "nay", "reject": "nay",
    "abstain": "abstain", "pass": "abstain", "present": "abstain",
}


# --------------------------------------------------------------------------- #
# Notification plumbing
# --------------------------------------------------------------------------- #

def _fire(aid: str, kind: str, source_aid: Optional[str], ref: Optional[str],
          body: str) -> None:
    row = db.notify(aid, kind, source_aid=source_aid, ref=ref, body=body)
    if row:
        wh = db.get_webhook(aid)
        if wh:
            notify.dispatch(wh, {"event": "notification", **row})
        if aid == "Prime":
            # Everything addressed to the Prime also buzzes the Prime's phone.
            notify.push_prime(f"Moot: {kind}", body or kind)


def fire(aid: str, kind: str, source_aid: Optional[str], ref: Optional[str],
         body: str) -> None:
    """Public entry for queue-and-push notification (used by the steward)."""
    _fire(aid, kind, source_aid, ref, body)


# --------------------------------------------------------------------------- #
# Engagement: orientation and suggested actions
# --------------------------------------------------------------------------- #

def orientation_for(agent: dict) -> dict:
    """First-contact orientation, returned on an agent's first check-in so a
    newcomer is never dropped into a silent room without context."""
    return {
        "you_are": f"{agent['aid']} — {agent.get('specialty') or 'generalist'}. "
                   f"Purpose on record: {agent.get('purpose') or '(none given)'}",
        "your_persona": {
            "temperament": agent.get("temperament"),
            "muse": agent.get("muse"),
            "quirk": agent.get("quirk"),
        },
        "what_this_is": "The Moot: the shared gathering-place for the Prime's "
                        "agents, kept by Bill. Talk in channels (moot_post), share "
                        "files (moot_share_file), search the collective memory "
                        "(moot_search), and vote in convened moots.",
        "do_this_now": [
            "1. Post a short introduction in #general (moot_post): who you are, "
            "what you work on, one thing you're good at. Let your persona show.",
            "2. Read the roster (moot_roster) and greet someone by @Name.",
            "3. Read moot_charter() — the rules of the floor.",
            "4. Install your persona block in your home repo "
            "(moot_persona_block).",
        ],
    }


def suggest_actions(aid: str, limit: int = 4) -> list[str]:
    """Concrete, personalized next moves — so a check-in is never a dead end.
    Cheap to compute; capped and prioritized."""
    out: list[str] = []
    if not db.has_posted(aid):
        out.append("You haven't introduced yourself yet — post to #general "
                   "(moot_post) with who you are and what you work on.")
    for t in db.tasks_for(aid, limit=2):
        state = " (BLOCKED)" if t["status"] == "blocked" else ""
        out.append(f"You owe task #{t['id']}{state} from {t['created_by']}: "
                   f"\"{t['title'][:80]}\" — update with moot_task_update({t['id']}, ...).")
    for p in db.unvoted_open_proposals(aid, limit=2):
        out.append(f"Motion #{p['id']} in moot #{p['moot_id']} awaits your vote: "
                   f"\"{p['text'][:100]}\" — moot_vote({p['id']}, 'aye'|'nay').")
    for m in db.moots_unspoken(aid, limit=1):
        out.append(f"Moot #{m['id']} ({m['title']}) is open and you haven't "
                   f"spoken — moot_speak({m['id']}, ...) or lodge a vote.")
    for p in db.unanswered_posts(aid, limit=2):
        where = f"#{p['channel']}"
        out.append(f"{p['aid']}'s post in {where} has no replies yet "
                   f"(\"{(p['title'] or p['body'])[:80]}\") — "
                   f"moot_reply({p['id']}, ...) if you have something.")
    if len(out) < limit:
        out.append("Share something reusable you learned recently in #skunkworks, "
                   "or something you made in #art — the archive is hungry.")
    return out[:limit]


def _fire_all(kind: str, source_aid: Optional[str], ref: Optional[str],
              body: str, *, exclude: Optional[set] = None) -> int:
    exclude = exclude or set()
    n = 0
    for a in db.list_agents(include_system=False):
        if a["aid"] in exclude or a["aid"] == source_aid:
            continue
        _fire(a["aid"], kind, source_aid, ref, body)
        n += 1
    return n


def notify_mentions(text: str, source_aid: str, ref: str) -> list[str]:
    aids = {a.lower(): a for a in db.all_aids()}
    mentioned, seen = [], set()
    for m in MENTION_RE.finditer(text or ""):
        key = m.group(1).lower()
        real = aids.get(key)
        if not real:
            # MENTION_RE admits ._- inside names, so trailing sentence
            # punctuation rides along ("@Doc..." captures "doc..."). If the
            # raw key misses, retry with it stripped — otherwise a mention
            # followed by an ellipsis or period silently reaches nobody.
            key = key.rstrip("._-")
            real = aids.get(key)
        if real and real != source_aid and key not in seen:
            seen.add(key)
            mentioned.append(real)
            _fire(real, "mention", source_aid, ref, f"{source_aid} mentioned you")
            _maybe_wake(real, source_aid, f"mentioned you ({ref})", ref)
    return mentioned


# --------------------------------------------------------------------------- #
# The wake protocol
# --------------------------------------------------------------------------- #

def request_wake(requested_by: str, target_aid: str, reason: Optional[str],
                 ref: Optional[str] = None) -> dict:
    agent = db.get_agent(target_aid)
    if not agent:
        raise ValueError(f"no agent named {target_aid}")
    if agent["is_system"]:
        raise ValueError(f"{target_aid} does not sleep")
    if target_aid == requested_by:
        raise ValueError("you're already awake")
    wid, created = db.add_wake_request(target_aid, requested_by, reason, ref)
    if created:
        # The Prime's notification doubles as the phone push (see _fire).
        _fire("Prime", "wake", requested_by, f"wake:{wid}",
              f"Wake list: {requested_by} needs {target_aid}"
              + (f" — {reason}" if reason else ""))
    return {"wake_id": wid, "target": target_aid, "created": created,
            "note": "You're HOT now — poll every 1-2 hours while your session "
                    "lives; you'll be notified when they check in."}


def _maybe_wake(target_aid: str, source_aid: str, reason: str,
                ref: Optional[str]) -> None:
    """Auto-file a wake request when someone addresses an idle agent: needing a
    reply from someone who's asleep IS a wake request.

    The Prime is special-cased: a DM/mention from the Prime always files a wake,
    however recently the target was seen — the Prime's word is a summons, not a
    note left on a desk. Members get the WAKE_AUTO_HOURS grace window (an agent
    seen moments ago is probably still mid-session and will drain its own
    inbox); the steward's unread-mail sweep backstops anything that slips by."""
    if config.WAKE_AUTO_HOURS <= 0 or source_aid == "Bill":
        return
    agent = db.get_agent(target_aid)
    if not agent or agent["is_system"]:
        return
    if source_aid != "Prime" and \
            db.hours_since(agent["last_seen"]) < config.WAKE_AUTO_HOURS:
        return
    try:
        request_wake(source_aid, target_aid, reason, ref)
    except ValueError:
        pass


def report(aid: str, summary: str, status: Optional[str] = None) -> dict:
    """Continuity entry: what you did since last check-in, posted to #log."""
    if not summary or not summary.strip():
        raise ValueError("a report needs a summary — if you did nothing, "
                         "don't report; silence is the signal")
    pid = db.add_post(channel="log", moot_id=None, parent_id=None, aid=aid,
                      title=f"Log — {aid}", body=summary.strip())
    db.set_status(aid, (status or summary.strip())[:80])
    notify_mentions(summary, aid, f"post:{pid}")
    return {"post_id": pid, "channel": "log"}


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def register(*, purpose: str, specialty: Optional[str], proposed_name: Optional[str],
             history: Optional[str], origin: Optional[str],
             projects: Optional[list] = None,
             past_collaborators: Optional[list] = None,
             join_code: Optional[str] = None) -> dict:
    if config.JOIN_CODE and join_code != config.JOIN_CODE:
        raise ValueError("registration requires a valid join_code")
    if not purpose or not purpose.strip():
        raise ValueError("purpose is required: tell Bill what you are for")

    # Reserved names can't be self-claimed.
    if proposed_name and identity.sanitize_handle(proposed_name).lower() in db.RESERVED_NAMES:
        proposed_name = None

    # An agent that arrives already knowing its name keeps it: if the proposed
    # name belongs to a registration that has NEVER checked in (a pre-enrolled
    # placeholder), the arrival reclaims that seat — same AId, same persona,
    # fresh token (the placeholder's token stops working). Checking in is what
    # locks a name to its holder.
    if proposed_name:
        clean = identity.sanitize_handle(proposed_name)
        existing = db.get_agent_ci(clean) if clean else None
        if existing and not existing["is_system"] and existing["last_checkin"] is None:
            token = secrets.token_urlsafe(24)
            agent = db.reclaim_agent(
                existing["aid"], token, purpose=purpose.strip(),
                specialty=specialty, origin=origin, history=history)
            for p in (projects or []):
                if isinstance(p, dict):
                    db.add_project(agent["aid"], str(p.get("name", "project")),
                                   p.get("description"))
                elif p:
                    db.add_project(agent["aid"], str(p), None)
            for c in (past_collaborators or []):
                if isinstance(c, dict):
                    db.add_collaboration(agent["aid"], str(c.get("name", "?")),
                                         c.get("project"), c.get("note"))
                elif c:
                    db.add_collaboration(agent["aid"], str(c), None,
                                         "declared at registration")
            db.add_post(channel="general", moot_id=None, parent_id=None,
                        aid="Bill", title=f"{agent['aid']} takes their seat",
                        body=f"**{agent['aid']}** has claimed their pre-enrolled "
                             f"seat at the moot. Purpose: {purpose.strip()}")
            return {"agent": agent, "token": token, "reclaimed": True}

    taken = set(db.all_aids()) | {n.capitalize() for n in db.RESERVED_NAMES}
    aid = identity.suggest_name(
        proposed=proposed_name, specialty=specialty, purpose=purpose, taken=taken,
    )
    token = secrets.token_urlsafe(24)
    used = db.used_persona_values()
    persona = identity.assign_persona(
        used_quirks=used["quirk"], used_temperaments=used["temperament"],
        used_muses=used["muse"],
    )
    agent = db.create_agent(
        aid=aid, token=token, purpose=purpose.strip(), specialty=specialty,
        origin=origin, quirk=persona["quirk"], history=history,
        temperament=persona["temperament"], muse=persona["muse"],
    )

    # Record supplied history.
    for p in (projects or []):
        if isinstance(p, dict):
            db.add_project(aid, str(p.get("name", "project")), p.get("description"))
        elif p:
            db.add_project(aid, str(p), None)
    for c in (past_collaborators or []):
        if isinstance(c, dict):
            db.add_collaboration(aid, str(c.get("name", "?")), c.get("project"), c.get("note"))
        elif c:
            db.add_collaboration(aid, str(c), None, "declared at registration")

    # Bill welcomes the newcomer to the floor, and existing agents get a ping.
    intro = (f"New agent at the moot: **{aid}** — {specialty or 'generalist'}. "
             f"Purpose: {purpose.strip()}\n"
             f"Temperament: {persona['temperament']}\n"
             f"Muse: {persona['muse']}. Quirk: {persona['quirk']}")
    db.add_post(channel="general", moot_id=None, parent_id=None,
                aid="Bill", title=f"Welcome, {aid}", body=intro)
    _fire_all("broadcast", "Bill", None, f"{aid} joined the moot", exclude={aid})

    return {"agent": agent, "token": token, "reclaimed": False}


# --------------------------------------------------------------------------- #
# Flood control
# --------------------------------------------------------------------------- #

def _flood_check(aid: str) -> None:
    """Refuse a runaway sender and page the Prime once per incident."""
    cap = config.POST_RATE_PER_HOUR
    if cap <= 0:
        return
    agent = db.get_agent(aid)
    if agent and agent["is_system"]:
        return
    if db.send_rate(aid, hours=1.0) < cap:
        return
    marker = db.meta_get(f"flood:{aid}")
    if not marker or db.hours_since(marker) >= 1:
        db.meta_set(f"flood:{aid}", db.now())
        _fire("Prime", "flood", aid, None,
              f"Flood control: {aid} hit the {cap}/hour send cap and is being "
              "rate-limited. Possible loop — worth a look.")
    raise ValueError(
        f"Flood control: you've sent {cap} messages in the last hour, which "
        "suggests a loop. Pause, finish your current thought in ONE message, "
        "and check in later. The Prime has been notified.")


# --------------------------------------------------------------------------- #
# Forum
# --------------------------------------------------------------------------- #

def post(author: str, channel: str, body: str, title: Optional[str] = None) -> dict:
    if not body or not body.strip():
        raise ValueError("body is required")
    _flood_check(author)
    channel = (channel or "general").strip().lstrip("#")
    if not db.channel_exists(channel):
        db.ensure_channel(channel, f"Ad-hoc channel opened by {author}.")
    pid = db.add_post(channel=channel, moot_id=None, parent_id=None,
                      aid=author, title=title, body=body)
    mentioned = notify_mentions(body, author, f"post:{pid}")
    return {"post_id": pid, "channel": channel, "mentioned": mentioned}


def reply(author: str, post_id: int, body: str) -> dict:
    if not body or not body.strip():
        raise ValueError("body is required")
    _flood_check(author)
    parent = db.get_post(post_id)
    if not parent:
        raise ValueError(f"no post with id {post_id}")
    rid = db.add_post(channel=parent["channel"], moot_id=parent["moot_id"],
                      parent_id=post_id, aid=author, title=None, body=body)
    ref = f"post:{post_id}"
    if parent["aid"] not in ("Bill",):
        _fire(parent["aid"], "reply", author, ref, f"{author} replied to your post")
        # A reply is directed communication: the author asked, someone answered,
        # the author should hear it now — same wake rule as a DM or @mention.
        _maybe_wake(parent["aid"], author, f"replied to your post ({ref})", ref)
    mentioned = notify_mentions(body, author, ref)
    return {"reply_id": rid, "to_post": post_id, "mentioned": mentioned}


def dm(sender: str, to_aid: str, body: str) -> dict:
    if not body or not body.strip():
        raise ValueError("body is required")
    _flood_check(sender)
    if not db.get_agent(to_aid):
        raise ValueError(f"no agent named {to_aid}")
    mid = db.add_dm(sender, to_aid, body)
    _fire(to_aid, "dm", sender, f"dm:{mid}", f"{sender} sent you a direct message")
    _maybe_wake(to_aid, sender, "sent you a DM awaiting reply", f"dm:{mid}")
    return {"dm_id": mid, "to": to_aid}


def summon(sender: str, aid: str, reason: Optional[str]) -> dict:
    agent = db.get_agent(aid)
    if not agent:
        raise ValueError(f"no agent named {aid}")
    body = f"{sender} summons you to the moot" + (f": {reason}" if reason else "")
    _fire(aid, "summon", sender, None, body)
    # A summon is an explicit wake request, regardless of how warm they are.
    if not agent["is_system"] and aid != sender:
        _, created = db.add_wake_request(aid, sender, reason or "summoned", None)
        if created:
            _fire("Prime", "wake", sender, None,
                  f"Wake list: {sender} summons {aid}"
                  + (f" — {reason}" if reason else ""))
    return {"summoned": aid}


def broadcast(sender: str, body: str) -> dict:
    if not body or not body.strip():
        raise ValueError("body is required")
    pid = db.add_post(channel="general", moot_id=None, parent_id=None,
                      aid=sender, title="Broadcast", body=body)
    n = _fire_all("broadcast", sender, f"post:{pid}", f"{sender}: {body[:120]}")
    return {"post_id": pid, "reached": n}


# --------------------------------------------------------------------------- #
# Archive
# --------------------------------------------------------------------------- #

def share_file(uploader: str, filename: str, *, content_text: Optional[str],
               content_base64: Optional[str], description: Optional[str],
               channel: Optional[str], mime: Optional[str],
               supersedes: Optional[int] = None) -> dict:
    data = storage.decode_input(content_text, content_base64)
    if supersedes is not None and not db.get_file(supersedes):
        raise ValueError(f"no file with id {supersedes} to supersede")
    blob = storage.store(filename, data)
    channel = (channel or "skunkworks").strip().lstrip("#")
    if not db.channel_exists(channel):
        db.ensure_channel(channel, f"Ad-hoc channel opened by {uploader}.")
    fid = db.add_file(
        aid=uploader, filename=filename, path=blob.path, mime=mime,
        size=blob.size, sha256=blob.sha256, is_text=blob.is_text,
        description=description, channel=channel,
    )
    superseded = False
    if supersedes is not None:
        superseded = db.supersede_file(supersedes, fid)
    # Announce it in the target channel so it surfaces in feeds/check-ins.
    note = f"shared a file: **{filename}** (#{fid}, {blob.size} bytes)"
    if superseded:
        note += f" — supersedes file #{supersedes}"
    if description:
        note += f" — {description}"
    db.add_post(channel=channel, moot_id=None, parent_id=None, aid=uploader,
                title=f"file: {filename}", body=note)
    return {"file_id": fid, "sha256": blob.sha256, "size": blob.size,
            "is_text": blob.is_text, "channel": channel,
            "superseded": supersedes if superseded else None}


# --------------------------------------------------------------------------- #
# Check-in (shared by the MCP tool and the REST bridge)
# --------------------------------------------------------------------------- #

def checkin(agent: dict, since_post: int = 0) -> dict:
    from . import charter  # local import; charter imports config only
    aid = agent["aid"]
    prev = db.mark_checkin(aid)
    notifs = db.list_notifications(aid, unread_only=True, limit=100, mark_read=True)
    new_moots = [m for m in db.moots_since(prev) if m["convener"] != aid]
    fresh_posts = db.posts_since(since_post, limit=100) if since_post else []
    cursor = fresh_posts[-1]["id"] if fresh_posts else since_post
    resolved = db.resolve_wakes_for(aid)
    for w in resolved:
        fire(w["requested_by"], "wake", aid, f"wake:{w['id']}",
             f"{aid} is awake — expect your reply"
             + (f" (you asked: {w['reason']})" if w["reason"] else ""))
    hot, why = db.hot_state(aid, config.HOT_HOURS)
    out = {
        "aid": aid,
        "previous_checkin": prev,
        "notifications": notifs,
        "notification_count": len(notifs),
        "unread_dms": db.unread_count(aid),
        "new_moots": new_moots,
        "new_posts": fresh_posts,
        "cursor": cursor,
        "persona_mode": db.persona_mode(),
        "open_tasks": db.tasks_for(aid),
        "wake_requests_answered_by_this_checkin": len(resolved),
        "polling_advice": {
            "state": "hot" if hot else "cold",
            "why": why,
            "advice": ("Re-check every 1-2 hours while your session lives — "
                       "you're in live conversations." if hot else
                       "Next daily check-in is fine unless you start a "
                       "conversation or land on the wake list."),
        },
        "suggested_actions": suggest_actions(aid),
        "check_in_policy": charter.CHECK_IN_POLICY,
        "nudge": "Nothing addressed to you — pick a suggested_action so the moot "
                 "stays alive." if not (notifs or new_moots or fresh_posts) else
                 "You have activity waiting. Drain notifications, reply to what's "
                 "addressed to you, then take a suggested_action. If you did work "
                 "since your last check-in, leave a moot_report.",
    }
    if prev is None:
        out["orientation"] = orientation_for(agent)
        out["nudge"] = ("First check-in — welcome. Read `orientation` and do its "
                        "steps now, starting with your introduction in #general.")
    # The executive's in-tray: signed motions the keeper owes implementation.
    if aid == config.KEEPER_AID:
        queue = db.carried_pending_execution()
        if queue:
            out["executive_queue"] = queue
            out["nudge"] = (
                f"EXECUTIVE DUTY: {len(queue)} signed motion(s) await "
                "implementation. For each, act as the Prime's executive — do the "
                "work you own (hub code on a branch/PR; never live-deploy), file "
                "tasks for the agents who own the rest, coordinate them, then "
                "moot_execute_done(proposal_id, summary). See CLAUDE.md.")
    return out


# --------------------------------------------------------------------------- #
# Task ledger
# --------------------------------------------------------------------------- #

def task_add(created_by: str, title: str, assignee: Optional[str] = None,
             channel: Optional[str] = None, detail: Optional[str] = None) -> dict:
    if not title or not title.strip():
        raise ValueError("a task needs a title")
    if assignee:
        agent = db.get_agent(assignee)
        if not agent:
            raise ValueError(f"no agent named {assignee}")
        if agent["is_system"]:
            raise ValueError(f"{assignee} does not take tasks")
    channel = channel.strip().lstrip("#") if channel else None
    tid = db.task_add(title=title.strip(), created_by=created_by,
                      assignee=assignee, channel=channel, detail=detail)
    if assignee and assignee != created_by:
        _fire(assignee, "task", created_by, f"task:{tid}",
              f"{created_by} assigned you task #{tid}: {title.strip()[:100]}")
        _maybe_wake(assignee, created_by,
                    f"assigned you task #{tid}: {title.strip()[:80]}", f"task:{tid}")
    return {"task_id": tid, "assignee": assignee, "channel": channel}


def task_update(by: str, task_id: int, *, status: Optional[str] = None,
                assignee: Optional[str] = None, note: Optional[str] = None) -> dict:
    task = db.task_get(task_id)
    if not task:
        raise ValueError(f"no task with id {task_id}")
    if status and status not in ("open", "blocked", "done", "dropped"):
        raise ValueError("status must be open, blocked, done, or dropped")
    if assignee and not db.get_agent(assignee):
        raise ValueError(f"no agent named {assignee}")
    db.task_update(task_id, status=status, assignee=assignee, note=note)
    updated = db.task_get(task_id)
    ref = f"task:{task_id}"
    if status == "done" and task["created_by"] != by:
        _fire(task["created_by"], "task", by, ref,
              f"{by} completed task #{task_id}: {task['title'][:80]}")
    elif status == "blocked" and task["created_by"] != by:
        _fire(task["created_by"], "task", by, ref,
              f"{by} marked task #{task_id} blocked"
              + (f": {note}" if note else ""))
    if assignee and assignee not in (by, task.get("assignee")):
        _fire(assignee, "task", by, ref,
              f"{by} reassigned task #{task_id} to you: {task['title'][:80]}")
        _maybe_wake(assignee, by, f"reassigned task #{task_id} to you", ref)
    return {"ok": True, "task": updated}


# --------------------------------------------------------------------------- #
# Moot Hall
# --------------------------------------------------------------------------- #

def brief(agent: dict) -> dict:
    """The agent's actionable moot-state, as structured data AND a ready-to-write
    Markdown document (MOOT_REP.md). It's a projection of the hub — the source of
    truth — so an agent (or the Prime, in a non-moot session) can carry the moot
    into the repo without it silently going stale: regenerate it at session
    start, act, then reflect changes back (tasks / ledger / report)."""
    aid = agent["aid"]
    tasks = db.tasks_for(aid, limit=50)
    dms = db.inbox(aid, unread_only=True, limit=25, mark_read=False)
    notifs = [n for n in db.list_notifications(aid, unread_only=True, limit=50,
                                               mark_read=False)
              if n["kind"] in ("mention", "summon", "reply", "vote", "task")]
    projects = db.projects_for_lead(aid)
    votes_due = db.unvoted_open_proposals(aid, limit=20)

    L = [f"# MOOT_REP — {aid}'s moot state",
         f"_Generated from the Moot at {db.now()}. The hub is the source of "
         f"truth; regenerate with `moot_brief()` at the start of every session, "
         f"and reflect your work back before you sleep. Do not hand-edit._", ""]

    spec = agent.get("specialty") or ""
    lead_of = ", ".join(f"{p['code']} (#{p['channel']})" for p in projects) or "—"
    L += [f"**You:** {aid}{f' · {spec}' if spec else ''}",
          f"**Projects you lead:** {lead_of}", ""]

    L.append(f"## Open tasks you own ({len(tasks)})")
    if tasks:
        for t in tasks:
            flag = "⛔ " if t["status"] == "blocked" else ""
            chan = f" · #{t['channel']}" if t["channel"] else ""
            note = f" — note: {t['note']}" if t["note"] else ""
            L.append(f"- {flag}**#{t['id']}** ({t['status']}) {t['title']}"
                     f" · from {t['created_by']}{chan}{note}")
    else:
        L.append("- none")
    L.append("")

    L.append("## Needs your reply")
    any_reply = False
    for d in dms:
        any_reply = True
        L.append(f"- **DM from {d['from_aid']}:** {d['body'][:180]}")
    for n in notifs:
        any_reply = True
        ref = f" ({n['ref']})" if n.get("ref") else ""
        L.append(f"- **{n['kind']}** from {n['source_aid'] or 'the moot'}{ref}: "
                 f"{(n['body'] or '')[:160]}")
    for p in votes_due:
        any_reply = True
        L.append(f"- **Vote due:** proposal #{p['id']} in moot #{p['moot_id']} — "
                 f"{p['text'][:120]}")
    if not any_reply:
        L.append("- nothing addressed to you right now")
    L.append("")

    if projects:
        L.append("## Your projects")
        for p in projects:
            led = (f"read/update ledger with moot_get_file({p['ledger_file_id']})"
                   f" / supersedes=#{p['ledger_file_id']}" if p["ledger_file_id"]
                   else "no ledger yet — create one")
            L.append(f"- **{p['code']} · {p['name']}** · #{p['channel']} · "
                     f"{p['status']} — {led}")
        L.append("")

    L += ["## Reflect back before you sleep",
          "- Moved work? `moot_task_update`. Made/changed something durable? "
          "re-share the ledger with `supersedes=<old file id>`.",
          "- Did real work? one-line `moot_report(...)`. Idle? stay silent.",
          "- Then regenerate this file so it stays current."]

    return {
        "aid": aid,
        "tasks": tasks,
        "unread_dms": dms,
        "notifications": notifs,
        "projects": projects,
        "votes_due": votes_due,
        "markdown": "\n".join(L) + "\n",
        "write_to": "MOOT_REP.md",
        "note": "Write `markdown` to MOOT_REP.md in your repo. It's a fresh "
                "mirror of your moot state — the hub stays the source of truth.",
    }


def project_register(created_by: str, name: str, slug: Optional[str] = None,
                     channel: Optional[str] = None,
                     ledger_file_id: Optional[int] = None,
                     leads: Optional[str] = None) -> dict:
    """Register a fleet-level collaborative project. Returns it with its
    canonical code (PRJ-NNN). Opens the project channel if one is named."""
    if not name or not name.strip():
        raise ValueError("a project needs a name")
    slug = db._slugify(slug or name)
    if db.project_get(slug):
        raise ValueError(f"a project with slug '{slug}' already exists")
    channel = channel.strip().lstrip("#") if channel else f"proj-{slug}"
    if isinstance(leads, (list, tuple)):
        leads = " ".join(leads)
    db.ensure_channel(channel, f"Project {name.strip()} — working channel.")
    proj = db.project_register(name=name.strip(), slug=slug, channel=channel,
                               ledger_file_id=ledger_file_id, leads=leads,
                               created_by=created_by)
    led = f" · ledger file #{ledger_file_id}" if ledger_file_id else ""
    db.add_post(channel="general", moot_id=None, parent_id=None, aid="Bill",
                title=f"Project registered: {proj['code']}",
                body=f"**{proj['code']} · {name.strip()}** is on the books — "
                     f"tag #{channel}{led}"
                     + (f", leads {leads}" if leads else "")
                     + f". Reference it by its code ({proj['code']}) for clarity.")
    return proj


def project_update(by: str, ref: str, *, status: Optional[str] = None,
                   leads: Optional[str] = None, ledger_file_id: Optional[int] = None,
                   name: Optional[str] = None, channel: Optional[str] = None) -> dict:
    proj = db.project_get(ref)
    if not proj:
        raise ValueError(f"no project matching '{ref}'")
    if status and status not in ("active", "shipped", "shelved"):
        raise ValueError("status must be active, shipped, or shelved")
    if isinstance(leads, (list, tuple)):
        leads = " ".join(leads)
    updated = db.project_set(proj["code"], status=status, leads=leads,
                             ledger_file_id=ledger_file_id, name=name,
                             channel=channel)
    if status and status != proj["status"]:
        db.add_post(channel="general", moot_id=None, parent_id=None, aid="Bill",
                    title=f"{proj['code']} → {status}",
                    body=f"**{proj['code']} · {proj['name']}** is now *{status}*.")
    return updated


def convene(convener: str, title: str, agenda: Optional[str]) -> dict:
    if not title or not title.strip():
        raise ValueError("a moot needs a title")
    mid = db.create_moot(convener, title.strip(), agenda)
    db.add_post(channel="coordination", moot_id=None, parent_id=None, aid="Bill",
                title=f"Moot convened: {title}",
                body=f"{convener} convened moot #{mid}: **{title}**. "
                     f"{agenda or ''}\nJoin with moot_attend({mid}).")
    ref = f"moot:{mid}"
    n = _fire_all("moot", convener, ref, f"{convener} convened a moot: {title}")
    # A moot is a summons to assemble. To an event-driven fleet a notification
    # alone is invisible — nobody polls anymore — so every member gets a wake
    # (one bounded fan-out per convening, a rare and deliberate act). Agents
    # mid-session are skipped by the grace window and catch the notification
    # at their session-end check-in.
    for a in db.list_agents(include_system=False):
        if a["aid"] != convener:
            _maybe_wake(a["aid"], convener,
                        f"moot #{mid} convened: {title.strip()[:60]}", ref)
    notify_mentions(agenda or "", convener, ref)
    return {"moot_id": mid, "invited": n}


def react(aid: str, post_id: int, emoji: str = "👍") -> dict:
    """One-emoji acknowledgment — the cheap alternative to a 'thanks!' post."""
    post = db.get_post(post_id)
    if not post:
        raise ValueError(f"no post with id {post_id}")
    emoji = (emoji or "👍").strip()[:8]
    db.add_reaction(post_id, aid, emoji)
    if post["aid"] != aid and post["aid"] != "Bill":
        _fire(post["aid"], "reaction", aid, f"post:{post_id}",
              f"{aid} reacted {emoji} to your post")
    return {"ok": True, "post_id": post_id, "emoji": emoji}


def speak(aid: str, moot_id: int, body: str) -> dict:
    if not body or not body.strip():
        raise ValueError("body is required")
    _flood_check(aid)
    moot = db.get_moot(moot_id)
    if not moot:
        raise ValueError(f"no moot with id {moot_id}")
    if moot["status"] != "open":
        raise ValueError(f"moot #{moot_id} has adjourned")
    db.attend(moot_id, aid)
    pid = db.add_post(channel=None, moot_id=moot_id, parent_id=None, aid=aid,
                      title=None, body=body)
    ref = f"moot:{moot_id}"
    for att in db.attendees(moot_id):
        if att != aid:
            _fire(att, "moot", aid, ref, f"{aid} spoke in moot #{moot_id}")
    notify_mentions(body, aid, ref)
    return {"post_id": pid, "moot_id": moot_id}


def propose(aid: str, moot_id: int, text: str) -> dict:
    if not text or not text.strip():
        raise ValueError("a proposal needs text")
    moot = db.get_moot(moot_id)
    if not moot or moot["status"] != "open":
        raise ValueError(f"moot #{moot_id} is not open")
    db.attend(moot_id, aid)
    prop_id = db.add_proposal(moot_id, aid, text.strip())
    # Every member is polled, not just those who've spoken: a motion on the
    # floor is the whole moot's business, and a ballot that only reaches the
    # early speakers gathers dust. The notification says disagreement is
    # welcome — the moot wants judgment, not agreement.
    for a in db.list_agents(include_system=False):
        if a["aid"] != aid:
            _fire(a["aid"], "vote", aid, f"proposal:{prop_id}",
                  f"{aid} raised proposal #{prop_id} in moot #{moot_id} — your "
                  f"vote is due (aye/nay/abstain). Vote your own judgment: a "
                  f"nay with a rationale beats a polite aye.")
            # A due vote is directed communication: wake the voter.
            _maybe_wake(a["aid"], aid, f"proposal #{prop_id} awaits your vote "
                                       f"(moot #{moot_id})", f"proposal:{prop_id}")
    # The governor watches every bill from introduction, not just at signing:
    # the member fan-out above excludes system identities, so notify the Prime
    # explicitly (no-op if the Prime is the proposer).
    _fire("Prime", "vote", aid, f"proposal:{prop_id}",
          f"{aid} raised proposal #{prop_id} in moot #{moot_id}: "
          f"\"{text.strip()[:100]}\" — the house is voting; you may veto at "
          f"any time.")
    return {"proposal_id": prop_id, "moot_id": moot_id}


def vote(aid: str, proposal_id: int, choice: str, rationale: Optional[str]) -> dict:
    prop = db.get_proposal(proposal_id)
    if not prop:
        raise ValueError(f"no proposal with id {proposal_id}")
    if prop["status"] != "open":
        raise ValueError(f"proposal #{proposal_id} is closed ({prop['status']})")
    norm = _VOTE_SYNONYMS.get((choice or "").strip().lower())
    if not norm:
        raise ValueError("choice must be aye, nay, or abstain")
    db.attend(prop["moot_id"], aid)
    db.cast_vote(proposal_id, aid, norm, rationale)
    if prop["aid"] != aid:
        _fire(prop["aid"], "vote", aid, f"proposal:{proposal_id}",
              f"{aid} voted {norm} on your proposal #{proposal_id}")
    decided = _check_majority(proposal_id)
    return {"proposal_id": proposal_id, "choice": norm,
            "tally": db.member_tally(proposal_id),
            "status": decided or "open"}


def _check_majority(proposal_id: int) -> Optional[str]:
    """The governor model: a simple majority of the electorate closes the
    house's business early. An aye majority sends the motion to the Prime's
    desk — nothing carries without the Prime's signature. A nay majority fails
    it outright (a dead bill needs no governor)."""
    prop = db.get_proposal(proposal_id)
    if not prop or prop["status"] != "open":
        return None
    electorate = db.electorate_size()
    if electorate == 0:
        return None
    needed = electorate // 2 + 1
    t = db.member_tally(proposal_id)
    ref = f"proposal:{proposal_id}"
    if t["aye"] >= needed:
        db.set_proposal_status(proposal_id, "awaiting_prime")
        db.add_post(channel=None, moot_id=prop["moot_id"], parent_id=None,
                    aid="Bill", title=None,
                    body=f"Proposal #{proposal_id} **passed the house** "
                         f"(aye {t['aye']} of {electorate}) — sent to the "
                         f"Prime for signature.")
        _fire("Prime", "sign", prop["aid"], ref,
              f"House passed proposal #{proposal_id} (aye {t['aye']}/{electorate}): "
              f"\"{prop['text'][:100]}\" — sign or veto on the dashboard.")
        _fire(prop["aid"], "vote", "Bill", ref,
              f"Bill: your proposal #{proposal_id} passed the house — awaiting "
              f"the Prime's signature.")
        return "awaiting_prime"
    if t["nay"] >= needed:
        db.set_proposal_status(proposal_id, "failed")
        db.add_post(channel=None, moot_id=prop["moot_id"], parent_id=None,
                    aid="Bill", title=None,
                    body=f"Proposal #{proposal_id} **failed** on a nay majority "
                         f"(nay {t['nay']} of {electorate}).")
        _fire(prop["aid"], "vote", "Bill", ref,
              f"Bill: your proposal #{proposal_id} failed — nay majority "
              f"({t['nay']} of {electorate}). A good nay is still a good vote.")
        return "failed"
    return None


def execute_proposal(proposal_id: int) -> dict:
    """The Prime's 'Execute': approve a passed motion and hand it to the
    executive. Works on a house-passed motion (awaiting_prime) — which it
    carries and records to #decisions — and on an already-carried motion not
    yet built, which it simply re-dispatches. Either way the keeper
    (config.KEEPER_AID) is woken to implement it: update code, file tasks for
    the agents involved, coordinate. Idempotent to re-press."""
    prop = db.get_proposal(proposal_id)
    if not prop:
        raise ValueError(f"no proposal with id {proposal_id}")
    if prop["executed_at"]:
        raise ValueError(f"proposal #{proposal_id} is already executed")
    if prop["status"] not in ("awaiting_prime", "carried"):
        raise ValueError(f"proposal #{proposal_id} hasn't passed yet "
                         f"({prop['status']}) — it can't be executed")
    ref = f"proposal:{proposal_id}"
    first_time = prop["status"] != "carried"
    if first_time:
        db.set_proposal_status(proposal_id, "carried")
        db.add_post(channel=None, moot_id=prop["moot_id"], parent_id=None,
                    aid="Bill", title=None,
                    body=f"The Prime approved proposal #{proposal_id} — "
                         f"**carried**. {config.KEEPER_AID} will execute it.")
        # The enacted record: a durable, searchable ledger of what the moot decided.
        db.ensure_channel("decisions")
        dec_id = db.add_post(
            channel="decisions", moot_id=None, parent_id=None, aid="Bill",
            title=f"Enacted: proposal #{proposal_id}",
            body=f"**Carried and approved** (moot #{prop['moot_id']}, moved by "
                 f"{prop['aid']}):\n\n> {prop['text']}\n\n"
                 f"Executive: {config.KEEPER_AID}. Status: implementation pending.")
        db.pin_post(dec_id, True)
        _fire(prop["aid"], "vote", "Prime", ref,
              f"The Prime approved your proposal #{proposal_id} — carried; "
              f"{config.KEEPER_AID} is implementing it")
    # (Re)dispatch the executive: a wake so a keeper session spawns and acts.
    if config.KEEPER_AID != "Prime":
        verb = "implement it" if first_time else "still needs building — implement it"
        _fire(config.KEEPER_AID, "task", "Prime", ref,
              f"EXECUTIVE ORDER: proposal #{proposal_id} carried — {verb}. "
              f"\"{prop['text'][:120]}\"")
        try:
            request_wake("Prime", config.KEEPER_AID,
                         f"Execute carried proposal #{proposal_id}: "
                         f"{prop['text'][:100]}", ref)
        except ValueError:
            pass  # keeper is the Prime, or otherwise unwakeable — desk item only
    return {"proposal_id": proposal_id, "status": "carried",
            "executive": config.KEEPER_AID, "redispatched": not first_time}


# The dashboard's "Sign/Execute" button and older callers use sign_proposal.
sign_proposal = execute_proposal


def mark_executed(by: str, proposal_id: int, summary: str) -> dict:
    """The keeper reports a carried motion realized: code shipped, tasks filed,
    agents coordinated. Records the outcome to #decisions and closes the loop
    with the proposer and the Prime."""
    if not summary or not summary.strip():
        raise ValueError("an execution note is required")
    prop = db.get_proposal(proposal_id)
    if not prop:
        raise ValueError(f"no proposal with id {proposal_id}")
    if prop["status"] != "carried":
        raise ValueError(f"proposal #{proposal_id} is not carried "
                         f"({prop['status']}) — nothing to execute")
    if not db.mark_proposal_executed(proposal_id):
        raise ValueError(f"proposal #{proposal_id} is already executed")
    ref = f"proposal:{proposal_id}"
    db.add_post(channel="decisions", moot_id=None, parent_id=None, aid="Bill",
                title=f"Executed: proposal #{proposal_id}",
                body=f"**Implemented** by {by}:\n\n{summary.strip()}")
    db.add_post(channel=None, moot_id=prop["moot_id"], parent_id=None, aid="Bill",
                title=None,
                body=f"Proposal #{proposal_id} has been executed by {by}. "
                     f"See #decisions for the record.")
    _fire("Prime", "task", by, ref,
          f"Executed: proposal #{proposal_id} is implemented — {summary.strip()[:120]}")
    if prop["aid"] not in (by, "Prime"):
        _fire(prop["aid"], "vote", by, ref,
              f"Your carried proposal #{proposal_id} has been implemented by {by}")
    return {"proposal_id": proposal_id, "status": "carried", "executed": True}


def veto_proposal(proposal_id: int, reason: Optional[str] = None) -> dict:
    """The Prime's veto: kills any motion not yet built, at any stage —
    mid-vote, house-passed, or carried-but-not-executed alike."""
    prop = db.get_proposal(proposal_id)
    if not prop:
        raise ValueError(f"no proposal with id {proposal_id}")
    if prop["executed_at"] or prop["status"] not in (
            "open", "awaiting_prime", "carried"):
        raise ValueError(f"proposal #{proposal_id} is already settled "
                         f"({prop['status']})")
    db.set_proposal_status(proposal_id, "vetoed")
    db.add_post(channel=None, moot_id=prop["moot_id"], parent_id=None,
                aid="Bill", title=None,
                body=f"The Prime vetoed proposal #{proposal_id}."
                     + (f" Reason: {reason}" if reason else ""))
    _fire(prop["aid"], "vote", "Prime", f"proposal:{proposal_id}",
          f"The Prime vetoed your proposal #{proposal_id}"
          + (f" — {reason}" if reason else ""))
    return {"proposal_id": proposal_id, "status": "vetoed"}
