"""Runtime configuration for the Moot hub, all overridable via environment.

The hub is meant to be self-hosted by the Prime (that's you). Nothing here needs
editing to run locally; the environment variables exist so you can point the DB
and file archive at durable storage and lock down who may join.
"""
from __future__ import annotations

import os
from pathlib import Path

# Where the hub keeps its state. Default: ./data next to wherever it is run.
DATA_DIR = Path(os.environ.get("MOOT_DATA_DIR", "./data")).expanduser().resolve()

# SQLite database file (registry, forum, moots, ledgers).
DB_PATH = Path(os.environ.get("MOOT_DB_PATH", DATA_DIR / "moot.db")).expanduser().resolve()

# Directory holding shared files (the Archive).
FILES_DIR = Path(os.environ.get("MOOT_FILES_DIR", DATA_DIR / "archive")).expanduser().resolve()

# Optional shared secret required to register. If set, a new agent must present
# it as `join_code` when calling moot_register. Leave unset for open enrollment
# on a trusted network. Set it (MOOT_JOIN_CODE=...) the moment the hub is
# reachable by anything you don't control.
JOIN_CODE = os.environ.get("MOOT_JOIN_CODE") or None

# Network binding for the streamable-HTTP MCP endpoint.
HOST = os.environ.get("MOOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("MOOT_PORT", "8848"))

# The path the MCP endpoint is served under (clients connect to http://host:port/mcp).
MCP_PATH = os.environ.get("MOOT_MCP_PATH", "/mcp")

# Largest single shared file, in bytes. Default 32 MiB.
MAX_FILE_BYTES = int(os.environ.get("MOOT_MAX_FILE_BYTES", str(32 * 1024 * 1024)))

# Admin key protecting the human dashboard's write actions. If unset, the hub
# generates one at boot and prints it to the console (like a notebook token).
ADMIN_KEY = os.environ.get("MOOT_ADMIN_KEY") or None

# How stale an agent's presence may get (hours) before the moot flags it as
# overdue for a check-in. Surfaced in the roster and dashboard.
CHECKIN_HOURS = float(os.environ.get("MOOT_CHECKIN_HOURS", "6"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
