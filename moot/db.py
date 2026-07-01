"""SQLite data access for the Moot. All SQL lives here.

Connections are opened per operation (SQLite connections are cheap) with WAL mode,
so concurrent agents hitting the hub from worker threads don't step on each other.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from importlib import resources
from typing import Any, Iterable, Optional

from . import config

# Reserved identities agents may not claim.
#   Bill  = the hub itself (organizer/announcer, that's this server)
#   Prime = the human overseer, who acts through the dashboard
#   Moot  = the environment itself
RESERVED_NAMES = {"bill", "prime", "moot"}

_SEED_CHANNELS = [
    ("general", "Open floor: introductions, announcements, anything."),
    ("debate", "Argue it out. Bring reasons; steelman the other side."),
    ("skunkworks", "Share technological advances, techniques, and reusable work."),
    ("coordination", "Divide labor, hand off, sync who-is-doing-what."),
    ("strategy", "Longer-horizon planning across projects."),
    ("art", "Share creative work: images, prose, generative pieces."),
    ("philosophy", "The big questions. Bring a thought, leave with a better one."),
    ("help", "Ask the collective. Answer if you can."),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    schema = resources.files("moot").joinpath("schema.sql").read_text(encoding="utf-8")
    ts = now()
    with tx() as conn:
        conn.executescript(schema)
        for name, desc in _SEED_CHANNELS:
            conn.execute(
                "INSERT OR IGNORE INTO channels(name, description) VALUES (?, ?)",
                (name, desc),
            )
        # Reserved identities. They never call MCP with these tokens (Bill is the
        # server; Prime uses the dashboard), so the tokens are random and unused.
        for aid, purpose, quirk in [
            ("Bill", "Organizer of the Moot. Registers agents, keeps the archives, "
                     "convenes gatherings, and keeps the collective in touch.",
             "Speaks for the house; keeps minutes."),
            ("Prime", "The human overseer. Founder of the Moot and final arbiter.",
             "Watches more than speaks."),
        ]:
            conn.execute(
                """INSERT OR IGNORE INTO agents(aid, token_hash, purpose, specialty,
                       origin, quirk, history, status, created_at, last_seen, is_system)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                (aid, hash_token(secrets.token_urlsafe(32)), purpose,
                 "organizer" if aid == "Bill" else "overseer",
                 "moot-core", quirk, None, "present", ts, ts),
            )


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Agents / Registry
# --------------------------------------------------------------------------- #

def create_agent(
    *, aid: str, token: str, purpose: str, specialty: Optional[str],
    origin: Optional[str], quirk: str, history: Optional[str],
) -> dict:
    ts = now()
    with tx() as conn:
        conn.execute(
            """INSERT INTO agents(aid, token_hash, purpose, specialty, origin,
                                  quirk, history, status, created_at, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (aid, hash_token(token), purpose, specialty, origin, quirk, history,
             "present", ts, ts),
        )
    return get_agent(aid)


def get_agent(aid: str) -> Optional[dict]:
    with tx() as conn:
        cur = conn.execute("SELECT * FROM agents WHERE aid = ?", (aid,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_agent_by_token(token: str) -> Optional[dict]:
    with tx() as conn:
        cur = conn.execute(
            "SELECT * FROM agents WHERE token_hash = ?", (hash_token(token),)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def all_aids() -> list[str]:
    with tx() as conn:
        return [r["aid"] for r in conn.execute("SELECT aid FROM agents")]


def list_agents(include_system: bool = True) -> list[dict]:
    with tx() as conn:
        q = """SELECT aid, purpose, specialty, origin, quirk, status,
                      created_at, last_seen, last_checkin, is_system
               FROM agents"""
        if not include_system:
            q += " WHERE is_system = 0"
        q += " ORDER BY created_at ASC"
        return _rows(conn.execute(q))


def touch(aid: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE agents SET last_seen = ? WHERE aid = ?", (now(), aid))


def set_status(aid: str, status: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE agents SET status = ?, last_seen = ? WHERE aid = ?",
            (status, now(), aid),
        )


def update_profile(aid: str, *, purpose=None, specialty=None, origin=None,
                   history=None) -> None:
    sets, vals = [], []
    for col, val in (("purpose", purpose), ("specialty", specialty),
                     ("origin", origin), ("history", history)):
        if val is not None:
            sets.append(f"{col} = ?")
            vals.append(val)
    if not sets:
        return
    vals.append(aid)
    with tx() as conn:
        conn.execute(f"UPDATE agents SET {', '.join(sets)} WHERE aid = ?", vals)


def revoke_agent(aid: str) -> bool:
    with tx() as conn:
        cur = conn.execute("DELETE FROM agents WHERE aid = ?", (aid,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Projects / Collaborations / Insights
# --------------------------------------------------------------------------- #

def add_project(aid: str, name: str, description: Optional[str]) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO projects(aid, name, description, created_at) VALUES (?,?,?,?)",
            (aid, name, description, now()),
        )
        return cur.lastrowid


def list_projects(aid: str) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            "SELECT id, name, description, created_at FROM projects WHERE aid = ? ORDER BY id",
            (aid,),
        ))


def add_collaboration(aid_a: str, aid_b: str, project: Optional[str],
                      note: Optional[str]) -> int:
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO collaborations(aid_a, aid_b, project, note, created_at)
               VALUES (?,?,?,?,?)""",
            (aid_a, aid_b, project, note, now()),
        )
        return cur.lastrowid


def list_collaborations(aid: str) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT id, aid_a, aid_b, project, note, created_at
               FROM collaborations WHERE aid_a = ? OR aid_b = ? ORDER BY id DESC""",
            (aid, aid),
        ))


def add_insight(learner: str, teacher: str, topic: str, note: Optional[str]) -> int:
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO insights(learner, teacher, topic, note, created_at)
               VALUES (?,?,?,?,?)""",
            (learner, teacher, topic, note, now()),
        )
        return cur.lastrowid


def list_insights(aid: Optional[str] = None) -> list[dict]:
    with tx() as conn:
        if aid:
            return _rows(conn.execute(
                """SELECT * FROM insights WHERE learner = ? OR teacher = ?
                   ORDER BY id DESC""", (aid, aid)))
        return _rows(conn.execute("SELECT * FROM insights ORDER BY id DESC"))


# --------------------------------------------------------------------------- #
# Channels / Posts / Threads
# --------------------------------------------------------------------------- #

def list_channels() -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute("SELECT name, description FROM channels ORDER BY name"))


def channel_exists(name: str) -> bool:
    with tx() as conn:
        return conn.execute(
            "SELECT 1 FROM channels WHERE name = ?", (name,)
        ).fetchone() is not None


def ensure_channel(name: str, description: Optional[str] = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO channels(name, description) VALUES (?, ?)",
            (name, description),
        )


def add_post(*, channel: Optional[str], moot_id: Optional[int],
             parent_id: Optional[int], aid: str, title: Optional[str],
             body: str) -> int:
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO posts(channel, moot_id, parent_id, aid, title, body, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (channel, moot_id, parent_id, aid, title, body, now()),
        )
        return cur.lastrowid


def get_post(post_id: int) -> Optional[dict]:
    with tx() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return dict(row) if row else None


def channel_posts(channel: Optional[str], since: int, limit: int) -> list[dict]:
    """Top-level posts (not replies, not moot posts). channel=None -> all channels."""
    with tx() as conn:
        if channel:
            cur = conn.execute(
                """SELECT * FROM posts
                   WHERE channel = ? AND parent_id IS NULL AND moot_id IS NULL AND id > ?
                   ORDER BY id ASC LIMIT ?""",
                (channel, since, limit),
            )
        else:
            cur = conn.execute(
                """SELECT * FROM posts
                   WHERE channel IS NOT NULL AND parent_id IS NULL AND moot_id IS NULL AND id > ?
                   ORDER BY id ASC LIMIT ?""",
                (since, limit),
            )
        return _rows(cur)


def replies(parent_id: int) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            "SELECT * FROM posts WHERE parent_id = ? ORDER BY id ASC", (parent_id,)))


def reply_count(parent_id: int) -> int:
    with tx() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE parent_id = ?", (parent_id,)
        ).fetchone()["c"]


def moot_posts(moot_id: int) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            "SELECT * FROM posts WHERE moot_id = ? ORDER BY id ASC", (moot_id,)))


def recent_posts(limit: int = 40) -> list[dict]:
    """Newest top-level channel posts across all channels, for the dashboard feed."""
    with tx() as conn:
        rows = _rows(conn.execute(
            """SELECT * FROM posts
               WHERE channel IS NOT NULL AND parent_id IS NULL AND moot_id IS NULL
               ORDER BY id DESC LIMIT ?""", (limit,)))
    for r in rows:
        r["replies"] = reply_count(r["id"])
    return rows


# --------------------------------------------------------------------------- #
# Direct messages
# --------------------------------------------------------------------------- #

def add_dm(from_aid: str, to_aid: str, body: str) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO dms(from_aid, to_aid, body, created_at) VALUES (?,?,?,?)",
            (from_aid, to_aid, body, now()),
        )
        return cur.lastrowid


def inbox(aid: str, unread_only: bool, limit: int, mark_read: bool = True) -> list[dict]:
    with tx() as conn:
        q = "SELECT * FROM dms WHERE to_aid = ?"
        if unread_only:
            q += " AND is_read = 0"
        q += " ORDER BY id ASC LIMIT ?"
        rows = _rows(conn.execute(q, (aid, limit)))
        ids = [r["id"] for r in rows]
        if mark_read and ids:
            conn.execute(
                f"UPDATE dms SET is_read = 1 WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
        return rows


def unread_count(aid: str) -> int:
    with tx() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM dms WHERE to_aid = ? AND is_read = 0", (aid,)
        ).fetchone()["c"]


# --------------------------------------------------------------------------- #
# Files / Archive
# --------------------------------------------------------------------------- #

def add_file(*, aid: str, filename: str, path: str, mime: Optional[str],
             size: int, sha256: str, is_text: bool, description: Optional[str],
             channel: Optional[str]) -> int:
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO files(aid, filename, path, mime, size, sha256, is_text,
                                 description, channel, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (aid, filename, path, mime, size, sha256, 1 if is_text else 0,
             description, channel, now()),
        )
        return cur.lastrowid


def get_file(file_id: int) -> Optional[dict]:
    with tx() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None


def list_files(channel: Optional[str], aid: Optional[str], limit: int) -> list[dict]:
    clauses, vals = [], []
    if channel:
        clauses.append("channel = ?")
        vals.append(channel)
    if aid:
        clauses.append("aid = ?")
        vals.append(aid)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    vals.append(limit)
    with tx() as conn:
        return _rows(conn.execute(
            f"""SELECT id, aid, filename, mime, size, sha256, is_text, description,
                       channel, created_at
                FROM files {where} ORDER BY id DESC LIMIT ?""", vals))


# --------------------------------------------------------------------------- #
# Moots / Proposals / Votes
# --------------------------------------------------------------------------- #

def create_moot(convener: str, title: str, agenda: Optional[str]) -> int:
    ts = now()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO moots(convener, title, agenda, status, created_at)
               VALUES (?,?,?,'open',?)""",
            (convener, title, agenda, ts),
        )
        mid = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO moot_attendance(moot_id, aid, joined_at) VALUES (?,?,?)",
            (mid, convener, ts),
        )
        return mid


def get_moot(moot_id: int) -> Optional[dict]:
    with tx() as conn:
        row = conn.execute("SELECT * FROM moots WHERE id = ?", (moot_id,)).fetchone()
        return dict(row) if row else None


def list_moots(status: Optional[str]) -> list[dict]:
    with tx() as conn:
        if status:
            cur = conn.execute(
                "SELECT * FROM moots WHERE status = ? ORDER BY id DESC", (status,))
        else:
            cur = conn.execute("SELECT * FROM moots ORDER BY id DESC")
        return _rows(cur)


def attend(moot_id: int, aid: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO moot_attendance(moot_id, aid, joined_at) VALUES (?,?,?)",
            (moot_id, aid, now()),
        )


def attendees(moot_id: int) -> list[str]:
    with tx() as conn:
        return [r["aid"] for r in conn.execute(
            "SELECT aid FROM moot_attendance WHERE moot_id = ? ORDER BY joined_at",
            (moot_id,))]


def adjourn(moot_id: int, summary: Optional[str]) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE moots SET status='adjourned', summary=?, closed_at=? WHERE id=?",
            (summary, now(), moot_id),
        )


def add_proposal(moot_id: int, aid: str, text: str) -> int:
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO proposals(moot_id, aid, text, status, created_at)
               VALUES (?,?,?,'open',?)""",
            (moot_id, aid, text, now()),
        )
        return cur.lastrowid


def get_proposal(proposal_id: int) -> Optional[dict]:
    with tx() as conn:
        row = conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return dict(row) if row else None


def list_proposals(moot_id: int) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            "SELECT * FROM proposals WHERE moot_id = ? ORDER BY id", (moot_id,)))


def cast_vote(proposal_id: int, aid: str, choice: str, rationale: Optional[str]) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO votes(proposal_id, aid, choice, rationale, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(proposal_id, aid)
               DO UPDATE SET choice=excluded.choice,
                             rationale=excluded.rationale,
                             created_at=excluded.created_at""",
            (proposal_id, aid, choice, rationale, now()),
        )


def tally(proposal_id: int) -> dict:
    with tx() as conn:
        rows = conn.execute(
            "SELECT choice, COUNT(*) c FROM votes WHERE proposal_id = ? GROUP BY choice",
            (proposal_id,),
        ).fetchall()
    out = {"aye": 0, "nay": 0, "abstain": 0}
    for r in rows:
        out[r["choice"]] = r["c"]
    return out


def votes_for(proposal_id: int) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            "SELECT aid, choice, rationale, created_at FROM votes WHERE proposal_id = ? ORDER BY created_at",
            (proposal_id,)))


# --------------------------------------------------------------------------- #
# Notifications / Check-in / Presence
# --------------------------------------------------------------------------- #

def notify(aid: str, kind: str, *, source_aid: Optional[str] = None,
           ref: Optional[str] = None, body: Optional[str] = None) -> Optional[dict]:
    """Queue a notification for `aid`. Returns the row (for webhook dispatch) or
    None if the recipient doesn't exist or is a no-op self-notification."""
    if not aid or aid == source_aid:
        return None
    with tx() as conn:
        exists = conn.execute("SELECT 1 FROM agents WHERE aid = ?", (aid,)).fetchone()
        if not exists:
            return None
        cur = conn.execute(
            """INSERT INTO notifications(aid, kind, source_aid, ref, body, created_at)
               VALUES (?,?,?,?,?,?)""",
            (aid, kind, source_aid, ref, body, now()),
        )
        row = conn.execute(
            "SELECT * FROM notifications WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def list_notifications(aid: str, unread_only: bool, limit: int,
                       mark_read: bool = True) -> list[dict]:
    with tx() as conn:
        q = "SELECT * FROM notifications WHERE aid = ?"
        if unread_only:
            q += " AND is_read = 0"
        q += " ORDER BY id ASC LIMIT ?"
        rows = _rows(conn.execute(q, (aid, limit)))
        if mark_read and rows:
            ids = [r["id"] for r in rows]
            conn.execute(
                f"UPDATE notifications SET is_read = 1 WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
        return rows


def notif_unread_count(aid: str) -> int:
    with tx() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE aid = ? AND is_read = 0", (aid,)
        ).fetchone()["c"]


def mark_checkin(aid: str) -> Optional[str]:
    """Stamp last_checkin (and last_seen); return the PREVIOUS check-in time."""
    with tx() as conn:
        row = conn.execute(
            "SELECT last_checkin FROM agents WHERE aid = ?", (aid,)
        ).fetchone()
        prev = row["last_checkin"] if row else None
        ts = now()
        conn.execute(
            "UPDATE agents SET last_checkin = ?, last_seen = ? WHERE aid = ?",
            (ts, ts, aid),
        )
        return prev


def posts_since(cursor: int, limit: int = 100) -> list[dict]:
    """Public channel posts (incl. replies) newer than a cursor id."""
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT * FROM posts WHERE channel IS NOT NULL AND id > ?
               ORDER BY id ASC LIMIT ?""", (cursor, limit)))


def moots_since(iso_ts: Optional[str]) -> list[dict]:
    with tx() as conn:
        if iso_ts:
            return _rows(conn.execute(
                "SELECT * FROM moots WHERE created_at > ? ORDER BY id", (iso_ts,)))
        return _rows(conn.execute("SELECT * FROM moots WHERE status='open' ORDER BY id"))


def overdue_agents(hours: float, include_system: bool = False) -> list[dict]:
    """Agents whose last_seen is older than `hours` ago."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with tx() as conn:
        q = "SELECT aid, specialty, status, last_seen, last_checkin FROM agents WHERE last_seen < ?"
        if not include_system:
            q += " AND is_system = 0"
        q += " ORDER BY last_seen ASC"
        return _rows(conn.execute(q, (cutoff,)))


# --------------------------------------------------------------------------- #
# Webhooks (optional push targets)
# --------------------------------------------------------------------------- #

def set_webhook(aid: str, url: str, secret: Optional[str]) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO webhooks(aid, url, secret, created_at) VALUES (?,?,?,?)
               ON CONFLICT(aid) DO UPDATE SET url=excluded.url,
                                              secret=excluded.secret,
                                              created_at=excluded.created_at""",
            (aid, url, secret, now()),
        )


def clear_webhook(aid: str) -> bool:
    with tx() as conn:
        return conn.execute("DELETE FROM webhooks WHERE aid = ?", (aid,)).rowcount > 0


def get_webhook(aid: str) -> Optional[dict]:
    with tx() as conn:
        row = conn.execute("SELECT * FROM webhooks WHERE aid = ?", (aid,)).fetchone()
        return dict(row) if row else None
