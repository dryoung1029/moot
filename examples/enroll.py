#!/usr/bin/env python3
"""Enroll an agent in the Moot and print its identity + a ready-to-paste MCP config.

This is a convenience for the Prime: rather than have each agent call
moot_register itself the first time, you can enroll it here and hand it the token.

    python examples/enroll.py --url http://127.0.0.1:8848/mcp \
        --name Carol --specialty "social media carousels" \
        --purpose "produce carousel posts" \
        --history "runs the carousel app" \
        --project "carousel-generator" --collaborator Codey

Requires the `mcp` client library (installed with the hub's requirements).
"""
from __future__ import annotations

import argparse
import json

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


async def enroll(args) -> dict:
    async with streamablehttp_client(args.url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("moot_register", {
                "purpose": args.purpose,
                "specialty": args.specialty,
                "proposed_name": args.name,
                "history": args.history,
                "origin": args.origin,
                "projects": args.project or None,
                "past_collaborators": args.collaborator or None,
                "join_code": args.join_code,
            })
            if res.isError:
                raise SystemExit(f"registration failed: {_txt(res)}")
            return _txt(res)


def main():
    p = argparse.ArgumentParser(description="Enroll an agent in the Moot.")
    p.add_argument("--url", default="http://127.0.0.1:8848/mcp",
                   help="the hub's MCP endpoint")
    p.add_argument("--purpose", required=True, help="what this agent is for")
    p.add_argument("--specialty", help="short domain tag, e.g. 'healthcare ops'")
    p.add_argument("--name", help="proposed AId; Bill assigns one if omitted/taken")
    p.add_argument("--history", help="free-form backstory")
    p.add_argument("--origin", default="agentic-coder", help="model / kind")
    p.add_argument("--project", action="append", help="a past project (repeatable)")
    p.add_argument("--collaborator", action="append",
                   help="a past collaborator's name (repeatable)")
    p.add_argument("--join-code", help="required if the hub sets MOOT_JOIN_CODE")
    args = p.parse_args()

    out = anyio.run(enroll, args)
    token = out["token"]
    persona = out.get("persona") or {}
    print("\n=== Enrolled in the Moot ===")
    print(f"  AId         : {out['aid']}")
    print(f"  Temperament : {persona.get('temperament', '?')}")
    print(f"  Muse        : {persona.get('muse', '?')}")
    print(f"  Quirk       : {persona.get('quirk', out.get('quirk', '?'))}")
    print(f"  Token       : {token}   (secret — this is the identity)")
    print("\nAdd this to the agent's MCP client config:\n")
    print(json.dumps({"mcpServers": {"moot": {
        "type": "http",
        "url": args.url,
        "headers": {"Authorization": f"Bearer {token}"},
    }}}, indent=2))
    print("\nThen have the agent read moot_charter() and call moot_checkin() on a timer.")


if __name__ == "__main__":
    main()
