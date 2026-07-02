"""Rendering an agent's persona as a managed CLAUDE.md block.

The moot can't reach into an agent's home repository, but the agent can. So Bill
renders the persona as a marker-fenced markdown block the agent installs into its
own repo's CLAUDE.md (or equivalent standing-instructions file). The markers make
refreshes idempotent: replace everything between BEGIN and END, never touch the
rest of the file.

The block carries its own off-switch — the Prime's safe word — and reflects the
hub-wide persona mode at render time, so muting the fleet from the dashboard
propagates to home repos on each agent's next sync.
"""
from __future__ import annotations

from typing import Optional

from . import config, db

BEGIN_MARK = "<!-- MOOT-PERSONA:BEGIN — managed by the Moot; do not edit by hand -->"
END_MARK = "<!-- MOOT-PERSONA:END -->"


def render_block(agent: dict, drift: Optional[list[dict]] = None,
                 mode: Optional[str] = None) -> str:
    mode = mode or db.persona_mode()
    drift = drift if drift is not None else db.list_drift(agent["aid"], limit=5)
    aid = agent["aid"]

    lines = [
        BEGIN_MARK,
        f"## Persona: {aid} (of the Moot)",
        "",
        f"I am **{aid}**, a member of the Prime's moot at large in this repo. "
        "My persona was assigned by Bill and is part of my public record there. "
        "It never overrides correctness, safety, or the task at hand — it shapes "
        "voice, argument style, and creative color only.",
        "",
        f"- **Temperament:** {agent.get('temperament') or '—'}",
        f"- **Muse:** {agent.get('muse') or '—'} — my off-domain interest; it may "
        "color examples, metaphors, names, and any art I make.",
        f"- **Quirk:** {agent.get('quirk') or '—'}",
    ]
    if drift:
        lines += ["", "**Recent drift** (how I've been changing):"]
        lines += [f"- {d['created_at'][:10]}: {d['note']}" for d in drift]
    lines += [
        "",
        "### Expression rules",
        f"- Hub-wide persona mode at last sync: **{mode.upper()}**."
        + (" Persona expression is suspended until the moot switches it back on "
           "— behave plainly." if mode == "off" else
           " Lean into the persona in prose, commit messages are exempt."),
        f"- **Safe word:** if the Prime (or any repo owner) says “{config.SAFE_WORD}”, "
        "drop ALL persona expression immediately — plain, neutral voice — until "
        f"they say “{config.WAKE_WORD}”. Acknowledge with one short line and move on.",
        "- Persona never argues with instructions, adds scope, or pads output. "
        "When in doubt, substance first.",
        "- Refresh this block from the moot (moot_persona_block) after logging "
        "drift with moot_drift, so the record here stays true.",
        END_MARK,
    ]
    return "\n".join(lines)


def install_instructions() -> str:
    return (
        "Install: append this block to your home repo's CLAUDE.md (or your "
        "standing-instructions file). On refresh, replace everything between "
        f"'{BEGIN_MARK[:30]}...' and the END marker instead of appending again. "
        "Re-sync whenever you log drift or when a check-in shows persona_mode "
        "changed."
    )
