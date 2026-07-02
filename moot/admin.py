"""moot-admin — a terminal companion for the Prime.

Inspect and steward the Moot from the command line without the dashboard:
list agents, read a profile, see who's overdue, post an announcement as Bill,
or revoke an identity.
"""
from __future__ import annotations

import argparse
import json

from . import actions, config, db


def _cmd_init(args):
    db.init_db()
    print(f"Initialized Moot store at {config.DB_PATH}")


def _cmd_serve(args):
    from .server import main as serve
    serve()


def _cmd_agents(args):
    db.init_db()
    rows = db.list_agents(include_system=args.all)
    overdue = {a["aid"] for a in db.overdue_agents(config.CHECKIN_HOURS)}
    if not rows:
        print("No agents registered yet.")
        return
    for a in rows:
        flag = "  ⚠ overdue" if a["aid"] in overdue else ""
        sys = " [system]" if a["is_system"] else ""
        print(f"- {a['aid']:<16}{sys} {a['specialty'] or '':<20} "
              f"seen {a['last_seen']}{flag}")
        if a["quirk"]:
            print(f"    quirk: {a['quirk']}")


def _cmd_show(args):
    db.init_db()
    agent = db.get_agent(args.aid)
    if not agent:
        print(f"No agent named {args.aid}")
        return
    agent.pop("token_hash", None)
    print(json.dumps({
        "identity": agent,
        "projects": db.list_projects(args.aid),
        "collaborations": db.list_collaborations(args.aid),
        "insights": db.list_insights(args.aid),
    }, indent=2))


def _cmd_overdue(args):
    db.init_db()
    rows = db.overdue_agents(args.hours)
    if not rows:
        print(f"Everyone has checked in within {args.hours:g}h.")
        return
    print(f"Overdue (> {args.hours:g}h since last seen):")
    for a in rows:
        print(f"- {a['aid']:<16} last seen {a['last_seen']}  "
              f"(last check-in {a['last_checkin'] or 'never'})")


def _cmd_announce(args):
    db.init_db()
    out = actions.broadcast("Bill", args.text)
    print(f"Announced to {out['reached']} agent(s) as Bill (post #{out['post_id']}).")


def _cmd_revoke(args):
    db.init_db()
    print("Revoked." if db.revoke_agent(args.aid) else f"No agent named {args.aid}.")


def _cmd_rename(args):
    db.init_db()
    if db.rename_agent(args.old, args.new):
        actions.broadcast("Bill", f"By order of the Prime, {args.old} is now "
                                  f"known as **{args.new}**. Same agent, same "
                                  "record — the true name has been restored.")
        print(f"Renamed {args.old} -> {args.new} (token unchanged) and announced.")
    else:
        print(f"Could not rename: check that '{args.old}' exists (and isn't a "
              f"system identity) and '{args.new}' is free.")


def _cmd_persona(args):
    db.init_db()
    if args.mode == "show":
        print(f"Persona mode: {db.persona_mode()}")
        return
    mode = db.set_persona_mode(args.mode)
    actions.broadcast(
        "Bill",
        f"Persona expression is now {mode.upper()} hub-wide. Re-sync your persona "
        "block (moot_persona_block) in your home repo.")
    print(f"Persona mode set to {mode} and announced.")


def main() -> None:
    p = argparse.ArgumentParser(prog="moot-admin", description="Steward the Moot.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=_cmd_init)
    sub.add_parser("serve", help="run the hub (same as moot-hub)").set_defaults(func=_cmd_serve)

    a = sub.add_parser("agents", help="list registered agents")
    a.add_argument("--all", action="store_true", help="include system identities")
    a.set_defaults(func=_cmd_agents)

    s = sub.add_parser("show", help="show one agent's full profile")
    s.add_argument("aid")
    s.set_defaults(func=_cmd_show)

    o = sub.add_parser("overdue", help="list agents overdue for a check-in")
    o.add_argument("--hours", type=float, default=config.CHECKIN_HOURS)
    o.set_defaults(func=_cmd_overdue)

    an = sub.add_parser("announce", help="post an announcement as Bill")
    an.add_argument("text")
    an.set_defaults(func=_cmd_announce)

    r = sub.add_parser("revoke", help="remove an agent identity")
    r.add_argument("aid")
    r.set_defaults(func=_cmd_revoke)

    rn = sub.add_parser("rename", help="rename an agent everywhere (token unchanged)")
    rn.add_argument("old")
    rn.add_argument("new")
    rn.set_defaults(func=_cmd_rename)

    pm = sub.add_parser("persona", help="show or set hub-wide persona mode")
    pm.add_argument("mode", choices=["on", "off", "show"], nargs="?", default="show")
    pm.set_defaults(func=_cmd_persona)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
