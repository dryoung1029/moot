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


def push_prime(title: str, body: str) -> None:
    """Buzz the Prime's phone (ntfy topic set via MOOT_PRIME_PUSH_URL)."""
    from . import config
    if not config.PRIME_PUSH_URL:
        return
    threading.Thread(target=_ntfy, args=(config.PRIME_PUSH_URL, title, body),
                     daemon=True).start()
