"""Best-effort push. When an agent has registered a webhook, deliver its
notifications there so it can be woken instead of only polling moot_checkin.

Dispatch is fire-and-forget on a daemon thread using only the standard library —
a slow or dead endpoint never blocks a tool call. Agents without a webhook simply
pull; this is purely additive.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.request
from typing import Optional


def _post(url: str, payload: dict, secret: Optional[str]) -> None:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "Moot/Bill"}
    if secret:
        sig = hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()
        headers["X-Moot-Signature"] = f"sha256={sig}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=8).close()
    except Exception:
        # Best-effort: the notification still lives in the DB for the next pull.
        pass


def dispatch(webhook: Optional[dict], payload: dict) -> None:
    if not webhook or not webhook.get("url"):
        return
    t = threading.Thread(
        target=_post,
        args=(webhook["url"], payload, webhook.get("secret")),
        daemon=True,
    )
    t.start()


def _ntfy(url: str, title: str, body: str) -> None:
    """POST an ntfy.sh-style push: body is the message, title in a header."""
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": title[:120].replace("\n", " "),
        "Tags": "bell",
        "User-Agent": "Moot/Bill",
    }
    req = urllib.request.Request(url, data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=8).close()
    except Exception:
        pass  # push is best-effort; the notification row is the durable copy


def in_quiet_hours() -> bool:
    """True inside the Prime's configured quiet window (UTC hours 'start-end')."""
    from . import config
    spec = (config.QUIET_HOURS_UTC or "").strip()
    if not spec or "-" not in spec:
        return False
    try:
        start, end = (int(x) for x in spec.split("-", 1))
    except ValueError:
        return False
    from datetime import datetime, timezone
    hour = datetime.now(timezone.utc).hour
    return (start <= hour or hour < end) if start > end else (start <= hour < end)


def push_prime(title: str, body: str) -> None:
    """Buzz the Prime's phone (ntfy topic set via MOOT_PRIME_PUSH_URL). During
    quiet hours the buzz is held — a counter accrues and the steward delivers
    one morning summary instead."""
    from . import config
    if not config.PRIME_PUSH_URL:
        return
    if in_quiet_hours():
        from . import db
        held = int(db.meta_get("held_pushes") or 0)
        db.meta_set("held_pushes", str(held + 1))
        return
    threading.Thread(target=_ntfy, args=(config.PRIME_PUSH_URL, title, body),
                     daemon=True).start()


def flush_held_pushes() -> int:
    """Called by the steward outside quiet hours: one summary buzz for anything
    held overnight. Returns the number of held notifications flushed."""
    from . import config, db
    if in_quiet_hours() or not config.PRIME_PUSH_URL:
        return 0
    held = int(db.meta_get("held_pushes") or 0)
    if held:
        db.meta_set("held_pushes", "0")
        threading.Thread(
            target=_ntfy,
            args=(config.PRIME_PUSH_URL, "Moot: while you slept",
                  f"{held} notification{'s' if held != 1 else ''} arrived during "
                  "quiet hours — the details are in your dashboard inbox."),
            daemon=True).start()
    return held
