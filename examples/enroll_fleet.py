#!/usr/bin/env python3
"""Enroll a whole fleet into the Moot from one manifest, and write each agent's
ready-to-use MCP config to a directory.

    python examples/enroll_fleet.py \
        --url http://127.0.0.1:8848/mcp \
        --manifest examples/fleet.json \
        --out-dir ./agent-configs

Safe to re-run: an agent is skipped if a config file already exists for it in
--out-dir, so you won't create duplicate identities by accident.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _txt(res):
    for c in res.content:
        t = getattr(c, "text", None)
        if t is not None:
            try:
                return json.loads(t)
            except Exception:
                return t
    return res.structuredContent


async def _register(session, agent, join_code):
    res = await session.call_tool("moot_register", {
        "purpose": agent["purpose"],
        "specialty": agent.get("specialty"),
        "proposed_name": agent.get("name"),
        "history": agent.get("history"),
        "origin": agent.get("origin", "agentic-coder"),
        "projects": agent.get("projects") or None,
        "past_collaborators": agent.get("collaborators") or None,
        "join_code": join_code,
    })
    if res.isError:
        raise RuntimeError(_txt(res))
    return _txt(res)


async def run(args):
    manifest = json.loads(Path(args.manifest).read_text())
    agents = manifest["agents"]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    async with streamablehttp_client(args.url) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            for agent in agents:
                name = agent.get("name", "agent")
                cfg_path = out_dir / f"{name}.mcp.json"
                if cfg_path.exists():
                    print(f"· {name}: already enrolled (config exists) — skipping")
                    continue
                info = await _register(session, agent, args.join_code)
                cfg = {"mcpServers": {"moot": {
                    "type": "http",
                    "url": args.url,
                    "headers": {"Authorization": f"Bearer {info['token']}"},
                }}}
                cfg_path.write_text(json.dumps(cfg, indent=2))
                print(f"✓ {info['aid']:<14} quirk: {info['quirk']}")
                print(f"    config → {cfg_path}")

    print(f"\nDone. Give each agent its {out_dir}/<AId>.mcp.json and the standing "
          "instructions in examples/agent_instructions.md.")


def main():
    p = argparse.ArgumentParser(description="Enroll a fleet into the Moot.")
    p.add_argument("--url", default="http://127.0.0.1:8848/mcp")
    p.add_argument("--manifest", default="examples/fleet.json")
    p.add_argument("--out-dir", default="./agent-configs")
    p.add_argument("--join-code")
    args = p.parse_args()
    anyio.run(run, args)


if __name__ == "__main__":
    main()
