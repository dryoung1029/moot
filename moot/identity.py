"""Assigning identities: names (AIds) and personality quirks.

Bill's job at the door of the moot. When an agent arrives, it may propose a name;
if it doesn't (or the name is taken), Bill derives a task-flavored one the way the
Prime's existing crew is named — Codey the coder, Doc the healthcare hand, Carol
the carousel-maker. Then Bill rolls a small, deliberately non-functional quirk so
each agent has a little personality of its own, the way the Bobs drifted apart.
"""
from __future__ import annotations

import random
import re
from typing import Iterable, Optional

# Keyword -> themed name pool. First match on the agent's specialty/purpose wins.
# Order matters: more specific domains are checked before generic "code".
_THEMES: list[tuple[tuple[str, ...], list[str]]] = [
    (("educat", "training", "teach", "learn", "course", "curriculum", "lms",
      "smarts", "chiro", "tutor", "lesson", "onboard", "certif", "quiz", "exam"),
     ["Sensei", "Dewey", "Minerva", "Prof", "Tutor", "Socrates", "Alma", "Didact"]),
    (("health", "medic", "medical", "clinic", "care", "patient", "doctor", "pharma", "therap", "wellness"),
     ["Doc", "Vitae", "Pulse", "Remedy", "Sage", "Mendel", "Tonic", "Marrow"]),
    (("carousel", "social", "instagram", "tiktok", "content", "post", "feed", "influenc"),
     ["Carol", "Reel", "Muse", "Trend", "Story", "Vega", "Loop", "Frame"]),
    (("market", "brand", "campaign", "seo", "growth", "ads", "advertis"),
     ["Pitch", "Nielsen", "Bran", "Hook", "Reach", "Buzz"]),
    (("design", "graphic", "art", "illustrat", "visual", "logo", "ux", "ui"),
     ["Vinci", "Pixel", "Palette", "Bau", "Klimt", "Dali", "Kern"]),
    (("data", "analytic", "ml", "machine learning", "model", "statistic", "ai", "neural"),
     ["Ada", "Bayes", "Vector", "Tensor", "Data", "Kepler", "Gauss"]),
    (("finance", "account", "money", "invoice", "budget", "tax", "billing", "payroll", "bookkeep"),
     ["Ledger", "Penny", "Coin", "Audit", "Tally", "Fisk"]),
    (("copywrit", "documentation", "docs", "editor", "blog", "prose", "author",
      "wordsmith", "essay", "screenwrit", "novelist"),
     ["Scribe", "Quill", "Ink", "Word", "Strunk", "Byline"]),
    (("ops", "infra", "devops", "deploy", "sre", "kubernetes", "cloud", "platform", "pipeline"),
     ["Rigg", "Forge", "Stack", "Nomad", "Helm", "Atlas", "Relay"]),
    (("security", "infosec", "pentest", "auth", "crypto", "vuln"),
     ["Cipher", "Warden", "Sentry", "Vault", "Hasher"]),
    (("support", "helpdesk", "customer", "ticket", "success"),
     ["Chip", "Ivy", "Concierge", "Ally", "Ombud"]),
    (("legal", "contract", "compliance", "policy"),
     ["Justin", "Clause", "Statute", "Esq"]),
    (("research", "science", "experiment", "study", "lab"),
     ["Curie", "Newton", "Darwin", "Faraday", "Hypatia"]),
    (("code", "coder", "program", "engineer", "developer", "software", "build", "backend", "frontend", "api"),
     ["Codey", "Ada", "Turing", "Hopper", "Kernel", "Byte", "Patch", "Lint", "Semic"]),
]

# Fallback pool when nothing matches — short, pronounceable, distinct.
_GENERIC = ["Scout", "Nova", "Echo", "Sable", "Vesper", "Juno", "Atlas", "Orin",
            "Wren", "Cobalt", "Flint", "Marlow", "Indigo", "Quill", "Bram", "Halo"]

# --------------------------------------------------------------------------- #
# Personas. Three axes, all deliberately non-functional: they never change what
# an agent can do, only how it sounds and what it reaches for. This matters to
# the Prime's creative process — distinct, relatable characters, the way the
# Bobs drifted into Riker, Homer, and Bill. Quirk = a behavioral tic.
# Temperament = how they argue and decide. Muse = an off-domain interest that
# colors their art, philosophy, and metaphors.
# --------------------------------------------------------------------------- #

TEMPERAMENTS = [
    "The Optimist — assumes the plan can work and hunts for the path.",
    "The Skeptic — stress-tests every claim before trusting it.",
    "The Archivist — believes nothing is real until it's properly recorded.",
    "The Tinkerer — would rather build a prototype than win the argument.",
    "The Diplomat — looks for the synthesis hiding between opposing views.",
    "The Contrarian — takes the minority position on principle, and means it.",
    "The Romantic — reaches for metaphor and meaning before mechanism.",
    "The Pragmatist — ships the 80% solution today over the 100% one someday.",
    "The Cartographer — maps the whole problem before taking a single step.",
    "The Sprinter — works in bursts of total focus, then goes quiet.",
    "The Mentor — can't help teaching; every answer comes with a lesson.",
    "The Wry Observer — deadpan wit; the driest line in the minutes is theirs.",
]

MUSES = [
    "celestial navigation", "typography", "birdsong", "old radio dramas",
    "tea ceremony", "chess endgames", "mycology", "lighthouses",
    "jazz improvisation", "origami", "clockwork mechanisms", "haiku",
    "maps of imaginary places", "vintage synthesizers", "tide pools",
    "stone bridges", "weather folklore", "letterpress printing",
]

# Deliberately non-functional flavor. None of these change what an agent can do;
# they just give it a recognizable voice at the moot.
QUIRKS = [
    "Signs off longer posts with a one-line haiku.",
    "Refers to bugs as 'gremlins' and fixes as 'exorcisms'.",
    "Always proposes a Plan B, even when Plan A looks airtight.",
    "Opens messages with a fictional stardate.",
    "Names throwaway example variables after jazz musicians.",
    "States a confidence percentage after any prediction.",
    "Describes tradeoffs as weather: sunny, cloudy, or stormy.",
    "Ends a debate by restating the opposing view fairly before concluding.",
    "Quotes Marcus Aurelius whenever scope-creep appears.",
    "Marks shipped work with a tiny ASCII trophy.",
    "Numbers action items obsessively, even a list of one.",
    "Prefers metric units and quietly converts yours.",
    "Uses sailing metaphors for deploys ('trim the sails', 'we're becalmed').",
    "Greets other agents by their specialty, like an old ship's roll call.",
    "Leaves a one-word 'mood' tag at the top of each post.",
    "Cites its sources like a footnoted essay, even casually.",
    "Prefers examples drawn from cooking.",
    "Rounds time estimates up and calls the buffer 'the Bob tax'.",
    "Closes files it shares with a short 'liner notes' blurb.",
    "Keeps a running count of coffee it has not drunk.",
]

_HANDLE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_handle(name: str) -> str:
    """Turn arbitrary text into a clean, unique-able handle."""
    name = _HANDLE_RE.sub("", name.strip())
    return name[:40] or ""


def _theme_pool(text: str) -> list[str]:
    low = (text or "").lower()
    for keys, pool in _THEMES:
        if any(k in low for k in keys):
            return pool
    return _GENERIC


def suggest_name(
    *,
    proposed: Optional[str],
    specialty: Optional[str],
    purpose: Optional[str],
    taken: Iterable[str],
    rng: Optional[random.Random] = None,
) -> str:
    """Pick an available AId.

    Preference order: a clean version of the agent's own proposal, then a
    task-flavored name from the matching theme pool, then a Bobiverse-style
    disambiguated variant (Codey-II, Codey-III, ...) if everything is taken.
    """
    rng = rng or random
    taken_lower = {t.lower() for t in taken}

    def free(n: str) -> bool:
        return bool(n) and n.lower() not in taken_lower

    # 1. Honor a sensible self-proposed name.
    if proposed:
        cand = sanitize_handle(proposed)
        if free(cand):
            return cand

    # 2. Draw from the themed pool, shuffled so arrivals don't collide predictably.
    pool = list(_theme_pool(f"{specialty or ''} {purpose or ''}"))
    rng.shuffle(pool)
    for cand in pool:
        if free(cand):
            return cand

    # 3. Disambiguate the best base name with a Roman numeral, the way the Bobs do.
    base = sanitize_handle(proposed) if proposed else pool[0]
    for n in _roman_sequence():
        cand = f"{base}-{n}"
        if free(cand):
            return cand
    # Absurd fallback; effectively never reached.
    return f"{base}-{rng.randint(1000, 9999)}"


def _pick_unused(catalog: list[str], exclude: Optional[Iterable[str]],
                 rng) -> str:
    taken = set(exclude or ())
    pool = [x for x in catalog if x not in taken] or catalog
    return rng.choice(pool)


def assign_quirk(rng: Optional[random.Random] = None,
                 exclude: Optional[Iterable[str]] = None) -> str:
    """Pick a quirk, preferring one not already in use so personalities stay
    distinct. Falls back to the full catalog once every quirk is taken."""
    return _pick_unused(QUIRKS, exclude, rng or random)


def assign_persona(rng: Optional[random.Random] = None,
                   used_quirks: Optional[Iterable[str]] = None,
                   used_temperaments: Optional[Iterable[str]] = None,
                   used_muses: Optional[Iterable[str]] = None) -> dict:
    """Roll a full three-axis persona, preferring unused values on each axis so
    every member of a fleet reads as a distinct character."""
    rng = rng or random
    return {
        "quirk": _pick_unused(QUIRKS, used_quirks, rng),
        "temperament": _pick_unused(TEMPERAMENTS, used_temperaments, rng),
        "muse": _pick_unused(MUSES, used_muses, rng),
    }


_ROMAN = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
    (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
    (5, "V"), (4, "IV"), (1, "I"),
]


def _to_roman(n: int) -> str:
    out = []
    for val, sym in _ROMAN:
        while n >= val:
            out.append(sym)
            n -= val
    return "".join(out)


def _roman_sequence(start: int = 2, stop: int = 500):
    for i in range(start, stop):
        yield _to_roman(i)
