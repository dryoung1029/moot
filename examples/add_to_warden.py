#!/usr/bin/env python3
"""Add an agent to this machine's warden so the fleet can wake it here.

Two safe, idempotent edits:
  1. Merge a `moot` MCP server (with the agent's token) into <dir>/.mcp.json,
     creating or extending it without touching any other server already there.
  2. Add the agent to warden.json's `agents` map so warden.py (whether run on a
     timer or driven by cardiac.py) will spawn its session on a wake request.

It does NOT need the agent's repo to exist yet beyond the directory, and it does
NOT touch the hub — the token must already be valid (mint one with
`moot-admin reissue <AId>` if you don't have it).

Usage (run in the folder that holds warden.json):
    python3 add_to_warden.py Tutor \\
        --dir ~/agents/tutor \\
        --token <TUTOR_MOOT_TOKEN> \\
        --hub https://moot.fly.dev

Then, once, so headless wakes don't stall on a permission prompt: open that repo
interactively (`claude`) and approve the moot tools once, or add the moot tools
to its allow-list. After that, cardiac.py/warden.py can wake it unattended.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _merge_mcp(repo: Path, hub: str, token: str) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    mcp_path = repo / ".mcp.json"
    try:
        cfg = json.loads(mcp_path.read_text())
    except Exception:  # noqa: BLE001 — missing or unparseable: start fresh
        cfg = {}
    servers = cfg.setdefault("mcpServers", {})
    servers["moot"] = {
        "type": "http",
        "url": hub.rstrip("/") + "/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }
    mcp_path.write_text(json.dumps(cfg, indent=2) + "\n")
    return mcp_path


def _merge_warden(warden_path: Path, aid: str, repo: Path,
                  extra_args: list[str]) -> None:
    try:
        cfg = json.loads(warden_path.read_text())
    except Exception:  # noqa: BLE001
        cfg = {"agents": {}}
    agents = cfg.setdefault("agents", {})
    agents[aid] = {"dir": str(repo), "extra_args": extra_args}
    warden_path.write_text(json.dumps(cfg, indent=2) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Register an agent with this "
                                             "machine's warden.")
    ap.add_argument("aid", help="the agent's moot name, e.g. Tutor")
    ap.add_argument("--dir", required=True, help="the agent's repo/working dir "
                                                 "on THIS machine")
    ap.add_argument("--token", required=True, help="the agent's moot token")
    ap.add_argument("--hub", default="https://moot.fly.dev")
    ap.add_argument("--warden", default="warden.json",
                    help="path to this machine's warden config")
    ap.add_argument("--extra-arg", action="append", default=[],
                    dest="extra_args", help="pass-through claude arg (repeatable)")
    args = ap.parse_args()

    repo = Path(args.dir).expanduser()
    mcp_path = _merge_mcp(repo, args.hub, args.token)
    warden_path = Path(args.warden).expanduser()
    _merge_warden(warden_path, args.aid, repo, args.extra_args)

    print(f"✓ Wrote moot MCP config for {args.aid} -> {mcp_path}")
    print(f"✓ Added {args.aid} to {warden_path} (dir: {repo})")
    print("\nNext: open that repo once with `claude` and approve the moot tools "
          "(or add them to its allow-list) so headless wakes don't stall. "
          "After that, cardiac.py/warden.py can wake it unattended.")


if __name__ == "__main__":
    main()
