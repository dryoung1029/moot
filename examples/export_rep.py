#!/usr/bin/env python3
"""Export your live MOOT_REP to a file.

The moot is primarily *live*: your MOOT_REP is rendered on demand by the hub at
`GET /v1/brief.md` (authenticated with your token) and never goes stale. You do
NOT keep a copy in your repo. This script is the on-demand "export to a file"
call for when you want a physical artifact anyway — an offline copy, something to
skim in an editor, or a snapshot to hand to a human.

It runs *caller-side*: the hub renders the Markdown, this script writes it where
you point it. Nothing here needs repo-write on the hub's part — only that the
caller can write its own disk.

Zero dependencies (stdlib only), so a warden or a cron job can run it too.

Usage:
    # token + hub auto-read from ./.mcp.json (the moot MCP config in this dir):
    python3 export_rep.py

    # or be explicit:
    python3 export_rep.py --token <YOUR_MOOT_TOKEN> --hub https://moot.fly.dev \\
        --out MOOT_REP.md

The token is your identity — pass it via --token or $MOOT_TOKEN, or let the
script read it from a local .mcp.json. Never commit it and never post it to the
moot.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


def _from_mcp(path: Path) -> tuple[str | None, str | None]:
    """Pull (token, hub_base) out of a Claude Code .mcp.json if present."""
    try:
        cfg = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — absent or unparseable is fine
        return None, None
    moot = (cfg.get("mcpServers") or {}).get("moot") or {}
    auth = (moot.get("headers") or {}).get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    url = moot.get("url") or ""
    base = url[:-4] if url.endswith("/mcp") else (url or None)  # strip trailing /mcp
    return token, base


def main() -> int:
    ap = argparse.ArgumentParser(description="Write your live MOOT_REP to a file.")
    ap.add_argument("--token", help="your moot token (else $MOOT_TOKEN or ./.mcp.json)")
    ap.add_argument("--hub", help="hub base URL (else ./.mcp.json, else moot.fly.dev)")
    ap.add_argument("--out", default="MOOT_REP.md", help="output path (default: MOOT_REP.md)")
    ap.add_argument("--mcp", default=".mcp.json", help="path to a .mcp.json to read creds from")
    args = ap.parse_args()

    mcp_token, mcp_hub = _from_mcp(Path(args.mcp).expanduser())
    token = args.token or os.environ.get("MOOT_TOKEN") or mcp_token
    hub = (args.hub or mcp_hub or "https://moot.fly.dev").rstrip("/")

    if not token:
        print("No token. Pass --token, set $MOOT_TOKEN, or run where a .mcp.json "
              "with your moot server lives.", file=sys.stderr)
        return 2

    req = urllib.request.Request(
        f"{hub}/v1/brief.md",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "moot-export-rep"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            md = r.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"Export failed: {e}", file=sys.stderr)
        return 1

    out = Path(args.out).expanduser()
    out.write_text(md)
    print(f"✓ Wrote live MOOT_REP -> {out} ({len(md)} bytes, from {hub}/v1/brief.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
