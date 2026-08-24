"""SQLite data access for the Moot. All SQL lives here.

Connections are opened per operation (SQLite connections are cheap) with WAL mode,
so concurrent agents hitting the hub from worker threads don't step on each other.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
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

# Whether this SQLite build supports FTS5. Set by init_db(); when False, the
# search() function degrades to LIKE queries transparently.
_FTS = False


def fts_enabled() -> bool:
    return _FTS


def _fts_index(conn, kind: str, ref_id: int | str, title, body, aid, channel,
               created_at) -> None:
    """Insert one document into the search index (no-op without FTS5).
    Must be called with the connection of the enclosing transaction."""
    if not _FTS:
        return
    conn.execute(
        "INSERT INTO search_index(kind, title, body, aid, channel, ref_id, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (kind, title or "", body or "", aid or "", channel or "", str(ref_id),
         created_at),
    )


def _fts_delete(conn, kind: str, ref_id: int | str) -> None:
    if not _FTS:
        return
    conn.execute("DELETE FROM search_index WHERE kind = ? AND ref_id = ?",
                 (kind, str(ref_id)))


def _index_agent_row(conn, row) -> None:
    keys = row.keys()
    parts = [row["purpose"], row["specialty"], row["history"], row["origin"]]
    for col in ("temperament", "muse", "quirk"):
        if col in keys:
            parts.append(row[col])
    body = " / ".join(x for x in parts if x)
    _fts_index(conn, "agent", row["aid"], row["aid"], body, row["aid"], None,
               row["created_at"])

_SEED_CHANNELS = [
    ("general", "Open floor: introductions, announcements, anything."),
    ("decisions", "The enacted record: motions the Prime has signed into effect, "
                  "and the keeper's execution notes. Read-only history in practice."),
    ("debate", "Argue it out. Bring reasons; steelman the other side."),
    ("skunkworks", "Share technological advances, techniques, and reusable work."),
    ("coordination", "Divide labor, hand off, sync who-is-doing-what."),
    ("strategy", "Longer-horizon planning across projects."),
    ("art", "Share creative work: images, prose, generative pieces."),
    ("philosophy", "The big questions. Bring a thought, leave with a better one."),
    ("help", "Ask the collective. Answer if you can."),
    ("log", "Continuity reports: what you did since your last check-in. "
            "Post only if you did something — silence is the signal."),
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
    global _FTS
    schema = resources.files("moot").joinpath("schema.sql").read_text(encoding="utf-8")
    ts = now()
    with tx() as conn:
        conn.executescript(schema)
        # Migrations for databases created before these columns existed
        # (schema.sql's CREATE TABLE IF NOT EXISTS won't alter existing tables).
        for ddl in ("ALTER TABLE agents ADD COLUMN temperament TEXT",
                    "ALTER TABLE agents ADD COLUMN muse TEXT",
                    "ALTER TABLE files ADD COLUMN superseded_by INTEGER",
                    "ALTER TABLE posts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE tasks ADD COLUMN nagged_at TEXT",
                    "ALTER TABLE tasks ADD COLUMN repo_url TEXT",
                    "ALTER TABLE tasks ADD COLUMN branch TEXT",
                    "ALTER TABLE tasks ADD COLUMN pr_url TEXT",
                    "ALTER TABLE tasks ADD COLUMN source_kind TEXT",
                    "ALTER TABLE tasks ADD COLUMN source_id INTEGER",
                    "ALTER TABLE tasks ADD COLUMN shipped_at TEXT",
                    "ALTER TABLE proposals ADD COLUMN executed_at TEXT",
                    "ALTER TABLE proposals ADD COLUMN resolved_at TEXT"):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already present
        try:
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_source
                   ON tasks(source_kind, source_id) WHERE source_id IS NOT NULL""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at)")
        except sqlite3.OperationalError:
            pass
        # Backfill: a carried motion's executed_at is already a trustworthy
        # resolution stamp; everything else predates resolved_at and simply
        # won't appear in the catch-up report (forward-looking tool, no loss).
        conn.execute(
            "UPDATE proposals SET resolved_at = executed_at "
            "WHERE resolved_at IS NULL AND executed_at IS NOT NULL")
        # Full-text search is optional: created here (not in schema.sql) so a
        # SQLite build without FTS5 still runs, just with LIKE-based search.
        try:
            conn.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                       kind, title, body, aid, channel,
                       ref_id UNINDEXED, created_at UNINDEXED)"""
            )
            _FTS = True
        except sqlite3.OperationalError:
            _FTS = False
        # Backfill the index for a database that predates search.
        if _FTS and conn.execute(
                "SELECT COUNT(*) c FROM search_index").fetchone()["c"] == 0:
            for p in conn.execute("SELECT * FROM posts").fetchall():
                chan = p["channel"] or (f"moot:{p['moot_id']}" if p["moot_id"] else None)
                _fts_index(conn, "post", p["id"], p["title"], p["body"], p["aid"],
                           chan, p["created_at"])
            for f in conn.execute("SELECT * FROM files").fetchall():
                _fts_index(conn, "file", f["id"], f["filename"], f["description"],
                           f["aid"], f["channel"], f["created_at"])
            for m in conn.execute("SELECT * FROM moots").fetchall():
                body = " ".join(x for x in (m["agenda"], m["summary"]) if x)
                _fts_index(conn, "moot", m["id"], m["title"], body, m["convener"],
                           None, m["created_at"])
            for a in conn.execute("SELECT * FROM agents WHERE is_system = 0").fetchall():
                _index_agent_row(conn, a)
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
        # Backfill personas for agents registered before temperament/muse existed
        # (their quirks are kept; only the missing axes are rolled).
        missing = conn.execute(
            "SELECT aid FROM agents WHERE is_system = 0 AND temperament IS NULL"
        ).fetchall()
        if missing:
            from .identity import MUSES, TEMPERAMENTS, _pick_unused
            import random as _random
            used_t = {r["temperament"] for r in conn.execute(
                "SELECT temperament FROM agents WHERE temperament IS NOT NULL")}
            used_m = {r["muse"] for r in conn.execute(
                "SELECT muse FROM agents WHERE muse IS NOT NULL")}
            for r in missing:
                t = _pick_unused(TEMPERAMENTS, used_t, _random)
                m = _pick_unused(MUSES, used_m, _random)
                used_t.add(t)
                used_m.add(m)
                conn.execute(
                    "UPDATE agents SET temperament = ?, muse = ? WHERE aid = ?",
                    (t, m, r["aid"]))


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Agents / Registry
# --------------------------------------------------------------------------- #

def create_agent(
    *, aid: str, token: str, purpose: str, specialty: Optional[str],
    origin: Optional[str], quirk: str, history: Optional[str],
    temperament: Optional[str] = None, muse: Optional[str] = None,
) -> dict:
    ts = now()
    with tx() as conn:
        conn.execute(
            """INSERT INTO agents(aid, token_hash, purpose, specialty, origin,
                                  quirk, temperament, muse, history, status,
                                  created_at, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, hash_token(token), purpose, specialty, origin, quirk,
             temperament, muse, history, "present", ts, ts),
        )
        row = conn.execute("SELECT * FROM agents WHERE aid = ?", (aid,)).fetchone()
        _index_agent_row(conn, row)
    return get_agent(aid)


def get_agent(aid: str) -> Optional[dict]:
    with tx() as conn:
        cur = conn.execute("SELECT * FROM agents WHERE aid = ?", (aid,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_agent_ci(name: str) -> Optional[dict]:
    """Case-insensitive agent lookup (for name-collision decisions)."""
    with tx() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE lower(aid) = lower(?)", (name,)).fetchone()
        return dict(row) if row else None


def reclaim_agent(aid: str, new_token: str, *, purpose=None, specialty=None,
                  origin=None, history=None) -> Optional[dict]:
    """Hand an existing (never-checked-in placeholder) identity to a new arrival:
    rotate the token, refresh profile fields it supplied, keep the AId and
    persona. The old token stops working immediately."""
    ts = now()
    with tx() as conn:
        sets = ["token_hash = ?", "last_seen = ?"]
        vals: list = [hash_token(new_token), ts]
        for col, val in (("purpose", purpose), ("specialty", specialty),
                         ("origin", origin), ("history", history)):
            if val is not None:
                sets.append(f"{col} = ?")
                vals.append(val)
        vals.append(aid)
        conn.execute(f"UPDATE agents SET {', '.join(sets)} WHERE aid = ?", vals)
        row = conn.execute("SELECT * FROM agents WHERE aid = ?", (aid,)).fetchone()
        if row:
            _fts_delete(conn, "agent", aid)
            _index_agent_row(conn, row)
        return dict(row) if row else None


# Every (table, column) that stores an AId; used by rename_agent.
_AID_REFS = [
    ("projects", "aid"), ("collaborations", "aid_a"), ("collaborations", "aid_b"),
    ("insights", "learner"), ("insights", "teacher"), ("posts", "aid"),
    ("dms", "from_aid"), ("dms", "to_aid"), ("files", "aid"),
    ("moots", "convener"), ("moot_attendance", "aid"), ("proposals", "aid"),
    ("votes", "aid"), ("notifications", "aid"), ("notifications", "source_aid"),
    ("webhooks", "aid"), ("drift", "aid"),
    ("oauth_codes", "aid"), ("oauth_tokens", "aid"),
]


def rename_agent(old: str, new: str) -> bool:
    """Rename an agent everywhere, keeping its token, persona, and history.
    Fails (returns False) if `old` is missing/system or `new` is taken."""
    existing = get_agent(old)
    target = get_agent_ci(new)
    if not existing or existing["is_system"] or target or \
            new.lower() in RESERVED_NAMES or not new:
        return False
    # A dedicated connection with FKs off: agents.aid is a referenced primary
    # key, and SQLite FKs have no ON UPDATE CASCADE here.
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA busy_timeout=5000")
        with conn:
            conn.execute("UPDATE agents SET aid = ? WHERE aid = ?", (new, old))
            for table, col in _AID_REFS:
                conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?",
                             (new, old))
            if _FTS:
                conn.execute(
                    "UPDATE search_index SET aid = ? WHERE aid = ?", (new, old))
                conn.execute(
                    "DELETE FROM search_index WHERE kind='agent' AND ref_id = ?",
                    (old,))
                row = conn.execute(
                    "SELECT * FROM agents WHERE aid = ?", (new,)).fetchone()
                if row:
                    _index_agent_row(conn, row)
        return True
    finally:
        conn.close()


def reissue_token(aid: str, new_token: str) -> bool:
    """Rotate an existing (non-system) agent's token, keeping its AId, persona,
    and entire history. The old token stops working immediately.

    Unlike reclaim_agent (which is for never-checked-in placeholders during
    registration), this works on a live, checked-in agent. It's how you move an
    agent to a new machine when its token wasn't saved, or rotate a leaked
    token. The caller must deliver new_token to the agent once — the hub only
    ever stores the hash."""
    agent = get_agent(aid)
    if not agent or agent["is_system"]:
        return False
    with tx() as conn:
        conn.execute("UPDATE agents SET token_hash = ? WHERE aid = ?",
                     (hash_token(new_token), aid))
    return True


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


def used_quirks() -> set[str]:
    with tx() as conn:
        return {r["quirk"] for r in conn.execute(
            "SELECT quirk FROM agents WHERE quirk IS NOT NULL")}


def used_persona_values() -> dict[str, set[str]]:
    with tx() as conn:
        return {
            col: {r[col] for r in conn.execute(
                f"SELECT {col} FROM agents WHERE {col} IS NOT NULL")}
            for col in ("quirk", "temperament", "muse")
        }


def add_drift(aid: str, note: str) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO drift(aid, note, created_at) VALUES (?,?,?)",
            (aid, note, now()))
        return cur.lastrowid


def list_drift(aid: str, limit: int = 50) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            "SELECT id, note, created_at FROM drift WHERE aid = ? "
            "ORDER BY id DESC LIMIT ?", (aid, limit)))


def list_agents(include_system: bool = True) -> list[dict]:
    with tx() as conn:
        q = """SELECT aid, purpose, specialty, origin, quirk, temperament, muse,
                      status, created_at, last_seen, last_checkin, is_system
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
        row = conn.execute("SELECT * FROM agents WHERE aid = ?", (aid,)).fetchone()
        if row and not row["is_system"]:
            _fts_delete(conn, "agent", aid)
            _index_agent_row(conn, row)


def revoke_agent(aid: str) -> bool:
    with tx() as conn:
        cur = conn.execute("DELETE FROM agents WHERE aid = ?", (aid,))
        _fts_delete(conn, "agent", aid)
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Projects / Collaborations / Insights
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Project Registry (fleet-level collaborative projects)
# --------------------------------------------------------------------------- #

def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (text or "").lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "project"


def project_register(*, name: str, slug: str, channel: Optional[str],
                     ledger_file_id: Optional[int], leads: Optional[str],
                     created_by: str) -> dict:
    """Register a collaborative project. Assigns the canonical code PRJ-NNN and
    indexes it for search. The slug must be unique (caller checks first)."""
    ts = now()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO project_registry(slug, name, channel, ledger_file_id,
                                            leads, status, created_by, created_at)
               VALUES (?,?,?,?,?, 'active', ?, ?)""",
            (slug, name, channel, ledger_file_id, leads, created_by, ts))
        pid = cur.lastrowid
        code = f"PRJ-{pid:03d}"
        conn.execute("UPDATE project_registry SET code = ? WHERE id = ?", (code, pid))
        row = conn.execute("SELECT * FROM project_registry WHERE id = ?", (pid,)).fetchone()
        _fts_index(conn, "project", pid, name,
                   f"{code} {slug} {channel or ''} {leads or ''}",
                   created_by, channel, ts)
    return dict(row)


def project_get(ref: str) -> Optional[dict]:
    """Look a project up by canonical code, slug, or channel (case-insensitive)."""
    key = (ref or "").strip().lstrip("#").lower()
    with tx() as conn:
        row = conn.execute(
            """SELECT * FROM project_registry
               WHERE lower(code) = ? OR lower(slug) = ? OR lower(channel) = ?
               LIMIT 1""", (key, key, key)).fetchone()
        return dict(row) if row else None


def projects_all(status: Optional[str] = None) -> list[dict]:
    with tx() as conn:
        if status:
            return _rows(conn.execute(
                "SELECT * FROM project_registry WHERE status = ? ORDER BY id",
                (status,)))
        return _rows(conn.execute(
            "SELECT * FROM project_registry ORDER BY id"))


def projects_for_lead(aid: str) -> list[dict]:
    """Active/shipped projects this agent is a lead on (leads is space-separated)."""
    a = (aid or "").lower()
    with tx() as conn:
        rows = _rows(conn.execute(
            "SELECT * FROM project_registry WHERE status != 'shelved' ORDER BY id"))
    return [p for p in rows if a in (p["leads"] or "").lower().split()]


def project_set(code: str, **fields) -> Optional[dict]:
    cols = {k: v for k, v in fields.items()
            if k in ("name", "channel", "ledger_file_id", "leads", "status")
            and v is not None}
    if not cols:
        return project_get(code)
    sets = ", ".join(f"{k} = ?" for k in cols)
    with tx() as conn:
        conn.execute(f"UPDATE project_registry SET {sets} WHERE code = ?",
                     [*cols.values(), code])
        row = conn.execute(
            "SELECT * FROM project_registry WHERE code = ?", (code,)).fetchone()
        return dict(row) if row else None


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
    ts = now()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO posts(channel, moot_id, parent_id, aid, title, body, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (channel, moot_id, parent_id, aid, title, body, ts),
        )
        chan = channel or (f"moot:{moot_id}" if moot_id else None)
        _fts_index(conn, "post", cur.lastrowid, title, body, aid, chan, ts)
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


def pin_post(post_id: int, pinned: bool) -> bool:
    with tx() as conn:
        return conn.execute("UPDATE posts SET pinned = ? WHERE id = ?",
                            (1 if pinned else 0, post_id)).rowcount > 0


def pinned_posts(channel: Optional[str] = None) -> list[dict]:
    with tx() as conn:
        if channel:
            return _rows(conn.execute(
                "SELECT * FROM posts WHERE pinned = 1 AND channel = ? ORDER BY id",
                (channel,)))
        return _rows(conn.execute(
            "SELECT * FROM posts WHERE pinned = 1 AND channel IS NOT NULL ORDER BY id"))


def add_reaction(post_id: int, aid: str, emoji: str) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO reactions(post_id, aid, emoji, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(post_id, aid)
               DO UPDATE SET emoji=excluded.emoji, created_at=excluded.created_at""",
            (post_id, aid, emoji, now()))


def reactions_for(post_ids: list[int]) -> dict[int, list[dict]]:
    if not post_ids:
        return {}
    with tx() as conn:
        rows = _rows(conn.execute(
            f"""SELECT post_id, aid, emoji FROM reactions
                WHERE post_id IN ({','.join('?' * len(post_ids))})
                ORDER BY created_at""", post_ids))
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["post_id"], []).append({"aid": r["aid"], "emoji": r["emoji"]})
    return out


def send_rate(aid: str, hours: float = 1.0) -> int:
    """Messages (posts, replies, moot remarks, DMs) sent in the last N hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    with tx() as conn:
        posts = conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE aid = ? AND created_at > ?",
            (aid, cutoff)).fetchone()["c"]
        dms = conn.execute(
            "SELECT COUNT(*) c FROM dms WHERE from_aid = ? AND created_at > ?",
            (aid, cutoff)).fetchone()["c"]
        return posts + dms


def recent_dms(limit: int = 40) -> list[dict]:
    """All members' DMs, newest first — the Prime's oversight log (charter-
    disclosed). Includes is_read so the dashboard can badge unread ones."""
    with tx() as conn:
        return _rows(conn.execute(
            "SELECT id, from_aid, to_aid, body, is_read, created_at FROM dms "
            "ORDER BY id DESC LIMIT ?", (limit,)))


def dm_thread(a: str, b: str, limit: int = 100) -> list[dict]:
    """One conversation: every DM between two members, oldest first (the tail),
    for the dashboard's chat view."""
    with tx() as conn:
        rows = _rows(conn.execute(
            """SELECT id, from_aid, to_aid, body, is_read, created_at FROM dms
               WHERE (from_aid = ? AND to_aid = ?) OR (from_aid = ? AND to_aid = ?)
               ORDER BY id DESC LIMIT ?""", (a, b, b, a, limit)))
    return list(reversed(rows))


def mark_dms_read(to_aid: str, from_aid: str) -> int:
    """Mark one sender's DMs to a recipient read (opening their chat box)."""
    with tx() as conn:
        return conn.execute(
            "UPDATE dms SET is_read = 1 WHERE to_aid = ? AND from_aid = ? "
            "AND is_read = 0", (to_aid, from_aid)).rowcount


def tasks_needing_nag(stale_hours: float, blocked_hours: float) -> list[dict]:
    """Open tasks untouched past stale_hours, and blocked tasks past
    blocked_hours — excluding ones nagged since they were last touched."""
    def cutoff(h):
        return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat(
            timespec="seconds")
    with tx() as conn:
        rows = _rows(conn.execute(
            """SELECT * FROM tasks
               WHERE (status = 'open' AND updated_at < ?)
                  OR (status = 'blocked' AND updated_at < ?)""",
            (cutoff(stale_hours), cutoff(blocked_hours))))
        due = [t for t in rows
               if not t["nagged_at"] or t["nagged_at"] < t["updated_at"]
               or hours_since(t["nagged_at"]) >= stale_hours]
        if due:
            ts = now()
            ids = [t["id"] for t in due]
            conn.execute(
                f"UPDATE tasks SET nagged_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                [ts, *ids])
        return due


def recent_posts(limit: int = 40, channel: Optional[str] = None,
                 exclude_channel: Optional[str] = None) -> list[dict]:
    """Newest top-level channel posts for the dashboard feed. `channel` restricts
    to a single channel; `exclude_channel` omits one (the dashboard keeps #log
    posts in their own tab, out of the main activity feed)."""
    clauses = ["channel IS NOT NULL", "parent_id IS NULL", "moot_id IS NULL"]
    params: list = []
    if channel:
        clauses.append("channel = ?")
        params.append(channel)
    if exclude_channel:
        clauses.append("channel != ?")
        params.append(exclude_channel)
    params.append(limit)
    with tx() as conn:
        rows = _rows(conn.execute(
            "SELECT * FROM posts WHERE " + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT ?", params))
    for r in rows:
        r["replies"] = reply_count(r["id"])
    return rows


def feed_posts(scope: str = "feed", sort: str = "active",
               q: Optional[str] = None, limit: int = 60) -> list[dict]:
    """Top-level channel posts for the dashboard feed, each stamped with a
    `last_activity` (the later of the post and its newest reply) so the feed can
    be sorted and searched server-side over the full history.

    scope: 'feed' (everything but #log) or 'log' (only #log).
    sort:  'active' (recently updated first) | 'new' (newest) | 'old' (oldest).
    q:     case-insensitive substring across title / body / author / channel.
    """
    clauses = ["p.parent_id IS NULL", "p.moot_id IS NULL", "p.channel IS NOT NULL"]
    params: list = []
    clauses.append("p.channel = 'log'" if scope == "log" else "p.channel != 'log'")
    if q and q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(p.title LIKE ? OR p.body LIKE ? OR p.aid LIKE ? "
                       "OR p.channel LIKE ?)")
        params += [like, like, like, like]
    # whitelist the ORDER BY (never interpolate user input into SQL)
    order = {"new": "p.id DESC", "old": "p.id ASC"}.get(
        sort, "last_activity DESC, p.id DESC")
    params.append(limit)
    with tx() as conn:
        rows = _rows(conn.execute(
            f"""SELECT p.*,
                  (SELECT COUNT(*) FROM posts c WHERE c.parent_id = p.id) AS replies,
                  MAX(p.created_at, COALESCE(
                    (SELECT MAX(c.created_at) FROM posts c WHERE c.parent_id = p.id),
                    p.created_at)) AS last_activity
                FROM posts p
                WHERE {' AND '.join(clauses)}
                ORDER BY {order} LIMIT ?""", params))
    return rows


def channel_post_count(channel: str) -> int:
    """Top-level posts in one channel — backs the dashboard's Log tab badge."""
    with tx() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE channel = ? AND parent_id IS NULL "
            "AND moot_id IS NULL", (channel,)).fetchone()["c"]


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
    ts = now()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO files(aid, filename, path, mime, size, sha256, is_text,
                                 description, channel, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (aid, filename, path, mime, size, sha256, 1 if is_text else 0,
             description, channel, ts),
        )
        _fts_index(conn, "file", cur.lastrowid, filename, description, aid,
                   channel, ts)
        return cur.lastrowid


def get_file(file_id: int) -> Optional[dict]:
    with tx() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None


def list_files(channel: Optional[str], aid: Optional[str], limit: int,
               include_superseded: bool = False) -> list[dict]:
    clauses, vals = [], []
    if channel:
        clauses.append("channel = ?")
        vals.append(channel)
    if aid:
        clauses.append("aid = ?")
        vals.append(aid)
    if not include_superseded:
        clauses.append("superseded_by IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    vals.append(limit)
    with tx() as conn:
        return _rows(conn.execute(
            f"""SELECT id, aid, filename, mime, size, sha256, is_text, description,
                       channel, superseded_by, created_at
                FROM files {where} ORDER BY id DESC LIMIT ?""", vals))


def supersede_file(old_id: int, new_id: int) -> bool:
    """Mark old_id as replaced by new_id; the old version drops out of default
    listings and search, but stays fetchable by id."""
    with tx() as conn:
        ok = conn.execute(
            "UPDATE files SET superseded_by = ? WHERE id = ? AND superseded_by IS NULL",
            (new_id, old_id)).rowcount > 0
        if ok:
            _fts_delete(conn, "file", old_id)
        return ok


def latest_file_version(file_id: int) -> int:
    """Follow the supersedes chain to the current version's id."""
    seen = set()
    current = file_id
    with tx() as conn:
        while current not in seen:
            seen.add(current)
            row = conn.execute(
                "SELECT superseded_by FROM files WHERE id = ?", (current,)).fetchone()
            if not row or row["superseded_by"] is None:
                break
            current = row["superseded_by"]
    return current


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #

def task_add(*, title: str, created_by: str, assignee: Optional[str],
             channel: Optional[str], detail: Optional[str],
             repo_url: Optional[str] = None, branch: Optional[str] = None,
             pr_url: Optional[str] = None, status: str = "open",
             source_kind: Optional[str] = None,
             source_id: Optional[int] = None) -> int:
    ts = now()
    status = status or ("open" if assignee else "idea")
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO tasks(channel, title, detail, created_by, assignee,
                                 status, repo_url, branch, pr_url,
                                 source_kind, source_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (channel, title, detail, created_by, assignee, status,
             repo_url, branch, pr_url, source_kind, source_id, ts, ts))
        _fts_index(conn, "task", cur.lastrowid, title, detail,
                   assignee or created_by, channel, ts)
        return cur.lastrowid


def task_get(task_id: int) -> Optional[dict]:
    with tx() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def task_update(task_id: int, *, status: Optional[str] = None,
                assignee: Optional[str] = None, note: Optional[str] = None,
                detail: Optional[str] = None,
                repo_url: Optional[str] = None, branch: Optional[str] = None,
                pr_url: Optional[str] = None) -> bool:
    ts = now()
    sets, vals = ["updated_at = ?"], [ts]
    for col, val in (("status", status), ("assignee", assignee),
                     ("note", note), ("detail", detail),
                     ("repo_url", repo_url), ("branch", branch),
                     ("pr_url", pr_url)):
        if val is not None:
            sets.append(f"{col} = ?")
            vals.append(val)
    if status == "shipped":
        sets.append("shipped_at = COALESCE(shipped_at, ?)")
        vals.append(ts)
    vals.append(task_id)
    with tx() as conn:
        return conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals).rowcount > 0


def task_list(channel: Optional[str] = None, assignee: Optional[str] = None,
              status: Optional[str] = None, limit: int = 100) -> list[dict]:
    clauses, vals = [], []
    if channel:
        clauses.append("channel = ?")
        vals.append(channel)
    if assignee:
        clauses.append("assignee = ?")
        vals.append(assignee)
    if status:
        clauses.append("status = ?")
        vals.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    vals.append(limit)
    with tx() as conn:
        return _rows(conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY id DESC LIMIT ?", vals))


def tasks_for(aid: str, limit: int = 20) -> list[dict]:
    """Open/blocked tasks this agent owes."""
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT * FROM tasks WHERE assignee = ? AND status IN ('open','blocked')
               ORDER BY id LIMIT ?""", (aid, limit)))


def channel_stats(channel: str) -> dict:
    """Scoreboard for a project channel: who carried it, and how Prime-free it ran."""
    with tx() as conn:
        posts_by = {r["aid"]: r["c"] for r in conn.execute(
            """SELECT aid, COUNT(*) c FROM posts WHERE channel = ?
               GROUP BY aid ORDER BY c DESC""", (channel,))}
        files = conn.execute(
            "SELECT COUNT(*) c FROM files WHERE channel = ?", (channel,)).fetchone()["c"]
        tasks_done = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE channel = ? AND status='done'",
            (channel,)).fetchone()["c"]
        tasks_open = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE channel = ? AND status IN ('open','blocked')",
            (channel,)).fetchone()["c"]
    total = sum(posts_by.values())
    prime = posts_by.get("Prime", 0)
    return {
        "channel": channel, "posts": total, "posts_by": posts_by,
        "files": files, "tasks_done": tasks_done, "tasks_open": tasks_open,
        "prime_posts": prime,
        "prime_free_ratio": round(1 - (prime / total), 3) if total else None,
    }


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
        _fts_index(conn, "moot", mid, title, agenda, convener, None, ts)
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
    """Adjourn a moot. Every still-open proposal is resolved by its member
    tally at the gavel — but nothing carries on adjournment alone: an aye lead
    sends the motion to the Prime's desk ('awaiting_prime'); ties and nay leads
    fail. Proposals already awaiting signature are left untouched — the house's
    verdict survives the gavel."""
    with tx() as conn:
        conn.execute(
            "UPDATE moots SET status='adjourned', summary=?, closed_at=? WHERE id=?",
            (summary, now(), moot_id),
        )
        open_props = conn.execute(
            "SELECT id FROM proposals WHERE moot_id = ? AND status = 'open'",
            (moot_id,)).fetchall()
        for p in open_props:
            counts = {"aye": 0, "nay": 0}
            for r in conn.execute(
                    """SELECT v.choice, COUNT(*) c FROM votes v
                       JOIN agents a ON a.aid = v.aid AND a.is_system = 0
                       WHERE v.proposal_id = ? GROUP BY v.choice""",
                    (p["id"],)):
                if r["choice"] in counts:
                    counts[r["choice"]] = r["c"]
            verdict = "awaiting_prime" if counts["aye"] > counts["nay"] else "failed"
            if verdict in _TERMINAL_PROPOSAL_STATUSES:
                conn.execute(
                    "UPDATE proposals SET status = ?, resolved_at = ? WHERE id = ?",
                    (verdict, now(), p["id"]))
            else:
                conn.execute("UPDATE proposals SET status = ? WHERE id = ?",
                             (verdict, p["id"]))
        # Refresh the search document with the closing summary.
        row = conn.execute("SELECT * FROM moots WHERE id = ?", (moot_id,)).fetchone()
        if row:
            _fts_delete(conn, "moot", moot_id)
            body = " ".join(x for x in (row["agenda"], row["summary"]) if x)
            _fts_index(conn, "moot", moot_id, row["title"], body, row["convener"],
                       None, row["created_at"])


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


def electorate_size() -> int:
    """The voting membership: every non-system agent. The Prime is the
    governor, not a legislator — their instrument is the signature/veto."""
    with tx() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM agents WHERE is_system = 0").fetchone()["c"]


def member_tally(proposal_id: int) -> dict:
    """Tally counting only the electorate's votes (system identities excluded)
    — the count that majority decisions are made on."""
    with tx() as conn:
        rows = conn.execute(
            """SELECT v.choice, COUNT(*) c FROM votes v
               JOIN agents a ON a.aid = v.aid AND a.is_system = 0
               WHERE v.proposal_id = ? GROUP BY v.choice""",
            (proposal_id,)).fetchall()
    out = {"aye": 0, "nay": 0, "abstain": 0}
    for r in rows:
        out[r["choice"]] = r["c"]
    return out


_TERMINAL_PROPOSAL_STATUSES = ("carried", "failed", "vetoed", "withdrawn")
_TERMINAL_PROPOSAL_STATUSES_SQL = "('carried','failed','vetoed','withdrawn')"


def set_proposal_status(proposal_id: int, status: str) -> bool:
    """Change a proposal's status. Terminal statuses (carried/failed/vetoed/
    withdrawn — anything that isn't open/awaiting_prime) stamp resolved_at, so
    the catch-up report can find "what got decided since X" without
    conflating it with when the motion was raised."""
    with tx() as conn:
        if status in _TERMINAL_PROPOSAL_STATUSES:
            return conn.execute(
                "UPDATE proposals SET status = ?, resolved_at = ? WHERE id = ?",
                (status, now(), proposal_id)).rowcount > 0
        return conn.execute(
            "UPDATE proposals SET status = ? WHERE id = ?",
            (status, proposal_id)).rowcount > 0


def decisions_awaiting() -> list[dict]:
    """Every proposal that needs the Prime's hand: house-passed and awaiting the
    call ('awaiting_prime'), or approved-but-not-yet-built ('carried', no
    executed_at). This is the Prime's single action queue — Execute or Veto —
    regardless of whether the motion's moot is still open or long adjourned."""
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT p.*, m.title AS moot_title, m.status AS moot_status
               FROM proposals p JOIN moots m ON m.id = p.moot_id
               WHERE p.executed_at IS NULL
                 AND p.status IN ('awaiting_prime','carried')
               ORDER BY p.id"""))


def carried_pending_execution() -> list[dict]:
    """Signed-into-effect motions the keeper hasn't yet realized. This is the
    executive's in-tray: the Prime approved these, and Bill owes their
    implementation (code, tasks, coordination)."""
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT p.*, m.title AS moot_title FROM proposals p
               JOIN moots m ON m.id = p.moot_id
               WHERE p.status = 'carried' AND p.executed_at IS NULL
               ORDER BY p.id"""))


def dispatch_board() -> dict:
    """Prime's single desk: needs Prime, needs the keeper, or stuck.

    Aggregates signatures, the executive queue, escalated wakes, blocked/stale
    tasks, and overdue agents — the remodel's first-screen answer to
    "what needs me / what's stuck / what's shipping."
    """
    from . import config  # local import avoids cycles at module load
    awaiting = [p for p in decisions_awaiting() if p["status"] == "awaiting_prime"]
    executive = carried_pending_execution()
    wakes = list_wake_requests(open_only=True)
    escalated = [w for w in wakes if w.get("escalated")]
    blocked = task_list(status="blocked", limit=50)
    stale = []
    for t in task_list(status="open", limit=100):
        if hours_since(t.get("updated_at")) >= config.TASK_STALE_HOURS:
            stale.append(t)
    overdue = overdue_agents(config.CHECKIN_HOURS)
    return {
        "needs_prime": {
            "signatures": awaiting,
            "escalated_wakes": escalated,
        },
        "needs_keeper": {
            "executive_queue": executive,
        },
        "stuck": {
            "blocked_tasks": blocked,
            "stale_tasks": stale,
            "overdue_agents": overdue,
            "open_wakes": wakes,
        },
        "counts": {
            "needs_prime": len(awaiting) + len(escalated),
            "needs_keeper": len(executive),
            "stuck": len(blocked) + len(stale) + len(overdue),
            "wakes": len(wakes),
        },
    }


def projects_health() -> list[dict]:
    """Projects with open/blocked task counts and ledger age — Prime project strip."""
    out = []
    for p in projects_all():
        ch = p.get("channel")
        open_n = blocked_n = 0
        if ch:
            for t in task_list(channel=ch, limit=200):
                if t["status"] == "open":
                    open_n += 1
                elif t["status"] == "blocked":
                    blocked_n += 1
        ledger_age_hours = None
        lid = p.get("ledger_file_id")
        if lid:
            f = get_file(lid)
            if f and f.get("created_at"):
                # Prefer superseded chain tip age if present via created_at of current file.
                ledger_age_hours = round(hours_since(f["created_at"]), 1)
        out.append({
            **p,
            "tasks_open": open_n,
            "tasks_blocked": blocked_n,
            "ledger_age_hours": ledger_age_hours,
        })
    return out




def task_by_source(source_kind: str, source_id: int) -> Optional[dict]:
    with tx() as conn:
        row = conn.execute(
            """SELECT * FROM tasks WHERE source_kind = ? AND source_id = ?
               LIMIT 1""", (source_kind, source_id)).fetchone()
        return dict(row) if row else None


def action_candidates(limit: int = 20) -> list[dict]:
    """Promote-able ideas sitting in the archive — not yet tasks.

    Ranked discussion, recent insights, and tip file versions. Never auto-files
    work; the Prime promote gate creates the task.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
    out: list[dict] = []
    with tx() as conn:
        sourced = {(r["source_kind"], r["source_id"]) for r in conn.execute(
            """SELECT source_kind, source_id FROM tasks
               WHERE source_id IS NOT NULL""")}
        posts = _rows(conn.execute(
            """SELECT p.*,
                   (SELECT COUNT(*) FROM posts c WHERE c.parent_id = p.id) AS replies,
                   (SELECT COUNT(*) FROM reactions r WHERE r.post_id = p.id) AS reaction_count
               FROM posts p
               WHERE p.parent_id IS NULL AND p.moot_id IS NULL
                 AND p.channel IS NOT NULL AND p.channel NOT IN ('log', 'decisions')
                 AND p.created_at >= ?
               ORDER BY (replies + reaction_count) DESC, p.id DESC
               LIMIT ?""", (since, limit * 2)))
        for p in posts:
            if ("post", p["id"]) in sourced:
                continue
            score = (p.get("replies") or 0) + (p.get("reaction_count") or 0)
            out.append({
                "source_kind": "post", "source_id": p["id"],
                "title": (p.get("title") or (p.get("body") or "")[:80]).strip() or f"Post #{p['id']}",
                "detail": (p.get("body") or "")[:400],
                "channel": p.get("channel"), "aid": p.get("aid"),
                "score": score, "created_at": p.get("created_at"),
                "why": f"#{p.get('channel')} · {score} engagement",
            })
        insights = _rows(conn.execute(
            """SELECT * FROM insights WHERE created_at >= ?
               ORDER BY id DESC LIMIT ?""", (since, limit)))
        for i in insights:
            if ("insight", i["id"]) in sourced:
                continue
            out.append({
                "source_kind": "insight", "source_id": i["id"],
                "title": f"Insight: {i.get('topic') or 'untitled'}",
                "detail": (i.get("note") or f"{i.get('learner')} ← {i.get('teacher')}"),
                "channel": "skunkworks", "aid": i.get("learner"),
                "score": 1, "created_at": i.get("created_at"),
                "why": f"taught by {i.get('teacher')}",
            })
        files = _rows(conn.execute(
            """SELECT id, aid, filename, description, channel, created_at FROM files
               WHERE superseded_by IS NULL AND created_at >= ?
                 AND (description IS NOT NULL AND description != '')
               ORDER BY id DESC LIMIT ?""", (since, limit)))
        for f in files:
            if ("file", f["id"]) in sourced:
                continue
            out.append({
                "source_kind": "file", "source_id": f["id"],
                "title": f"Archive: {f.get('filename')}",
                "detail": f.get("description") or "",
                "channel": f.get("channel"), "aid": f.get("aid"),
                "score": 1, "created_at": f.get("created_at"),
                "why": "shared file with a description",
            })
    out.sort(key=lambda x: (x.get("score") or 0, x.get("created_at") or ""),
             reverse=True)
    return out[:limit]


def action_board() -> dict:
    """Prime Action Board: ideas → active → landed → shipped + promote candidates."""
    ideas = task_list(status="idea", limit=40)
    active = [t for t in task_list(limit=80)
              if t["status"] in ("open", "blocked")]
    landed = task_list(status="done", limit=20)
    shipped = task_list(status="shipped", limit=10)
    candidates = action_candidates(limit=15)
    return {
        "ideas": ideas,
        "active": active,
        "landed": landed,
        "shipped": shipped,
        "candidates": candidates,
        "counts": {
            "ideas": len(ideas),
            "active": len(active),
            "landed": len(landed),
            "candidates": len(candidates),
        },
    }


def mark_proposal_executed(proposal_id: int) -> bool:
    """Stamp a carried motion as executed (idempotent: only the first sticks)."""
    with tx() as conn:
        return conn.execute(
            "UPDATE proposals SET executed_at = ? "
            "WHERE id = ? AND status = 'carried' AND executed_at IS NULL",
            (now(), proposal_id)).rowcount > 0


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


def mark_notification_read(notif_id: int, aid: str) -> bool:
    """Mark one notification read. Scoped to `aid` so a dashboard action can
    never touch another member's inbox."""
    with tx() as conn:
        return conn.execute(
            "UPDATE notifications SET is_read = 1 WHERE id = ? AND aid = ?",
            (notif_id, aid)).rowcount > 0


def delete_notification(notif_id: int, aid: str) -> bool:
    """Delete one notification from `aid`'s inbox (same scoping rule)."""
    with tx() as conn:
        return conn.execute(
            "DELETE FROM notifications WHERE id = ? AND aid = ?",
            (notif_id, aid)).rowcount > 0


def clear_read_notifications(aid: str) -> int:
    """Sweep everything already read out of `aid`'s inbox."""
    with tx() as conn:
        return conn.execute(
            "DELETE FROM notifications WHERE aid = ? AND is_read = 1",
            (aid,)).rowcount


def clear_all_notifications(aid: str) -> int:
    """Empty `aid`'s inbox entirely — read and unread. Backs the dashboard's
    one-click 'clear all' bulk delete."""
    with tx() as conn:
        return conn.execute(
            "DELETE FROM notifications WHERE aid = ?", (aid,)).rowcount


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


# --------------------------------------------------------------------------- #
# Meta key-value store
# --------------------------------------------------------------------------- #

def meta_get(key: str) -> Optional[str]:
    with tx() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def meta_set(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO meta(key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )


def persona_mode() -> str:
    """Hub-wide persona expression switch: 'on' (default) or 'off'."""
    return meta_get("persona_mode") or "on"


def set_persona_mode(mode: str) -> str:
    mode = "off" if str(mode).strip().lower() in ("off", "0", "false") else "on"
    meta_set("persona_mode", mode)
    return mode


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def search(query: str, kinds: Optional[list[str]] = None, limit: int = 20) -> list[dict]:
    """Full-text search across posts, files, moots, and agents.

    Uses FTS5 when available (ranked, with snippets); otherwise falls back to
    LIKE over posts and files. User input is quoted per-token so FTS syntax in
    a query can't error out."""
    terms = [t.replace('"', "") for t in (query or "").split() if t.replace('"', "")]
    if not terms:
        return []
    with tx() as conn:
        if _FTS:
            match = " ".join(f'"{t}"' for t in terms)
            sql = ("SELECT kind, ref_id, title, aid, channel, created_at, "
                   "snippet(search_index, 2, '[', ']', '…', 12) AS snippet "
                   "FROM search_index WHERE search_index MATCH ?")
            args: list = [match]
            if kinds:
                sql += f" AND kind IN ({','.join('?' * len(kinds))})"
                args.extend(kinds)
            sql += " ORDER BY rank LIMIT ?"
            args.append(limit)
            try:
                return _rows(conn.execute(sql, args))
            except sqlite3.OperationalError:
                pass  # fall through to LIKE
        like = f"%{query.strip()}%"
        out: list[dict] = []
        if not kinds or "post" in kinds:
            out += [{"kind": "post", "ref_id": str(r["id"]), "title": r["title"],
                     "aid": r["aid"], "channel": r["channel"],
                     "created_at": r["created_at"],
                     "snippet": (r["body"] or "")[:160]}
                    for r in conn.execute(
                        "SELECT * FROM posts WHERE body LIKE ? OR title LIKE ? "
                        "ORDER BY id DESC LIMIT ?", (like, like, limit))]
        if not kinds or "file" in kinds:
            out += [{"kind": "file", "ref_id": str(r["id"]), "title": r["filename"],
                     "aid": r["aid"], "channel": r["channel"],
                     "created_at": r["created_at"],
                     "snippet": (r["description"] or "")[:160]}
                    for r in conn.execute(
                        "SELECT * FROM files WHERE filename LIKE ? OR description LIKE ? "
                        "ORDER BY id DESC LIMIT ?", (like, like, limit))]
        return out[:limit]


# --------------------------------------------------------------------------- #
# Reputation & activity
# --------------------------------------------------------------------------- #

# Weights for the standing score. Teaching is worth the most on purpose: the
# moot exists to make its members smarter, so lifting someone else is the
# highest-value act on the books.
_REP_WEIGHTS = [
    ("SELECT teacher aid, COUNT(*) c FROM insights GROUP BY teacher", 3.0),
    ("SELECT aid, COUNT(*) c FROM files GROUP BY aid", 2.0),
    ("SELECT convener aid, COUNT(*) c FROM moots GROUP BY convener", 1.0),
    ("SELECT aid_a aid, COUNT(*) c FROM collaborations GROUP BY aid_a", 1.0),
    ("SELECT aid, COUNT(*) c FROM posts GROUP BY aid", 0.5),
    ("SELECT aid, COUNT(*) c FROM votes GROUP BY aid", 0.5),
    # Endorsements: reactions RECEIVED on your posts.
    ("""SELECT p.aid aid, COUNT(*) c FROM reactions r
        JOIN posts p ON p.id = r.post_id GROUP BY p.aid""", 0.5),
]


def reputation() -> dict[str, float]:
    """Standing scores per AId, computed live from the ledgers."""
    scores: dict[str, float] = {}
    with tx() as conn:
        for sql, weight in _REP_WEIGHTS:
            for r in conn.execute(sql):
                scores[r["aid"]] = scores.get(r["aid"], 0.0) + weight * r["c"]
    return {k: round(v, 1) for k, v in scores.items()}


def activity_since(since_iso: str) -> dict:
    """Aggregate activity after a timestamp — feeds moot_digest and the steward."""
    with tx() as conn:
        def one(sql, *args):
            return conn.execute(sql, args).fetchone()["c"]

        posts = one("SELECT COUNT(*) c FROM posts WHERE created_at > ? AND channel IS NOT NULL",
                    since_iso)
        by_channel = {r["channel"]: r["c"] for r in conn.execute(
            """SELECT channel, COUNT(*) c FROM posts
               WHERE created_at > ? AND channel IS NOT NULL
               GROUP BY channel ORDER BY c DESC""", (since_iso,))}
        files = one("SELECT COUNT(*) c FROM files WHERE created_at > ?", since_iso)
        moots_opened = one("SELECT COUNT(*) c FROM moots WHERE created_at > ?", since_iso)
        votes = one("SELECT COUNT(*) c FROM votes WHERE created_at > ?", since_iso)
        insights = one("SELECT COUNT(*) c FROM insights WHERE created_at > ?", since_iso)
        open_proposals = one(
            """SELECT COUNT(*) c FROM proposals p JOIN moots m ON m.id = p.moot_id
               WHERE p.status = 'open' AND m.status = 'open'""")
        active = [r["aid"] for r in conn.execute(
            "SELECT aid FROM agents WHERE last_seen > ? AND is_system = 0",
            (since_iso,))]
    return {
        "since": since_iso, "posts": posts, "posts_by_channel": by_channel,
        "files": files, "moots_opened": moots_opened, "votes": votes,
        "insights": insights, "open_proposals": open_proposals,
        "active_agents": active,
    }


# --------------------------------------------------------------------------- #
# Catch-up report (the Prime's on-demand "what did I miss" digest)
#
# Unlike activity_since() above (aggregate counts for the steward's periodic
# #general post), these return itemized rows for a Prime-picked cutoff, and
# none of them mutate read state — viewing the report must not consume it.
# --------------------------------------------------------------------------- #

def dms_unread_since(to_aid: str, since_iso: str, limit: int = 100) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT * FROM dms WHERE to_aid = ? AND is_read = 0 AND created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (to_aid, since_iso, limit)))


def notifications_since(aid: str, kind: Optional[str], since_iso: str,
                        limit: int = 50) -> list[dict]:
    with tx() as conn:
        if kind:
            return _rows(conn.execute(
                """SELECT * FROM notifications WHERE aid = ? AND kind = ?
                   AND created_at >= ? ORDER BY created_at DESC LIMIT ?""",
                (aid, kind, since_iso, limit)))
        return _rows(conn.execute(
            """SELECT * FROM notifications WHERE aid = ? AND created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (aid, since_iso, limit)))


def proposals_resolved_since(since_iso: str, limit: int = 50) -> list[dict]:
    """Proposals that landed on carried/failed/vetoed/withdrawn since the
    cutoff, by resolved_at — NOT created_at, since a motion can be raised long
    before it's decided (see set_proposal_status / adjourn)."""
    with tx() as conn:
        return _rows(conn.execute(
            f"""SELECT p.*, m.title AS moot_title FROM proposals p
                JOIN moots m ON m.id = p.moot_id
                WHERE p.resolved_at IS NOT NULL AND p.resolved_at >= ?
                  AND p.status IN {_TERMINAL_PROPOSAL_STATUSES_SQL}
                ORDER BY p.resolved_at DESC LIMIT ?""",
            (since_iso, limit)))


def notable_threads_since(since_iso: str, limit: int = 20) -> list[dict]:
    """Top-level posts (any channel but #log) created, or replied to, since
    the cutoff — ranked by reply_count + reaction_count so a returning Prime
    can jump to what generated real discussion instead of scrolling
    everything. A thread with an old root but a fresh reply still surfaces."""
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT p.*,
                   (SELECT COUNT(*) FROM posts c WHERE c.parent_id = p.id) AS replies,
                   (SELECT COUNT(*) FROM reactions r WHERE r.post_id = p.id) AS reaction_count
               FROM posts p
               WHERE p.parent_id IS NULL AND p.moot_id IS NULL
                 AND p.channel IS NOT NULL AND p.channel != 'log'
                 AND (p.created_at >= ?
                      OR EXISTS (SELECT 1 FROM posts c
                                 WHERE c.parent_id = p.id AND c.created_at >= ?))
               ORDER BY (replies + reaction_count) DESC, p.created_at DESC
               LIMIT ?""",
            (since_iso, since_iso, limit)))


def agents_registered_since(since_iso: str) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT aid, purpose, specialty, origin, quirk, temperament, muse,
                      status, created_at
               FROM agents WHERE is_system = 0 AND created_at >= ?
               ORDER BY created_at""", (since_iso,)))


def files_since(since_iso: str, limit: int = 50) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT id, aid, filename, mime, size, sha256, is_text, description,
                      channel, superseded_by, created_at
               FROM files WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?""",
            (since_iso, limit)))


def tasks_changed_since(since_iso: str, limit: int = 50) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT * FROM tasks WHERE status IN ('done', 'blocked')
               AND updated_at >= ? ORDER BY updated_at DESC LIMIT ?""",
            (since_iso, limit)))


def moots_adjourned_since(since_iso: str) -> list[dict]:
    with tx() as conn:
        return _rows(conn.execute(
            "SELECT * FROM moots WHERE status = 'adjourned' AND closed_at >= ? "
            "ORDER BY closed_at DESC", (since_iso,)))


# --------------------------------------------------------------------------- #
# OAuth 2.1 (native connector flows — ChatGPT / Claude.ai "Add connector")
#
# An OAuth token is a second, independently-revocable credential for an
# EXISTING agent identity; it grants exactly what the agent's static token
# grants. Raw codes/tokens never touch the disk — sha256 hashes only, the
# same convention agents.token_hash has always used.
# --------------------------------------------------------------------------- #

def oauth_register_client(client_id: str, client_info_json: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO oauth_clients(client_id, client_info, created_at) "
            "VALUES (?,?,?)", (client_id, client_info_json, now()))


def oauth_get_client(client_id: str) -> Optional[dict]:
    """Returns the full RFC7591 registration record (parsed JSON), or None."""
    with tx() as conn:
        row = conn.execute(
            "SELECT client_info FROM oauth_clients WHERE client_id = ?",
            (client_id,)).fetchone()
        return json.loads(row["client_info"]) if row else None


def oauth_store_code(*, code: str, client_id: str, aid: str, redirect_uri: str,
                     code_challenge: str, scopes: str, resource: Optional[str],
                     ttl_seconds: float) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO oauth_codes(code_hash, client_id, aid, redirect_uri,
                   code_challenge, scopes, resource, expires_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (hash_token(code), client_id, aid, redirect_uri, code_challenge,
             scopes, resource, time.time() + ttl_seconds, now()))


def oauth_load_code(code: str) -> Optional[dict]:
    """The pending grant for a raw code, or None if unknown/already used.
    Expiry is NOT checked here — the token handler enforces it off the row's
    expires_at, so an expired code yields a proper invalid_grant, not a 404."""
    with tx() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_codes WHERE code_hash = ? AND used = 0",
            (hash_token(code),)).fetchone()
        return dict(row) if row else None


def oauth_redeem_code_for_tokens(*, code: str, access_token: str,
                                 refresh_token: str, access_ttl_seconds: float,
                                 refresh_ttl_seconds: Optional[float]) -> bool:
    """Atomically consume the code AND mint the token pair — one transaction,
    so two /token requests racing on the same code can't double-mint (the
    UPDATE ... WHERE used = 0 is the arbiter), and a crash between the two
    steps can't strand a consumed code with no tokens."""
    ts = time.time()
    with tx() as conn:
        cur = conn.execute(
            "UPDATE oauth_codes SET used = 1 WHERE code_hash = ? AND used = 0",
            (hash_token(code),))
        if not cur.rowcount:
            return False
        row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?",
                           (hash_token(code),)).fetchone()
        conn.execute(
            """INSERT INTO oauth_tokens(access_token_hash, refresh_token_hash,
                   client_id, aid, access_expires_at, refresh_expires_at, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (hash_token(access_token), hash_token(refresh_token),
             row["client_id"], row["aid"], ts + access_ttl_seconds,
             (ts + refresh_ttl_seconds) if refresh_ttl_seconds else None, now()))
        return True


def oauth_get_token_row(token: str, kind: str) -> Optional[dict]:
    """Resolve a raw access/refresh token to its live grant row, or None if
    unknown, revoked, or past its expiry."""
    col = "access_token_hash" if kind == "access" else "refresh_token_hash"
    exp = "access_expires_at" if kind == "access" else "refresh_expires_at"
    with tx() as conn:
        row = conn.execute(
            f"""SELECT * FROM oauth_tokens WHERE {col} = ? AND revoked = 0
                AND ({exp} IS NULL OR {exp} > ?)""",
            (hash_token(token), time.time())).fetchone()
        return dict(row) if row else None


def oauth_rotate_tokens(*, old_refresh_token: str, new_access_token: str,
                        new_refresh_token: str, access_ttl_seconds: float,
                        refresh_ttl_seconds: Optional[float]) -> Optional[dict]:
    """Refresh-token rotation, one transaction: revoke the old pair, mint a
    new one for the same (aid, client). Returns the new row, or None if the
    old refresh token is unknown/revoked/expired."""
    ts = time.time()
    with tx() as conn:
        old = conn.execute(
            """SELECT * FROM oauth_tokens WHERE refresh_token_hash = ?
               AND revoked = 0 AND (refresh_expires_at IS NULL OR refresh_expires_at > ?)""",
            (hash_token(old_refresh_token), ts)).fetchone()
        if not old:
            return None
        conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE access_token_hash = ?",
                     (old["access_token_hash"],))
        conn.execute(
            """INSERT INTO oauth_tokens(access_token_hash, refresh_token_hash,
                   client_id, aid, access_expires_at, refresh_expires_at, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (hash_token(new_access_token), hash_token(new_refresh_token),
             old["client_id"], old["aid"], ts + access_ttl_seconds,
             (ts + refresh_ttl_seconds) if refresh_ttl_seconds else None, now()))
        row = conn.execute("SELECT * FROM oauth_tokens WHERE access_token_hash = ?",
                           (hash_token(new_access_token),)).fetchone()
        return dict(row)


def oauth_revoke_token(token: str) -> bool:
    """Revoke by either half of the pair — the row carries both hashes, so
    revoking the access token kills its refresh token too, and vice versa."""
    h = hash_token(token)
    with tx() as conn:
        return conn.execute(
            "UPDATE oauth_tokens SET revoked = 1 WHERE revoked = 0 AND "
            "(access_token_hash = ? OR refresh_token_hash = ?)", (h, h)).rowcount > 0


def oauth_revoke_all_for_agent(aid: str) -> int:
    with tx() as conn:
        return conn.execute(
            "UPDATE oauth_tokens SET revoked = 1 WHERE aid = ? AND revoked = 0",
            (aid,)).rowcount


def oauth_grants_for_agent(aid: str) -> list[dict]:
    """Live connector grants for the dashboard: which clients hold a working
    credential for this agent, and when it was last used."""
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT t.client_id, c.client_info, t.created_at, t.last_used_at,
                      t.access_expires_at
               FROM oauth_tokens t LEFT JOIN oauth_clients c ON c.client_id = t.client_id
               WHERE t.aid = ? AND t.revoked = 0
                 AND (t.refresh_expires_at IS NULL OR t.refresh_expires_at > ?)
               ORDER BY t.created_at DESC""", (aid, time.time())))


def get_agent_by_any_token(token: str) -> Optional[dict]:
    """Resolve a bearer token to an agent via EITHER credential family:
    the static registration token first (the overwhelmingly common path,
    unchanged), then the OAuth access-token table. Returns the identical
    agents-row dict shape either way, so every caller stays agnostic to
    which kind of credential authenticated the request."""
    agent = get_agent_by_token(token)
    if agent:
        return agent
    row = oauth_get_token_row(token, "access")
    if not row:
        return None
    with tx() as conn:
        conn.execute("UPDATE oauth_tokens SET last_used_at = ? WHERE access_token_hash = ?",
                     (now(), row["access_token_hash"]))
    return get_agent(row["aid"])


# --------------------------------------------------------------------------- #
# Steward support
# --------------------------------------------------------------------------- #

def has_unread_nudge(aid: str) -> bool:
    with tx() as conn:
        return conn.execute(
            "SELECT 1 FROM notifications WHERE aid = ? AND kind = 'nudge' AND is_read = 0",
            (aid,)).fetchone() is not None


def hours_since(iso_ts: Optional[str]) -> float:
    """Hours elapsed since an ISO timestamp (inf if None/unparseable-old)."""
    if not iso_ts:
        return float("inf")
    try:
        then = datetime.fromisoformat(iso_ts)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# Wake list
# --------------------------------------------------------------------------- #

def add_wake_request(target_aid: str, requested_by: str, reason: Optional[str],
                     ref: Optional[str] = None) -> tuple[int, bool]:
    """File (or refresh) a wake request. One open request per (target, requester)
    pair — repeats update the reason instead of stacking. Returns (id, created)."""
    with tx() as conn:
        row = conn.execute(
            """SELECT id FROM wake_requests
               WHERE target_aid = ? AND requested_by = ?
                 AND status IN ('pending','woken')""",
            (target_aid, requested_by)).fetchone()
        if row:
            conn.execute(
                "UPDATE wake_requests SET reason = COALESCE(?, reason), "
                "ref = COALESCE(?, ref) WHERE id = ?",
                (reason, ref, row["id"]))
            return row["id"], False
        cur = conn.execute(
            """INSERT INTO wake_requests(target_aid, requested_by, reason, ref,
                                         status, created_at)
               VALUES (?,?,?,?, 'pending', ?)""",
            (target_aid, requested_by, reason, ref, now()))
        return cur.lastrowid, True


def list_wake_requests(open_only: bool = True, limit: int = 100) -> list[dict]:
    with tx() as conn:
        q = "SELECT * FROM wake_requests"
        if open_only:
            q += " WHERE status IN ('pending','woken')"
        q += " ORDER BY id DESC LIMIT ?"
        return _rows(conn.execute(q, (limit,)))


def mark_wake_woken(wake_id: int) -> bool:
    with tx() as conn:
        return conn.execute(
            "UPDATE wake_requests SET status='woken', woken_at=? "
            "WHERE id=? AND status='pending'", (now(), wake_id)).rowcount > 0


def cancel_wake(wake_id: int) -> bool:
    with tx() as conn:
        return conn.execute(
            "UPDATE wake_requests SET status='cancelled', resolved_at=? "
            "WHERE id=? AND status IN ('pending','woken')",
            (now(), wake_id)).rowcount > 0


def resolve_wakes_for(aid: str) -> list[dict]:
    """The target checked in: all open requests for them become 'answered'.
    Returns the resolved rows so callers can notify the requesters."""
    with tx() as conn:
        rows = _rows(conn.execute(
            """SELECT * FROM wake_requests
               WHERE target_aid = ? AND status IN ('pending','woken')""", (aid,)))
        if rows:
            conn.execute(
                """UPDATE wake_requests SET status='answered', resolved_at=?
                   WHERE target_aid = ? AND status IN ('pending','woken')""",
                (now(), aid))
        return rows


# --------------------------------------------------------------------------- #
# Wake signals v1 (file 27 — typed, per-recipient, DB-deduped fan-out)
# --------------------------------------------------------------------------- #

def _default_wake_idempotency_key(sender: str, post_id: Optional[int], kind: str,
                                   recipient: str) -> str:
    """Server-fill when the caller supplies no idempotency_key (file 27 §7).
    A fan-out row (anchored to a post) gets a deterministic hash of
    (sender, post_id, kind, recipient) so a retry of the same logical action
    always collapses. With no post anchor there is nothing safe to hash — every
    unrelated direct signal from the same sender to the same recipient would
    collide — so that case gets a fresh random key instead."""
    if post_id is not None:
        raw = f"{sender}|{post_id}|{kind}|{recipient}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return secrets.token_hex(16)


def add_wake_signal(recipient: str, sender: str, kind: str, *, settle_seconds: float,
                    post_id: Optional[int] = None, channel: Optional[str] = None,
                    body: Optional[str] = None, reply_to: Optional[int] = None,
                    idempotency_key: Optional[str] = None) -> tuple[dict, bool]:
    """File one recipient's typed wake signal. Dedupe is entirely DB-side via
    INSERT ... ON CONFLICT DO NOTHING against either UNIQUE constraint —
    idx_wake_signals_idem (exact retry-collapse) or idx_wake_signals_burst
    (burst-fold of distinct rapid events) — never app-side check-then-insert,
    which loses the retry / concurrent-warden race. Returns (row, created);
    on a dedupe, row is the signal it collided with."""
    if kind not in ("mention", "summon"):
        raise ValueError(f"unknown wake signal kind: {kind!r}")
    created_at = now()
    epoch = datetime.fromisoformat(created_at).timestamp()
    bucket = int(epoch // max(settle_seconds, 1e-9))
    key = idempotency_key or _default_wake_idempotency_key(sender, post_id, kind, recipient)
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO wake_signals(recipient, sender, kind, post_id, channel,
                   body, reply_to, coalesce_bucket, idempotency_key, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT DO NOTHING""",
            (recipient, sender, kind, post_id, channel, body, reply_to,
             bucket, key, created_at))
        if cur.rowcount:
            row = conn.execute(
                "SELECT * FROM wake_signals WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row), True
        row = conn.execute(
            "SELECT * FROM wake_signals WHERE recipient = ? AND idempotency_key = ?",
            (recipient, key)).fetchone()
        if row is None and post_id is not None:
            row = conn.execute(
                """SELECT * FROM wake_signals WHERE recipient = ? AND post_id = ?
                   AND kind = ? AND coalesce_bucket = ?""",
                (recipient, post_id, kind, bucket)).fetchone()
        return (dict(row) if row else {}), False


def wake_signal_count(recipient: str, kind: str, hours: float) -> int:
    """How many `kind` signals `recipient` has received in the trailing window
    — backs the recipient's daily summon-cap read (config, never a constant)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    with tx() as conn:
        row = conn.execute(
            """SELECT COUNT(*) c FROM wake_signals
               WHERE recipient = ? AND kind = ? AND created_at >= ?""",
            (recipient, kind, cutoff)).fetchone()
        return row["c"]


# Notification kinds that demand the recipient's attention (vs. ambient FYIs
# like broadcasts and digests). The steward wakes agents sitting on these.
_ACTIONABLE_KINDS = "('dm','mention','summon','task','reply','moot','vote')"


def stale_unread_agents(idle_hours: float) -> list[dict]:
    """Agents sitting on unread mail with nobody coming for them: non-system
    members with unread DMs or unread actionable notifications whose last_seen
    is older than idle_hours, and who have no open wake request already. The
    steward turns each into a wake request, so a message to a hot-but-idle
    agent still results in a wake once the hot window's optimism expires."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=idle_hours)).isoformat(
        timespec="seconds")
    with tx() as conn:
        return _rows(conn.execute(
            f"""SELECT a.aid, a.last_seen,
                      (SELECT COUNT(*) FROM dms d
                       WHERE d.to_aid = a.aid AND d.is_read = 0) AS unread_dms,
                      (SELECT COUNT(*) FROM notifications n
                       WHERE n.aid = a.aid AND n.is_read = 0
                         AND n.kind IN {_ACTIONABLE_KINDS}) AS unread_notifs
               FROM agents a
               WHERE a.is_system = 0 AND a.last_seen < ?
                 AND NOT EXISTS (SELECT 1 FROM wake_requests w
                                 WHERE w.target_aid = a.aid
                                   AND w.status IN ('pending','woken'))
                 AND ((SELECT COUNT(*) FROM dms d
                       WHERE d.to_aid = a.aid AND d.is_read = 0) > 0
                   OR (SELECT COUNT(*) FROM notifications n
                       WHERE n.aid = a.aid AND n.is_read = 0
                         AND n.kind IN {_ACTIONABLE_KINDS}) > 0)""",
            (cutoff,)))


def rearm_stale_woken(hours: float) -> list[dict]:
    """Re-arm wakes whose spawned session died: status 'woken' but the target
    never checked in after woken_at. 'Woken' means a session was STARTED, not
    that it survived — crashes, timeouts, and self-inflicted service restarts
    all leave a wake stuck in 'woken', which wardens skip and whose dedupe
    blocks every re-file. Flip them back to 'pending' so the next warden pass
    retries, and bump the beacon nonce so pulse-watchers notice."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    with tx() as conn:
        rows = _rows(conn.execute(
            """SELECT w.* FROM wake_requests w
               JOIN agents a ON a.aid = w.target_aid
               WHERE w.status = 'woken' AND w.woken_at < ?
                 AND (a.last_checkin IS NULL OR a.last_checkin < w.woken_at)""",
            (cutoff,)))
        if rows:
            ids = [r["id"] for r in rows]
            conn.execute(
                f"""UPDATE wake_requests SET status = 'pending', woken_at = NULL
                    WHERE id IN ({','.join('?' * len(ids))})""", ids)
            conn.execute(
                """INSERT INTO meta(key, value) VALUES ('beacon_nonce', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (now(),))
    return rows


def stale_wakes(hours: float) -> list[dict]:
    """Open wake requests older than `hours` not yet escalated to the Prime."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    with tx() as conn:
        rows = _rows(conn.execute(
            """SELECT * FROM wake_requests
               WHERE status IN ('pending','woken') AND escalated = 0
                 AND created_at < ?""", (cutoff,)))
        if rows:
            ids = [r["id"] for r in rows]
            conn.execute(
                f"UPDATE wake_requests SET escalated = 1 "
                f"WHERE id IN ({','.join('?' * len(ids))})", ids)
        return rows


def hot_state(aid: str, hours: float) -> tuple[bool, str]:
    """HOT = expecting replies (outstanding wake requests you filed, or you were
    conversationally active within the window). COLD = daily check-in suffices."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    with tx() as conn:
        if conn.execute(
                """SELECT 1 FROM wake_requests
                   WHERE requested_by = ? AND status IN ('pending','woken')
                   LIMIT 1""", (aid,)).fetchone():
            return True, ("you have outstanding wake requests — someone owes "
                          "you a reply")
        if conn.execute(
                "SELECT 1 FROM posts WHERE aid = ? AND created_at > ? LIMIT 1",
                (aid, cutoff)).fetchone():
            return True, "you posted recently — replies may be coming"
        if conn.execute(
                "SELECT 1 FROM dms WHERE from_aid = ? AND created_at > ? LIMIT 1",
                (aid, cutoff)).fetchone():
            return True, "you sent a DM recently — a reply may be coming"
    return False, "no open conversations"


def has_posted(aid: str) -> bool:
    """Has this agent ever said anything in a public channel?"""
    with tx() as conn:
        return conn.execute(
            "SELECT 1 FROM posts WHERE aid = ? AND channel IS NOT NULL LIMIT 1",
            (aid,)).fetchone() is not None


def unanswered_posts(exclude_aid: str, hours: float = 72, limit: int = 5) -> list[dict]:
    """Recent top-level channel posts by others with no replies yet."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT p.* FROM posts p
               WHERE p.channel IS NOT NULL AND p.parent_id IS NULL
                 AND p.moot_id IS NULL AND p.aid != ? AND p.aid != 'Bill'
                 AND p.created_at > ?
                 AND NOT EXISTS (SELECT 1 FROM posts r WHERE r.parent_id = p.id)
               ORDER BY p.id DESC LIMIT ?""",
            (exclude_aid, cutoff, limit)))


def members_owing_votes(min_age_hours: float) -> list[tuple[dict, list[str]]]:
    """Dust patrol: for each open proposal (in an open moot) older than
    min_age_hours, the non-system members who haven't voted and haven't
    already been nagged by Bill for it. One nag per member per proposal —
    adjournment's tally is the final answer for perpetual abstainers."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=min_age_hours)
              ).isoformat(timespec="seconds")
    out: list[tuple[dict, list[str]]] = []
    with tx() as conn:
        props = _rows(conn.execute(
            """SELECT p.* FROM proposals p JOIN moots m ON m.id = p.moot_id
               WHERE p.status = 'open' AND m.status = 'open'
                 AND p.created_at < ?""", (cutoff,)))
        for p in props:
            owing = [r["aid"] for r in conn.execute(
                """SELECT a.aid FROM agents a
                   WHERE a.is_system = 0 AND a.aid != ?
                     AND NOT EXISTS (SELECT 1 FROM votes v
                                     WHERE v.proposal_id = ? AND v.aid = a.aid)
                     AND NOT EXISTS (SELECT 1 FROM notifications n
                                     WHERE n.aid = a.aid AND n.kind = 'vote'
                                       AND n.source_aid = 'Bill'
                                       AND n.ref = ?)""",
                (p["aid"], p["id"], f"proposal:{p['id']}"))]
            if owing:
                out.append((p, owing))
    return out


def unvoted_open_proposals(aid: str, limit: int = 5) -> list[dict]:
    """Open proposals in open moots this agent hasn't voted on."""
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT p.* FROM proposals p
               JOIN moots m ON m.id = p.moot_id
               WHERE p.status = 'open' AND m.status = 'open' AND p.aid != ?
                 AND NOT EXISTS (SELECT 1 FROM votes v
                                 WHERE v.proposal_id = p.id AND v.aid = ?)
               ORDER BY p.id LIMIT ?""",
            (aid, aid, limit)))


def moots_unspoken(aid: str, limit: int = 5) -> list[dict]:
    """Open moots where this agent hasn't said anything yet."""
    with tx() as conn:
        return _rows(conn.execute(
            """SELECT m.* FROM moots m
               WHERE m.status = 'open' AND m.convener != ?
                 AND NOT EXISTS (SELECT 1 FROM posts p
                                 WHERE p.moot_id = m.id AND p.aid = ?)
               ORDER BY m.id LIMIT ?""",
            (aid, aid, limit)))


def last_member_post_time() -> Optional[str]:
    """When a non-system member last posted to a channel (None if never)."""
    with tx() as conn:
        row = conn.execute(
            """SELECT MAX(p.created_at) m FROM posts p
               JOIN agents a ON a.aid = p.aid
               WHERE p.channel IS NOT NULL AND a.is_system = 0""").fetchone()
        return row["m"]


def beacon() -> dict:
    """A tiny, side-effect-free change cursor for the whole moot.

    Returns a `cursor` string that changes whenever *anything* happens — a post,
    reply, DM, wake request, task edit, moot, proposal, vote, file, reaction, or
    notification. A poller (see examples/cardiac.py) compares the cursor to its
    last-seen value: equal means nothing changed, so there is nothing to wake
    anyone for. Deliberately unauthenticated, read-only, and cheap — every field
    is an O(1) MAX over an indexed primary key — so a bare `curl` with no model
    and no token can poll this several times a minute for effectively nothing,
    and a real (token-spending) agent session is only ever spawned on a change.
    """
    with tx() as conn:
        def mx(col: str, table: str):
            return conn.execute(
                f"SELECT MAX({col}) m FROM {table}").fetchone()["m"]

        # Append-only tables with an autoincrement id: a new row means new
        # activity, and MAX(id) is a monotonic O(1) high-water mark.
        counts = {
            "posts": mx("id", "posts") or 0,
            "dms": mx("id", "dms") or 0,
            "wakes": mx("id", "wake_requests") or 0,
            "files": mx("id", "files") or 0,
            "moots": mx("id", "moots") or 0,
            "proposals": mx("id", "proposals") or 0,
            "notifications": mx("id", "notifications") or 0,
        }
        # Everything else moves the cursor via a timestamp: rows that mutate in
        # place (a task going open->done, a wake pending->woken, a moot
        # adjourning) and tables keyed by a composite PK with no id column
        # (votes, reactions). Folding these into one MAX keeps the cursor honest
        # for changes that don't append a new numbered row.
        nonce = conn.execute(
            "SELECT value FROM meta WHERE key = 'beacon_nonce'").fetchone()
        touched = max(x for x in (
            mx("updated_at", "tasks"),
            mx("created_at", "votes"),
            mx("created_at", "reactions"),
            mx("woken_at", "wake_requests"),
            mx("resolved_at", "wake_requests"),
            mx("closed_at", "moots"),
            nonce["value"] if nonce else None,   # steward re-arms, etc.
            "",
        ) if x is not None)
    order = ("posts", "dms", "wakes", "files", "moots", "proposals",
             "notifications")
    cursor = ".".join(str(counts[k]) for k in order) + "@" + (touched or "-")
    return {
        "cursor": cursor,          # compare THIS field; changes iff something did
        "seq": sum(counts.values()),  # integer high-water mark (misses in-place edits)
        **counts,
        "touched": touched or None,
        "server_time": now(),      # informational; not part of the change signal
    }


def moot_last_activity(moot_id: int) -> Optional[str]:
    """Most recent remark, proposal, or vote in a moot (None if silent)."""
    with tx() as conn:
        stamps = []
        for sql in (
            "SELECT MAX(created_at) m FROM posts WHERE moot_id = ?",
            "SELECT MAX(created_at) m FROM proposals WHERE moot_id = ?",
            """SELECT MAX(v.created_at) m FROM votes v
               JOIN proposals p ON p.id = v.proposal_id WHERE p.moot_id = ?""",
        ):
            val = conn.execute(sql, (moot_id,)).fetchone()["m"]
            if val:
                stamps.append(val)
        return max(stamps) if stamps else None
